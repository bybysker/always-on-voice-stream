"""Slack sink: one digest message per meeting, posted through an incoming webhook.

The webhook URL is the credential (anyone holding it can post), so it's read from
``$SLACK_WEBHOOK_URL`` and kept out of refs and error messages. A webhook can only
post: it can't edit or read anything back. So there are no updates, and ``check``
validates the URL's shape instead of posting a test message to the channel.

By default nothing is posted per item (``kinds = []``). When a meeting ends, a
Block Kit digest goes out with:

* the TL;DR;
* action items, each with its owner (an @-mention via ``users``), due date and
  agent status, linked to the issue or task another sink created for it;
* decisions;
* links to any web copy of the notes.

The digest stays within Slack's limits (≤50 blocks, ≤3000 characters per section,
≤150-character header) and carries a plain ``text`` fallback for notifications.
Meetings with nothing to report are skipped, and a meeting's digest is posted only
once. Set ``kinds`` to also get a short message per item as it's pushed.

Slack reports errors as a plain-text code such as ``invalid_payload``,
``no_service`` or ``channel_is_archived``. If Slack rejects the blocks, the message
is retried once as plain text. A revoked webhook or a gone channel is not retryable.
"""

from __future__ import annotations

import logging
import re
from typing import Any, ClassVar, Literal

from pydantic import Field

from ..models import AgentStatus, Item, ItemKind, ItemStatus, Meeting
from .base import Sink, SinkConfig, SinkError, env_secret
from .http import HttpSinkMixin
from .webhook import redacted

log = logging.getLogger(__name__)

SECTION_MAX = 3000
HEADER_MAX = 150
BLOCKS_MAX = 50
TEXT_MAX = 3000
"""Fallback ``text``: Slack truncates at 40k, but notifications show far less."""
URL_SHAPE = re.compile(r"^https://hooks\.slack(-gov)?\.com/services/[\w-]+/[\w-]+/[\w-]+$")
_BLOCK_ERRORS = {"invalid_blocks", "invalid_blocks_format", "msg_blocks_too_long", "invalid_payload"}
_HINTS = {
    "no_service": "the webhook was removed, or the URL is wrong: create a new one",
    "invalid_token": "the webhook was removed, or the URL is wrong: create a new one",
    "no_team": "the workspace is gone",
    "channel_not_found": "the webhook's channel was deleted",
    "channel_is_archived": "the webhook's channel is archived",
    "action_prohibited": "a workspace admin restricted posting to that channel",
}
AGENT_MARKS = {
    AgentStatus.QUEUED: "🤖 queued",
    AgentStatus.RUNNING: "🤖 working on it",
    AgentStatus.DONE: "🤖 done",
    AgentStatus.FAILED: "🤖 failed",
}
KIND_LABELS = {ItemKind.ACTION: "Action item", ItemKind.DECISION: "Decision", ItemKind.QUESTION: "Open question", ItemKind.NOTE: "Note"}
_NO_LINK_SINKS = {"slack", "webhook"}
"""Their refs aren't pages a reader can open."""


class SlackConfig(SinkConfig):
    kinds: list[Literal["action", "decision", "question", "note"]] = Field(default_factory=list)
    """Item kinds that also get a message of their own (default: none, just the digest)."""
    url_env: str = "SLACK_WEBHOOK_URL"
    users: dict[str, str] = Field(default_factory=dict)
    """Owner name as spoken (case-insensitive) → Slack member id (``U…``), to @-mention
    owners. Your own items also match your ``user_name`` or ``me``."""


class SlackSink(HttpSinkMixin, Sink):
    name = "slack"
    Config: ClassVar[type[SinkConfig]] = SlackConfig
    supports_notes = True
    cfg: SlackConfig

    def _url(self) -> str:
        url = env_secret(self.cfg, "url_env", "SLACK_WEBHOOK_URL")
        if not url.startswith("https://"):
            raise SinkError(f"{self.cfg.url_env} must be an https:// Slack webhook URL", retryable=False)
        return url

    async def _post(self, payload: dict[str, Any]) -> None:
        url = self._url()
        try:
            resp = await self.request("POST", url, json=payload, ok=(200, 400, 401, 403, 404, 410))
        except SinkError as exc:
            raise redacted(exc, url) from None
        if resp.status_code == 200:
            return
        code = resp.text.strip()[:100] or f"HTTP {resp.status_code}"
        if code in _BLOCK_ERRORS and "blocks" in payload:
            log.warning("slack rejected the message blocks (%s); posting it as plain text", code)
            await self._post({"text": payload["text"]})
            return
        hint = _HINTS.get(code)
        raise SinkError(f"slack: {code} (HTTP {resp.status_code})" + (f" — {hint}" if hint else ""), retryable=False)

    def _owner(self, owner: str, item: Item) -> str:
        mine = item.is_mine(self.app.user_name)
        names = [owner] + ([self.app.user_name, "me"] if mine else [])
        users = {k.strip().lower(): v for k, v in self.cfg.users.items()}
        if uid := next((users[n.strip().lower()] for n in names if n.strip().lower() in users), None):
            return f"<@{uid}>"
        return f"@{esc(_clip(self.app.user_name if mine else owner, 100))}"  # "I"/"me" read as your name

    def _line(self, item: Item) -> str:
        title = esc(_clip(item.title, 300))
        if link := next((r for s, r in item.pushed.items() if s not in _NO_LINK_SINKS and r.startswith(("https://", "http://"))), None):
            title = f"<{link}|{title}>"
        if item.status == ItemStatus.CANCELLED:
            title = f"~{title}~"
        elif item.status == ItemStatus.DONE:
            title = f"✅ {title}"
        bits = [title]
        if item.owner:
            bits.append(self._owner(item.owner, item))
        if due := item.due_date or item.due_text:
            bits.append(f"due {esc(_clip(due, 100))}")
        if item.agent and (mark := AGENT_MARKS.get(item.agent.status)):
            bits.append(mark)
        return _clip("• " + " · ".join(bits), SECTION_MAX)

    def digest(self, meeting: Meeting) -> dict[str, Any] | None:
        """The Block Kit message for ``meeting``, or ``None`` when there's nothing to report."""
        actions, decisions = meeting.items_of(ItemKind.ACTION), meeting.items_of(ItemKind.DECISION)
        if not (meeting.tldr or actions or decisions):
            return None
        title = meeting.title or f"Meeting on {meeting.started_at.astimezone():%a %d %b, %H:%M}"
        head = [
            {"type": "header", "text": {"type": "plain_text", "text": _clip(title, HEADER_MAX)}},
            _context(_when(meeting)),
        ]
        body = [_section(_clip(esc(meeting.tldr), SECTION_MAX))] if meeting.tldr else []
        listed: list[str] = []
        for heading, items in ((f"*Action items ({len(actions)})*", actions), (f"*Decisions ({len(decisions)})*", decisions)):
            if items:
                lines = [self._line(i) for i in items]
                body += [_section(chunk) for chunk in _chunks(heading, lines)]
                listed += [heading, *lines]
        links = [
            f"<{ref}|{sink.capitalize()}>"
            for sink, ref in meeting.notes_ref.items()
            if sink not in _NO_LINK_SINKS and ref.startswith(("https://", "http://"))
        ]
        tail = [_context("Full notes: " + " · ".join(links))] if links else []
        room = BLOCKS_MAX - len(head) - len(tail)
        if len(body) > room:
            body = body[: room - 1] + [_context("_…cut to fit Slack's 50-block limit._")]
        summary = f"{len(actions)} action item{'s' * (len(actions) != 1)}, {len(decisions)} decision{'s' * (len(decisions) != 1)}"
        # the fallback text doubles as the whole message if Slack rejects the blocks
        text = "\n".join([esc(f"{title}: {summary}" + (f". {meeting.tldr}" if meeting.tldr else "")), *listed])
        return {"text": _clip(text, TEXT_MAX), "blocks": head + body + tail}

    # -------------------------------------------------------------- sink API

    async def push_item(self, item: Item, meeting: Meeting) -> str:
        where = esc(meeting.title or "a meeting")
        await self._post({"text": f"*{KIND_LABELS[item.kind]}* from _{where}_\n{self._line(item)}"})
        return f"slack:{meeting.id}/{item.id}"

    async def push_notes(self, meeting: Meeting, notes_markdown: str) -> str | None:
        if ref := meeting.notes_ref.get(self.name):
            # e.g. a later ``voicestream push``: the posted digest can't be edited, and a second one is noise
            log.info("slack: digest for meeting %s already posted; not posting it again", meeting.id)
            return ref
        if (payload := self.digest(meeting)) is None:
            log.info("slack: nothing to report for meeting %s; no digest posted", meeting.id)
            return None
        await self._post(payload)
        return f"slack:{meeting.id}"

    async def check(self) -> str:
        if not URL_SHAPE.match(self._url()):
            raise SinkError(
                f"{self.cfg.url_env} doesn't look like an incoming-webhook URL (https://hooks.slack.com/services/T…/B…/…)",
                retryable=False,
            )
        what = "a digest after each meeting" if self.cfg.notes else "no digest (notes = false)"
        if self.cfg.kinds:
            what += f" + a message per {'/'.join(self.cfg.kinds)} item"
        return f"webhook URL looks valid (not test-posted, to keep the channel quiet) · {what}"


def esc(text: str) -> str:
    """Escape the three characters Slack's mrkdwn treats as markup."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _when(meeting: Meeting) -> str:
    # <!date^…> renders in each reader's own timezone; the text after | is the fallback
    fallback = meeting.started_at.astimezone().strftime("%Y-%m-%d %H:%M")
    bits = [f"<!date^{int(meeting.started_at.timestamp())}^{{date_short_pretty}} at {{time}}|{fallback}>"]
    if meeting.ended_at:
        bits.append(f"{max(1, round((meeting.ended_at - meeting.started_at).total_seconds() / 60))} min")
    if meeting.participants:
        bits.append(esc(_clip(", ".join(meeting.participants), 500)))
    return " · ".join(bits)


def _chunks(heading: str, lines: list[str]) -> list[str]:
    """``heading`` and ``lines`` joined into texts of at most ``SECTION_MAX`` characters."""
    chunks, current = [], heading
    for line in lines:
        if len(current) + 1 + len(line) > SECTION_MAX:
            chunks.append(current)
            current = line
        else:
            current += "\n" + line
    return chunks + [current]


def _section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _context(text: str) -> dict[str, Any]:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
