"""Todoist sink: your own action items land in your personal task list.

Uses the unified API v1 (``https://api.todoist.com/api/v1``). The REST v2 endpoints
it replaced now answer 410. Todoist is a personal list, so the default is
``owners = "mine"``: only items owned by you (``user_name`` or "me") are pushed.

Each create carries an ``X-Request-Id`` derived from the item and its revision, so
Todoist can recognise a retry of the same create (say, after a lost response)
instead of adding the task twice. That is what makes retrying creates on 5xx safe.

Due dates go up as ``due_date`` when the extractor resolved one. Otherwise the
spoken phrase goes up as ``due_string`` for Todoist's own parser, in the meeting's
language, but only when the task is created: parsed again on a later update,
"tomorrow" would move. If Todoist can't parse it, the task is saved without a due
date; the phrase is still in the description. A due date is never cleared, since
you may have set it in Todoist yourself.

API priorities run opposite to the app's p1–p4: 4 is the most urgent (p1) and 1 is
none (p4). Done, cancelled and dismissed items close the task. Tasks are never
deleted, and never reopened, since you may have ticked them off in Todoist.

Ref format: ``https://app.todoist.com/app/task/<task id>``.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from typing import Any, ClassVar, Literal

import httpx
from pydantic import Field

from ..models import Item, ItemStatus, Meeting
from .base import Sink, SinkConfig, SinkError, env_secret, item_markdown
from .http import HttpSinkMixin

log = logging.getLogger(__name__)

API = "https://api.todoist.com/api/v1"
TASK_URL = "https://app.todoist.com/app/task/"
PRIORITY = {"high": 4, "medium": 3, "low": 2}
"""API priorities: 4 = urgent (p1 in the app) … 1 = none (p4)."""
CONTENT_MAX = 500
DESCRIPTION_MAX = 16_000
_ERRORS = (400, 401, 403, 404, 410)
"""Statuses :meth:`TodoistSink._api` explains itself (the mixin retries 429 and 5xx)."""
_DUE_PHRASE = ("due_string", "due_lang")


class TodoistConfig(SinkConfig):
    owners: Literal["all", "mine"] = "mine"
    token_env: str = "TODOIST_TOKEN"
    """API token from Todoist → Settings → Integrations → Developer."""
    project_id: str | None = None
    """Project for new tasks (default: your Inbox)."""
    labels: list[str] = Field(default_factory=list)
    """Label names for new tasks."""


class TodoistError(SinkError):
    def __init__(self, message: str, *, status: int) -> None:
        super().__init__(message, retryable=False)
        self.status = status


class TodoistSink(HttpSinkMixin, Sink):
    name = "todoist"
    Config: ClassVar[type[SinkConfig]] = TodoistConfig
    supports_update = True
    cfg: TodoistConfig

    async def _api(self, method: str, path: str, json: dict[str, Any] | None = None, *, request_id: str | None = None) -> httpx.Response:
        headers = {"Authorization": f"Bearer {env_secret(self.cfg, 'token_env', 'TODOIST_TOKEN')}"}
        if request_id:
            headers["X-Request-Id"] = request_id
        resp = await self.request(
            method,
            API + path,
            json=json,
            headers=headers,
            ok=(200, 204, *_ERRORS),
            # every write here is safe to repeat: updates and closes set absolute values, creates carry X-Request-Id
            write_safe=True,
        )
        if resp.status_code in _ERRORS:
            hint = {
                401: f"check {self.cfg.token_env}",
                403: f"check {self.cfg.token_env}",
                404: "it no longer exists, or this token can't see it",
                410: "Todoist retired this endpoint; update voicestream",
            }.get(resp.status_code)
            raise TodoistError(
                f"todoist: HTTP {resp.status_code} on {method} {path}: {resp.text[:300]}" + (f" — {hint}" if hint else ""),
                status=resp.status_code,
            )
        return resp

    async def _save(self, path: str, data: dict[str, Any], *, request_id: str | None = None) -> httpx.Response:
        """POST task fields; if Todoist rejects the due phrase, again without it."""
        try:
            return await self._api("POST", path, data, request_id=request_id)
        except TodoistError as exc:
            if exc.status != 400 or "due_string" not in data:
                raise
            log.warning("%s; saving without the due date %r", exc, data["due_string"])
        data = {k: v for k, v in data.items() if k not in _DUE_PHRASE}
        return await self._api("POST", path, data, request_id=request_id and stable_uuid(request_id, "no-due"))

    async def _close(self, task_id: str) -> None:
        try:
            await self._api("POST", f"/tasks/{task_id}/close")
        except TodoistError as exc:
            if exc.status not in (400, 404):
                raise
            # the fields were just saved, so the task exists: it's already completed
            log.info("todoist: task %s was already closed (%s)", task_id, exc)

    def _fields(self, item: Item, meeting: Meeting, *, create: bool) -> dict[str, Any]:
        data: dict[str, Any] = {
            "content": _clip(item.title, CONTENT_MAX),
            "description": _clip(item_markdown(item, meeting), DESCRIPTION_MAX),
            "priority": PRIORITY.get(item.priority or "", 1),
        }
        if item.due_date:
            data["due_date"] = item.due_date
        elif item.due_text and create:
            data["due_string"] = item.due_text
            # needed for a non-English phrase; with auto-detection (the default) only the meeting knows its language
            if lang := self.app.transcription.language or meeting.language:
                data["due_lang"] = lang
        return data

    # -------------------------------------------------------------- sink API

    async def push_item(self, item: Item, meeting: Meeting) -> str:
        data = self._fields(item, meeting, create=True)
        if self.cfg.project_id:
            data["project_id"] = self.cfg.project_id
        if self.cfg.labels:
            data["labels"] = self.cfg.labels
        resp = await self._save("/tasks", data, request_id=stable_uuid(meeting.id, item.id, str(item.revision)))
        task_id = resp.json().get("id") if resp.content else None
        if not task_id:
            raise SinkError(f"todoist: created a task but got no id back: {resp.text[:200]}", retryable=False)
        if is_closed(item):
            try:
                await self._close(task_id)
            except SinkError as exc:  # the task exists: failing now would create a duplicate on retry
                log.warning("todoist: created task %s but couldn't close it: %s", task_id, exc)
        return f"{TASK_URL}{task_id}"

    async def update_item(self, item: Item, meeting: Meeting, ref: str) -> None:
        task_id = ref.rstrip("/").rpartition("/")[2]
        await self._save(f"/tasks/{task_id}", self._fields(item, meeting, create=False))
        if is_closed(item):
            await self._close(task_id)

    async def check(self) -> str:
        if self.cfg.project_id:
            project = (await self._api("GET", f"/projects/{self.cfg.project_id}")).json()
            line = f"token ok · new tasks go to project {project.get('name', self.cfg.project_id)}"
        else:
            page = (await self._api("GET", "/projects")).json()
            count = f"{len(page.get('results') or [])}{'+' if page.get('next_cursor') else ''}"
            line = f"token ok · {count} projects · new tasks go to your Inbox"
        return line + (" · only items you own" if self.cfg.owners == "mine" else "")


def is_closed(item: Item) -> bool:
    return item.dismissed or item.status in (ItemStatus.DONE, ItemStatus.CANCELLED)


def stable_uuid(*parts: str) -> str:
    """A UUID that's the same for the same ``parts`` (used as ``X-Request-Id``)."""
    return str(uuid.UUID(bytes=hashlib.sha256("/".join(parts).encode()).digest()[:16], version=4))


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
