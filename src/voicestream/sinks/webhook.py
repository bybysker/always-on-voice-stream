"""Generic webhook sink: item events and meeting notes, POSTed as JSON.

Use it with Zapier, Make or n8n catch hooks, or with your own server. Each delivery
is one JSON object::

    {"event": "item.created" | "item.updated" | "meeting.notes",
     "item": {...},           # Item.model_dump(mode="json"); item events only
     "item_markdown": "...",  # the item rendered as Markdown; item events only
     "ref": "...",            # what item.created returned; item.updated only
     "meeting": {"id", "title", "started_at", "ended_at", "participants", "tldr"},
     "notes_markdown": "..."} # meeting.notes only

Headers:

* ``X-Voicestream-Event``: the event name.
* ``X-Voicestream-Delivery``: stable per event, meeting, item and revision, so a
  receiver can drop duplicate deliveries.
* ``X-Voicestream-Signature: sha256=<hex>``: only when ``$VOICESTREAM_WEBHOOK_SECRET``
  is set. It is the HMAC-SHA256 of the raw request body under that secret. Check it
  against the exact bytes received, before parsing::

      expected = "sha256=" + hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
      ok = hmac.compare_digest(expected, headers["X-Voicestream-Signature"])

Any 2xx is success. If the response is a JSON object with a ``url`` (failing that,
an ``id``), that becomes the item's ref. Otherwise the ref is ``<scheme>://<host>#<item
id>``. The full URL never appears in refs or errors, because a catch-hook URL is a
credential in itself. For the same reason it is best kept in
``$VOICESTREAM_WEBHOOK_URL`` rather than in the config file.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from typing import Any, ClassVar, Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator

from .. import __version__
from ..models import Item, Meeting
from .base import Sink, SinkConfig, SinkError, env_secret, item_markdown
from .http import HttpSinkMixin

log = logging.getLogger(__name__)

MEETING_FIELDS = {"id", "title", "started_at", "ended_at", "participants", "tldr", "language"}


class WebhookConfig(SinkConfig):
    kinds: list[Literal["action", "decision", "question", "note"]] = Field(default=["action", "decision", "question", "note"])
    url: str | None = None
    """Endpoint to POST to. If unset, it's read from ``url_env``."""
    url_env: str = "VOICESTREAM_WEBHOOK_URL"
    secret_env: str = "VOICESTREAM_WEBHOOK_SECRET"
    """Env var holding the signing secret. Deliveries are unsigned when it isn't set."""

    @field_validator("url")
    @classmethod
    def _http(cls, url: str | None) -> str | None:
        if url is not None and not url.startswith(("https://", "http://")):
            raise ValueError("url must start with https:// or http://")
        return url


class WebhookSink(HttpSinkMixin, Sink):
    name = "webhook"
    Config: ClassVar[type[SinkConfig]] = WebhookConfig
    supports_update = True
    supports_notes = True
    cfg: WebhookConfig

    def _url(self) -> str:
        url = self.cfg.url or env_secret(self.cfg, "url_env", "VOICESTREAM_WEBHOOK_URL")
        if not url.startswith(("https://", "http://")):
            raise SinkError(f"{self.cfg.url_env} must start with https:// or http://", retryable=False)
        return url

    async def _deliver(self, event: str, delivery: str, payload: dict[str, Any]) -> Any:
        """POST one event and return the response's JSON (``None`` if it has none)."""
        url = self._url()
        body = json.dumps({"event": event, **payload}, ensure_ascii=False, separators=(",", ":")).encode()
        headers = {
            "Content-Type": "application/json",
            "User-Agent": f"voicestream/{__version__}",
            "X-Voicestream-Event": event,
            "X-Voicestream-Delivery": delivery,
        }
        if secret := os.environ.get(self.cfg.secret_env):
            headers["X-Voicestream-Signature"] = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        try:
            resp = await self.request("POST", url, content=body, headers=headers)
        except SinkError as exc:
            raise redacted(exc, url) from None
        try:
            return resp.json()
        except ValueError:
            return None

    def _item_payload(self, item: Item, meeting: Meeting) -> dict[str, Any]:
        return {
            "item": item.model_dump(mode="json"),
            "item_markdown": item_markdown(item, meeting),
            "meeting": meeting.model_dump(mode="json", include=MEETING_FIELDS),
        }

    # -------------------------------------------------------------- sink API

    async def push_item(self, item: Item, meeting: Meeting) -> str:
        delivery = delivery_id("item.created", meeting.id, item.id, str(item.revision))
        data = await self._deliver("item.created", delivery, self._item_payload(item, meeting))
        return _ref(data, f"{origin(self._url())}#{item.id}")

    async def update_item(self, item: Item, meeting: Meeting, ref: str) -> None:
        delivery = delivery_id("item.updated", meeting.id, item.id, str(item.revision))
        await self._deliver("item.updated", delivery, {**self._item_payload(item, meeting), "ref": ref})

    async def push_notes(self, meeting: Meeting, notes_markdown: str) -> str | None:
        delivery = delivery_id("meeting.notes", meeting.id, hashlib.sha256(notes_markdown.encode()).hexdigest())
        payload = {"meeting": meeting.model_dump(mode="json", include=MEETING_FIELDS), "notes_markdown": notes_markdown}
        return _ref(await self._deliver("meeting.notes", delivery, payload), f"{origin(self._url())}#{meeting.id}")

    async def check(self) -> str:
        url = self._url()
        what = ", ".join(self.cfg.kinds) or "no items"
        if self.cfg.notes:
            what += " + notes"
        signed = "signed" if os.environ.get(self.cfg.secret_env) else f"unsigned: set {self.cfg.secret_env} to sign"
        return f"POSTs {what} to {origin(url)}/… ({signed}); not test-posted"


def origin(url: str) -> str:
    """``scheme://host[:port]`` of ``url``, which is safe to show, unlike its path or credentials."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc.rpartition('@')[2]}"


def redacted(exc: SinkError, url: str) -> SinkError:
    """``exc`` with the secret ``url`` cut down to its origin."""
    return SinkError(str(exc).replace(url, f"{origin(url)}/…"), retryable=exc.retryable)


def delivery_id(*parts: str) -> str:
    return hashlib.sha256("/".join(parts).encode()).hexdigest()[:32]


def _ref(data: Any, fallback: str) -> str:
    if isinstance(data, dict):
        if isinstance(url := data.get("url"), str) and url.startswith(("https://", "http://")):
            return url
        if isinstance(rid := data.get("id"), str | int) and str(rid):
            return str(rid)
    return fallback
