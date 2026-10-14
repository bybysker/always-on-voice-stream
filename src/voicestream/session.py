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
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Callable
from datetime import date
from pathlib import Path
from typing import Any

from .agents.runner import AgentRunner, Engine
from .audio import AudioSource, Clock, build_source
from .brain.extractor import Extractor
from .brain.llm import LLM, ClaudeLLM, LLMUnavailable
from .brain.summarizer import rerender, summarize
from .brain.tracker import ItemTracker
from .config import Config
from .costs import CostMeter
from .dispatch import Dispatcher
from .events import EventBus
from .models import AudioFrame, Event, ItemKind, Meeting, ScreenNote, TranscriptSegment, utcnow
from .sinks.base import Sink, build_sinks
from .storage import SessionStore
from .transcribe import Transcriber, build_transcriber
from .transcribe.echo import EchoFilter

log = logging.getLogger(__name__)


class MeetingSession:
    """Everything scoped to one meeting."""

    def __init__(self, app: Assistant, t0: float, title: str | None = None) -> None:
        self.app = app
        cfg = app.cfg
        self.t0 = t0
        self.meeting = Meeting(title=title)
        self.store = SessionStore(cfg.data_path, self.meeting, store_audio=cfg.capture.store_audio)
        self.tracker = ItemTracker(self.meeting, app.bus, cfg.user_name)
        self.extractor = Extractor(cfg, self.meeting, self.tracker, app.llm, app.costs, app.bus)
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
        )
        self.last_speech = t0
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

    async def finish(self, reason: str = "ended") -> Path | None:
        """Final extraction, summary, push, notes. Idempotent."""
        if self.finished:
            return None
        self.finished = True
        cfg = self.app.cfg
        self.app.bus.publish("meeting.ending", meeting_id=self.meeting.id, reason=reason)
        self.extractor.stop()
        self.dispatcher.stop()
        for t in self._tasks[:2]:
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(t, 10)
        if not self.meeting.segments and not self.meeting.items:
            await self._shutdown_tasks()
            self._unlisten()
            self.store.close()
            self._cleanup_empty()
            return None
        try:
            await self.extractor.flush()
        except Exception:
            log.exception("final extraction failed")
        if self.agents.busy and cfg.agents.finish_wait_s > 0:
            log.info("waiting up to %.0fs for running agents…", cfg.agents.finish_wait_s)
            await self.agents.drain(cfg.agents.finish_wait_s)
        self.meeting.ended_at = utcnow()
        try:
            notes = await summarize(
                cfg,
                self.meeting,
                self.tracker,
                self.app.llm if cfg.brain.provider == "anthropic" else None,
                budget_ok=not self.app.costs.over_budget(),
            )
        except Exception:
            log.exception("summary failed")
            notes = rerender(self.meeting, cfg.user_name)
        self.store.save_notes(notes)
        try:
            await self.dispatcher.finalize(notes)
        except Exception:
            log.exception("final push failed")
        self.store.rename_with_title()
        self.store.save()
        notes_path = self.store.dir / "notes.md"
        self.app.bus.publish(
            "meeting.ended",
            meeting_id=self.meeting.id,
            title=self.meeting.title,
            notes=str(notes_path),
            dir=str(self.store.dir),
            items=len([i for i in self.meeting.items.values() if not i.dismissed]),
        )
        if self.agents.busy:
            # agents keep working; refresh the notes when they're done
            self.app.track(asyncio.ensure_future(self._late_agents()))
        else:
            await self._shutdown_tasks()
            self._unlisten()
        self.store.close()
        return notes_path

    async def _late_agents(self) -> None:
        await self.agents.drain(self.app.cfg.agents.timeout_s + 30)
        notes = rerender(self.meeting, self.app.cfg.user_name)
        self.store.save_notes(notes)
        self.store.save()
        await self.dispatcher.sync(require_stable=False, only={i.id for i in self.meeting.items.values() if i.pushed})
        await self._shutdown_tasks()
        self._unlisten()
        self.app.bus.publish("meeting.notes_updated", meeting_id=self.meeting.id, notes=str(self.store.dir / "notes.md"))

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
                if p.is_file() and p.name in ("transcript.md", "meeting.json", "transcript.jsonl"):
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
    ) -> None:
        self.cfg = cfg
        self.bus = bus or EventBus()
        self.costs = costs or CostMeter(cfg, self.bus)
        self.clock = Clock()
        self.title = title
        self.always_on = (cfg.always_on.idle_split_minutes > 0) if always_on is None else always_on
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
        self._end_requested = asyncio.Event()
        self._background: set[asyncio.Task[Any]] = set()
        self._meeting_lock = asyncio.Lock()
        self.echo = EchoFilter(cfg.echo)
        self.sources: list[AudioSource] = []
        self.transcribers: dict[str, Transcriber] = {}
        self.screen_watcher: Any = None
        self._last_level_pub: dict[str, float] = {}
        self.idle_poll_s = 5.0

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

    def track(self, task: asyncio.Task[Any]) -> None:
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # ------------------------------------------------------------------- run

    async def run(self) -> None:
        self.sources = self._build_sources()
        if not self.sources:
            raise RuntimeError("no audio sources configured")
        names = [s.name for s in self.sources]
        self.echo.system_active = "system" in names and "mic" in names
        for src in self.sources:
            self.transcribers[src.name] = self._make_transcriber(src.name)
        self.bus.publish(
            "assistant.started",
            sources=[s.describe() for s in self.sources],
            backend=self.cfg.transcription.backend,
            extraction="claude" if self.llm else "heuristic",
            agents=self.cfg.agents.mode,
            always_on=self.always_on,
        )
        if not self.always_on or self.title:
            await self._ensure_meeting(self.clock.now())

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
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(all_pipes), timeout=30)
            if not all_pipes.done():
                all_pipes.cancel()
            await self._merge_q.put(None)
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(merger, 10)
            for t in (monitor, screen, stopper):
                if t is not None:
                    t.cancel()
            await self.end_meeting("stopped")
            if self._background:
                log.info("waiting for background work (agents) to finish… Ctrl+C again to abort")
                with contextlib.suppress(BaseException):
                    await asyncio.wait(self._background, timeout=self.cfg.agents.timeout_s + 60)
            for t in self.transcribers.values():
                with contextlib.suppress(Exception):
                    await t.aclose()
            if self.llm:
                await self.llm.aclose()
            for sink in self.sinks:
                with contextlib.suppress(Exception):
                    await sink.aclose()
            self.bus.publish("assistant.stopped", cost=self.costs.snapshot())
        results = all_pipes.result() if all_pipes.done() and not all_pipes.cancelled() else []
        for r in results:
            if isinstance(r, Exception) and not isinstance(r, asyncio.CancelledError):
                log.error("capture pipeline failed: %r", r)

    def stop(self) -> None:
        self._stop.set()

    async def _tee(self, src: AudioSource) -> AsyncIterator[AudioFrame]:
        async for frame in src.frames():
            if self.current is not None and self.cfg.capture.store_audio:
                self.current.store.write_audio(frame)
            yield frame

    async def _pipe(self, src: AudioSource) -> None:
        transcriber = self.transcribers[src.name]
        label = self.cfg.source_label(src.name)
        retry = 2.0
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                async for seg in transcriber.stream(src.name, label, self._tee(src)):
                    await self._merge_q.put(seg)
                return  # source ended normally (file replay, device closed)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("%s: capture/transcription failed: %s", src.name, exc)
                self.bus.publish("source.error", source=src.name, error=str(exc))
                if not src.live or self._stop.is_set():
                    raise
                if time.monotonic() - started > 60:
                    retry = 2.0
                await asyncio.sleep(retry)
                retry = min(retry * 2, 60.0)
                src.reset()

    async def _merger(self) -> None:
        while True:
            try:
                seg = await asyncio.wait_for(self._merge_q.get(), timeout=0.5)
            except asyncio.TimeoutError:
                for s in self.echo.tick(time.monotonic()):
                    await self._route(s)
                continue
            if seg is None:
                for s in self.echo.flush():
                    await self._route(s)
                return
            for s in self.echo.add(seg, time.monotonic()):
                await self._route(s)
            for s in self.echo.tick(time.monotonic()):
                await self._route(s)

    async def _route(self, seg: TranscriptSegment) -> None:
        session = await self._ensure_meeting(seg.t_start)
        session.last_speech = self.clock.now()
        session.add_segment(seg)

    async def _ensure_meeting(self, t_first: float) -> MeetingSession:
        async with self._meeting_lock:
            if self.current is None or self.current.finished:
                t0 = max(0.0, min(t_first, self.clock.now()) - 0.5)
                self.current = MeetingSession(self, t0=t0, title=self.title)
                self.title = None  # only the first meeting gets the CLI title
                self.current.start()
            return self.current

    async def _idle_monitor(self) -> None:
        idle_s = self.cfg.always_on.idle_split_minutes * 60
        while True:
            await asyncio.sleep(self.idle_poll_s)
            cur = self.current
            if cur is None or cur.finished or not self.always_on or idle_s <= 0:
                continue
            if cur.meeting.segments and self.clock.now() - cur.last_speech >= idle_s:
                log.info("no speech for %.0f min: closing the meeting", idle_s / 60)
                self.track(asyncio.ensure_future(self.end_meeting("idle")))

    async def end_meeting(self, reason: str = "manual") -> Path | None:
        async with self._meeting_lock:
            session = self.current
            if session is None or session.finished:
                return None
            self.current = None
        path = await session.finish(reason)
        if path is not None:
            self.finished_meetings.append(session)
        return path

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
            "cost": self.costs.snapshot(),
        }

    def approve_agent(self, item_id: str, prompt: str | None = None, meeting_id: str | None = None) -> bool:
        session, _ = self._find_item(item_id, meeting_id)
        return session.agents.approve(item_id, prompt)

    def reject_agent(self, item_id: str, meeting_id: str | None = None) -> bool:
        session, _ = self._find_item(item_id, meeting_id)
        return session.agents.reject(item_id)

    def dismiss_item(self, item_id: str, dismissed: bool = True, meeting_id: str | None = None) -> bool:
        session, _ = self._find_item(item_id, meeting_id)
        return session.tracker.set_dismissed(item_id, dismissed) is not None

    def edit_item(self, item_id: str, meeting_id: str | None = None, **fields: Any) -> dict[str, Any]:
        session, item = self._find_item(item_id, meeting_id)
        session.tracker.edit(item_id, **fields)
        session.save_soon()
        return item.model_dump(mode="json")

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
