"""Decides when items are pushed to sinks, and keeps external copies in sync."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Collection

from .config import Config
from .events import EventBus
from .models import Item, ItemStatus, Meeting
from .sinks.base import Sink, SinkError

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 4
POLL_S = 5.0
"""How often the background loop pushes (``immediate`` mode) and syncs pushed items."""
RETRY_DELAYS_S = (5.0, 15.0, 45.0)
"""After a meeting's final push: when to try again the pushes that failed for a reason that may pass."""
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
        self.retry_delays_s = RETRY_DELAYS_S
        self._lock = asyncio.Lock()
        self._stopped = asyncio.Event()
        self._hurry = asyncio.Event()

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
            if self.on_change:
                self.on_change()  # save the ref now: an interrupted run must not create it again
            if self.bus:
                self.bus.publish(
                    "item.pushed", meeting_id=self.meeting.id, item_id=item.id, sink=sink.name, ref=item.pushed.get(sink.name), action=action
                )
            log.info("%s %s → %s", "pushed" if action == "create" else "updated", item.id, sink.name)
            return True
        except asyncio.CancelledError:
            if action == "create":
                # cut off mid-request: it may exist already, so only a manual push may try again
                self._attempts[key] = (-1, MAX_ATTEMPTS)
            raise
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

    async def sync(
        self, *, require_stable: bool = True, only: set[str] | None = None, sink_names: Collection[str] | None = None
    ) -> int:
        """Push/update everything that is due (to ``sink_names`` only, if given). Returns the number of
        successful operations."""
        async with self._lock:
            done = 0
            for item in list(self.meeting.items.values()):
                if only is not None and item.id not in only:
                    continue
                if require_stable and not self._stable(item):
                    continue
                for sink in self.sinks:
                    if sink_names is not None and sink.name not in sink_names:
                        continue
                    action = self._todo(item, sink)
                    if action and await self._push(item, sink, action):
                        done += 1
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
                await asyncio.wait_for(self._stopped.wait(), timeout=POLL_S)
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

    def _retryable(self) -> list[tuple[Item, Sink]]:
        """The (item, sink) pushes whose current revision failed for a reason that may pass: not a refusal,
        not a create that may have been applied."""
        sinks = {s.name: s for s in self.sinks}
        pairs = []
        for (item_id, sink_name), (rev, failures) in self._attempts.items():
            item, sink = self.meeting.items.get(item_id), sinks.get(sink_name)
            if item is not None and sink is not None and rev == item.revision and 0 < failures < MAX_ATTEMPTS:
                pairs.append((item, sink))
        return pairs

    async def retry_failed(self) -> None:
        """After :meth:`finalize`: try the transiently failed pushes (just those) again a few times, with
        back-off. Nothing else would: the loop that keeps pushed items in sync has stopped with the meeting."""
        for delay in self.retry_delays_s:
            if not self._retryable():
                return
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._hurry.wait(), delay)
            async with self._lock:
                pairs = self._retryable()
                if pairs:
                    log.info("retrying %d failed push(es)", len(pairs))
                for item, sink in pairs:
                    if action := self._todo(item, sink):  # e.g. not if it was dismissed meanwhile
                        await self._push(item, sink, action)
            if self._hurry.is_set():
                return  # shutting down: that was the last go

    def hurry(self) -> None:
        """Shutting down: make :meth:`retry_failed` try once more now and stop."""
        self._hurry.set()

    async def push_notes(self, notes_markdown: str, sink_names: Collection[str] | None = None) -> dict[str, str]:
        out: dict[str, str] = {}
        for sink in sorted(self.sinks, key=lambda s: s.name in LINKS_OTHER_NOTES):
            if not (sink.supports_notes and sink.cfg.notes) or (sink_names is not None and sink.name not in sink_names):
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
