"""Meetings on disk: the list, a meeting's details, and editing, pushing and re-summarizing past ones.

Used by the app's Meetings screen (through :class:`Meetings`) and by ``voicestream push`` /
``voicestream summarize`` (the module-level functions), so both do the same thing.

Three rules keep a meeting from ever existing as two diverging copies:

* a meeting the running assistant still holds in memory (the one in progress, or one that ended
  during this run) is changed through that in-memory session, never by rewriting its file — the
  session's next save would undo the change;
* a meeting that is still being recorded by *another* process (``voicestream run`` in a
  terminal) is read-only here: that process owns its file. One whose recording was cut off (a
  crash, a force quit, a power cut: nobody holds its ``.recording`` lock any more, see
  :func:`voicestream.storage.is_recording`) is ended here instead (:func:`finish_interrupted`), so
  it can be edited, re-summarized and sent like any other;
* while a push, a sync of an edit or new notes are under way for a past meeting, everything uses
  that work's copy (:class:`_Held`) and the work runs one at a time per meeting — otherwise its
  final save would undo an edit made meanwhile, and two pushes at once would each create the items.

Relative deadlines ("by Friday") in a re-summarized meeting are resolved from the meeting's own
date, not from today.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from zoneinfo import ZoneInfo

from .. import known_people
from ..brain.people import other_side, people_event, speaker_label
from ..brain.summarizer import rerender, summarize
from ..brain.tracker import ItemTracker
from ..config import Config
from ..dispatch import Dispatcher, pending_review
from ..events import EventBus
from ..models import Event, ItemKind, Meeting
from ..storage import (
    RECORDING_LOCK,
    atomic_write,
    is_recording,
    list_sessions,
    load_meeting,
    read_transcript,
    write_transcript_md,
)

if TYPE_CHECKING:
    from ..brain.llm import LLM
    from ..costs import CostMeter
    from ..session import Assistant, MeetingSession
    from ..sinks.base import Sink

log = logging.getLogger(__name__)


class MeetingNotFound(LookupError):
    pass


class MeetingBusy(RuntimeError):
    """The meeting can't be changed now (still in progress); the message says why."""


# ------------------------------------------------------------------ shared helpers


def meeting_day(cfg: Config, meeting: Meeting) -> date:
    """The meeting's local date: what "tomorrow" and "by Friday" were said relative to."""
    return meeting.started_at.astimezone(ZoneInfo(cfg.timezone) if cfg.timezone else None).date()


def resolve_folder(cfg: Config, folder: str) -> Path:
    """The session folder called ``folder`` — only an existing one: the name comes from a URL, so it
    is matched against the real list rather than joined to a path (no ``..``, no absolute paths)."""
    for d in list_sessions(cfg.data_path):
        if d.name == folder:
            return d
    raise MeetingNotFound(f"no meeting {folder!r}")


def save_meeting(folder: Path, meeting: Meeting, notes: str | None = None) -> None:
    if notes is not None:
        atomic_write(folder / "notes.md", notes)
    atomic_write(folder / "meeting.json", meeting.model_dump_json(indent=1))


def notes_markdown(folder: Path, meeting: Meeting) -> str | None:
    try:
        return (folder / "notes.md").read_text(encoding="utf-8")
    except OSError:
        return meeting.summary_markdown


def summary(meeting: Meeting, folder: Path, user_name: str, *, live: bool = False) -> dict[str, Any]:
    """One row of the Meetings list (no transcript: every meeting.json holds the whole of it)."""
    visible = [i for i in meeting.items.values() if not i.dismissed]
    counts = {k.value: sum(1 for i in visible if i.kind == k) for k in ItemKind}
    pushed: dict[str, int] = {}
    for item in visible:
        for sink in item.pushed:
            pushed[sink] = pushed.get(sink, 0) + 1
    if meeting.segments:
        duration = max(s.t_end for s in meeting.segments)
    elif meeting.ended_at is not None:
        duration = (meeting.ended_at - meeting.started_at).total_seconds()
    else:
        duration = 0.0
    return {
        "folder": folder.name,
        "id": meeting.id,
        "title": meeting.title,
        "started_at": meeting.started_at.isoformat(),
        "ended_at": meeting.ended_at.isoformat() if meeting.ended_at else None,
        "duration_s": round(duration, 1),
        "in_progress": meeting.ended_at is None,
        "live": live,
        "language": meeting.language,
        "tldr": meeting.tldr,
        "participants": meeting.participants,
        "counts": counts,
        "my_actions": sum(
            1 for i in visible if i.kind == ItemKind.ACTION and i.status.value == "open" and i.is_mine(user_name)
        ),
        "pushed": pushed,
        "has_notes": (folder / "notes.md").is_file() or bool(meeting.summary_markdown),
        "short_clip": meeting.short_clip,
        "interrupted": meeting.interrupted,
    }


def _search_text(meeting: Meeting, folder: Path) -> str:
    parts = [folder.name, meeting.title or "", meeting.tldr or "", *meeting.participants]
    parts += [i.title for i in meeting.items.values() if not i.dismissed]
    return " ".join(parts).casefold()


async def push_meeting(
    cfg: Config,
    meeting: Meeting,
    sinks: list[Sink],
    *,
    bus: EventBus | None = None,
    notes: bool = True,
    sink_names: Collection[str] | None = None,
    dispatcher: Dispatcher | None = None,
    save: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Push (or update) a meeting's items, then its notes, to ``sinks``; returns what happened:
    ``{items: {id: {sink: ref|"error"}}, notes: {sink: ref}, errors: [...], count, up_to_date}``.

    ``save`` runs after every push, so an interrupted run keeps the refs of what it created. Pass the
    live session's ``dispatcher`` for a meeting held in memory: its lock orders this after a push
    already in flight, so nothing is created twice.
    """
    bus = bus or EventBus()
    items: dict[str, dict[str, str]] = {}
    errors: list[str] = []

    def on_event(ev: Event) -> None:
        if ev.data.get("meeting_id") != meeting.id:
            return
        if ev.type == "item.pushed":
            items.setdefault(str(ev.data["item_id"]), {})[str(ev.data["sink"])] = str(ev.data.get("ref") or "")
        elif ev.type == "sink.error":
            item_id = ev.data.get("item_id")
            if item_id:
                items.setdefault(str(item_id), {})[str(ev.data["sink"])] = "error"
            errors.append(f"{item_id or 'notes'} → {ev.data.get('sink')}: {ev.data.get('error')}")

    unlisten = bus.listen(on_event)
    d = dispatcher or Dispatcher(cfg, meeting, sinks, bus, on_change=save)
    refs: dict[str, str] = {}
    try:
        n = await d.sync(require_stable=False, sink_names=sink_names)
        if notes and meeting.summary_markdown:
            refs = await d.push_notes(meeting.summary_markdown, sink_names=sink_names)
    finally:
        unlisten()
        if save is not None:
            save()
    return {
        "items": items,
        "notes": refs,
        "errors": errors,
        "count": n,
        "up_to_date": n == 0 and any(i.pushed for i in meeting.items.values()),
    }


async def review_meeting(
    meeting: Meeting,
    dispatcher: Dispatcher,
    approve: Collection[str],
    reject: Collection[str] = (),
    *,
    save: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Apply your end-of-meeting decisions (:meth:`Dispatcher.review`) and say what happened:
    ``{sent: {item id: {sink: ref}}, errors: [...], pending: [item ids still waiting]}``."""
    sent: dict[str, dict[str, str]] = {}
    errors: list[str] = []

    def on_event(ev: Event) -> None:
        if ev.data.get("meeting_id") != meeting.id or not ev.data.get("item_id"):
            return
        if ev.type == "item.pushed":
            sent.setdefault(str(ev.data["item_id"]), {})[str(ev.data["sink"])] = str(ev.data.get("ref") or "")
        elif ev.type == "sink.error":
            errors.append(f"{ev.data['item_id']} → {ev.data.get('sink')}: {ev.data.get('error')}")

    unlisten = dispatcher.bus.listen(on_event) if dispatcher.bus is not None else None
    try:
        await dispatcher.review(approve, reject)
    finally:
        if unlisten:
            unlisten()
        if save is not None:
            save()
    return {"sent": sent, "errors": errors, "pending": dispatcher.pending_review()}


async def summarize_meeting(
    cfg: Config, meeting: Meeting, llm: LLM | None, *, tracker: ItemTracker | None = None, budget_ok: bool = True
) -> str:
    """Re-run the end-of-meeting summary (items consolidated, notes rewritten); returns the notes.
    Doesn't write anything: :func:`save_meeting` does."""
    day = meeting_day(cfg, meeting)
    tracker = tracker or ItemTracker(meeting, None, cfg.user_name, today=day)
    return await summarize(cfg, meeting, tracker, llm, today=day, budget_ok=budget_ok)


def finish_interrupted(cfg: Config, folder: Path) -> Meeting | None:
    """End a meeting whose recording was cut off — its process died before the wrap-up (a crash, a
    force quit, a power cut), and nothing else would ever end it. Returns it, or ``None`` (nothing
    changed) when it has ended already or a live process is still recording it.

    It ends at the last words heard, with the whole transcript from ``transcript.jsonl`` (``meeting.json``
    is only saved when items change, so it may hold few lines or none). The notes are the basic ones a
    forced stop writes, and nothing is sent: "Rewrite notes" and "Send to my tools" do the rest."""
    if is_recording(folder):  # first: once its lock is free, a recorder has saved its final state
        return None
    try:
        meeting, d = load_meeting(folder)
    except (OSError, ValueError):
        return None
    if meeting.ended_at is not None:
        return None
    heard = read_transcript(d)
    if len(heard) > len(meeting.segments):
        meeting.segments = heard
    spoken = max((s.t_end for s in meeting.segments), default=0.0)
    meeting.ended_at = meeting.started_at + timedelta(seconds=spoken)
    meeting.interrupted = True
    save_meeting(d, meeting, rerender(meeting, cfg.user_name))
    (d / RECORDING_LOCK).unlink(missing_ok=True)
    log.info("%s: its recording was cut off before the wrap-up; ended at its last words (%.0f s)", d.name, spoken)
    return meeting


# --------------------------------------------------------------------- the app's view


class MeetingsHost(Protocol):
    """What :class:`Meetings` needs from the app host."""

    cfg: Config
    bus: EventBus
    costs: CostMeter

    @property
    def assistant(self) -> Assistant | None: ...

    def track(self, task: asyncio.Task[Any]) -> None: ...

    # optional: ``assistants`` — the running assistant and earlier ones still finishing a meeting's
    # agents after a restart (AppHost); without it, only ``assistant``


@dataclass
class _Held:
    """A past meeting with work in flight: the one copy every operation on it uses meanwhile."""

    meeting: Meeting
    dir: Path
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    """One push / sync / summary at a time for this meeting."""
    users: int = 0


class Meetings:
    """The Meetings screen's operations, sharing the host's bus, cost meter and (while listening)
    the running assistant's sinks and in-memory meetings."""

    def __init__(self, host: MeetingsHost) -> None:
        self.host = host
        self._cache: dict[str, tuple[int, dict[str, Any], str]] = {}
        """folder name -> (meeting.json mtime, summary, search text): parsing every meeting.json
        (each holds its whole transcript) on every list would be slow after a few months."""
        self._held: dict[str, _Held] = {}
        """folder name -> the copy of a past meeting that has work in flight."""

    @property
    def cfg(self) -> Config:
        return self.host.cfg

    # ---------------------------------------------------------------- lookups

    def _sessions(self) -> list[MeetingSession]:
        # after a restart, the previous assistant may still be finishing a meeting's agents: that
        # meeting is still held in memory (and saved from there), so it is changed there too
        assistants = getattr(self.host, "assistants", None)
        if assistants is None:
            assistants = [self.host.assistant] if self.host.assistant is not None else []
        return [s for a in assistants for s in [a.current, *a.finished_meetings] if s is not None]

    def live_session(self, folder: str) -> MeetingSession | None:
        for s in self._sessions():
            if s.store.dir.name == folder:
                return s
        return None

    def _load(self, folder: str) -> tuple[Meeting, Path]:
        """The meeting from disk — or the copy that work in flight holds (it is newer, and it is the
        one that work will save)."""
        held = self._held.get(folder)
        if held is not None:
            return held.meeting, held.dir
        return load_meeting(resolve_folder(self.cfg, folder))

    def _hold(self, folder: str, meeting: Meeting, d: Path) -> _Held:
        """Register work on ``meeting`` (got from :meth:`_load`, so the held copy if there is one).
        Synchronous on purpose: from here on every edit lands in this copy. Pair with :meth:`_release`."""
        held = self._held.get(folder)
        if held is None:
            held = self._held[folder] = _Held(meeting, d)
        held.users += 1
        return held

    def _release(self, folder: str, held: _Held) -> None:
        held.users -= 1
        if held.users <= 0 and self._held.get(folder) is held:
            del self._held[folder]  # everything is saved: the next read is from disk again

    def _summary(self, d: Path) -> tuple[dict[str, Any], str] | None:
        try:
            mtime = (d / "meeting.json").stat().st_mtime_ns
        except OSError:
            return None
        hit = self._cache.get(d.name)
        if hit is not None and hit[0] == mtime:
            return hit[1], hit[2]
        try:
            meeting, _ = load_meeting(d)
        except Exception as exc:  # noqa: BLE001 - one damaged file must not hide the others
            log.warning("skipping unreadable meeting %s: %s", d.name, exc)
            return None
        row = summary(meeting, d, self.cfg.user_name)
        text = _search_text(meeting, d)
        self._cache[d.name] = (mtime, row, text)
        return row, text

    # --------------------------------------------------------------------- read

    def list(self, limit: int = 50, offset: int = 0, q: str = "") -> dict[str, Any]:
        live = {s.store.dir.name: s for s in self._sessions()}
        rows: list[tuple[dict[str, Any], str]] = []
        for d in list_sessions(self.cfg.data_path):
            s = live.get(d.name)
            if s is not None:  # fresher than its file, which is saved a second later
                rows.append((summary(s.meeting, d, self.cfg.user_name, live=not s.finished), _search_text(s.meeting, d)))
            elif (hit := self._summary(d)) is not None:
                if hit[0]["in_progress"] and self._recover(d.name, d) is not None:
                    hit = self._summary(d) or hit  # cut off, and ended just now
                rows.append(hit)
        needle = q.strip().casefold()
        if needle:
            rows = [r for r in rows if needle in r[1]]
        rows.sort(key=lambda r: r[0]["started_at"], reverse=True)
        return {"meetings": [r[0] for r in rows[offset : offset + limit]], "total": len(rows)}

    def _recover(self, folder: str, d: Path) -> Meeting | None:
        """End ``folder``'s meeting if its recording was cut off (:func:`finish_interrupted`); never one
        this app holds in memory or has work in flight on."""
        if folder in self._held or self.live_session(folder) is not None:
            return None
        meeting = finish_interrupted(self.cfg, d)
        if meeting is not None:
            self._cache.pop(folder, None)  # a coarse file clock could leave the old mtime
            self.host.bus.publish("meeting.saved", meeting_id=meeting.id, folder=folder)
        return meeting

    def recover_interrupted(self) -> list[str]:
        """At start-up: end the meetings a crash or a force quit left open (their ``.recording`` lock
        file stayed behind, unlocked). Returns their folders. Older ones without a lock file are ended
        when they are listed or opened."""
        done = []
        for d in list_sessions(self.cfg.data_path):
            if (d / RECORDING_LOCK).exists() and self._recover(d.name, d) is not None:
                done.append(d.name)
        return done

    def detail(self, folder: str) -> dict[str, Any]:
        d = resolve_folder(self.cfg, folder)
        s = self.live_session(folder)
        meeting = s.meeting if s is not None else self._load(folder)[0]
        if s is None and meeting.ended_at is None:
            meeting = self._recover(folder, d) or meeting
            if meeting.ended_at is None:  # being recorded elsewhere: its transcript file is the up-to-date one
                heard = read_transcript(d)
                if len(heard) > len(meeting.segments):
                    meeting = meeting.model_copy(update={"segments": heard})
        return {
            "meeting": meeting.model_dump(mode="json"),
            "notes_markdown": notes_markdown(d, meeting),
            "folder": folder,
            "in_memory": s is not None,
            "review": self._review_state(meeting, s),
        }

    def _review_state(self, meeting: Meeting, s: MeetingSession | None) -> dict[str, Any]:
        """The tools that wait for your go-ahead (``review = true``), the to-dos waiting for it, and the
        approved ones that haven't reached them."""
        sinks = [sink for sink in (s.dispatcher.sinks if s is not None else self._review_sinks()) if sink.cfg.review]
        tools = [sink.name for sink in sinks]
        pending = pending_review(meeting, sinks, self.cfg.dispatch.min_confidence) if meeting.ended_at else []
        # approved, but not there yet (the push failed): the page offers to try again
        unsent = [i for i in meeting.items.values() if not i.dismissed and any(
            sink.name not in i.pushed and i.id in (meeting.review[sink.name].approved if sink.name in meeting.review else ())
            for sink in sinks)]
        return {"sinks": tools, "pending": pending, "unsent": [i.id for i in unsent]}

    def _editable(self, folder: str) -> tuple[Meeting, Path]:
        meeting, d = self._load(folder)
        if meeting.ended_at is None:
            if self._recover(folder, d) is None:
                raise MeetingBusy("this meeting is still being recorded by another voicestream (end it there first)")
            meeting, d = self._load(folder)
        return meeting, d

    # -------------------------------------------------------------------- edit

    def add_item(self, folder: str, kind: str, title: str, owner: str | None = None, due_text: str | None = None) -> dict[str, Any]:
        s = self.live_session(folder)
        if s is not None:
            item = s.tracker.add_manual(ItemKind(kind), title, owner, due_text)
            s.save_soon()
            return item.model_dump(mode="json")
        meeting, d = self._editable(folder)
        tracker = ItemTracker(meeting, self.host.bus, self.cfg.user_name, today=meeting_day(self.cfg, meeting))
        item = tracker.add_manual(ItemKind(kind), title, owner, due_text)
        self._write(d, meeting)
        return item.model_dump(mode="json")

    def edit_item(self, folder: str, item_id: str, **fields: Any) -> dict[str, Any]:
        s = self.live_session(folder)
        if s is not None:
            return s.app.edit_item(item_id, meeting_id=s.meeting.id, **fields)  # its own assistant: maybe not the running one
        meeting, d = self._editable(folder)
        if item_id not in meeting.items:
            raise KeyError(item_id)
        tracker = ItemTracker(meeting, self.host.bus, self.cfg.user_name, today=meeting_day(self.cfg, meeting))
        tracker.edit(item_id, **fields)
        self._write(d, meeting)
        self._sync_later(meeting, d, item_id)
        return meeting.items[item_id].model_dump(mode="json")

    def dismiss_item(self, folder: str, item_id: str, dismissed: bool = True) -> dict[str, Any]:
        s = self.live_session(folder)
        if s is not None:
            s.app.dismiss_item(item_id, dismissed=dismissed, meeting_id=s.meeting.id)
            return s.meeting.items[item_id].model_dump(mode="json")
        meeting, d = self._editable(folder)
        if item_id not in meeting.items:
            raise KeyError(item_id)
        ItemTracker(meeting, self.host.bus, self.cfg.user_name).set_dismissed(item_id, dismissed)
        self._write(d, meeting)
        self._sync_later(meeting, d, item_id)
        return meeting.items[item_id].model_dump(mode="json")

    def edit_people(self, folder: str, add: list[str], remove: list[str]) -> dict[str, Any]:
        """Who the meeting was with, as you correct it (``known_people.edit_meeting``). Its notes and
        transcript.md are written again with the names; what your tools have changes with the next push."""
        s = self.live_session(folder)
        if s is not None:
            s.edit_people(add, remove)
            return {**people_event(s.meeting), "folder": s.store.dir.name}
        meeting, d = self._editable(folder)
        known_people.edit_meeting(self.cfg.data_path, meeting, add, remove, self.cfg.user_name)
        self._write(d, meeting)
        label, other = self.cfg.capture.system.label, other_side(meeting)
        write_transcript_md(d, meeting, lambda seg: speaker_label(seg, other, label))
        self.host.bus.publish("meeting.people", **people_event(meeting), folder=d.name)
        return {**people_event(meeting), "folder": d.name}

    def _write(self, d: Path, meeting: Meeting) -> None:
        """Save an edited past meeting; its notes list the items, so they are rebuilt too."""
        notes = rerender(meeting, self.cfg.user_name) if (d / "notes.md").is_file() else None
        save_meeting(d, meeting, notes)
        self.host.bus.publish("meeting.saved", meeting_id=meeting.id, folder=d.name)

    def _sync_later(self, meeting: Meeting, d: Path, item_id: str) -> None:
        """An edit (done, dismissed, new deadline…) of an item that is already in your tools is
        propagated there in the background, as the live meeting does."""
        item = meeting.items.get(item_id)
        if item is None or not item.pushed:
            return
        held = self._hold(d.name, meeting, d)  # now, not in the task: an edit made before it runs lands here too

        async def go() -> None:
            try:
                async with held.lock:
                    sinks, temporary = self._sinks()
                    try:
                        d_ = Dispatcher(self.cfg, meeting, sinks, self.host.bus, on_change=lambda: save_meeting(d, meeting))
                        await d_.sync(require_stable=False, only={item_id})
                    finally:
                        save_meeting(d, meeting)
                        if temporary:
                            await _close(sinks)
            finally:
                self._release(d.name, held)

        self.host.track(asyncio.ensure_future(go()))

    # -------------------------------------------------------------------- push

    def _review_sinks(self) -> list[Sink]:
        """The sinks that wait for your go-ahead: the running assistant's, else just those built afresh
        (nothing to close: their HTTP clients are only made when they send something)."""
        a = self.host.assistant
        if a is not None:
            return [s for s in a.sinks if s.cfg.review]
        from ..sinks.base import build_sinks, sink_class

        names = []
        for name, raw in self.cfg.sinks.items():
            try:
                default = sink_class(name).Config.model_fields["review"].default
            except (KeyError, ImportError):
                continue
            if (raw or {}).get("enabled") and (raw or {}).get("review", default):
                names.append(name)
        return build_sinks(self.cfg, only=names) if names else []

    def _sinks(self) -> tuple[list[Sink], bool]:
        """The running assistant's sinks (shared: never closed here), else fresh ones to close after."""
        a = self.host.assistant
        if a is not None:
            return a.sinks, False
        from ..sinks.base import build_sinks

        return build_sinks(self.cfg), True

    async def push(self, folder: str, sink_names: list[str] | None = None, notes: bool = True, force: bool = False) -> dict[str, Any]:
        s = self.live_session(folder)
        if s is not None:
            return await push_meeting(
                self.cfg, s.meeting, s.dispatcher.sinks, bus=self.host.bus, notes=notes and s.finished,
                sink_names=sink_names, dispatcher=s.dispatcher, save=s.save_soon,
            )
        meeting, d = self._load(folder)
        if meeting.ended_at is None and not force:
            if self._recover(folder, d) is None:
                raise MeetingBusy("this meeting is still in progress in another voicestream: end it there first")
            meeting, d = self._load(folder)
        held = self._hold(folder, meeting, d)
        try:
            async with held.lock:  # a second push waits, then finds the items already pushed
                sinks, temporary = self._sinks()
                try:
                    if not sinks:
                        return {"items": {}, "notes": {}, "errors": ["no tools are enabled (Settings → Your tools)"],
                                "count": 0, "up_to_date": False}
                    result = await push_meeting(
                        self.cfg, meeting, sinks, bus=self.host.bus, notes=notes, sink_names=sink_names,
                        save=lambda: save_meeting(d, meeting),
                    )
                finally:
                    if temporary:
                        await _close(sinks)
        finally:
            self._release(folder, held)
        self.host.bus.publish("meeting.saved", meeting_id=meeting.id, folder=d.name)
        return result

    async def review(self, folder: str, approve: list[str], reject: list[str]) -> dict[str, Any]:
        """Validate the end-of-meeting to-do list: send ``approve`` to the tools that wait for it, never
        ``reject``. Returns ``{sent, errors, pending}``."""
        s = self.live_session(folder)
        if s is not None:
            if not s.finished:
                raise MeetingBusy("the meeting is still in progress: its to-do list is reviewed when it ends")
            return await review_meeting(s.meeting, s.dispatcher, approve, reject, save=s.save_soon)
        meeting, d = self._editable(folder)
        held = self._hold(folder, meeting, d)
        try:
            async with held.lock:
                sinks, temporary = self._sinks()
                try:
                    dispatcher = Dispatcher(self.cfg, meeting, sinks, self.host.bus, on_change=lambda: save_meeting(d, meeting))
                    result = await review_meeting(meeting, dispatcher, approve, reject, save=lambda: save_meeting(d, meeting))
                finally:
                    if temporary:
                        await _close(sinks)
        finally:
            self._release(folder, held)
        self.host.bus.publish("meeting.saved", meeting_id=meeting.id, folder=d.name)
        return result

    # --------------------------------------------------------------- summarize

    def start_summarize(self, folder: str) -> None:
        """Rewrite the notes in the background (a model call can take a minute); publishes
        ``meeting.notes_updated {meeting_id, folder, notes, error?}`` when done."""
        resolve_folder(self.cfg, folder)
        s = self.live_session(folder)
        if s is not None and not s.finished:
            raise MeetingBusy("the meeting is still in progress: its notes are written when it ends")
        if s is None:
            self._editable(folder)  # refuse a meeting another process is recording, now
        task = asyncio.ensure_future(self.summarize(folder))
        task.add_done_callback(_log_failure)
        self.host.track(task)

    async def summarize(self, folder: str) -> str:
        bus = self.host.bus
        s = self.live_session(folder)
        llm, temporary = self._llm()
        try:
            budget_ok = not self.host.costs.over_budget()
            if s is not None:
                meeting, d = s.meeting, s.store.dir
                notes = await summarize_meeting(self.cfg, meeting, llm, tracker=s.tracker, budget_ok=budget_ok)
                s.store.save_notes(notes)
                s.store.save()
            else:
                meeting, d = self._editable(folder)
                held = self._hold(folder, meeting, d)
                try:
                    async with held.lock:
                        notes = await summarize_meeting(self.cfg, meeting, llm, budget_ok=budget_ok)
                        save_meeting(d, meeting, notes)
                        known_people.remember_meeting(self.cfg.data_path, meeting, self.cfg.user_name)
                finally:
                    self._release(folder, held)
        except Exception as exc:
            bus.publish("meeting.notes_updated", meeting_id=None, folder=folder, notes=None, error=str(exc))
            raise
        finally:
            if temporary and llm is not None:
                await llm.aclose()
        bus.publish("meeting.notes_updated", meeting_id=meeting.id, folder=d.name, notes=str(d / "notes.md"))
        bus.publish("meeting.saved", meeting_id=meeting.id, folder=d.name)
        return notes

    def _llm(self) -> tuple[LLM | None, bool]:
        """The running assistant's Claude client, else a fresh one billed to the shared meter."""
        if self.cfg.brain.provider != "anthropic":
            return None, False
        a = self.host.assistant
        if a is not None and a.llm is not None:
            return a.llm, False
        from ..brain.llm import ClaudeLLM, LLMUnavailable

        try:
            return ClaudeLLM(on_usage=self.host.costs.add_llm), True
        except LLMUnavailable as exc:
            log.info("re-summarizing without Claude: %s", exc)
            return None, False


async def _close(sinks: list[Sink]) -> None:
    for sink in sinks:
        try:
            await sink.aclose()
        except Exception:  # noqa: BLE001 - closing must not hide the result
            log.debug("closing sink %s failed", sink.name, exc_info=True)


def _log_failure(task: asyncio.Future[Any]) -> None:
    if not task.cancelled() and (exc := task.exception()):
        log.warning("rewriting the notes failed: %s", exc)
