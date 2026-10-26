"""End-of-meeting consolidation: one stronger-model pass over the whole meeting
that fixes/merges the live items and writes the notes."""

from __future__ import annotations

import logging
from datetime import date, datetime

from .. import known_people
from ..config import Config
from ..models import FollowUpEmail, Item, ItemKind, ItemStatus, Meeting, fmt_ts
from ..transcribe.echo import words
from .llm import LLM, LLMError, text_block
from .people import finalize, other_side, speaker_label
from .prompts import SUMMARY_SYSTEM, meeting_context, render_items, render_screen, render_segment
from .schema import WireSummary, wire_new_to_item, wire_update_to_update
from .tracker import ExtractionResult, ItemTracker

log = logging.getLogger(__name__)

MAX_SUMMARY_CHARS = 600_000  # ~150k tokens


def full_transcript(meeting: Meeting, others_label: str = "Others") -> str:
    """The whole meeting as the summary reads it. With one other person in the meeting, the meeting
    audio's lines carry their name instead of ``others_label`` (the stored transcript keeps the label)."""
    other = other_side(meeting, sure=True)
    events = [(s.t_start, render_segment(i, s, speaker_label(s, other, others_label))) for i, s in enumerate(meeting.segments)]
    events += [(n.t, render_screen(n)) for n in meeting.screen_notes]
    events.sort(key=lambda e: e[0])
    text = "\n".join(t for _, t in events)
    if len(text) > MAX_SUMMARY_CHARS:
        head = text[: MAX_SUMMARY_CHARS // 4]
        tail = text[-(MAX_SUMMARY_CHARS * 3 // 4) :]
        text = head + "\n[… middle of a very long meeting omitted …]\n" + tail
    return text


async def summarize(
    cfg: Config,
    meeting: Meeting,
    tracker: ItemTracker,
    llm: LLM | None,
    today: date | None = None,
    budget_ok: bool = True,
) -> str:
    """Consolidate items and set ``meeting.summary_markdown``. Returns the notes markdown."""
    today = today or date.today()
    if llm is not None and budget_ok and meeting.segments:
        try:
            others = cfg.capture.system.label
            other = other_side(meeting, sure=True)
            named = (f"\nThe other side of the meeting ('{others}' lines) is {other}: their lines carry that name."
                     if other else "")
            content = [
                text_block(f"<transcript>\n{full_transcript(meeting, others)}\n</transcript>", cache=True),
                text_block(
                    meeting_context(meeting, cfg.user_name, today, cfg.brain.attendees, cfg.brain.language)
                    + named
                    + f"\n\nItems captured live:\n{render_items([i for i in meeting.items.values() if not i.dismissed])}"
                ),
            ]
            wire = await llm.structured(
                model=cfg.brain.summary_model,
                system=SUMMARY_SYSTEM,
                content=content,
                schema=WireSummary,
                max_tokens=8000,
                effort=cfg.brain.summary_effort,
            )
            tracker.apply(
                ExtractionResult(
                    # found after the fact: never a live voice command that may auto-run an agent
                    new_items=[wire_new_to_item(i).model_copy(update={"explicit_request": False}) for i in wire.missing_items],
                    updates=[wire_update_to_update(u) for u in wire.updates],
                ),
                {i: s.id for i, s in enumerate(meeting.segments)},
            )
            if wire.participants:
                # the whole meeting tells who took part better than the live pass, which may have kept
                # someone who was only mentioned (and sinks like HubSpot file the notes on these people);
                # names you added, or known ones you clearly spoke to, stay (brain.people.finalize)
                known = [*cfg.brain.attendees, *known_people.known_before(cfg.data_path, meeting.id)]
                finalize(meeting, wire.participants, cfg.user_name, known)
                tracker.publish_people()
            if wire.title.strip():
                meeting.title = wire.title.strip()[:120]
            meeting.tldr = wire.tldr.strip() or None
            meeting.notes_body = wire.notes_markdown.strip() or None
            if cfg.brain.language == "auto":  # a forced language stays, whatever was spoken
                meeting.language = (wire.language or "").strip().lower()[:5] or meeting.language
            if wire.followup_subject.strip() and wire.followup_body.strip():
                meeting.followup_email = FollowUpEmail(
                    subject=wire.followup_subject.strip()[:200], body_markdown=wire.followup_body.strip()
                )
        except LLMError as exc:
            log.warning("summary failed (%s); writing basic notes", exc)
    if not meeting.language:  # no model summary (offline, over budget, failed): still French notes for a French meeting
        meeting.language = detect_language(" ".join(s.text for s in meeting.segments))
    return rerender(meeting, cfg.user_name)


def is_short_clip(cfg: Config, meeting: Meeting) -> bool:
    """Too little speech to be a meeting (``always_on.short_clip_speech_s`` / ``short_clip_lines``):
    listening all day, a line from a video or a word with a colleague opens a "meeting" too, and it
    isn't worth a summary-model call or notes in every tool."""
    ao = cfg.always_on
    return meeting.speech_seconds() < ao.short_clip_speech_s and len(meeting.segments) < ao.short_clip_lines


def known_language(cfg: Config) -> str | None:
    """The meeting language when the config fixes it (``brain.language``, else ``transcription.language``)."""
    for lang in (cfg.brain.language, cfg.transcription.language):
        if lang and lang.lower() != "auto":
            return lang.lower()[:5]
    return None


_STOPWORDS = {
    "fr": {"le", "la", "les", "un", "une", "des", "et", "est", "je", "tu", "nous", "vous", "on", "que", "qui",
           "pas", "pour", "dans", "avec", "sur", "ce", "c'est", "il", "elle", "du", "de", "au", "mais", "oui"},
    "en": {"the", "a", "an", "and", "is", "are", "i", "you", "we", "they", "that", "this", "to", "of", "in",
           "for", "with", "on", "it", "not", "be", "will", "can", "but", "yes", "so", "have", "do"},
}  # fmt: skip


def detect_language(text: str) -> str | None:
    """French or English by stop-word counts (enough to label the notes); ``None`` when unclear."""
    ws = words(text)
    counts = {lang: sum(w in stop for w in ws) for lang, stop in _STOPWORDS.items()}
    best = max(counts, key=lambda k: counts[k])
    other = min(counts.values())
    return best if counts[best] >= 3 and counts[best] > 1.5 * other else None


def rerender(meeting: Meeting, user_name: str) -> str:
    """Rebuild the notes from the stored summary parts + current items (e.g. after a late agent result)."""
    meeting.summary_markdown = render_notes(meeting, meeting.tldr or "", meeting.notes_body or "", user_name)
    return meeting.summary_markdown


_LABELS = {
    "en": {
        "meeting": "Meeting", "recorded": "recorded", "with": "with", "actions": "Action items",
        "decisions": "Decisions", "questions": "Open questions", "notes": "Notes",
        "agents": "Work done by agents during the meeting", "due": "due", "agent": "agent",
    },
    "fr": {
        "meeting": "Réunion", "recorded": "enregistrées", "with": "avec", "actions": "Actions",
        "decisions": "Décisions", "questions": "Questions ouvertes", "notes": "Notes",
        "agents": "Travail réalisé par les agents pendant la réunion", "due": "échéance", "agent": "agent",
    },
}  # fmt: skip
_FR_DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
_FR_MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre",
              "octobre", "novembre", "décembre"]  # fmt: skip


_FR_HEADINGS = {"summary": "Résumé", "key points": "Points clés", "decisions": "Décisions",
                "open questions": "Questions ouvertes", "notes": "Notes"}  # fmt: skip


def _localize_headings(body: str, lang: str) -> str:
    """The model sometimes keeps the prompt's English section names in French notes."""
    if lang != "fr":
        return body
    out = []
    for line in body.split("\n"):
        if line.startswith("#") and (name := line.lstrip("#").strip().lower()) in _FR_HEADINGS:
            line = f"{line[: len(line) - len(line.lstrip('#'))]} {_FR_HEADINGS[name]}"
        out.append(line)
    return "\n".join(out)


def _lang(meeting: Meeting) -> str:
    return "fr" if (meeting.language or "").startswith("fr") else "en"


def _long_date(dt: datetime, lang: str) -> str:
    if lang == "fr":  # independent of the OS locale
        return f"{_FR_DAYS[dt.weekday()]} {dt.day} {_FR_MONTHS[dt.month - 1]} {dt.year}, {dt:%H:%M}"
    return f"{dt:%A %d %B %Y, %H:%M}"


def render_notes(meeting: Meeting, tldr: str, body: str, user_name: str) -> str:
    lang = _lang(meeting)
    t = _LABELS[lang]
    started = meeting.started_at.astimezone()
    title = meeting.title or f"{t['meeting']} {started:%Y-%m-%d %H:%M}"
    dur = meeting.segments[-1].t_end if meeting.segments else 0.0
    lines = [f"# {title}", ""]
    meta = f"*{_long_date(started, lang)}* · {fmt_ts(dur)} {t['recorded']}"
    if meeting.participants:
        meta += f" · {t['with']} {', '.join(meeting.participants)}"
    lines += [meta, ""]
    if tldr:
        lines += [f"> {tldr}", ""]

    actions = [i for i in meeting.items_of(ItemKind.ACTION) if i.status != ItemStatus.CANCELLED]
    if actions:
        lines += [f"## {t['actions']}", ""]
        mine = [i for i in actions if i.is_mine(user_name)]
        others = [i for i in actions if not i.is_mine(user_name)]
        for it in mine + others:
            lines.append(_action_line(it, t))
        lines.append("")

    if body:
        lines += [_localize_headings(body, lang), ""]
    else:
        for kind, key in ((ItemKind.DECISION, "decisions"), (ItemKind.QUESTION, "questions"), (ItemKind.NOTE, "notes")):
            its = meeting.items_of(kind)
            if its:
                lines += [f"## {t[key]}", ""] + [f"- {i.title}" for i in its] + [""]

    agent_items = [i for i in meeting.items.values() if i.agent and i.agent.result_summary]
    if agent_items:
        lines += [f"## {t['agents']}", ""]
        for it in agent_items:
            assert it.agent is not None
            lines.append(f"### {it.id}: {it.title}")
            lines.append("")
            lines.append(it.agent.result_summary or "")
            if it.agent.result_files:
                lines.append("")
                lines += [f"- `{f}`" for f in it.agent.result_files]
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _action_line(it: Item, t: dict[str, str] = _LABELS["en"]) -> str:
    box = "x" if it.status == ItemStatus.DONE else " "
    bits = [f"- [{box}] **{it.title}**"]
    if it.owner:
        bits.append(f"— {it.owner}")
    if it.due_date or it.due_text:
        bits.append(f"· {t['due']} {it.due_date or it.due_text}")
    if it.agent and it.agent.status.value in ("done", "running", "queued"):
        bits.append(f"· 🤖 {t['agent']} {it.agent.status.value}")
    line = " ".join(bits)
    if it.detail:
        line += f"\n  {it.detail}"
    return line
