"""Notes templates and AI fields: what the end-of-meeting notes are made of.

A **template** is the shape of a meeting's notes: its sections ("Pain points", "Objections"…) and the
**fields** it fills in (short answers such as "Budget" or "Next step"). A sales call and a one-on-one
don't need the same notes, so the wrap-up either uses the template you chose (``brain.notes_template``,
or the one you picked on the meeting's page) or, with ``"auto"``, lets Claude pick the one that fits
the meeting it just read. It is the same single summary call either way: nothing extra is billed.

**Fields** (``brain.fields``) are questions every meeting answers, on top of its template's own: a
line each, ``"Budget"`` or ``"Budget: what budget, and in which currency, did they mention?"`` (a
name, then what to look for). The answers are a few words each, ``""`` when the meeting didn't say,
and they lead the notes (so they reach the Google Doc, Notion and the HubSpot meeting log too).

Your own templates go in the settings file, and one with a built-in's id replaces it::

    [brain.templates.board]
    name = "Board meeting"
    when = "a board or investor meeting"
    sections = ["Summary", "Metrics", "Asks", "Decisions"]
    fields = ["Runway", "Next board date"]
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import BrainConfig

AUTO = "auto"
"""``brain.notes_template``: Claude picks the template that fits each meeting."""
GENERAL = "general"
"""The template a meeting falls back on: the notes Voicestream always wrote."""
MAX_FIELDS = 20
"""At most this many fields per meeting (the template's and yours together)."""


@dataclass(frozen=True)
class Field:
    name: str
    question: str = ""
    """What to look for; ``""``: the name says it."""

    def prompt(self) -> str:
        return f"{self.name}: {self.question}" if self.question else self.name


@dataclass(frozen=True)
class Template:
    id: str
    name: str
    when: str
    """When it fits, for Claude choosing one (``auto``) and for you in Settings."""
    sections: tuple[str, ...]
    fields: tuple[Field, ...] = ()
    builtin: bool = True

    def prompt(self) -> str:
        line = f"- {self.id} ({self.name}): {self.when} — sections: {'; '.join(self.sections)}"
        if self.fields:
            line += f" — fields: {'; '.join(f.prompt() for f in self.fields)}"
        return line

    def view(self) -> dict[str, object]:
        """For the app (``GET /api/settings``)."""
        return {"id": self.id, "name": self.name, "when": self.when, "sections": list(self.sections),
                "fields": [f.name for f in self.fields], "builtin": self.builtin}


def parse_field(line: str) -> Field | None:
    """``"Budget: what budget did they mention?"`` → name and question; ``"Budget"`` → just a name."""
    name, _, question = line.strip().partition(":")
    name, question = " ".join(name.split()), " ".join(question.split())
    return Field(name[:60], question[:300]) if name else None


BUILTIN: tuple[Template, ...] = (
    Template(
        GENERAL, "General", "any meeting the others don't fit: a sync, a discussion, a call",
        ("Summary", "Key points", "Decisions", "Open questions"),
    ),
    Template(
        "sales", "Sales call",
        "a call with a prospect or a customer about buying, renewing or expanding: discovery, demo, negotiation",
        ("Summary", "Customer context", "Pain points", "Current solution", "Decision process and timeline",
         "Objections and concerns", "Competitors"),
        (Field("Budget", "the budget or price range they mentioned, with the currency"),
         Field("Decision maker", "who decides or signs, and who else is involved"),
         Field("Timeline", "when they want to decide or start"),
         Field("Competitors", "other vendors or solutions they are considering"),
         Field("Next step", "the next step both sides agreed on, with its date")),
    ),
    Template(
        "interview", "Customer interview",
        "user research or discovery with a user or customer, to learn rather than to sell",
        ("Summary", "Who they are", "Goals", "Pain points", "Current workarounds", "Requests", "Notable quotes",
         "Insights"),
        (Field("Main pain point", "the problem that matters most to them, in their words"),
         Field("Top request", "what they most want changed or added")),
    ),
    Template(
        "one_on_one", "One-on-one",
        "a one-to-one between a manager and a team member, or a mentoring or coaching conversation",
        ("Summary", "Wins", "Challenges and blockers", "Feedback", "Goals and growth", "Open questions"),
    ),
    Template(
        "project", "Team or project meeting",
        "a stand-up, sprint review, project review or team sync about work in progress",
        ("Summary", "Progress", "Blockers", "Risks", "Decisions", "Open questions"),
        (Field("Status", "on track, at risk or late, and why, as the team said it"),),
    ),
    Template(
        "recruiting", "Job interview",
        "interviewing a candidate for a job",
        ("Summary", "Background and experience", "Strengths", "Concerns", "Motivation", "Practical details"),
        (Field("Role", "the position discussed"),
         Field("Salary expectations", "what the candidate asked for"),
         Field("Availability", "when they could start, notice period")),
    ),
)


def all_templates(brain: BrainConfig) -> dict[str, Template]:
    """The built-in templates, then yours (``brain.templates``; one with a built-in's id replaces it)."""
    out = {t.id: t for t in BUILTIN}
    for tid, custom in brain.templates.items():
        out[tid] = Template(
            tid, custom.name, custom.when or custom.name, tuple(custom.sections),
            tuple(f for line in custom.fields if (f := parse_field(line))), builtin=False,
        )
    return out


def global_fields(brain: BrainConfig) -> list[Field]:
    return [f for line in brain.fields if (f := parse_field(line))]


def fields_for(template: Template | None, brain: BrainConfig) -> list[Field]:
    """The template's fields, then yours (a name only once, case aside), at most :data:`MAX_FIELDS`."""
    seen: set[str] = set()
    out: list[Field] = []
    for f in [*(template.fields if template else ()), *global_fields(brain)]:
        if f.name.casefold() not in seen:
            seen.add(f.name.casefold())
            out.append(f)
    return out[:MAX_FIELDS]


def choose(brain: BrainConfig, choice: str | None) -> tuple[Template | None, dict[str, Template]]:
    """The template to write the notes with: ``choice`` (picked for this meeting), else the setting.
    ``None`` means Claude picks among the second value (``auto``, or a template that no longer exists)."""
    templates = all_templates(brain)
    pick = choice or brain.notes_template
    return templates.get(pick), templates


def prompt_block(template: Template | None, templates: dict[str, Template], brain: BrainConfig) -> str:
    """The wrap-up's instructions for the notes' shape (after the cache breakpoint: they change)."""
    yours = global_fields(brain)
    if template is not None:
        text = f"Notes template: {template.id} ({template.name}) — sections: {'; '.join(template.sections)}"
        fields = fields_for(template, brain)
        if fields:
            text += "\nFields to fill in:\n" + "\n".join(f"- {f.prompt()}" for f in fields)
        return text
    text = ("Notes templates — pick the one that fits this meeting best (its id in `template`; "
            f"'{GENERAL}' when none fits) and use its sections and fields:\n")
    text += "\n".join(t.prompt() for t in templates.values())
    if yours:
        text += "\nFields to fill in for every meeting, whatever the template:\n" + "\n".join(f"- {f.prompt()}" for f in yours)
    return text
