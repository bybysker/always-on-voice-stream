from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import os
import re
import socket
import threading
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

from voicestream.config import Config, DashboardConfig
from voicestream.dashboard import server
from voicestream.dashboard.server import LOOPBACK_HOSTS, TOKEN_HEADER, allowed_hosts_for, create_app, serve
from voicestream.events import EventBus
from voicestream.session import Assistant

TOKEN = "test-token-123"
BASE = "http://127.0.0.1:8765"
ITEM = {"id": "A1", "kind": "action", "title": "Send the pricing deck", "revision": 1, "agent": None}


class FakeAssistant:
    """Records control-API calls; the signatures mirror :class:`Assistant` (checked below)."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.bus = EventBus()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.meeting: dict[str, Any] | None = {"id": "m1", "title": "Weekly sync", "items": {"A1": ITEM}, "segments": []}
        self.agent_ok = True
        self.ended = threading.Event()
        self.tracked: list[asyncio.Task[Any]] = []

    def _record(self, name: str, **args: Any) -> None:
        self.calls.append((name, args))

    def _find(self, item_id: str) -> None:
        if self.meeting is None or item_id not in self.meeting["items"]:
            raise KeyError(item_id)

    def _session(self) -> None:
        if self.meeting is None:
            raise LookupError("no meeting in progress")

    def state(self) -> dict[str, Any]:
        return {"user_name": "Alex", "sources": ["mic: input device default"], "sinks": ["markdown"], "meeting": self.meeting}

    def approve_agent(self, item_id: str, prompt: str | None = None, meeting_id: str | None = None) -> bool:
        self._record("approve_agent", item_id=item_id, prompt=prompt, meeting_id=meeting_id)
        self._find(item_id)
        return self.agent_ok

    def reject_agent(self, item_id: str, meeting_id: str | None = None) -> bool:
        self._record("reject_agent", item_id=item_id, meeting_id=meeting_id)
        self._find(item_id)
        return self.agent_ok

    def dismiss_item(self, item_id: str, dismissed: bool = True, meeting_id: str | None = None) -> bool:
        self._record("dismiss_item", item_id=item_id, dismissed=dismissed, meeting_id=meeting_id)
        self._find(item_id)
        return True

    def edit_item(self, item_id: str, meeting_id: str | None = None, **fields: Any) -> dict[str, Any]:
        self._record("edit_item", item_id=item_id, meeting_id=meeting_id, fields=fields)
        self._find(item_id)
        return {**ITEM, **fields, "revision": 2}

    def add_item(self, kind: str, title: str, owner: str | None = None, due_text: str | None = None) -> dict[str, Any]:
        self._record("add_item", kind=kind, title=title, owner=owner, due_text=due_text)
        self._session()
        return {"id": "A2", "kind": kind, "title": title, "owner": owner, "due_text": due_text}

    async def push_item(self, item_id: str, meeting_id: str | None = None) -> dict[str, str]:
        self._record("push_item", item_id=item_id, meeting_id=meeting_id)
        self._find(item_id)
        return {"markdown": "/data/actions.md", "notion": "error"}

    async def extract_now(self) -> None:
        self._record("extract_now")
        self._session()

    async def end_meeting(self, reason: str = "manual") -> Path | None:
        self._record("end_meeting", reason=reason)
        self.ended.set()
        return None

    def track(self, task: asyncio.Task[Any]) -> None:
        self.tracked.append(task)


@pytest.fixture
def fake(cfg: Config) -> FakeAssistant:
    return FakeAssistant(cfg)


@pytest.fixture
def client(fake: FakeAssistant) -> Iterator[TestClient]:
    # as a context manager the app keeps one event loop, so background tasks get to run
    with TestClient(create_app(fake, TOKEN), base_url=BASE, headers={TOKEN_HEADER: TOKEN}) as c:
        yield c


def _shape(fn: Any) -> list[tuple[str, Any, Any]]:
    return [(p.name, p.kind, p.default) for p in inspect.signature(fn).parameters.values()]


@pytest.mark.parametrize(
    "name",
    ["state", "approve_agent", "reject_agent", "dismiss_item", "edit_item", "add_item", "push_item", "extract_now", "end_meeting", "track"],
)
def test_fake_assistant_matches_the_real_control_api(name: str) -> None:
    real, fake = getattr(Assistant, name), getattr(FakeAssistant, name)
    assert _shape(fake) == _shape(real)
    assert inspect.iscoroutinefunction(fake) == inspect.iscoroutinefunction(real)


# ------------------------------------------------------------------------ the page


def test_index_is_served_without_a_token_with_a_strict_csp(fake: FakeAssistant) -> None:
    with TestClient(create_app(fake, TOKEN), base_url=BASE) as anonymous:
        r = anonymous.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    nonce = re.search(r"script-src 'nonce-([^']+)'", r.headers["content-security-policy"]).group(1)
    assert f'<script nonce="{nonce}">' in r.text and f'<style nonce="{nonce}">' in r.text
    assert "<script>" not in r.text
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["cache-control"] == "no-store"
    assert TOKEN not in r.text


def test_page_is_self_contained_and_never_parses_html_strings() -> None:
    page = server.INDEX_HTML.read_text(encoding="utf-8")
    assert not re.search(r"""(src|href)\s*=\s*["']?(https?:)?//""", page)  # no CDN, works offline
    assert "@import" not in page
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert sink not in page
    assert page.count("<script>") == page.count("<style>") == 1  # the server adds nonces to these


# --------------------------------------------------------------------------- auth


@pytest.mark.parametrize("path", ["/api/state", "/api/events", "/api/file?path=x", "/api/nope", "/favicon.ico"])
def test_everything_but_the_page_requires_the_token(fake: FakeAssistant, path: str) -> None:
    with TestClient(create_app(fake, TOKEN), base_url=BASE) as anonymous:
        assert anonymous.get(path).status_code == 401
        assert anonymous.get(path, headers={TOKEN_HEADER: "wrong"}).status_code == 401
        assert anonymous.get(path, params={"token": TOKEN + "x"}).status_code == 401
        # non-ASCII must be a clean 401, not a TypeError from compare_digest
        assert anonymous.get(path, params={"token": "jeton-é"}).status_code == 401


def test_mutations_without_a_token_never_reach_the_assistant(fake: FakeAssistant) -> None:
    with TestClient(create_app(fake, TOKEN), base_url=BASE) as anonymous:
        assert anonymous.post("/api/items/A1/push").status_code == 401
        assert anonymous.post("/api/meeting/end").status_code == 401
        assert anonymous.post("/api/items", json={"kind": "action", "title": "x"}).status_code == 401
    assert fake.calls == []


def test_token_is_accepted_as_header_or_query_param(fake: FakeAssistant) -> None:
    with TestClient(create_app(fake, TOKEN), base_url=BASE) as anonymous:
        assert anonymous.get("/api/state", headers={TOKEN_HEADER: TOKEN}).json()["meeting"]["id"] == "m1"
        assert anonymous.get("/api/state", params={"token": TOKEN}).status_code == 200


def test_an_empty_token_is_refused(fake: FakeAssistant) -> None:
    with pytest.raises(ValueError):
        create_app(fake, "")


def test_api_responses_are_hardened_and_have_no_cors(client: TestClient) -> None:
    r = client.get("/api/state", headers={"origin": "https://evil.example"})
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert not any(h.startswith("access-control-") for h in r.headers)


# ---------------------------------------------------------------------- Host guard


@pytest.mark.parametrize("host", ["127.0.0.1:8765", "localhost:8765", "LOCALHOST", "[::1]:8765", "127.0.0.1"])
def test_loopback_hosts_are_accepted(client: TestClient, host: str) -> None:
    assert client.get("/api/state", headers={"host": host}).status_code == 200


@pytest.mark.parametrize(
    "host", ["evil.example", "evil.example:8765", "localhost.evil.example", "127.0.0.1.nip.io:8765", "[::1", "evil@127.0.0.1"]
)
def test_dns_rebinding_hosts_are_refused_even_for_the_page(client: TestClient, host: str) -> None:
    assert client.get("/api/state", headers={"host": host}).status_code == 403
    assert client.get("/", headers={"host": host}).status_code == 403
    assert client.get("/api/events", headers={"host": host}).status_code == 403


def test_allowed_hosts_follow_the_bind_address(fake: FakeAssistant) -> None:
    assert allowed_hosts_for("127.0.0.1") == LOOPBACK_HOSTS
    assert allowed_hosts_for("[::1]") == LOOPBACK_HOSTS
    assert allowed_hosts_for("0.0.0.0") is None and allowed_hosts_for("::") is None
    lan = allowed_hosts_for("192.168.1.5")
    assert lan == LOOPBACK_HOSTS | {"192.168.1.5"}
    headers = {TOKEN_HEADER: TOKEN}
    with TestClient(create_app(fake, TOKEN, allowed_hosts=lan), base_url=BASE, headers=headers) as c:
        assert c.get("/api/state", headers={"host": "192.168.1.5:8765"}).status_code == 200
        assert c.get("/api/state", headers={"host": "evil.example"}).status_code == 403
    with TestClient(create_app(fake, TOKEN, allowed_hosts=None), base_url=BASE, headers=headers) as c:
        assert c.get("/api/state", headers={"host": "my-laptop.local:8765"}).status_code == 200


# ------------------------------------------------------------------------- routes


@pytest.mark.parametrize(
    ("method", "path", "body", "call", "status"),
    [
        ("POST", "/api/items/A1/push?meeting_id=m1", None, ("push_item", {"item_id": "A1", "meeting_id": "m1"}), 200),
        ("POST", "/api/items/A1/dismiss", None, ("dismiss_item", {"item_id": "A1", "dismissed": True, "meeting_id": None}), 200),
        ("POST", "/api/items/A1/restore?meeting_id=m1", None, ("dismiss_item", {"item_id": "A1", "dismissed": False, "meeting_id": "m1"}), 200),
        ("POST", "/api/items/A1/agent/run", {"prompt": "  Draft the deck  "}, ("approve_agent", {"item_id": "A1", "prompt": "Draft the deck", "meeting_id": None}), 200),
        ("POST", "/api/items/A1/agent/run", None, ("approve_agent", {"item_id": "A1", "prompt": None, "meeting_id": None}), 200),
        ("POST", "/api/items/A1/agent/run?meeting_id=m1", {"prompt": " "}, ("approve_agent", {"item_id": "A1", "prompt": None, "meeting_id": "m1"}), 200),
        ("POST", "/api/items/A1/agent/reject?meeting_id=m1", None, ("reject_agent", {"item_id": "A1", "meeting_id": "m1"}), 200),
        (
            "PATCH",
            "/api/items/A1",
            {"title": "Send the deck", "owner": "Sam", "due_text": "Friday", "due_date": "2026-10-16", "status": "done", "priority": "high"},
            (
                "edit_item",
                {
                    "item_id": "A1",
                    "meeting_id": None,
                    "fields": {"title": "Send the deck", "owner": "Sam", "due_text": "Friday", "due_date": "2026-10-16", "status": "done", "priority": "high"},
                },
            ),
            200,
        ),
        # only the fields that were sent are passed on
        ("PATCH", "/api/items/A1?meeting_id=m1", {"owner": "Sam"}, ("edit_item", {"item_id": "A1", "meeting_id": "m1", "fields": {"owner": "Sam"}}), 200),
        ("POST", "/api/items", {"kind": "decision", "title": " Ship v2 ", "owner": "Sam"}, ("add_item", {"kind": "decision", "title": "Ship v2", "owner": "Sam", "due_text": None}), 201),
        ("POST", "/api/items", {"kind": "action", "title": "Call Sam", "owner": "", "due_text": "Friday"}, ("add_item", {"kind": "action", "title": "Call Sam", "owner": None, "due_text": "Friday"}), 201),
        ("POST", "/api/extract", None, ("extract_now", {}), 200),
    ],
)
def test_routes_call_the_assistant(
    client: TestClient, fake: FakeAssistant, method: str, path: str, body: Any, call: tuple[str, dict[str, Any]], status: int
) -> None:
    r = client.request(method, path, json=body)
    assert r.status_code == status, r.text
    assert fake.calls == [call]


def test_route_responses(client: TestClient, fake: FakeAssistant) -> None:
    assert client.get("/api/state").json() == fake.state()
    assert client.post("/api/items/A1/push").json() == {"results": {"markdown": "/data/actions.md", "notion": "error"}}
    assert client.post("/api/items/A1/dismiss").json() == {"ok": True}
    assert client.patch("/api/items/A1", json={"title": "New"}).json()["title"] == "New"
    assert client.post("/api/items", json={"kind": "note", "title": "Budget is fixed"}).json()["id"] == "A2"


def test_agent_actions_that_dont_apply_are_conflicts(client: TestClient, fake: FakeAssistant) -> None:
    fake.agent_ok = False
    r = client.post("/api/items/A1/agent/run")
    assert r.status_code == 409 and "already" in r.json()["error"]
    assert client.post("/api/items/A1/agent/reject").status_code == 409


def test_end_meeting_runs_in_the_background_and_is_tracked(client: TestClient, fake: FakeAssistant) -> None:
    r = client.post("/api/meeting/end")
    assert r.status_code == 202 and r.json() == {"ok": True}
    assert fake.ended.wait(5)
    assert fake.calls == [("end_meeting", {"reason": "dashboard"})]
    assert len(fake.tracked) == 1


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/api/items/ZZ/push", None),
        ("POST", "/api/items/ZZ/dismiss", None),
        ("POST", "/api/items/ZZ/restore", None),
        ("POST", "/api/items/ZZ/agent/run", None),
        ("POST", "/api/items/ZZ/agent/reject", None),
        ("PATCH", "/api/items/ZZ", {"title": "x"}),
    ],
)
def test_unknown_items_are_404(client: TestClient, method: str, path: str, body: Any) -> None:
    r = client.request(method, path, json=body)
    assert r.status_code == 404
    assert r.json() == {"error": "unknown item ZZ"}


@pytest.mark.parametrize("exc", [KeyError("markdown"), IndexError("list index out of range"), ValueError("bad state")])
def test_internal_errors_are_500s_not_passed_off_as_404_or_400(fake: FakeAssistant, exc: Exception) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise exc

    fake.dismiss_item = fake.add_item = broken  # type: ignore[method-assign]
    app = create_app(fake, TOKEN)
    with TestClient(app, base_url=BASE, headers={TOKEN_HEADER: TOKEN}, raise_server_exceptions=False) as c:
        assert c.post("/api/items/A1/dismiss").status_code == 500
        assert c.post("/api/items", json={"kind": "action", "title": "x"}).status_code == 500


def test_no_meeting_in_progress_is_404(client: TestClient, fake: FakeAssistant) -> None:
    fake.meeting = None
    for path, body in (("/api/items", {"kind": "action", "title": "x"}), ("/api/extract", None)):
        r = client.post(path, json=body)
        assert r.status_code == 404 and r.json()["error"] == "no meeting in progress"


def test_unknown_routes_and_methods_answer_json(client: TestClient) -> None:
    assert client.get("/api/nope").status_code == 404
    r = client.get("/api/extract")
    assert r.status_code == 405 and "error" in r.json()


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("PATCH", "/api/items/A1", {"bogus": 1}),
        ("PATCH", "/api/items/A1", {"meeting_id": "m2"}),
        ("PATCH", "/api/items/A1", {"status": "finished"}),
        ("PATCH", "/api/items/A1", {"priority": "urgent"}),
        ("PATCH", "/api/items/A1", {"due_date": "friday"}),
        ("PATCH", "/api/items/A1", {"title": "   "}),
        ("PATCH", "/api/items/A1", [1, 2]),
        ("POST", "/api/items", {"kind": "task", "title": "x"}),
        ("POST", "/api/items", {"kind": "action"}),
        ("POST", "/api/items", {"kind": "action", "title": "x", "extra": True}),
        ("POST", "/api/items/A1/agent/run", {"prompt": 5}),
    ],
)
def test_invalid_bodies_are_400_and_never_reach_the_assistant(
    client: TestClient, fake: FakeAssistant, method: str, path: str, body: Any
) -> None:
    r = client.request(method, path, json=body)
    assert r.status_code == 400, r.text
    assert r.json()["error"]
    assert fake.calls == []


def test_malformed_json_is_400(client: TestClient, fake: FakeAssistant) -> None:
    r = client.patch("/api/items/A1", content=b"{not json", headers={"content-type": "application/json"})
    assert r.status_code == 400 and "JSON" in r.json()["error"]
    assert fake.calls == []


# ---------------------------------------------------------------------- /api/file


@pytest.fixture
def files(cfg: Config, tmp_path: Path) -> dict[str, Path]:
    session = cfg.data_path / "sessions" / "2026-10-14_100000-weekly-sync"
    workspace = session / "agents" / "A1-send-the-deck"
    workspace.mkdir(parents=True)
    (session / "notes.md").write_text("# Weekly sync\n\n- [ ] Send the deck\n", encoding="utf-8")
    (workspace / "draft.txt").write_text("Hi Sam, here is the deck.\n", encoding="utf-8")
    (workspace / "chart.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe\x00\x00")
    (cfg.data_path / ".env").write_text("ANTHROPIC_API_KEY=sk-secret\n", encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("outside the data dir\n", encoding="utf-8")
    return {"session": session, "workspace": workspace, "secret": secret}


def test_file_serves_text_inside_the_data_dir(client: TestClient, cfg: Config, files: dict[str, Path]) -> None:
    r = client.get("/api/file", params={"path": str(files["session"] / "notes.md")})
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/markdown; charset=utf-8"
    assert r.text.startswith("# Weekly sync")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in r.headers["content-security-policy"]
    rel = (files["workspace"] / "draft.txt").relative_to(cfg.data_path)
    r = client.get("/api/file", params={"path": str(rel)})
    assert r.status_code == 200 and r.headers["content-type"] == "text/plain; charset=utf-8"


def test_file_refuses_paths_outside_the_data_dir(client: TestClient, cfg: Config, files: dict[str, Path]) -> None:
    secret = files["secret"]
    attempts = [
        "../secret.txt",
        "sessions/../../secret.txt",
        str(files["session"] / ".." / ".." / ".." / "secret.txt"),
        str(secret),
        "/etc/passwd",
        ".env",
        str(cfg.data_path / ".env"),
    ]
    for path in attempts:
        r = client.get("/api/file", params={"path": path})
        assert r.status_code == 403, path
        assert "outside" not in r.text
    # percent-encoded traversal decodes to the same thing
    assert client.get("/api/file?path=..%2Fsecret.txt").status_code == 403


def test_file_refuses_symlinks_that_leave_the_data_dir(client: TestClient, cfg: Config, files: dict[str, Path]) -> None:
    session = files["session"]
    try:
        os.symlink(files["secret"], session / "link.txt")
        os.symlink(files["secret"].parent, session / "linkdir", target_is_directory=True)
        os.symlink(cfg.data_path / ".env", session / "env.txt")  # inside, but a dot-file
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")
    for path in (session / "link.txt", session / "linkdir" / "secret.txt", session / "env.txt"):
        assert client.get("/api/file", params={"path": str(path)}).status_code == 403


def test_file_errors(client: TestClient, files: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = files["workspace"]
    assert client.get("/api/file").status_code == 400
    assert client.get("/api/file", params={"path": ""}).status_code == 400
    assert client.get("/api/file?path=notes%00.md").status_code == 400
    assert client.get("/api/file", params={"path": str(workspace / "missing.md")}).status_code == 404
    assert client.get("/api/file", params={"path": str(workspace)}).status_code == 404
    assert client.get("/api/file", params={"path": "a" * 300}).status_code == 404  # stat → ENAMETOOLONG
    assert client.get("/api/file", params={"path": str(workspace / "chart.png")}).status_code == 415
    monkeypatch.setattr(server, "MAX_FILE_BYTES", 10)
    assert client.get("/api/file", params={"path": str(workspace / "draft.txt")}).status_code == 413


# ------------------------------------------------------- live server + event stream


async def _frames(response: httpx.Response) -> AsyncIterator[tuple[str, Any]]:
    """(event type, parsed data) per SSE frame; comments come out as ("comment", text)."""
    kind, data, comment = "", "", ""
    async for line in response.aiter_lines():
        if line == "":
            if kind:
                yield kind, json.loads(data)
            elif comment:
                yield "comment", comment
            kind, data, comment = "", "", ""
        elif line.startswith(":"):
            comment = line[1:].strip()
        elif line.startswith("event:"):
            kind = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data += line[len("data:") :].strip()


async def _next_event(frames: AsyncIterator[tuple[str, Any]]) -> tuple[str, Any]:
    """The next real event, skipping keepalives (one may slip in on a slow machine)."""
    while True:
        kind, data = await asyncio.wait_for(anext(frames), 5)
        if kind != "comment":
            return kind, data


@contextlib.asynccontextmanager
async def _live(fake: FakeAssistant, host: str = "127.0.0.1", **kw: Any) -> AsyncIterator[str]:
    url, task = await serve(fake, DashboardConfig(host=host, port=0), **kw)
    try:
        yield url
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
async def test_serve_returns_a_tokened_url_and_serves(fake: FakeAssistant, host: str) -> None:
    async with _live(fake, host) as url:
        assert re.fullmatch(rf"http://{re.escape(host)}:\d+/\?token=[\w-]{{32}}", url)
        base, token = url.split("?token=")
        async with httpx.AsyncClient(trust_env=False, timeout=5) as http:
            assert (await http.get(base)).status_code == 200
            assert (await http.get(f"{base}api/state")).status_code == 401
            assert (await http.get(f"{base}api/state", params={"token": token})).json()["meeting"]["id"] == "m1"
    with pytest.raises(httpx.TransportError):  # refused (or, on Windows, sometimes a connect timeout)
        async with httpx.AsyncClient(trust_env=False, timeout=5) as http:
            await http.get(base)


async def test_serve_raises_oserror_when_the_port_is_taken(fake: FakeAssistant) -> None:
    with socket.create_server(("127.0.0.1", 0)) as taken, pytest.raises(OSError):
        await serve(fake, DashboardConfig(port=taken.getsockname()[1]))


async def test_event_stream_sends_state_then_bus_events(fake: FakeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "KEEPALIVE_S", 0.2)
    segment = {"id": "s_1", "source": "system", "speaker": "Others", "text": "Ashley, can you send it?", "t_start": 1.0, "t_end": 3.0}
    async with _live(fake, token=TOKEN) as url:
        base = url.split("?")[0]
        async with httpx.AsyncClient(trust_env=False, timeout=5) as http, http.stream(
            "GET", f"{base}api/events", params={"token": TOKEN}
        ) as r:
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/event-stream")
            frames = _frames(r)
            kind, data = await _next_event(frames)
            assert kind == "state" and data["type"] == "state"
            assert data["data"]["meeting"]["id"] == "m1"

            fake.bus.publish("segment", meeting_id="m1", segment=segment, index=0)
            fake.bus.publish("level", source="mic", db=-20.0, speech=True)
            fake.bus.publish("level", source="mic", db=-21.0, speech=True)  # within the throttle window
            fake.bus.publish("level", source="system", db=float("-inf"), speech=False)
            fake.bus.publish("cost", today={"total_usd": 0.01}, daily_budget_usd=2.0)
            received = [await _next_event(frames) for _ in range(4)]
            assert [k for k, _ in received] == ["segment", "level", "level", "cost"]
            assert received[0][1]["data"] == {"meeting_id": "m1", "segment": segment, "index": 0}
            assert received[1][1]["data"] == {"source": "mic", "db": -20.0, "speech": True}
            assert received[2][1]["data"]["db"] is None  # -inf isn't JSON

            assert await asyncio.wait_for(anext(frames), 5) == ("comment", "keepalive")
            assert len(fake.bus._queues) == 1

        # the client went away: the stream unsubscribes from the bus
        for _ in range(100):
            if not fake.bus._queues:
                break
            await asyncio.sleep(0.02)
        assert not fake.bus._queues


async def test_shutdown_ends_open_event_streams_cleanly(fake: FakeAssistant, caplog: pytest.LogCaptureFixture) -> None:
    # a raw connection, like a browser's EventSource (it doesn't hang up when the server stops)
    url, task = await serve(fake, DashboardConfig(port=0), token=TOKEN)
    port = int(url.split(":")[2].split("/")[0])
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET /api/events?token={TOKEN} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"event: state"), 5)
    assert head.startswith(b"HTTP/1.1 200")
    loop = asyncio.get_running_loop()
    started = loop.time()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert loop.time() - started < 0.9  # well inside uvicorn's 1 s graceful-shutdown timeout
    rest = await asyncio.wait_for(reader.read(), 5)
    assert rest.endswith(b"0\r\n\r\n")  # the response was completed, not cut off
    writer.close()
    assert not fake.bus._queues
    assert not [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
