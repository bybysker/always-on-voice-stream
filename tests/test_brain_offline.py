"""Offline brain: spoken-deadline resolution, the rule-based extractor and the item tracker.

Dates are resolved against ``helpers.WEDNESDAY`` (2026-10-14).
"""

from __future__ import annotations

import re
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


@pytest.mark.parametrize("spoken", ["EOD Friday", "by end of day Friday"])
def test_resolve_eod_on_a_named_day(spoken: str) -> None:
    assert resolve_due(spoken, WEDNESDAY) == "2026-10-16"


def test_resolve_weekday_followed_by_next_week() -> None:
    assert resolve_due("Friday next week", WEDNESDAY) == "2026-10-23"


@pytest.mark.parametrize(
    ("spoken", "expected"),
    [
        ("by Monday EOD", "2026-10-19"),
        ("EOD next Wednesday", "2026-10-21"),
        ("end of day Wednesday", "2026-10-14"),  # said on a Wednesday: today, not next week
        ("Wednesday EOD", "2026-10-14"),
        ("next week Friday", "2026-10-23"),
        ("Friday of next week", "2026-10-23"),
        ("by tomorrow EOD", "2026-10-15"),
    ],
)
def test_resolve_eod_and_next_week_around_a_named_day(spoken: str, expected: str) -> None:
    assert resolve_due(spoken, WEDNESDAY) == expected


@pytest.mark.parametrize(
    ("spoken", "expected"),
    [
        ("d'ici vendredi", "2026-10-16"),
        ("vendredi prochain", "2026-10-16"),  # the coming Friday
        ("vendredi de la semaine prochaine", "2026-10-23"),
        ("fin de la semaine prochaine", "2026-10-23"),
        ("d'ici la fin de la semaine", "2026-10-16"),
        ("la semaine prochaine", "2026-10-19"),
        ("dans quinze jours", "2026-10-28"),  # a fortnight, not 15 days
        ("d'ici quinze jours", "2026-10-28"),
        ("dans huit jours", "2026-10-21"),  # a week
        ("dans trois jours", "2026-10-17"),
        ("dans deux semaines", "2026-10-28"),
        ("le 1er décembre", "2026-12-01"),
        ("le 20 octobre", "2026-10-20"),
        ("demain", "2026-10-15"),
        ("après-demain", "2026-10-16"),
        ("aujourd'hui", "2026-10-14"),
        ("ce soir", "2026-10-14"),
        ("en fin de journée", "2026-10-14"),
        ("vendredi en fin de journée", "2026-10-16"),  # the named day wins here too
        ("d'ici la fin du mois", "2026-10-31"),
        ("le mois prochain", "2026-11-01"),
    ],
)
def test_resolve_french_deadlines(spoken: str, expected: str) -> None:
    assert resolve_due(spoken, WEDNESDAY) == expected


def test_french_possessive_mon_is_not_monday() -> None:
    assert resolve_due("avant mon départ", WEDNESDAY) is None
    assert resolve_due("pour mon retour lundi", WEDNESDAY) == "2026-10-19"
    assert resolve_due("by Mon", WEDNESDAY) == resolve_due("Mon morning", WEDNESDAY) == "2026-10-19"


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


def test_follow_up_keeps_its_verb() -> None:
    (it,) = _extract(seg("Follow up with legal about the NDA next week."))
    assert it.title.lower().startswith("follow up with legal")


@pytest.mark.parametrize("text", ["The action items are: Sam sends the deck.", "Follow-ups: Sarah books the venue."])
def test_plural_item_markers_leave_no_stray_s(text: str) -> None:
    assert [i.title for i in _extract(seg(text)) if re.match(r"S\b", i.title)] == []


@pytest.mark.parametrize(
    ("text", "title", "due"),
    [("I'll work on this feature tomorrow.", "Work on this feature", "tomorrow"),
     ("I'll comment on this doc by Friday.", "Comment on this doc", "by Friday")],
)
def test_on_this_object_is_not_a_deadline(text: str, title: str, due: str) -> None:
    (it,) = _extract(seg(text))
    assert (it.title, it.owner, it.due_text) == (title, "Alex", due)


@pytest.mark.parametrize("text", ["So can you send me the file?", "Okay, could you share the deck with Acme?"])
def test_leading_filler_is_not_an_owner(text: str) -> None:
    (it,) = _extract(seg(text, "system"))
    assert it.owner == "Alex"  # someone else asking "can you" asks the user


@pytest.mark.parametrize("text", ["Let me know if you have any questions.", "Let me share my screen."])
def test_more_meeting_chatter_is_not_an_action(text: str) -> None:
    assert _extract(seg(text)) == []


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


def test_follow_up_and_action_as_verbs() -> None:
    (mine,) = _extract(seg("I'll follow up with legal tomorrow."))
    assert (mine.title, mine.owner, mine.due_text) == ("Follow up with legal", "Alex", "tomorrow")
    (meeting,) = _extract(seg("Let's schedule a follow-up meeting next week."))
    assert meeting.title == "Schedule a follow-up meeting"
    (act,) = _extract(seg("We need to take action on the backlog."))
    assert act.title == "Take action on the backlog"
    (label,) = _extract(seg("Follow-up: Sam sends the deck."))
    assert label.title == "Sam sends the deck"


@pytest.mark.parametrize(
    ("text", "owner"),
    [("Okay Sarah, can you book the venue?", "Sarah"),  # fillers before the name are skipped
     ("Hey Sarah, can you book the venue?", "Sarah"),
     ("Ben, can you book the venue?", "Ben"),  # a name, although "ben" is also a French filler
     ("sarah, can you book the venue?", None)],  # not capitalised: not read as a name
)
def test_named_request_needs_a_capitalised_name(text: str, owner: str | None) -> None:
    (it,) = _extract(seg(text))
    assert (it.title, it.owner) == ("Book the venue", owner)


# ------------------------------------------------------------- heuristic (FR)


def test_french_commitments() -> None:
    a, b, c = _extract(
        seg("Je vais envoyer le devis à Acme d'ici vendredi."),
        seg("Je m'occupe de relancer le client demain."),
        seg("Il faut qu'on mette à jour le budget avant lundi."),
    )
    assert (a.kind, a.title, a.owner, a.due_text) == (ItemKind.ACTION, "Envoyer le devis à Acme", "Alex", "d'ici vendredi")
    assert (b.title, b.owner, b.due_text) == ("Relancer le client", "Alex", "demain")
    assert (c.title, c.owner, c.due_text) == ("Mette à jour le budget", None, "avant lundi")
    (theirs,) = _extract(seg("Je vais préparer la démo pour jeudi.", "system"))
    assert (theirs.title, theirs.owner, theirs.due_text) == ("Préparer la démo", None, "pour jeudi")
    (named,) = _extract(seg("Marie-Claire va mettre à jour la roadmap dans quinze jours.", "system"))
    assert (named.title, named.owner, named.due_text) == ("Mettre à jour la roadmap", "Marie-Claire", "dans quinze jours")


@pytest.mark.parametrize(
    ("text", "source", "title", "owner"),
    [
        ("Sarah, tu peux réserver la salle pour l'offsite ?", "mic", "Réserver la salle pour l'offsite", "Sarah"),
        ("Élodie, est-ce que tu peux relire le contrat ?", "mic", "Relire le contrat", "Élodie"),
        ("Jean-Pierre, tu pourrais envoyer le devis ?", "mic", "Envoyer le devis", "Jean-Pierre"),
        ("Alors, tu peux envoyer le devis ?", "system", "Envoyer le devis", "Alex"),  # a filler, not a name
        ("Tu peux envoyer le compte rendu ?", "system", "Envoyer le compte rendu", "Alex"),  # asked of me
        ("Est-ce que vous pouvez relire la proposition ?", "mic", "Relire la proposition", None),  # I asked
    ],
)
def test_french_requests(text: str, source: str, title: str, owner: str | None) -> None:
    (it,) = _extract(seg(text, source))
    assert (it.kind, it.title, it.owner) == (ItemKind.ACTION, title, owner)


def test_french_decisions_questions_and_markers() -> None:
    d, n, q, m = _extract(
        seg("On a décidé de partir sur le plan annuel."),
        seg("Nous avons décidé de garder le prix actuel.", "system"),
        seg("Question ouverte : qui gère la migration ?", "system"),
        seg("Point d'action : je dois envoyer le compte rendu."),
    )
    assert (d.kind, d.title) == (ItemKind.DECISION, "Partir sur le plan annuel")
    assert (n.kind, n.title) == (ItemKind.DECISION, "Garder le prix actuel")
    assert (q.kind, q.title) == (ItemKind.QUESTION, "Qui gère la migration")
    assert (m.kind, m.title, m.owner) == (ItemKind.ACTION, "Envoyer le compte rendu", "Alex")


def test_dis_claude_is_a_command_from_the_mic_only() -> None:
    (it,) = _extract(seg("Dis Claude, prépare un résumé de la proposition Globex."))
    assert (it.owner, it.delegate, it.explicit_request) == ("Assistant", True, True)
    assert it.title == "Prépare un résumé de la proposition Globex"
    assert it.agent_prompt == "prépare un résumé de la proposition Globex."
    (hé,) = _extract(seg("Hé Claude, rédige un mail de relance pour Acme."))
    assert hé.explicit_request and hé.title == "Rédige un mail de relance pour Acme"
    assert _extract(seg("Dis Claude, exporte la liste des clients.", "system")) == []
    assert _extract(seg("Dis Claude merci")) == []


@pytest.mark.parametrize("text", ["Je vais partager mon écran.", "On va commencer.", "Je vais voir."])
def test_french_meeting_chatter_is_not_an_action(text: str) -> None:
    assert _extract(seg(text)) == []


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


def test_unresolvable_new_deadline_clears_the_old_date() -> None:
    t = _tracker()
    t.apply(ExtractionResult(new_items=[_new("Send the deck", due_text="by Friday")]))
    t.apply(ExtractionResult(updates=[ItemUpdate(id="A1", due_text="sometime after the offsite")]))
    item = t.meeting.items["A1"]
    assert (item.due_text, item.due_date) == ("sometime after the offsite", None)


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


@pytest.mark.parametrize("cleared", ["", None])
def test_edit_can_clear_owner_and_deadline(cleared: str | None) -> None:
    t = _tracker()
    t.apply(ExtractionResult(new_items=[_new("Send the deck", owner="Sam", due_text="by Friday")]))
    item = t.edit("A1", owner=cleared, due_text=cleared, due_date=cleared)
    assert item is not None and (item.owner, item.due_text, item.due_date) == (None, None, None)


def test_add_manual_and_set_dismissed() -> None:
    bus = EventBus()
    t = _tracker(bus)
    item = t.add_manual(ItemKind.ACTION, "Send the deck", owner="me", due_text="by Friday")
    assert (item.id, item.owner, item.due_date, item.confidence) == ("A1", "Alex", "2026-10-16", 1.0)
    assert t.set_dismissed("A1").dismissed and t.set_dismissed("A1").revision == 2  # second call is a no-op
    assert t.set_dismissed("A9") is None
    assert [e.type for e in bus.history] == ["item.added", "item.updated"]


def test_add_manual_duplicate_returns_the_existing_item() -> None:
    t = _tracker()
    first = t.add_manual(ItemKind.ACTION, "Send the deck to Acme")
    assert t.add_manual(ItemKind.ACTION, "Send the deck to Acme").id == first.id


def test_add_manual_merges_new_fields_and_refuses_a_blank_title() -> None:
    t = _tracker()
    t.add_manual(ItemKind.ACTION, "Send the deck to Acme")
    item = t.add_manual(ItemKind.ACTION, "Send the deck to Acme", owner="Sam", due_text="by Friday")
    assert (item.id, item.owner, item.due_date, len(t.meeting.items)) == ("A1", "Sam", "2026-10-16", 1)
    with pytest.raises(ValueError):
        t.add_manual(ItemKind.ACTION, "   ")


def test_edit_clearing_the_due_words_clears_the_date_and_unassigned_clears_the_owner() -> None:
    t = _tracker()
    t.apply(ExtractionResult(new_items=[_new("Send the deck", owner="Sam", due_text="by Friday", priority="high")]))
    item = t.edit("A1", due_text="")  # what the dashboard sends: due_text only
    assert item is not None and (item.due_text, item.due_date, item.owner, item.revision) == (None, None, "Sam", 2)
    t.edit("A1", owner="unassigned", priority=None, title="", status=None)  # title/status can't be cleared
    assert (item.owner, item.priority, item.title, item.status, item.revision) == (None, None, "Send the deck", ItemStatus.OPEN, 3)


def test_edit_with_new_unresolvable_deadline_drops_the_old_date() -> None:
    t = _tracker()
    t.apply(ExtractionResult(new_items=[_new("Send the deck", due_text="by Friday")]))
    item = t.edit("A1", due_text="after the offsite")
    assert item is not None and (item.due_text, item.due_date) == ("after the offsite", None)


def test_model_update_with_empty_fields_keeps_them_and_same_words_keep_the_date() -> None:
    t = _tracker()
    t.apply(ExtractionResult(new_items=[_new("Send the deck", owner="Sam", due_text="after the offsite", due_date="2026-10-20")]))
    _, updated = t.apply(ExtractionResult(updates=[ItemUpdate(id="A1", owner=None, due_text="after the offsite")]))
    item = t.meeting.items["A1"]
    assert updated == [] and (item.owner, item.due_text, item.due_date) == ("Sam", "after the offsite", "2026-10-20")


def test_explicit_command_upgrades_a_suggested_agent() -> None:
    t = _tracker()
    t.apply(ExtractionResult(new_items=[_new("Draft the launch email", delegate=True, agent_prompt="Maybe draft it")]))
    agent = t.meeting.items["A1"].agent
    assert agent is not None and not agent.explicit
    t.apply(ExtractionResult(new_items=[_new("Draft the launch email", delegate=True, agent_prompt="Draft it now", explicit_request=True)]))
    assert (agent.explicit, agent.prompt, t.meeting.items["A1"].revision) == (True, "Draft it now", 2)
    agent.status = AgentStatus.RUNNING  # once it runs, a repeated command changes nothing
    t.apply(ExtractionResult(new_items=[_new("Draft the launch email", agent_prompt="Other", explicit_request=True)]))
    assert agent.prompt == "Draft it now" and t.meeting.items["A1"].revision == 2


@pytest.mark.parametrize(
    "text",
    ["We should update the budget sheet before Monday.", "They will send the contract next week.",
     "On va envoyer le devis demain.", "Il doit relire le contrat avant vendredi."],
)
def test_a_pronoun_opening_the_sentence_is_not_an_owner(text: str) -> None:
    t = _tracker()
    (item,), _ = t.apply(extract_heuristic([(0, seg(text, "system"))], user_name="Alex", wake_phrases=_BC.wake_phrases))
    assert item.owner is None
