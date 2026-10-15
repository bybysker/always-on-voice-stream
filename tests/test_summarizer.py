"""End-of-meeting pass: consolidation of the live items, the notes text and late re-rendering."""

from __future__ import annotations

import pytest

from helpers import WEDNESDAY, seg
from voicestream.brain import summarizer as summarizer_mod
from voicestream.brain.llm import LLMError, ScriptedLLM
from voicestream.brain.prompts import SUMMARY_SYSTEM
from voicestream.brain.schema import WireNewItem, WireSummary, WireUpdate
from voicestream.brain.summarizer import full_transcript, rerender, summarize
from voicestream.brain.tracker import ExtractedItem, ExtractionResult, ItemTracker
from voicestream.config import Config
from voicestream.models import AgentStatus, ItemStatus, Meeting


def _meeting() -> tuple[Meeting, ItemTracker]:
    m = Meeting(title="live title")
    tracker = ItemTracker(m, None, "Alex", today=WEDNESDAY)
    m.segments += [
        seg("Sam will update the roadmap.", "system", t=0.0),
        seg("I'll send the deck by Friday.", "mic", t=65.0),
        seg("Sarah, please book the offsite venue.", "mic", t=70.0),
    ]
    tracker.apply(ExtractionResult(new_items=[
        ExtractedItem(kind="action", title="Update the roadmap", owner="Sam"),
        ExtractedItem(kind="action", title="Send the deck", owner="me", due_text="by Friday", delegate=True),
        ExtractedItem(kind="decision", title="Use annual billing"),
        ExtractedItem(kind="question", title="Who owns the migration"),
    ]))
    return m, tracker


def _update(item_id: str, **kw: object) -> WireUpdate:
    base: dict[str, object] = dict(title="", detail="", owner="", due_text="", due_date="", status="unchanged",
                                   dismiss=False, delegate="unchanged", agent_prompt="", segment_refs=[])
    return WireUpdate(id=item_id, **{**base, **kw})


def _missing(title: str, **kw: object) -> WireNewItem:
    base: dict[str, object] = dict(kind="action", detail="", owner="Sarah", due_text="", due_date="", priority="none",
                                   confidence=0.8, evidence="book the offsite venue", segment_refs=[2], delegate=False,
                                   agent_prompt="", explicit_request=False)
    return WireNewItem(title=title, **{**base, **kw})


def _summary(**kw: object) -> WireSummary:
    base: dict[str, object] = dict(title="Q4 planning", tldr="We planned Q4.", notes_markdown="## Summary\n- Planned Q4",
                                   updates=[], missing_items=[], participants=[], language="en",
                                   followup_subject="", followup_body="")
    return WireSummary(**{**base, **kw})


async def test_summary_applies_corrections_and_writes_notes(cfg: Config) -> None:
    m, tracker = _meeting()
    llm = ScriptedLLM([_summary(
        updates=[_update("A1", due_text="next week", segment_refs=[0])],
        missing_items=[_missing("Book the offsite venue")],
        participants=["Sam", "Sarah", "Alex"],
    )])
    notes = await summarize(cfg, m, tracker, llm, today=WEDNESDAY)
    assert notes == m.summary_markdown
    assert (m.title, m.tldr, m.notes_body) == ("Q4 planning", "We planned Q4.", "## Summary\n- Planned Q4")
    assert m.items["A1"].due_date == "2026-10-19" and m.items["A1"].segment_ids == [m.segments[0].id]
    assert (m.items["A3"].title, m.items["A3"].owner, m.items["A3"].segment_ids) == ("Book the offsite venue", "Sarah", [m.segments[2].id])
    assert m.participants == ["Sam", "Sarah"]  # the user isn't listed as a participant

    lines = notes.splitlines()
    assert lines[0] == "# Q4 planning"
    assert "01:13 recorded · with Sam, Sarah" in lines[2]
    assert "> We planned Q4." in lines
    actions = [line for line in lines if line.startswith("- [")]
    # the user's own items come first
    assert actions == ["- [ ] **Send the deck** — Alex · due 2026-10-16", "- [ ] **Update the roadmap** — Sam · due 2026-10-19",
                       "- [ ] **Book the offsite venue** — Sarah"]
    assert "## Summary" in lines and "## Decisions" not in lines  # the model's notes replace the item sections


async def test_summary_request_layout(cfg: Config) -> None:
    m, tracker = _meeting()
    llm = ScriptedLLM([_summary()])
    await summarize(cfg, m, tracker, llm, today=WEDNESDAY)
    (call,) = llm.calls
    assert (call["model"], call["system"], call["schema"]) == (cfg.brain.summary_model, SUMMARY_SYSTEM, WireSummary)
    transcript, context = call["content"]
    assert transcript["cache_control"] == {"type": "ephemeral"}
    assert transcript["text"] == f"<transcript>\n{full_transcript(m)}\n</transcript>"
    assert "[#1 01:05 Me] I'll send the deck by Friday." in transcript["text"]
    assert "cache_control" not in context
    assert "Today: 2026-10-14 (Wednesday)" in context["text"]
    assert "A2 [action, owner=Alex, due=2026-10-16, agent=suggested] Send the deck" in context["text"]


async def test_items_found_afterwards_never_count_as_live_commands(cfg: Config) -> None:
    m, tracker = _meeting()
    llm = ScriptedLLM([_summary(missing_items=[_missing("Draft the offsite agenda", owner="Assistant", delegate=True,
                                                        agent_prompt="Draft it", explicit_request=True)])])
    await summarize(cfg, m, tracker, llm, today=WEDNESDAY)
    agent = m.items["A3"].agent
    assert agent is not None and agent.explicit is False


async def test_empty_summary_parts_fall_back_to_item_sections(cfg: Config) -> None:
    m, tracker = _meeting()
    notes = await summarize(cfg, m, tracker, ScriptedLLM([_summary(title="  ", tldr="", notes_markdown="")]), today=WEDNESDAY)
    assert m.title == "live title" and m.tldr is None and m.notes_body is None
    assert "## Decisions\n\n- Use annual billing" in notes and "## Open questions\n\n- Who owns the migration" in notes
    assert "> " not in notes


async def test_without_llm_notes_are_built_from_items(cfg: Config) -> None:
    m, tracker = _meeting()
    m.title = None
    notes = await summarize(cfg, m, tracker, None, today=WEDNESDAY)
    assert notes.startswith("# Meeting ")
    assert "## Action items" in notes and "## Decisions" in notes and "## Open questions" in notes


@pytest.mark.parametrize(
    "case", ["llm_error", "over_budget", "no_segments"],
)
async def test_summary_falls_back_without_touching_items(cfg: Config, case: str) -> None:
    m, tracker = _meeting()
    if case == "no_segments":
        m.segments.clear()
    llm = ScriptedLLM([LLMError("overloaded")] if case == "llm_error" else [_summary()])
    before = {k: i.model_dump() for k, i in m.items.items()}
    notes = await summarize(cfg, m, tracker, llm, today=WEDNESDAY, budget_ok=case != "over_budget")
    assert len(llm.calls) == (1 if case == "llm_error" else 0)
    assert {k: i.model_dump() for k, i in m.items.items()} == before
    assert m.title == "live title" and "## Decisions" in notes


async def test_notes_skip_cancelled_and_dismissed_and_tick_done(cfg: Config) -> None:
    m, tracker = _meeting()
    tracker.apply(ExtractionResult(new_items=[
        ExtractedItem(kind="action", title="Order the cake"), ExtractedItem(kind="action", title="Email the vendor list"),
    ]))
    tracker.edit("A1", status=ItemStatus.DONE)
    tracker.edit("A3", status=ItemStatus.CANCELLED)
    tracker.set_dismissed("A4")
    notes = await summarize(cfg, m, tracker, None, today=WEDNESDAY)
    assert "- [x] **Update the roadmap** — Sam" in notes
    assert "Order the cake" not in notes and "Email the vendor list" not in notes


async def test_rerender_adds_late_agent_results(cfg: Config) -> None:
    m, tracker = _meeting()
    await summarize(cfg, m, tracker, ScriptedLLM([_summary()]), today=WEDNESDAY)
    assert "Work done by agents" not in (m.summary_markdown or "")
    agent = m.items["A2"].agent
    assert agent is not None
    agent.status = AgentStatus.DONE
    agent.result_summary = "Drafted the deck outline."
    agent.result_files = ["deck.md", "notes/sources.md"]
    notes = rerender(m, "Alex")
    assert notes == m.summary_markdown
    assert "> We planned Q4." in notes and "## Summary" in notes  # stored summary parts are reused
    assert "- [ ] **Send the deck** — Alex · due 2026-10-16 · 🤖 agent done" in notes
    section = notes[notes.index("## Work done by agents during the meeting"):]
    assert section.splitlines()[2:] == [
        "### A2: Send the deck", "", "Drafted the deck outline.", "", "- `deck.md`", "- `notes/sources.md`"
    ]


def test_full_transcript_keeps_head_and_tail_of_very_long_meetings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(summarizer_mod, "MAX_SUMMARY_CHARS", 400)
    m = Meeting()
    m.segments += [seg(f"line number {i} of the long meeting", t=float(i)) for i in range(100)]
    text = full_transcript(m)
    assert text.startswith("[#0 00:00 Me] line number 0")
    assert text.endswith("line number 99 of the long meeting")
    assert "[… middle of a very long meeting omitted …]" in text and "line number 50 " not in text


async def test_summary_sets_language_and_followup_email(cfg, tmp_path) -> None:
    from voicestream.brain.llm import ScriptedLLM
    from voicestream.brain.summarizer import summarize
    from voicestream.brain.tracker import ItemTracker
    from voicestream.models import Meeting, TranscriptSegment

    cfg.brain.provider = "anthropic"
    m = Meeting(segments=[TranscriptSegment(source="system", speaker="Others", text="On valide le devis.", t_start=0, t_end=2)])
    wire = _summary(title="Point devis", language="fr", followup_subject="Récap : point devis",
                    followup_body="Bonjour,\n\n- Devis validé\n\nAlex")
    notes = await summarize(cfg, m, ItemTracker(m, None, "Alex"), ScriptedLLM([wire]))
    assert m.language == "fr" and "Réunion" not in notes and notes.startswith("# Point devis")
    assert m.followup_email is not None and m.followup_email.subject == "Récap : point devis"
    assert "enregistrées" in notes  # French labels for a French meeting


def test_french_notes_get_french_section_headings() -> None:
    m = Meeting(title="Point devis", language="fr")
    body = "## Summary\n- Devis validé\n\n### Key points\n- Envoi vendredi\n\n## Summary of costs\n- 3 k€"
    notes = summarizer_mod.render_notes(m, "", body, "Alex")
    assert "## Résumé\n" in notes and "### Points clés\n" in notes
    assert "## Summary of costs" in notes  # only the standard headings are translated
    assert summarizer_mod.render_notes(Meeting(language="en"), "", body, "Alex").count("## Summary") == 2


def test_summary_prompt_asks_for_headings_in_the_notes_language() -> None:
    assert "Points clés" in SUMMARY_SYSTEM
