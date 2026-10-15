"""Decides when items are pushed to sinks, and keeps external copies in sync."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from .config import Config
from .events import EventBus
from .models import Item, ItemStatus, Meeting
from .sinks.base import Sink, SinkError

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 4
LINKS_OTHER_NOTES = {"slack"}
"""Sinks whose notes link to the copies the other sinks made (the Slack digest): they get the notes last."""


class Dispatcher:
    def __init__(
        self,
        cfg: Config,
        meeting: Meeting,
        sinks: list[Sink],
        bus: EventBus | None = None,
        now: Callable[[], float] = time.monotonic,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.dc = cfg.dispatch
        self.meeting = meeting
        self.sinks = sinks
        self.bus = bus
        self.now = now
        self.on_change = on_change
        self._changed_at: dict[str, tuple[int, float]] = {}  # item id -> (revision, first seen at)
        self._attempts: dict[tuple[str, str], tuple[int, int]] = {}
        """(item id, sink) -> (item revision, failed attempts). Editing an item gives a failed push a
        fresh start (e.g. after you granted a permission), except an uncertain create (see _push)."""
        self.notes_retry_s = 5.0
        self._lock = asyncio.Lock()
        self._stopped = asyncio.Event()

    # ------------------------------------------------------------- eligibility

    def _stable(self, item: Item) -> bool:
        rev, seen = self._changed_at.get(item.id, (-1, 0.0))
        if rev != item.revision:
            self._changed_at[item.id] = (item.revision, self.now())
            return self.dc.stable_s <= 0
        return self.now() - seen >= self.dc.stable_s

    def _todo(self, item: Item, sink: Sink) -> str | None:
        """'create' | 'update' | None"""
        rev, failures = self._attempts.get((item.id, sink.name), (item.revision, 0))
        if failures >= MAX_ATTEMPTS and (rev == item.revision or rev < 0):
            return None
        if sink.name in item.pushed:
            if sink.supports_update and item.pushed_revision.get(sink.name, 0) < item.revision:
                return "update"
            return None
        if not sink.accepts(item, self.dc.min_confidence):
            return None
        return "create"

    # ----------------------------------------------------------------- pushing

    async def _push(self, item: Item, sink: Sink, action: str) -> bool:
        key = (item.id, sink.name)
        revision = item.revision  # what is sent; an edit made while the request is in flight is synced next round
        try:
            if action == "create":
                ref = await sink.push_item(item, self.meeting)
                item.pushed[sink.name] = ref
            else:
                await sink.update_item(item, self.meeting, item.pushed[sink.name])
            item.pushed_revision[sink.name] = revision
            self._attempts.pop(key, None)
            if self.bus:
                self.bus.publish(
                    "item.pushed", meeting_id=self.meeting.id, item_id=item.id, sink=sink.name, ref=item.pushed.get(sink.name), action=action
                )
            log.info("%s %s → %s", "pushed" if action == "create" else "updated", item.id, sink.name)
            return True
        except SinkError as exc:
            if exc.uncertain and action == "create":
                # may exist already: only a manual push may try again (rev -1 never matches an edit)
                self._attempts[key] = (-1, MAX_ATTEMPTS)
            else:
                self._attempts[key] = (revision, MAX_ATTEMPTS if not exc.retryable else self._failures(key, revision) + 1)
            self._report(item, sink, exc)
        except Exception as exc:  # network errors, bugs in a sink
            self._attempts[key] = (revision, self._failures(key, revision) + 1)
            self._report(item, sink, exc)
        return False

    def _failures(self, key: tuple[str, str], revision: int) -> int:
        rev, failures = self._attempts.get(key, (revision, 0))
        return failures if rev == revision else 0

    def _report(self, item: Item, sink: Sink, exc: Exception) -> None:
        log.warning("push %s → %s failed: %s", item.id, sink.name, exc)
        if self.bus:
            self.bus.publish("sink.error", meeting_id=self.meeting.id, item_id=item.id, sink=sink.name, error=str(exc))

    async def sync(self, *, require_stable: bool = True, only: set[str] | None = None) -> int:
        """Push/update everything that is due. Returns the number of successful operations."""
        async with self._lock:
            done = 0
            for item in list(self.meeting.items.values()):
                if only is not None and item.id not in only:
                    continue
                if require_stable and not self._stable(item):
                    continue
                for sink in self.sinks:
                    action = self._todo(item, sink)
                    if action and await self._push(item, sink, action):
                        done += 1
            if done and self.on_change:
                self.on_change()
            return done

    async def push_now(self, item_id: str, sink_names: list[str] | None = None) -> dict[str, str]:
        """Manual push (dashboard button / CLI), ignoring confidence and stability."""
        item = self.meeting.items.get(item_id)
        if item is None:
            raise KeyError(item_id)
        results: dict[str, str] = {}
        async with self._lock:
            for sink in self.sinks:
                if sink_names and sink.name not in sink_names:
                    continue
                if item.kind.value not in sink.cfg.kinds:
                    continue
                self._attempts.pop((item.id, sink.name), None)
                action = "update" if sink.name in item.pushed else "create"
                if action == "create" and (item.dismissed or item.status == ItemStatus.CANCELLED):
                    results[sink.name] = "skipped (dismissed or cancelled)"
                    continue
                if action == "update" and not sink.supports_update:
                    results[sink.name] = item.pushed[sink.name]
                    continue
                ok = await self._push(item, sink, action)
                results[sink.name] = item.pushed.get(sink.name, "") if ok else "error"
        if self.on_change:
            self.on_change()
        return results

    async def run(self) -> None:
        """Background loop for ``dispatch.mode = "immediate"``; also keeps pushed items in sync."""
        while not self._stopped.is_set():
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
            if self._stopped.is_set():
                break
            try:
                if self.dc.mode == "immediate":
                    await self.sync(require_stable=True)
                elif any(item.pushed for item in self.meeting.items.values()):
                    # keep already-pushed items in sync in every mode
                    await self.sync(require_stable=True, only={i.id for i in self.meeting.items.values() if i.pushed})
            except Exception:
                log.exception("dispatch round failed")

    def stop(self) -> None:
        self._stopped.set()

    async def finalize(self, notes_markdown: str | None) -> None:
        """End of meeting: push remaining items (unless manual) and publish notes."""
        if self.dc.mode != "manual":
            await self.sync(require_stable=False)
        else:
            await self.sync(require_stable=False, only={i.id for i in self.meeting.items.values() if i.pushed})
        if notes_markdown and self.dc.mode != "manual":
            await self.push_notes(notes_markdown)

    async def push_notes(self, notes_markdown: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for sink in sorted(self.sinks, key=lambda s: s.name in LINKS_OTHER_NOTES):
            if not (sink.supports_notes and sink.cfg.notes):
                continue
            for attempt in range(2):  # notes are pushed once per meeting: give a transient failure one more go
                try:
                    ref = await sink.push_notes(self.meeting, notes_markdown)
                    if ref:
                        self.meeting.notes_ref[sink.name] = ref
                        out[sink.name] = ref
                        if self.bus:
                            self.bus.publish("notes.pushed", meeting_id=self.meeting.id, sink=sink.name, ref=ref)
                    break
                except Exception as exc:
                    if attempt == 0 and isinstance(exc, SinkError) and exc.retryable:
                        log.info("notes → %s failed (%s); retrying in %.0fs", sink.name, exc, self.notes_retry_s)
                        await asyncio.sleep(self.notes_retry_s)
                        continue
                    log.warning("notes → %s failed: %s", sink.name, exc)
                    if self.bus:
                        self.bus.publish("sink.error", meeting_id=self.meeting.id, sink=sink.name, error=str(exc))
                    break
        if out and self.on_change:
            self.on_change()
        return out

    async def aclose(self) -> None:
        for sink in self.sinks:
            try:
                await sink.aclose()
            except Exception:  # pragma: no cover
                pass
