"""End-of-meeting consolidation: one stronger-model pass over the whole meeting
that fixes/merges the live items and writes the notes."""

from __future__ import annotations

import logging
from datetime import date, datetime

from ..config import Config
from ..models import FollowUpEmail, Item, ItemKind, ItemStatus, Meeting, fmt_ts
from .llm import LLM, LLMError, text_block
from .prompts import SUMMARY_SYSTEM, meeting_context, render_items, render_screen, render_segment
from .schema import WireSummary, wire_new_to_item, wire_update_to_update
from .tracker import ExtractionResult, ItemTracker

log = logging.getLogger(__name__)

MAX_SUMMARY_CHARS = 600_000  # ~150k tokens


def full_transcript(meeting: Meeting) -> str:
    events = [(s.t_start, render_segment(i, s)) for i, s in enumerate(meeting.segments)]
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
            content = [
                text_block(f"<transcript>\n{full_transcript(meeting)}\n</transcript>", cache=True),
                text_block(
                    meeting_context(meeting, cfg.user_name, today, cfg.brain.attendees, cfg.brain.language)
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
                    participants=wire.participants,
                ),
                {i: s.id for i, s in enumerate(meeting.segments)},
            )
            if wire.title.strip():
                meeting.title = wire.title.strip()[:120]
            meeting.tldr = wire.tldr.strip() or None
            meeting.notes_body = wire.notes_markdown.strip() or None
            meeting.language = (wire.language or "").strip().lower()[:5] or meeting.language
            if wire.followup_subject.strip() and wire.followup_body.strip():
                meeting.followup_email = FollowUpEmail(
                    subject=wire.followup_subject.strip()[:200], body_markdown=wire.followup_body.strip()
                )
        except LLMError as exc:
            log.warning("summary failed (%s); writing basic notes", exc)
    return rerender(meeting, cfg.user_name)


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
        lines += [body, ""]
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
