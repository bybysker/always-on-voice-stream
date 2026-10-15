"""Linear sink: action items become issues in one team, kept in sync as they're refined.

Linear's GraphQL API takes a personal API key as the bare ``Authorization``
value, *not* ``Bearer <key>``. That form is for OAuth tokens: if you use one,
put the whole ``Bearer …`` string in the env var. Failures come back as an
``errors`` array, with HTTP 200 or 400. ``RATELIMITED`` errors are retryable.

Owners become assignees only through the ``assignees`` table or, with
``match_members``, an unambiguous match against the team's members. Anyone else
stays in the description as text. Done items move to the team's first
*completed* workflow state, and cancelled or dismissed ones to its first
*canceled* state. An issue is never moved back to an open state, because it may
have been closed in Linear itself.

Ref format: ``<issue url>#<issue id>``.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from typing import Any, ClassVar, Self

from pydantic import Field, model_validator

from ..config import Config
from ..models import Item, ItemStatus, Meeting
from .base import Sink, SinkConfig, SinkError, env_secret, item_markdown
from .http import RETRYABLE, HttpSinkMixin

log = logging.getLogger(__name__)

API_URL = "https://api.linear.app/graphql"
PRIORITY = {"high": 2, "medium": 3, "low": 4}
"""Linear priorities: 0 = none, 1 = urgent, 2 = high, 3 = medium, 4 = low."""

TEAM = """query ($filter: TeamFilter) {
  viewer { name }
  teams(filter: $filter, first: 1) { nodes { id key name states { nodes { id type position } } } }
}"""
MEMBERS = """query ($team: String!) {
  team(id: $team) { members(first: 250) { nodes { id name displayName } } }
}"""
ISSUE_CREATE = """mutation ($input: IssueCreateInput!) {
  issueCreate(input: $input) { success issue { id identifier url } }
}"""
ISSUE = """query ($id: String!) { issue(id: $id) { id url } }"""
ISSUE_UPDATE = """mutation ($id: String!, $input: IssueUpdateInput!) {
  issueUpdate(id: $id, input: $input) { success }
}"""


class LinearConfig(SinkConfig):
    token_env: str = "LINEAR_API_KEY"
    team_id: str | None = None
    team_key: str | None = None
    """Team key such as ``ENG``, resolved to the team id once. ``team_id`` wins if both are set."""
    project_id: str | None = None
    label_ids: list[str] = Field(default_factory=list)
    """Labels for new issues. Updates leave labels alone."""
    assignees: dict[str, str] = Field(default_factory=dict)
    """Owner name as spoken (case-insensitive) → Linear user id. Your own items
    also match your ``user_name`` or ``me``."""
    match_members: bool = False
    """Also assign owners that match exactly one team member, either by full or
    display name (case-insensitive) or by a first name nobody else on the team has."""

    @model_validator(mode="after")
    def need_team(self) -> Self:
        if not (self.team_id or self.team_key):
            raise ValueError("set team_id or team_key")
        return self


@dataclass(slots=True)
class _Team:
    id: str
    key: str
    name: str
    states: dict[str, str]
    """Workflow state type (``completed``, ``canceled``…) → id of its first state."""


class LinearSink(HttpSinkMixin, Sink):
    name = "linear"
    Config: ClassVar[type[SinkConfig]] = LinearConfig
    supports_update = True
    cfg: LinearConfig

    def __init__(self, cfg: LinearConfig, app: Config) -> None:  # type: ignore[override]
        super().__init__(cfg, app)
        self._team: _Team | None = None
        self._members: list[dict[str, str]] | None = None

    async def _gql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        resp = await self.request(
            "POST",
            API_URL,
            json={"query": query, "variables": variables},
            headers={"Authorization": env_secret(self.cfg, "token_env", "LINEAR_API_KEY")},
            ok=(200, 400),  # GraphQL errors, rate limits included, can arrive as HTTP 400
            # every call is safe to repeat: reads, updates, and creates that carry their own id
            write_safe=True,
        )
        try:
            body = resp.json()
        except ValueError:
            raise SinkError(
                f"linear: HTTP {resp.status_code}, non-JSON response: {resp.text[:200]}", retryable=resp.status_code != 400
            ) from None
        if errors := body.get("errors"):
            raise _error(errors)
        if resp.status_code != 200 or not isinstance(body.get("data"), dict):
            raise SinkError(f"linear: HTTP {resp.status_code}: {resp.text[:400]}", retryable=False)
        return body["data"]

    # ------------------------------------------------------------ lookups

    async def _load_team(self) -> tuple[str, _Team]:
        """Return ``(viewer name, team)`` in one round trip."""
        c = self.cfg
        flt = {"id": {"eq": c.team_id}} if c.team_id else {"key": {"eqIgnoreCase": c.team_key}}
        data = await self._gql(TEAM, {"filter": flt})
        nodes = (data.get("teams") or {}).get("nodes") or []
        if not nodes:
            raise SinkError(f"Linear team '{c.team_id or c.team_key}' not found, or this API key can't access it", retryable=False)
        t = nodes[0]
        states: dict[str, str] = {}
        for s in sorted(t["states"]["nodes"], key=lambda s: s.get("position", 0)):
            states.setdefault(s["type"], s["id"])
        return (data.get("viewer") or {}).get("name", "?"), _Team(t["id"], t["key"], t["name"], states)

    async def _team_info(self) -> _Team:
        if self._team is None:
            _, self._team = await self._load_team()
        return self._team

    async def _existing(self, issue_id: str) -> dict[str, str] | None:
        try:
            return (await self._gql(ISSUE, {"id": issue_id})).get("issue")
        except SinkError:
            return None

    async def _assignee(self, item: Item, team: _Team) -> str | None:
        if not item.owner:
            return None
        names = [item.owner]
        if item.is_mine(self.app.user_name):
            names += [self.app.user_name, "me"]
        explicit = {k.strip().lower(): v for k, v in self.cfg.assignees.items()}
        if uid := next((explicit[n.strip().lower()] for n in names if n.strip().lower() in explicit), None):
            return uid
        if not self.cfg.match_members:
            return None
        if self._members is None:
            data = await self._gql(MEMBERS, {"team": team.id})
            self._members = ((data.get("team") or {}).get("members") or {}).get("nodes") or []
        return next((uid for n in names if (uid := match_member(n, self._members))), None)

    async def _fields(self, item: Item, meeting: Meeting, team: _Team) -> dict[str, Any]:
        """Fields shared by create and update. Unknown assignees are left untouched, never cleared."""
        data: dict[str, Any] = {
            "title": item.title,
            "description": item_markdown(item, meeting),
            "dueDate": item.due_date,
            "priority": PRIORITY.get(item.priority or "", 0),
        }
        if assignee := await self._assignee(item, team):
            data["assigneeId"] = assignee
        if item.dismissed or item.status == ItemStatus.CANCELLED:
            state_type: str | None = "canceled"
        else:
            state_type = "completed" if item.status == ItemStatus.DONE else None
        if state_type:
            if state := team.states.get(state_type):
                data["stateId"] = state
            else:
                log.warning("Linear team %s has no '%s' workflow state; leaving %s's state alone", team.key, state_type, item.id)
        return data

    # -------------------------------------------------------------- sink API

    async def push_item(self, item: Item, meeting: Meeting) -> str:
        team = await self._team_info()
        data = {k: v for k, v in (await self._fields(item, meeting, team)).items() if v is not None}
        data["teamId"] = team.id
        if self.cfg.project_id:
            data["projectId"] = self.cfg.project_id
        if self.cfg.label_ids:
            data["labelIds"] = self.cfg.label_ids
        # A deterministic id makes a retried create (after a lost response) fail instead of
        # duplicating the issue; the existing issue is then looked up under that id.
        data["id"] = issue_uuid(team.id, meeting.id, item.id)
        try:
            result = (await self._gql(ISSUE_CREATE, {"input": data})).get("issueCreate") or {}
        except SinkError:
            if existing := await self._existing(data["id"]):
                return f"{existing['url']}#{existing['id']}"
            raise
        issue = result.get("issue") or {}
        if not result.get("success") or not issue.get("id"):
            raise SinkError(f"linear: issueCreate failed: {result}", retryable=False)
        return f"{issue['url']}#{issue['id']}"

    async def update_item(self, item: Item, meeting: Meeting, ref: str) -> None:
        team = await self._team_info()
        data = await self._fields(item, meeting, team)  # dueDate None clears a removed due date
        result = (await self._gql(ISSUE_UPDATE, {"id": issue_id_from_ref(ref), "input": data})).get("issueUpdate") or {}
        if not result.get("success"):
            raise SinkError(f"linear: issueUpdate failed: {result}", retryable=False)

    async def check(self) -> str:
        viewer, self._team = await self._load_team()  # always a live call, even when cached
        return f"signed in as {viewer} · team {self._team.key} ({self._team.name})"


def _error(errors: list[dict[str, Any]]) -> SinkError:
    """Map a GraphQL ``errors`` array to a SinkError; rate limits and server errors are retryable."""
    ext = [e.get("extensions") or {} for e in errors]
    retryable = any(x.get("code") == "RATELIMITED" or int(x.get("statusCode") or 0) in RETRYABLE for x in ext)
    message = "; ".join(
        (x.get("userPresentableMessage") or e.get("message") or "error") + (f" [{x['code']}]" if x.get("code") else "")
        for e, x in zip(errors, ext, strict=True)
    )
    return SinkError(f"linear: {message}", retryable=retryable)


def issue_uuid(*parts: str) -> str:
    """A stable UUID in the v4 format Linear requires for client-chosen issue ids."""
    return str(uuid.UUID(bytes=hashlib.sha256("/".join(parts).encode()).digest()[:16], version=4))


def issue_id_from_ref(ref: str) -> str:
    return ref.rpartition("#")[2]


def match_member(name: str, members: list[dict[str, str]]) -> str | None:
    """A member whose full or display name is ``name`` (case-insensitive) or, for a
    bare first name, the only member with that first name. Ambiguity → ``None``."""
    n = name.strip().lower()
    if not n:
        return None
    exact = {m["id"] for m in members if n in {(m.get("name") or "").lower(), (m.get("displayName") or "").lower()}}
    if exact:
        return exact.pop() if len(exact) == 1 else None
    if " " in n:
        return None
    first = {m["id"] for m in members if (m.get("name") or "").lower().split()[:1] == [n]}
    return first.pop() if len(first) == 1 else None
