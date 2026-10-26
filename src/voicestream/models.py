"""Core data types shared by every stage of the pipeline.

Audio flows as int16 mono PCM at ``SAMPLE_RATE`` between sources, the VAD and
transcribers. Everything downstream of transcription is plain pydantic models
so it can be persisted to JSON and streamed to the dashboard unchanged.
"""

from __future__ import annotations

import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, Field

SAMPLE_RATE = 16_000
"""Internal sample rate (Hz). Sources resample to this; backends resample from it."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:10]}"


# --------------------------------------------------------------------------- audio


@dataclass(slots=True)
class AudioFrame:
    """A block of captured audio from one source.

    ``t_start`` is seconds since the meeting clock started (see ``Clock``), so
    frames from different sources can be aligned in time.
    """

    source: str
    samples: np.ndarray  # int16, mono, SAMPLE_RATE
    t_start: float

    @property
    def duration(self) -> float:
        return len(self.samples) / SAMPLE_RATE

    @property
    def t_end(self) -> float:
        return self.t_start + self.duration


@dataclass(slots=True)
class Utterance:
    """A contiguous stretch of speech cut out by the VAD."""

    source: str
    samples: np.ndarray  # int16, mono, SAMPLE_RATE
    t_start: float
    t_end: float
    speech_seconds: float = 0.0

    @property
    def duration(self) -> float:
        return len(self.samples) / SAMPLE_RATE


# ---------------------------------------------------------------------- transcript


class TranscriptSegment(BaseModel):
    id: str = Field(default_factory=lambda: new_id("s_"))
    source: str
    """Capture source name, e.g. ``mic`` or ``system``."""
    speaker: str
    """Display label: ``Me`` for the mic, ``Others`` for system audio, or a diarized name."""
    text: str
    t_start: float
    t_end: float
    wall_time: datetime = Field(default_factory=utcnow)
    backend: str = ""
    confidence: float | None = None

    def render(self) -> str:
        return f"[{fmt_ts(self.t_start)}] {self.speaker}: {self.text}"


class ScreenNote(BaseModel):
    id: str = Field(default_factory=lambda: new_id("scr_"))
    t: float
    wall_time: datetime = Field(default_factory=utcnow)
    description: str
    image_path: str | None = None


# --------------------------------------------------------------------------- items


class ItemKind(str, Enum):
    ACTION = "action"
    DECISION = "decision"
    QUESTION = "question"
    NOTE = "note"


class ItemStatus(str, Enum):
    OPEN = "open"
    DONE = "done"
    CANCELLED = "cancelled"


class AgentStatus(str, Enum):
    SUGGESTED = "suggested"  # extractor thinks an agent could do this; waiting for approval
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    REJECTED = "rejected"


class AgentRun(BaseModel):
    prompt: str
    rationale: str = ""
    status: AgentStatus = AgentStatus.SUGGESTED
    explicit: bool = False
    """True when the user directly asked the assistant (wake phrase) for this."""
    started_at: datetime | None = None
    finished_at: datetime | None = None
    workspace: str | None = None
    result_summary: str | None = None
    result_files: list[str] = Field(default_factory=list)
    cost_usd: float = 0.0
    error: str | None = None


class Item(BaseModel):
    """Anything worth remembering from a meeting: an action item, decision, open
    question or key note. Action items are what get pushed to task tools."""

    id: str
    kind: ItemKind
    title: str
    detail: str = ""
    owner: str | None = None
    due_text: str | None = None
    due_date: str | None = None  # ISO yyyy-mm-dd when resolvable
    priority: Literal["low", "medium", "high"] | None = None
    status: ItemStatus = ItemStatus.OPEN
    confidence: float = 0.8
    evidence: str | None = None
    """Short verbatim quote from the transcript that supports the item."""
    segment_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    revision: int = 1
    dismissed: bool = False
    agent: AgentRun | None = None
    pushed: dict[str, str] = Field(default_factory=dict)
    """Sink name -> external reference (URL or id) once pushed."""
    pushed_revision: dict[str, int] = Field(default_factory=dict)
    """Sink name -> item revision that was last pushed (to detect updates)."""

    def is_mine(self, user_name: str) -> bool:
        owner, me = _name_key(self.owner or ""), _name_key(user_name)
        if not owner:  # blank owners (hand-edited or imported meetings) are nobody's
            return False
        if owner in {"me", "i", "myself", "moi"}:
            return True
        if not me:
            return False
        # "Alex" is you when you're "Alex Martin"; "Alex Dupont" (a client) is not you when you're "Alex"
        return owner == me or (" " not in owner and owner == me.split()[0])

    def one_line(self) -> str:
        bits = [self.title]
        if self.owner:
            bits.append(f"@{self.owner}")
        if self.due_date or self.due_text:
            bits.append(f"due {self.due_date or self.due_text}")
        return " — ".join(bits)


def _name_key(name: str) -> str:
    """Case- and accent-insensitive form of a person's name ("Élodie " == "elodie")."""
    folded = unicodedata.normalize("NFKD", name.strip().lower())
    return " ".join("".join(c for c in folded if not unicodedata.combining(c)).split())


def next_item_id(kind: ItemKind, existing: set[str]) -> str:
    prefix = {"action": "A", "decision": "D", "question": "Q", "note": "N"}[kind.value]
    n = 1
    while f"{prefix}{n}" in existing:
        n += 1
    return f"{prefix}{n}"


# ------------------------------------------------------------------------- meeting


class FollowUpEmail(BaseModel):
    subject: str
    body_markdown: str
    to: list[str] = Field(default_factory=list)
    """Recipient e-mail addresses when known (otherwise left for you to fill in)."""


class PersonInfo(BaseModel):
    """How a name came to be among a meeting's :attr:`Meeting.participants` (see :mod:`.brain.people`).

    A name with none of these set — or with no entry at all, as in meetings saved before this
    existed — came from Claude."""

    heard: bool = False
    """You addressed them by name ("Sam, can you…", "Merci Julie")."""
    strong: bool = False
    """…in a way that leaves little doubt (a greeting, a question put to them): kept even when
    Claude's final notes don't list them."""
    model: bool = False
    """Claude listed them."""
    you: bool = False
    """You added them by hand."""

    def confirmed(self) -> bool:
        """Claude or you agree: not just a guess from how you spoke."""
        return self.model or self.you or not self.heard


class Meeting(BaseModel):
    # microseconds: back-to-back meetings (always-on mode) can start within the same second, and
    # sinks key their refs and idempotency ids on this
    id: str = Field(default_factory=lambda: utcnow().strftime("%Y%m%d-%H%M%S-%f"))
    title: str | None = None
    started_at: datetime = Field(default_factory=utcnow)
    ended_at: datetime | None = None
    participants: list[str] = Field(default_factory=list)
    """The people you met (you excepted): what the notes, the follow-up e-mail and your tools use."""
    people: dict[str, PersonInfo] = Field(default_factory=dict)
    """Participant name -> where it came from. The transcript keeps its raw speaker labels ("Others");
    the windows and notes put a name on them from here."""
    people_removed: list[str] = Field(default_factory=list)
    """Names you took out of :attr:`participants`: never added back to this meeting, by hearing them or by Claude."""
    segments: list[TranscriptSegment] = Field(default_factory=list)
    screen_notes: list[ScreenNote] = Field(default_factory=list)
    items: dict[str, Item] = Field(default_factory=dict)
    language: str | None = None
    """ISO-639-1 code of the meeting's main language (e.g. ``fr``), detected at wrap-up."""
    tldr: str | None = None
    notes_body: str | None = None
    """Model-written notes sections (the full notes are re-rendered from these + items)."""
    followup_email: FollowUpEmail | None = None
    """Ready-to-send recap for the other participants (sinks may create it as a draft)."""
    summary_markdown: str | None = None
    notes_ref: dict[str, str] = Field(default_factory=dict)
    short_clip: bool = False
    """Too little speech to be a meeting (see ``always_on.short_clip_speech_s``): kept, not summarized or sent."""
    interrupted: bool = False
    """Its recording was cut off (a crash, a force quit, a power cut) before the wrap-up: ended afterwards
    at the last words heard, with basic notes and nothing sent (see ``app.meetings.finish_interrupted``)."""

    def items_of(self, kind: ItemKind, include_dismissed: bool = False) -> list[Item]:
        return [
            i
            for i in self.items.values()
            if i.kind == kind and (include_dismissed or not i.dismissed)
        ]

    def transcript_text(self, since: float | None = None) -> str:
        return "\n".join(s.render() for s in self.segments if since is None or s.t_end >= since)

    def speech_seconds(self) -> float:
        return sum(max(0.0, s.t_end - s.t_start) for s in self.segments)


# -------------------------------------------------------------------------- events


class Event(BaseModel):
    """Envelope published on the in-process bus and streamed to the dashboard."""

    type: str
    data: dict[str, Any] = Field(default_factory=dict)
    ts: datetime = Field(default_factory=utcnow)


def fmt_ts(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
