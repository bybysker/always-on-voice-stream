"""The Mac app's HTTP API (voicestream/dashboard/app_routes.py + the host-aware dashboard server)."""

from __future__ import annotations

import json
import re
import sys
import time
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient
from test_app_host import _factory, _write_config
from test_app_meetings import WED, Rec, _save

from voicestream import stt_models
from voicestream.app import desktop
from voicestream.app import devices as devices_mod
from voicestream.app.host import AppHost
from voicestream.config import load_config
from voicestream.dashboard import app_routes
from voicestream.dashboard.server import TOKEN_HEADER, create_app
from voicestream.storage import load_meeting

TOKEN = "app-token-456"
BASE = "http://127.0.0.1:8765"
PLANNING = "2026-10-14_110000-planning"
LIVE_ELSEWHERE = "2026-10-16_100000"


@pytest.fixture
def host(tmp_path: Path) -> AppHost:
    h = AppHost(_write_config(tmp_path), process_setup=False, prefs_path=tmp_path / "app.json", assistant_factory=_factory())
    _save(h.cfg, PLANNING, started=WED, title="Q4 planning")
    _save(h.cfg, LIVE_ELSEWHERE, started=WED + timedelta(days=2), ended=False, notes=False)
    return h


@pytest.fixture
def client(host: AppHost) -> Iterator[TestClient]:
    with TestClient(create_app(host, TOKEN), base_url=BASE, headers={TOKEN_HEADER: TOKEN}) as c:
        yield c


def _wait_state(client: TestClient, *states: str, timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        body = client.get("/api/app").json()
        if body["state"] in states:
            return body
        if time.monotonic() > deadline:
            raise AssertionError(f"state stayed {body['state']!r}")
        time.sleep(0.05)


ROUTES = [
    ("GET", "/api/app"), ("POST", "/api/listen/start"), ("POST", "/api/listen/stop"), ("GET", "/api/settings"),
    ("PATCH", "/api/settings"), ("PUT", "/api/secrets/ANTHROPIC_API_KEY"), ("POST", "/api/secrets/ANTHROPIC_API_KEY/test"),
    ("GET", "/api/doctor"), ("GET", "/api/devices"), ("GET", "/api/meetings"), ("GET", f"/api/meetings/{PLANNING}"),
    ("POST", f"/api/meetings/{PLANNING}/push"), ("POST", f"/api/meetings/{PLANNING}/summarize"),
    ("POST", f"/api/meetings/{PLANNING}/items"), ("POST", f"/api/meetings/{PLANNING}/people"),
    ("PATCH", f"/api/meetings/{PLANNING}/items/A1"),
    ("POST", f"/api/meetings/{PLANNING}/items/A1/dismiss"), ("GET", "/api/cost"), ("GET", "/api/models"),
    ("POST", "/api/models/base/download"), ("DELETE", "/api/models/base"), ("POST", "/api/setup/complete"),
    ("PATCH", "/api/prefs"), ("POST", "/api/reveal"), ("POST", "/api/open-settings"), ("POST", "/api/permissions/microphone"),
    ("POST", "/api/hotkey/recording"),
]  # fmt: skip


# ----------------------------------------------------------------- auth and page


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_every_app_route_needs_the_token(host: AppHost, method: str, path: str) -> None:
    with TestClient(create_app(host, TOKEN), base_url=BASE) as anonymous:
        assert anonymous.request(method, path, json={}).status_code == 401
        assert anonymous.request(method, path, json={}, headers={TOKEN_HEADER: "wrong"}).status_code == 401
    assert host.assistant is None and not host.prefs.setup_done  # nothing happened


def test_the_app_page_has_a_strict_csp_and_no_token(host: AppHost) -> None:
    with TestClient(create_app(host, TOKEN), base_url=BASE) as anonymous:
        r = anonymous.get("/app")
        assert anonymous.get("/app", headers={"host": "evil.example"}).status_code == 403  # DNS rebinding
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    csp = r.headers["content-security-policy"]
    nonce = re.search(r"script-src 'nonce-([^']+)' 'self'", csp).group(1)
    assert f"style-src 'nonce-{nonce}' 'self'" in csp and "default-src 'none'" in csp and "frame-ancestors 'none'" in csp
    assert f'<script nonce="{nonce}">' in r.text and "<script>" not in r.text
    assert TOKEN not in r.text and "/api/app" in r.text
    assert r.headers["cache-control"] == "no-store" and r.headers["referrer-policy"] == "no-referrer"


def test_static_files_are_served_only_when_present(host: AppHost, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    static = tmp_path / "static"
    static.mkdir()
    monkeypatch.setattr(app_routes, "STATIC", static)
    (static / "app.js").write_text("console.log('hi')\n", encoding="utf-8")
    (static / "secret.txt").write_text("no\n", encoding="utf-8")
    with TestClient(create_app(host, TOKEN), base_url=BASE) as anonymous:
        js = anonymous.get("/static/app.js")
        assert js.status_code == 200 and js.headers["content-type"].startswith("text/javascript") and js.text.startswith("console")
        assert js.headers["x-content-type-options"] == "nosniff"
        assert anonymous.get("/static/app.css").status_code == 404  # not written yet
        for path in ("/static/secret.txt", "/static/%2E%2E/static/app.js", "/static/index.html", "/static/app.js/x"):
            assert anonymous.get(path).status_code in (401, 404), path


def test_the_bundled_fonts_are_served_from_the_app_itself(host: AppHost) -> None:
    """The UI's typeface ships with the app (no font service: the app works offline and loads
    nothing from the internet), with its licence; the page's CSP allows fonts from itself only."""
    static = Path(app_routes.__file__).parent / "static"
    assert (static / "fonts" / "OFL.txt").read_text(encoding="utf-8").startswith("Geist")
    css = (static / "app.css").read_text(encoding="utf-8")
    with TestClient(create_app(host, TOKEN), base_url=BASE) as anonymous:
        assert "font-src 'self';" in anonymous.get("/app").headers["content-security-policy"]
        for name in app_routes.FONTS:
            assert f"url('/static/fonts/{name}')" in css, name  # each one is used, and only these
            r = anonymous.get("/static/fonts/" + name)  # a style sheet's font request can't send the token
            assert r.status_code == 200 and r.headers["content-type"] == "font/woff2" and r.content[:4] == b"wOF2", name
        assert anonymous.get("/static/fonts/OFL.txt").status_code in (401, 404)
    assert set(re.findall(r"url\('/static/fonts/([^']+)'\)", css)) == set(app_routes.FONTS)


def test_plain_dashboard_has_no_app_routes(host: AppHost) -> None:
    from test_dashboard import FakeAssistant

    with TestClient(create_app(FakeAssistant(host.cfg), TOKEN), base_url=BASE, headers={TOKEN_HEADER: TOKEN}) as c:
        assert c.get("/api/app").status_code == 404
        assert c.get("/app").status_code == 404
    with TestClient(create_app(FakeAssistant(host.cfg), TOKEN), base_url=BASE) as anonymous:
        assert anonymous.get("/app").status_code == 401  # public only in app mode


# ------------------------------------------------------------------- listening


def test_live_routes_answer_409_when_not_listening(client: TestClient) -> None:
    assert client.get("/api/state").status_code == 409
    for method, path, body in (("POST", "/api/items", {"kind": "action", "title": "x"}), ("POST", "/api/extract", None),
                               ("PATCH", "/api/items/A1", {"title": "x"}), ("POST", "/api/meeting/end", None)):
        r = client.request(method, path, json=body)
        assert r.status_code == 409 and r.json() == {"error": "not listening"}


def test_listen_lifecycle_over_http(client: TestClient, host: AppHost) -> None:
    body = client.post("/api/listen/start").json()
    assert body["state"] == "listening" and client.get("/api/state").status_code == 200
    assert client.get("/api/state").json()["listening"] == "listening"
    assert client.post("/api/listen/pause").json()["state"] == "paused"
    assert client.get("/api/state").json()["listening"] == "paused"
    assert client.post("/api/listen/resume").json()["state"] == "listening"
    first = host.assistant
    client.post("/api/listen/restart")
    _wait_state(client, "listening")
    assert host.assistant is not None and host.assistant is not first
    assert client.post("/api/listen/stop?force=1").json()["state"] in ("stopping", "stopped")
    _wait_state(client, "stopped")
    assert client.post("/api/listen/dance").status_code == 404
    assert client.get("/api/listen/start").status_code == 405


async def test_the_event_stream_follows_the_host_across_restarts(host: AppHost) -> None:
    """The live view's stream subscribes to the host's bus: it survives a restart and starts with
    the app's state."""
    from voicestream.dashboard import server

    stream = server._Api(host, "")._stream()
    first = await stream.__anext__()
    assert "event: state" in first and "event: app.state" in first
    assert len(host.bus._queues) == 1
    await host.listen_start()
    await host.listen_restart()
    assert len(host.bus._queues) == 1  # still subscribed: the new assistant shares the bus
    await host.listen_stop()
    await stream.aclose()
    assert not host.bus._queues


# --------------------------------------------------------------------- settings


def test_settings_round_trip(client: TestClient, host: AppHost, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-never-shown")
    view = client.get("/api/settings").json()
    assert view["values"]["user_name"] == "Alex" and view["config_path"] == str(host.config_path)
    assert view["secrets"]["ANTHROPIC_API_KEY"] is True and "sk-ant-never-shown" not in json.dumps(view)
    r = client.patch("/api/settings", json={"changes": {"user_name": "Alex Martin", "brain.language": "fr"}})
    assert r.status_code == 200 and r.json()["values"]["user_name"] == "Alex Martin" and r.json()["restart_required"] is False
    assert load_config(host.config_path).brain.language == "fr" and host.cfg.user_name == "Alex Martin"
    before = host.config_path.read_text(encoding="utf-8")
    r = client.patch("/api/settings", json={"changes": {"transcription.backend": "telepathy"}})
    assert r.status_code == 400 and r.json()["field"] == "transcription.backend" and "Input should be" in r.json()["error"]
    assert host.config_path.read_text(encoding="utf-8") == before
    for bad in ({"changes": {}}, {"changes": "x"}, {"nope": 1}, [1]):
        assert client.patch("/api/settings", json=bad).status_code == 400
    assert any(e.type == "settings.changed" for e in host.bus.history)


def test_settings_change_during_a_meeting_is_pending(client: TestClient, host: AppHost) -> None:
    host.config_path.write_text(host.config_path.read_text().replace("idle_split_minutes = 10", "idle_split_minutes = 0"))
    host.reload_config()
    client.post("/api/listen/start")
    deadline = time.monotonic() + 5
    while not client.get("/api/app").json()["in_meeting"] and time.monotonic() < deadline:
        time.sleep(0.05)
    r = client.patch("/api/settings", json={"changes": {"user_name": "Sam"}})
    assert r.json()["restart_required"] is True and client.get("/api/app").json()["restart_pending"]
    client.post("/api/listen/restart")  # "apply now": ends the meeting
    body = _wait_state(client, "listening")
    assert not body["restart_pending"] and host.assistant.cfg.user_name == "Sam"
    client.post("/api/listen/stop?force=1")
    _wait_state(client, "stopped")


def test_secrets_are_write_only(client: TestClient, host: AppHost) -> None:
    r = client.put("/api/secrets/OPENAI_API_KEY", json={"value": "sk-openai-xyz"})
    assert r.status_code == 200 and r.json() == {"set": True, "store": "file"}
    assert "sk-openai-xyz" not in client.get("/api/settings").text
    assert client.get("/api/settings").json()["secrets"]["OPENAI_API_KEY"] is True
    assert client.put("/api/secrets/OPENAI_API_KEY", json={"value": ""}).json() == {"set": False, "store": "file"}
    for name in ("PATH", "..%2F..%2Fetc", "DYLD_INSERT_LIBRARIES"):
        assert client.put(f"/api/secrets/{name}", json={"value": "x"}).status_code == 404
        assert client.post(f"/api/secrets/{name}/test").status_code == 404
    assert client.put("/api/secrets/OPENAI_API_KEY", json={"value": "a\nb"}).status_code == 400
    assert client.put("/api/secrets/OPENAI_API_KEY", json={"value": 5}).status_code == 400
    assert client.post("/api/secrets/OPENAI_API_KEY/test").json() == {"ok": False, "detail": "OPENAI_API_KEY is not set"}


def test_doctor_and_devices(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    checks = client.get("/api/doctor").json()["checks"]
    assert checks and {"id", "area", "status", "title", "detail", "fix", "action"} <= set(checks[0])
    assert checks[-1]["id"] == "budget" and checks[-1]["status"] == "ok"
    monkeypatch.setattr(devices_mod, "list_devices", lambda cfg: {"inputs": [{"index": 0, "name": "MacBook Pro Microphone", "default": True}],
                                                                 "system_audio": {"method": "auto", "available": True, "detail": "tap"}})
    assert client.get("/api/devices").json()["inputs"][0]["name"] == "MacBook Pro Microphone"


@pytest.mark.parametrize(
    ("kind", "capture", "native", "platform", "expected"),
    [
        ("microphone", (1200, 16000), None, "linux", "granted"),
        ("microphone", (0, 16000), None, "darwin", "denied"),  # macOS hands a refused app pure zeros
        ("microphone", (0, 16000), None, "linux", "unknown"),
        ("microphone", (0, 16000), "granted", "darwin", "granted"),
        ("microphone", (500, 16000), "denied", "darwin", "denied"),
        ("system-audio", (0, 16000), None, "darwin", "unknown"),  # silence: nothing may be playing
        ("system-audio", (900, 16000), None, "darwin", "granted"),
    ],
)
def test_permission_probes(client: TestClient, host: AppHost, monkeypatch: pytest.MonkeyPatch, kind: str,
                           capture: tuple[int, int], native: str | None, platform: str, expected: str) -> None:
    async def fake_capture(cfg: Any, spec: str, seconds: float) -> tuple[int, int]:
        return capture

    monkeypatch.setattr(devices_mod, "_capture", fake_capture)
    monkeypatch.setattr(devices_mod.sys, "platform", platform)
    host.native_hooks = type("Hooks", (), {"microphone_status": staticmethod(lambda: native)})() if native else None
    body = client.post(f"/api/permissions/{kind}").json()
    assert body["status"] == expected
    assert (body["peak_db"] is None) == (capture[0] == 0 or expected == "denied")
    assert client.post("/api/permissions/camera").status_code == 404


def test_a_capture_that_fails_is_unknown_not_a_crash(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from voicestream.audio import CaptureSetupError

    async def fails(cfg: Any, spec: str, seconds: float) -> tuple[int, int]:
        raise CaptureSetupError("no input device matching 'USB'")

    monkeypatch.setattr(devices_mod, "_capture", fails)
    body = client.post("/api/permissions/microphone").json()
    assert body["status"] == "unknown" and "USB" in body["detail"]


# --------------------------------------------------------------------- meetings


def test_meeting_routes(client: TestClient, host: AppHost, monkeypatch: pytest.MonkeyPatch) -> None:
    rows = client.get("/api/meetings").json()
    assert rows["total"] == 2 and [m["folder"] for m in rows["meetings"]] == [LIVE_ELSEWHERE, PLANNING]
    assert client.get("/api/meetings?q=q4").json()["total"] == 1
    for bad in ("limit=0", "limit=abc", "offset=-1", "limit=100000"):
        assert client.get(f"/api/meetings?{bad}").status_code == 400
    d = client.get(f"/api/meetings/{PLANNING}").json()
    assert d["meeting"]["title"] == "Q4 planning" and d["notes_markdown"].startswith("# Q4") and not d["in_memory"]
    r = client.post(f"/api/meetings/{PLANNING}/items", json={"kind": "action", "title": "Call Sam", "due_text": "demain"})
    assert r.status_code == 201 and r.json()["due_date"] == "2026-10-15"  # the day after the meeting
    assert client.patch(f"/api/meetings/{PLANNING}/items/A1", json={"status": "done"}).json()["status"] == "done"
    assert client.post(f"/api/meetings/{PLANNING}/items/D1/dismiss").json()["dismissed"] is True
    assert client.post(f"/api/meetings/{PLANNING}/items/D1/restore").json()["dismissed"] is False
    assert client.patch(f"/api/meetings/{PLANNING}/items/ZZ", json={"status": "done"}).json() == {"error": "unknown item ZZ"}
    assert client.patch(f"/api/meetings/{PLANNING}/items/A1", json={"bogus": 1}).status_code == 400
    r = client.patch(f"/api/meetings/{LIVE_ELSEWHERE}/items/A1", json={"title": "x"})
    assert r.status_code == 409 and "still being recorded" in r.json()["error"]
    assert load_meeting(host.cfg.data_path / "sessions" / PLANNING)[0].items["A1"].status.value == "done"

    rec = Rec(host.cfg)
    monkeypatch.setattr("voicestream.sinks.base.build_sinks", lambda app, only=None: [rec])
    pushed = client.post(f"/api/meetings/{PLANNING}/push", json={"notes": False}).json()
    assert pushed["items"] == {"A1": {"rec": "rec:A1"}, "A3": {"rec": "rec:A3"}} and pushed["errors"] == []  # A2 was there
    assert client.post(f"/api/meetings/{LIVE_ELSEWHERE}/push", json={}).status_code == 409
    assert client.post(f"/api/meetings/{PLANNING}/push", json={"sinks": "rec"}).status_code == 400
    r = client.post(f"/api/meetings/{PLANNING}/summarize")
    assert r.status_code == 202
    deadline = time.monotonic() + 5
    while not any(e.type == "meeting.notes_updated" for e in host.bus.history) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert any(e.type == "meeting.notes_updated" and e.data["folder"] == PLANNING for e in host.bus.history)
    assert client.post(f"/api/meetings/{LIVE_ELSEWHERE}/summarize").status_code == 409


@pytest.mark.parametrize(
    "path",
    [
        "/api/meetings/..", "/api/meetings/%2E%2E", "/api/meetings/..%2F..%2Fetc", "/api/meetings/%2Fetc%2Fpasswd",
        "/api/meetings/2026-10-14_110000", "/api/meetings/sessions", f"/api/meetings/{PLANNING}%2F..",
    ],
)
def test_meeting_folders_cannot_escape(client: TestClient, host: AppHost, path: str) -> None:
    before = {d: (d / "meeting.json").read_bytes() for d in (host.cfg.data_path / "sessions").iterdir()}
    r = client.get(path)
    assert r.status_code == 404, path
    assert client.post(f"{path}/summarize").status_code in (404, 405)
    # "/api/meetings/../items" is "/api/items" (the live route: not listening) once the client normalizes it
    assert client.post(f"{path}/items", json={"kind": "action", "title": "x"}).status_code in (404, 405, 409)
    assert {d: (d / "meeting.json").read_bytes() for d in (host.cfg.data_path / "sessions").iterdir()} == before


# ------------------------------------------------------------------- cost, models


def test_cost(client: TestClient, host: AppHost) -> None:
    spend = host.cfg.data_path / "spend"
    spend.mkdir(parents=True, exist_ok=True)
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    (spend / f"{yesterday}.json").write_text(json.dumps({"day": yesterday, "stt_usd": 0.5, "llm_usd": 0.25, "agents_usd": 0.0}))
    (spend / "garbage.json").write_text("{")
    host.costs.add_agent(0.125)
    body = client.get("/api/cost?hours=4").json()
    assert body["today"]["agents_usd"] == 0.125
    assert len(body["history"]) == 30 and body["history"][-1]["day"] == date.today().isoformat()
    assert body["history"][-1]["agents_usd"] == 0.125 and body["history"][-2] == {"day": yesterday, "stt_usd": 0.5, "llm_usd": 0.25, "agents_usd": 0.0}
    est = body["estimate"]
    assert est["hours"] == 4.0 and est["backend"] == "fake" and est["stt_usd"] == 0.0
    assert est["per_month_usd"] == pytest.approx(est["total_usd"] * 20, abs=1e-3)
    assert body["budget"] == {"daily_usd": 3.0, "agents_daily_usd": 5.0}
    for bad in ("hours=0", "hours=abc", "hours=25"):
        assert client.get(f"/api/cost?{bad}").status_code == 400


def test_models(client: TestClient, host: AppHost, monkeypatch: pytest.MonkeyPatch) -> None:
    body = client.get("/api/models").json()
    rec = next(m for m in body["models"] if m["recommended"])
    assert rec["name"] == "large-v3-turbo-q5_0" and rec["label"] == "Recommended" and not rec["installed"]
    assert body["in_use"] is None and body["models_dir"] == str(stt_models.models_dir(host.cfg))

    async def fake_download(cfg: Any, name: str, *, on_progress: Any = None, **kw: Any) -> Path:
        on_progress(10, 20)
        path = stt_models.model_path(cfg, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(stt_models.GGML_MAGIC + b"\0")
        return path

    monkeypatch.setattr(stt_models, "download", fake_download)
    assert client.post("/api/models/base/download").status_code == 202
    deadline = time.monotonic() + 5
    while not next(m for m in client.get("/api/models").json()["models"] if m["name"] == "base")["installed"]:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert [e.data["state"] for e in host.bus.history if e.type == "model.download" and e.data["name"] == "base"][-1] == "done"
    assert client.post("/api/models/base/cancel").json() == {"ok": True, "cancelled": False}
    assert client.delete("/api/models/base").json() == {"ok": True, "removed": True}
    for path in ("/api/models/../../x/download", "/api/models/ggml-base.bin/download", "/api/models/%2E%2E/cancel"):
        assert client.post(path).status_code == 404, path
    assert client.delete("/api/models/nope").status_code == 404


# --------------------------------------------------------------- prefs, desktop


def test_setup_and_prefs(client: TestClient, host: AppHost) -> None:
    assert client.get("/api/app").json()["setup_done"] is False
    assert client.post("/api/setup/complete").json()["setup_done"] is True
    assert client.get("/api/app").json()["setup_done"] is True
    body = client.patch("/api/prefs", json={"ui_language": "fr", "listen_on_launch": False}).json()
    assert body["ui_language"] == "fr" and body["listen_on_launch"] is False
    assert client.get("/api/app").json()["prefs"] == {
        "listen_on_launch": False, "start_at_login": False, "ui_language": "fr",
        "hotkey": {"code": "KeyR", "mods": ["alt", "cmd"], "key": "R"}, "hotkey_sound": True,
        "meeting_start": "manual", "floating_button": True, "floating_position": None, "spotlight_launcher": True,
        "call_suggestions": True, "silence_ask_minutes": 15, "silence_end_minutes": 60,
        "check_updates": True, "update_prereleases": False}  # fmt: skip
    for bad in ({"ui_language": "de"}, {"setup_done": False}, {"listen_on_launch": "maybe"}):
        assert client.patch("/api/prefs", json=bad).status_code == 400


def test_the_keyboard_shortcut_round_trip(client: TestClient, host: AppHost, tmp_path: Path) -> None:
    """Settings saves the shortcut through /api/prefs; the server checks it the way the page does."""
    assert client.get("/api/app").json()["hotkey"] == {"status": "pending", "error": None, "recording": False}
    host.set_hotkey_status("ok")  # the native shell's answer for the default one
    # recorded on an AZERTY keyboard: the physical key is KeyQ, its label is A; modifiers in any order
    body = client.patch("/api/prefs", json={"hotkey": {"code": "KeyQ", "mods": ["cmd", "ctrl", "cmd"], "key": "a"}})
    assert body.status_code == 200, body.text
    assert body.json()["hotkey"] == {"code": "KeyQ", "mods": ["ctrl", "cmd"], "key": "A"}
    state = client.get("/api/app").json()
    assert state["prefs"]["hotkey"] == {"code": "KeyQ", "mods": ["ctrl", "cmd"], "key": "A"}
    assert state["hotkey"]["status"] == "pending"  # until the shell says whether macOS took it
    assert json.loads((tmp_path / "app.json").read_text())["hotkey"] == {"code": "KeyQ", "mods": ["ctrl", "cmd"], "key": "A"}

    for bad, why in (
        ({"code": "KeyQ", "mods": ["cmd"]}, "every Mac app has"),             # ⌘Q quits
        ({"code": "Space", "mods": ["cmd"]}, "macOS uses ⌘Space itself"),     # Spotlight
        ({"code": "KeyC", "mods": ["cmd"]}, "taken in many apps"),            # ⌘C copies
        ({"code": "KeyE", "mods": ["alt", "shift"]}, "taken in many apps"),   # ⌥ types characters
        ({"code": "KeyR", "mods": ["shift"]}, "Add ⌘, ⌥ or ⌃"),
        ({"code": "KeyR", "mods": []}, "Add ⌘, ⌥ or ⌃"),
        ({"code": "Fn", "mods": ["alt", "cmd"]}, "can't use this key"),
        ({"code": "KeyR", "mods": ["alt", "hyper"]}, "hotkey.mods"),
        ({"code": "KeyR", "mods": ["alt", "cmd"], "extra": 1}, "extra"),
        ({"code": "KeyR", "mods": ["alt", "cmd"], "key": "R" * 40}, "at most 16"),
    ):  # fmt: skip
        r = client.patch("/api/prefs", json={"hotkey": bad})
        assert r.status_code == 400 and why in r.json()["error"], (bad, r.text)
    assert host.prefs.hotkey is not None and host.prefs.hotkey.code == "KeyQ"  # nothing refused was kept

    assert client.patch("/api/prefs", json={"hotkey": {"code": "F13", "mods": ["ctrl"]}}).status_code == 200  # an F-key: one is enough
    r = client.patch("/api/prefs", json={"hotkey": {"code": "F5", "mods": ["ctrl"]}})  # macOS's: focus on the toolbar
    assert r.status_code == 400 and "macOS uses ⌃F5 itself" in r.json()["error"]
    assert client.patch("/api/prefs", json={"hotkey": None, "hotkey_sound": False}).json()["hotkey"] is None  # turned off
    state = client.get("/api/app").json()
    assert state["prefs"]["hotkey"] is None and state["prefs"]["hotkey_sound"] is False
    assert client.patch("/api/prefs", json={"ui_language": "en"}).json()["hotkey"] is None  # stays off
    assert client.patch("/api/prefs", json={"hotkey": {}}).json()["hotkey"] == {"code": "KeyR", "mods": ["alt", "cmd"], "key": "R"}


AZERTY_KEYS = {"KeyQ": "a", "KeyA": "q", "KeyW": "z", "KeyZ": "w", "KeyM": ",", "Semicolon": "m", "Comma": ";"}


def test_a_shortcut_recorded_with_option_is_checked_by_the_keyboard_layout(client: TestClient, host: AppHost,
                                                                           tmp_path: Path) -> None:
    """With ⌥ held a Mac gives the page the character ⌥ types, so it may send no label: the server then
    goes by what the native shell read from macOS for that key (a French AZERTY keyboard here), as the
    page does — not by the US letter at that place."""
    assert client.get("/api/app").json()["keyboard_layout"] == {}  # the browser, Linux: not known
    # not known: the US letter of the physical key, as before
    assert client.patch("/api/prefs", json={"hotkey": {"code": "KeyQ", "mods": ["alt", "cmd"]}}).json()["hotkey"]["key"] == "Q"

    host.set_keyboard_layout(AZERTY_KEYS)  # the native shell, at launch
    assert client.get("/api/app").json()["keyboard_layout"] == AZERTY_KEYS
    # ⌥⌘ and the key printed W there (KeyZ) is Close All: refused, and said with the key's own letter
    r = client.patch("/api/prefs", json={"hotkey": {"code": "KeyZ", "mods": ["alt", "cmd"], "key": ""}})
    assert r.status_code == 400 and "⌥⌘W is a command every Mac app has" in r.json()["error"], r.text
    # ⌥⌘ and the key printed Z (KeyW) is fine there
    body = client.patch("/api/prefs", json={"hotkey": {"code": "KeyW", "mods": ["alt", "cmd"]}}).json()
    assert body["hotkey"] == {"code": "KeyW", "mods": ["alt", "cmd"], "key": "Z"}
    body = client.patch("/api/prefs", json={"hotkey": {"code": "KeyQ", "mods": ["alt", "cmd"]}}).json()
    assert body["hotkey"]["key"] == "A"  # the key printed A, not Q
    assert json.loads((tmp_path / "app.json").read_text())["hotkey"]["key"] == "A"
    r = client.patch("/api/prefs", json={"hotkey": {"code": "Semicolon", "mods": ["alt", "cmd"]}})  # AZERTY's M
    assert r.status_code == 400 and "⌥⌘M is a command every Mac app has" in r.json()["error"]
    # what the page saw is what counts when it sent it (⌃⌥: WebKit gives what the key types)
    assert client.patch("/api/prefs", json={"hotkey": {"code": "KeyQ", "mods": ["ctrl", "alt"], "key": "a"}}).json()[
        "hotkey"]["key"] == "A"
    # the host checks it the same way when it is called directly
    assert host.update_prefs({"hotkey": {"code": "KeyZ", "mods": ["ctrl", "alt"]}}).hotkey.key == "W"


def test_recording_a_shortcut_turns_the_current_one_off_for_a_while(client: TestClient, host: AppHost,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    from voicestream.app import host as host_mod

    assert client.post("/api/hotkey/recording", json={"active": True}).json()["hotkey"]["recording"] is True
    assert client.post("/api/hotkey/recording", json={"active": False}).json()["hotkey"]["recording"] is False
    client.post("/api/hotkey/recording", json={"active": True})
    client.patch("/api/prefs", json={"hotkey": {"code": "KeyK", "mods": ["ctrl", "alt"]}})  # saved: recording is over
    assert client.get("/api/app").json()["hotkey"]["recording"] is False
    monkeypatch.setattr(host_mod, "HOTKEY_RECORDING_S", 0.1)  # a page that never says it is done
    client.post("/api/hotkey/recording", json={"active": True})
    deadline = time.monotonic() + 5
    while client.get("/api/app").json()["hotkey"]["recording"] and time.monotonic() < deadline:
        time.sleep(0.05)
    assert client.get("/api/app").json()["hotkey"]["recording"] is False
    for bad in ({}, {"active": "yes please"}, {"active": True, "x": 1}):
        assert client.post("/api/hotkey/recording", json=bad).status_code == 400


def test_reveal_only_known_places(client: TestClient, host: AppHost, monkeypatch: pytest.MonkeyPatch) -> None:
    launched: list[list[str]] = []
    monkeypatch.setattr(desktop, "_launch", launched.append)
    monkeypatch.setattr(desktop.sys, "platform", "darwin")
    assert client.post("/api/reveal", json={"what": "data_dir"}).json() == {"ok": True}
    assert launched[-1] == ["open", str(host.cfg.data_path)]
    client.post("/api/reveal", json={"what": "config"})
    assert launched[-1] == ["open", "-R", str(host.config_path)]
    client.post("/api/reveal", json={"what": "meeting", "folder": PLANNING})
    assert launched[-1] == ["open", str(host.cfg.data_path / "sessions" / PLANNING)]
    client.post("/api/reveal", json={"what": "log"})  # no log file yet: its folder (or a clear 409)
    count = len(launched)
    for body in ({"what": "/etc/passwd"}, {"what": "meeting", "folder": "../.."}, {"what": "meeting"},
                 {"what": "data_dir", "path": "/"}, {"what": "meeting", "folder": "/etc"}):
        assert client.post("/api/reveal", json=body).status_code in (400, 404), body
    assert len(launched) == count


def test_open_settings_fixed_panes(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    launched: list[list[str]] = []
    monkeypatch.setattr(desktop, "_launch", launched.append)
    monkeypatch.setattr(desktop.sys, "platform", "darwin")
    assert client.post("/api/open-settings", json={"pane": "microphone"}).json() == {"ok": True}
    assert launched == [["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"]]
    for body in ({"pane": "x-apple.systempreferences:evil"}, {"pane": "file:///etc/passwd"}, {}):
        assert client.post("/api/open-settings", json=body).status_code == 400
    monkeypatch.setattr(desktop.sys, "platform", "linux")
    r = client.post("/api/open-settings", json={"pane": "screen_audio"})
    assert r.status_code == 409 and "macOS" in r.json()["error"]
    assert len(launched) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="xdg-open")
def test_reveal_elsewhere_opens_the_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(desktop.sys, "platform", "linux")
    f = tmp_path / "notes.md"
    f.write_text("x")
    assert desktop.reveal_command(f) == ["xdg-open", str(tmp_path)]
    with pytest.raises(desktop.DesktopError):
        desktop.reveal(tmp_path / "missing")


def test_the_settings_screen_shows_a_hand_edited_file(client: TestClient, host: AppHost) -> None:
    path = host.config_path
    path.write_text(path.read_text(encoding="utf-8").replace('user_name = "Alex"', 'user_name = "Camille Martin"'),
                    encoding="utf-8")  # edited by hand, as the app asks for some settings
    assert client.get("/api/settings").json()["values"]["user_name"] == "Camille Martin"
    assert host.cfg.user_name == "Camille Martin"


def test_a_meeting_cut_off_by_a_crash_is_finished_over_http(client: TestClient, host: AppHost,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    from test_app_meetings import crashed_meeting

    monkeypatch.setattr("voicestream.sinks.base.build_sinks", lambda app, only=None: [Rec(host.cfg)])
    folder = crashed_meeting(host.cfg)
    row = next(r for r in client.get("/api/meetings").json()["meetings"] if r["folder"] == folder)
    assert row["interrupted"] and not row["in_progress"]
    assert client.post(f"/api/meetings/{folder}/push", json={}).status_code == 200  # no force needed
    assert client.get(f"/api/meetings/{LIVE_ELSEWHERE}").json()["meeting"]["ended_at"] is None  # recorded elsewhere


def test_live_item_routes_reach_the_assistant_holding_the_meeting() -> None:
    from types import SimpleNamespace

    from voicestream.dashboard.server import _Api

    def assistant(*ids: str) -> Any:
        return SimpleNamespace(current=None, finished_meetings=[SimpleNamespace(meeting=SimpleNamespace(id=i)) for i in ids])

    new, old = assistant("m3"), assistant("m1", "m2")  # after a restart, the old one finishes m2's agents
    api = _Api(SimpleNamespace(assistant=new, assistants=[new, old], is_app_host=True), "")
    assert api.holder("m2") is old and api.holder("m3") is new and api.holder(None) is new and api.holder("m9") is new
