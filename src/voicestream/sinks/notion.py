"""Notion sink: action items become pages in a tasks database, meeting notes a page.

Written against Notion API version ``2026-03-11``. Two recent changes shape it:

* Since ``2025-09-03`` a database is a container of one or more *data sources*. The
  property schema lives on the data source and pages are created with a
  ``data_source_id`` parent, so a configured database id is resolved to its data source.
* ``2026-03-11`` renamed ``archived`` to ``in_trash`` in request and response bodies.

Every team's database looks different ("Name" or "Task", status vs select vs checkbox,
people vs text owner), so the schema is read once and mapped by type and name. Only
properties that exist with a compatible type are written, and status/select values are
only ever options that already exist. Whatever can't be expressed that way still shows
up: an unmapped owner in the page body, a done/cancelled/dismissed state with no
matching option as a ``[done]``-style title prefix.

Page content is sent as Notion-flavoured Markdown (the create-page ``markdown`` field
and ``/pages/{id}/markdown``), so Notion builds the blocks and handles their rich-text
limits. Long notes go up in pieces well below the per-request block and size caps.

Item refs are the page URL, plus ``#<page id>`` when the URL doesn't contain the id.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..config import Config as AppConfig
from ..models import Item, ItemStatus, Meeting
from .base import Sink, SinkConfig, SinkError, env_secret, item_markdown
from .http import HttpSinkMixin

log = logging.getLogger(__name__)

API = "https://api.notion.com/v1"
DEFAULT_ENV = "NOTION_TOKEN"
TEXT_MAX = 2000
"""Characters per rich-text object."""
ARRAY_MAX = 100
"""Elements per rich-text array."""
MD_CHUNK_CHARS = 10_000
MD_CHUNK_LINES = 250
"""Markdown goes up in pieces of about this size (a request may carry 1000 blocks / 500 KB)."""
AGENT_MARK = "Agent result"
"""Label :func:`item_markdown` puts before an agent's result; used to append it only once."""

# role -> (property names tried in order, case-insensitively; compatible property types)
_ROLES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "status": (("status", "state", "stage", "done", "completed", "complete"), ("status", "select", "checkbox")),
    "due": (("due", "due date", "deadline", "due on", "date due"), ("date",)),
    "owner": (("owner", "assignee", "assignees", "assigned to", "responsible"), ("people", "rich_text")),
    "priority": (("priority", "prio"), ("select",)),
    "meeting": (("meeting", "source meeting", "source", "from meeting"), ("rich_text", "select")),
    "date": (("date", "meeting date", "when"), ("date",)),  # notes databases
}
# Unnamed fallbacks: a tasks database with exactly one property of this type uses it.
_ONLY_OF_TYPE = {"status": "status", "owner": "people"}
_STATUS_NAMES: dict[ItemStatus, tuple[str, ...]] = {
    ItemStatus.OPEN: ("Not started", "To do", "To-do", "Todo", "Open", "Backlog", "New"),
    ItemStatus.DONE: ("Done", "Complete", "Completed", "Closed", "Finished"),
    ItemStatus.CANCELLED: ("Cancelled", "Canceled", "Won't do", "Won’t do", "Not doing", "Dropped", "Abandoned"),
}
# Status properties group their options; these groups always exist, whatever the options are called.
_STATUS_GROUPS = {ItemStatus.OPEN: "To-do", ItemStatus.DONE: "Complete"}
_ID = re.compile(r"(?<![0-9a-f])[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}(?![0-9a-f])")


class NotionProperties(BaseModel):
    """Property names to use instead of auto-detection (the title property is always found)."""

    model_config = ConfigDict(extra="forbid")

    status: str | None = None
    due: str | None = None
    owner: str | None = None
    priority: str | None = None
    meeting: str | None = None


class NotionConfig(SinkConfig):
    token_env: str = DEFAULT_ENV
    """Env var holding the internal integration secret."""
    notion_version: str = "2026-03-11"
    tasks_database_id: str | None = None
    tasks_data_source_id: str | None = None
    """Takes precedence over ``tasks_database_id``; needed when a database has several data sources."""
    notes_database_id: str | None = None
    notes_data_source_id: str | None = None
    notes_parent_page_id: str | None = None
    """Without a notes database, each meeting's notes become a child page of this page."""
    properties: NotionProperties = Field(default_factory=NotionProperties)
    status_open: str | None = None
    status_done: str | None = None
    status_cancelled: str | None = None
    """Status/select option names; tried before the defaults ("Not started", "Done", "Cancelled"…)."""
    priorities: dict[Literal["low", "medium", "high"], str] = Field(
        default_factory=lambda: {"low": "Low", "medium": "Medium", "high": "High"}
    )
    """Item priority -> option name of the priority select."""
    users: dict[str, str] = Field(default_factory=dict)
    """Owner name (case-insensitive; your ``user_name`` or "me" for yourself) -> Notion user id.
    Needed for people properties; unmapped owners are left to the page body."""
    trash_dismissed: bool = False
    """Move pages of dismissed items to the trash (restored if the item is un-dismissed)."""

    @model_validator(mode="after")
    def _has_target(self) -> NotionConfig:
        if not any(
            (
                self.tasks_database_id,
                self.tasks_data_source_id,
                self.notes_database_id,
                self.notes_data_source_id,
                self.notes_parent_page_id,
            )
        ):
            raise ValueError("set tasks_database_id (or tasks_data_source_id) and/or a notes target")
        return self


@dataclass(slots=True)
class _Schema:
    data_source_id: str
    name: str
    title: str
    """Name of the title property."""
    props: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Role (status, due, owner, ...) -> property config as returned by the API."""
    states: dict[ItemStatus, str] = field(default_factory=dict)
    """Item status -> existing option of the status/select property."""

    def describe(self) -> str:
        return ", ".join([f"title={self.title}", *(f"{role}={p['name']}" for role, p in self.props.items())])


class NotionSink(HttpSinkMixin, Sink):
    name = "notion"
    Config: ClassVar[type[SinkConfig]] = NotionConfig
    supports_update = True
    supports_notes = True
    cfg: NotionConfig

    def __init__(self, cfg: NotionConfig, app: AppConfig) -> None:  # type: ignore[override]
        super().__init__(cfg, app)
        self._users = {name.strip().lower(): uid for name, uid in cfg.users.items()}
        self._schemas: dict[str, _Schema] = {}

    # ------------------------------------------------------------------- items

    async def push_item(self, item: Item, meeting: Meeting) -> str:
        s = await self._schema("tasks")
        props = {k: v for k, v in self._properties(s, item, meeting).items() if any(v.values())}
        parent = {"type": "data_source_id", "data_source_id": s.data_source_id}
        return await self._create_page(parent, props, item_markdown(item, meeting))

    async def update_item(self, item: Item, meeting: Meeting, ref: str) -> None:
        page_id = _page_id(ref)
        s = await self._schema("tasks")
        body: dict[str, Any] = {"properties": self._properties(s, item, meeting)}
        if self.cfg.trash_dismissed:
            body["in_trash"] = item.dismissed
        # re-sending the same values is harmless, so overload errors may be retried
        await self._call("PATCH", f"pages/{page_id}", body, write_safe=True)
        if item.agent and item.agent.result_summary and not item.dismissed:
            # the result usually lands after the page was created
            page = await self._call("GET", f"pages/{page_id}/markdown")
            if AGENT_MARK not in page.get("markdown", ""):
                await self._append(page_id, _md_chunks(f"**{AGENT_MARK}:**\n\n{item.agent.result_summary}"))

    def _properties(self, s: _Schema, item: Item, meeting: Meeting) -> dict[str, Any]:
        """Values for every mapped property; empty ones clear the property on update.

        Values that can't be expressed (an unmapped owner, a missing option) are left out,
        so manual edits in Notion aren't wiped.
        """
        state = ItemStatus.CANCELLED if item.dismissed else item.status
        out: dict[str, Any] = {}
        shown = state == ItemStatus.OPEN
        if (p := s.props.get("status")) is not None:
            if p["type"] == "checkbox":
                if state != ItemStatus.CANCELLED:
                    out[p["name"]] = {"checkbox": state == ItemStatus.DONE}
                    shown = True
            elif option := s.states.get(state):
                out[p["name"]] = {p["type"]: {"name": option}}
                shown = True
        label = "dismissed" if item.dismissed else state.value
        out[s.title] = {"title": _rt(item.title if shown else f"[{label}] {item.title}")}
        if (p := s.props.get("due")) is not None:
            out[p["name"]] = {"date": {"start": item.due_date} if item.due_date else None}
        owner = (item.owner or "").strip()
        if (p := s.props.get("owner")) is not None:
            if p["type"] == "rich_text":
                out[p["name"]] = {"rich_text": _rt(owner)}
            elif not owner:
                out[p["name"]] = {"people": []}
            elif uid := self._user_id(item):
                out[p["name"]] = {"people": [{"object": "user", "id": uid}]}
        if (p := s.props.get("priority")) is not None:
            if not item.priority:
                out[p["name"]] = {"select": None}
            elif option := _option(p, [self.cfg.priorities.get(item.priority)]):
                out[p["name"]] = {"select": {"name": option}}
        if (p := s.props.get("meeting")) is not None:
            if p["type"] == "rich_text":
                out[p["name"]] = {"rich_text": _rt(_meeting_label(meeting))}
            elif option := _option(p, [_meeting_label(meeting)]):
                out[p["name"]] = {"select": {"name": option}}
        return out

    def _user_id(self, item: Item) -> str | None:
        """Notion user id of a non-blank owner, only ever via the configured ``users`` map."""
        names = [(item.owner or "").strip().lower()]
        if item.is_mine(self.app.user_name):
            names += [self.app.user_name.strip().lower(), "me"]
        return next((self._users[n] for n in names if n in self._users), None)

    # ------------------------------------------------------------------- notes

    async def push_notes(self, meeting: Meeting, notes_markdown: str) -> str | None:
        head, _, rest = notes_markdown.partition("\n")
        body = rest.lstrip("\n") if head.startswith("# ") else notes_markdown  # the page title shows it
        if ref := meeting.notes_ref.get(self.name):
            # pushed before (e.g. ``voicestream push``): refresh our page instead of duplicating it
            page_id = _page_id(ref)
            first, *more = _md_chunks(body)
            await self._call(
                "PATCH", f"pages/{page_id}/markdown", {"type": "replace_content", "replace_content": {"new_str": first}}
            )
            await self._append(page_id, more)
            return ref
        c = self.cfg
        title = {"title": _rt(_meeting_label(meeting))}
        props: dict[str, Any]
        if c.notes_data_source_id or c.notes_database_id:
            s = await self._schema("notes")
            parent = {"type": "data_source_id", "data_source_id": s.data_source_id}
            props = {s.title: title}
            if (p := s.props.get("date")) is not None:
                props[p["name"]] = {"date": {"start": meeting.started_at.isoformat()}}
        elif c.notes_parent_page_id:
            parent = {"type": "page_id", "page_id": c.notes_parent_page_id}
            props = {"title": title}
        else:
            return None
        return await self._create_page(parent, props, body)

    # ------------------------------------------------------------------- check

    async def check(self) -> str:
        me = await self._call("GET", "users/me")
        workspace = (me.get("bot") or {}).get("workspace_name")
        parts = [f"connected as {me.get('name') or 'integration'}" + (f" ({workspace})" if workspace else "")]
        c = self.cfg
        if c.tasks_data_source_id or c.tasks_database_id:
            s = await self._schema("tasks")
            parts.append(f"tasks → {s.name} [{s.describe()}]")
        if c.notes_data_source_id or c.notes_database_id:
            notes = await self._schema("notes")
            parts.append(f"notes → {notes.name}")
        elif c.notes_parent_page_id:
            page = await self._call(
                "GET", f"pages/{c.notes_parent_page_id}", what=f"the notes parent page {c.notes_parent_page_id}"
            )
            parts.append(f"notes → under {page.get('url') or c.notes_parent_page_id}")
        return "; ".join(parts)

    # -------------------------------------------------------------------- API

    def _headers(self) -> dict[str, str]:
        try:
            token = env_secret(self.cfg, "token_env", DEFAULT_ENV)
        except SinkError as exc:
            raise SinkError(
                f"{exc}: create an internal integration at https://www.notion.so/my-integrations and use its secret",
                retryable=False,
            ) from exc
        return {"Authorization": f"Bearer {token}", "Notion-Version": self.cfg.notion_version}

    async def _call(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        what: str | None = None,
        write_safe: bool = False,
    ) -> dict[str, Any]:
        """One API call; Notion's client errors become actionable :class:`SinkError` messages."""
        resp = await self.request(
            method,
            f"{API}/{path}",
            json=body,
            headers=self._headers(),
            write_safe=write_safe,
            ok=(200, 400, 401, 403, 404, 409),
        )
        code = resp.status_code
        if code == 200:
            return resp.json()
        detail = _error_detail(resp)
        if code == 400:
            self._schemas.clear()  # most likely the database changed since we read it
            raise SinkError(f"Notion rejected {method} /{path}: {detail}", retryable=False)
        if code == 401:
            raise SinkError(f"Notion rejected the token in ${self.cfg.token_env}: {detail}", retryable=False)
        if code == 403:
            raise SinkError(
                f"the Notion integration lacks a capability for {method} /{path} (see its Capabilities settings): {detail}",
                retryable=False,
            )
        if code == 404:
            raise SinkError(
                f"Notion can't see {what or '/' + path}: share it with your integration "
                f"(in Notion: ⋯ menu → Connections). {detail}",
                retryable=False,
            )
        raise SinkError(f"Notion conflict on {method} /{path}, will retry: {detail}")  # 409

    async def _data_source(self, kind: str, ds_id: str | None, db_id: str | None) -> str:
        if ds_id:
            return ds_id
        if not db_id:
            raise SinkError(f"no Notion {kind} database configured (set {kind}_database_id)", retryable=False)
        db = await self._call("GET", f"databases/{db_id}", what=f"the {kind} database {db_id}")
        sources = db.get("data_sources") or []
        if not sources:
            raise SinkError(f"the Notion {kind} database {db_id} has no data sources", retryable=False)
        if len(sources) > 1:
            log.warning(
                "Notion %s database has %d data sources; using %r (set %s_data_source_id to choose)",
                kind, len(sources), sources[0].get("name"), kind,
            )
        return sources[0]["id"]

    async def _schema(self, kind: Literal["tasks", "notes"]) -> _Schema:
        """Resolve and map a target data source (cached until Notion rejects a request)."""
        if (cached := self._schemas.get(kind)) is not None:
            return cached
        c = self.cfg
        if kind == "tasks":
            ds_id = await self._data_source(kind, c.tasks_data_source_id, c.tasks_database_id)
            roles: tuple[str, ...] = ("status", "due", "owner", "priority", "meeting")
        else:
            ds_id = await self._data_source(kind, c.notes_data_source_id, c.notes_database_id)
            roles = ("date",)
        ds = await self._call("GET", f"data_sources/{ds_id}", what=f"the {kind} data source {ds_id}")
        props = {name: {**p, "name": name} for name, p in (ds.get("properties") or {}).items()}
        s = _Schema(
            data_source_id=ds_id,
            name="".join(t.get("plain_text", "") for t in ds.get("title") or []) or ds_id,
            title=next((name for name, p in props.items() if p.get("type") == "title"), "title"),
        )
        for role in roles:
            if (p := _detect(props, role, getattr(c.properties, role, None))) is not None:
                s.props[role] = p
        if (status := s.props.get("status")) is not None and status["type"] != "checkbox":
            wanted = {
                ItemStatus.OPEN: c.status_open,
                ItemStatus.DONE: c.status_done,
                ItemStatus.CANCELLED: c.status_cancelled,
            }
            for state, defaults in _STATUS_NAMES.items():
                option = _option(status, (wanted[state], *defaults)) or _group_option(status, _STATUS_GROUPS.get(state))
                if option:
                    s.states[state] = option
        log.info("Notion %s → %s: %s", kind, s.name, s.describe())
        self._schemas[kind] = s
        return s

    async def _create_page(self, parent: dict[str, Any], properties: dict[str, Any], markdown: str) -> str:
        first, *more = _md_chunks(markdown)
        body = {"parent": parent, "properties": properties} | ({"markdown": first} if first else {})
        page = await self._call("POST", "pages", body)
        ref = _ref(page)
        try:
            await self._append(_page_id(ref), more)
        except SinkError as exc:  # the page exists: failing now would create a duplicate on retry
            log.warning("Notion page %s created, but appending the rest of its content failed: %s", ref, exc)
        return ref

    async def _append(self, page_id: str, chunks: Iterable[str]) -> None:
        for chunk in chunks:
            await self._call(
                "PATCH",
                f"pages/{page_id}/markdown",
                {"type": "insert_content", "insert_content": {"content": chunk, "position": {"type": "end"}}},
            )


# --------------------------------------------------------------------- helpers


def _detect(props: dict[str, dict[str, Any]], role: str, override: str | None) -> dict[str, Any] | None:
    """The property playing ``role``: the configured name, else a known name, else the only one of its type."""
    names, types = _ROLES[role]
    by_name = {name.lower(): p for name, p in props.items()}
    if override:
        p = by_name.get(override.lower())
        if p is None or p.get("type") not in types:
            log.warning("Notion property %r (for %s) is missing or not one of: %s", override, role, ", ".join(types))
            return None
        return p
    for name in names:
        if (p := by_name.get(name)) is not None and p.get("type") in types:
            return p
    only = [p for p in props.values() if p.get("type") == _ONLY_OF_TYPE.get(role)]
    return only[0] if len(only) == 1 else None


def _option(prop: dict[str, Any], wanted: Iterable[str | None]) -> str | None:
    """The first of ``wanted`` that exists as an option of a select/status property (any case)."""
    options = {o["name"].lower(): o["name"] for o in (prop.get(prop["type"]) or {}).get("options", [])}
    return next((options[w.lower()] for w in wanted if w and w.lower() in options), None)


def _group_option(prop: dict[str, Any], group: str | None) -> str | None:
    """First option in a status property's group (e.g. ``Complete``), whatever it's called."""
    if group is None or prop["type"] != "status":
        return None
    config = prop.get("status") or {}
    names = {o["id"]: o["name"] for o in config.get("options", [])}
    for g in config.get("groups", []):
        if g.get("name", "").lower() == group.lower():
            return next((names[i] for i in g.get("option_ids", []) if i in names), None)
    return None


def _rt(text: str) -> list[dict[str, Any]]:
    """Plain rich text within Notion's limits (2000 chars per object, 100 objects)."""
    pieces = [text[i : i + TEXT_MAX] for i in range(0, len(text), TEXT_MAX)][:ARRAY_MAX]
    return [{"type": "text", "text": {"content": piece}} for piece in pieces]


def _md_chunks(md: str) -> list[str]:
    """Split Markdown into request-sized pieces, never inside a code fence.

    Past the soft limit it cuts at the next blank line (dropping it); past twice the
    limit, e.g. in one long list, at any line.
    """
    if len(md) <= MD_CHUNK_CHARS and md.count("\n") < MD_CHUNK_LINES:
        return [md]
    chunks: list[str] = []
    cur: list[str] = []
    size, fenced = 0, False
    for line in md.splitlines():
        blank = not line.strip()
        full = size >= MD_CHUNK_CHARS or len(cur) >= MD_CHUNK_LINES
        overfull = size >= 2 * MD_CHUNK_CHARS or len(cur) >= 2 * MD_CHUNK_LINES
        if cur and not fenced and (overfull or (full and blank)):
            chunks.append("\n".join(cur))
            cur, size = [], 0
            if blank:
                continue
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        cur.append(line)
        size += len(line) + 1
    if cur:
        chunks.append("\n".join(cur))
    return chunks


def _meeting_label(meeting: Meeting) -> str:
    return meeting.title or f"Meeting {meeting.started_at.astimezone():%Y-%m-%d %H:%M}"


def _ref(page: dict[str, Any]) -> str:
    page_id = page["id"].replace("-", "")
    url = page.get("url")
    if not url:
        return page_id
    return url if page_id in url else f"{url}#{page_id}"


def _page_id(ref: str) -> str:
    ids = _ID.findall(ref)
    if not ids:
        raise SinkError(f"not a Notion page reference: {ref!r}", retryable=False)
    return ids[-1].replace("-", "")


def _error_detail(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("message") or resp.text)[:300]
    except (ValueError, AttributeError):
        return resp.text[:300]
