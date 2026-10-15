"""Local Markdown sink: the running TODO.md checklist and per-meeting notes files."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from voicestream.config import Config
from voicestream.dispatch import Dispatcher
from voicestream.models import AgentRun, AgentStatus, Item, ItemKind, ItemStatus, Meeting
from voicestream.sinks.base import build_sinks
from voicestream.sinks.markdown import MarkdownConfig, MarkdownSink

STARTED = datetime(2026, 10, 14, 13, 30, 12, tzinfo=timezone.utc)


def _sink(cfg: Config, **kw: Any) -> MarkdownSink:
    return MarkdownSink(MarkdownConfig(**kw), cfg)


def _item(id: str = "A1", **kw: Any) -> Item:
    return Item(id=id, kind=ItemKind.ACTION, title=kw.pop("title", f"Task {id}"), **kw)


def _meeting(id: str = "20261014-133012", title: str | None = "Weekly sync") -> Meeting:
    return Meeting(id=id, title=title, started_at=STARTED)


def _lines(sink: MarkdownSink) -> list[str]:
    return sink.todo.read_text(encoding="utf-8").splitlines()


async def test_first_push_creates_the_checklist(cfg: Config) -> None:
    sink = _sink(cfg)
    m = _meeting()
    ref = await sink.push_item(_item(title="Send the pricing deck", owner="Sam Lee", due_date="2026-10-16"), m)
    assert sink.todo == cfg.data_path / "TODO.md"
    assert ref == f"file://{sink.todo}#20261014-133012/A1"
    assert _lines(sink) == [
        "# TODO",
        "",
        "- [ ] Send the pricing deck @Sam_Lee 📅 2026-10-16 _(Weekly sync)_ <!-- vs:20261014-133012/A1 -->",
    ]
    assert sink.todo.read_text(encoding="utf-8").endswith("-->\n")


async def test_line_falls_back_to_spoken_due_and_start_time_label(cfg: Config) -> None:
    sink = _sink(cfg)
    m = _meeting(title=None)
    await sink.push_item(_item(due_text="next week"), m)
    label = STARTED.astimezone().strftime("%Y-%m-%d %H:%M")
    assert _lines(sink)[2] == f"- [ ] Task A1 📅 next week _({label})_ <!-- vs:20261014-133012/A1 -->"


async def test_newest_items_go_right_under_the_header(cfg: Config) -> None:
    sink = _sink(cfg)
    m = _meeting()
    for i in (1, 2, 3):
        await sink.push_item(_item(f"A{i}"), m)
    assert [ln.split(" _(")[0] for ln in _lines(sink)] == ["# TODO", "", "- [ ] Task A3", "- [ ] Task A2", "- [ ] Task A1"]


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"status": ItemStatus.DONE}, "- [x] Task A1"),
        ({"dismissed": True}, "- [x] ~~Task A1~~"),
        ({"status": ItemStatus.CANCELLED}, "- [ ] ~~Task A1~~"),
        ({"title": "Send the final deck", "owner": "Sarah"}, "- [ ] Send the final deck @Sarah"),
        ({"agent": AgentRun(prompt="p", status=AgentStatus.DONE)}, "- [ ] Task A1 🤖 done"),
    ],
)
async def test_update_rewrites_the_line_in_place(cfg: Config, change: dict[str, Any], expected: str) -> None:
    sink = _sink(cfg)
    m = _meeting()
    a1, a2 = _item("A1"), _item("A2")
    ref = await sink.push_item(a1, m)
    await sink.push_item(a2, m)
    before = _lines(sink)
    await sink.update_item(a1.model_copy(update=change), m, ref)
    after = _lines(sink)
    assert len(after) == len(before)
    assert after[:3] == before[:3]  # header and the other item untouched
    assert after[3].startswith(expected + " _(Weekly sync)_") and after[3].endswith("<!-- vs:20261014-133012/A1 -->")


async def test_same_item_id_in_two_meetings_stays_separate(cfg: Config) -> None:
    sink = _sink(cfg)
    monday, tuesday = _meeting("20261012-090000", "Monday standup"), _meeting("20261013-090000", "Tuesday standup")
    await sink.push_item(_item("A1", title="Book the venue"), monday)
    ref = await sink.push_item(_item("A1", title="Draft the agenda"), tuesday)
    await sink.update_item(_item("A1", title="Draft the agenda", status=ItemStatus.DONE), tuesday, ref)
    assert _lines(sink)[2:] == [
        "- [x] Draft the agenda _(Tuesday standup)_ <!-- vs:20261013-090000/A1 -->",
        "- [ ] Book the venue _(Monday standup)_ <!-- vs:20261012-090000/A1 -->",
    ]


async def test_user_edits_to_the_file_are_preserved(cfg: Config) -> None:
    sink = _sink(cfg)
    sink.todo.parent.mkdir(parents=True)
    sink.todo.write_text("# My tasks\n\n- [ ] Water the plants\n\nSome notes I keep here.\n", encoding="utf-8")
    m = _meeting()
    ref = await sink.push_item(_item(), m)
    await sink.update_item(_item(status=ItemStatus.DONE), m, ref)
    lines = _lines(sink)
    assert lines[:2] == ["# My tasks", ""]
    assert lines[2].startswith("- [x] Task A1")
    assert lines[3:] == ["- [ ] Water the plants", "", "Some notes I keep here."]


@pytest.mark.xfail(strict=True, reason="BUG: a TODO.md holding only its header line gets new items inserted above the header")
async def test_header_only_file_keeps_its_header_first(cfg: Config) -> None:
    sink = _sink(cfg)
    sink.todo.parent.mkdir(parents=True)
    sink.todo.write_text("# TODO\n", encoding="utf-8")
    await sink.push_item(_item(), _meeting())
    assert _lines(sink)[0] == "# TODO"


async def test_concurrent_pushes_lose_no_lines(cfg: Config) -> None:
    sink = _sink(cfg)
    m = _meeting()
    await asyncio.gather(*(sink.push_item(_item(f"A{i}"), m) for i in range(1, 21)))
    assert sum("<!-- vs:" in ln for ln in _lines(sink)) == 20


async def test_notes_file_is_named_after_start_time_and_title(cfg: Config) -> None:
    sink = _sink(cfg)
    stamp = STARTED.astimezone().strftime("%Y-%m-%d_%H%M")
    ref = await sink.push_notes(_meeting(title="Q4 Planning: Acme & Globex"), "# Notes\n")
    path = cfg.data_path / "notes" / f"{stamp}-q4-planning-acme-globex.md"
    assert ref == f"file://{path}" and path.read_text(encoding="utf-8") == "# Notes\n"
    await sink.push_notes(_meeting(title=None), "untitled")
    assert (cfg.data_path / "notes" / f"{stamp}-meeting.md").read_text(encoding="utf-8") == "untitled"


async def test_custom_locations(cfg: Config, tmp_path: Path) -> None:
    sink = _sink(cfg, todo_file=str(tmp_path / "vault" / "Tasks.md"), notes_dir=str(tmp_path / "vault" / "Meetings"))
    await sink.push_item(_item(), _meeting())
    await sink.push_notes(_meeting(), "notes")
    assert (tmp_path / "vault" / "Tasks.md").is_file()
    assert len(list((tmp_path / "vault" / "Meetings").glob("*-weekly-sync.md"))) == 1
    assert str(tmp_path / "vault" / "Tasks.md") in await sink.check()


def test_markdown_is_enabled_by_default(cfg: Config) -> None:
    (sink,) = build_sinks(cfg)
    assert isinstance(sink, MarkdownSink) and sink.supports_update and sink.supports_notes


async def test_dispatcher_end_to_end(cfg: Config) -> None:
    m = _meeting()
    m.items["A1"] = _item("A1", title="Send the deck", confidence=0.9)
    m.items["A2"] = _item("A2", title="Maybe look at logos", confidence=0.3)
    m.items["D1"] = Item(id="D1", kind=ItemKind.DECISION, title="Annual billing only", confidence=0.9)
    sink = _sink(cfg)
    d = Dispatcher(cfg, m, [sink])
    await d.finalize("# Weekly sync\n")
    assert [ln.split(" _(")[0] for ln in _lines(sink)[2:]] == ["- [ ] Send the deck"]
    assert m.items["A1"].pushed["markdown"].endswith("#20261014-133012/A1")
    assert m.notes_ref["markdown"].startswith("file://") and m.notes_ref["markdown"].endswith("-weekly-sync.md")
    m.items["A1"].status = ItemStatus.DONE
    m.items["A1"].revision += 1
    assert await d.sync(require_stable=False) == 1
    assert _lines(sink)[2].startswith("- [x] Send the deck")
