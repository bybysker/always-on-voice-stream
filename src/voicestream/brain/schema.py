"""Wire schemas sent to Claude as strict JSON schemas.

Strict structured outputs allow at most 24 optional and 16 union-typed fields
per request, so every field here is *required* and "absent" is encoded as an
empty string / ``"none"`` / ``"unchanged"`` instead of ``null``. Converters turn
them into the richer internal models in :mod:`voicestream.brain.tracker`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..models import ItemKind, ItemStatus
from .tracker import ExtractedItem, ExtractionResult, ItemUpdate


class WireNewItem(BaseModel):
    kind: Literal["action", "decision", "question", "note"]
    title: str = Field(description="Imperative, specific, <= 12 words (e.g. 'Send revised pricing deck to Acme').")
    detail: str = Field(description="Extra context needed to act on it; '' if none.")
    owner: str = Field(description="Person responsible; the user's name for first-person commitments; 'Assistant' for requests to the AI; '' if unclear.")
    due_text: str = Field(description="Deadline as spoken ('by Friday'); '' if none.")
    due_date: str = Field(description="Deadline as YYYY-MM-DD resolved against today's date; '' if none.")
    priority: Literal["low", "medium", "high", "none"]
    confidence: float = Field(description="0-1: how clearly this was committed/decided.")
    evidence: str = Field(description="Short verbatim quote (<= 25 words) from the transcript.")
    segment_refs: list[int] = Field(description="Line numbers (#n) the item comes from.")
    delegate: bool = Field(description="True if an AI agent with web search + a file workspace could do or draft this now.")
    agent_prompt: str = Field(description="If delegate: a self-contained brief for the agent with all needed context; else ''.")
    explicit_request: bool = Field(description="True if the user directly addressed the assistant (wake phrase).")


class WireUpdate(BaseModel):
    id: str = Field(description="Existing item id, e.g. 'A3'.")
    title: str = Field(description="New title, or '' to keep.")
    detail: str = Field(description="New detail, or '' to keep.")
    owner: str = Field(description="New owner, or '' to keep.")
    due_text: str = Field(description="New deadline as spoken, or '' to keep.")
    due_date: str = Field(description="New YYYY-MM-DD deadline, or '' to keep.")
    status: Literal["unchanged", "open", "done", "cancelled"]
    dismiss: bool = Field(description="True if the item was a mistake (misheard, hypothetical, duplicate).")
    delegate: Literal["unchanged", "yes", "no"]
    agent_prompt: str = Field(description="Updated agent brief, or '' to keep.")
    segment_refs: list[int]


class WireExtraction(BaseModel):
    new_items: list[WireNewItem]
    updates: list[WireUpdate]
    meeting_title: str = Field(description="Short title for the meeting if it has become clear and none is set; else ''.")
    participants: list[str] = Field(description="Names of other participants newly mentioned; [] if none.")


class WireSummary(BaseModel):
    title: str = Field(description="Short descriptive meeting title.")
    tldr: str = Field(description="2-3 sentence summary.")
    notes_markdown: str = Field(
        description="Meeting notes in Markdown: ## Summary, ## Key points, ## Decisions, ## Open questions. "
        "Do NOT include the action-item list (it is rendered separately)."
    )
    updates: list[WireUpdate] = Field(description="Corrections to existing items after reading the whole meeting.")
    missing_items: list[WireNewItem] = Field(description="Important items the live pass missed; [] if none.")
    participants: list[str]
    language: str = Field(description="ISO-639-1 code of the language most of the meeting was spoken in, e.g. 'fr' or 'en'.")
    followup_subject: str = Field(description="Subject of a short follow-up email to the other participants; '' if pointless (e.g. solo).")
    followup_body: str = Field(
        description="Body of that email in Markdown, written in the meeting's language as the user (first person): "
        "thanks, 2-4 key points/decisions, each person's next steps with dates, a sign-off with the user's name; '' if no subject."
    )


def _opt(s: str) -> str | None:
    s = (s or "").strip()
    return s or None


def wire_new_to_item(w: WireNewItem) -> ExtractedItem:
    return ExtractedItem(
        kind=ItemKind(w.kind),
        title=w.title,
        detail=w.detail,
        owner=_opt(w.owner),
        due_text=_opt(w.due_text),
        due_date=_opt(w.due_date),
        priority=None if w.priority == "none" else w.priority,
        confidence=w.confidence,
        evidence=w.evidence,
        segment_refs=list(w.segment_refs),
        delegate=w.delegate,
        agent_prompt=_opt(w.agent_prompt),
        explicit_request=w.explicit_request,
    )


def wire_update_to_update(w: WireUpdate) -> ItemUpdate:
    return ItemUpdate(
        id=w.id,
        title=_opt(w.title),
        detail=_opt(w.detail),
        owner=_opt(w.owner),
        due_text=_opt(w.due_text),
        due_date=_opt(w.due_date),
        status=None if w.status == "unchanged" else ItemStatus(w.status),
        dismiss=w.dismiss,
        delegate=None if w.delegate == "unchanged" else (w.delegate == "yes"),
        agent_prompt=_opt(w.agent_prompt),
        segment_refs=list(w.segment_refs),
    )


def wire_to_result(w: WireExtraction) -> ExtractionResult:
    return ExtractionResult(
        new_items=[wire_new_to_item(i) for i in w.new_items],
        updates=[wire_update_to_update(u) for u in w.updates],
        meeting_title=_opt(w.meeting_title),
        participants=[p for p in w.participants if p.strip()],
    )
