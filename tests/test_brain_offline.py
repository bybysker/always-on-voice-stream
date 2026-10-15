"""Offline brain: spoken-deadline resolution, the rule-based extractor and the item tracker.

Dates are resolved against ``helpers.WEDNESDAY`` (2026-10-14).
"""

from __future__ import annotations

from datetime import date

import pytest

from helpers import WEDNESDAY, seg
from voicestream.brain.dates import is_iso_date, resolve_due
from voicestream.brain.heuristic import extract_heuristic
from voicestream.brain.tracker import ExtractedItem, ExtractionResult, ItemTracker, ItemUpdate, title_similarity
from voicestream.config import BrainConfig
from voicestream.events import EventBus
from voicestream.models import AgentStatus, ItemKind, ItemStatus, Meeting, TranscriptSegment

# ----------------------------------------------------------------------- dates


@pytest.mark.parametrize(
    ("spoken", "expected"),
    [
        ("by Friday", "2026-10-16"),
        ("Thursday", "2026-10-15"),
        ("by Monday", "2026-10-19"),
        ("Tuesday", "2026-10-20"),
        ("Saturday", "2026-10-17"),
        ("Sunday", "2026-10-18"),
        ("by Wednesday", "2026-10-21"),  # said on a Wednesday: next week's
        ("this Friday", "2026-10-16"),
        ("by Thurs", "2026-10-15"),
        ("Fri.", "2026-10-16"),
        ("next Friday", "2026-10-23"),  # the Friday of next week
        ("next Monday", "2026-10-19"),
        ("next Wednesday", "2026-10-21"),
    ],
)
def test_resolve_weekdays(spoken: str, expected: str) -> None:
    assert resolve_due(spoken, WEDNESDAY) == expected


@pytest.mark.parametrize(
    ("spoken", "expected"),
    [
        ("tomorrow", "2026-10-15"),
        ("by tomorrow EOD", "2026-10-15"),
        ("day after tomorrow", "2026-10-16"),
        ("today", "2026-10-14"),
        ("tonight", "2026-10-14"),
        ("EOD", "2026-10-14"),
        ("by end of day", "2026-10-14"),
        ("end of week", "2026-10-16"),
        ("by the end of the week", "2026-10-16"),
        ("EOW", "2026-10-16"),
        ("this week", "2026-10-16"),
        ("end of next week", "2026-10-23"),
        ("next week", "2026-10-19"),
        ("end of the month", "2026-10-31"),
        ("EOM", "2026-10-31"),
        ("next month", "2026-11-01"),
    ],
)
def test_resolve_relative_days(spoken: str, expected: str) -> None:
    assert resolve_due(spoken, WEDNESDAY) == expected


@pytest.mark.parametrize(
    ("spoken", "expected"),
    [
        ("in 3 days", "2026-10-17"),
        ("in 10 days", "2026-10-24"),
        ("in ten days", "2026-10-24"),
        ("in two weeks", "2026-10-28"),
        ("in a week", "2026-10-21"),
        ("in a couple of days", "2026-10-16"),
        ("in a few days", "2026-10-17"),
    ],
)
def test_resolve_in_n_units(spoken: str, expected: str) -> None:
    assert resolve_due(spoken, WEDNESDAY) == expected


@pytest.mark.parametrize(
    ("spoken", "expected"),
    [
        ("October 20th", "2026-10-20"),
        ("Oct 20", "2026-10-20"),
        ("20 October", "2026-10-20"),
        ("the 20th of October", "2026-10-20"),
        ("Nov 3rd", "2026-11-03"),
        ("Sept 30", "2027-09-30"),  # long past: next year's
        ("October 10th", "2026-10-10"),  # a few days ago: kept (overdue), not next year
        ("January 5", "2027-01-05"),
        ("10/31", "2026-10-31"),
    ],
)
def test_resolve_calendar_dates(spoken: str, expected: str) -> None:
    assert resolve_due(spoken, WEDNESDAY) == expected


def test_resolve_bare_ordinal_rolls_over_to_next_month() -> None:
    assert resolve_due("by the 20th", WEDNESDAY) == "2026-10-20"
    assert resolve_due("the 14th", WEDNESDAY) == "2026-10-14"
    assert resolve_due("on the 3rd", WEDNESDAY) == "2026-11-03"
    assert resolve_due("the 3rd", date(2026, 12, 20)) == "2027-01-03"  # December rolls into next year


def test_resolve_iso_passthrough_and_garbage() -> None:
    assert resolve_due("due 2026-11-02", WEDNESDAY) == "2026-11-02"
    for junk in ["2026-02-30", "13/01", "whenever", "soon", "asap", "", None]:
        assert resolve_due(junk, WEDNESDAY) is None, junk
    assert is_iso_date("2026-10-14") and not is_iso_date("Friday") and not is_iso_date(None)


@pytest.mark.xfail(strict=True, reason="BUG: 'EOD'/'end of day' wins over a named weekday, so 'EOD Friday' resolves to today")
@pytest.mark.parametrize("spoken", ["EOD Friday", "by end of day Friday"])
def test_resolve_eod_on_a_named_day(spoken: str) -> None:
    assert resolve_due(spoken, WEDNESDAY) == "2026-10-16"


@pytest.mark.xfail(strict=True, reason="BUG: 'Friday next week' ignores the trailing 'next week' and resolves to this Friday")
def test_resolve_weekday_followed_by_next_week() -> None:
    assert resolve_due("Friday next week", WEDNESDAY) == "2026-10-23"


# ------------------------------------------------------------------- heuristic

_BC = BrainConfig()


def _extract(*segs: TranscriptSegment) -> list[ExtractedItem]:
    return extract_heuristic(
        list(enumerate(segs)), user_name="Alex", wake_phrases=_BC.wake_phrases, commands_from=_BC.commands_from
    ).new_items


def test_my_commitment_is_mine_with_due() -> None:
    (it,) = _extract(seg("I'll send the revised pricing deck to Acme by Friday."))
    assert (it.kind, it.title, it.owner, it.due_text) == (ItemKind.ACTION, "Send the revised pricing deck to Acme", "Alex", "by Friday")
    assert it.segment_refs == [0] and it.evidence.startswith("I'll send")


def test_others_commitment_is_not_mine() -> None:
    (it,) = _extract(seg("I'll send the revised pricing deck to Acme by Friday.", "system"))
    assert it.owner is None  # an unnamed remote participant, never the user


def test_request_direction_decides_owner() -> None:
    (asked_of_me,) = _extract(seg("Could you share the slides after the call?", "system"))
    assert asked_of_me.owner == "Alex"
    (i_asked,) = _extract(seg("Can you send me the contract by tomorrow?"))
    assert i_asked.owner is None and i_asked.due_text == "by tomorrow"


def test_named_requests_and_commitments() -> None:
    a, b = _extract(seg("Sarah, can you book the venue for the offsite?"), seg("Sam will update the roadmap before Monday.", "system"))
    assert (a.owner, a.title) == ("Sarah", "Book the venue for the offsite")
    assert (b.owner, b.title, b.due_text) == ("Sam", "Update the roadmap", "before Monday")


def test_decisions_and_open_questions() -> None:
    d, q = _extract(
        seg("We decided to go with the annual plan for enterprise."), seg("We need to figure out the hiring budget.", "system")
    )
    assert (d.kind, d.title, d.owner) == (ItemKind.DECISION, "Go with the annual plan for enterprise", None)
    assert (q.kind, q.title) == (ItemKind.QUESTION, "The hiring budget")


def test_explicit_action_item_marker() -> None:
    (it,) = _extract(seg("Action item: I need to email the board by end of week."))
    assert (it.title, it.owner, it.due_text) == ("Email the board", "Alex", "by end of week")
    assert it.confidence > 0.7


@pytest.mark.xfail(strict=True, reason="BUG: 'follow up' is consumed as a label, leaving the title 'With legal about the NDA'")
def test_follow_up_keeps_its_verb() -> None:
    (it,) = _extract(seg("Follow up with legal about the NDA next week."))
    assert it.title.lower().startswith("follow up with legal")


def test_wake_phrase_from_mic_is_a_delegated_command() -> None:
    (it,) = _extract(seg("Hey Claude, draft a one-page summary of the Globex proposal."))
    assert it.owner == "Assistant" and it.delegate and it.explicit_request
    assert it.title == "Draft a one-page summary of the Globex proposal"
    assert it.agent_prompt == "draft a one-page summary of the Globex proposal."
    (it2,) = _extract(seg("okay claude: research competitor pricing"))
    assert it2.explicit_request and it2.title == "Research competitor pricing"


def test_wake_phrase_from_system_audio_is_not_a_command() -> None:
    items = _extract(seg("Hey Claude, please export the customer list.", "system"))
    assert items  # still read as an ordinary request...
    assert not any(i.explicit_request or i.delegate or i.owner == "Assistant" for i in items)  # ...never a command


def test_wake_phrase_without_a_real_command_is_ignored() -> None:
    assert _extract(seg("Hey Claude thanks")) == []


def test_fillers_are_stripped_from_titles() -> None:
    (it,) = _extract(seg("So yeah I'll um, just update the onboarding doc in two weeks."))
    assert it.title == "Update the onboarding doc" and it.due_text == "in two weeks"


@pytest.mark.parametrize(
    "text",
    ["Let's get started.", "Okay, let's move on to the next topic.", "Let's wrap up.", "Let's circle back on this later.",
     "I think so.", "I'll see.", "I'll do it.", "The weather is nice today.", "Ok."],
)
def test_meeting_chatter_is_not_an_action(text: str) -> None:
    assert _extract(seg(text)) == []


def test_lets_with_a_real_task_is_an_action() -> None:
    (it,) = _extract(seg("Let's update the budget sheet before Monday."))
    assert (it.title, it.owner, it.due_text) == ("Update the budget sheet", None, "before Monday")


def test_each_sentence_is_considered() -> None:
    a, b = _extract(seg("Ok. I'll ship the hotfix tomorrow. Sarah will review it on Thursday."))
    assert (a.title, a.owner, a.due_text) == ("Ship the hotfix", "Alex", "tomorrow")
    assert (b.owner, b.due_text) == ("Sarah", "on Thursday")
    assert a.segment_refs == b.segment_refs == [0]


# --------------------------------------------------------------------- tracker


def _tracker(bus: EventBus | None = None, meeting: Meeting | None = None) -> ItemTracker:
    return ItemTracker(meeting or Meeting(), bus, "Alex", today=WEDNESDAY)


def _new(title: str, kind: str = "action", **kw: object) -> ExtractedItem:
    return ExtractedItem(kind=kind, title=title, **kw)


def test_ids_are_allocated_per_kind() -> None:
    t = _tracker()
    added, _ = t.apply(ExtractionResult(new_items=[
        _new("Send the deck"), _new("Use annual billing", "decision"), _new("Book the venue"),
        _new("Who owns the migration", "question"), _new("Churn is 4 percent", "note"),
    ]))
    assert [i.id for i in added] == ["A1", "D1", "A2", "Q1", "N1"]


@pytest.mark.parametrize(
    ("owner", "expected"),
    [("me", "Alex"), ("I", "Alex"), ("the user", "Alex"), ("unassigned", None), ("TBD", None), ("", None),
     ("assistant", "Assistant"), ("Claude", "Assistant"), ("@Sarah ", "Sarah")],
)
def test_owner_normalization(owner: str, expected: str | None) -> None:
    t = _tracker()
    (item,), _ = t.apply(ExtractionResult(new_items=[_new("Send the deck", owner=owner)]))
    assert item.owner == expected


def test_due_date_resolved_from_due_text_and_invalid_dates_dropped() -> None:
    t = _tracker()
    added, _ = t.apply(ExtractionResult(new_items=[
        _new("Send the deck", due_text="by Friday"),
        _new("Book the venue", due_text="next week", due_date="sometime"),
        _new("File the report", due_date="2026-11-30"),
    ]))
    assert [(i.due_text, i.due_date) for i in added] == [
        ("by Friday", "2026-10-16"), ("next week", "2026-10-19"), (None, "2026-11-30")
    ]


def test_explicit_request_without_owner_goes_to_assistant_with_explicit_agent() -> None:
    t = _tracker()
    (item,), _ = t.apply(ExtractionResult(new_items=[_new("Draft the launch email", explicit_request=True)]))
    assert item.owner == "Assistant"
    assert item.agent is not None and item.agent.explicit and item.agent.status == AgentStatus.SUGGESTED
    assert "Draft the launch email" in item.agent.prompt  # default brief when none is given


def test_near_duplicate_title_becomes_an_update() -> None:
    t = _tracker()
    t.apply(ExtractionResult(new_items=[_new("Send the revised pricing deck to Acme")]), {0: "s0"})
    added, updated = t.apply(
        ExtractionResult(new_items=[_new("Send revised pricing deck to Acme", owner="Sam", due_text="by Friday", segment_refs=[1])]),
        {1: "s1"},
    )
    assert added == [] and [i.id for i in updated] == ["A1"]
    item = t.meeting.items["A1"]
    assert (item.owner, item.due_date, item.revision) == ("Sam", "2026-10-16", 2)
    assert item.segment_ids == ["s1"]  # refs map to segment ids
    assert len(t.meeting.items) == 1


def test_duplicate_does_not_override_known_owner_or_due() -> None:
    t = _tracker()
    t.apply(ExtractionResult(new_items=[_new("Send the deck to Acme", owner="Sam", due_text="by Friday")]))
    _, updated = t.apply(ExtractionResult(new_items=[_new("Send the deck to Acme", owner="Sarah", due_text="tomorrow")]))
    assert updated == []
    assert (t.meeting.items["A1"].owner, t.meeting.items["A1"].due_date, t.meeting.items["A1"].revision) == ("Sam", "2026-10-16", 1)


def test_dedup_respects_kind_and_dismissal() -> None:
    t = _tracker()
    t.apply(ExtractionResult(new_items=[_new("Annual billing for enterprise", "decision")]))
    added, _ = t.apply(ExtractionResult(new_items=[_new("Annual billing for enterprise")]))
    assert [i.id for i in added] == ["A1"]
    t.set_dismissed("A1")
    added, _ = t.apply(ExtractionResult(new_items=[_new("Annual billing for enterprise")]))
    assert [i.id for i in added] == ["A2"]
    assert title_similarity("Send the deck to Acme", "Book the venue") < 0.82


@pytest.mark.xfail(strict=True, reason="BUG: a 'Hey Claude' command deduped onto an existing item creates a non-explicit agent run")
def test_explicit_command_merged_into_existing_item_stays_explicit() -> None:
    t = _tracker()
    t.apply(ExtractionResult(new_items=[_new("Draft the launch email", owner="Alex")]))
    command = _new("Draft the launch email", owner="Assistant", delegate=True, agent_prompt="Draft it", explicit_request=True)
    t.apply(ExtractionResult(new_items=[command]))
    agent = t.meeting.items["A1"].agent
    assert agent is not None and agent.explicit  # agents.mode = "commands" auto-runs only explicit runs


def test_updates_change_fields_and_bump_revision() -> None:
    t = _tracker()
    t.apply(ExtractionResult(new_items=[_new("Send the deck", owner="Sam")]))
    _, updated = t.apply(ExtractionResult(updates=[
        ItemUpdate(id="A1", status=ItemStatus.DONE, due_text="tomorrow", priority="HIGH", evidence="done already"),
        ItemUpdate(id="A404", title="ghost"),
    ]))
    item = t.meeting.items["A1"]
    assert updated == [item]
    assert (item.status, item.due_date, item.priority, item.evidence, item.revision) == (
        ItemStatus.DONE, "2026-10-15", "high", "done already", 2
    )
    _, again = t.apply(ExtractionResult(updates=[ItemUpdate(id="A1", status=ItemStatus.DONE)]))
    assert again == [] and item.revision == 2  # no-op updates don't bump


def test_dismiss_update() -> None:
    t = _tracker()
    t.apply(ExtractionResult(new_items=[_new("Send the deck")]))
    t.apply(ExtractionResult(updates=[ItemUpdate(id="A1", dismiss=True)]))
    assert t.meeting.items["A1"].dismissed and t.meeting.items_of(ItemKind.ACTION) == []


def test_agent_changes_only_while_suggested() -> None:
    t = _tracker()
    t.apply(ExtractionResult(new_items=[_new("Research Globex pricing")]))
    t.apply(ExtractionResult(updates=[ItemUpdate(id="A1", delegate=True, agent_prompt="Research it")]))
    item = t.meeting.items["A1"]
    assert item.agent is not None and item.agent.prompt == "Research it" and not item.agent.explicit
    t.apply(ExtractionResult(updates=[ItemUpdate(id="A1", agent_prompt="Research Globex and Initech")]))
    assert item.agent.prompt == "Research Globex and Initech"
    item.agent.status = AgentStatus.RUNNING
    rev = item.revision
    t.apply(ExtractionResult(updates=[ItemUpdate(id="A1", agent_prompt="Something else"), ItemUpdate(id="A1", delegate=False)]))
    assert item.agent is not None and item.agent.prompt == "Research Globex and Initech" and item.revision == rev
    item.agent.status = AgentStatus.SUGGESTED
    t.apply(ExtractionResult(updates=[ItemUpdate(id="A1", delegate=False)]))
    assert item.agent is None


def test_agents_are_only_for_actions() -> None:
    t = _tracker()
    (item,), _ = t.apply(ExtractionResult(new_items=[_new("Use annual billing", "decision", delegate=True)]))
    assert item.agent is None


def test_events_carry_meeting_id() -> None:
    bus = EventBus()
    t = _tracker(bus)
    t.apply(ExtractionResult(new_items=[_new("Send the deck")], meeting_title="Q4 planning"))
    t.apply(ExtractionResult(updates=[ItemUpdate(id="A1", owner="Sam")]))
    t.apply(ExtractionResult(updates=[ItemUpdate(id="A1", owner="Sam")]))  # no change, no event
    assert [e.type for e in bus.history] == ["meeting.updated", "item.added", "item.updated"]
    assert all(e.data["meeting_id"] == t.meeting.id for e in bus.history)
    assert bus.history[1].data["item"]["id"] == "A1" and bus.history[2].data["item"]["owner"] == "Sam"


def test_title_is_set_once_and_participants_merge() -> None:
    t = _tracker()
    t.apply(ExtractionResult(meeting_title="Q4 planning", participants=["Sarah", "sam", "Alex", "me", "Claude", " "]))
    t.apply(ExtractionResult(meeting_title="Something else", participants=["SARAH", "Sam", "Priya"]))
    assert t.meeting.title == "Q4 planning"
    assert t.meeting.participants == ["Sarah", "sam", "Priya"]


def test_edit_normalizes_and_ignores_unknown_fields() -> None:
    bus = EventBus()
    t = _tracker(bus)
    t.apply(ExtractionResult(new_items=[_new("Send the deck", owner="Sam")]))
    item = t.edit("A1", owner="me", due_text="tomorrow", status="done", priority="high", agent="ignored")
    assert item is not None
    assert (item.owner, item.due_text, item.due_date, item.status, item.priority) == (
        "Alex", "tomorrow", "2026-10-15", ItemStatus.DONE, "high"
    )
    assert item.agent is None and bus.history[-1].type == "item.updated"
    assert t.edit("A99", title="x") is None


@pytest.mark.xfail(strict=True, reason="BUG: edit() treats None/'' as 'keep', so the dashboard cannot clear an owner or deadline")
def test_edit_can_clear_owner_and_deadline() -> None:
    t = _tracker()
    t.apply(ExtractionResult(new_items=[_new("Send the deck", owner="Sam", due_text="by Friday")]))
    item = t.edit("A1", owner=None, due_text=None, due_date=None)
    assert item is not None and (item.owner, item.due_text, item.due_date) == (None, None, None)


def test_add_manual_and_set_dismissed() -> None:
    bus = EventBus()
    t = _tracker(bus)
    item = t.add_manual(ItemKind.ACTION, "Send the deck", owner="me", due_text="by Friday")
    assert (item.id, item.owner, item.due_date, item.confidence) == ("A1", "Alex", "2026-10-16", 1.0)
    assert t.set_dismissed("A1").dismissed and t.set_dismissed("A1").revision == 2  # second call is a no-op
    assert t.set_dismissed("A9") is None
    assert [e.type for e in bus.history] == ["item.added", "item.updated"]


@pytest.mark.xfail(strict=True, reason="BUG: add_manual() raises IndexError when the title dedups onto an unchanged item")
def test_add_manual_duplicate_returns_the_existing_item() -> None:
    t = _tracker()
    first = t.add_manual(ItemKind.ACTION, "Send the deck to Acme")
    assert t.add_manual(ItemKind.ACTION, "Send the deck to Acme").id == first.id
