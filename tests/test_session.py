"""The always-on Assistant end to end, offline: replayed WAV files → fake transcription → scripted
Claude → Markdown sink, plus meeting splitting, echo suppression, agents and the control API."""

from __future__ import annotations

import asyncio
import itertools
import typing
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from pydantic import BaseModel

from helpers import bursts, noise, seg, write_wav
from voicestream import models
from voicestream.agents.runner import Engine, EngineResult
from voicestream.audio.base import AudioSource, Clock, float_to_int16
from voicestream.audio.file import WavFileSource
from voicestream.brain.llm import ScriptedLLM
from voicestream.brain.schema import WireExtraction, WireNewItem, WireSummary, WireUpdate
from voicestream.config import Config
from voicestream.costs import CostMeter
from voicestream.events import EventBus
from voicestream.models import AgentStatus, AudioFrame, Event, TranscriptSegment
from voicestream.session import Assistant
from voicestream.sinks.markdown import MarkdownConfig, MarkdownSink
from voicestream.storage import load_meeting
from voicestream.transcribe.base import FakeTranscriber

LIFECYCLE = {"assistant.started", "meeting.started", "segment", "meeting.ending", "meeting.ended", "assistant.stopped"}


# --------------------------------------------------------------------- helpers


def _blank(annotation: Any) -> Any:
    origin = typing.get_origin(annotation)
    if origin is list:
        return []
    if origin is typing.Literal:
        return typing.get_args(annotation)[0]
    return {str: "", bool: False, float: 0.0, int: 0}[annotation]


M = typing.TypeVar("M", bound=BaseModel)


def _wire(schema: type[M], **fields: Any) -> M:
    """A wire answer; required fields not given get their 'absent' value ('' / [] / first literal), so
    the scripted replies keep validating as the schemas grow."""
    blanks = {name: _blank(f.annotation) for name, f in schema.model_fields.items() if f.is_required()}
    return schema.model_validate(blanks | fields)


def _new(kind: str, title: str, refs: list[int], **kw: Any) -> WireNewItem:
    return _wire(WireNewItem, kind=kind, title=title, segment_refs=refs, priority="none", **kw)


def _update(id: str, **kw: Any) -> WireUpdate:
    return _wire(WireUpdate, id=id, status="unchanged", delegate="unchanged", **kw)


class IdleSource(AudioSource):
    """A live input that only hears room noise; tests inject speech through ``Assistant._route``."""

    async def frames(self) -> AsyncIterator[AudioFrame]:
        n = 0
        while not self.stopped:
            yield AudioFrame(self.name, float_to_int16(noise(0.05, seed=n)), self.clock.now())
            n += 1
            await asyncio.sleep(0.05)


class FakeEngine(Engine):
    name = "fake"

    def __init__(self, gate: asyncio.Event | None = None) -> None:
        self.gate = gate
        self.prompts: list[str] = []

    async def run(self, prompt: str, workspace: Path) -> EngineResult:
        self.prompts.append(prompt)
        if self.gate is not None:
            await self.gate.wait()
        (workspace / "pricing.md").write_text("| Globex | $49 |\n| Initech | $59 |\n", encoding="utf-8")
        return EngineResult(ok=True, summary="Globex is about 20% cheaper than Initech.", cost_usd=0.25,
                            files=["TASK.md", "pricing.md"])


class Harness:
    def __init__(self, cfg: Config, sources: list[AudioSource], scripts: dict[str, list[str]] | None = None,
                 llm: ScriptedLLM | None = None, clock: Clock | None = None, **kw: Any) -> None:
        self.bus = EventBus(history=5000)
        self.costs = CostMeter(cfg, self.bus)
        if llm is not None:
            llm.on_usage = self.costs.add_llm
        self.transcribers: dict[str, FakeTranscriber] = {}

        def transcriber(source: str) -> FakeTranscriber:
            t = FakeTranscriber((scripts or {}).get(source, []), vad=cfg.vad, on_usage=self.costs.add_stt)
            self.transcribers[source] = t
            return t

        self.sink = MarkdownSink(MarkdownConfig(), cfg)
        self.a = Assistant(cfg, sources=sources, transcriber_factory=transcriber, llm=llm, sinks=[self.sink],
                           bus=self.bus, costs=self.costs, **kw)
        if clock is not None:
            self.a.clock = clock  # sources and meetings share one timeline
        self.a.idle_poll_s = 0.05

    def types(self, keep: set[str] = LIFECYCLE) -> list[str]:
        return [e.type for e in self.bus.history if e.type in keep]

    def events(self, type: str) -> list[Event]:
        return [e for e in self.bus.history if e.type == type]

    async def wait_for(self, type: str, pred: Callable[[Event], bool] = lambda e: True, timeout: float = 10.0) -> Event:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            for e in self.bus.history:
                if e.type == type and pred(e):
                    return e
            await asyncio.sleep(0.01)
        raise AssertionError(f"no {type} event within {timeout}s; saw {sorted({e.type for e in self.bus.history})}")

    async def say(self, text: str, source: str = "mic") -> None:
        await self.a._route(seg(text, source, t=self.a.clock.now(), dur=2.0))

    def todo(self) -> str:
        return self.sink.todo.read_text(encoding="utf-8")


def _wav_sources(tmp_path: Path, clock: Clock, **signals: np.ndarray) -> list[AudioSource]:
    return [WavFileSource(write_wav(tmp_path / f"{name}.wav", sig), name=name, clock=clock, speed=0) for name, sig in signals.items()]


def _session_dirs(cfg: Config) -> list[Path]:
    base = cfg.data_path / "sessions"
    return sorted(base.iterdir()) if base.is_dir() else []


# ------------------------------------------------------------- end to end (Claude)


async def test_recording_to_notes_todos_and_costs(cfg: Config, tmp_path: Path) -> None:
    cfg.brain.provider = "anthropic"
    lines = [
        "Sam, could you send the revised pricing deck to Acme by Friday?",
        "Sure. I'll also draft the launch email.",
        "Great, that's everything for today.",
    ]
    extraction = _wire(
        WireExtraction,
        new_items=[
            _new("action", "Send revised pricing deck to Acme", [0], owner="Sam", due_text="by Friday",
                 due_date="2026-10-16", confidence=0.9, evidence="could you send the revised pricing deck to Acme by Friday"),
            _new("action", "Draft the launch email", [1], owner="Alex", confidence=0.85, evidence="I'll also draft the launch email"),
            _new("action", "Maybe redo the logo", [2], owner="", confidence=0.3),
        ],
        participants=["Sam"],
    )
    summary = _wire(
        WireSummary,
        title="Acme pricing sync", tldr="Sam sends Acme the revised pricing; Alex drafts the launch email.",
        notes_markdown="## Summary\n\n- Acme asked twice for revised pricing.",
        updates=[_update("A2", due_date="2026-10-20")], participants=["Sam"], language="en",
    )
    llm = ScriptedLLM([extraction, summary])
    clock = Clock()
    h = Harness(cfg, _wav_sources(tmp_path, clock, mic=bursts(3)), {"mic": lines}, llm=llm, clock=clock)
    await asyncio.wait_for(h.a.run(), 30)

    # what Claude was asked: the live pass sees numbered lines, the summary sees everything + live items
    assert [c["schema"] for c in llm.calls] == [WireExtraction, WireSummary]
    live, final = llm.calls
    assert live["model"] == cfg.brain.model and final["model"] == cfg.brain.summary_model
    transcript_block = live["content"][0]
    assert transcript_block["cache_control"] == {"type": "ephemeral"}
    assert "[#0 00:00 Me] Sam, could you send the revised pricing deck to Acme by Friday?" in transcript_block["text"]
    assert "[#2 00:08 Me] Great, that's everything for today." in transcript_block["text"]
    assert "User (speaker 'Me'): Alex" in live["content"][-1]["text"]
    assert "Existing items:\nnone" in live["content"][-1]["text"]
    assert all(f"[#{i} " in final["content"][0]["text"] for i in range(3))
    assert "A1 [action, owner=Sam, due=2026-10-16] Send revised pricing deck to Acme" in final["content"][1]["text"]

    # the session folder, renamed after the title the summary found
    (folder,) = _session_dirs(cfg)
    assert folder.name.endswith("-acme-pricing-sync")
    md = (folder / "transcript.md").read_text(encoding="utf-8")
    assert "**[00:00] Me:** Sam, could you send the revised pricing deck to Acme by Friday?" in md
    assert "**[00:04] Me:** Sure. I'll also draft the launch email." in md
    jsonl = (folder / "transcript.jsonl").read_text(encoding="utf-8").splitlines()
    assert [TranscriptSegment.model_validate_json(x).text for x in jsonl] == lines
    meeting, _ = load_meeting(folder)
    assert meeting.title == "Acme pricing sync" and meeting.participants == ["Sam"] and meeting.ended_at is not None
    assert [s.text for s in meeting.segments] == lines
    a1, a2, a3 = meeting.items["A1"], meeting.items["A2"], meeting.items["A3"]
    assert (a1.owner, a1.due_date, a1.segment_ids) == ("Sam", "2026-10-16", [meeting.segments[0].id])
    assert a2.due_date == "2026-10-20" and a2.revision == 2  # corrected by the summary pass
    assert set(a1.pushed) == set(a2.pushed) == {"markdown"} and a3.pushed == {}  # 0.3 confidence: not pushed

    notes = (folder / "notes.md").read_text(encoding="utf-8")
    assert notes.startswith("# Acme pricing sync\n")
    assert "> Sam sends Acme the revised pricing; Alex drafts the launch email." in notes
    mine = notes.index("**Draft the launch email** — Alex · due 2026-10-20")
    theirs = notes.index("**Send revised pricing deck to Acme** — Sam · due 2026-10-16")
    assert mine < theirs  # your own actions first
    assert "## Summary\n\n- Acme asked twice for revised pricing." in notes
    assert meeting.notes_ref["markdown"].endswith("-acme-pricing-sync.md")
    assert Path(meeting.notes_ref["markdown"].removeprefix("file://")).read_text(encoding="utf-8") == notes

    todo = h.todo()
    assert f"<!-- vs:{meeting.id}/A1 -->" in todo and f"<!-- vs:{meeting.id}/A2 -->" in todo
    assert "Maybe redo the logo" not in todo and "_(Acme pricing sync)_" in todo

    assert h.types() == ["assistant.started", "meeting.started", "segment", "segment", "segment",
                         "meeting.ending", "meeting.ended", "assistant.stopped"]
    ended = h.events("meeting.ended")[0].data
    assert ended["dir"] == str(folder) and ended["notes"] == str(folder / "notes.md") and ended["items"] == 3
    started = h.events("assistant.started")[0].data
    assert started["extraction"] == "claude" and started["backend"] == cfg.transcription.backend

    cost = h.events("assistant.stopped")[0].data["cost"]["session"]
    prices = [cfg.pricing.llm_per_mtok[m] for m in (cfg.brain.model, cfg.brain.summary_model)]
    assert cost["llm_usd"] == pytest.approx(sum(1000 * p.input + 100 * p.output for p in prices) / 1e6)  # ScriptedLLM usage
    assert set(cost["tokens"]) == {cfg.brain.model, cfg.brain.summary_model}
    assert cost["stt_minutes"] == pytest.approx(sum(u.duration for u in h.transcribers["mic"].calls) / 60)
    assert cost["stt_minutes"] == pytest.approx(9.0 / 60, abs=0.01)  # three 3-second utterances, no silence


# ------------------------------------------------------------------ offline mode


async def test_heuristic_mode_without_claude(cfg: Config, tmp_path: Path) -> None:
    lines = ["I'll send the revised deck to Sam by Friday.", "We decided to go with annual billing for enterprise."]
    clock = Clock()
    h = Harness(cfg, _wav_sources(tmp_path, clock, mic=bursts(2)), {"mic": lines}, clock=clock)
    await asyncio.wait_for(h.a.run(), 30)
    (folder,) = _session_dirs(cfg)
    assert "_" in folder.name and "-" not in folder.name.split("_")[-1]  # no title, so no slug
    notes = (folder / "notes.md").read_text(encoding="utf-8")
    assert "## Action items" in notes and "**Send the revised deck to Sam** — Alex · due " in notes
    assert "## Decisions\n\n- Go with annual billing for enterprise" in notes
    todo = h.todo()
    assert "Send the revised deck to Sam @Alex" in todo and "annual billing" not in todo  # decisions stay in the notes
    assert h.events("assistant.started")[0].data["extraction"] == "heuristic"
    assert {e.data["mode"] for e in h.events("extraction")} == {"heuristic"}


async def test_mic_echo_of_system_audio_is_dropped(cfg: Config, tmp_path: Path) -> None:
    clock = Clock()
    sources = _wav_sources(tmp_path, clock, mic=bursts(2), system=bursts(2))
    scripts = {
        "system": ["Can you send the revised pricing deck to Acme by Friday?", "Great, thanks everyone."],
        "mic": ["can you send the revised price deck to acme by friday", "I'll book the venue for the offsite."],
    }
    h = Harness(cfg, sources, scripts, clock=clock)
    await asyncio.wait_for(h.a.run(), 30)
    (session,) = h.a.finished_meetings
    got = sorted((s.source, s.speaker, s.text) for s in session.meeting.segments)
    assert got == [
        ("mic", "Me", "I'll book the venue for the offsite."),
        ("system", "Others", "Can you send the revised pricing deck to Acme by Friday?"),
        ("system", "Others", "Great, thanks everyone."),
    ]
    assert h.a.echo.dropped == 1
    owners = {i.title: i.owner for i in session.meeting.items.values()}
    assert owners == {"Send the revised pricing deck to Acme": "Alex", "Book the venue for the offsite": "Alex"}


async def test_a_meeting_without_speech_leaves_nothing_behind(cfg: Config, tmp_path: Path) -> None:
    clock = Clock()
    h = Harness(cfg, _wav_sources(tmp_path, clock, mic=noise(4.0)), clock=clock)
    await asyncio.wait_for(h.a.run(), 30)
    assert "meeting.started" in h.types()  # not always-on: a meeting was opened up front…
    assert _session_dirs(cfg) == []  # …and removed again
    assert "meeting.ended" not in h.types() and h.a.finished_meetings == []
    assert h.types()[-1] == "assistant.stopped"


# -------------------------------------------------------------------- always-on


async def test_always_on_splits_meetings_after_silence(cfg: Config) -> None:
    cfg.always_on.idle_split_minutes = 0.02  # 1.2 s
    h = Harness(cfg, [IdleSource("mic")])
    run = asyncio.ensure_future(h.a.run())
    try:
        await h.wait_for("assistant.started")
        assert h.a.always_on and h.a.current is None and _session_dirs(cfg) == []  # nothing until someone speaks
        await h.say("I'll send the deck to Sam by Friday.")
        first = h.a.current
        assert first is not None
        await h.wait_for("meeting.ended")
        assert h.a.current is None
        await h.say("I'll book the venue for the offsite by Monday.")
        second = h.a.current
        assert second is not None and second is not first and second.meeting.id != first.meeting.id
        assert second.meeting.segments[0].t_start == pytest.approx(0.5, abs=0.05)  # each meeting has its own clock
        h.a.stop()
        await asyncio.wait_for(run, 30)
    finally:
        run.cancel()
    assert [e.data["reason"] for e in h.events("meeting.ending")] == ["idle", "stopped"]
    assert h.a.finished_meetings == [first, second]
    assert len(_session_dirs(cfg)) == 2
    assert "Send the deck to Sam" in (first.store.dir / "notes.md").read_text(encoding="utf-8")
    second_notes = (second.store.dir / "notes.md").read_text(encoding="utf-8")
    assert "Book the venue for the offsite" in second_notes and "Send the deck to Sam" not in second_notes
    todo = h.todo()
    assert "Send the deck to Sam" in todo and "Book the venue for the offsite" in todo


@pytest.mark.xfail(strict=True, reason="BUG: Meeting.id has 1-second resolution, so back-to-back meetings share TODO.md refs")
async def test_back_to_back_meetings_keep_separate_todo_lines(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    second, ticks = datetime(2026, 10, 14, 15, 30, 0, tzinfo=UTC), itertools.count()
    # both meetings start within the same second (each call is a microsecond later)
    monkeypatch.setattr(models, "utcnow", lambda: second + timedelta(microseconds=next(ticks)))
    cfg.always_on.idle_split_minutes = 10
    h = Harness(cfg, [IdleSource("mic")])
    run = asyncio.ensure_future(h.a.run())
    try:
        await h.wait_for("assistant.started")
        await h.say("I'll send the deck to Sam by Friday.")
        await h.a.end_meeting()
        await h.say("I'll book the venue for the offsite by Monday.")
        h.a.stop()
        await asyncio.wait_for(run, 30)
    finally:
        run.cancel()
    todo = h.todo()
    assert "Send the deck to Sam" in todo and "Book the venue for the offsite" in todo


# ------------------------------------------------------------------ control API


async def test_control_api_and_agent_results_in_notes(cfg: Config) -> None:
    cfg.agents.mode = "suggest"
    engine = FakeEngine()
    h = Harness(cfg, [IdleSource("mic")], agent_engine=engine, always_on=False)
    run = asyncio.ensure_future(h.a.run())
    try:
        await h.wait_for("meeting.started")
        await h.say("Hey Claude, compare Globex and Initech pricing for the Acme email.")
        await h.say("Sam will book the venue by Friday.")
        await h.a.extract_now()
        state = h.a.state()
        assert state["agents_mode"] == "suggest" and state["sinks"] == ["markdown"]
        assert {i["id"]: i["title"] for i in state["meeting"]["items"].values()} == {
            "A1": "Compare Globex and Initech pricing for the Acme email", "A2": "Book the venue",
        }
        session = h.a.current
        assert session is not None
        items = session.meeting.items
        assert items["A1"].agent is not None and items["A1"].agent.status == AgentStatus.SUGGESTED  # waits for you

        assert h.a.approve_agent("A1")
        await h.wait_for("agent.updated", lambda e: e.data["item"]["agent"]["status"] == "done")
        assert engine.prompts[0].startswith("compare Globex and Initech pricing for the Acme email.")
        assert not h.a.reject_agent("A1")  # already done

        assert h.a.edit_item("A2", owner="Sarah")["owner"] == "Sarah"
        assert h.a.dismiss_item("A2") and items["A2"].dismissed
        added = h.a.add_item("action", "Send the recap email", owner="me", due_text="tomorrow")
        assert (added["id"], added["owner"], added["due_text"]) == ("A3", "Alex", "tomorrow") and added["due_date"]
        ref = await h.a.push_item("A3")
        assert ref == {"markdown": f"file://{h.sink.todo}#{session.meeting.id}/A3"}
        with pytest.raises(KeyError):
            h.a.dismiss_item("A9")

        path = await h.a.end_meeting()
        assert path is not None and h.a.current is None
        with pytest.raises(LookupError):
            h.a.add_item("action", "Too late")
        assert h.a.state()["last_meeting"]["dir"] == str(session.store.dir)
        h.a.stop()
        await asyncio.wait_for(run, 30)
    finally:
        run.cancel()

    notes = path.read_text(encoding="utf-8")
    section = notes[notes.index("## Work done by agents during the meeting"):]
    assert "### A1: Compare Globex and Initech pricing for the Acme email" in section
    assert "Globex is about 20% cheaper than Initech." in section
    assert f"- `{session.store.dir / 'agents'}" in section and "pricing.md`" in section
    assert "Book the venue" not in notes  # dismissed
    todo = h.todo()
    a1_line = next(ln for ln in todo.splitlines() if ln.endswith("/A1 -->"))
    assert "🤖 done" in a1_line and "Send the recap email" in todo and "Book the venue" not in todo
    assert h.costs.session.agents_usd == pytest.approx(0.25)


async def test_agent_still_running_at_the_end_updates_the_notes_later(cfg: Config) -> None:
    cfg.agents.mode = "commands"  # your own "Hey Claude" request runs without a click
    cfg.agents.finish_wait_s = 0
    gate = asyncio.Event()
    h = Harness(cfg, [IdleSource("mic")], agent_engine=FakeEngine(gate), always_on=False)
    run = asyncio.ensure_future(h.a.run())
    try:
        await h.wait_for("meeting.started")
        await h.say("Hey Claude, compare Globex and Initech pricing for the Acme email.")
        await h.a.extract_now()
        await h.wait_for("agent.updated", lambda e: e.data["item"]["agent"]["status"] == "running")
        path = await h.a.end_meeting()
        assert path is not None and "Work done by agents" not in path.read_text(encoding="utf-8")
        gate.set()
        await h.wait_for("meeting.notes_updated")
        h.a.stop()
        await asyncio.wait_for(run, 30)
    finally:
        run.cancel()
    assert "Globex is about 20% cheaper than Initech." in path.read_text(encoding="utf-8")


@pytest.mark.xfail(strict=True, reason="BUG: the session folder is renamed (title slug) while agents still work in agents/ under the old path")
async def test_late_agent_results_land_in_the_renamed_session_folder(cfg: Config) -> None:
    cfg.agents.mode = "commands"
    cfg.agents.finish_wait_s = 0
    gate = asyncio.Event()
    h = Harness(cfg, [IdleSource("mic")], agent_engine=FakeEngine(gate), always_on=False, title="Pricing sync")
    run = asyncio.ensure_future(h.a.run())
    try:
        await h.wait_for("meeting.started")
        await h.say("Hey Claude, compare Globex and Initech pricing for the Acme email.")
        await h.a.extract_now()
        await h.wait_for("agent.updated", lambda e: e.data["item"]["agent"]["status"] == "running")
        await h.a.end_meeting()
        gate.set()
        await h.wait_for("meeting.notes_updated")
        h.a.stop()
        await asyncio.wait_for(run, 30)
    finally:
        run.cancel()
    (session,) = h.a.finished_meetings
    assert session.store.dir.name.endswith("-pricing-sync")
    agent = session.meeting.items["A1"].agent
    assert agent is not None and agent.status == AgentStatus.DONE
    assert agent.result_files and all(Path(f).is_file() and session.store.dir in Path(f).parents for f in agent.result_files)
    assert "Globex is about 20% cheaper" in (session.store.dir / "notes.md").read_text(encoding="utf-8")
