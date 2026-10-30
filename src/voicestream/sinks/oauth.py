"""Signing in to a remote MCP server in the browser, as the MCP authorization spec describes: OAuth 2.1
with PKCE, the server found from the MCP server itself, and Voicestream registering itself as a client
(dynamic client registration). There is no key to copy: you click Connect, sign in to the service in
your browser, and it sends you back to Voicestream.

1. :func:`discover` asks the MCP server where to sign in: the ``resource_metadata`` of its 401
   answer, else ``/.well-known/oauth-protected-resource``; then that authorization server's metadata
   (``/.well-known/oauth-authorization-server`` or ``openid-configuration``). A server that publishes
   neither gets the endpoints the 2025-03-26 spec gives by default (``/authorize``, ``/token``,
   ``/register`` on its own origin).
2. :class:`SignIn` registers Voicestream (once per return address: the app's port can change), then
   builds the sign-in page's address with a PKCE challenge and a ``state`` it checks on the way back.
3. The browser comes back to the app's ``/oauth/<service>/callback`` with a code, which
   :meth:`SignIn.finish` exchanges for tokens, saved in a file only you can read
   (``<data_dir>/oauth/<service>.json``).
4. :class:`TokenStore` hands sinks the access token, refreshed with the refresh token before it expires.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

from .base import SinkError

log = logging.getLogger(__name__)

CLIENT_NAME = "Voicestream"
FLOW_TTL_S = 15 * 60
"""How long a sign-in started in the app may take to come back."""
REFRESH_MARGIN_S = 120
_TIMEOUT = httpx.Timeout(20.0, connect=10.0)


class OAuthError(SinkError):
    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)


# ---------------------------------------------------------------------- discovery


def _origin(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


def _path(url: str) -> str:
    return urlsplit(url).path.rstrip("/")


def _resource_metadata_url(www_authenticate: str) -> str | None:
    for part in www_authenticate.replace("Bearer ", "", 1).split(","):
        key, _, value = part.strip().partition("=")
        if key.strip().lower() == "resource_metadata" and value:
            return value.strip().strip('"')
    return None


async def _get_json(http: httpx.AsyncClient, url: str) -> dict[str, Any] | None:
    try:
        resp = await http.get(url, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        log.debug("oauth: %s: %s", url, exc)
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


async def discover(http: httpx.AsyncClient, resource: str) -> dict[str, Any]:
    """Where to sign in for the MCP server at ``resource``: the authorization server's endpoints, and
    the scopes the server asks for."""
    origin, path = _origin(resource), _path(resource)
    prm_urls: list[str] = []
    try:
        probe = await http.post(resource, json={"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "voicestream", "version": "1"}}},
            headers={"Accept": "application/json, text/event-stream"})
    except httpx.HTTPError as exc:
        raise OAuthError(f"could not reach {resource}: {exc}") from exc
    if probe.status_code < 300:
        raise OAuthError(f"{resource} doesn't ask for a sign-in: it needs no connection, or a key instead")
    if url := _resource_metadata_url(probe.headers.get("www-authenticate", "")):
        prm_urls.append(url)
    prm_urls += [f"{origin}/.well-known/oauth-protected-resource{path}", f"{origin}/.well-known/oauth-protected-resource"]
    prm: dict[str, Any] = {}
    for url in dict.fromkeys(prm_urls):
        if (found := await _get_json(http, url)) is not None:
            prm = found
            break
    issuer = str((prm.get("authorization_servers") or [origin])[0]).rstrip("/")
    i_origin, i_path = _origin(issuer), _path(issuer)
    candidates = (
        [f"{i_origin}/.well-known/oauth-authorization-server{i_path}", f"{i_origin}/.well-known/openid-configuration{i_path}",
         f"{issuer}/.well-known/openid-configuration"]
        if i_path else [f"{i_origin}/.well-known/oauth-authorization-server", f"{i_origin}/.well-known/openid-configuration"]
    )
    meta: dict[str, Any] | None = None
    for url in candidates:
        if (meta := await _get_json(http, url)) is not None:
            break
    if meta is None:  # the 2025-03-26 spec's defaults
        meta = {"authorization_endpoint": f"{i_origin}/authorize", "token_endpoint": f"{i_origin}/token",
                "registration_endpoint": f"{i_origin}/register"}
    if not meta.get("authorization_endpoint") or not meta.get("token_endpoint"):
        raise OAuthError(f"{issuer} doesn't say where to sign in (no authorization or token endpoint)")
    methods = meta.get("code_challenge_methods_supported")
    if methods and "S256" not in methods:
        raise OAuthError(f"{issuer} doesn't support PKCE with S256, which a desktop app needs")
    scopes = prm.get("scopes_supported") or []
    return {
        "resource": resource,
        "issuer": issuer,
        "authorization_endpoint": meta["authorization_endpoint"],
        "token_endpoint": meta["token_endpoint"],
        "registration_endpoint": meta.get("registration_endpoint"),
        "scope": " ".join(str(s) for s in scopes) or None,
    }


async def register(http: httpx.AsyncClient, meta: dict[str, Any], redirect_uri: str) -> dict[str, Any]:
    """Voicestream as an OAuth client of that server (``client_id``, maybe ``client_secret``)."""
    endpoint = meta.get("registration_endpoint")
    if not endpoint:
        raise OAuthError(f"{meta['issuer']} doesn't let apps register themselves; it would need a client id for Voicestream")
    body = {
        "client_name": CLIENT_NAME,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    if meta.get("scope"):
        body["scope"] = meta["scope"]
    resp = await http.post(endpoint, json=body)
    if resp.status_code not in (200, 201):
        raise OAuthError(f"{meta['issuer']} refused to register Voicestream (HTTP {resp.status_code}): {resp.text[:200]}")
    data = resp.json()
    if not data.get("client_id"):
        raise OAuthError(f"{meta['issuer']} registered Voicestream without a client id")
    return {"client_id": data["client_id"], "client_secret": data.get("client_secret"), "redirect_uri": redirect_uri}


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


async def _token(http: httpx.AsyncClient, meta: dict[str, Any], client: dict[str, Any], form: dict[str, str]) -> dict[str, Any]:
    form = {**form, "client_id": client["client_id"], "resource": meta["resource"]}
    auth = (client["client_id"], client["client_secret"]) if client.get("client_secret") else None
    try:
        resp = await http.post(meta["token_endpoint"], data=form, auth=auth, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise SinkError(f"could not reach {meta['token_endpoint']}: {exc}", retryable=True) from exc
    if resp.status_code != 200:
        raise OAuthError(f"{meta['issuer']} refused the sign-in (HTTP {resp.status_code}): {resp.text[:200]}")
    data = resp.json()
    if not data.get("access_token"):
        raise OAuthError(f"{meta['issuer']} gave no access token")
    return data


# --------------------------------------------------------------------- the tokens


class TokenStore:
    """One service's sign-in, in ``<data_dir>/oauth/<service>.json`` (0600)."""

    def __init__(self, data_path: Path, service: str) -> None:
        self.path = data_path / "oauth" / f"{service}.json"
        self.service = service

    def load(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def save(self, data: dict[str, Any]) -> None:
        from ..app.files import editing, write_private

        with editing(self.path):
            write_private(self.path, json.dumps(data, indent=1))

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)

    def connected(self) -> bool:
        data = self.load()
        return bool(data and (data.get("refresh_token") or data.get("access_token")))

    def keep(self, data: dict[str, Any], tokens: dict[str, Any]) -> dict[str, Any]:
        expires = tokens.get("expires_in")
        data = {
            **data,
            "access_token": tokens["access_token"],
            "refresh_token": tokens.get("refresh_token") or data.get("refresh_token"),
            "expires_at": time.time() + float(expires) if expires else None,
        }
        self.save(data)
        return data

    async def access_token(self, http: httpx.AsyncClient, *, force_refresh: bool = False) -> str:
        """A token that works now: refreshed when it is about to expire (or ``force_refresh``)."""
        data = self.load()
        if not data or not data.get("access_token"):
            raise OAuthError(f"not connected to {self.service.capitalize()}: click Connect in Settings → Your tools")
        expires_at = data.get("expires_at")
        due = force_refresh or (expires_at is not None and expires_at - time.time() < REFRESH_MARGIN_S)
        if due and data.get("refresh_token"):
            tokens = await _token(http, data["meta"], data["client"], {"grant_type": "refresh_token", "refresh_token": data["refresh_token"]})
            data = self.keep(data, tokens)
        elif due and force_refresh:
            raise OAuthError(f"the {self.service.capitalize()} sign-in has expired: click Connect in Settings → Your tools")
        return str(data["access_token"])


# ----------------------------------------------------------------------- sign-in


@dataclass
class _Flow:
    meta: dict[str, Any]
    client: dict[str, Any]
    verifier: str
    started: float = field(default_factory=time.monotonic)


class SignIn:
    """The sign-ins started in the app and not yet back, by ``state``."""

    def __init__(self, store: TokenStore, resource: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.store = store
        self.resource = resource
        self.transport = transport
        self._flows: dict[str, _Flow] = {}

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=_TIMEOUT, transport=self.transport, follow_redirects=True)

    async def start(self, redirect_uri: str) -> str:
        """The address of the service's sign-in page."""
        async with self._http() as http:
            meta = await discover(http, self.resource)
            saved = self.store.load() or {}
            client = saved.get("client")
            if not client or client.get("redirect_uri") != redirect_uri or (saved.get("meta") or {}).get("issuer") != meta["issuer"]:
                client = await register(http, meta, redirect_uri)
        verifier, challenge = pkce()
        state = secrets.token_urlsafe(24)
        now = time.monotonic()
        self._flows = {s: f for s, f in self._flows.items() if now - f.started < FLOW_TTL_S}
        self._flows[state] = _Flow(meta, client, verifier)
        query = {
            "response_type": "code",
            "client_id": client["client_id"],
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "resource": self.resource,
        }
        if meta.get("scope"):
            query["scope"] = meta["scope"]
        sep = "&" if "?" in meta["authorization_endpoint"] else "?"
        return f"{meta['authorization_endpoint']}{sep}{urlencode(query)}"

    async def finish(self, state: str, code: str | None, error: str | None = None) -> None:
        """Back from the sign-in page: the code becomes tokens, saved for the sink."""
        flow = self._flows.pop(state, None)
        if flow is None or time.monotonic() - flow.started >= FLOW_TTL_S:
            raise OAuthError("this sign-in is unknown or too old: click Connect again")
        if error or not code:
            raise OAuthError(f"the sign-in was not completed ({error or 'no code came back'})")
        async with self._http() as http:
            tokens = await _token(http, flow.meta, flow.client, {
                "grant_type": "authorization_code", "code": code, "redirect_uri": flow.client["redirect_uri"],
                "code_verifier": flow.verifier,
            })
        self.store.keep({"meta": flow.meta, "client": flow.client, "connected_at": time.time()}, tokens)
