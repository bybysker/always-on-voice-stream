"""What you download from a meeting: its summary (a PDF to send to the people you met) and its transcript
(PDF or plain text).

The summary is :func:`recap_view`: the meeting page's Summary tab shows it, and the PDF is made from
it, so what you read there is what they get. It is the meeting's :class:`~voicestream.models.Recap`
(written by Claude at the wrap-up, for them: no fields, no private sections) with the meeting's own
to-dos, decisions and open questions, minus the ones you left out. A meeting summarized before recaps
existed gets one from what it has: its summary line, its notes items and its "Next step" field.

Both are in the meeting's language (they go to the people who spoke it), whatever the app's. The
transcript names everyone as the meeting page does, and you by your name rather than "Me".
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal
from urllib.parse import quote

from .brain.people import other_side, speaker_label, voice_labels
from .brain.summarizer import _FR_DAYS, _FR_MONTHS, _lang
from .models import Item, ItemKind, ItemStatus, Meeting, TranscriptSegment, fmt_ts
from .pdf import Column, Document

What = Literal["summary", "transcript"]
Format = Literal["pdf", "txt"]
FORMATS: dict[str, tuple[str, ...]] = {"summary": ("pdf",), "transcript": ("pdf", "txt")}
MEDIA_TYPES = {"pdf": "application/pdf", "txt": "text/plain; charset=utf-8"}

LABELS = {
    "en": {
        "summary": "Meeting summary", "transcript": "Transcript", "meeting": "Meeting",
        "overview": "Overview", "discussed": "What we discussed", "decisions": "Decisions",
        "next_steps": "Next steps", "todos": "To-dos", "questions": "Open questions",
        "task": "Task", "owner": "Owner", "due": "Due", "participants": "Participants: {names}",
        "empty": "Nothing was noted in this meeting.", "page": "Page {n} of {total}",
        "file_summary": "Summary", "file_transcript": "Transcript", "others": "Others",
    },
    "fr": {
        "summary": "Compte rendu de réunion", "transcript": "Transcription", "meeting": "Réunion",
        "overview": "En résumé", "discussed": "Points abordés", "decisions": "Décisions",
        "next_steps": "Prochaines étapes", "todos": "Actions", "questions": "Questions ouvertes",
        "task": "Action", "owner": "Responsable", "due": "Échéance", "participants": "Participants\xa0: {names}",
        "empty": "Rien n’a été noté pendant cette réunion.", "page": "Page {n} sur {total}",
        "file_summary": "Compte rendu", "file_transcript": "Transcription", "others": "Autres participants",
    },
}  # fmt: skip
_EN_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_FR_SHORT_DAYS = ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."]
_FR_SHORT_MONTHS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]
_ASSISTANT = {"assistant", "claude", "l'assistant", "l’assistant"}
"""Owners of the requests you made to the assistant ("Hey Claude…"): never in a summary for others."""
_NEXT_STEP_FIELDS = {"next step", "next steps", "prochaine étape", "prochaines étapes"}


def known_name(user_name: str) -> str:
    """Your name, or ``""`` while it is still the default label ("Me")."""
    name = " ".join(user_name.split())
    return "" if name.casefold() in {"", "me", "moi", "i"} else name


# ------------------------------------------------------------------ the summary


def _visible(meeting: Meeting, kind: ItemKind) -> list[Item]:
    items = meeting.items_of(kind)
    if kind == ItemKind.QUESTION:
        return [i for i in items if i.status == ItemStatus.OPEN]  # answered ones aren't open any more
    return [i for i in items if i.status != ItemStatus.CANCELLED and (i.owner or "").strip().casefold() not in _ASSISTANT]


def due_label(item: Item, lang: str, year: int | None = None) -> str:
    """"Fri 13 Nov" / "ven. 13 nov." (with the year when it isn't ``year``), else the deadline as said."""
    if item.due_date:
        try:
            d = date.fromisoformat(item.due_date)
        except ValueError:
            pass
        else:
            text = (f"{_FR_SHORT_DAYS[d.weekday()]} {d.day} {_FR_SHORT_MONTHS[d.month - 1]}" if lang == "fr"
                    else f"{_EN_DAYS[d.weekday()]} {d.day} {d.strftime('%b')}")
            return text if year in (None, d.year) else f"{text} {d.year}"
    return (item.due_text or "").strip()


def owner_label(item: Item, user_name: str) -> str:
    me = known_name(user_name)
    return me if me and item.is_mine(user_name) else " ".join((item.owner or "").split())


def _day(dt: datetime, lang: str) -> str:
    if lang == "fr":  # independent of the OS locale
        return f"{_FR_DAYS[dt.weekday()]} {dt.day} {_FR_MONTHS[dt.month - 1]} {dt.year}"
    return f"{dt:%A} {dt.day} {dt:%B %Y}"


def duration_s(meeting: Meeting) -> float:
    if meeting.segments:
        return max(s.t_end for s in meeting.segments)
    if meeting.ended_at is not None:
        return max(0.0, (meeting.ended_at - meeting.started_at).total_seconds())
    return 0.0


def _duration(seconds: float, lang: str) -> str:
    minutes = max(1, round(seconds / 60))
    if minutes < 60:
        return f"{minutes} min"
    h, m = divmod(minutes, 60)
    return (f"{h} h {m:02d}" if lang == "fr" else f"{h} h {m} min") if m else f"{h} h"


def when_line(meeting: Meeting, lang: str) -> str:
    """"Wednesday 4 November 2026, 14:00–14:42 · 42 min"."""
    start = meeting.started_at.astimezone()
    line = f"{_day(start, lang)}, {start:%H:%M}"
    if meeting.ended_at is not None:
        line += f"–{meeting.ended_at.astimezone():%H:%M}"
    seconds = duration_s(meeting)
    return f"{line} · {_duration(seconds, lang)}" if seconds > 0 else line


def meeting_title(meeting: Meeting, lang: str) -> str:
    return " ".join((meeting.title or "").split()) or f"{LABELS[lang]['meeting']} {meeting.started_at.astimezone():%Y-%m-%d %H:%M}"


def people(meeting: Meeting, user_name: str) -> list[str]:
    """Who took part: you first (once your name is known), then the others."""
    me = known_name(user_name)
    others = [p for p in meeting.participants if p.strip() and p.casefold() != me.casefold()]
    return ([me] if me else []) + others


def recap_view(meeting: Meeting, user_name: str) -> dict[str, Any]:
    """The summary as the Summary tab shows it and the PDF prints it (hidden items included, marked)."""
    lang = _lang(meeting)
    recap = meeting.recap
    hidden = set(recap.hidden) if recap else set()
    year = meeting.started_at.astimezone().year

    def row(i: Item) -> dict[str, Any]:
        return {"id": i.id, "title": " ".join(i.title.split()), "hidden": i.id in hidden}

    if recap is not None:
        overview, discussed, next_steps = recap.overview, list(recap.discussed), list(recap.next_steps)
    else:  # summarized before summaries existed, or without Claude: made from what the meeting has
        overview = meeting.tldr or ""
        discussed = [" ".join(i.title.split()) for i in _visible(meeting, ItemKind.NOTE)]
        next_steps = [v for k, v in meeting.fields.items() if k.strip().casefold() in _NEXT_STEP_FIELDS and v.strip()]
    return {
        "written": recap is not None and not recap.from_notes,
        "edited": bool(recap and recap.edited),
        "lang": lang,
        "labels": LABELS[lang],
        "title": meeting_title(meeting, lang),
        "when": when_line(meeting, lang),
        "participants": people(meeting, user_name),
        "overview": overview,
        "discussed": discussed,
        "decisions": [row(i) for i in _visible(meeting, ItemKind.DECISION)],
        "next_steps": next_steps,
        "todos": [{**row(i), "owner": owner_label(i, user_name), "due": due_label(i, lang, year),
                   "done": i.status == ItemStatus.DONE} for i in _visible(meeting, ItemKind.ACTION)],
        "questions": [row(i) for i in _visible(meeting, ItemKind.QUESTION)],
    }


def summary_pdf(meeting: Meeting, user_name: str, created: datetime | None = None) -> bytes:
    v = recap_view(meeting, user_name)
    labels = v["labels"]
    doc = Document(title=v["title"], author=known_name(user_name), subject=labels["summary"], lang=v["lang"],
                   footer=f"{v['title']} · {_day(meeting.started_at.astimezone(), v['lang'])}",
                   page_label=labels["page"], created=created)
    doc.title(v["title"], kicker=labels["summary"])
    doc.meta_line(v["when"])
    if v["participants"]:
        doc.meta_line(labels["participants"].format(names=", ".join(v["participants"])))
    doc.separator()
    shown = lambda rows: [r for r in rows if not r["hidden"]]  # noqa: E731
    sections = 0
    if v["overview"].strip():
        doc.heading(labels["overview"])
        doc.paragraph(v["overview"])
        sections += 1
    for key, points in (("discussed", v["discussed"]), ("decisions", [r["title"] for r in shown(v["decisions"])]),
                        ("next_steps", v["next_steps"])):
        if points:
            doc.heading(labels[key])
            doc.bullets(points)
            sections += 1
    todos = shown(v["todos"])
    if todos:
        doc.heading(labels["todos"])
        doc.checklist([Column(labels["task"], 0), Column(labels["owner"], 120), Column(labels["due"], 86)],
                      [(t["done"], [t["title"], t["owner"] or "—", t["due"] or "—"]) for t in todos])
        sections += 1
    questions = shown(v["questions"])
    if questions:
        doc.heading(labels["questions"])
        doc.bullets(r["title"] for r in questions)
        sections += 1
    if not sections:
        doc.paragraph(labels["empty"], after=0)
    return doc.to_bytes()


# --------------------------------------------------------------- the transcript


def speaker_names(meeting: Meeting, user_name: str, others_label: str = "Others") -> Callable[[TranscriptSegment], str]:
    """Each line's speaker as the meeting page names it (the voices' names, the one other person), and
    your microphone's lines by your name."""
    other, voices, me = other_side(meeting), voice_labels(meeting), known_name(user_name)
    others = LABELS[_lang(meeting)]["others"] if others_label == "Others" else others_label  # the default, translated

    def name(seg: TranscriptSegment) -> str:
        if seg.source == "mic":
            return me or seg.speaker
        label = speaker_label(seg, other, others_label, voices)
        return others if label == others_label else label

    return name


def transcript_lines(meeting: Meeting, user_name: str, others_label: str = "Others") -> list[tuple[str, str, str]]:
    """``(stamp, speaker, text)`` for every line, in the order they were said."""
    name = speaker_names(meeting, user_name, others_label)
    segs = sorted(meeting.segments, key=lambda s: s.t_start)
    return [(fmt_ts(s.t_start), name(s), " ".join(s.text.split())) for s in segs if s.text.strip()]


def _transcript_head(meeting: Meeting, user_name: str) -> tuple[str, list[str], dict[str, str]]:
    lang = _lang(meeting)
    labels = LABELS[lang]
    meta = [when_line(meeting, lang)]
    if names := people(meeting, user_name):
        meta.append(labels["participants"].format(names=", ".join(names)))
    return meeting_title(meeting, lang), meta, labels


def transcript_text(meeting: Meeting, user_name: str, others_label: str = "Others") -> bytes:
    title, meta, _ = _transcript_head(meeting, user_name)
    out = [title, *meta, ""]
    out += [f"[{stamp}] {speaker}: {text}" for stamp, speaker, text in transcript_lines(meeting, user_name, others_label)]
    return ("\n".join(out) + "\n").encode("utf-8")


def transcript_pdf(meeting: Meeting, user_name: str, others_label: str = "Others", created: datetime | None = None) -> bytes:
    title, meta, labels = _transcript_head(meeting, user_name)
    lang = _lang(meeting)
    doc = Document(title=f"{labels['transcript']} — {title}", author=known_name(user_name), subject=labels["transcript"],
                   lang=lang, footer=f"{title} · {_day(meeting.started_at.astimezone(), lang)}",
                   page_label=labels["page"], created=created)
    doc.title(title, kicker=labels["transcript"])
    for line in meta:
        doc.meta_line(line)
    doc.separator()
    lines = transcript_lines(meeting, user_name, others_label)
    for stamp, speaker, text in lines:
        doc.transcript_line(stamp, speaker, text)
    if not lines:
        doc.paragraph(labels["empty"], after=0)
    return doc.to_bytes()


# ------------------------------------------------------------------ the files


@dataclass(frozen=True)
class Export:
    data: bytes
    filename: str
    media_type: str

    def content_disposition(self) -> str:
        """``attachment`` with the name for every browser: plain ASCII, and the real one in UTF-8."""
        return f"attachment; filename=\"{ascii_name(self.filename)}\"; filename*=UTF-8''{quote(self.filename, safe='')}"


_UNSAFE = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]+')


def filename(meeting: Meeting, kind: str, ext: str) -> str:
    """"2026-11-04 Lancement v2 - Compte rendu.pdf": the date first, so they sort; no character a disk refuses."""
    lang = _lang(meeting)
    title = " ".join(_UNSAFE.sub(" ", meeting_title(meeting, lang)).split()).strip(". ")[:80].rstrip()
    label = LABELS[lang]["file_summary" if kind == "summary" else "file_transcript"]
    day = meeting.started_at.astimezone().strftime("%Y-%m-%d")
    return f"{day} {title} - {label}.{ext}" if title else f"{day} {label}.{ext}"


def ascii_name(name: str) -> str:
    folded = "".join(c for c in unicodedata.normalize("NFKD", name) if not unicodedata.combining(c))
    return re.sub(r"[^\x20-\x7e]", "_", folded).replace('"', "'").replace("\\", "_")


def export(meeting: Meeting, what: str, fmt: str, *, user_name: str, others_label: str = "Others",
           created: datetime | None = None) -> Export:
    """The file for ``what`` (``summary``, ``transcript``) in ``fmt`` (:data:`FORMATS`); ``ValueError``: no such file."""
    if fmt not in FORMATS.get(what, ()):
        raise ValueError(f"no {what} as {fmt}: " + ", ".join(f"{w} as {'/'.join(f)}" for w, f in FORMATS.items()))
    if what == "summary":
        data = summary_pdf(meeting, user_name, created)
    elif fmt == "pdf":
        data = transcript_pdf(meeting, user_name, others_label, created)
    else:
        data = transcript_text(meeting, user_name, others_label)
    return Export(data, filename(meeting, what, fmt), MEDIA_TYPES[fmt])
