"""Live extraction loop: when it calls Claude (cost), what it sends (prompt-cache layout) and how it
degrades (retries, heuristic fallback). Time is a fake clock so scheduling is deterministic."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import pytest

from helpers import WEDNESDAY, seg
from voicestream.brain import extractor as extractor_mod
from voicestream.brain.extractor import Extractor
from voicestream.brain.llm import LLMError, LLMUnavailable, ScriptedLLM
from voicestream.brain.prompts import EXTRACTION_SYSTEM
from voicestream.brain.schema import WireExtraction, WireNewItem, WireUpdate
from voicestream.brain.tracker import ItemTracker
from voicestream.config import Config
from voicestream.costs import CostMeter
from voicestream.events import EventBus
from voicestream.models import Meeting, ScreenNote, TranscriptSegment

NEUTRAL = (
    "The quarterly numbers look stable across all regions and churn stayed flat compared with the "
    "previous quarter, according to the finance review that was shared with the whole group."
)
COMMIT = "I'll send the deck to Acme by Friday."


def _empty() -> WireExtraction:
    return WireExtraction(new_items=[], updates=[], meeting_title="", participants=[])


def _wire_item(title: str, refs: list[int], owner: str = "Alex") -> WireNewItem:
    return WireNewItem(
        kind="action", title=title, detail="", owner=owner, due_text="by Friday", due_date="", priority="none",
        confidence=0.9, evidence=title, segment_refs=refs, delegate=False, agent_prompt="", explicit_request=False,
    )


def _wire_update(item_id: str, refs: list[int]) -> WireUpdate:
    return WireUpdate(
        id=item_id, title="", detail="", owner="Sam", due_text="", due_date="", status="unchanged", dismiss=False,
        delegate="unchanged", agent_prompt="", segment_refs=refs,
    )


class Rig:
    """An Extractor on a settable fake clock, fed by a ScriptedLLM (``llm=False``: heuristic only)."""

    def __init__(self, cfg: Config, responses: Sequence[Any] = (), *, llm: bool = True, costs: CostMeter | None = None) -> None:
        cfg.brain.provider = "anthropic" if llm else "heuristic"
        self.t = 0.0
        self.meeting = Meeting()
        self.bus = EventBus()
        self.tracker = ItemTracker(self.meeting, self.bus, cfg.user_name, today=WEDNESDAY)
        self.llm = ScriptedLLM(responses)
        self.ex = Extractor(
            cfg, self.meeting, self.tracker, self.llm, costs=costs, bus=self.bus, now=lambda: self.t, today=lambda: WEDNESDAY
        )

    def say(self, text: str, source: str = "mic", at: float | None = None) -> TranscriptSegment:
        if at is not None:
            self.t = at
        s = seg(text, source, t=self.t)
        self.meeting.segments.append(s)
        self.ex.on_segment(s)
        return s

    def due_at(self, t: float) -> bool:
        self.t = t
        return self.ex.due()

    async def extract_at(self, t: float, reason: str = "scheduled") -> tuple[list[Any], list[Any]]:
        self.t = t
        return await self.ex.extract_now(reason)

    def transcript_blocks(self, call: int) -> list[dict[str, Any]]:
        return self.llm.calls[call]["content"][:-1]

    def context_block(self, call: int) -> dict[str, Any]:
        return self.llm.calls[call]["content"][-1]


def test_neutral_text_has_no_trigger_phrases(cfg: Config) -> None:
    assert len(NEUTRAL) >= cfg.brain.min_new_chars
    assert not any(p in NEUTRAL.lower() for p in cfg.brain.trigger_phrases + cfg.brain.wake_phrases)


# ------------------------------------------------------------------- scheduling


def test_not_due_without_new_lines(cfg: Config) -> None:
    assert not Rig(cfg, llm=False).due_at(10_000)


def test_due_after_interval_once_enough_new_text(cfg: Config) -> None:
    rig = Rig(cfg, llm=False)
    rig.say(NEUTRAL, at=80.0)
    assert not rig.due_at(89.9)
    assert rig.due_at(cfg.brain.extract_interval_s)


def test_not_due_with_too_little_text_before_the_trickle_limit(cfg: Config) -> None:
    rig = Rig(cfg, llm=False)
    rig.say("Numbers look stable.", at=85.0)
    assert not rig.due_at(100.0)


def test_trigger_phrase_brings_extraction_forward(cfg: Config) -> None:
    rig = Rig(cfg, llm=False)
    rig.say("Could you send me the slides?", "system", at=50.0)
    assert not rig.due_at(50.0 + cfg.brain.trigger_delay_s - 0.1)
    assert rig.due_at(50.0 + cfg.brain.trigger_delay_s)


def test_wake_phrase_from_mic_is_the_fast_path(cfg: Config) -> None:
    rig = Rig(cfg, llm=False)
    rig.say("Hey Claude, draft a summary of the pricing discussion.", at=50.0)
    assert not rig.due_at(52.9)
    assert rig.due_at(53.0)


def test_wake_phrase_from_system_audio_gets_no_fast_path(cfg: Config) -> None:
    rig = Rig(cfg, llm=False)
    rig.say("Hey Claude, draft a summary of the pricing discussion.", "system", at=50.0)
    assert not rig.due_at(53.0)
    assert not rig.due_at(70.0)


def test_later_trigger_phrase_does_not_postpone_a_wake(cfg: Config) -> None:
    rig = Rig(cfg, llm=False)
    rig.say("Hey Claude, draft a summary of the pricing discussion.", at=50.0)
    rig.say("And can you add the Globex numbers?", "system", at=51.0)
    assert rig.due_at(53.0)


async def test_triggers_wait_at_least_8s_after_the_last_run(cfg: Config) -> None:
    rig = Rig(cfg, llm=False)
    rig.say(NEUTRAL, at=99.0)
    await rig.extract_at(100.0)
    rig.say("Hey Claude, draft a summary of the pricing discussion.", at=101.0)
    assert not rig.due_at(107.9)
    assert rig.due_at(108.0)


def test_silence_flushes_pending_lines(cfg: Config) -> None:
    rig = Rig(cfg, llm=False)
    rig.say("Numbers look stable.", at=10.0)
    assert not rig.due_at(39.9)
    assert rig.due_at(40.0)


async def test_silence_flush_needs_20s_since_the_last_run(cfg: Config) -> None:
    rig = Rig(cfg, llm=False)
    rig.say("Numbers look stable.", at=5.0)
    await rig.extract_at(40.0)
    rig.meeting.segments.append(seg("Churn is flat.", t=6.0))  # arrived without a fresh on_segment
    assert not rig.due_at(59.9)
    assert rig.due_at(60.0)


def test_trickle_of_short_lines_is_flushed_after_4_intervals(cfg: Config) -> None:
    rig = Rig(cfg, llm=False)
    limit = 4 * cfg.brain.extract_interval_s
    for t in range(0, int(limit), 10):
        rig.say("yes", at=float(t))
        assert not rig.due_at(t + 0.5)
    assert rig.ex.pending_chars() < cfg.brain.min_new_chars
    assert not rig.due_at(limit - 0.1)
    assert rig.due_at(limit)


# ------------------------------------------------------------------- extraction


async def test_extract_now_applies_heuristic_items_and_advances(cfg: Config) -> None:
    rig = Rig(cfg, llm=False)
    s = rig.say(COMMIT, at=1.0)
    added, updated = await rig.extract_at(2.0, "manual")
    assert [(i.id, i.owner, i.due_date) for i in added] == [("A1", "Alex", "2026-10-16")] and updated == []
    assert added[0].segment_ids == [s.id]
    assert rig.ex.processed_upto == 1 and rig.ex.pending_segments() == []
    assert await rig.extract_at(3.0) == ([], [])
    event = rig.bus.history[-1]
    assert event.type == "extraction"
    assert {k: event.data[k] for k in ("reason", "mode", "lines", "added")} == {
        "reason": "manual", "mode": "heuristic", "lines": 1, "added": ["A1"]
    }


async def test_llm_results_go_through_the_tracker(cfg: Config) -> None:
    rig = Rig(cfg, [
        WireExtraction(new_items=[_wire_item("Send the deck to Acme", [1])], updates=[], meeting_title="Acme sync", participants=["Sam"]),
        WireExtraction(new_items=[], updates=[_wire_update("A1", [0, 2])], meeting_title="", participants=[]),
    ])
    s0 = rig.say(NEUTRAL, "system", at=0.0)
    s1 = rig.say(COMMIT, at=5.0)
    (item,), _ = await rig.extract_at(10.0)
    assert (item.id, item.due_date, item.segment_ids) == ("A1", "2026-10-16", [s1.id])
    assert rig.meeting.title == "Acme sync" and rig.meeting.participants == ["Sam"]
    s2 = rig.say("Actually Sam is taking that one.", "system", at=20.0)
    _, (upd,) = await rig.extract_at(30.0)
    assert upd.owner == "Sam" and upd.segment_ids == [s1.id, s0.id, s2.id]  # updates may cite earlier lines
    assert rig.ex.processed_upto == 3 and rig.ex.mode == "llm"


async def test_prompt_cache_layout_is_append_only(cfg: Config) -> None:
    rig = Rig(cfg, [WireExtraction(new_items=[_wire_item("Send the deck to Acme", [1])], updates=[], meeting_title="", participants=[]),
                    _empty(), _empty()])
    rig.say(NEUTRAL, "system", at=0.0)
    rig.say(COMMIT, at=5.0)
    await rig.extract_at(10.0)
    rig.say("Numbers look stable.", "system", at=20.0)
    rig.say("Churn is flat too.", at=25.0)
    await rig.extract_at(30.0)
    rig.say("Let's review hiring next.", "system", at=40.0)
    await rig.extract_at(50.0)

    calls = [rig.transcript_blocks(i) for i in range(3)]
    assert [len(c) for c in calls] == [1, 2, 3]
    for prev, cur in zip(calls, calls[1:]):
        # earlier blocks are re-sent byte for byte (only the cache marker moves)
        assert [b["text"] for b in cur[: len(prev)]] == [b["text"] for b in prev]
    for blocks in calls:
        assert [("cache_control" in b) for b in blocks] == [False] * (len(blocks) - 1) + [True]
        assert blocks[-1]["cache_control"] == {"type": "ephemeral"}
    assert calls[0][0]["text"] == (
        "<transcript part='1'>\n[#0 00:00 Others] " + NEUTRAL + "\n[#1 00:05 Me] " + COMMIT + "\n</transcript>"
    )
    assert calls[2][2]["text"].startswith("<transcript part='3'>\n[#4 00:40 Others]")

    first_ctx, second_ctx = rig.context_block(0), rig.context_block(1)
    assert "cache_control" not in first_ctx and "cache_control" not in second_ctx
    assert "Existing items:\nnone" in first_ctx["text"] and "Today: 2026-10-14 (Wednesday)" in first_ctx["text"]
    assert "A1 [action, owner=Alex, due=2026-10-16] Send the deck to Acme" in second_ctx["text"]
    assert "lines #2–#3" in second_ctx["text"]
    assert all(c["system"] == EXTRACTION_SYSTEM and c["model"] == cfg.brain.model for c in rig.llm.calls)


async def test_screen_notes_are_interleaved_once(cfg: Config) -> None:
    rig = Rig(cfg, [_empty(), _empty()])
    rig.say("Here is the roadmap.", "system", at=0.0)
    rig.meeting.screen_notes.append(ScreenNote(t=4.0, description="Slide: Q4 roadmap"))
    rig.say("Looks good.", at=8.0)
    await rig.extract_at(10.0)
    rig.say("Next topic.", "system", at=20.0)
    await rig.extract_at(30.0)
    first, second = rig.transcript_blocks(0)[0]["text"], rig.transcript_blocks(1)[-1]["text"]
    assert first.index("Here is the roadmap") < first.index("<screen 00:04> Slide: Q4 roadmap </screen>") < first.index("Looks good")
    assert "<screen" not in second


async def test_llm_error_keeps_lines_pending_and_schedules_a_retry(cfg: Config) -> None:
    rig = Rig(cfg, [LLMError("overloaded"), LLMError("overloaded"), _empty()])
    rig.say("Numbers look stable.", at=0.0)
    assert await rig.extract_at(1.0) == ([], [])
    assert rig.ex.processed_upto == 0 and len(rig.ex.pending_segments()) == 1
    assert not rig.due_at(10.9)
    assert rig.due_at(11.0)  # retry after 10 s
    assert await rig.extract_at(11.0) == ([], [])
    # the retried block is identical, so the cache prefix stays valid
    assert rig.transcript_blocks(1) == rig.transcript_blocks(0)
    rig.say("Churn is flat.", at=25.0)  # fresh speech: the silence flush can't fire at 31 s
    assert not rig.due_at(30.9) and rig.due_at(31.0)  # second retry after 20 s
    await rig.extract_at(31.0)
    assert rig.ex.processed_upto == 2
    (block,) = rig.transcript_blocks(2)
    assert block["text"].startswith("<transcript part='1'>") and "Churn is flat." in block["text"]


async def test_three_failures_fall_back_to_the_heuristic(cfg: Config) -> None:
    rig = Rig(cfg, [LLMError("a"), LLMError("b"), LLMError("c"), LLMError("d")])
    rig.say(COMMIT, at=0.0)
    for t in (1.0, 12.0, 33.0):
        result = await rig.extract_at(t)
    assert [i.title for i in result[0]] == ["Send the deck to Acme"]
    assert rig.ex.processed_upto == 1 and rig.ex.llm is not None  # still tries Claude next time
    rig.say("Numbers look stable.", at=40.0)
    assert await rig.extract_at(50.0) == ([], [])  # failure count was reset: back to retrying
    assert rig.ex.processed_upto == 1


@pytest.mark.xfail(strict=True, reason="BUG: lines handled by the heuristic fallback are never added to the transcript blocks Claude sees")
async def test_lines_from_a_fallback_round_stay_in_claude_context(cfg: Config) -> None:
    rig = Rig(cfg, [LLMError("a"), LLMError("b"), LLMError("c"), _empty()])
    rig.say(COMMIT, at=0.0)
    for t in (1.0, 12.0, 33.0):
        await rig.extract_at(t)
    rig.say("Actually Sam is taking that one.", "system", at=40.0)
    await rig.extract_at(50.0)
    assert any(COMMIT in b["text"] for b in rig.transcript_blocks(3))


@pytest.mark.xfail(strict=True, reason="BUG: the extraction event reports mode='llm' for a round the heuristic handled after 3 failures")
async def test_fallback_round_is_reported_as_heuristic(cfg: Config) -> None:
    rig = Rig(cfg, [LLMError("a"), LLMError("b"), LLMError("c")])
    rig.say(COMMIT, at=0.0)
    for t in (1.0, 12.0, 33.0):
        await rig.extract_at(t)
    assert rig.bus.history[-1].data["mode"].startswith("heuristic")


async def test_unavailable_llm_switches_to_heuristic_for_good(cfg: Config) -> None:
    rig = Rig(cfg, [LLMUnavailable("ANTHROPIC_API_KEY is not set")])
    rig.say(COMMIT, at=0.0)
    added, _ = await rig.extract_at(1.0)
    assert [i.title for i in added] == ["Send the deck to Acme"]
    assert rig.ex.llm is None and rig.ex.mode == "heuristic"
    rig.say("Sarah will book the venue before Monday.", "system", at=10.0)
    added, _ = await rig.extract_at(20.0)
    assert [i.owner for i in added] == ["Sarah"]
    assert len(rig.llm.calls) == 1


async def test_over_budget_uses_heuristic_until_budget_returns(cfg: Config) -> None:
    cfg.budget.daily_usd = 0.0
    rig = Rig(cfg, [_empty()], costs=CostMeter(cfg, persist=False))
    rig.say(COMMIT, at=0.0)
    added, _ = await rig.extract_at(1.0)
    assert [i.id for i in added] == ["A1"] and rig.llm.calls == []
    assert rig.ex.mode == "heuristic-budget"
    cfg.budget.daily_usd = 100.0
    rig.say("Numbers look stable.", at=10.0)
    await rig.extract_at(20.0)
    assert len(rig.llm.calls) == 1 and rig.ex.mode == "llm"


async def test_final_flush(cfg: Config) -> None:
    rig = Rig(cfg, [LLMError("overloaded")])
    assert await rig.ex.flush() == ([], [])
    rig.say(COMMIT, at=0.0)
    added, _ = await rig.ex.flush()  # no retry at the end of a meeting: heuristic right away
    assert [i.title for i in added] == ["Send the deck to Acme"]
    assert rig.ex.pending_segments() == [] and rig.bus.history[-1].data["reason"] == "final"


async def test_context_is_capped_by_dropping_the_oldest_blocks(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extractor_mod, "MAX_CONTEXT_CHARS", 2 * len(NEUTRAL) + 150)
    rig = Rig(cfg, [_empty(), _empty(), _empty(), _empty()])
    for i in range(3):
        rig.say(NEUTRAL, at=10.0 * i)
        await rig.extract_at(10.0 * i + 5)
    blocks = [b["text"] for b in rig.transcript_blocks(2)]
    assert len(blocks) == 2 and blocks[0].startswith("<transcript part='2'>")
    assert sum(map(len, blocks)) <= extractor_mod.MAX_CONTEXT_CHARS
    assert rig.transcript_blocks(2)[-1]["cache_control"] == {"type": "ephemeral"}
    monkeypatch.setattr(extractor_mod, "MAX_CONTEXT_CHARS", 10)
    rig.say(NEUTRAL, at=40.0)
    await rig.extract_at(45.0)
    (only,) = rig.transcript_blocks(3)  # an oversized new block is still sent on its own
    assert only["text"].startswith("<transcript part='4'>")


async def test_run_loop_extracts_when_due_and_stops(cfg: Config) -> None:
    rig = Rig(cfg, llm=False)
    task = asyncio.ensure_future(rig.ex.run())
    rig.say("Hey Claude, draft a summary of the pricing discussion.", at=100.0)
    await asyncio.sleep(0.01)
    assert rig.ex.processed_upto == 0  # not due yet
    rig.t = 103.0
    rig.ex.on_screen(ScreenNote(t=103.0, description="x"))  # any input wakes the loop
    for _ in range(100):
        await asyncio.sleep(0.01)
        if rig.ex.processed_upto:
            break
    assert rig.ex.processed_upto == 1
    assert [i.owner for i in rig.meeting.items.values()] == ["Assistant"]
    rig.ex.stop()
    await asyncio.wait_for(task, 1.0)
