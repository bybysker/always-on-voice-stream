"""The always-on assistant: wires capture → transcription → extraction → sinks/agents.

::

    mic ─┐                                   ┌─ Extractor (Claude) ─ ItemTracker ─┬─ Dispatcher → Notion/Monday/…
         ├─ Transcriber ─ EchoFilter ─ Meeting ┤                                   └─ AgentRunner → agents doing tasks
    sys ─┘                                   └─ SessionStore (transcript.md, meeting.json, notes.md)

One :class:`Assistant` runs for the whole day. Audio capture and transcription
are continuous; transcript segments are routed to the current
:class:`MeetingSession`, which is created on the first speech and finalized
(summary, push, notes) after ``always_on.idle_split_minutes`` of silence or
when you end it.

The Mac app can instead run one assistant per meeting you start (:attr:`Assistant.manual`): the
meeting opens with listening and listening ends with it, however it ends; a long silence asks
whether you are still in it, then ends it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import sys
import time
from collections.abc import AsyncIterator, Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

from .agents.runner import AgentRunner, Engine
from .audio import AudioSource, CaptureSetupError, Clock, build_source
from .audio.base import aclose_quietly
from .brain.extractor import Extractor
from .brain.llm import LLM, ClaudeLLM, LLMUnavailable
from .brain.summarizer import is_short_clip, known_language, rerender, summarize
from .brain.tracker import ItemTracker
from .config import Config
from .costs import CostMeter
from .dispatch import Dispatcher
from .events import EventBus
from .models import AudioFrame, Event, ItemKind, Meeting, ScreenNote, TranscriptSegment, utcnow
from .sinks.base import Sink, build_sinks
from .storage import SessionStore, private_dir
from .transcribe import Transcriber, TranscriptionError, build_transcriber
from .transcribe.echo import EchoFilter

log = logging.getLogger(__name__)

LATE_AGENT_SLACK_S = 30.0
"""Extra wait for agents still working after a meeting, on top of their own time limits."""
SILENT_SYSTEM_AFTER_S = 120.0
"""Your speech heard while system audio has been nothing but digital silence before we warn about it."""
RESTART_DELAY_S = 2.0
"""First wait before restarting a failed live capture; doubles on each failure in a row, up to a minute."""
FORCE_GRACE_S = 3.0
"""After ``stop(force=True)``: how long a step already under way (the final transcription, a push in
flight) still gets before it is cancelled. Steps not started yet are skipped."""


_SLEEP_CLOCK: int | None = (
    getattr(time, "CLOCK_MONOTONIC", None) if sys.platform == "darwin"  # unlike mach_absolute_time, runs on in sleep
    else getattr(time, "CLOCK_BOOTTIME", None) if sys.platform.startswith("linux")
    else None
)  # fmt: skip


def sleep_inclusive_now() -> float:
    """Seconds on a clock that keeps running while the computer sleeps.

    ``time.monotonic()`` stops while a Mac sleeps (CPython reads ``mach_absolute_time``; Linux's
    CLOCK_MONOTONIC skips suspend too), and with it the meeting :class:`~voicestream.audio.Clock` and
    asyncio's timers. Measured on it, "10 minutes of silence" could take all night with the lid closed,
    and the evening's meeting would absorb the next morning's. Elsewhere: the wall clock."""
    return time.clock_gettime(_SLEEP_CLOCK) if _SLEEP_CLOCK is not None else time.time()


def _moved(path: str, old: Path, new: Path) -> str:
    p = Path(path)
    return str(new / p.relative_to(old)) if p.is_relative_to(old) else path


class MeetingSession:
    """Everything scoped to one meeting."""

    wrapup_wait_s = 10.0
    """How long :meth:`finish` waits for the extraction and push loops before the final pass."""

    def __init__(self, app: Assistant, t0: float, title: str | None = None, started_at: datetime | None = None,
                 auto: bool = False) -> None:
        self.app = app
        cfg = app.cfg
        self.t0 = t0
        self.auto = auto
        """Opened by always-on listening on the first speech heard, not by you: a few stray lines are a
        short clip rather than a meeting (see :func:`~voicestream.brain.summarizer.is_short_clip`)."""
        self.meeting = Meeting(title=title, language=known_language(cfg))
        filed_at = self.meeting.started_at
        if started_at is not None:
            self.meeting.started_at = started_at
        self.store = SessionStore(cfg.data_path, self.meeting, store_audio=cfg.capture.store_audio, filed_at=filed_at)
        self.replayed_at = started_at
        """When an imported transcript's meeting took place (the folder keeps the import time)."""
        day: date | None = None
        if started_at is not None:
            # "d'ici vendredi" means the Friday after the meeting, not after the import
            day = started_at.astimezone(ZoneInfo(cfg.timezone) if cfg.timezone else None).date()
        self.day = day
        self.tracker = ItemTracker(self.meeting, app.bus, cfg.user_name, today=day)
        self.extractor = Extractor(cfg, self.meeting, self.tracker, app.llm, app.costs, app.bus, today=self.speech_day)
        self.dispatcher = Dispatcher(cfg, self.meeting, app.sinks, app.bus, on_change=self.save_soon)
        self.agents = AgentRunner(
            cfg,
            self.meeting,
            self.store.agents_dir,
            app.bus,
            app.costs,
            engine=app.agent_engine,
            llm=app.llm,
            on_change=self.save_soon,
            commands_trusted=lambda: not app.echo_unprotected,
        )
        self.last_speech_wall = app.wall()
        """When speech was last heard, on :attr:`Assistant.wall` (time asleep counts as silence)."""
        self.last_heard_at = utcnow()
        self.asked = False
        """"Still in a meeting?" was asked during the silence going on now (:meth:`Assistant._check_silence`)."""
        self.finished = False
        self._tasks: list[asyncio.Task[Any]] = []
        self._save_handle: asyncio.TimerHandle | None = None
        self._unlisten = app.bus.listen(self._on_event)

    # --------------------------------------------------------------- lifecycle

    def start(self) -> None:
        self._tasks = [
            asyncio.ensure_future(self.extractor.run()),
            asyncio.ensure_future(self.dispatcher.run()),
            asyncio.ensure_future(self.agents.run()),
        ]
        self.app.bus.publish(
            "meeting.started", meeting_id=self.meeting.id, title=self.meeting.title, dir=str(self.store.dir)
        )
        self.store.save()
        log.info("meeting started → %s", self.store.dir)

    def _on_event(self, ev: Event) -> None:
        if ev.data.get("meeting_id") != self.meeting.id:
            return
        if ev.type in ("item.added", "item.updated"):
            item_id = (ev.data.get("item") or {}).get("id")
            item = self.meeting.items.get(item_id) if item_id else None
            if item is not None:
                self.agents.consider(item)
                self.save_soon()

    def heard(self) -> None:
        """Speech just came in: the silence that ends the meeting starts over."""
        self.last_speech_wall = self.app.wall()
        self.last_heard_at = utcnow()
        self.asked = False  # the next long silence may ask again

    def speech_day(self) -> date:
        """The day "demain" / "by Friday" are relative to: the meeting's own date for an import, else the
        date speech was last heard — not the wrap-up's, which comes the next morning when the Mac slept."""
        if self.day is not None:
            return self.day
        tz = ZoneInfo(self.app.cfg.timezone) if self.app.cfg.timezone else None
        return self.last_heard_at.astimezone(tz).date()

    def add_segment(self, seg: TranscriptSegment) -> None:
        seg = seg.model_copy(update={"t_start": max(0.0, seg.t_start - self.t0), "t_end": max(0.0, seg.t_end - self.t0)})
        self.meeting.segments.append(seg)
        self.store.append_segment(seg)
        self.extractor.on_segment(seg)
        self.app.bus.publish(
            "segment", meeting_id=self.meeting.id, segment=seg.model_dump(mode="json"), index=len(self.meeting.segments) - 1
        )

    def add_screen(self, note: ScreenNote) -> None:
        note = note.model_copy(update={"t": max(0.0, note.t - self.t0)})
        self.meeting.screen_notes.append(note)
        self.extractor.on_screen(note)
        self.app.bus.publish("screen", meeting_id=self.meeting.id, note=note.model_dump(mode="json"))
        self.save_soon()

    def save_soon(self, delay: float = 1.0) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.store.save()
            return
        if self._save_handle is None:
            self._save_handle = loop.call_later(delay, self._save_now)

    def _save_now(self) -> None:
        self._save_handle = None
        self.store.save()

    def flush_save(self) -> None:
        """Write a pending :meth:`save_soon` now (shutting down: the loop won't run it)."""
        if self._save_handle is not None:
            self._save_handle.cancel()
            self._save_now()

    async def finish(self, reason: str = "ended") -> Path | None:
        """Final extraction, summary, push, notes. Idempotent."""
        if self.finished:
            return None
        self.finished = True
        cfg = self.app.cfg
        self.app.bus.publish("meeting.ending", meeting_id=self.meeting.id, reason=reason)
        self.extractor.stop()
        self.dispatcher.stop()
        if self._tasks:
            # Wait, but never cancel: a push cancelled mid-request may exist in the sink without a ref here,
            # and finalize() would create it again (it takes the dispatcher's lock, so it waits for a round
            # still in flight); flush() likewise waits for an extraction call that is already paid for.
            # Only a forced stop (quitting the app) stops waiting.
            await self.app.unless_forced(asyncio.wait(self._tasks[:2], timeout=self.wrapup_wait_s), grace=0)
        if not self.meeting.segments and not self.meeting.items:
            await self._shutdown_tasks()
            self._unlisten()
            self.store.close()
            self._cleanup_empty()
            # the dashboard waits for this to leave "wrapping up…"
            self.app.bus.publish("meeting.ended", meeting_id=self.meeting.id, title=self.meeting.title, notes=None, items=0)
            return None
        try:
            await self.app.unless_forced(self.extractor.flush())
        except Exception:
            log.exception("final extraction failed")
        if self.agents.busy and cfg.agents.finish_wait_s > 0 and not self.app.forced:
            log.info("waiting up to %.0fs for running agents…", cfg.agents.finish_wait_s)
            await self.app.unless_forced(self.agents.drain(cfg.agents.finish_wait_s), grace=0)
        if self.agents.busy and self.app.forced:
            await self.agents.cancel_all()  # quitting: marked failed ("cancelled"), the notes say so
        if self.replayed_at is not None:
            spoken = max((s.t_end for s in self.meeting.segments), default=0.0)
            self.meeting.ended_at = self.replayed_at + timedelta(seconds=spoken)
        elif reason in ("idle", "silence"):
            # it ended when the talking stopped, not N minutes later (or the next morning, when the Mac slept)
            self.meeting.ended_at = self.last_heard_at
        else:
            self.meeting.ended_at = utcnow()
        # the extraction above has run, so a "Hey Claude" request in it is an item (and its agent runs)
        short = self.auto and is_short_clip(cfg, self.meeting)
        if short:
            self.meeting.short_clip = True
            log.info("only %.0fs of speech in %d line(s): kept as a short clip, no summary and no notes sent",
                     self.meeting.speech_seconds(), len(self.meeting.segments))
        try:
            summarized, notes = await self.app.unless_forced(
                summarize(
                    cfg,
                    self.meeting,
                    self.tracker,
                    self.app.llm if cfg.brain.provider == "anthropic" and not short else None,
                    today=self.speech_day(),
                    budget_ok=not self.app.costs.over_budget(),
                )
            )
            if not summarized:  # a forced stop: basic notes now; `voicestream summarize` can redo them
                notes = rerender(self.meeting, cfg.user_name)
        except Exception:
            log.exception("summary failed")
            notes = rerender(self.meeting, cfg.user_name)
        if self._rename():
            notes = rerender(self.meeting, cfg.user_name)  # the agents' files moved with the folder
        self._save_notes(notes)
        try:
            # a short clip's to-dos go out like any others, its notes stay on this computer
            await self.app.unless_forced(self.dispatcher.finalize(None if short else notes))
        except Exception:
            log.exception("final push failed")
        if not self.app.forced:
            # a transient failure at wrap-up (Wi-Fi still reconnecting after the Mac woke up…) gets more goes
            self.app.track(asyncio.ensure_future(self.dispatcher.retry_failed()))
        self.store.save()
        notes_path = self.store.dir / "notes.md"
        self.app.bus.publish(
            "meeting.ended",
            meeting_id=self.meeting.id,
            title=self.meeting.title,
            notes=str(notes_path),
            dir=str(self.store.dir),
            items=len([i for i in self.meeting.items.values() if not i.dismissed]),
            short_clip=short,
        )
        if self.agents.busy:
            # agents keep working; refresh the notes when they're done
            self.app.track(asyncio.ensure_future(self._late_agents()))
        else:
            await self._shutdown_tasks()
            self._unlisten()
        self.store.close()
        return notes_path

    def _save_notes(self, notes: str) -> None:
        try:
            self.store.save_notes(notes)
        except OSError:
            # the sinks don't need the local copy: keep wrapping up (push, events, shutdown)
            log.exception("could not write notes.md")

    async def _late_agents(self) -> None:
        ac = self.app.cfg.agents
        while self.agents.busy:
            # agents run max_concurrent at a time, each within its own time limit: wait for the whole queue
            finished = self.agents.finished
            waves = math.ceil(self.agents.pending / max(1, ac.max_concurrent))
            await self.app.unless_forced(self.agents.drain(waves * ac.timeout_s + LATE_AGENT_SLACK_S), grace=0)
            if self.app.forced or (self.agents.busy and self.agents.finished == finished):
                break  # quitting, or none of them finished in time
        if self.agents.busy:
            await self.agents.cancel_all()  # marked failed ("cancelled") rather than "running" for good
        self._rename()  # held back while the agents were writing into the folder
        notes = rerender(self.meeting, self.app.cfg.user_name)
        self._save_notes(notes)
        self.store.save()
        await self.app.unless_forced(
            self.dispatcher.sync(require_stable=False, only={i.id for i in self.meeting.items.values() if i.pushed})
        )
        await self._shutdown_tasks()
        self._unlisten()
        self.app.bus.publish("meeting.notes_updated", meeting_id=self.meeting.id, notes=str(self.store.dir / "notes.md"))

    def _rename(self) -> bool:
        """Append the title slug to the folder name, carrying the agents' paths along. Never while an
        agent is working in it: it would keep writing under the old path."""
        if self.agents.busy:
            return False
        old = self.store.dir
        self.store.rename_with_title()
        new = self.store.dir
        if new == old:
            return False
        if self.agents.root == old / "agents":
            self.agents.root = self.store.agents_dir
        for item in self.meeting.items.values():
            run = item.agent
            if run is None or not (run.workspace or run.result_files):
                continue
            run.workspace = _moved(run.workspace, old, new) if run.workspace else None
            run.result_files = [_moved(f, old, new) for f in run.result_files]
            # only the paths changed: refreshes the dashboard's file links, the console stays quiet
            self.app.bus.publish("agent.updated", meeting_id=self.meeting.id, item=item.model_dump(mode="json"), moved=True)
        return True

    async def _shutdown_tasks(self) -> None:
        self.agents.stop()
        for t in self._tasks:
            if not t.done():
                t.cancel()
            with contextlib.suppress(BaseException):
                await t

    def _cleanup_empty(self) -> None:
        try:
            for p in sorted(self.store.dir.rglob("*"), reverse=True):
                if p.is_file() and (
                    p.name in ("transcript.md", "meeting.json", "transcript.jsonl")
                    or (p.name.startswith("audio-") and p.suffix == ".wav")  # capture.store_audio
                ):
                    p.unlink()
                elif p.is_dir():
                    p.rmdir()
            self.store.dir.rmdir()
        except OSError:
            pass


class Assistant:
    def __init__(
        self,
        cfg: Config,
        *,
        sources: list[AudioSource] | None = None,
        source_specs: list[str] | None = None,
        transcriber_factory: Callable[[str], Transcriber] | None = None,
        llm: LLM | None | str = "auto",
        sinks: list[Sink] | None = None,
        agent_engine: Engine | None = None,
        title: str | None = None,
        always_on: bool | None = None,
        bus: EventBus | None = None,
        costs: CostMeter | None = None,
        replay_speed: float = 1.0,
        manual: bool = False,
        ask_after_s: float = 0.0,
        end_after_s: float = 0.0,
    ) -> None:
        self.cfg = cfg
        private_dir(cfg.data_path)
        self.bus = bus or EventBus()
        self.costs = costs or CostMeter(cfg, self.bus)
        self.clock = Clock()
        self.wall: Callable[[], float] = sleep_inclusive_now
        """What the silence that ends a meeting is measured on: unlike :attr:`clock`, it runs while asleep."""
        self.title = title
        self.always_on = (cfg.always_on.idle_split_minutes > 0) if always_on is None else always_on
        self.manual = manual
        """One meeting, started on purpose (the Mac app's "manual" mode): it opens as listening starts and
        listening ends with it, however it ends (Stop, End meeting, a long silence); no silence splits it,
        whatever :attr:`always_on` says. The app may switch it while listening (Settings)."""
        self.ask_after_s = ask_after_s
        """:attr:`manual`: after this long without speech, ask once whether you are still in the meeting
        (event ``meeting.still_there``); ``0``: never."""
        self.end_after_s = end_after_s
        """:attr:`manual`: after this long without speech, end the meeting (reason ``silence``); ``0``: never."""
        self.replay_speed = replay_speed
        if llm == "auto":
            llm = self._make_llm()
        self.llm: LLM | None = llm if not isinstance(llm, str) else None
        self.sinks = sinks if sinks is not None else build_sinks(cfg)
        self.agent_engine = agent_engine
        self._sources = sources
        self._source_specs = source_specs
        self._transcriber_factory = transcriber_factory
        self.current: MeetingSession | None = None
        self.finished_meetings: list[MeetingSession] = []
        self._merge_q: asyncio.Queue[TranscriptSegment | None] = asyncio.Queue()
        self._stop = asyncio.Event()
        self._background: set[asyncio.Task[Any]] = set()
        self._meeting_lock = asyncio.Lock()
        self.echo = EchoFilter(cfg.echo)
        self.sources: list[AudioSource] = []
        self.transcribers: dict[str, Transcriber] = {}
        self.screen_watcher: Any = None
        self._last_level_pub: dict[str, float] = {}
        self.idle_poll_s = 5.0
        self._system_live = False
        self._system_sound = False  # system audio has delivered something other than digital silence
        self._mic_speech_s = 0.0  # your speech heard while it hadn't
        self._silent_warned = False
        self._paused = False
        self._paused_at = 0.0  # on self.wall
        self._force = asyncio.Event()
        self.keep_open = False
        """After a replay/import, keep running (the dashboard) until :meth:`stop`."""
        self.wound_down = asyncio.Event()
        """Set once a stopping :meth:`run` has closed capture and speech-to-text and wrapped up the meeting
        in progress: all that is left is background work (an ended meeting's agents, push retries), which
        can take many minutes. The app starts the next assistant then instead of waiting for it."""
        self._running = False
        """:meth:`run` is capturing, or winding down (until the meeting in progress is wrapped up)."""
        self._done = asyncio.Event()
        """:meth:`run` has returned, or was cancelled: nothing more will be wrapped up."""
        self._end_reason: str | None = None
        """:attr:`manual`: why the meeting, and so listening, is ending (:meth:`end_meeting`)."""
        self._ended_path: Path | None = None
        self._ending_on_silence: MeetingSession | None = None
        """The meeting a long silence is ending (its end is under way: not asked for twice)."""

    # ----------------------------------------------------------------- setup

    def _make_llm(self) -> LLM | None:
        if self.cfg.brain.provider != "anthropic":
            return None
        try:
            return ClaudeLLM(on_usage=self.costs.add_llm)
        except LLMUnavailable as exc:
            log.warning("Claude unavailable (%s): using offline heuristic extraction", exc)
            return None

    def _build_sources(self) -> list[AudioSource]:
        if self._sources is not None:
            return self._sources
        specs = self._source_specs or self.cfg.capture.sources
        return [build_source(spec, self.cfg, self.clock, speed=self.replay_speed) for spec in specs]

    def _make_transcriber(self, source: str) -> Transcriber:
        if self._transcriber_factory is not None:
            t = self._transcriber_factory(source)
        else:
            t = build_transcriber(
                self.cfg,
                source,
                on_usage=self.costs.add_stt,
                on_partial=lambda src, spk, text: self.bus.publish("transcript.partial", source=src, speaker=spk, text=text),
            )
        if hasattr(t, "on_level"):
            t.on_level = self._on_level  # type: ignore[attr-defined]
        return t

    def _on_level(self, source: str, db: float, speech: bool) -> None:
        now = time.monotonic()
        if now - self._last_level_pub.get(source, 0.0) >= 0.25:
            self._last_level_pub[source] = now
            self.bus.publish("level", source=source, db=round(db, 1), speech=speech)

    def _on_backlog(self, source: str, behind_s: float, queued: int, backend: str) -> None:
        """Transcription can't keep up (or caught up again, ``behind_s`` 0): the app's banner shows the warning."""
        self.bus.publish("transcription.backlog", source=source, behind_s=behind_s, queued=queued, backend=backend)
        if behind_s <= 0:
            log.info("%s: speech-to-text has caught up", source)
            return
        if backend in ("whisper-cpp", "local"):
            advice = ("This computer is too slow for the speech model in use: choose a smaller one "
                      "(Settings → Speech-to-text), or a cloud service (OpenAI or Deepgram).")
        else:
            advice = "The speech-to-text service is answering slowly: check the internet connection."
        log.warning("Speech-to-text is falling behind (%s): what was said %.0f s ago is still waiting to be written "
                    "down (%d waiting). %s", self.cfg.source_label(source), behind_s, queued, advice)

    def track(self, task: asyncio.Task[Any]) -> None:
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    @property
    def auto_meetings(self) -> bool:
        """Meetings open on the first speech and close after a silence (always-on, not :attr:`manual`)."""
        return self.always_on and not self.manual

    # ------------------------------------------------------------------- run

    async def run(self) -> None:
        try:
            self.sources = self._build_sources()
            if not self.sources:
                raise RuntimeError("no audio sources configured")
            names = [s.name for s in self.sources]
            self.echo.system_active = "system" in names and "mic" in names
            self._system_live = any(s.name == "system" and s.live for s in self.sources)
            for src in self.sources:
                t = self._make_transcriber(src.name)
                if src.live and hasattr(t, "on_backlog"):  # a file replayed at full speed queues up by design
                    t.on_backlog = self._on_backlog  # type: ignore[attr-defined]
                self.transcribers[src.name] = t
        except BaseException:
            # a missing key or device: the app keeps running and may start another assistant, so the
            # transcribers built so far (a whisper-server child…), the Claude client and the sinks are closed
            await self._close_clients()
            raise
        self.bus.publish(
            "assistant.started",
            sources=[s.describe() for s in self.sources],
            backend=self.cfg.transcription.backend,
            extraction="claude" if self.llm else "heuristic",
            agents=self.cfg.agents.mode,
            always_on=self.auto_meetings,
            manual=self.manual,
        )
        try:
            if not self.auto_meetings or self.title:
                await self._ensure_meeting(self.clock.now())  # a meeting started by hand opens now, not on speech
        except BaseException:
            await self._close_clients()  # its folder couldn't be made (a full disk…): as a failed setup
            raise
        self._running = True
        try:
            await self._capture()
        finally:
            self._running = False
            self._done.set()

    async def _capture(self) -> None:
        """Listen until :meth:`stop` (or until every capture ends), then wind down: the last words, the
        meeting in progress wrapped up, the background work."""
        pipes = [asyncio.ensure_future(self._pipe(src)) for src in self.sources]
        merger = asyncio.ensure_future(self._merger())
        monitor = asyncio.ensure_future(self._idle_monitor())
        screen = asyncio.ensure_future(self._screen()) if self.cfg.screen.enabled else None
        stopper = asyncio.ensure_future(self._stop.wait())
        all_pipes = asyncio.ensure_future(asyncio.gather(*pipes, return_exceptions=True))
        try:
            await asyncio.wait({stopper, all_pipes}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for src in self.sources:
                src.stop()
            # let transcribers flush their last utterances
            await self.unless_forced(asyncio.wait({all_pipes}, timeout=30))
            if not all_pipes.done():
                all_pipes.cancel()
            await self._merge_q.put(None)
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(merger, 10)
            for t in (monitor, screen, stopper):
                if t is not None:
                    t.cancel()
            self._ended_path = await self._end(self._end_reason or "stopped")
            # the background work doesn't need speech-to-text: free it now (a whisper-server and its model
            # in memory) for an assistant started meanwhile (see wound_down); _close_clients does it again
            for t in self.transcribers.values():
                with contextlib.suppress(Exception):
                    await t.aclose()
            self.wound_down.set()
            if self._background:
                log.info("waiting for background work (agents) to finish… Ctrl+C again to abort")
            await self._finish_background()
            await self._linger()
            await self._close_clients()
            self.bus.publish("assistant.stopped", cost=self.costs.snapshot())
        results = all_pipes.result() if all_pipes.done() and not all_pipes.cancelled() else []
        for r in results:
            if isinstance(r, Exception) and not isinstance(r, asyncio.CancelledError):
                log.error("capture pipeline failed: %r", r)

    async def _close_clients(self) -> None:
        for t in self.transcribers.values():
            with contextlib.suppress(Exception):
                await t.aclose()
        if self.llm:
            with contextlib.suppress(Exception):
                await self.llm.aclose()
        for sink in self.sinks:
            with contextlib.suppress(Exception):
                await sink.aclose()

    def stop(self, force: bool = False) -> None:
        """Stop listening; the meeting in progress is wrapped up (summary, pushes) and running agents get
        their time. ``force=True`` (quitting the app) cuts that short so it never takes minutes: agents
        are cancelled, steps not started yet (summary, pushes) are skipped and those under way get
        :data:`FORCE_GRACE_S`. It may follow a normal stop that is taking too long."""
        if force:
            self._force.set()
        self._stop.set()

    @property
    def forced(self) -> bool:
        return self._force.is_set()

    async def unless_forced(self, aw: Any, grace: float | None = None) -> tuple[bool, Any]:
        """Await ``aw`` unless :meth:`stop` is forced: then a step not started is skipped and one under
        way gets ``grace`` seconds (default :data:`FORCE_GRACE_S`) before it is cancelled. Returns
        ``(completed, result)``; the step's own exceptions propagate."""
        grace = FORCE_GRACE_S if grace is None else grace
        if self._force.is_set():
            if asyncio.iscoroutine(aw):
                aw.close()  # never started: no "was never awaited" warning
            return False, None
        task = asyncio.ensure_future(aw)
        forced = asyncio.ensure_future(self._force.wait())
        try:
            await asyncio.wait({task, forced}, return_when=asyncio.FIRST_COMPLETED)
            if not task.done() and grace > 0:
                await asyncio.wait({task}, timeout=grace)
        except BaseException:
            task.cancel()
            raise
        finally:
            forced.cancel()
        if not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
            return False, None
        return True, task.result()

    # ----------------------------------------------------------- pause/resume

    @property
    def paused(self) -> bool:
        return self._paused

    def pause(self) -> bool:
        """Stop hearing anything without ending the meeting: every captured frame becomes digital silence
        of the same length, so the clocks run on, the voice detector closes the utterance in progress and
        nothing more is transcribed (or billed). Returns whether it changed anything."""
        if self._paused:
            return False
        self._paused = True
        self._paused_at = self.wall()
        log.info("paused: nothing is recorded until resumed")
        self.bus.publish("assistant.paused")
        return True

    def resume(self) -> bool:
        if not self._paused:
            return False
        self._paused = False
        cur = self.current
        if cur is not None and not cur.finished:
            # the meeting stayed open while paused, and the pause doesn't count as silence: the idle timer goes on
            # from where it stood (a line that came in during the pause started it over)
            silent_before = max(0.0, self._paused_at - cur.last_speech_wall)
            cur.last_speech_wall = self.wall() - silent_before
        log.info("resumed")
        self.bus.publish("assistant.resumed")
        return True

    async def _linger(self) -> None:
        if self.keep_open and not self._stop.is_set():
            self.bus.publish("assistant.waiting")
            await self._stop.wait()
            await self._finish_background()  # pushes and agents started from the dashboard meanwhile

    async def _finish_background(self) -> None:
        """Shutting down: give late agents (and push retries) a bounded time, then settle what's left so no
        agent stays "running" for good and the notes and sinks get their final state."""
        for s in self.finished_meetings:
            s.dispatcher.hurry()
        if self._background:
            with contextlib.suppress(Exception):
                await self.unless_forced(
                    asyncio.wait(set(self._background), timeout=self.cfg.agents.timeout_s + 2 * LATE_AGENT_SLACK_S), grace=0
                )
        for s in self.finished_meetings:
            if s.agents.busy:
                await s.agents.cancel_all()
                s.store.save()  # now, not in a debounced save the closing loop would never run
        if self._background:  # _late_agents now writes the notes and syncs
            with contextlib.suppress(BaseException):
                await asyncio.wait(set(self._background), timeout=FORCE_GRACE_S if self.forced else LATE_AGENT_SLACK_S)
        if self.forced:
            for t in list(self._background):  # quitting: whatever is still going is given up
                t.cancel()
                with contextlib.suppress(BaseException):
                    await t
        for s in self.finished_meetings:
            s.flush_save()

    async def replay_transcript(
        self, segments: list[TranscriptSegment], speed: float = 0.0, started_at: datetime | None = None
    ) -> Path | None:
        """Run one meeting from an existing transcript (no audio): extraction, notes, pushes, agents.

        ``speed`` > 0 replays in (scaled) real time so the live extraction loop and
        dashboard behave as in a real meeting; 0 feeds everything at once. ``started_at`` is when the
        meeting took place (default: it just ended), for the notes' date, CRM logs and relative deadlines.
        """
        if started_at is None:
            started_at = utcnow() - timedelta(seconds=max((s.t_end for s in segments), default=0.0))
        self.bus.publish(
            "assistant.started",
            sources=["transcript import"],
            backend="import",
            extraction="claude" if self.llm else "heuristic",
            agents=self.cfg.agents.mode,
            always_on=False,
        )
        session = await self._ensure_meeting(0.0, started_at=started_at)
        session.t0 = 0.0
        prev = 0.0
        for seg in sorted(segments, key=lambda s: s.t_start):
            if self._stop.is_set():
                break
            if speed > 0:
                await asyncio.sleep(max(0.0, (seg.t_end - prev) / speed))
                prev = seg.t_end
            session.heard()
            session.add_segment(seg)
            if speed <= 0:
                await asyncio.sleep(0)
        path = await self.end_meeting("replayed")
        await self._finish_background()
        if path is not None:
            path = session.store.dir / path.name  # late agents may have let the folder be renamed
        await self._linger()
        if self.llm:
            await self.llm.aclose()
        for sink in self.sinks:
            with contextlib.suppress(Exception):
                await sink.aclose()
        self.bus.publish("assistant.stopped", cost=self.costs.snapshot())
        return path

    @property
    def echo_unprotected(self) -> bool:
        """System audio is captured but has only ever been digital silence while you talked — most likely
        macOS withholds it (no System Audio Recording permission yet). The echo filter then has nothing to
        compare with, so the speakers' sound picked up by your mic passes as you, wake phrases included."""
        return self._system_live and self.echo.system_active and not self._system_sound and self._mic_speech_s >= SILENT_SYSTEM_AFTER_S

    def _check_system_audio(self, seg: TranscriptSegment) -> None:
        # paused, system audio is silenced on purpose: a line still arriving from before the pause
        # mustn't count towards "macOS is withholding it"
        if seg.source != "mic" or self._system_sound or not self._system_live or self._paused:
            return
        self._mic_speech_s += max(0.0, seg.t_end - seg.t_start)
        if self.echo_unprotected and not self._silent_warned:
            self._silent_warned = True
            msg = (f"system audio has been pure digital silence while you talked for {SILENT_SYSTEM_AFTER_S / 60:.0f} min. "
                   "On a call, macOS is probably withholding it: allow Voicestream (or the terminal running it) under "
                   "Privacy & Security → Screen & System Audio Recording, then restart it. Until system audio comes through, "
                   "the others' voices from your speakers count as yours and 'Hey Claude' commands wait for your OK.")
            log.warning(msg)
            self.bus.publish("source.error", source="system", error=msg)

    async def _tee(self, src: AudioSource) -> AsyncIterator[AudioFrame]:
        zero_s, warned = 0.0, False
        # owns the capture: closing this generator (a failed transcriber, a restart) stops the device
        frames = src.frames()
        try:
            async for frame in frames:
                if self._paused:
                    # the device stays open (resuming is instant), but nothing it hears goes any further
                    frame = AudioFrame(frame.source, np.zeros_like(frame.samples), frame.t_start)
                    zero_s = 0.0  # nor does the silence count as a missing microphone permission
                elif src.name == "mic" and src.live and not warned:
                    # a denied macOS microphone permission yields exact digital silence, not an error
                    zero_s = zero_s + frame.duration if not frame.samples.any() else 0.0
                    if zero_s >= 10.0:
                        warned = True
                        msg = ("microphone delivers only digital silence — check the microphone permission "
                               "(Privacy & Security → Microphone) for Voicestream or the terminal running it")
                        log.warning(msg)
                        self.bus.publish("source.error", source=src.name, error=msg)
                if self.current is not None and self.cfg.capture.store_audio:
                    self.current.store.write_audio(frame)
                if src.name == "system" and not self._system_sound and frame.samples.any():
                    self._system_sound = True
                    if self._silent_warned:
                        log.warning("system audio is coming through now")
                yield frame
        finally:
            await aclose_quietly(frames)

    async def _transcribe(self, src: AudioSource, transcriber: Transcriber, label: str) -> None:
        frames = self._tee(src)
        segments = transcriber.stream(src.name, label, frames)
        try:
            async for seg in segments:
                await self._merge_q.put(seg)
        finally:
            # a streaming backend that fails mid-way leaves its frames unfinished: close them, or the
            # capture keeps running and the restart stacks a second one on the same queue
            await aclose_quietly(segments)
            await aclose_quietly(frames)

    async def _pipe(self, src: AudioSource) -> None:
        transcriber = self.transcribers[src.name]
        label = self.cfg.source_label(src.name)
        retry = RESTART_DELAY_S
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                await self._transcribe(src, transcriber, label)
                return  # source ended normally (file replay, device closed)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("%s: capture/transcription failed: %s", src.name, exc)
                self.bus.publish("source.error", source=src.name, error=str(exc))
                # recordings, missing devices/packages and bad credentials won't fix themselves
                fatal = isinstance(exc, (CaptureSetupError, ImportError)) or (
                    isinstance(exc, TranscriptionError) and exc.fatal
                )
                if not src.live or fatal or self._stop.is_set():
                    if not self._stop.is_set():  # given up for good: the app shows it as an error
                        self.bus.publish("source.stopped", source=src.name, error=str(exc))
                    raise
                if time.monotonic() - started > 60:
                    retry = RESTART_DELAY_S
                await asyncio.sleep(retry)
                retry = min(retry * 2, 60.0)
                src.reset()

    async def _merger(self) -> None:
        while True:
            try:
                seg = await asyncio.wait_for(self._merge_q.get(), timeout=0.5)
            except asyncio.TimeoutError:
                for s in self.echo.tick(time.monotonic()):
                    await self._route_safely(s)
                continue
            if seg is None:
                for s in self.echo.flush():
                    await self._route_safely(s)
                return
            try:
                ready = self.echo.add(seg, time.monotonic())
            except Exception:
                log.exception("echo filter failed; keeping the segment")
                ready = [seg]
            for s in ready:
                await self._route_safely(s)
            for s in self.echo.tick(time.monotonic()):
                await self._route_safely(s)

    async def _route_safely(self, seg: TranscriptSegment) -> None:
        # one failure (a full disk when the next meeting's folder is created…) must cost this segment, not
        # stop routing for the rest of the day while the transcribers keep running and billing
        try:
            await self._route(seg)
        except Exception as exc:
            log.exception("could not record a transcript segment")
            self.bus.publish("source.error", source=seg.source, error=f"could not record the transcript: {exc}")

    async def _route(self, seg: TranscriptSegment) -> None:
        self._check_system_audio(seg)
        if self.manual and self.current is None:
            return  # the meeting started by hand is the only one: words heard without it open no other
        session = await self._ensure_meeting(seg.t_start)
        session.heard()
        session.add_segment(seg)

    async def _ensure_meeting(self, t_first: float, started_at: datetime | None = None) -> MeetingSession:
        async with self._meeting_lock:
            if self.current is not None and self._silent_too_long(self.current):
                # just woke up from sleep: this speech came before the idle monitor's next look, and it
                # belongs to a new meeting
                if (session := self._detach()) is not None:
                    self.track(asyncio.ensure_future(self._wrap_up(session, "idle")))
            if self.current is None or self.current.finished:
                t0 = max(0.0, min(t_first, self.clock.now()) - 0.5)
                auto = self.auto_meetings and self.title is None and started_at is None
                self.current = MeetingSession(self, t0=t0, title=self.title, started_at=started_at, auto=auto)
                self.title = None  # only the first meeting gets the CLI title
                self.current.start()
            return self.current

    def _silent_too_long(self, session: MeetingSession) -> bool:
        """Always-on: no speech in ``session`` for ``idle_split_minutes``, counting time asleep (the Mac
        with its lid closed) but not time on Pause, when the meeting stays open."""
        idle_s = self.cfg.always_on.idle_split_minutes * 60
        if not self.auto_meetings or idle_s <= 0 or self._paused or session.finished or not session.meeting.segments:
            return False
        return self.wall() - session.last_speech_wall >= idle_s

    def _check_silence(self, session: MeetingSession) -> None:
        """:attr:`manual`: a meeting started by hand never splits on a silence, but one left running by
        mistake (the call is over, you forgot it) mustn't record all day. After :attr:`ask_after_s`
        without speech it asks, once per silence, whether you are still in it; after :attr:`end_after_s`
        it ends. Time asleep counts, time on Pause doesn't, as for the split; speech, or "Keep"
        (:meth:`keep_meeting`), starts the count over."""
        if self._paused or session.finished or self._stop.is_set() or self._ending_on_silence is session:
            return
        silent = self.wall() - session.last_speech_wall
        if self.end_after_s > 0 and silent >= self.end_after_s:
            log.info("no speech for %.0f min: ending the meeting", silent / 60)
            self._ending_on_silence = session
            self.track(asyncio.ensure_future(self.end_meeting("silence")))
        elif self.ask_after_s > 0 and silent >= self.ask_after_s and not session.asked:
            session.asked = True
            ends_at = utcnow() + timedelta(seconds=self.end_after_s - silent) if self.end_after_s > 0 else None
            self.bus.publish("meeting.still_there", meeting_id=session.meeting.id, silent_minutes=int(silent // 60),
                             ends_at=ends_at.isoformat() if ends_at else None)

    async def _idle_monitor(self) -> None:
        wall, awake = self.wall(), time.monotonic()
        while True:
            await asyncio.sleep(self.idle_poll_s)
            # time.monotonic() (and this sleep) stand still while the computer sleeps, self.wall() doesn't
            slept = (self.wall() - wall) - (time.monotonic() - awake)
            wall, awake = self.wall(), time.monotonic()
            if slept >= 60:
                log.info("the computer was asleep for about %.0f min", slept / 60)
            cur = self.current
            if cur is not None and self._silent_too_long(cur):
                log.info("no speech for %.0f min: closing the meeting", self.cfg.always_on.idle_split_minutes)
                self.track(asyncio.ensure_future(self.end_meeting("idle")))
            elif cur is not None and self.manual:
                self._check_silence(cur)

    def _detach(self) -> MeetingSession | None:
        """Take the meeting in progress out of :attr:`current` (with :attr:`_meeting_lock` held): the next
        speech starts a new one while this one wraps up."""
        session = self.current
        if session is None or session.finished:
            return None
        self.current = None
        # listed right away: its items can still be edited, pushed and run while it wraps up
        self.finished_meetings.append(session)
        return session

    async def _wrap_up(self, session: MeetingSession, reason: str) -> Path | None:
        path = await session.finish(reason)
        if path is None:
            self.finished_meetings.remove(session)  # nothing was said: nothing to keep
        return path

    async def end_meeting(self, reason: str = "manual") -> Path | None:
        """End the meeting in progress (summary, notes, pushes); returns its notes, ``None`` if there was
        none or nothing was said. :attr:`manual`: listening ends with it (:meth:`_end_listening`)."""
        if self.manual and self._running:
            return await self._end_listening(reason)
        return await self._end(reason)

    async def _end(self, reason: str) -> Path | None:
        async with self._meeting_lock:
            session = self._detach()
        if session is None:
            return None
        return await self._wrap_up(session, reason)

    async def _end_listening(self, reason: str) -> Path | None:
        """A meeting started by hand is all this assistant records: its end is a stop, in the order a stop
        takes. Capture is closed and its last words written down first, then the meeting is wrapped up
        with ``reason``: ended any other way, those words would be lost (or open another meeting) and the
        microphone would stay on. Says so at once (``assistant.ending``), as the last words can take a
        few seconds; returns once the meeting is wrapped up."""
        if not self._stop.is_set():
            self._end_reason = reason
            cur = self.current
            self.bus.publish("assistant.ending", reason=reason, meeting_id=cur.meeting.id if cur is not None else None)
            self.stop()
        waits = [asyncio.ensure_future(e.wait()) for e in (self.wound_down, self._done)]
        try:
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waits:
                w.cancel()
        return self._ended_path

    def keep_meeting(self) -> bool:
        """The answer to "Still in a meeting?": yes. The silence counts start over (asked again after
        another :attr:`ask_after_s`, ended after another :attr:`end_after_s`). Returns whether a meeting
        was there to keep."""
        cur = self.current
        if cur is None or cur.finished or self._stop.is_set():
            return False
        cur.last_speech_wall = self.wall()
        cur.asked = False
        self.bus.publish("meeting.kept", meeting_id=cur.meeting.id)
        return True

    async def _screen(self) -> None:
        try:
            from .screen.capture import ScreenWatcher
        except ImportError as exc:
            log.error("screen capture unavailable: %s (pip install 'voicestream[screen]')", exc)
            return
        try:
            self.screen_watcher = ScreenWatcher(self.cfg, self.llm, self.costs, clock=self.clock)
        except Exception as exc:
            log.error("screen capture disabled: %s", exc)
            return

        def deliver(note: ScreenNote) -> None:
            if self.current is not None and not self.current.finished:
                self.current.add_screen(note)

        await self.screen_watcher.run(deliver, should_capture=lambda: self.current is not None)

    # ------------------------------------------------------------ control API

    def _session(self) -> MeetingSession:
        if self.current is None:
            raise LookupError("no meeting in progress")
        return self.current

    def _find_item(self, item_id: str, meeting_id: str | None = None):
        """Item ids (A1, D2…) are per meeting; prefer the given/current meeting."""
        for s in [self.current, *reversed(self.finished_meetings)]:
            if s is None or (meeting_id and s.meeting.id != meeting_id):
                continue
            if item_id in s.meeting.items:
                return s, s.meeting.items[item_id]
        raise KeyError(item_id)

    def state(self) -> dict[str, Any]:
        cur = self.current
        last = self.finished_meetings[-1] if self.finished_meetings else None
        return {
            "user_name": self.cfg.user_name,
            "sources": [s.describe() for s in self.sources],
            "backend": self.cfg.transcription.backend,
            "extraction": "claude" if self.llm else "heuristic",
            "agents_mode": self.cfg.agents.mode,
            "dispatch_mode": self.cfg.dispatch.mode,
            "sinks": [s.name for s in self.sinks],
            "meeting": cur.meeting.model_dump(mode="json") if cur else None,
            "meeting_dir": str(cur.store.dir) if cur else None,
            "last_meeting": {"id": last.meeting.id, "title": last.meeting.title, "dir": str(last.store.dir)} if last else None,
            # between meetings, the last one in full: a fresh dashboard can still review, edit and push it
            "previous": last.meeting.model_dump(mode="json") if last and cur is None else None,
            "cost": self.costs.snapshot(),
            "listening": "paused" if self._paused else "listening",
            "stopping": self._stop.is_set(),
        }

    def approve_agent(self, item_id: str, prompt: str | None = None, meeting_id: str | None = None) -> bool:
        session, _ = self._find_item(item_id, meeting_id)
        return session.agents.approve(item_id, prompt)

    def reject_agent(self, item_id: str, meeting_id: str | None = None) -> bool:
        session, _ = self._find_item(item_id, meeting_id)
        return session.agents.reject(item_id)

    def dismiss_item(self, item_id: str, dismissed: bool = True, meeting_id: str | None = None) -> bool:
        session, _ = self._find_item(item_id, meeting_id)
        ok = session.tracker.set_dismissed(item_id, dismissed) is not None
        self._sync_finished(session, item_id)
        return ok

    def edit_item(self, item_id: str, meeting_id: str | None = None, **fields: Any) -> dict[str, Any]:
        session, item = self._find_item(item_id, meeting_id)
        session.tracker.edit(item_id, **fields)
        session.save_soon()
        self._sync_finished(session, item_id)
        return item.model_dump(mode="json")

    def _sync_finished(self, session: MeetingSession, item_id: str) -> None:
        """Once a meeting is wrapped up its dispatcher loop is gone: propagate an edit, a dismissal or a
        completion to the sinks that already have the item right away (the dispatcher lock orders it
        after a wrap-up push still in flight)."""
        item = session.meeting.items.get(item_id)
        if not session.finished or item is None or not item.pushed:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self.track(asyncio.ensure_future(session.dispatcher.sync(require_stable=False, only={item_id})))

    def add_item(self, kind: str, title: str, owner: str | None = None, due_text: str | None = None) -> dict[str, Any]:
        session = self._session()
        item = session.tracker.add_manual(ItemKind(kind), title, owner, due_text)
        session.save_soon()
        return item.model_dump(mode="json")

    async def push_item(self, item_id: str, meeting_id: str | None = None) -> dict[str, str]:
        session, _ = self._find_item(item_id, meeting_id)
        return await session.dispatcher.push_now(item_id)

    async def extract_now(self) -> None:
        session = self._session()
        await session.extractor.extract_now("manual")

    def today(self) -> date:
        return date.today()
