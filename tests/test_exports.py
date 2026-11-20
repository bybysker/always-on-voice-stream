"""What you download from a meeting (voicestream/exports.py): its summary PDF, its transcript as PDF and text."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from test_pdf import page_texts
from voicestream.exports import (
    due_label,
    export,
    filename,
    recap_view,
    summary_pdf,
    transcript_lines,
    transcript_text,
)
from voicestream.models import Item, ItemKind, ItemStatus, Meeting, Recap, TranscriptSegment

START = datetime(2026, 11, 4, 13, 0, tzinfo=timezone.utc)


def _seg(text: str, source: str = "system", speaker: str = "Others", t: float = 0.0, voice: int | None = None) -> TranscriptSegment:
    return TranscriptSegment(source=source, speaker=speaker, text=text, t_start=t, t_end=t + 4, voice=voice)


def _meeting(**kw: object) -> Meeting:
    items = {
        "A1": Item(id="A1", kind=ItemKind.ACTION, title="Send the final copy", owner="Julie", due_date="2026-11-06"),
        "A2": Item(id="A2", kind=ItemKind.ACTION, title="Prepare the launch e-mails", owner="me", due_date="2027-01-08"),
        "A3": Item(id="A3", kind=ItemKind.ACTION, title="Approve the mock-up", owner="Paul", status=ItemStatus.DONE),
        "A4": Item(id="A4", kind=ItemKind.ACTION, title="Research Globex pricing", owner="Assistant"),
        "A5": Item(id="A5", kind=ItemKind.ACTION, title="Ask my manager for a discount", owner="me"),
        "A6": Item(id="A6", kind=ItemKind.ACTION, title="Old plan", owner="Paul", status=ItemStatus.CANCELLED),
        "A7": Item(id="A7", kind=ItemKind.ACTION, title="Misheard", owner="Paul", dismissed=True),
        "D1": Item(id="D1", kind=ItemKind.DECISION, title="Launch stays on 18 November"),
        "Q1": Item(id="Q1", kind=ItemKind.QUESTION, title="Translate the page at launch?"),
        "Q2": Item(id="Q2", kind=ItemKind.QUESTION, title="Who hosts it?", status=ItemStatus.DONE),
        "N1": Item(id="N1", kind=ItemKind.NOTE, title="Mock-up approved by Acme"),
    }
    base: dict[str, object] = dict(
        title="Acme v2 launch", started_at=START, ended_at=START + timedelta(minutes=42), language="en",
        participants=["Julie", "Paul"], items=items, tldr="Launch check-in.",
        fields={"Budget": "40 k€ a year", "Next step": "Copy by Friday"},
        segments=[_seg("Hello both", "mic", "Me", 0), _seg("Hi, the mock-up is approved", t=5, voice=1),
                  _seg("   ", t=9), _seg("I will send the copy", t=12, voice=2)],
        recap=Recap(overview="We checked the v2 launch.", discussed=["Mock-up approved", "Copy due Friday"],
                    next_steps=["Final review on 13 November"], hidden=["A5"]),
    )
    return Meeting(**{**base, **kw})


def test_the_summary_is_the_recap_and_the_meetings_items_minus_what_isnt_for_them() -> None:
    v = recap_view(_meeting(), "Alex Martin")
    assert (v["title"], v["lang"], v["written"], v["edited"]) == ("Acme v2 launch", "en", True, False)
    assert v["participants"] == ["Alex Martin", "Julie", "Paul"]
    assert v["overview"] == "We checked the v2 launch." and v["next_steps"] == ["Final review on 13 November"]
    todos = {t["id"]: t for t in v["todos"]}
    assert list(todos) == ["A1", "A2", "A3", "A5"]  # not the assistant's, the cancelled or the dismissed ones
    assert todos["A2"]["owner"] == "Alex Martin" and todos["A1"]["owner"] == "Julie"  # "me" is you, by name
    assert (todos["A1"]["due"], todos["A2"]["due"], todos["A3"]["due"]) == ("Fri 6 Nov", "Fri 8 Jan 2027", "")
    assert todos["A3"]["done"] and todos["A5"]["hidden"] and not todos["A1"]["hidden"]
    assert [d["title"] for d in v["decisions"]] == ["Launch stays on 18 November"]
    assert [q["id"] for q in v["questions"]] == ["Q1"]  # an answered question isn't open
    assert v["labels"]["discussed"] == "What we discussed"


def test_a_meeting_from_before_summaries_gets_one_from_what_it_has() -> None:
    v = recap_view(_meeting(recap=None), "Me")
    assert not v["written"] and v["overview"] == "Launch check-in."
    assert v["discussed"] == ["Mock-up approved by Acme"] and v["next_steps"] == ["Copy by Friday"]
    assert v["participants"] == ["Julie", "Paul"]  # your name isn't known yet: not "Me"
    assert [t["owner"] for t in v["todos"] if t["id"] == "A2"] == ["me"]


def test_the_summary_pdf_holds_what_the_tab_shows_and_nothing_private() -> None:
    texts = page_texts(summary_pdf(_meeting(), "Alex Martin", created=START))
    text = " ".join(texts)
    for expected in ("MEETING SUMMARY", "Acme v2 launch", "Participants: Alex Martin, Julie, Paul", "Overview",
                     "We checked the v2 launch.", "What we discussed", "Mock-up approved", "Decisions",
                     "Launch stays on 18 November", "Next steps", "Final review on 13 November", "To-dos",
                     "TASK OWNER DUE", "Send the final copy Julie Fri 6 Nov", "Approve the mock-up Paul —",
                     "Open questions", "Translate the page at launch?", "Page 1 of 1"):
        assert expected in text, expected
    for private in ("Ask my manager", "Globex", "Budget", "40 k€", "Old plan", "Misheard", "Who hosts it?", "Launch check-in."):
        assert private not in text, private
    order = [text.index(h) for h in ("Overview", "What we discussed", "Decisions", "Next steps", "To-dos", "Open questions")]
    assert order == sorted(order)


def test_a_french_meeting_gets_a_french_summary_whatever_the_apps_language() -> None:
    text = " ".join(page_texts(summary_pdf(_meeting(language="fr"), "Alex Martin")))
    for expected in ("COMPTE RENDU DE RÉUNION", "mercredi 4 novembre 2026", "Participants\xa0: Alex Martin", "En résumé",
                     "Points abordés", "Prochaines étapes", "ACTION RESPONSABLE ÉCHÉANCE", "ven. 6 nov.", "ven. 8 janv. 2027",
                     "Questions ouvertes", "Page 1 sur 1"):
        assert expected in text, expected


def test_an_empty_meeting_still_gives_a_summary() -> None:
    text = " ".join(page_texts(summary_pdf(Meeting(started_at=START, language="en"), "Alex")))
    assert "Nothing was noted in this meeting." in text and "Meeting 2026-11-04" in text


def test_due_dates_as_people_read_them() -> None:
    item = Item(id="A1", kind=ItemKind.ACTION, title="x", due_date="2026-12-25")
    assert due_label(item, "en", 2026) == "Fri 25 Dec" and due_label(item, "fr", 2026) == "ven. 25 déc."
    assert due_label(item, "en", 2025) == "Fri 25 Dec 2026"
    assert due_label(Item(id="A2", kind=ItemKind.ACTION, title="x", due_date="soon", due_text="by Friday"), "en") == "by Friday"


def test_the_transcript_names_everyone_as_the_meeting_page_does() -> None:
    m = _meeting(voices={"1": "Julie", "2": "Paul"})
    assert transcript_lines(m, "Alex Martin") == [
        ("00:00", "Alex Martin", "Hello both"), ("00:05", "Julie", "Hi, the mock-up is approved"),
        ("00:12", "Paul", "I will send the copy"),  # a blank line is left out
    ]
    text = transcript_text(m, "Alex Martin").decode("utf-8")
    start, end = START.astimezone(), (START + timedelta(minutes=42)).astimezone()  # in this computer's time zone
    assert text.splitlines()[:4] == ["Acme v2 launch", f"Wednesday 4 November 2026, {start:%H:%M}–{end:%H:%M} · 1 min",
                                     "Participants: Alex Martin, Julie, Paul", ""]
    assert text.endswith("[00:12] Paul: I will send the copy\n")


def test_the_other_side_is_named_and_the_default_label_translated() -> None:
    unvoiced = [_seg("Bonjour", "mic", "Me", 0), _seg("Bonjour à vous", t=5), _seg("On commence ?", t=9)]
    one = _meeting(participants=["Julie"], language="fr", segments=unvoiced)
    assert [s for _, s, _ in transcript_lines(one, "Alex")] == ["Alex", "Julie", "Julie"]  # the one other person
    told_apart = _meeting(participants=["Julie"], language="fr")
    assert [s for _, s, _ in transcript_lines(told_apart, "Alex")] == ["Alex", "Speaker 1", "Speaker 2"]  # as on the page
    two = _meeting(language="fr", segments=unvoiced)
    assert [s for _, s, _ in transcript_lines(two, "Me")] == ["Me", "Autres participants", "Autres participants"]
    yours = _meeting(segments=[_seg("Salut", "mic", "Me", 0), _seg("Salut", speaker="Eux", t=5)])
    assert [s for _, s, _ in transcript_lines(yours, "Me", others_label="Eux")] == ["Me", "Eux"]  # your own label stays


def test_files_are_named_by_date_and_title_and_safe_on_any_disk() -> None:
    m = _meeting(title='Acme: v2 / "launch"?  ')
    assert filename(m, "summary", "pdf") == "2026-11-04 Acme v2 launch - Summary.pdf"
    assert filename(_meeting(language="fr"), "transcript", "txt") == "2026-11-04 Acme v2 launch - Transcription.txt"
    assert filename(_meeting(title=None, language="fr"), "summary", "pdf").startswith("2026-11-04 Réunion 2026-11-04 ")
    e = export(_meeting(title="Réunion d’équipe"), "transcript", "txt", user_name="Alex")
    assert e.media_type == "text/plain; charset=utf-8"
    assert e.content_disposition() == ('attachment; filename="2026-11-04 Reunion d_equipe - Transcript.txt"; '
                                       "filename*=UTF-8''2026-11-04%20R%C3%A9union%20d%E2%80%99%C3%A9quipe%20-%20Transcript.txt")


@pytest.mark.parametrize(("what", "fmt"), [("summary", "txt"), ("notes", "pdf"), ("transcript", "docx")])
def test_only_the_files_there_are(what: str, fmt: str) -> None:
    with pytest.raises(ValueError, match="summary as pdf, transcript as pdf/txt"):
        export(_meeting(), what, fmt, user_name="Alex")


def test_every_export_is_made() -> None:
    m = _meeting()
    for what, fmt in (("summary", "pdf"), ("transcript", "pdf"), ("transcript", "txt")):
        e = export(m, what, fmt, user_name="Alex", created=START)
        assert e.data and e.filename.endswith("." + fmt)
        if fmt == "pdf":
            assert e.data.startswith(b"%PDF-1.4") and e.media_type == "application/pdf"
    assert "Hi, the mock-up is approved" in " ".join(page_texts(export(m, "transcript", "pdf", user_name="Alex").data))
