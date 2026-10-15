"""monday.com sink: one board item per action item, kept in sync as it's refined.

monday's GraphQL API takes the raw token in ``Authorization`` (no ``Bearer``)
and should always get an explicit ``API-Version``: without one you silently get
whichever version is *Current*, and that rolls over every quarter.
``column_values`` is a JSON-encoded *string* keyed by board-specific column ids,
so every column mapping is opt-in (``voicestream doctor`` lists the board's
columns when one is wrong). With none configured you still get the item name,
plus the full context as an update: monday's comment thread, which renders HTML.

Errors usually arrive as HTTP 200 with an ``errors`` array. Rate-limit and
complexity-budget errors are raised as retryable :class:`SinkError`.

Ref format: ``<item url>#<item id>``. The fragment is harmless in a browser and
keeps the id at hand for updates.
"""

from __future__ import annotations

import html
import json
import logging
import re
import uuid
from typing import Annotated, Any, ClassVar

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from ..models import Item, ItemStatus, Meeting
from .base import Sink, SinkConfig, SinkError, env_secret, item_markdown
from .http import RETRYABLE, HttpSinkMixin

log = logging.getLogger(__name__)

API_URL = "https://api.monday.com/v2"
NAME_MAX = 255
LONG_TEXT_MAX = 2000
UPDATE_CHUNK = 6000
"""monday documents no size limit for update bodies, so long text is split into
an update plus threaded replies to keep each one comfortably small."""

RATE_LIMIT_CODES = {
    "COMPLEXITY_BUDGET_EXHAUSTED",
    "ComplexityException",
    "IP_RATE_LIMIT_EXCEEDED",
    "maxConcurrencyExceeded",
    "RATE_LIMIT_EXCEEDED",
    "DAILY_LIMIT_EXCEEDED",
}

CREATE_ITEM = """mutation ($board: ID!, $group: String, $name: String!, $columns: JSON, $labels: Boolean) {
  create_item(board_id: $board, group_id: $group, item_name: $name, column_values: $columns, create_labels_if_missing: $labels) { id url }
}"""
CREATE_UPDATE = """mutation ($item: ID!, $parent: ID, $body: String!) {
  create_update(item_id: $item, parent_id: $parent, body: $body) { id }
}"""
CHANGE_COLUMNS = """mutation ($board: ID!, $item: ID!, $columns: JSON!, $labels: Boolean) {
  change_multiple_column_values(board_id: $board, item_id: $item, column_values: $columns, create_labels_if_missing: $labels) { id }
}"""
CHECK = """query ($board: ID!) {
  me { name }
  boards(ids: [$board]) { name groups { id } columns { id title type } }
}"""


class _Labels(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StatusLabels(_Labels):
    """Defaults are the labels of a fresh monday status column."""

    open: str = "Working on it"
    done: str = "Done"
    cancelled: str = "Stuck"


class PriorityLabels(_Labels):
    low: str = "Low"
    medium: str = "Medium"
    high: str = "High"


class MondayConfig(SinkConfig):
    token_env: str = "MONDAY_TOKEN"
    api_version: str = "2026-10"
    """Pinned on purpose; bump it when monday retires this version."""
    board_id: Annotated[str, BeforeValidator(str)]
    group_id: str | None = None
    """Group for new items (default: the board's first group)."""
    notes_group_id: str | None = None
    """If set, meeting notes become an item in this group, with the notes as its update."""
    status_column: str | None = None
    """A *status* column id, e.g. ``status``."""
    status_labels: StatusLabels = Field(default_factory=StatusLabels)
    date_column: str | None = None
    """A *date* column for the due date."""
    owner_column: str | None = None
    """A *people* column, filled only for owners listed in ``owner_ids``."""
    owner_ids: dict[str, int] = Field(default_factory=dict)
    """Owner name as spoken (case-insensitive) → monday user id. Your own items
    also match your ``user_name`` or ``me``."""
    owner_text_column: str | None = None
    """A *text* column that gets the owner's name (works for anyone)."""
    priority_column: str | None = None
    """A *status* column with ``priority_labels``."""
    priority_labels: PriorityLabels = Field(default_factory=PriorityLabels)
    details_column: str | None = None
    """A *long text* column for the item's detail. The full context is posted as an update either way."""
    create_labels: bool = True
    """Create missing status labels (needs permission to change the board's structure)."""


_EXPECTED_TYPES = {
    "status_column": "status",
    "priority_column": "status",
    "date_column": "date",
    "owner_column": "people",
    "owner_text_column": "text",
    "details_column": "long_text",
}


class MondaySink(HttpSinkMixin, Sink):
    name = "monday"
    Config: ClassVar[type[SinkConfig]] = MondayConfig
    supports_update = True
    supports_notes = True
    cfg: MondayConfig

    # ---------------------------------------------------------------- transport

    async def _gql(self, query: str, variables: dict[str, Any], *, create: bool = False) -> dict[str, Any]:
        headers = {"Authorization": env_secret(self.cfg, "token_env", "MONDAY_TOKEN"), "API-Version": self.cfg.api_version}
        if create:  # monday replays the first response for a repeated key instead of creating twice
            headers["Idempotency-Key"] = str(uuid.uuid4())
        resp = await self.request(
            "POST",
            API_URL,
            json={"query": query, "variables": variables},
            headers=headers,
            ok=(200, 400),  # 400 = bad query/values; its body carries the useful error message
            # safe to repeat: reads, column changes (idempotent) and creates (keyed above)
            write_safe=True,
        )
        try:
            body = resp.json()
        except ValueError:
            raise SinkError(
                f"monday: HTTP {resp.status_code}, non-JSON response: {resp.text[:200]}", retryable=resp.status_code != 400
            ) from None
        errors = body.get("errors") or []
        if body.get("error_message"):  # the older top-level error_code/error_message shape
            errors.append(
                {"message": body["error_message"], "extensions": {"code": body.get("error_code"), "status_code": body.get("status_code")}}
            )
        if errors:
            raise _error(errors)
        if resp.status_code != 200 or not isinstance(body.get("data"), dict):
            raise SinkError(f"monday: HTTP {resp.status_code}: {resp.text[:400]}", retryable=False)
        return body["data"]

    # -------------------------------------------------------------- rendering

    def _name(self, item: Item) -> str:
        return (f"[Dismissed] {item.title}" if item.dismissed else item.title)[:NAME_MAX]

    def _owner_id(self, item: Item) -> int | None:
        ids = {k.strip().lower(): v for k, v in self.cfg.owner_ids.items()}
        names = [item.owner or ""]
        if item.is_mine(self.app.user_name):
            names += [self.app.user_name, "me"]
        return next((ids[n.strip().lower()] for n in names if n.strip().lower() in ids), None)

    def _status(self, item: Item, create: bool) -> str | None:
        labels = self.cfg.status_labels
        if item.dismissed or item.status == ItemStatus.CANCELLED:
            return labels.cancelled
        if item.status == ItemStatus.DONE:
            return labels.done
        # an update never drags an item back from a state someone set on the board
        return labels.open if create else None

    def _columns(self, item: Item, *, create: bool) -> dict[str, Any]:
        """Column values for the item. Unknown values are left out rather than cleared."""
        c = self.cfg
        cols: dict[str, Any] = {}
        if c.status_column and (label := self._status(item, create)):
            cols[c.status_column] = {"label": label}
        if c.date_column and item.due_date:
            cols[c.date_column] = {"date": item.due_date}
        if c.owner_column and (uid := self._owner_id(item)) is not None:
            cols[c.owner_column] = {"personsAndTeams": [{"id": uid, "kind": "person"}]}
        if c.owner_text_column and item.owner:
            cols[c.owner_text_column] = item.owner
        if c.priority_column and item.priority:
            cols[c.priority_column] = {"label": getattr(c.priority_labels, item.priority)}
        if c.details_column and item.detail:
            cols[c.details_column] = {"text": item.detail[:LONG_TEXT_MAX]}
        return cols

    # ------------------------------------------------------------------ calls

    async def _create(self, name: str, group: str | None, columns: dict[str, Any] | None) -> tuple[str, str]:
        """Create an item; returns ``(item id, ref)``."""
        data = await self._gql(
            CREATE_ITEM,
            {
                "board": self.cfg.board_id,
                "group": group,
                "name": name,
                "columns": json.dumps(columns) if columns else None,
                "labels": self.cfg.create_labels,
            },
            create=True,
        )
        created = data.get("create_item") or {}
        if not created.get("id"):
            raise SinkError(f"monday: create_item returned no item: {data}", retryable=False)
        item_id = str(created["id"])
        return item_id, f"{created['url']}#{item_id}" if created.get("url") else item_id

    async def _post_update(self, item_id: str, markdown: str) -> None:
        """Post Markdown as an update; overflow goes into threaded replies so it reads in order."""
        parent: str | None = None
        for chunk in chunks(markdown, UPDATE_CHUNK):
            data = await self._gql(CREATE_UPDATE, {"item": item_id, "parent": parent, "body": to_html(chunk)}, create=True)
            if parent is None:
                parent = (data.get("create_update") or {}).get("id")

    async def push_item(self, item: Item, meeting: Meeting) -> str:
        item_id, ref = await self._create(self._name(item), self.cfg.group_id, self._columns(item, create=True))
        try:
            await self._post_update(item_id, item_markdown(item, meeting))
        except SinkError as exc:  # the item exists: failing now would make the retry create a duplicate
            log.warning("monday item %s created, but posting its details failed: %s", item_id, exc)
        return ref

    async def update_item(self, item: Item, meeting: Meeting, ref: str) -> None:
        columns = {"name": self._name(item), **self._columns(item, create=False)}
        await self._gql(
            CHANGE_COLUMNS,
            {"board": self.cfg.board_id, "item": item_id_from_ref(ref), "columns": json.dumps(columns), "labels": self.cfg.create_labels},
        )

    async def push_notes(self, meeting: Meeting, notes_markdown: str) -> str | None:
        if not self.cfg.notes_group_id:
            return None
        if ref := meeting.notes_ref.get(self.name):  # re-push: add the new version to the same item
            item_id = item_id_from_ref(ref)
        else:
            title = meeting.title or f"meeting on {meeting.started_at.astimezone():%Y-%m-%d %H:%M}"
            item_id, ref = await self._create(f"Meeting notes: {title}"[:NAME_MAX], self.cfg.notes_group_id, None)
        await self._post_update(item_id, notes_markdown)
        return ref

    async def check(self) -> str:
        data = await self._gql(CHECK, {"board": self.cfg.board_id})
        boards = data.get("boards") or []
        if not boards:
            raise SinkError(f"monday board {self.cfg.board_id} not found, or this token can't see it", retryable=False)
        board = boards[0]
        groups = {g["id"] for g in board.get("groups") or []}
        for field in ("group_id", "notes_group_id"):
            if (gid := getattr(self.cfg, field)) and gid not in groups:
                raise SinkError(
                    f"monday {field} '{gid}' is not a group on this board (groups: {', '.join(sorted(groups))})", retryable=False
                )
        columns = {col["id"]: col for col in board.get("columns") or []}
        for field, kind in _EXPECTED_TYPES.items():
            col_id = getattr(self.cfg, field)
            if col_id and (columns.get(col_id) or {}).get("type") != kind:
                options = ", ".join(f"{c['id']} ({c['title']})" for c in columns.values() if c.get("type") == kind) or "none"
                raise SinkError(
                    f"monday {field} '{col_id}' is not a {kind} column on this board ({kind} columns: {options})", retryable=False
                )
        mapped = sum(1 for field in _EXPECTED_TYPES if getattr(self.cfg, field))
        return f"signed in as {(data.get('me') or {}).get('name', '?')} · board '{board['name']}' · {mapped} column(s) mapped"


def _error(errors: list[dict[str, Any]]) -> SinkError:
    """Map a GraphQL ``errors`` array to a SinkError; rate limits and server errors are retryable."""
    ext = [e.get("extensions") or {} for e in errors]
    retryable = any(x.get("code") in RATE_LIMIT_CODES or int(x.get("status_code") or 0) in RETRYABLE for x in ext)
    wait = next((x["retry_in_seconds"] for x in ext if x.get("retry_in_seconds")), None)
    message = "; ".join(e.get("message", "error") + (f" [{x['code']}]" if x.get("code") else "") for e, x in zip(errors, ext, strict=True))
    return SinkError(f"monday: {message}" + (f" (retry in {wait}s)" if wait else ""), retryable=retryable)


def item_id_from_ref(ref: str) -> str:
    return ref.rpartition("#")[2]


def chunks(text: str, size: int) -> list[str]:
    """Split on paragraph boundaries into pieces of at most ``size`` characters."""
    out: list[str] = []
    for para in text.strip().split("\n\n"):
        for i in range(0, len(para), size):  # hard-split a monster paragraph
            piece = para[i : i + size]
            if out and len(out[-1]) + 2 + len(piece) <= size:
                out[-1] += "\n\n" + piece
            else:
                out.append(piece)
    return out


_INLINE = [
    (re.compile(r"\*\*(.+?)\*\*"), r"<b>\1</b>"),
    (re.compile(r"(?<![\w*])([_*])(?![_*\s])(.+?)(?<![\s_*])\1(?![\w*])"), r"<i>\2</i>"),
    (re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)"), r'<a href="\2">\1</a>'),
]
_BULLET = re.compile(r"^[-*] (\[( |x|X)\] )?")


def to_html(markdown: str) -> str:
    """Just enough Markdown → HTML for monday updates (headings, bullets, quotes, emphasis, links)."""
    out = []
    for para in re.split(r"\n\s*\n", markdown.strip()):
        lines, quote = [], True
        for raw in para.splitlines():
            line = raw.strip()
            is_quote = line.startswith(">")
            quote &= is_quote
            line = line.lstrip("> ") if is_quote else line
            heading = re.match(r"#{1,6} ", line)
            if heading:
                line = line[heading.end() :]
            bullet = _BULLET.match(line)
            if bullet:
                box = {" ": "☐ ", "x": "☑ ", "X": "☑ "}.get(bullet.group(2) or "", "• ")
                line = box + line[bullet.end() :]
            text = html.escape(line)
            for pattern, repl in _INLINE:
                text = pattern.sub(repl, text)
            lines.append(f"<b>{text}</b>" if heading else text)
        body = "<br>".join(lines)
        out.append(f"<blockquote>{body}</blockquote>" if quote else f"<p>{body}</p>")
    return "".join(out)
