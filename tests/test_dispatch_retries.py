"""Retry rules: failed pushes recover after an edit, uncertain creates never auto-resend,
manual pushes never create dismissed/cancelled items, notes get one more try."""

from __future__ import annotations

from voicestream.config import Config
from voicestream.dispatch import MAX_ATTEMPTS, Dispatcher
from voicestream.models import Item, ItemKind, ItemStatus, Meeting
from voicestream.sinks.base import Sink, SinkConfig, SinkError


class _Flaky(Sink):
    name = "flaky"
    supports_update = True
    supports_notes = True

    def __init__(self, cfg: SinkConfig, app: Config, errors: list[SinkError | None]) -> None:
        super().__init__(cfg, app)
        self.errors = errors
        self.creates = 0
        self.notes = 0

    async def push_item(self, item, meeting):  # type: ignore[override]
        self.creates += 1
        err = self.errors.pop(0) if self.errors else None
        if err:
            raise err
        return f"ref-{item.id}"

    async def push_notes(self, meeting, notes_markdown):  # type: ignore[override]
        self.notes += 1
        err = self.errors.pop(0) if self.errors else None
        if err:
            raise err
        return "notes-ref"


def _setup(cfg: Config, errors: list[SinkError | None]) -> tuple[Meeting, _Flaky, Dispatcher]:
    m = Meeting()
    m.items["A1"] = Item(id="A1", kind=ItemKind.ACTION, title="Book the venue", owner="Alex", confidence=0.9)
    sink = _Flaky(SinkConfig(enabled=True), cfg, errors)
    d = Dispatcher(cfg, m, [sink])
    d.notes_retry_s = 0
    return m, sink, d


async def test_definite_failure_gets_a_fresh_start_after_an_edit(cfg: Config) -> None:
    m, sink, d = _setup(cfg, [SinkError("not authorized (-1743)", retryable=False)])
    await d.sync(require_stable=False)
    await d.sync(require_stable=False)
    assert sink.creates == 1  # blocked until something changes
    m.items["A1"].revision += 1  # e.g. you fixed the permission and edited the item
    await d.sync(require_stable=False)
    assert sink.creates == 2 and m.items["A1"].pushed == {"flaky": "ref-A1"}


async def test_uncertain_create_is_never_resent_automatically(cfg: Config) -> None:
    m, sink, d = _setup(cfg, [SinkError("timeout — may have been applied", retryable=False, uncertain=True)])
    await d.sync(require_stable=False)
    m.items["A1"].revision += 1
    await d.sync(require_stable=False)
    assert sink.creates == 1  # could already exist: no duplicate
    assert (await d.push_now("A1")) == {"flaky": "ref-A1"}  # you checked, and asked for it
    assert sink.creates == 2


async def test_retryable_failures_are_capped(cfg: Config) -> None:
    m, sink, d = _setup(cfg, [SinkError("busy", retryable=True)] * (MAX_ATTEMPTS + 2))
    for _ in range(MAX_ATTEMPTS + 2):
        await d.sync(require_stable=False)
    assert sink.creates == MAX_ATTEMPTS


async def test_manual_push_never_creates_dismissed_or_cancelled_items(cfg: Config) -> None:
    m, sink, d = _setup(cfg, [])
    m.items["A1"].status = ItemStatus.CANCELLED
    assert (await d.push_now("A1"))["flaky"].startswith("skipped")
    m.items["A1"].status = ItemStatus.OPEN
    m.items["A1"].dismissed = True
    assert (await d.push_now("A1"))["flaky"].startswith("skipped")
    assert sink.creates == 0


async def test_notes_get_one_more_try_on_a_transient_error(cfg: Config) -> None:
    m, sink, d = _setup(cfg, [SinkError("502", retryable=True)])
    assert await d.push_notes("# Notes") == {"flaky": "notes-ref"}
    assert sink.notes == 2 and m.notes_ref["flaky"] == "notes-ref"


async def test_notes_are_not_retried_on_a_permanent_error(cfg: Config) -> None:
    m, sink, d = _setup(cfg, [SinkError("bad config", retryable=False)])
    assert await d.push_notes("# Notes") == {}
    assert sink.notes == 1
