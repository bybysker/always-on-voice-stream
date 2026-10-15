"""When items reach the sinks (end / immediate / manual), which ones, and how failures and later
edits are handled — against in-memory sinks and a fake clock."""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

import pytest

from voicestream.config import Config
from voicestream.dispatch import MAX_ATTEMPTS, Dispatcher
from voicestream.events import EventBus
from voicestream.models import AgentRun, Item, ItemKind, Meeting
from voicestream.sinks.base import Sink, SinkConfig, SinkError


class MemorySink(Sink):
    """Records every call; ``fail`` holds exceptions raised by the next calls, in order."""

    name: ClassVar[str] = "memory"
    supports_update: ClassVar[bool] = True
    supports_notes: ClassVar[bool] = True

    def __init__(self, app: Config, **cfg: Any) -> None:
        super().__init__(SinkConfig(enabled=True, **cfg), app)
        self.created: list[Item] = []
        self.updated: list[tuple[Item, str]] = []
        self.notes: list[str] = []
        self.fail: list[Exception] = []

    def _maybe_fail(self) -> None:
        if self.fail:
            raise self.fail.pop(0)

    async def push_item(self, item: Item, meeting: Meeting) -> str:
        self._maybe_fail()
        self.created.append(item.model_copy(deep=True))
        return f"{self.name}://{item.id}"

    async def update_item(self, item: Item, meeting: Meeting, ref: str) -> None:
        self._maybe_fail()
        self.updated.append((item.model_copy(deep=True), ref))

    async def push_notes(self, meeting: Meeting, notes_markdown: str) -> str | None:
        self._maybe_fail()
        self.notes.append(notes_markdown)
        return f"{self.name}://notes/{meeting.id}"

    @property
    def created_ids(self) -> list[str]:
        return [i.id for i in self.created]


class CreateOnlySink(MemorySink):
    name: ClassVar[str] = "create-only"
    supports_update: ClassVar[bool] = False
    supports_notes: ClassVar[bool] = False


def _named(name: str, app: Config, **cfg: Any) -> MemorySink:
    """Another MemorySink under its own name (sinks are keyed by name on the item)."""
    return type(f"{name.title()}Sink", (MemorySink,), {"name": name})(app, **cfg)


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _action(id: str, confidence: float = 0.9, **kw: Any) -> Item:
    return Item(id=id, kind=ItemKind.ACTION, title=kw.pop("title", f"Task {id}"), confidence=confidence, **kw)


def _edit(item: Item, **fields: Any) -> None:
    for k, v in fields.items():
        setattr(item, k, v)
    item.revision += 1


def _dispatcher(cfg: Config, meeting: Meeting, *sinks: Sink, clock: Clock | None = None) -> tuple[Dispatcher, EventBus, list[int]]:
    bus = EventBus()
    changes: list[int] = []
    d = Dispatcher(cfg, meeting, list(sinks), bus, now=clock or Clock(), on_change=lambda: changes.append(1))
    return d, bus, changes


def _events(bus: EventBus, type: str) -> list[dict[str, Any]]:
    return [e.data for e in bus.history if e.type == type]


# ------------------------------------------------------------------------- modes


async def test_end_mode_pushes_once_at_the_end_with_notes(cfg: Config, meeting: Meeting) -> None:
    meeting.items["A1"] = _action("A1")
    meeting.items["D1"] = Item(id="D1", kind=ItemKind.DECISION, title="Annual billing", confidence=0.9)
    sink = MemorySink(cfg)
    d, bus, changes = _dispatcher(cfg, meeting, sink)
    await d.finalize("# Notes")
    assert sink.created_ids == ["A1"]  # sinks take actions only by default
    item = meeting.items["A1"]
    assert item.pushed == {"memory": "memory://A1"} and item.pushed_revision == {"memory": 1}
    assert sink.notes == ["# Notes"] and meeting.notes_ref == {"memory": f"memory://notes/{meeting.id}"}
    assert _events(bus, "item.pushed") == [
        {"meeting_id": meeting.id, "item_id": "A1", "sink": "memory", "ref": "memory://A1", "action": "create"}
    ]
    assert _events(bus, "notes.pushed") == [{"meeting_id": meeting.id, "sink": "memory", "ref": f"memory://notes/{meeting.id}"}]
    assert changes
    await d.finalize(None)  # deduplicated: a second end pass creates nothing new
    assert sink.created_ids == ["A1"] and sink.updated == []


async def test_immediate_mode_waits_for_the_item_to_settle(cfg: Config, meeting: Meeting) -> None:
    cfg.dispatch.mode = "immediate"
    cfg.dispatch.stable_s = 45
    clock = Clock()
    meeting.items["A1"] = _action("A1")
    sink = MemorySink(cfg)
    d, _, _ = _dispatcher(cfg, meeting, sink, clock=clock)
    assert await d.sync() == 0  # first seen now
    clock.t += 30
    _edit(meeting.items["A1"], owner="Sam")  # refined: the stability window restarts
    assert await d.sync() == 0
    clock.t += 44
    assert await d.sync() == 0
    clock.t += 1
    assert await d.sync() == 1
    assert sink.created[0].owner == "Sam" and sink.created[0].revision == 2
    clock.t += 100
    assert await d.sync() == 0


async def test_immediate_mode_with_no_stability_window(cfg: Config, meeting: Meeting) -> None:
    cfg.dispatch.stable_s = 0
    meeting.items["A1"] = _action("A1")
    sink = MemorySink(cfg)
    d, _, _ = _dispatcher(cfg, meeting, sink)
    assert await d.sync() == 1 and sink.created_ids == ["A1"]


async def test_manual_mode_pushes_only_on_request(cfg: Config, meeting: Meeting) -> None:
    cfg.dispatch.mode = "manual"
    meeting.items["A1"] = _action("A1")
    meeting.items["A2"] = _action("A2")
    sink = MemorySink(cfg)
    d, _, _ = _dispatcher(cfg, meeting, sink)
    assert await d.push_now("A1") == {"memory": "memory://A1"}
    _edit(meeting.items["A1"], title="Send the final deck")
    await d.finalize("# Notes")
    assert sink.created_ids == ["A1"]  # A2 was never requested
    assert [(i.title, ref) for i, ref in sink.updated] == [("Send the final deck", "memory://A1")]  # pushed items stay in sync
    assert sink.notes == [] and meeting.notes_ref == {}
    assert await d.push_notes("# Notes") == {"memory": f"memory://notes/{meeting.id}"}  # explicit request


async def test_run_loop_stops_promptly(cfg: Config, meeting: Meeting) -> None:
    d, _, _ = _dispatcher(cfg, meeting, MemorySink(cfg))
    task = asyncio.ensure_future(d.run())
    await asyncio.sleep(0)
    d.stop()
    await asyncio.wait_for(task, 1.0)


# ------------------------------------------------------------------- eligibility


async def test_global_and_per_sink_confidence_thresholds(cfg: Config, meeting: Meeting) -> None:
    cfg.dispatch.min_confidence = 0.6
    meeting.items["A1"] = _action("A1", confidence=0.95)
    meeting.items["A2"] = _action("A2", confidence=0.6)
    meeting.items["A3"] = _action("A3", confidence=0.45)
    default = MemorySink(cfg)
    strict = _named("strict", cfg, min_confidence=0.9)
    lenient = _named("lenient", cfg, min_confidence=0.4)
    d, _, _ = _dispatcher(cfg, meeting, default, strict, lenient)
    await d.finalize(None)
    assert default.created_ids == ["A1", "A2"]
    assert strict.created_ids == ["A1"]
    assert lenient.created_ids == ["A1", "A2", "A3"]


async def test_mine_only_sink(cfg: Config, meeting: Meeting) -> None:
    for id, owner in (("A1", "Alex"), ("A2", "me"), ("A3", "Alex Chen"), ("A4", "Sam"), ("A5", None), ("A6", "Alexandra")):
        meeting.items[id] = _action(id, owner=owner)
    mine = MemorySink(cfg, owners="mine")
    everyone = _named("team", cfg)
    d, _, _ = _dispatcher(cfg, meeting, mine, everyone)
    await d.finalize(None)
    assert mine.created_ids == ["A1", "A2"]  # "Alex Chen" may be a client who shares your first name
    assert everyone.created_ids == ["A1", "A2", "A3", "A4", "A5", "A6"]


async def test_kinds_dismissed_and_agent_task_filters(cfg: Config, meeting: Meeting) -> None:
    meeting.items["A1"] = _action("A1")
    meeting.items["A2"] = _action("A2", dismissed=True)
    meeting.items["A3"] = _action("A3", agent=AgentRun(prompt="research it"))
    meeting.items["D1"] = Item(id="D1", kind=ItemKind.DECISION, title="Go annual", confidence=0.9)
    meeting.items["Q1"] = Item(id="Q1", kind=ItemKind.QUESTION, title="Who owns QA?", confidence=0.9)
    log = MemorySink(cfg, kinds=["action", "decision"], include_agent_tasks=False)
    d, _, _ = _dispatcher(cfg, meeting, log)
    await d.finalize(None)
    assert log.created_ids == ["A1", "D1"]


# ------------------------------------------------------------------------ updates


async def test_edits_after_a_push_are_propagated_once(cfg: Config, meeting: Meeting) -> None:
    meeting.items["A1"] = _action("A1")
    full, create_only = MemorySink(cfg), CreateOnlySink(cfg)
    d, bus, _ = _dispatcher(cfg, meeting, full, create_only)
    await d.sync(require_stable=False)
    _edit(meeting.items["A1"], due_date="2026-10-16")
    assert await d.sync(require_stable=False) == 1
    assert [(i.due_date, i.revision, ref) for i, ref in full.updated] == [("2026-10-16", 2, "memory://A1")]
    assert meeting.items["A1"].pushed_revision == {"memory": 2, "create-only": 1}
    assert create_only.updated == []
    assert await d.sync(require_stable=False) == 0  # nothing new
    assert _events(bus, "item.pushed")[-1]["action"] == "update"


async def test_an_edit_made_while_a_push_is_in_flight_is_synced_afterwards(cfg: Config, meeting: Meeting) -> None:
    meeting.items["A1"] = _action("A1")
    sink = MemorySink(cfg)
    in_flight, answered = asyncio.Event(), asyncio.Event()

    async def slow_push(item: Item, m: Meeting) -> str:
        ref = await MemorySink.push_item(sink, item, m)  # records the item as sent
        in_flight.set()
        await answered.wait()  # the external API takes its time
        return ref

    sink.push_item = slow_push  # type: ignore[method-assign]
    d, _, _ = _dispatcher(cfg, meeting, sink)
    push = asyncio.ensure_future(d.sync(require_stable=False))
    await in_flight.wait()
    _edit(meeting.items["A1"], owner="Sam")  # the extractor refines the item meanwhile
    answered.set()
    await push
    await d.sync(require_stable=False)
    assert sink.created[0].owner is None
    assert [i.owner for i, _ in sink.updated] == ["Sam"]


async def test_dismissal_after_a_push_is_propagated(cfg: Config, meeting: Meeting) -> None:
    meeting.items["A1"] = _action("A1")
    sink = MemorySink(cfg)
    d, _, _ = _dispatcher(cfg, meeting, sink)
    await d.sync(require_stable=False)
    _edit(meeting.items["A1"], dismissed=True)
    await d.finalize(None)
    assert len(sink.updated) == 1 and sink.updated[0][0].dismissed


async def test_updates_also_wait_for_stability(cfg: Config, meeting: Meeting) -> None:
    cfg.dispatch.mode = "immediate"
    cfg.dispatch.stable_s = 10
    clock = Clock()
    meeting.items["A1"] = _action("A1")
    sink = MemorySink(cfg)
    d, _, _ = _dispatcher(cfg, meeting, sink, clock=clock)
    await d.sync()
    clock.t += 10
    assert await d.sync() == 1
    _edit(meeting.items["A1"], owner="Sam")
    assert await d.sync() == 0 and sink.updated == []
    clock.t += 10
    assert await d.sync() == 1 and sink.updated[0][0].owner == "Sam"


# ------------------------------------------------------------------------ failures


async def test_non_retryable_error_is_not_retried(cfg: Config, meeting: Meeting) -> None:
    meeting.items["A1"] = _action("A1")
    meeting.items["A2"] = _action("A2")
    sink = MemorySink(cfg)
    sink.fail = [SinkError("board does not exist", retryable=False)]
    d, bus, _ = _dispatcher(cfg, meeting, sink)
    assert await d.sync(require_stable=False) == 1
    for _ in range(3):
        await d.sync(require_stable=False)
    await d.finalize(None)
    assert sink.created_ids == ["A2"] and meeting.items["A1"].pushed == {}
    assert _events(bus, "sink.error") == [
        {"meeting_id": meeting.id, "item_id": "A1", "sink": "memory", "error": "board does not exist"}
    ]


async def test_retryable_errors_are_capped(cfg: Config, meeting: Meeting) -> None:
    meeting.items["A1"] = _action("A1")
    sink = MemorySink(cfg)
    sink.fail = [SinkError("rate limited")] * (MAX_ATTEMPTS - 1) + [RuntimeError("bug in sink")] + [SinkError("x")] * 5
    d, bus, _ = _dispatcher(cfg, meeting, sink)
    for _ in range(MAX_ATTEMPTS + 3):
        await d.sync(require_stable=False)
    assert len(_events(bus, "sink.error")) == MAX_ATTEMPTS
    assert sink.created == [] and len(sink.fail) == 5  # stopped calling the sink


async def test_a_successful_retry_resets_the_attempt_count(cfg: Config, meeting: Meeting) -> None:
    meeting.items["A1"] = _action("A1")
    sink = MemorySink(cfg)
    sink.fail = [SinkError("blip")] * (MAX_ATTEMPTS - 1)
    d, _, _ = _dispatcher(cfg, meeting, sink)
    for _ in range(MAX_ATTEMPTS):
        await d.sync(require_stable=False)
    assert sink.created_ids == ["A1"]
    sink.fail = [SinkError("blip")] * (MAX_ATTEMPTS - 1)
    for rev in range(MAX_ATTEMPTS):
        _edit(meeting.items["A1"], title=f"Task v{rev}")
        await d.sync(require_stable=False)
    assert [i.title for i, _ in sink.updated] == [f"Task v{MAX_ATTEMPTS - 1}"]


async def test_one_failing_sink_does_not_block_the_others(cfg: Config, meeting: Meeting) -> None:
    meeting.items["A1"] = _action("A1")
    broken, ok = MemorySink(cfg), CreateOnlySink(cfg)
    broken.fail = [SinkError("down", retryable=False)]
    d, _, _ = _dispatcher(cfg, meeting, broken, ok)
    await d.finalize(None)
    assert meeting.items["A1"].pushed == {"create-only": "create-only://A1"}


# ---------------------------------------------------------------------- push_now


async def test_push_now_ignores_confidence_and_stability_but_not_kinds(cfg: Config, meeting: Meeting) -> None:
    cfg.dispatch.mode = "immediate"
    cfg.dispatch.stable_s = 3600
    meeting.items["A1"] = _action("A1", confidence=0.1)
    meeting.items["D1"] = Item(id="D1", kind=ItemKind.DECISION, title="Go annual")
    sink = MemorySink(cfg)
    d, _, changes = _dispatcher(cfg, meeting, sink)
    assert await d.push_now("A1") == {"memory": "memory://A1"}
    assert await d.push_now("D1") == {}  # this sink takes actions only
    assert changes
    with pytest.raises(KeyError):
        await d.push_now("A9")


async def test_push_now_updates_or_reports_existing_refs(cfg: Config, meeting: Meeting) -> None:
    meeting.items["A1"] = _action("A1")
    full, create_only = MemorySink(cfg), CreateOnlySink(cfg)
    d, _, _ = _dispatcher(cfg, meeting, full, create_only)
    await d.push_now("A1")
    _edit(meeting.items["A1"], owner="Sam")
    assert await d.push_now("A1") == {"memory": "memory://A1", "create-only": "create-only://A1"}
    assert len(full.created) == 1 and full.updated[0][0].owner == "Sam"
    assert len(create_only.created) == 1  # never created twice
    assert await d.push_now("A1", ["create-only"]) == {"create-only": "create-only://A1"}


async def test_push_now_retries_an_item_that_hit_the_cap(cfg: Config, meeting: Meeting) -> None:
    meeting.items["A1"] = _action("A1")
    sink = MemorySink(cfg)
    sink.fail = [SinkError("bad token", retryable=False), SinkError("still bad")]
    d, _, _ = _dispatcher(cfg, meeting, sink)
    await d.sync(require_stable=False)
    assert await d.push_now("A1") == {"memory": "error"}
    assert await d.push_now("A1") == {"memory": "memory://A1"}  # the user fixed the token and clicked again


# -------------------------------------------------------------------------- notes


async def test_notes_go_only_to_sinks_that_want_them(cfg: Config, meeting: Meeting) -> None:
    yes = MemorySink(cfg)
    opted_out = _named("quiet", cfg, notes=False)
    unsupported = CreateOnlySink(cfg)
    broken = _named("broken", cfg)
    broken.fail = [RuntimeError("500 from server")]
    d, bus, _ = _dispatcher(cfg, meeting, yes, opted_out, unsupported, broken)
    await d.finalize("# Notes")
    assert yes.notes == ["# Notes"] and opted_out.notes == [] and unsupported.notes == [] and broken.notes == []
    assert meeting.notes_ref == {"memory": f"memory://notes/{meeting.id}"}
    assert [e["sink"] for e in _events(bus, "sink.error")] == ["broken"]
    assert _events(bus, "sink.error")[0]["meeting_id"] == meeting.id  # like the item errors
    await d.finalize("")
    assert yes.notes == ["# Notes"]  # empty notes are not published


async def test_the_slack_digest_gets_the_notes_last_so_it_can_link_the_others(cfg: Config, meeting: Meeting) -> None:
    seen: dict[str, set[str]] = {}  # sink -> the notes refs it could link to

    class Recorder(MemorySink):
        async def push_notes(self, meeting: Meeting, notes_markdown: str) -> str | None:
            seen[self.name] = set(meeting.notes_ref)
            return await super().push_notes(meeting, notes_markdown)

    sinks = [type(f"{n.title()}Recorder", (Recorder,), {"name": n})(cfg) for n in ("slack", "notion", "memory")]
    d, _, _ = _dispatcher(cfg, meeting, *sinks)
    await d.push_notes("# Notes")
    assert list(seen) == ["notion", "memory", "slack"]  # otherwise in config order
    assert seen["slack"] == {"notion", "memory"}
