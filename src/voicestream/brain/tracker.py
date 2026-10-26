"""Applies extraction deltas to the meeting's item list (dedup, normalization, events)."""

from __future__ import annotations

import logging
import re
from datetime import date
from difflib import SequenceMatcher
from typing import Literal, get_args

from pydantic import BaseModel, Field, field_validator

from ..events import EventBus
from ..models import AgentRun, AgentStatus, Item, ItemKind, ItemStatus, Meeting, next_item_id, utcnow
from .dates import is_iso_date, resolve_due
from .people import add_person, people_event

log = logging.getLogger(__name__)


class ExtractedItem(BaseModel):
    kind: ItemKind
    title: str
    detail: str = ""
    owner: str | None = None
    due_text: str | None = None
    due_date: str | None = None
    priority: Literal["low", "medium", "high"] | None = None
    confidence: float = 0.7
    evidence: str = ""
    segment_refs: list[int] = Field(default_factory=list)
    delegate: bool = False
    agent_prompt: str | None = None
    explicit_request: bool = False

    @field_validator("priority", mode="before")
    @classmethod
    def _prio(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.strip().lower()
            return v if v in ("low", "medium", "high") else None
        return v


ClearableField = Literal["detail", "owner", "due_text", "due_date", "priority"]


class ItemUpdate(BaseModel):
    """A change to an existing item. ``None`` (and ``""``) means "keep": the model can't clear a field.
    Only a manual edit can, by naming the field in ``clear``."""

    id: str
    title: str | None = None
    detail: str | None = None
    owner: str | None = None
    due_text: str | None = None
    due_date: str | None = None
    priority: Literal["low", "medium", "high"] | None = None
    status: ItemStatus | None = None
    confidence: float | None = None
    delegate: bool | None = None
    agent_prompt: str | None = None
    dismiss: bool = False
    evidence: str | None = None
    segment_refs: list[int] = Field(default_factory=list)
    explicit_request: bool = False
    """A direct "Hey Claude" command merged into this item: its agent run counts as explicit."""
    clear: set[ClearableField] = Field(default_factory=set)

    @field_validator("priority", mode="before")
    @classmethod
    def _prio(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.strip().lower()
            return v if v in ("low", "medium", "high") else None
        return v


class ExtractionResult(BaseModel):
    new_items: list[ExtractedItem] = Field(default_factory=list)
    updates: list[ItemUpdate] = Field(default_factory=list)
    meeting_title: str | None = None
    participants: list[str] = Field(default_factory=list)


_FIRST_PERSON = {"me", "i", "myself", "self", "user", "the user"}
_UNASSIGNED = {"", "unassigned", "unknown", "none", "null", "n/a", "tbd", "someone", "anyone"}
# pronouns aren't owners either ("We should…", "On va…" open a sentence like a name would)
_UNASSIGNED |= {"we", "they", "he", "she", "it", "this", "that", "you", "everyone", "everybody", "somebody", "nobody",
                "nous", "on", "ils", "elles", "il", "elle", "vous", "tout le monde", "quelqu'un", "personne"}  # fmt: skip
_ASSISTANT = {"assistant", "claude", "ai", "the assistant", "agent"}
_CLEARABLE = set(get_args(ClearableField))  # by a manual edit; title and status can't be cleared


def _norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", t.lower()).strip()


def title_similarity(a: str, b: str) -> float:
    na, nb = _norm_title(a), _norm_title(b)
    if not na or not nb:
        return 0.0
    ratio = SequenceMatcher(None, na, nb).ratio()
    wa, wb = set(na.split()), set(nb.split())
    jacc = len(wa & wb) / max(1, len(wa | wb))
    return max(ratio, jacc)


class ItemTracker:
    def __init__(
        self,
        meeting: Meeting,
        bus: EventBus | None = None,
        user_name: str = "Me",
        today: date | None = None,
        dedup_threshold: float = 0.82,
    ) -> None:
        self.meeting = meeting
        self.bus = bus
        self.user_name = user_name
        self._today = today
        self.dedup_threshold = dedup_threshold

    @property
    def today(self) -> date:
        return self._today or date.today()

    # ----------------------------------------------------------------- normalize

    def normalize_owner(self, owner: str | None) -> str | None:
        if owner is None:
            return None
        o = owner.strip().strip("@").strip()
        low = o.lower()
        if low in _UNASSIGNED:
            return None
        if low in _FIRST_PERSON:
            return self.user_name
        if low in _ASSISTANT:
            return "Assistant"
        return o[:80]

    def normalize_due(self, due_text: str | None, due_date: str | None) -> tuple[str | None, str | None]:
        due_text = (due_text or "").strip() or None
        if due_date and not is_iso_date(due_date):
            due_date = None
        if not due_date and due_text:
            due_date = resolve_due(due_text, self.today)
        return due_text, due_date

    # --------------------------------------------------------------------- apply

    def apply(
        self, result: ExtractionResult, seg_refs: dict[int, str] | None = None
    ) -> tuple[list[Item], list[Item]]:
        """Returns (added, updated)."""
        seg_refs = seg_refs or {}
        added: list[Item] = []
        updated: list[Item] = []

        if result.meeting_title and not self.meeting.title:
            self.meeting.title = result.meeting_title.strip()[:120]
            self._publish("meeting.updated", title=self.meeting.title)
        # Claude's word on a name you addressed confirms it; a name you removed stays out (brain.people)
        if any([add_person(self.meeting, p, self.user_name, model=True) for p in result.participants]):
            self.publish_people()

        for upd in result.updates:
            item = self.meeting.items.get(upd.id.strip())
            if item is None:
                log.debug("extractor referenced unknown item %s", upd.id)
                continue
            if self._apply_update(item, upd, seg_refs):
                updated.append(item)

        for new in result.new_items:
            title = new.title.strip()
            if not title:
                continue
            dup = self._find_duplicate(new)
            if dup is not None:
                upd = ItemUpdate(
                    id=dup.id,
                    owner=new.owner if not dup.owner else None,
                    due_text=new.due_text if not (dup.due_text or dup.due_date) else None,
                    due_date=new.due_date if not dup.due_date else None,
                    detail=new.detail if len(new.detail) > len(dup.detail) else None,
                    delegate=new.delegate if new.delegate and dup.agent is None else None,
                    agent_prompt=new.agent_prompt if dup.agent is None or new.explicit_request else None,
                    segment_refs=new.segment_refs,
                    explicit_request=new.explicit_request,
                )
                if self._apply_update(dup, upd, seg_refs) and dup not in updated:
                    updated.append(dup)
                continue
            item = self._create(new, seg_refs)
            self.meeting.items[item.id] = item
            added.append(item)

        for item in added:
            self._publish("item.added", item=item.model_dump(mode="json"))
        for item in updated:
            if item not in added:
                self._publish("item.updated", item=item.model_dump(mode="json"))
        return added, updated

    def _find_duplicate(self, new: ExtractedItem) -> Item | None:
        best: tuple[float, Item | None] = (0.0, None)
        for item in self.meeting.items.values():
            if item.kind != new.kind or item.dismissed:
                continue
            score = title_similarity(item.title, new.title)
            if score > best[0]:
                best = (score, item)
        return best[1] if best[0] >= self.dedup_threshold else None

    def _create(self, new: ExtractedItem, seg_refs: dict[int, str]) -> Item:
        due_text, due_date = self.normalize_due(new.due_text, new.due_date)
        owner = self.normalize_owner(new.owner)
        if new.explicit_request and not owner:
            owner = "Assistant"
        item = Item(
            id=next_item_id(new.kind, set(self.meeting.items)),
            kind=new.kind,
            title=new.title.strip()[:200],
            detail=new.detail.strip()[:2000],
            owner=owner,
            due_text=due_text,
            due_date=due_date,
            priority=new.priority,
            confidence=max(0.0, min(1.0, new.confidence)),
            evidence=(new.evidence or "").strip()[:400] or None,
            segment_ids=[seg_refs[r] for r in new.segment_refs if r in seg_refs],
        )
        if new.kind == ItemKind.ACTION and (new.delegate or new.explicit_request):
            prompt = (new.agent_prompt or "").strip() or _default_agent_prompt(item)
            item.agent = AgentRun(prompt=prompt, explicit=new.explicit_request)
        return item

    def _apply_update(self, item: Item, upd: ItemUpdate, seg_refs: dict[int, str]) -> bool:
        changed = False

        def setf(name: str, value: object, *, allow_none: bool = False) -> None:
            nonlocal changed
            if (value is not None or allow_none) and getattr(item, name) != value:
                setattr(item, name, value)
                changed = True

        for name in upd.clear:
            setf(name, "" if name == "detail" else None, allow_none=True)
        if "due_text" in upd.clear:
            setf("due_date", None, allow_none=True)  # it was resolved from those words
        if upd.dismiss:
            if not item.dismissed:
                item.dismissed = True
                changed = True
        if upd.title and upd.title.strip():
            setf("title", upd.title.strip()[:200])
        if upd.detail is not None and upd.detail.strip():
            setf("detail", upd.detail.strip()[:2000])
        if upd.owner is not None:
            setf("owner", self.normalize_owner(upd.owner))
        if upd.due_text is not None or upd.due_date is not None:
            due_text, due_date = self.normalize_due(upd.due_text or item.due_text, upd.due_date)
            # new words replace the date too, even when they don't resolve: sinks show due_date first
            new_words = due_text is not None and due_text != item.due_text
            setf("due_text", due_text)
            setf("due_date", due_date, allow_none=new_words)
        setf("priority", upd.priority)
        setf("status", upd.status)
        if upd.confidence is not None:
            setf("confidence", max(0.0, min(1.0, upd.confidence)))
        if upd.evidence and not item.evidence:
            setf("evidence", upd.evidence.strip()[:400])
        for r in upd.segment_refs:
            sid = seg_refs.get(r)
            if sid and sid not in item.segment_ids:
                item.segment_ids.append(sid)
        if item.kind == ItemKind.ACTION:
            if (upd.delegate or upd.explicit_request) and item.agent is None:
                prompt = (upd.agent_prompt or "").strip() or _default_agent_prompt(item)
                item.agent = AgentRun(prompt=prompt, explicit=upd.explicit_request)
                changed = True
            elif upd.explicit_request and item.agent is not None and item.agent.status == AgentStatus.SUGGESTED:
                # asked for directly now, so agents.mode = "commands" may auto-run it
                prompt = (upd.agent_prompt or "").strip() or item.agent.prompt
                if not item.agent.explicit or prompt != item.agent.prompt:
                    item.agent.explicit, item.agent.prompt = True, prompt
                    changed = True
            elif (
                upd.agent_prompt
                and item.agent is not None
                and item.agent.status == AgentStatus.SUGGESTED
                and upd.agent_prompt.strip() != item.agent.prompt
            ):
                item.agent.prompt = upd.agent_prompt.strip()
                changed = True
            elif upd.delegate is False and item.agent and item.agent.status == AgentStatus.SUGGESTED:
                item.agent = None
                changed = True
        if changed:
            item.revision += 1
            item.updated_at = utcnow()
        return changed

    # ------------------------------------------------------------------ manual

    def set_dismissed(self, item_id: str, dismissed: bool = True) -> Item | None:
        item = self.meeting.items.get(item_id)
        if item is None:
            return None
        if item.dismissed != dismissed:
            item.dismissed = dismissed
            item.revision += 1
            item.updated_at = utcnow()
            self._publish("item.updated", item=item.model_dump(mode="json"))
        return item

    def add_manual(self, kind: ItemKind, title: str, owner: str | None = None, due_text: str | None = None) -> Item:
        """Add an item by hand; a title matching an existing item is merged into it and returns it."""
        if not title.strip():
            raise ValueError("an item needs a title")
        new = ExtractedItem(kind=kind, title=title, owner=owner, due_text=due_text, confidence=1.0)
        dup = self._find_duplicate(new)
        added, _ = self.apply(ExtractionResult(new_items=[new]))
        if added:
            return added[0]
        assert dup is not None
        return dup

    def edit(self, item_id: str, **fields: object) -> Item | None:
        """Manual edit from the dashboard/CLI (title, detail, owner, due_text, due_date, status, priority).

        Unlike a model update, an empty value (``None``/``""``, or an owner like "unassigned") clears the
        field: that is what the dashboard sends when you empty an input. Title and status can't be cleared.
        """
        item = self.meeting.items.get(item_id)
        if item is None:
            return None
        allowed = {"title", "detail", "owner", "due_text", "due_date", "status", "priority"}
        given = {k: v for k, v in fields.items() if k in allowed}

        def emptied(k: str, v: object) -> bool:
            if v is None:
                return True
            return isinstance(v, str) and (not v.strip() or (k == "owner" and self.normalize_owner(v) is None))

        clear = {k for k, v in given.items() if k in _CLEARABLE and emptied(k, v)}
        upd = ItemUpdate(id=item_id, clear=clear, **{k: v for k, v in given.items() if k not in clear})
        if self._apply_update(item, upd, {}):
            self._publish("item.updated", item=item.model_dump(mode="json"))
        return item

    def _publish(self, type: str, **data: object) -> None:
        if self.bus is not None:
            self.bus.publish(type, meeting_id=self.meeting.id, **data)

    def publish_people(self) -> None:
        """``meeting.people``: who the meeting is with changed (the windows' "With …" line)."""
        if self.bus is not None:
            self.bus.publish("meeting.people", **people_event(self.meeting))


def _default_agent_prompt(item: Item) -> str:
    parts = [f"Task from a meeting: {item.title}."]
    if item.detail:
        parts.append(item.detail)
    if item.evidence:
        parts.append(f'What was said: "{item.evidence}"')
    return " ".join(parts)
