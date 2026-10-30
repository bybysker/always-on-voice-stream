"""Connecting to an MCP server by signing in in the browser (OAuth 2.1 + PKCE, as the MCP spec has it),
against a fake Promethee: its MCP server, its authorization server and the browser's way back."""

from __future__ import annotations

import base64
import hashlib
import json
import stat
import sys
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from voicestream.config import Config
from voicestream.models import Item, ItemKind, Meeting
from voicestream.sinks.base import SinkError
from voicestream.sinks.oauth import OAuthError, SignIn, TokenStore, discover
from voicestream.sinks.promethee import PrometheeConfig, PrometheeSink

MCP = "https://mcp.promethee.io/mcp"
AUTH = "https://auth.promethee.io"
BACK = "http://127.0.0.1:5555/oauth/promethee/callback"
CREATE = {"name": "create_task", "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]}}


class FakePromethee:
    """The MCP server (401 without a valid token) and its authorization server."""

    def __init__(self, *, metadata: bool = True, expires_in: int = 3600) -> None:
        self.metadata = metadata
        self.expires_in = expires_in
        self.clients: list[dict[str, Any]] = []
        self.codes: dict[str, str] = {}  # code -> PKCE challenge
        self.valid: set[str] = set()
        self.issued = 0
        self.token_forms: list[dict[str, list[str]]] = []
        self.created: list[dict[str, Any]] = []

    def _tokens(self) -> httpx.Response:
        self.issued += 1
        access = f"at-{self.issued}"
        self.valid = {access}
        return httpx.Response(200, json={"access_token": access, "token_type": "Bearer", "expires_in": self.expires_in,
                                         "refresh_token": f"rt-{self.issued}"})

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url).split("?")[0]
        if url == MCP:
            if request.headers.get("authorization", "").removeprefix("Bearer ") not in self.valid:
                header = f'Bearer resource_metadata="https://mcp.promethee.io/.well-known/oauth-protected-resource/mcp"'
                return httpx.Response(401, headers={"WWW-Authenticate": header} if self.metadata else {})
            msg = json.loads(request.content)
            if "id" not in msg:
                return httpx.Response(202)
            result: Any = {"protocolVersion": "2025-06-18", "serverInfo": {"name": "Promethee"}}
            if msg["method"] == "tools/list":
                result = {"tools": [CREATE]}
            elif msg["method"] == "tools/call":
                self.created.append(msg["params"]["arguments"])
                result = {"structuredContent": {"id": "T-1"}}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "result": result}, headers={"Mcp-Session-Id": "s1"})
        if url == "https://mcp.promethee.io/.well-known/oauth-protected-resource/mcp" and self.metadata:
            return httpx.Response(200, json={"resource": MCP, "authorization_servers": [AUTH], "scopes_supported": ["tasks"]})
        if url == f"{AUTH}/.well-known/oauth-authorization-server" and self.metadata:
            return httpx.Response(200, json={"issuer": AUTH, "authorization_endpoint": f"{AUTH}/authorize",
                                             "token_endpoint": f"{AUTH}/token", "registration_endpoint": f"{AUTH}/register",
                                             "code_challenge_methods_supported": ["S256"]})
        if url in (f"{AUTH}/register", "https://mcp.promethee.io/register"):
            body = json.loads(request.content)
            self.clients.append(body)
            return httpx.Response(201, json={"client_id": f"client-{len(self.clients)}", **body})
        if url in (f"{AUTH}/token", "https://mcp.promethee.io/token"):
            form = parse_qs(request.content.decode())
            self.token_forms.append(form)
            if form["grant_type"] == ["authorization_code"]:
                verifier = form["code_verifier"][0]
                challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                if self.codes.pop(form["code"][0], None) != challenge:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                return self._tokens()
            if form["grant_type"] == ["refresh_token"] and form["refresh_token"] == [f"rt-{self.issued}"]:
                return self._tokens()
            return httpx.Response(400, json={"error": "invalid_grant"})
        return httpx.Response(404)

    def sign_in(self, page: str) -> tuple[str, str]:
        """The person signs in on ``page``; returns the (state, code) the browser brings back."""
        q = {k: v[0] for k, v in parse_qs(urlsplit(page).query).items()}
        code = f"code-{len(self.codes) + 1}"
        self.codes[code] = q["code_challenge"]
        return q["state"], code


@pytest.fixture
def fake() -> FakePromethee:
    return FakePromethee()


def signin(cfg: Config, fake: FakePromethee) -> SignIn:
    return SignIn(TokenStore(cfg.data_path, "promethee"), MCP, transport=httpx.MockTransport(fake.handler))


async def test_discovery_follows_the_servers_own_metadata(fake: FakePromethee) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)) as http:
        meta = await discover(http, MCP)
    assert meta == {"resource": MCP, "issuer": AUTH, "authorization_endpoint": f"{AUTH}/authorize",
                    "token_endpoint": f"{AUTH}/token", "registration_endpoint": f"{AUTH}/register", "scope": "tasks"}


async def test_without_metadata_the_specs_defaults_are_used() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(FakePromethee(metadata=False).handler)) as http:
        meta = await discover(http, MCP)
    assert meta["authorization_endpoint"] == "https://mcp.promethee.io/authorize"
    assert meta["token_endpoint"] == "https://mcp.promethee.io/token"


async def test_sign_in_end_to_end(cfg: Config, fake: FakePromethee) -> None:
    s = signin(cfg, fake)
    page = await s.start(BACK)
    q = {k: v[0] for k, v in parse_qs(urlsplit(page).query).items()}
    assert page.startswith(f"{AUTH}/authorize?")
    assert q["client_id"] == "client-1" and q["redirect_uri"] == BACK and q["code_challenge_method"] == "S256"
    assert q["resource"] == MCP and q["scope"] == "tasks" and q["response_type"] == "code"
    assert fake.clients[0]["redirect_uris"] == [BACK] and fake.clients[0]["token_endpoint_auth_method"] == "none"

    await s.finish(*fake.sign_in(page))
    saved = s.store.load()
    assert saved is not None and saved["access_token"] == "at-1" and saved["refresh_token"] == "rt-1"
    assert s.store.connected()
    if sys.platform != "win32":
        assert stat.S_IMODE(s.store.path.stat().st_mode) == 0o600  # tokens: yours only
    assert fake.token_forms[0]["resource"] == [MCP]

    # the client registered once is kept for the same way back
    await s.start(BACK)
    assert len(fake.clients) == 1
    await s.start("http://127.0.0.1:6666/oauth/promethee/callback")  # the app on another port
    assert len(fake.clients) == 2


async def test_a_wrong_or_replayed_state_is_refused(cfg: Config, fake: FakePromethee) -> None:
    s = signin(cfg, fake)
    page = await s.start(BACK)
    state, code = fake.sign_in(page)
    with pytest.raises(OAuthError, match="unknown or too old"):
        await s.finish("forged", code)
    with pytest.raises(OAuthError, match="not completed .access_denied."):
        await s.finish(state, None, "access_denied")
    with pytest.raises(OAuthError, match="unknown or too old"):  # one use only
        await s.finish(state, code)
    assert not s.store.connected()


async def test_the_sink_uses_the_sign_in_and_renews_it(cfg: Config, fake: FakePromethee) -> None:
    s = signin(cfg, fake)
    await s.finish(*fake.sign_in(await s.start(BACK)))
    sink = PrometheeSink(PrometheeConfig(enabled=True, review=False), cfg)
    sink._client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    meeting = Meeting(title="Weekly")
    item = Item(id="A1", kind=ItemKind.ACTION, title="Send the deck")

    assert await sink.push_item(item, meeting) == "T-1"
    assert fake.created == [{"title": "Send the deck"}]

    # it is about to expire: renewed before use
    data = s.store.load()
    assert data is not None
    s.store.save({**data, "expires_at": time.time() + 10})
    assert "create_task" in await sink.check()
    assert s.store.load()["access_token"] == "at-2"  # type: ignore[index]

    # the server forgot it early: one refusal, renewed, asked again
    fake.valid = set()
    sink.mcp.session_id = None
    assert "create_task" in await sink.check()
    assert s.store.load()["access_token"] == "at-3"  # type: ignore[index]


async def test_not_connected_says_where_to_connect(cfg: Config, meeting: Meeting) -> None:
    sink = PrometheeSink(PrometheeConfig(enabled=True), cfg)
    sink._client = httpx.AsyncClient(transport=httpx.MockTransport(FakePromethee().handler))
    with pytest.raises(SinkError, match="not connected to Promethee: click Connect in Settings → Your tools") as exc:
        await sink.check()
    assert not exc.value.retryable
