"""Shared HTTP plumbing for sinks: one lazily-created client per sink, retries
with ``Retry-After`` support, and status codes mapped to :class:`SinkError`."""

from __future__ import annotations

import asyncio
import email.utils
import logging
import random
import time
from typing import Any

import httpx

from .base import SinkError

log = logging.getLogger(__name__)

RETRYABLE = {408, 429, 500, 502, 503, 504, 529}
MAYBE_EXECUTED = {500, 502, 503, 504}
"""A non-idempotent request that got one of these may have been applied: retrying could duplicate it.
(429 and 529 "overloaded" are rejected before processing, so they're safe to retry.)"""
IDEMPOTENT = {"GET", "HEAD", "DELETE", "PUT", "OPTIONS"}
NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
"""Transport errors raised before the request reached the server: always safe to retry. Any other
(read timeout, connection dropped mid-answer…) may come after the server has applied it."""
UNCERTAIN = (
    "it may have been applied anyway, so it isn't retried (that could make a duplicate): check {where}, "
    "then push it again (dashboard Push button or `voicestream push`) if it's missing"
)


def retry_after(resp: httpx.Response) -> float | None:
    ra = resp.headers.get("retry-after")
    if not ra:
        return None
    try:
        return max(0.0, float(ra))
    except ValueError:
        try:
            return max(0.0, email.utils.parsedate_to_datetime(ra).timestamp() - time.time())
        except (TypeError, ValueError):
            return None


class HttpSinkMixin:
    """Gives a sink ``self._client`` (replaceable in tests) and :meth:`request`."""

    _client: httpx.AsyncClient | None = None
    timeout_s: float = 30.0

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_s, connect=10.0))
        return self._client

    async def request(
        self,
        method: str,
        url: str,
        *,
        retries: int = 3,
        write_safe: bool = False,
        ok: tuple[int, ...] = (200, 201, 202, 204),
        **kw: Any,
    ) -> httpx.Response:
        """Send a request, retrying rate limits and (for idempotent calls) server errors.

        ``write_safe=True`` marks a non-idempotent request as safe to retry on 5xx
        (e.g. it carries an idempotency key). Raises :class:`SinkError` on failure; a
        non-retryable one when any other request may have been applied (a 5xx, or no answer
        after it was sent), so the dispatcher doesn't resend it either.
        """
        method = method.upper()
        # the dispatcher retries a retryable SinkError later, so an uncertain create must not be one
        resend_ok = method in IDEMPOTENT or write_safe
        uncertain = UNCERTAIN.format(where=getattr(self, "name", None) or "the tool")
        resp: httpx.Response | None = None
        for attempt in range(retries + 1):
            try:
                resp = await self.client().request(method, url, **kw)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if not resend_ok and not isinstance(exc, NOT_SENT):
                    raise SinkError(f"{method} {url}: {exc!r} — {uncertain}", retryable=False, uncertain=True) from exc
                if attempt == retries:
                    raise SinkError(f"{method} {url}: {exc!r}", retryable=True) from exc
                await asyncio.sleep(min(30.0, 2**attempt + random.random()))
                continue
            if resp.status_code in ok:
                return resp
            retryable = resp.status_code in RETRYABLE
            if resp.status_code in MAYBE_EXECUTED and not resend_ok:
                raise SinkError(
                    f"{method} {url} → HTTP {resp.status_code}: {resp.text[:400]} — {uncertain}", retryable=False, uncertain=True
                )
            if not retryable or attempt == retries:
                break
            await asyncio.sleep(min(60.0, retry_after(resp) or (2**attempt + random.random())))
        assert resp is not None
        raise SinkError(
            f"{method} {url} → HTTP {resp.status_code}: {resp.text[:400]}",
            retryable=resp.status_code in RETRYABLE,
        )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
