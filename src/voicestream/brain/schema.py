"""Wire schemas sent to Claude as strict JSON schemas.

Strict structured outputs allow at most 24 optional and 16 union-typed fields
per request, so every field here is *required* and "absent" is encoded as an
empty string / ``"none"`` / ``"unchanged"`` instead of ``null``. Converters turn
them into the richer internal models in :mod:`voicestream.brain.tracker`.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Literal

from pydantic import BaseModel, Field

from ..models import Classification, ClientContact, ItemKind, ItemStatus
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
    participants: list[str] = Field(
        description="Names of people newly found to be taking part in this meeting (speaking, or addressed as "
        "present); never people who are only talked about; [] if none."
    )


class WireSpeaker(BaseModel):
    speaker: str = Field(description="A voice label exactly as in the transcript, e.g. 'Speaker 2'.")
    name: str = Field(description="That person's name as said in the meeting.")


class WireContact(BaseModel):
    name: str = Field(description="Their name as said in the meeting.")
    email: str = Field(description="Their e-mail address only if it was said or shown in full; else ''.")
    role: str = Field(description="Their job or role at the organization if said; else ''.")


class WireClassification(BaseModel):
    kind: Literal["client", "prospect", "partner", "internal", "other"]
    confidence: float = Field(description="0-1: how clearly the transcript shows this kind.")
    company: str = Field(description="The client's or prospect's organization as named in the meeting; '' if not named or not a client/prospect meeting.")
    contacts: list[WireContact] = Field(description="People from that organization who took part; [] if none or not a client/prospect meeting.")
    step: Literal["none", "first_call", "discovery", "proposal", "won", "lost"]
    proposal_item: str = Field(description="Id of the to-do (e.g. 'A2') that sends the proposal or quote to them; '' if none.")
    followup_items: list[str] = Field(description="Ids of to-dos that only chase them for an answer on a proposal (a reminder, a follow-up call); [] if none.")
    reason: str = Field(description="One short sentence saying why this kind, in the notes' language.")
class WireField(BaseModel):
    name: str = Field(description="A field's name exactly as listed in the context, e.g. 'Budget'.")
    value: str = Field(description="The meeting's answer in a few words (<= 30), names and numbers as said; '' if not discussed.")


class WireTodoPlan(BaseModel):
    item: str = Field(description="The to-do: its id from the items captured live (e.g. 'A2'), or the exact title of one of your missing_items.")
    who: Literal["ai", "both", "you"]
    prompt: str = Field(description="For 'ai' and 'both': a self-contained prompt for an AI assistant, in the meeting's language; '' for 'you'.")
    you_do: str = Field(description="For 'both' and 'you': what only the user can do, one short sentence; '' for 'ai'.")


class WireSummary(BaseModel):
    title: str = Field(description="Short descriptive meeting title.")
    tldr: str = Field(description="2-3 sentence summary.")
    template: str = Field(description="Id of the notes template the notes follow (the one given, or the one picked).")
    notes_markdown: str = Field(
        description="Meeting notes in Markdown, one ## section per section of the notes template (omit empty ones). "
        "Do NOT include the action-item list (it is rendered separately), nor the fields."
    )
    fields: list[WireField] = Field(description="One entry per field listed in the context for the template used; [] if none are listed.")
    updates: list[WireUpdate] = Field(description="Corrections to existing items after reading the whole meeting.")
    missing_items: list[WireNewItem] = Field(description="Important items the live pass missed; [] if none.")
    participants: list[str] = Field(
        description="Everyone who took part (spoke, or was addressed as present), the user excepted; never people "
        "who were only talked about."
    )
    speakers: list[WireSpeaker] = Field(
        description="Who each 'Speaker N' of the transcript is, only when the transcript makes it clear (they introduce "
        "themselves, or answer right after being addressed by name); [] if none or no 'Speaker N' lines."
    )
    language: str = Field(description="ISO-639-1 code of the language most of the meeting was spoken in, e.g. 'fr' or 'en'.")
    followup_subject: str = Field(description="Subject of a short follow-up email to the other participants; '' if pointless (e.g. solo).")
    followup_body: str = Field(
        description="Body of that email in Markdown, written in the meeting's language as the user (first person): "
        "thanks, 2-4 key points/decisions, each person's next steps with dates, a sign-off with the user's name; '' if no subject."
    )
    classification: WireClassification = Field(description="What kind of meeting this was, for the user's CRM.")
    todo_plans: list[WireTodoPlan] = Field(description="One per open to-do of the user's: what an AI can do of it and what the user does; [] if none.")
    recap_overview: str = Field(
        description="For the summary sent to the participants: 2-3 sentences on what the meeting was for and where "
        "things stand, written for them; '' if nothing was discussed."
    )
    recap_discussed: list[str] = Field(
        description="For that summary: the topics discussed, one plain-text point each (<= 25 words), in the order "
        "they came up; [] if none."
    )
    recap_next_steps: list[str] = Field(
        description="For that summary: what happens next as agreed (milestones, meetings, decisions to come, with dates "
        "as said), not the individual to-dos; [] if none."
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


def wire_to_classification(w: WireClassification, action_ids: Collection[str]) -> Classification:
    """Claude's answer, held to its rules: only a client or prospect meeting has a company, contacts, a sales
    step and to-dos of the deal, and those to-dos are action items the meeting has (``action_ids``)."""
    business = w.kind in ("client", "prospect")
    ids = set(action_ids) if business else set()
    contacts = [
        ClientContact(name=" ".join(c.name.split())[:120], email=c.email.strip()[:160], role=" ".join(c.role.split())[:120])
        for c in (w.contacts if business else [])
        if c.name.strip()
    ]
    proposal = w.proposal_item.strip()
    return Classification(
        kind=w.kind,
        confidence=min(1.0, max(0.0, w.confidence)),
        company=" ".join(w.company.split())[:160] if business else "",
        contacts=contacts,
        step=w.step if business else "none",
        proposal_item=proposal if proposal in ids else "",
        followup_items=[i for i in dict.fromkeys(x.strip() for x in w.followup_items) if i in ids and i != proposal],
        reason=" ".join((w.reason or "").split())[:300],
    )


def wire_to_result(w: WireExtraction) -> ExtractionResult:
    return ExtractionResult(
        new_items=[wire_new_to_item(i) for i in w.new_items],
        updates=[wire_update_to_update(u) for u in w.updates],
        meeting_title=_opt(w.meeting_title),
        participants=[p for p in w.participants if p.strip()],
    )
