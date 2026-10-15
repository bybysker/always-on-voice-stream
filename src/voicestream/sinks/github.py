"""GitHub sink: action items become issues in one repository, kept in sync as they're refined.

Written against REST API version ``2026-03-10``, sent explicitly: without the header
GitHub still serves ``2022-11-28``. That version removed the singular ``assignee``
field, so owners go in ``assignees``.

In a PATCH, ``labels`` and ``assignees`` *replace* the whole set, so updates are
read-modify-write. The issue is fetched first, and only the labels and logins this
sink manages (``priority_labels`` and ``assignees`` from the config) are swapped.
Anything a person added in GitHub stays. The title and body belong to voicestream
and are rewritten.

The issue state follows the item: done closes it as *completed*, cancelled or
dismissed closes it as *not planned*, and back to open reopens it. It only does so
when the item's state changed since voicestream last pushed it. That last-pushed
state is kept in a hidden comment at the end of the body, so an issue someone closed
or reopened by hand in GitHub is left alone.

Rate limits: GitHub answers 403 *or* 429 for both primary and secondary limits.
Those requests are retried after ``retry-after``, after ``x-ratelimit-reset``, or
after 60 s, doubling each time. A wait longer than ``MAX_WAIT_S`` is not slept
through: it surfaces as a retryable :class:`SinkError`.

Ref format: the issue's ``html_url`` (``…/{owner}/{repo}/issues/{number}``), which
carries everything an update needs.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Iterable
from typing import Any, ClassVar, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import Field

from ..models import Item, ItemStatus, Meeting
from .base import Sink, SinkConfig, SinkError, env_secret, item_markdown
from .http import HttpSinkMixin, retry_after

log = logging.getLogger(__name__)

TITLE_MAX = 256
BODY_MAX = 65_536
"""GitHub's issue title and body limits, in characters."""
RATE_LIMIT_RETRIES = 2
MAX_WAIT_S = 120.0
"""Longest rate-limit wait slept through inline; longer ones are left to the dispatcher."""
_HANDLED = (200, 201, 401, 403, 404, 410, 422, 429)
"""Statuses :meth:`GitHubSink._api` interprets itself (the mixin retries the rest)."""
_MARKER = re.compile(r"<!-- voicestream [^>]*?state=(?P<state>\w+) -->")
OPEN, COMPLETED, NOT_PLANNED = "open", "completed", "not_planned"


class GitHubConfig(SinkConfig):
    token_env: str = "GITHUB_TOKEN"
    """Fine-grained token with *Issues: read and write* on the repo (classic: ``repo``)."""
    repo: str = Field(pattern=r"^[\w.-]+/[\w.-]+$")
    """``owner/name``."""
    labels: list[str] = Field(default_factory=lambda: ["meeting-action"])
    """Labels for new issues. Updates leave them alone."""
    priority_labels: dict[Literal["low", "medium", "high"], str] = Field(default_factory=dict)
    """Optional label per priority, e.g. ``{ high = "priority: high" }``. It is swapped when the priority changes."""
    assignees: dict[str, str] = Field(default_factory=dict)
    """Owner name as spoken (case-insensitive) → GitHub login. Your own items also
    match your ``user_name`` or ``me``. Other owners are only named in the body."""
    api_url: str = "https://api.github.com"
    """For GitHub Enterprise Server: ``https://<host>/api/v3``."""
    api_version: str = "2026-03-10"
    """Pinned on purpose; bump it deliberately."""


class GitHubError(SinkError):
    def __init__(self, message: str, *, status: int, retryable: bool = False) -> None:
        super().__init__(message, retryable=retryable)
        self.status = status


class GitHubSink(HttpSinkMixin, Sink):
    name = "github"
    Config: ClassVar[type[SinkConfig]] = GitHubConfig
    supports_update = True
    cfg: GitHubConfig

    async def _api(self, method: str, url: str, json: dict[str, Any] | None = None) -> Any:
        """Call the REST API (``url`` may be a path) and return the decoded JSON body."""
        if url.startswith("/"):
            url = self.cfg.api_url.rstrip("/") + url
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {env_secret(self.cfg, 'token_env', 'GITHUB_TOKEN')}",
            "X-GitHub-Api-Version": self.cfg.api_version,
        }
        attempt = 0
        while True:
            resp = await self.request(
                method,
                url,
                json=json,
                headers=headers,
                ok=_HANDLED,
                write_safe=method == "PATCH",  # absolute field values: repeating one is harmless
                follow_redirects=True,  # renamed repos and transferred issues redirect
            )
            if resp.status_code < 300:
                return resp.json()
            wait = rate_limit_wait(resp, attempt)
            if wait is None or wait > MAX_WAIT_S or attempt >= RATE_LIMIT_RETRIES:
                raise self._error(resp, wait)
            log.warning("github: rate limited (HTTP %d), retrying in %.0fs", resp.status_code, wait)
            await asyncio.sleep(wait)
            attempt += 1

    def _error(self, resp: httpx.Response, wait: float | None) -> GitHubError:
        try:
            data = resp.json()
        except ValueError:
            data = None
        data = data if isinstance(data, dict) else {}
        message = data.get("message") or resp.text[:200]
        if errors := [e for e in data.get("errors") or [] if isinstance(e, dict)]:
            message += " (" + "; ".join(e.get("message") or f"{e.get('field')} {e.get('code')}" for e in errors) + ")"
        status = resp.status_code
        if wait is not None:
            return GitHubError(f"github: rate limited (HTTP {status}), retry in {wait:.0f}s: {message}", status=status, retryable=True)
        hint = {
            401: f"check {self.cfg.token_env}",
            403: f"the token needs Issues: read and write on {self.cfg.repo}",
            404: f"check that {self.cfg.repo} exists and the token can access it",
        }.get(status)
        return GitHubError(f"github: HTTP {status} {message}" + (f" — {hint}" if hint else ""), status=status)

    async def _write(self, method: str, url: str, payload: dict[str, Any], item: Item) -> Any:
        """POST/PATCH ``payload``; if GitHub rejects the assignee, again without it."""
        try:
            return await self._api(method, url, payload)
        except GitHubError as exc:
            if exc.status != 422 or "assignees" not in payload:
                raise
            # usually a login that isn't a collaborator on the repo; the owner is still named in the body
            log.warning("%s; leaving %s unassigned", exc, item.id)
        return await self._api(method, url, {k: v for k, v in payload.items() if k != "assignees"})

    def _login(self, item: Item) -> str | None:
        if not item.owner:
            return None
        names = [item.owner] + ([self.app.user_name, "me"] if item.is_mine(self.app.user_name) else [])
        logins = {k.strip().lower(): v for k, v in self.cfg.assignees.items()}
        return next((logins[n.strip().lower()] for n in names if n.strip().lower() in logins), None)

    def _priority_label(self, item: Item) -> str | None:
        return self.cfg.priority_labels.get(item.priority) if item.priority else None

    def _patch(self, item: Item, meeting: Meeting, issue: dict[str, Any]) -> dict[str, Any]:
        """The PATCH that brings ``issue`` in line with ``item``, with only the fields that differ."""
        want = wanted_state(item)
        patch: dict[str, Any] = {}
        if issue.get("title") != (title := _clip(item.title, TITLE_MAX)):
            patch["title"] = title
        if (issue.get("body") or "") != (body := issue_body(item, meeting, want)):
            patch["body"] = body
        labels_now = [lb if isinstance(lb, str) else lb.get("name", "") for lb in issue.get("labels") or []]
        labels = swap(labels_now, self.cfg.priority_labels.values(), self._priority_label(item))
        if labels != labels_now:
            patch["labels"] = labels
        logins_now = [a.get("login", "") for a in issue.get("assignees") or []]
        logins = swap(logins_now, self.cfg.assignees.values(), self._login(item))
        if logins != logins_now:
            patch["assignees"] = logins
        if want != last_pushed_state(issue.get("body")):
            patch["state"] = OPEN if want == OPEN else "closed"
            if want != OPEN:
                patch["state_reason"] = want
        return patch

    # -------------------------------------------------------------- sink API

    async def push_item(self, item: Item, meeting: Meeting) -> str:
        labels = list(self.cfg.labels)
        if prio := self._priority_label(item):
            labels.append(prio)
        payload: dict[str, Any] = {"title": _clip(item.title, TITLE_MAX), "body": issue_body(item, meeting, OPEN), "labels": labels}
        if login := self._login(item):
            payload["assignees"] = [login]
        issue = await self._write("POST", f"/repos/{self.cfg.repo}/issues", payload, item)
        if (want := wanted_state(item)) != OPEN:  # issues are created open; close one that's already settled
            try:
                await self._api("PATCH", issue["url"], {"state": "closed", "state_reason": want, "body": issue_body(item, meeting, want)})
            except SinkError as exc:  # the issue exists: failing now would create a duplicate on retry
                log.warning("github: created %s but couldn't close it: %s", issue["html_url"], exc)
        return issue["html_url"]

    async def update_item(self, item: Item, meeting: Meeting, ref: str) -> None:
        issue = await self._api("GET", issue_api_path(ref))
        if patch := self._patch(item, meeting, issue):
            await self._write("PATCH", issue["url"], patch, item)

    async def check(self) -> str:
        repo = await self._api("GET", f"/repos/{self.cfg.repo}")
        if repo.get("archived"):
            raise SinkError(f"{self.cfg.repo} is archived, so no issues can be opened there", retryable=False)
        if not repo.get("has_issues", True):
            raise SinkError(f"issues are disabled on {self.cfg.repo} (Settings → General → Features)", retryable=False)
        line = f"{repo.get('full_name', self.cfg.repo)} · API {self.cfg.api_version}"
        if (repo.get("permissions") or {}).get("push") is False:
            line += " · no push access: GitHub will drop labels and assignees"
        return line


def rate_limit_wait(resp: httpx.Response, attempt: int = 0) -> float | None:
    """Seconds to wait before retrying ``resp``, or ``None`` if it isn't a rate limit."""
    if resp.status_code not in (403, 429):
        return None
    if (ra := retry_after(resp)) is not None:
        return ra
    reset = resp.headers.get("x-ratelimit-reset", "")
    if resp.headers.get("x-ratelimit-remaining") == "0" and reset.isdigit():
        return max(0.0, int(reset) - time.time())
    if resp.status_code == 429 or "rate limit" in resp.text.lower():  # "You have exceeded a secondary rate limit…"
        return 60.0 * 2**attempt
    return None


def wanted_state(item: Item) -> str:
    if item.dismissed or item.status == ItemStatus.CANCELLED:
        return NOT_PLANNED
    return COMPLETED if item.status == ItemStatus.DONE else OPEN


def issue_body(item: Item, meeting: Meeting, state: str) -> str:
    marker = f"\n\n<!-- voicestream meeting={meeting.id} item={item.id} state={state} -->"
    return _clip(item_markdown(item, meeting), BODY_MAX - len(marker)) + marker


def last_pushed_state(body: str | None) -> str:
    """The state voicestream last pushed, from the body's marker (``open`` if it's gone)."""
    m = _MARKER.search(body or "")
    return m["state"] if m and m["state"] in (COMPLETED, NOT_PLANNED) else OPEN


def issue_api_path(ref: str) -> str:
    """``https://github.com/acme/roadmap/issues/42`` → ``/repos/acme/roadmap/issues/42``."""
    parts = urlsplit(ref).path.strip("/").split("/")
    if len(parts) < 4 or parts[-2] != "issues" or not parts[-1].isdigit():
        raise SinkError(f"github: not an issue URL: {ref}", retryable=False)
    owner, repo, _, number = parts[-4:]
    return f"/repos/{owner}/{repo}/issues/{number}"


def swap(current: list[str], managed: Iterable[str], want: str | None) -> list[str]:
    """``current`` without the ``managed`` values other than ``want``, plus ``want``.

    Case-insensitive (GitHub labels and logins are) and order-preserving, so values a
    person added in GitHub survive."""
    drop = {m.lower() for m in managed} - ({want.lower()} if want else set())
    out = [c for c in current if c.lower() not in drop]
    if want and want.lower() not in {c.lower() for c in out}:
        out.append(want)
    return out


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
