"""A small client for remote MCP servers (the Streamable HTTP transport), enough for a sink to call a
server's tools: ``initialize``, ``tools/list`` and ``tools/call``.

Each JSON-RPC request is a POST; the server answers with JSON or with a short event stream
(``text/event-stream``) that carries the answer. The session id it hands out at ``initialize``
(``Mcp-Session-Id``) goes on every later request; when the server forgets it (HTTP 404), the
client starts a new session and asks again — once, and only for requests that change nothing:
a ``tools/call`` that may have been applied is never sent twice.
"""

from __future__ import annotations

import itertools
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .base import SinkError

log = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "voicestream", "version": "1"}
_ACCEPT = "application/json, text/event-stream"


class McpError(SinkError):
    """The server answered with a JSON-RPC error, or a tool reported one (``isError``)."""


class McpClient:
    """``request`` is the sink's :meth:`HttpSinkMixin.request` (its retries and error mapping);
    ``headers(renew)`` gives the auth headers for each request (read when sent: a key saved meanwhile
    counts). When the server refuses them, it is asked once more with ``renew=True`` (a sign-in's
    token refreshed), then the request is sent again: a refused request was never applied."""

    def __init__(self, url: str, request: Callable[..., Any], headers: Callable[[bool], Awaitable[dict[str, str]]], *,
                 name: str = "mcp") -> None:
        self.url = url
        self._request = request
        self._headers = headers
        self.name = name
        self.session_id: str | None = None
        self.protocol: str = PROTOCOL_VERSION
        self.server: dict[str, Any] = {}
        self._ids = itertools.count(1)
        self._tools: list[dict[str, Any]] | None = None

    # ------------------------------------------------------------------ transport

    async def _all_headers(self, renew: bool = False) -> dict[str, str]:
        headers = {"Accept": _ACCEPT, "Content-Type": "application/json", **await self._headers(renew)}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
            headers["MCP-Protocol-Version"] = self.protocol
        return headers

    async def _post(self, message: dict[str, Any], *, safe: bool) -> httpx.Response:
        resp = await self._send(message, safe=safe, renew=False)
        if resp.status_code == 401:
            resp = await self._send(message, safe=safe, renew=True)
        return resp

    async def _send(self, message: dict[str, Any], *, safe: bool, renew: bool) -> httpx.Response:
        return await self._request(
            "POST", self.url, json=message, headers=await self._all_headers(renew), write_safe=safe, ok=(200, 202, 204, 401, 403, 404)
        )

    async def _rpc(self, method: str, params: dict[str, Any] | None = None, *, safe: bool = True) -> dict[str, Any]:
        if self.session_id is None and method != "initialize":
            await self.initialize()
        msg_id = next(self._ids)
        message = {"jsonrpc": "2.0", "id": msg_id, "method": method, **({"params": params} if params is not None else {})}
        resp = await self._post(message, safe=safe)
        if resp.status_code == 404 and self.session_id and method != "initialize":
            # the server dropped our session (it restarted, or it expired): a new one, then the same question
            if not safe:
                raise SinkError(f"{self.name}: the server ended the session before {method} could be sent — "
                                "it wasn't applied; it will be tried again", retryable=True)
            self.session_id = None
            await self.initialize()
            resp = await self._post(message, safe=safe)
        if resp.status_code in (401, 403):
            raise SinkError(f"{self.name}: the server refused the credentials (HTTP {resp.status_code}): {resp.text[:200]}",
                            retryable=False)
        if resp.status_code == 404:
            raise SinkError(f"{self.name}: no MCP server at {self.url} (HTTP 404)", retryable=False)
        answer = _answer(resp, msg_id)
        if answer is None:
            raise SinkError(f"{self.name}: no answer to {method} from {self.url}: {resp.text[:200]}",
                            retryable=safe, uncertain=not safe)
        if "error" in answer:
            err = answer["error"] or {}
            raise McpError(f"{self.name}: {method} failed: {err.get('message') or err} (code {err.get('code')})", retryable=False)
        return answer.get("result") or {}

    # ------------------------------------------------------------------ protocol

    async def initialize(self) -> dict[str, Any]:
        self.session_id = None
        msg_id = next(self._ids)
        resp = await self._post({
            "jsonrpc": "2.0", "id": msg_id, "method": "initialize",
            "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO},
        }, safe=True)
        if resp.status_code in (401, 403):
            raise SinkError(f"{self.name}: the server refused the credentials (HTTP {resp.status_code}): {resp.text[:200]}",
                            retryable=False)
        if resp.status_code == 404:
            raise SinkError(f"{self.name}: no MCP server at {self.url} (HTTP 404)", retryable=False)
        answer = _answer(resp, msg_id)
        if answer is None or "error" in answer:
            raise SinkError(f"{self.name}: the server didn't accept the connection: {(answer or {}).get('error') or resp.text[:200]}",
                            retryable=answer is None)
        result = answer.get("result") or {}
        self.session_id = resp.headers.get("mcp-session-id")
        self.protocol = str(result.get("protocolVersion") or PROTOCOL_VERSION)
        self.server = result.get("serverInfo") or {}
        await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, safe=True)
        return result

    async def tools(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        """The server's tools (``name``, ``description``, ``inputSchema``), every page of them."""
        if self._tools is not None and not refresh:
            return self._tools
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(50):
            result = await self._rpc("tools/list", {"cursor": cursor} if cursor else {})
            tools += [t for t in result.get("tools") or [] if isinstance(t, dict) and t.get("name")]
            cursor = result.get("nextCursor")
            if not cursor:
                break
        self._tools = tools
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any], *, safe: bool = False) -> dict[str, Any]:
        """Call a tool; returns its result (``content``, maybe ``structuredContent``). ``safe``: calling it
        twice does no harm (an update that sets values), so a lost answer may be retried."""
        result = await self._rpc("tools/call", {"name": name, "arguments": arguments}, safe=safe)
        if result.get("isError"):
            raise McpError(f"{self.name}: {name} failed: {result_text(result)[:400] or 'no detail'}", retryable=False)
        return result

    def close(self) -> None:
        self.session_id = None
        self._tools = None


def _answer(resp: httpx.Response, msg_id: int) -> dict[str, Any] | None:
    """The JSON-RPC response with id ``msg_id`` in a JSON body or an event stream; ``None`` when absent."""
    if resp.status_code in (202, 204) or not resp.content:
        return None
    if resp.headers.get("content-type", "").startswith("text/event-stream"):
        messages = []
        for event in resp.text.replace("\r\n", "\n").split("\n\n"):
            data = "\n".join(line[5:].lstrip() for line in event.split("\n") if line.startswith("data:"))
            if data:
                try:
                    messages.append(json.loads(data))
                except ValueError:
                    log.debug("mcp: skipping an event that isn't JSON: %r", data[:200])
    else:
        try:
            body = resp.json()
        except ValueError:
            return None
        messages = body if isinstance(body, list) else [body]
    for m in messages:
        if isinstance(m, dict) and m.get("id") == msg_id and ("result" in m or "error" in m):
            return m
    return None


def result_text(result: dict[str, Any]) -> str:
    """The text parts of a tool result, joined."""
    return "\n".join(str(c.get("text") or "") for c in result.get("content") or [] if isinstance(c, dict) and c.get("type") == "text").strip()
