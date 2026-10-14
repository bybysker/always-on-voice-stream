"""Core data types shared by every stage of the pipeline.

Audio flows as int16 mono PCM at ``SAMPLE_RATE`` between sources, the VAD and
transcribers. Everything downstream of transcription is plain pydantic models
so it can be persisted to JSON and streamed to the dashboard unchanged.
"""

from __future__ import annotations

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
        if not self.owner:
            return False
        owner = self.owner.strip().lower()
        return owner in {"me", "i", "myself", user_name.strip().lower()} or (
            bool(user_name) and owner.split()[0] == user_name.strip().lower().split()[0]
        )

    def one_line(self) -> str:
        bits = [self.title]
        if self.owner:
            bits.append(f"@{self.owner}")
        if self.due_date or self.due_text:
            bits.append(f"due {self.due_date or self.due_text}")
        return " — ".join(bits)


def next_item_id(kind: ItemKind, existing: set[str]) -> str:
    prefix = {"action": "A", "decision": "D", "question": "Q", "note": "N"}[kind.value]
    n = 1
    while f"{prefix}{n}" in existing:
        n += 1
    return f"{prefix}{n}"


# ------------------------------------------------------------------------- meeting


class Meeting(BaseModel):
    id: str = Field(default_factory=lambda: utcnow().strftime("%Y%m%d-%H%M%S"))
    title: str | None = None
    started_at: datetime = Field(default_factory=utcnow)
    ended_at: datetime | None = None
    participants: list[str] = Field(default_factory=list)
    segments: list[TranscriptSegment] = Field(default_factory=list)
    screen_notes: list[ScreenNote] = Field(default_factory=list)
    items: dict[str, Item] = Field(default_factory=dict)
    tldr: str | None = None
    notes_body: str | None = None
    """Model-written notes sections (the full notes are re-rendered from these + items)."""
    summary_markdown: str | None = None
    notes_ref: dict[str, str] = Field(default_factory=dict)

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
