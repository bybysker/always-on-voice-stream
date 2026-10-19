"""Past meetings for the app's Meetings screen (voicestream/app/meetings.py), on disk and in memory."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_session import Harness, IdleSource
from test_summarizer import _missing, _summary

from helpers import seg
from voicestream.app import meetings as meetings_mod
from voicestream.app.meetings import MeetingBusy, MeetingNotFound, Meetings, meeting_day, resolve_folder, summarize_meeting
from voicestream.brain.llm import ScriptedLLM
from voicestream.config import Config
from voicestream.costs import CostMeter
from voicestream.events import EventBus
from voicestream.models import Item, ItemKind, ItemStatus, Meeting
from voicestream.sinks.base import Sink, SinkConfig
from voicestream.storage import atomic_write, load_meeting


class Rec(Sink):
    name = "rec"
    supports_update = True
    supports_notes = True

    def __init__(self, app: Config) -> None:
        super().__init__(SinkConfig(enabled=True), app)
        self.calls: list[tuple[str, str]] = []
        self.closed = False

    async def push_item(self, item: Item, meeting: Meeting) -> str:
        self.calls.append(("create", item.id))
        return f"rec:{item.id}"

    async def update_item(self, item: Item, meeting: Meeting, ref: str) -> None:
        self.calls.append(("update", item.id))

    async def push_notes(self, meeting: Meeting, notes_markdown: str) -> str | None:
        self.calls.append(("notes", meeting.id))
        return "rec:notes"

    async def aclose(self) -> None:
        self.closed = True


class StubHost:
    """The parts of AppHost that Meetings uses."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.bus = EventBus()
        self.costs = CostMeter(cfg, self.bus)
        self.assistant: Any = None
        self.tasks: list[asyncio.Task[Any]] = []

    def track(self, task: asyncio.Task[Any]) -> None:
        self.tasks.append(task)


def _save(cfg: Config, name: str, *, started: datetime, ended: bool = True, title: str | None = None,
          notes: bool = True) -> Path:
    m = Meeting(title=title or name, started_at=started, ended_at=started + timedelta(minutes=30) if ended else None)
    m.segments.append(seg("I'll send the deck by Friday.", "mic", t=0.0, dur=1800.0))
    m.items["A1"] = Item(id="A1", kind=ItemKind.ACTION, title="Send the deck", owner="Alex", confidence=0.9)
    m.items["A2"] = Item(id="A2", kind=ItemKind.ACTION, title="Book the venue", owner="Sam", confidence=0.9,
                         pushed={"rec": "rec:A2"}, pushed_revision={"rec": 1})
    m.items["D1"] = Item(id="D1", kind=ItemKind.DECISION, title="Annual billing")
    m.items["Q1"] = Item(id="Q1", kind=ItemKind.QUESTION, title="Who owns it", dismissed=True)
    m.tldr = f"{title or name} in short"
    folder = cfg.data_path / "sessions" / name
    atomic_write(folder / "meeting.json", m.model_dump_json(indent=1))
    if notes:
        atomic_write(folder / "notes.md", f"# {m.title}\n")
    return folder


WED = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)


@pytest.fixture
def saved(cfg: Config) -> dict[str, Path]:
    return {
        "old": _save(cfg, "2026-10-14_110000-planning", started=WED, title="Q4 planning"),
        # imported later (filed by import time) but held before the planning meeting
        "imported": _save(cfg, "2026-10-15_080000-import", started=WED - timedelta(days=2), title="Vendor call"),
        "live": _save(cfg, "2026-10-16_100000", started=WED + timedelta(days=2), ended=False, notes=False),
    }


def test_list_rows_order_search_and_paging(cfg: Config, saved: dict[str, Path]) -> None:
    (cfg.data_path / "sessions" / "2026-10-17_090000").mkdir()
    (cfg.data_path / "sessions" / "2026-10-17_090000" / "meeting.json").write_text("{broken", encoding="utf-8")
    m = Meetings(StubHost(cfg))
    out = m.list()
    assert out["total"] == 3  # the damaged one is skipped, not fatal
    assert [r["folder"] for r in out["meetings"]] == ["2026-10-16_100000", "2026-10-14_110000-planning", "2026-10-15_080000-import"]
    row = out["meetings"][1]
    assert (row["title"], row["tldr"], row["in_progress"], row["live"], row["has_notes"]) == ("Q4 planning", "Q4 planning in short", False, False, True)
    assert row["counts"] == {"action": 2, "decision": 1, "question": 0, "note": 0}  # the dismissed one isn't counted
    assert row["my_actions"] == 1 and row["pushed"] == {"rec": 1} and row["duration_s"] == 1800.0
    assert "segments" not in row and "items" not in row  # a list row never carries the transcript
    assert out["meetings"][0]["in_progress"] and not out["meetings"][0]["has_notes"]
    assert [r["title"] for r in m.list(q="VENDOR")["meetings"]] == ["Vendor call"]
    assert m.list(q="book the venue")["total"] == 3  # item titles are searched too
    page = m.list(limit=1, offset=1)
    assert page["total"] == 3 and [r["folder"] for r in page["meetings"]] == ["2026-10-14_110000-planning"]


def test_list_is_cached_until_the_file_changes(cfg: Config, saved: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    m = Meetings(StubHost(cfg))
    m.list()
    loads: list[Path] = []
    real = meetings_mod.load_meeting
    monkeypatch.setattr(meetings_mod, "load_meeting", lambda d: loads.append(Path(d)) or real(d))
    m.list()
    assert loads == []
    meeting, folder = real(saved["old"])
    meeting.title = "Renamed"
    atomic_write(folder / "meeting.json", meeting.model_dump_json())
    assert "Renamed" in [r["title"] for r in m.list()["meetings"]]
    assert loads == [saved["old"]]


@pytest.mark.parametrize("name", ["..", "../..", "../data", "/etc", "sessions", "2026-10-14_110000", "", "a/../2026-10-14_110000-planning"])
def test_folder_names_are_matched_never_joined(cfg: Config, saved: dict[str, Path], name: str) -> None:
    with pytest.raises(MeetingNotFound):
        resolve_folder(cfg, name)
    with pytest.raises(MeetingNotFound):
        Meetings(StubHost(cfg)).detail(name)


def test_detail(cfg: Config, saved: dict[str, Path]) -> None:
    d = Meetings(StubHost(cfg)).detail("2026-10-14_110000-planning")
    assert d["folder"] == "2026-10-14_110000-planning" and not d["in_memory"]
    assert d["meeting"]["title"] == "Q4 planning" and d["notes_markdown"] == "# Q4 planning\n"
    assert d["meeting"]["segments"][0]["text"].startswith("I'll send")


def test_edits_on_disk_rewrite_the_file_and_the_notes(cfg: Config, saved: dict[str, Path]) -> None:
    host = StubHost(cfg)
    m = Meetings(host)
    item = m.edit_item("2026-10-14_110000-planning", "A1", status="done", due_text="vendredi")
    assert item["status"] == "done"
    assert item["due_date"] == "2026-10-16"  # "vendredi" after the meeting (a Wednesday), not after today
    meeting, folder = load_meeting(saved["old"])
    assert meeting.items["A1"].status == ItemStatus.DONE
    assert "- [x] **Send the deck**" in (folder / "notes.md").read_text(encoding="utf-8")
    added = m.add_item("2026-10-14_110000-planning", "action", "Call Sam back", owner="me")
    assert added["id"] == "A3" and load_meeting(saved["old"])[0].items["A3"].owner == "Alex"
    assert m.dismiss_item("2026-10-14_110000-planning", "D1", True)["dismissed"] is True
    assert m.dismiss_item("2026-10-14_110000-planning", "D1", False)["dismissed"] is False
    assert [e.data["folder"] for e in host.bus.history if e.type == "meeting.saved"] == ["2026-10-14_110000-planning"] * 4
    with pytest.raises(KeyError):
        m.edit_item("2026-10-14_110000-planning", "ZZ", title="x")
    with pytest.raises(MeetingBusy):  # another voicestream is recording it: its next save would undo the edit
        m.edit_item("2026-10-16_100000", "A1", title="x")
    assert load_meeting(saved["live"])[0].items["A1"].title == "Send the deck"


async def test_an_edit_of_a_pushed_item_is_propagated(cfg: Config, saved: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    rec = Rec(cfg)
    monkeypatch.setattr("voicestream.sinks.base.build_sinks", lambda app, only=None: [rec])
    host = StubHost(cfg)
    Meetings(host).edit_item("2026-10-14_110000-planning", "A2", status="done")
    await asyncio.gather(*host.tasks)
    assert rec.calls == [("update", "A2")] and rec.closed  # temporary sinks are closed afterwards
    meeting, _ = load_meeting(saved["old"])
    assert meeting.items["A2"].pushed_revision["rec"] == meeting.items["A2"].revision


async def test_push_from_disk(cfg: Config, saved: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    rec = Rec(cfg)
    monkeypatch.setattr("voicestream.sinks.base.build_sinks", lambda app, only=None: [rec])
    meeting, folder = load_meeting(saved["old"])
    meeting.summary_markdown = "# Q4 planning\n"
    atomic_write(folder / "meeting.json", meeting.model_dump_json())
    host = StubHost(cfg)
    result = await Meetings(host).push("2026-10-14_110000-planning")
    assert result["items"] == {"A1": {"rec": "rec:A1"}}  # actions only (the sink's kinds)
    assert result["notes"] == {"rec": "rec:notes"} and result["errors"] == [] and result["count"] == 1
    assert ("create", "A1") in rec.calls and ("create", "A2") not in rec.calls  # A2 was already there
    assert load_meeting(saved["old"])[0].items["A1"].pushed == {"rec": "rec:A1"}
    assert rec.closed
    with pytest.raises(MeetingBusy):
        await Meetings(host).push("2026-10-16_100000")
    forced = await Meetings(host).push("2026-10-16_100000", force=True, notes=False)
    assert forced["count"] == 1


async def test_push_reports_failures_per_item(cfg: Config, saved: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    from voicestream.sinks.base import SinkError

    class Broken(Rec):
        async def push_item(self, item: Item, meeting: Meeting) -> str:
            raise SinkError("NOTION_TOKEN is not set", retryable=False)

    monkeypatch.setattr("voicestream.sinks.base.build_sinks", lambda app, only=None: [Broken(cfg)])
    result = await Meetings(StubHost(cfg)).push("2026-10-14_110000-planning", notes=False)
    assert result["items"]["A1"] == {"rec": "error"} and result["errors"] == ["A1 → rec: NOTION_TOKEN is not set"]


async def test_summarize_resolves_dates_from_the_meeting_day(cfg: Config, saved: dict[str, Path]) -> None:
    meeting, _ = load_meeting(saved["old"])
    assert meeting_day(cfg, meeting).isoformat() == "2026-10-14"
    llm = ScriptedLLM([_summary(missing_items=[_missing("Book the offsite venue", due_text="by Friday", segment_refs=[0])])])
    notes = await summarize_meeting(cfg, meeting, llm)
    venue = next(i for i in meeting.items.values() if i.title == "Book the offsite venue")
    assert venue.due_date == "2026-10-16"  # that Friday, whatever day it is now
    assert notes.startswith("# Q4 planning") and meeting.tldr == "We planned Q4."


async def test_summarize_in_the_background(cfg: Config, saved: dict[str, Path]) -> None:
    host = StubHost(cfg)  # offline (heuristic): the notes are rebuilt without a model
    m = Meetings(host)
    m.start_summarize("2026-10-14_110000-planning")
    await asyncio.gather(*host.tasks)
    done = [e for e in host.bus.history if e.type == "meeting.notes_updated"]
    assert done[-1].data["folder"] == "2026-10-14_110000-planning" and "error" not in done[-1].data
    assert (saved["old"] / "notes.md").read_text(encoding="utf-8").startswith("# Q4 planning")
    with pytest.raises(MeetingBusy):
        m.start_summarize("2026-10-16_100000")
    with pytest.raises(MeetingNotFound):
        m.start_summarize("../2026-10-14_110000-planning")


async def test_a_meeting_held_in_memory_is_changed_there(cfg: Config) -> None:
    h = Harness(cfg, [IdleSource("mic")], always_on=False)
    host = StubHost(cfg)
    host.assistant = h.a
    host.bus = h.bus
    run = asyncio.ensure_future(h.a.run())
    try:
        await h.wait_for("meeting.started")
        await h.say("I'll send the deck to Acme by Friday.")
        await h.a.extract_now()
        m = Meetings(host)
        folder = h.a.current.store.dir.name
        row = m.list()["meetings"][0]
        assert row["folder"] == folder and row["live"] and row["counts"]["action"] == 1
        assert m.detail(folder)["in_memory"]
        item = m.edit_item(folder, "A1", owner="Sam")
        assert item["owner"] == "Sam" and h.a.current.meeting.items["A1"].owner == "Sam"  # the live copy itself
        with pytest.raises(MeetingBusy):
            m.start_summarize(folder)  # its notes are written when it ends
        await h.a.end_meeting()
        result = await m.push(folder)  # the session's own dispatcher: nothing pushed twice
        assert result["errors"] == [] and h.sink.todo.read_text(encoding="utf-8").count("Send the deck") == 1
        h.a.stop()
        await asyncio.wait_for(run, 30)
    finally:
        run.cancel()
