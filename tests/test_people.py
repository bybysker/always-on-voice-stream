"""Who you talk to: names heard as you address people (brain/people.py), where they are kept, how
Claude and your corrections change them, and the people remembered across meetings (known_people.py)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from helpers import seg
from starlette.testclient import TestClient
from test_app_meetings import WED, StubHost, _save
from test_session import Harness, IdleSource
from test_summarizer import _summary

from voicestream import known_people
from voicestream.app.meetings import Meetings
from voicestream.brain.llm import ScriptedLLM
from voicestream.brain.people import (
    add_person,
    addressed,
    finalize,
    other_side,
    remove_person,
    same_person,
    speaker_label,
)
from voicestream.brain.summarizer import full_transcript, rerender, summarize
from voicestream.brain.tracker import ExtractionResult, ItemTracker
from voicestream.config import Config
from voicestream.models import Meeting, PersonInfo
from voicestream.storage import load_meeting
from voicestream.transcribe import keywords_for

KNOWN = ["Sam Lefèvre", "Julie", "Ashley", "Élodie Martin"]
NOT_NAMES = ["Camille Martin", "Me", "Others", "hey", "claude", "ok", "dis", "hé", "assistant"]


def names(text: str, known: list[str] = KNOWN) -> list[tuple[str, bool]]:
    return [(a.name, a.strong) for a in addressed(text, known, NOT_NAMES)]


# ----------------------------------------------------------------- the inference

STRONG = [
    # English, as whisper punctuates it
    ("Sam, can you send the deck?", "Sam Lefèvre"),
    ("Thanks Sam", "Sam Lefèvre"),
    ("Thanks, Sam!", "Sam Lefèvre"),
    ("Thank you so much, Tom.", "Tom"),
    ("Hi Marc, how are you?", "Marc"),
    ("Hey Tom.", "Tom"),
    ("Good morning Priya, welcome aboard.", "Priya"),
    ("What do you think, Paul?", "Paul"),
    ("Okay, so Paul, can you take it?", "Paul"),
    ("Hello everyone, Paul, could you start?", "Paul"),
    ("Okay Max, go ahead.", "Max"),
    ("Thanks Sam Taylor.", "Sam Taylor"),  # a different Sam from the one known
    ("Thanks again, Nadia.", "Nadia"),
    ("Bye Kevin!", "Kevin"),
    ("Sorry Mia, you were saying?", "Mia"),
    ("Right, Omar?", "Omar"),
    # French
    ("Bonjour Paul, ça va ?", "Paul"),
    ("Salut Marc !", "Marc"),
    ("Merci Julie.", "Julie"),
    ("Merci, Julie.", "Julie"),
    ("Merci beaucoup Hugo pour la démo.", "Hugo"),
    ("Paul, tu peux m'envoyer le devis ?", "Paul"),
    ("Léa, t'as vu le mail ?", "Léa"),
    ("Thomas, est-ce que tu peux regarder ?", "Thomas"),
    ("Inès, vous pouvez partager l'écran ?", "Inès"),
    ("D'accord Julie", "Julie"),
    ("D'accord Marc, je regarde.", "Marc"),
    ("Oui Chloé, c'est ça.", "Chloé"),
    ("Bonjour Monsieur Dubois.", "Dubois"),
    ("Coucou Zoé !", "Zoé"),
    ("Tu en penses quoi, Antoine ?", "Antoine"),
    ("Bonsoir Jean-Pierre.", "Jean-Pierre"),
    ("Élodie, tu peux présenter ?", "Élodie Martin"),  # the known form of the name
    ("Elodie, tu peux présenter ?", "Élodie Martin"),  # without its accent, as the transcriber may write it
]


@pytest.mark.parametrize(("text", "name"), STRONG)
def test_a_name_you_address_is_found(text: str, name: str) -> None:
    assert names(text) == [(name, True)]


WEAK_KNOWN = [
    "Sam, the deck is ready.",
    "That's fine for you, Sam.",
    "Sam?",
    "Alors, Julie, on fait quoi ?",
    "Ashley, the numbers look fine.",
    "Julie, je pense que c'est bon.",
]


@pytest.mark.parametrize("text", WEAK_KNOWN)
def test_a_known_name_counts_in_a_weaker_pattern(text: str) -> None:
    found = names(text)
    assert len(found) == 1 and not found[0][1]


@pytest.mark.parametrize("text", [t.replace("Sam", "Rob").replace("Julie", "Rose").replace("Ashley", "Ruth") for t in WEAK_KNOWN])
def test_an_unknown_name_needs_a_strong_pattern(text: str) -> None:
    assert names(text) == []


NEGATIVE = [
    # talked about, not to
    "I'll ask Sam.", "Sam said it's fine.", "I talked to Paul, he is fine.", "Thanks to Paul for the slides.",
    "Paul, he said no.", "Paul, Marie and Tom will join.", "We met Paul, Marie.", "Yes Paul said so.",
    "Did Sam send it?", "Let me check with Tom tomorrow.", "Sam's deck is great.", "Email Rachel the figures.",
    "On mange chez Marc.", "Je travaille avec Paul, on verra.", "Merci à Julie pour le travail.",
    "Julie m'a dit que c'était bon.", "Je vais demander à Léa.", "Il faut prévenir Hugo avant lundi.",
    "Paul, il est parti.", "C'est le dossier de Marc.",
    # words that open a sentence, days, months, teams, the assistant, you
    "Hey Claude, can you draft the email?", "Merci Camille.", "Thanks Camille!", "Honestly, can you check?",
    "Finally, could you send it?", "Marketing, can you share?", "Thanks Monday.", "Okay Google.",
    "Tomorrow, can you check?", "Bonjour à tous.", "Merci beaucoup.", "Merci à tous.", "Thank God.", "OK.",
    "Hi everyone!", "Hello team.", "Paris, c'est loin.", "Franchement, tu exagères.", "Sinon, tu peux voir ?",
    "Bref, vous voyez l'idée ?", "Écoute, tu peux regarder ?", "Ensuite, tu ajoutes la ligne.",
    "Dis Claude, fais un résumé.", "Thanks April.", "Merci Mai.", "Hi CEO.", "Thanks, everybody!",
    "Lundi, tu peux ?", "Guys, can you hear me?", "Basically, you need a key.", "Ben, tu vois, c'est compliqué.",
]


@pytest.mark.parametrize("text", NEGATIVE)
def test_names_only_talked_about_or_not_names_are_not_found(text: str) -> None:
    assert names(text) == []


def test_several_people_in_one_line_and_the_strongest_reading_wins() -> None:
    assert names("Hi Sam and Julie.") == [("Sam Lefèvre", True), ("Julie", True)]
    assert names("Hi Tom, Anna and Bob!", []) == [("Tom", True), ("Anna", True), ("Bob", True)]
    assert names("Sam, the deck is ready. Thanks Sam!") == [("Sam Lefèvre", True)]
    assert names("Julie, c'est bon. Paul, tu valides ?") == [("Julie", False), ("Paul", True)]


def test_a_known_person_may_carry_a_name_the_stoplist_has() -> None:
    assert names("Thanks April.", ["April Jones"]) == [("April Jones", True)]
    assert names("Merci Camille.", ["Camille"]) == []  # never you, known or not


def test_two_known_people_with_one_first_name_stay_as_said() -> None:
    assert names("Thanks Sam!", ["Sam Lefèvre", "Sam Martin"]) == [("Sam", True)]


def test_same_person() -> None:
    assert same_person("Sam", "Sam Lefèvre") and same_person("élodie", "Élodie Martin")
    assert not same_person("Sam Lefèvre", "Sam Martin") and not same_person("Sam", "Samuel")


# --------------------------------------------------------------- a meeting's people


def test_add_remove_and_never_back() -> None:
    m = Meeting()
    assert add_person(m, "Sam", "Alex", heard=True, strong=True)
    assert m.participants == ["Sam"] and m.people["Sam"] == PersonInfo(heard=True, strong=True)
    assert not m.people["Sam"].confirmed()
    assert add_person(m, "Sam Lefèvre", "Alex", model=True)  # Claude confirms, with the full name
    assert m.participants == ["Sam Lefèvre"] and m.people["Sam Lefèvre"].confirmed()
    assert not add_person(m, "Sam", "Alex", heard=True)  # nothing new
    assert not add_person(m, "Alex", "Alex", you=True) and not add_person(m, "me", "Alex", model=True)
    assert remove_person(m, "Sam")
    assert m.participants == [] and m.people == {} and m.people_removed == ["Sam"]
    assert not add_person(m, "Sam Lefèvre", "Alex", model=True)  # not by Claude
    assert not add_person(m, "Sam", "Alex", heard=True, strong=True)  # nor by hearing it again
    assert add_person(m, "Sam", "Alex", you=True)  # only by you
    assert m.participants == ["Sam"] and m.people_removed == [] and m.people["Sam"].you


def test_claude_s_final_word_keeps_what_you_said() -> None:
    m = Meeting(participants=["Legacy"])  # saved before people were tracked: Claude's
    add_person(m, "Sam", heard=True, strong=True)  # "Thanks Sam" — Claude may miss someone who never spoke
    add_person(m, "Julie", heard=True)  # "Julie, c'est bon." — a weak guess
    add_person(m, "Marc", model=True)  # the live pass thought so
    add_person(m, "Inès", you=True)
    finalize(m, ["Paul", "Julie"])
    assert m.participants == ["Sam", "Julie", "Inès", "Paul"]
    assert m.people["Julie"].model and m.people["Paul"] == PersonInfo(model=True)


def test_the_other_side_is_named_only_when_it_is_one_person() -> None:
    m = Meeting(participants=["Sam"])
    line, mine = seg("Hello", "system"), seg("Hi", "mic")
    assert other_side(m) == "Sam"
    assert speaker_label(line, other_side(m)) == "Sam" and speaker_label(mine, other_side(m)) == "Me"
    assert line.speaker == "Others"  # the stored label never changes
    m.participants.append("Julie")
    assert other_side(m) is None and speaker_label(line, other_side(m)) == "Others"
    named = seg("Hello", "system").model_copy(update={"speaker": "Léa"})  # an imported transcript's own name
    assert speaker_label(named, "Sam") == "Léa"


def test_the_live_extraction_confirms_and_respects_removals() -> None:
    m = Meeting()
    tracker = ItemTracker(m, None, "Alex")
    add_person(m, "Sam", heard=True)
    remove_person(m, "Marc")
    tracker.apply(ExtractionResult(participants=["Sam", "Marc", "Alex", "Claude"]))
    assert m.participants == ["Sam"] and m.people["Sam"].model and m.people_removed == ["Marc"]


# ---------------------------------------------------------- notes and the summary


def test_notes_say_who_and_the_summary_reads_the_other_side_by_name() -> None:
    m = Meeting(title="1:1", participants=["Sam"])
    m.segments += [seg("Can you send it?", "system", t=0), seg("Sure.", "mic", t=4)]
    assert "with Sam" in rerender(m, "Alex")
    text = full_transcript(m)
    assert "00:00 Sam] Can you send it?" in text and "00:04 Me] Sure." in text
    m.participants.append("Julie")
    assert "00:00 Others]" in full_transcript(m)


async def test_the_summary_keeps_your_corrections(cfg: Config) -> None:
    cfg.brain.provider = "anthropic"
    m = Meeting(title="Sync")
    m.segments.append(seg("Thanks Sam, Marc, see you.", "mic"))
    add_person(m, "Sam", heard=True, strong=True)
    add_person(m, "Inès", you=True)
    remove_person(m, "Marc")
    llm = ScriptedLLM([_summary(participants=["Marc", "Julie"])])
    await summarize(cfg, m, ItemTracker(m, None, cfg.user_name), llm)
    assert m.participants == ["Sam", "Inès", "Julie"]
    sent = llm.calls[0]["content"][1]["text"]
    assert "Not in this meeting (the user said so): Marc" in sent


# ------------------------------------------------------------------ known people


def test_known_people_are_remembered_and_forgotten_per_meeting(cfg: Config) -> None:
    root = cfg.data_path
    assert known_people.names(root) == []
    known_people.remember(root, ["Sam"], "m1")
    known_people.remember(root, ["Sam Lefèvre", "Julie"], "m2")  # the fuller name replaces "Sam"
    assert sorted(known_people.names(root)) == ["Julie", "Sam Lefèvre"]
    known_people.forget(root, "Julie", "m2")  # only m2 taught it
    known_people.forget(root, "Sam", "m2")  # m1 still vouches for Sam
    assert known_people.names(root) == ["Sam Lefèvre"]
    (root / known_people.FILE).write_text("{not json", encoding="utf-8")
    assert known_people.names(root) == []  # a damaged file is no reason to stop


def test_only_confirmed_participants_are_remembered(cfg: Config) -> None:
    m = Meeting(participants=["Old"])
    add_person(m, "Guess", heard=True, strong=True)
    add_person(m, "Claude says", model=True)
    add_person(m, "Typed", you=True)
    known_people.remember_meeting(cfg.data_path, m, cfg.user_name)
    assert sorted(known_people.names(cfg.data_path)) == ["Claude says", "Old", "Typed"]


def test_known_people_help_speech_recognition(cfg: Config) -> None:
    cfg.brain.attendees = ["Sarah"]
    known_people.remember(cfg.data_path, ["Sam Lefèvre", "Sarah"], "m1")
    assert keywords_for(cfg) == ["Sarah", "Alex", "Sam Lefèvre"]


# ------------------------------------------------------------- the live meeting


async def test_a_name_you_say_joins_the_meeting_while_the_line_is_handled(cfg: Config) -> None:
    known_people.remember(cfg.data_path, ["Julie Durand"], "earlier")
    h = Harness(cfg, [IdleSource("mic")], always_on=False)
    run = asyncio.ensure_future(h.a.run())
    try:
        await h.wait_for("meeting.started")
        session = h.a.current
        assert session is not None
        await h.say("I'll ask Sam about it.")
        await h.say("Sam, can you share your screen?", "system")  # them speaking: not you addressing Sam
        assert session.meeting.participants == []
        await h.say("Thanks Sam, that helps.")
        assert session.meeting.participants == ["Sam"]  # no extraction, no waiting
        event = h.events("meeting.people")[-1].data
        assert event["participants"] == ["Sam"] and event["people"]["Sam"]["heard"] and event["people"]["Sam"]["strong"]
        await h.say("Julie, c'est bon pour toi.")  # weak, but known from an earlier meeting
        assert session.meeting.participants == ["Sam", "Julie Durand"]

        session.edit_people(["Inès"], ["Sam"])
        await h.say("Merci Sam !")
        assert session.meeting.participants == ["Julie Durand", "Inès"] and session.meeting.people_removed == ["Sam"]
        assert "Inès" in known_people.names(cfg.data_path)

        session.edit_people([], ["Julie Durand"])
        await h.say("Can you hear me?", "system")
        path = await h.a.end_meeting()
        assert path is not None
        folder = path.parent
        saved, _ = load_meeting(folder)
        assert saved.participants == ["Inès"] and saved.people_removed == ["Sam", "Julie Durand"]
        assert "with Inès" in path.read_text(encoding="utf-8")
        # one other person: the written transcript names the other side; the raw one keeps its label
        assert "] Inès:** Can you hear me?" in (folder / "transcript.md").read_text(encoding="utf-8")
        assert '"speaker":"Others"' in (folder / "transcript.jsonl").read_text(encoding="utf-8")
        assert "Julie Durand" in known_people.names(cfg.data_path)  # the earlier meeting still vouches for her
    finally:
        h.a.stop()
        await asyncio.wait_for(run, 10)


# ------------------------------------------------------ a past meeting, the route


def test_correcting_a_past_meeting(cfg: Config) -> None:
    folder = _save(cfg, "2026-10-14_110000-planning", started=WED, title="Q4 planning")
    host = StubHost(cfg)
    m = Meetings(host)
    out = m.edit_people("2026-10-14_110000-planning", ["Sam"], [])
    assert out["participants"] == ["Sam"] and out["people"]["Sam"]["you"]
    meeting, _ = load_meeting(folder)
    assert meeting.participants == ["Sam"]
    assert "with Sam" in (folder / "notes.md").read_text(encoding="utf-8")
    assert known_people.names(cfg.data_path) == ["Sam"]
    out = m.edit_people("2026-10-14_110000-planning", [], ["Sam"])
    assert out["participants"] == [] and out["people_removed"] == ["Sam"]
    assert known_people.names(cfg.data_path) == []
    assert [e.data["participants"] for e in host.bus.history if e.type == "meeting.people"] == [["Sam"], []]


def test_people_route(tmp_path: Path) -> None:
    from test_app_host import _factory, _write_config
    from test_app_routes import BASE, TOKEN

    from voicestream.app.host import AppHost
    from voicestream.dashboard.server import TOKEN_HEADER, create_app

    host = AppHost(_write_config(tmp_path), process_setup=False, prefs_path=tmp_path / "app.json", assistant_factory=_factory())
    folder = _save(host.cfg, "2026-10-14_110000-planning", started=WED, title="Q4 planning")
    path = "/api/meetings/2026-10-14_110000-planning/people"
    with TestClient(create_app(host, TOKEN), base_url=BASE) as anonymous:
        assert anonymous.post(path, json={"add": ["Sam"]}).status_code == 401
        assert anonymous.post(path, json={"add": ["Sam"]}, headers={TOKEN_HEADER: TOKEN, "host": "evil.example"}).status_code == 403
    with TestClient(create_app(host, TOKEN), base_url=BASE, headers={TOKEN_HEADER: TOKEN}) as c:
        r = c.post(path, json={"add": ["  Sam  Lefèvre "], "remove": []})
        assert r.status_code == 200 and r.json()["participants"] == ["Sam Lefèvre"]
        assert c.post(path, json={"remove": ["Sam Lefèvre"]}).json()["people_removed"] == ["Sam Lefèvre"]
        assert c.post(path, json={}).status_code == 400
        assert c.post(path, json={"add": [""]}).status_code == 400
        assert c.post(path, json={"add": ["a\nb"]}).status_code == 400
        assert c.post(path, json={"add": ["x" * 81]}).status_code == 400
        assert c.post(path, json={"add": ["Sam"], "who": 1}).status_code == 400
        assert c.post("/api/meetings/../people", json={"add": ["Sam"]}).status_code == 404
        assert c.post("/api/meetings/nope/people", json={"add": ["Sam"]}).status_code == 404
    assert load_meeting(folder)[0].people_removed == ["Sam Lefèvre"]
