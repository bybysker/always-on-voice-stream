"""The app shells: ``voicestream app`` (launcher), the macOS shell's logic and the macOS hooks.

None of this needs a Mac: the host is a fake that serves the two pages the shells check, and
AppKit/PyObjC are replaced by small recorders where the code under test reaches for them. The
real window and menu bar are exercised on the macOS CI runner by the packaged app's
``--selftest`` (.github/workflows/mac-app.yml).
"""

from __future__ import annotations

import asyncio
import json
import os
import plistlib
import signal
import subprocess
import sys
import threading
import time
import types
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from voicestream import cli
from voicestream.app import (
    calls,
    floating,
    hotkey,
    launcher,
    native,
    native_hooks,
    spotlight,
)

TOKEN = "secret-token-123"


class FakeHost:
    """Just enough of AppHost: a loop thread, a tiny HTTP server, a recorded lifecycle."""

    def __init__(self, *, shutdown_s: float = 0.0, fail: Exception | None = None, state: str = "stopped") -> None:
        self.shutdown_s = shutdown_s
        self.fail = fail
        self.calls: list[tuple[str, Any]] = []
        self.capabilities = {"native_window": False, "login_item": True}
        self.listeners: list[Any] = []
        self._state = {"state": state, "in_meeting": False, "meeting": None, "error": None,
                       "prefs": {"ui_language": "auto", "start_at_login": False}}
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.server: ThreadingHTTPServer | None = None
        self.assistant: Any = None
        self.start_fails = False  # a press's start goes to the error state

    # --- the contract
    def run_in_thread(self) -> str:
        if self.fail is not None:
            raise self.fail
        self.thread.start()
        host = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802
                parts = urlsplit(self.path)
                token = parse_qs(parts.query).get("token", [None])[0] or self.headers.get("X-Voicestream-Token")
                if token != TOKEN:
                    body, ctype, code = b'{"error": "unauthorized"}', "application/json", 401
                elif parts.path == "/app":
                    body, ctype, code = b"<!doctype html><html><body>Voicestream</body></html>", "text/html", 200
                elif parts.path == "/api/app":
                    body, ctype, code = json.dumps({**host.state(), "version": "9.9"}).encode(), "application/json", 200
                else:
                    body, ctype, code = b"{}", "application/json", 404
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.server.server_address[1]}/app?token={TOKEN}"

    def call(self, coro: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def state(self) -> dict[str, Any]:
        return dict(self._state)

    def add_state_listener(self, fn: Any) -> None:
        self.listeners.append(fn)

    async def shutdown(self, force: bool = False) -> None:
        self.calls.append(("shutdown", force))
        if not force and self.shutdown_s:
            await asyncio.sleep(self.shutdown_s)

    async def listen_start(self) -> None:
        self.calls.append(("listen_start", None))

    async def listen_pause(self) -> None:
        self.calls.append(("listen_pause", None))

    async def listen_resume(self) -> None:
        self.calls.append(("listen_resume", None))

    async def listen_stop(self, force: bool = False) -> None:
        self.calls.append(("listen_stop", force))

    async def listen_restart(self, force: bool = False) -> None:
        self.calls.append(("listen_restart", force))

    def toggle_action(self) -> str | None:
        return hotkey.TOGGLE.get(self._state["state"])

    def meeting_action(self, what: str = "toggle") -> str | None:
        start = {"stopped": "start", "error": "restart", "paused": "resume"}
        if what == "stop":
            return "stop" if self._state["state"] in ("starting", "listening", "paused", "error") else None
        return (hotkey.TOGGLE if what == "toggle" else start).get(self._state["state"])

    async def meeting_toggle(self) -> str | None:
        """As AppHost's: a start that went to the error state (``start_fails``) is said as ``error``."""
        self.calls.append(("meeting_toggle", None))
        action = self.toggle_action()
        return "error" if action in ("start", "restart") and self.start_fails else action

    async def listen_toggle(self) -> str | None:
        return await self.meeting_toggle()

    async def meeting_start(self) -> str | None:
        self.calls.append(("meeting_start", None))
        return self.meeting_action("start")

    async def meeting_stop(self) -> str | None:
        self.calls.append(("meeting_stop", None))
        return self.meeting_action("stop")

    async def meeting_keep(self) -> bool:
        self.calls.append(("meeting_keep", None))
        return True

    async def suggestion_accept(self) -> str | None:
        self.calls.append(("suggestion_accept", None))
        return "start"

    def suggestion_dismiss(self) -> bool:
        self.calls.append(("suggestion_dismiss", None))
        return True

    def dismiss_notice(self) -> bool:
        self.calls.append(("dismiss_notice", None))
        return True

    def suggest_meeting(self, app_name: str | None = None, call_id: str | None = None) -> bool:
        self.calls.append(("suggest_meeting", (app_name, call_id)))
        return True

    def call_ended(self, app_name: str | None = None, call_id: str | None = None) -> None:
        self.calls.append(("call_ended", (app_name, call_id)))

    def request_focus(self, target: str = "start") -> None:
        self.calls.append(("request_focus", target))

    url_key = "K" * 43

    def launcher_url(self) -> str:
        return f"voicestream://meeting/start?key={self.url_key}"

    async def open_url(self, url: str) -> str:
        self.calls.append(("open_url", url))
        return self.url_outcome

    url_outcome = "start"

    def set_hotkey_status(self, status: str, error: int | None = None) -> None:
        self.calls.append(("set_hotkey_status", (status, error)))

    def set_keyboard_layout(self, keys: dict[str, str]) -> None:
        self.calls.append(("set_keyboard_layout", dict(keys)))

    def update_prefs(self, changes: dict[str, Any]) -> None:
        self.calls.append(("update_prefs", changes))

    # --- test helpers
    def close(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread.is_alive():
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(5)


@pytest.fixture
def host() -> Any:
    h = FakeHost()
    yield h
    h.close()


def _wait(pred: Any, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


# ------------------------------------------------------------------ arguments


def test_parser_options() -> None:
    args = launcher.build_parser().parse_args(["-c", "~/vs.toml", "--no-open", "--port", "8800", "--selftest", "5",
                                               "--selftest-report", "r.json", "-vv"])
    assert (args.config, args.no_open, args.browser, args.port, args.selftest, args.selftest_report, args.verbose) == (
        "~/vs.toml", True, False, 8800, 5.0, "r.json", 2)
    with pytest.raises(SystemExit):
        launcher.build_parser().parse_args(["--browser", "--no-open"])


def test_macos_launch_arguments_are_dropped_or_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []
    monkeypatch.setattr(launcher, "run", lambda args: seen.append(args) or 0)
    assert launcher.main(["-psn_0_12345", "--no-open"]) == 0
    assert seen[-1].no_open
    with pytest.raises(SystemExit):  # from a terminal a typo is an error...
        launcher.main(["--no-open", "-NSDocumentRevisionsDebugMode", "YES"])
    monkeypatch.setattr(sys, "frozen", True, raising=False)  # ...but the packaged app must open anyway
    assert launcher.main(["--no-open", "-NSDocumentRevisionsDebugMode", "YES"]) == 0
    assert seen[-1].no_open


@pytest.mark.parametrize(
    ("argv", "platform", "native_ok", "mode"),
    [
        (["--no-open"], "darwin", True, "headless"),
        (["--browser"], "darwin", True, "browser"),
        ([], "darwin", True, "native"),
        ([], "darwin", False, "browser"),
        ([], "linux", True, "browser"),
        ([], "win32", True, "browser"),
    ],
)
def test_choose_mode(monkeypatch: pytest.MonkeyPatch, argv: list[str], platform: str, native_ok: bool, mode: str) -> None:
    monkeypatch.setattr(launcher, "native_available", lambda: native_ok)
    assert launcher.choose_mode(launcher.build_parser().parse_args(argv), platform) == mode


def test_native_available_only_looks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launcher, "find_spec", lambda name: None if name == "webview" else object())
    assert launcher.native_available() is False
    monkeypatch.setattr(launcher, "find_spec", lambda name: object())
    assert launcher.native_available() is True


def test_cli_app_subcommand(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    made: list[Any] = []

    def make_host(config: Any, port: Any) -> FakeHost:
        made.append((config, port))
        h = FakeHost()
        made.append(h)
        return h

    monkeypatch.setattr(launcher, "make_host", make_host)
    report = tmp_path / "r.json"
    try:
        rc = cli.main(["-c", str(tmp_path / "vs.toml"), "app", "--no-open", "--port", "9123",
                       "--selftest", "0.2", "--selftest-report", str(report)])
    finally:
        for h in made[1::2]:
            h.close()
    assert rc == 0
    assert made[0] == (str(tmp_path / "vs.toml"), 9123)
    assert json.loads(report.read_text())["ok"] is True


# ------------------------------------------------------------ headless / browser


def test_headless_selftest_reports_and_stops_gracefully(host: FakeHost, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    report = tmp_path / "out" / "selftest.json"
    assert launcher.serve(host, mode="headless", selftest=0.3, report_path=report) == 0
    data = json.loads(report.read_text())
    assert data["ok"] is True and data["mode"] == "headless"
    assert data["checks"] == {"app_page": True, "api_app": True}
    assert data["http"]["api_app"]["state"] == "stopped"
    assert data["quit"]["graceful"] is True
    assert TOKEN not in report.read_text()  # the report is shared; the token opens the app
    assert (tmp_path / "out" / "selftest.json.phase").read_text().strip() == "done"
    assert host.calls == [("shutdown", False)]
    assert "http://127.0.0.1:" in capsys.readouterr().out  # --no-open prints where it is


def test_browser_mode_opens_the_page_and_quits_on_signal(host: FakeHost) -> None:
    opened: list[str] = []
    stop = launcher.StopRequest()
    threading.Timer(0.3, stop, args=(signal.SIGTERM,)).start()
    assert launcher.serve(host, mode="browser", stop=stop, open_url=opened.append) == 0
    assert len(opened) == 1 and opened[0].endswith(f"/app?token={TOKEN}")
    assert host.calls == [("shutdown", False)]


def test_selftest_fails_when_the_ui_does_not_answer(host: FakeHost, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launcher, "check_http", lambda url, timeout=10: {"page": {"status": 500, "html": False}})
    report = tmp_path / "r.json"
    assert launcher.serve(host, mode="headless", selftest=0.1, report_path=report) == 1
    data = json.loads(report.read_text())
    assert data["ok"] is False and data["checks"] == {"app_page": False, "api_app": False}


def test_start_failure(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    h = FakeHost(fail=RuntimeError("Microphone permission is off"))
    report = tmp_path / "r.json"
    assert launcher.serve(h, mode="headless", selftest=1, report_path=report) == 2
    assert "Microphone permission is off" in capsys.readouterr().err
    assert json.loads(report.read_text()) == {"ok": False, "mode": "headless", "errors": ["start: Microphone permission is off"]}


def test_shutdown_is_forced_after_the_grace_period() -> None:
    h = FakeHost(shutdown_s=30)
    h.thread.start()
    try:
        t0 = time.monotonic()
        assert launcher.shutdown_host(h, grace=0.3, force_after=2) is False
        assert time.monotonic() - t0 < 2
        assert h.calls == [("shutdown", False), ("shutdown", True)]
    finally:
        h.close()


def test_second_signal_forces_at_once() -> None:
    stop = launcher.StopRequest()
    stop(signal.SIGINT)
    assert stop.requested.is_set() and not stop.force.is_set()
    stop(signal.SIGINT)
    assert stop.force.is_set()
    h = FakeHost(shutdown_s=30)
    h.thread.start()
    try:
        t0 = time.monotonic()
        assert launcher.shutdown_host(h, grace=20, force_event=stop.force) is False
        assert time.monotonic() - t0 < 2
    finally:
        h.close()


def test_shutdown_of_a_host_that_is_gone() -> None:
    class Gone(FakeHost):
        def call(self, coro: Any) -> Any:
            coro.close()
            raise RuntimeError("the host is not running")

    assert launcher.shutdown_host(Gone()) is True


def test_missing_packages_are_explained(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setitem(sys.modules, "voicestream.app.host", None)  # as if the app's extras were missing
    assert launcher.run(launcher.build_parser().parse_args(["--no-open"])) == 2
    assert "pip install 'voicestream[dashboard,app]'" in capsys.readouterr().err


def test_make_host_passes_the_config_and_the_port(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[Any] = []

    class WithPort:
        def __init__(self, config_path: Any = None, port: int | None = None) -> None:
            seen.append((config_path, port))

    class WithoutPort:
        def __init__(self, config_path: Any = None) -> None:
            seen.append((config_path,))

    monkeypatch.setitem(sys.modules, "voicestream.app.host", types.SimpleNamespace(AppHost=WithPort))
    launcher.make_host(str(tmp_path / "a.toml"), 8801)
    monkeypatch.setitem(sys.modules, "voicestream.app.host", types.SimpleNamespace(AppHost=WithoutPort))
    launcher.make_host(None, 8801)  # warns, uses any free port
    assert seen == [(tmp_path / "a.toml", 8801), (None,)]


def test_capabilities_follow_what_this_system_can_do(host: FakeHost, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native_hooks, "login_item_status", lambda: "unsupported")
    launcher._set_capabilities(host)
    assert host.capabilities["login_item"] is False
    monkeypatch.setattr(native_hooks, "login_item_status", lambda: "disabled")
    launcher._set_capabilities(host)
    assert host.capabilities["login_item"] is True


def test_redact() -> None:
    assert launcher.redact(f"http://127.0.0.1:5/app?token={TOKEN}&x=1") == "http://127.0.0.1:5/app?token=redacted&x=1"


def test_std_streams_are_never_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdout", None)
    launcher._ensure_std_streams()
    print("no crash")  # a windowed app has no console
    assert sys.stdout is not None


# --------------------------------------------------------- native shell: pure logic


NOW = datetime(2026, 10, 19, 15, 0, 0, tzinfo=UTC)


def _state(state: str = "listening", minutes: float | None = None, **extra: Any) -> dict[str, Any]:
    """A host state; meetings start automatically unless ``mode="manual"`` (what most of these tests are about:
    the menu bar of always-on listening)."""
    meeting = None
    if minutes is not None:
        meeting = {"id": "m1", "title": "Sync", "started_at": (NOW - timedelta(minutes=minutes)).isoformat()}
    return {"state": state, "in_meeting": meeting is not None, "meeting": meeting, "error": None,
            "prefs": {"ui_language": "auto"}, "mode": "automatic", **extra}


def test_native_modules_import_without_pyobjc() -> None:
    """In a process where none of the macOS frameworks can be imported, both modules import and
    the hooks degrade to "can't tell" instead of raising."""
    code = (
        "import sys\n"
        "for m in ('objc', 'AppKit', 'Foundation', 'WebKit', 'AVFoundation', 'ServiceManagement', 'PyObjCTools', 'webview'):\n"
        "    sys.modules[m] = None\n"
        "sys.platform = 'darwin'\n"
        "from voicestream.app import hotkey, native, native_hooks\n"
        "assert native.status_symbol({'state': 'listening'}) == 'mic.fill'\n"
        "hotkey.CARBON_PATHS = ('/nonexistent/HIToolbox',)\n"  # nor HIToolbox (a Mac has it whatever we do)
        "assert hotkey.available() is False\n"  # said, not raised
        "assert native.listen_shortcut({'prefs': {'hotkey': {'code': 'KeyR', 'mods': ['alt', 'cmd']}},"
        " 'hotkey': {'status': 'ok'}}) == ('r', 0x180000)\n"
        "assert native_hooks.microphone_authorization() is None\n"
        "assert native_hooks.request_microphone() is None\n"
        "assert native_hooks.login_item_status() == 'unsupported'\n"
        "assert native_hooks.set_login_item(True) == 'unsupported'\n"
        "print('ok')\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False)
    assert out.returncode == 0 and out.stdout.strip() == "ok", out.stderr


@pytest.mark.parametrize(
    ("pref", "system", "lang"),
    [("fr", ["en-US"], "fr"), ("en", ["fr-FR"], "en"), ("auto", ["fr-CA", "en"], "fr"), ("auto", ["de-DE", "fr"], "en"),
     ("auto", [], "en"), (None, ["fr"], "fr")],
)
def test_pick_language(pref: str | None, system: list[str], lang: str) -> None:
    assert native.pick_language(pref, system) == lang


def test_timer_and_symbols() -> None:
    assert [native.format_elapsed(s) for s in (0, 65, 754, 3723, -5)] == ["0:00", "1:05", "12:34", "1:02:03", "0:00"]
    assert {s: native.status_symbol({"state": s}) for s in ("listening", "paused", "stopped", "error", "starting")} == {
        "listening": "mic.fill", "paused": "pause.circle", "stopped": "mic.slash",
        "error": "exclamationmark.triangle", "starting": "mic"}
    assert native.status_symbol({}) == "mic.slash"
    assert native.status_title(_state("listening", minutes=12.5), NOW) == "12:30"
    assert native.status_title(_state("paused", minutes=1), NOW) == "1:00"
    assert native.status_title(_state("listening"), NOW) == ""
    assert native.status_title(_state("stopping", minutes=3), NOW) == ""


def test_meeting_start_parsing() -> None:
    assert native.meeting_started({"in_meeting": True, "meeting": {"started_at": "2026-10-19T14:00:00Z"}}) == NOW - timedelta(hours=1)
    naive = native.meeting_started({"in_meeting": True, "meeting": {"started_at": "2026-10-19T14:00:00"}})
    assert naive is not None and naive.tzinfo is UTC
    assert native.meeting_started({"in_meeting": True, "meeting": {"started_at": "yesterday"}}) is None
    assert native.meeting_started({"in_meeting": False, "meeting": {"started_at": "2026-10-19T14:00:00Z"}}) is None


def test_state_line_in_both_languages() -> None:
    assert native.state_line(_state("listening"), "en", NOW) == "Listening"
    assert native.state_line(_state("listening", minutes=12.9), "en", NOW) == "Meeting in progress · 12 min"
    assert native.state_line(_state("listening", minutes=3), "fr", NOW) == "Réunion en cours · 3 min"
    assert native.state_line(_state("paused", minutes=3), "fr", NOW) == "En pause — rien n'est enregistré"
    assert native.state_line(_state("stopped"), "en", NOW) == "Not listening"
    err = native.state_line(_state("error", error="Microphone permission is off: System Settings → Privacy & Security "
                                                   "→ Microphone → Voicestream\nmore"), "en", NOW)
    assert err.startswith("Problem: Microphone permission is off") and err.endswith("…") and "\n" not in err
    assert native.state_line(_state("listening"), "en", NOW, quitting=True) == "Quitting — saving your meeting…"
    assert set(native.STRINGS["fr"]) == set(native.STRINGS["en"])
    # meetings started by hand: nothing is listened to between them
    assert native.state_line(_state("stopped", mode="manual"), "en", NOW) == "No meeting in progress"
    assert native.state_line(_state("stopped", mode="manual"), "fr", NOW) == "Aucune réunion en cours"
    assert native.state_line(_state("stopping", mode="manual"), "en", NOW) == "Saving the meeting…"
    assert native.state_line(_state("listening", minutes=3, mode="manual"), "en", NOW) == "Meeting in progress · 3 min"
    assert native.state_line({"state": "stopped"}, "en", NOW) == "No meeting in progress"  # manual: the default


def _menu(state: dict[str, Any], login: str = "disabled", **kw: Any) -> dict[str, native.MenuEntry]:
    return {e.key: e for e in native.menu_model(state, "en", login, now=NOW, **kw)}


def test_menu_model() -> None:
    keys = [e.key for e in native.menu_model(_state("stopped"), "en", "disabled", now=NOW)]
    for st in (_state("listening", 4), _state("paused", 4), _state("error"), _state("starting")):
        assert [e.key for e in native.menu_model(st, "fr", "enabled", now=NOW)] == keys  # built once, updated in place

    stopped = _menu(_state("stopped"))
    assert stopped["listen"].title == "Start Listening" and not stopped["pause"].visible  # menus: title case
    assert not stopped["end_meeting"].visible and not stopped["status"].enabled
    assert stopped["quit"].shortcut == "q" and not stopped["login"].checked

    meeting = _menu(_state("listening", minutes=4))
    assert meeting["pause"].visible and meeting["pause"].title == "Pause"
    assert meeting["listen"].title == "Stop Listening" and meeting["end_meeting"].visible

    paused = _menu(_state("paused", minutes=4))
    assert paused["pause"].title == "Resume Listening"

    starting = _menu(_state("starting"))
    assert starting["listen"].title == "Stop Listening" and starting["listen"].enabled  # may cancel a start
    assert not starting["pause"].visible and not _menu(_state("stopping"))["listen"].enabled

    assert not _menu(_state("stopped"), "unsupported")["login"].visible
    approval = _menu(_state("stopped"), "requires_approval")["login"]
    assert approval.checked and "System Settings" in approval.title

    quitting = _menu(_state("listening", minutes=4), quitting=True)
    assert not any(e.enabled for e in quitting.values() if not e.separator)

    # Start/Stop listening shows the keyboard shortcut, once macOS took it (the menu teaches it)
    assert stopped["listen"].shortcut == "" and stopped["quit"].modifiers == hotkey.APPKIT_MODIFIERS["cmd"]
    shortcut = {"prefs": {"hotkey": {"code": "KeyR", "mods": ["alt", "cmd"], "key": "R"}}, "hotkey": {"status": "ok"}}
    listen = _menu(_state("stopped", **shortcut))["listen"]
    assert (listen.shortcut, listen.modifiers) == ("r", (1 << 19) | (1 << 20))  # NSEventModifierFlagOption | Command
    assert _menu(_state("listening", **shortcut))["listen"].shortcut == "r"
    for status in ("taken", "failed", "pending", "off"):
        assert _menu(_state("stopped", **{**shortcut, "hotkey": {"status": status}}))["listen"].shortcut == ""
    azerty = {"prefs": {"hotkey": {"code": "KeyQ", "mods": ["ctrl", "cmd"], "key": "A"}}, "hotkey": {"status": "ok"}}
    assert _menu(_state("stopped", **azerty))["listen"].shortcut == "a"  # AppKit shows the character

    # paused, a press resumes (hotkey.TOGGLE): the shortcut is shown next to Resume listening, not next to
    # Stop listening, where it would be read as a stop
    assert hotkey.TOGGLE["paused"] == "resume"
    for lang, resume, stop in (("en", "Resume Listening", "Stop Listening"), ("fr", "Reprendre l'écoute", "Arrêter l'écoute")):
        paused = {e.key: e for e in native.menu_model(_state("paused", minutes=4, **shortcut), lang, "disabled", now=NOW)}
        assert (paused["pause"].title, paused["pause"].shortcut, paused["pause"].modifiers) == (resume, "r", 0x180000)
        assert (paused["listen"].title, paused["listen"].shortcut) == (stop, "")
        assert paused["listen"].modifiers == hotkey.APPKIT_MODIFIERS["cmd"]  # NSMenuItem's default, with no key
    listening = _menu(_state("listening", minutes=4, **shortcut))
    assert listening["pause"].shortcut == "" and listening["listen"].shortcut == "r"  # listening: a press stops
    for st in ("stopped", "error", "starting", "stopping", "listening"):
        assert native.shortcut_entry(_state(st)) == "listen"
    assert native.shortcut_entry(_state("paused")) == "pause"


def test_menu_model_when_meetings_start_by_hand() -> None:
    """Start/Stop Meeting; End Meeting Now would be Stop again, so it isn't there; Show/Hide Floating Button."""
    there = {"setup_done": True, "capabilities": {"floating_button": True}}
    stopped = _menu(_state("stopped", mode="manual", **there))
    assert (stopped["listen"].title, stopped["status"].title) == ("Start Meeting", "No meeting in progress")
    assert stopped["floating"].visible and stopped["floating"].title == "Hide Floating Button" and stopped["sep3"].visible
    meeting = _menu(_state("listening", minutes=4, mode="manual", **there))
    assert meeting["listen"].title == "Stop Meeting" and not meeting["end_meeting"].visible
    assert _menu(_state("paused", minutes=4, mode="manual"))["pause"].title == "Resume Meeting"
    hidden = _state("stopped", mode="manual", **there)
    hidden["prefs"] = {"floating_button": False}
    assert _menu(hidden)["floating"].title == "Show Floating Button"
    # before the setup, or without a floating button (the browser), there is nothing to show or hide
    assert not _menu(_state("stopped", mode="manual", capabilities={"floating_button": True}))["floating"].visible
    assert not _menu(_state("stopped", mode="manual", setup_done=True))["floating"].visible
    assert not _menu(_state("stopped", mode="manual"), "unsupported")["sep3"].visible
    fr = {e.key: e for e in native.menu_model(_state("listening", minutes=4, mode="manual", **there), "fr", "disabled", now=NOW)}
    assert (fr["listen"].title, fr["floating"].title) == ("Arrêter la réunion", "Masquer le bouton flottant")
    assert not _menu(_state("stopped", mode="manual", **there), quitting=True)["floating"].enabled


def test_meeting_menu_in_the_menu_bar() -> None:
    """Each command always there, disabled when it can't be used; the shortcut next to what a press does."""
    shortcut = {"prefs": {"hotkey": {"code": "KeyR", "mods": ["alt", "cmd"], "key": "R"}, "floating_button": True},
                "hotkey": {"status": "ok"}, "setup_done": True, "capabilities": {"floating_button": True}}

    def menu(st: dict[str, Any], **kw: Any) -> dict[str, native.MenuEntry]:
        return {e.key: e for e in native.meeting_menu_model(st, "en", **kw)}

    keys = [e.key for e in native.meeting_menu_model(_state("stopped"), "en")]
    assert keys == ["m_start", "m_stop", "m_pause", "m_end", "sep_m", "m_floating"]
    stopped = menu(_state("stopped", mode="manual", **shortcut))
    assert stopped["m_start"].title == "Start Meeting" and stopped["m_start"].enabled and stopped["m_start"].shortcut == "r"
    assert not stopped["m_stop"].enabled and not stopped["m_pause"].enabled and not stopped["m_end"].visible
    assert stopped["m_floating"].title == "Hide Floating Button" and stopped["m_floating"].visible
    rec = menu(_state("listening", minutes=2, mode="manual", **shortcut))
    assert rec["m_stop"].enabled and rec["m_stop"].shortcut == "r" and rec["m_start"].shortcut == ""
    assert not rec["m_start"].enabled and rec["m_pause"].enabled and rec["m_pause"].title == "Pause"
    paused = menu(_state("paused", minutes=2, mode="manual", **shortcut))
    assert paused["m_pause"].title == "Resume Meeting" and paused["m_pause"].shortcut == "r" and paused["m_stop"].shortcut == ""
    assert menu(_state("error", mode="manual", **shortcut))["m_start"].shortcut == "r"  # a press restarts
    starting = menu(_state("starting", mode="manual"))
    assert starting["m_stop"].enabled and not starting["m_start"].enabled
    auto = menu(_state("listening", minutes=2))
    assert auto["m_start"].title == "Start Listening" and auto["m_end"].visible and auto["m_end"].enabled
    assert not menu(_state("listening"))["m_end"].enabled  # no meeting yet
    assert not any(e.enabled for e in menu(_state("listening", minutes=2), quitting=True).values() if not e.separator)
    fr = {e.key: e for e in native.meeting_menu_model(_state("stopped", mode="manual"), "fr")}
    assert fr["m_start"].title == "Démarrer une réunion" and native.STRINGS["fr"]["meeting_menu"] == "Réunion"


def test_which_shortcut_the_shell_registers() -> None:
    hk = {"code": "KeyR", "mods": ["cmd", "alt"], "key": "R"}
    assert native.hotkey_wanted({"prefs": {"hotkey": hk}, "hotkey": {"recording": False}}) == ("KeyR", ("alt", "cmd"))
    assert native.hotkey_wanted({"prefs": {"hotkey": hk}, "hotkey": {"recording": True}}) is None  # Settings records one
    assert native.hotkey_wanted({"prefs": {"hotkey": hk}}, quitting=True) is None
    assert native.hotkey_wanted({"prefs": {"hotkey": None}}) is None and native.hotkey_wanted({}) is None


def test_quit_asks_only_while_listening() -> None:
    assert native.needs_quit_confirmation(_state("listening"))
    assert native.needs_quit_confirmation(_state("paused"))
    assert native.needs_quit_confirmation(_state("stopping", minutes=2))
    assert not native.needs_quit_confirmation(_state("stopped"))
    assert not native.needs_quit_confirmation(_state("error"))


# ------------------------------------------------- native shell: with a fake AppKit


class Recorder:
    """Any AppKit object: remembers the calls, returns what it was told to."""

    def __init__(self, **returns: Any) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.returns = returns

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)

        def method(*args: Any) -> Any:
            self.calls.append((name, args))
            return self.returns.get(name)

        return method

    def names(self) -> list[str]:
        return [n for n, _ in self.calls]


@pytest.fixture
def appkit(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    """A fake NSApplication, and an AppHelper.callAfter that runs at once."""
    app = Recorder(isHidden=True)
    monkeypatch.setitem(sys.modules, "AppKit", types.SimpleNamespace(
        NSApplication=types.SimpleNamespace(sharedApplication=lambda: app)))
    helper = types.SimpleNamespace(callAfter=lambda fn, *a, **k: fn(*a, **k))
    monkeypatch.setitem(sys.modules, "PyObjCTools", types.SimpleNamespace(AppHelper=helper))
    monkeypatch.setitem(sys.modules, "PyObjCTools.AppHelper", helper)
    return app


def _shell(host: FakeHost, **kw: Any) -> native.MacShell:
    shell = native.MacShell(host, f"http://127.0.0.1:1/app?token={TOKEN}", **kw)
    shell.window = types.SimpleNamespace(native=Recorder(isMiniaturized=False))
    return shell


def test_closing_the_window_hides_it_and_the_dock_icon(host: FakeHost, appkit: Recorder) -> None:
    shell = _shell(host)
    assert shell._on_closing() is False  # pywebview: False cancels the close
    assert shell.window.native.names() == ["orderOut_"]
    assert ("setActivationPolicy_", (native.POLICY_ACCESSORY,)) in appkit.calls and "hide_" in appkit.names()
    appkit.calls.clear()
    shell.show_window()
    assert appkit.calls[0] == ("setActivationPolicy_", (native.POLICY_REGULAR,))
    assert "unhide_" in appkit.names() and ("activateIgnoringOtherApps_", (True,)) in appkit.calls
    assert shell.window.native.names()[-1] == "makeKeyAndOrderFront_"
    shell.quitting = True
    assert shell._on_closing() is None  # pywebview's own quit (no delegate of ours) needs it to really close


def test_closing_the_window_while_our_delegate_wraps_up_a_quit_only_hides_it(
        host: FakeHost, appkit: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    """Closing the last window makes pywebview detach the app delegate: during our quit (up to ~35 s)
    that would skip applicationWillTerminate: (exit handlers, whisper-server cleanup)."""
    monkeypatch.setattr(native, "_PATCHED", True)
    shell = _shell(host)
    shell.quitting = True
    assert shell._on_closing() is False
    assert shell.window.native.names() == ["orderOut_"]


def test_menu_actions_only_post_to_the_host(host: FakeHost, appkit: Recorder) -> None:
    host.thread.start()
    shell = _shell(host)
    ended: list[str] = []

    class Assistant:
        async def end_meeting(self) -> None:
            ended.append("ended")

    host.assistant = Assistant()
    for state, key in (("listening", "pause"), ("paused", "pause"), ("stopped", "listen"), ("error", "listen"),
                       ("listening", "listen"), ("listening", "end_meeting")):
        shell.state = _state(state)
        shell.on_menu(key)
    assert _wait(lambda: len(host.calls) == 5 and ended)
    # the host's meeting actions, as every way of starting does ("error" may leave half the assistant
    # running: the host's start is a restart there)
    assert host.calls == [("listen_pause", None), ("listen_resume", None), ("meeting_start", None),
                          ("meeting_start", None), ("meeting_stop", None)]
    host.calls.clear()
    # the Meeting menu in the menu bar, and Show/Hide Floating Button
    for state, key in (("stopped", "m_start"), ("listening", "m_stop"), ("listening", "m_pause"), ("paused", "m_pause")):
        shell.state = _state(state)
        shell.on_menu(key)
    shell.state = _state("stopped", prefs={"floating_button": True})
    shell.on_menu("floating")
    shell.state = _state("stopped", prefs={"floating_button": False})
    shell.on_menu("m_floating")
    assert _wait(lambda: len(host.calls) == 6)
    assert host.calls == [("meeting_start", None), ("meeting_stop", None), ("listen_pause", None), ("listen_resume", None),
                          ("update_prefs", {"floating_button": False}), ("update_prefs", {"floating_button": True})]
    shell.on_menu("quit")
    assert appkit.names() == ["terminate_"]
    assert shell.quitting  # without our delegate, pywebview's quit asks the window: it must let go


def test_quit_while_listening_asks_then_stops_gracefully(host: FakeHost, appkit: Recorder,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    host.thread.start()
    shell = _shell(host)
    shell.state = _state("listening", minutes=5)
    answers = [False, True]
    monkeypatch.setattr(shell, "_confirm_quit", lambda: answers.pop(0))
    monkeypatch.setattr(native, "_system_is_quitting_us", lambda: False)

    assert shell.should_terminate() == native.TERMINATE_CANCEL  # "Cancel": keeps listening
    assert host.calls == [] and not shell.quitting

    assert shell.should_terminate() == native.TERMINATE_LATER
    assert shell.quitting
    assert shell.should_terminate() == native.TERMINATE_LATER  # Cmd+Q again: no second shutdown
    assert _wait(lambda: ("replyToApplicationShouldTerminate_", (True,)) in appkit.calls)
    assert host.calls == [("shutdown", False)]
    assert shell._quit_info["graceful"] is True


def test_quit_without_a_question_when_idle_or_at_logout(host: FakeHost, appkit: Recorder,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    host.thread.start()
    asked: list[bool] = []
    monkeypatch.setattr(native.MacShell, "_confirm_quit", lambda self: asked.append(True) or True)
    idle = _shell(host)
    idle.state = _state("stopped")
    assert idle.should_terminate() == native.TERMINATE_LATER
    monkeypatch.setattr(native, "_system_is_quitting_us", lambda: True)
    busy = _shell(host)
    busy.state = _state("listening", minutes=5)
    assert busy.should_terminate() == native.TERMINATE_LATER
    assert asked == []
    assert _wait(lambda: appkit.names().count("replyToApplicationShouldTerminate_") == 2)


def test_will_terminate_finishes_the_selftest_report(host: FakeHost, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[str] = []
    monkeypatch.setattr(native, "_run_exit_handlers", lambda: ran.append("atexit"))  # not pytest's own!
    report = tmp_path / "r.json"
    shell = _shell(host, selftest=1.0, report_path=report)
    shell.report = {"ok": True, "checks": {"window_loaded": True}}
    shell._quit_info = {"graceful": True, "shutdown_s": 0.4}
    shell.will_terminate()
    assert json.loads(report.read_text())["quit"] == {"graceful": True, "shutdown_s": 0.4}
    assert (tmp_path / "r.json.phase").read_text().strip() == "done"
    assert ran == ["atexit"]


class FakeNSObject:
    def __init_subclass__(cls, **kwargs: Any) -> None:
        """PyObjC's rule when it builds a class (objc.BadPrototypeError): a method that overrides one
        the class already has must keep that method's encoding."""
        super().__init_subclass__(**kwargs)
        for name, fn in vars(cls).items():
            mine = getattr(fn, "signature", None)
            inherited = next((vars(base)[name] for base in cls.__mro__[1:] if name in vars(base)), None)
            theirs = getattr(inherited, "signature", None)
            if mine is not None and theirs is not None and mine != theirs:
                raise RuntimeError(f"objc.BadPrototypeError: {name} has signature that is not compatible "
                                   f"with ObjC runtime: {theirs!r} != {mine!r}")

    @classmethod
    def alloc(cls) -> Any:
        return cls.__new__(cls)

    def init(self) -> Any:
        return self


@pytest.fixture
def pyobjc(monkeypatch: pytest.MonkeyPatch, appkit: Recorder) -> Any:
    """Stand-ins for objc (typedSelector records the encoding), Foundation, webview and pywebview's
    Cocoa backend, with the classes the shell defines once made fresh for each test."""
    def typed_selector(signature: bytes) -> Any:
        def mark(fn: Any) -> Any:
            fn.signature = signature
            return fn

        return mark

    objc = types.SimpleNamespace(typedSelector=typed_selector, _C_NSUInteger=b"Q", _C_NSBOOL=b"Z")  # 64-bit
    center = Recorder()
    foundation = types.SimpleNamespace(NSObject=FakeNSObject,
                                       NSNotificationCenter=types.SimpleNamespace(defaultCenter=lambda: center))

    class AppDelegate(FakeNSObject):  # pywebview's own: only applicationShouldTerminate_ (asks the windows)
        @typed_selector(b"I@:@")  # the encoding PyObjC 12.2's AppKit metadata gave it on the macOS runner
        def applicationShouldTerminate_(self, app: Any) -> bool:
            return True

    browser_view = types.SimpleNamespace(AppDelegate=AppDelegate, _shared_app_delegate=None)
    cocoa = types.ModuleType("webview.platforms.cocoa")
    cocoa.BrowserView = browser_view  # type: ignore[attr-defined]
    started: list[dict[str, Any]] = []

    class Hook(list):  # pywebview's events: ``window.events.closing += handler``
        def __iadd__(self, fn: Any) -> Any:
            self.append(fn)
            return self

    def create_window(*args: Any, **kwargs: Any) -> Any:
        return types.SimpleNamespace(events=types.SimpleNamespace(before_show=Hook(), closing=Hook()), native=Recorder())

    def start(**kwargs: Any) -> None:  # the delegate class pywebview would instantiate now
        started.append({**kwargs, "delegate_class": browser_view.AppDelegate})

    webview = types.ModuleType("webview")
    webview.create_window = create_window  # type: ignore[attr-defined]
    webview.start = start  # type: ignore[attr-defined]
    platforms = types.ModuleType("webview.platforms")
    platforms.cocoa = cocoa  # type: ignore[attr-defined]
    for name, module in (("objc", objc), ("Foundation", foundation), ("webview", webview),
                         ("webview.platforms", platforms), ("webview.platforms.cocoa", cocoa)):
        monkeypatch.setitem(sys.modules, name, module)
    for name, value in (("_PATCHED", False), ("_DELEGATE_CLASS", None), ("_TARGET_CLASS", None), ("_SHELL", None)):
        monkeypatch.setattr(native, name, value)
    monkeypatch.setattr(native_hooks, "app_support_dir", lambda: Path.home() / "vs-support")
    return types.SimpleNamespace(objc=objc, base=AppDelegate, browser_view=browser_view, center=center, started=started)


def test_app_delegate_is_a_subclass_with_the_runtime_encodings(host: FakeHost, pyobjc: Any,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """The macOS CI run: PyObjC refused the old objc.Category whole (BadPrototypeError, ``I@:@ != Q@:@``:
    pywebview's applicationShouldTerminate: is b"I@:@", ours said b"Q@:@"), so Dock-reopen and our quit
    were missing and the self-test hung on quitting. A subclass meets the same rule (the fake objc
    enforces it), so the override carries no encoding of its own: PyObjC copies pywebview's."""
    host.thread.start()  # a reopen also asks the host to focus the page's Start control
    native.install_app_delegate(pyobjc.browser_view)
    cls = pyobjc.browser_view.AppDelegate
    assert cls is not pyobjc.base and issubclass(cls, pyobjc.base) and cls.__name__ == "VoicestreamAppDelegate"
    assert not hasattr(vars(cls)["applicationShouldTerminate_"], "signature")  # inherits b"I@:@"
    assert cls.applicationShouldHandleReopen_hasVisibleWindows_.signature == b"Z@:@Z"
    assert cls.applicationWillTerminate_.signature == b"v@:@"
    assert pyobjc.base().applicationShouldTerminate_(None) is True  # pywebview's own class is left alone
    native.install_app_delegate(pyobjc.browser_view)  # once
    assert pyobjc.browser_view.AppDelegate is cls

    shell = _shell(host)
    monkeypatch.setattr(native, "_SHELL", shell)
    seen: list[str] = []
    monkeypatch.setattr(shell, "should_terminate", lambda: seen.append("terminate") or native.TERMINATE_LATER)
    monkeypatch.setattr(shell, "show_window", lambda: seen.append("show"))
    monkeypatch.setattr(shell, "will_terminate", lambda: seen.append("will_terminate"))
    delegate = cls.alloc().init()
    assert delegate.applicationShouldTerminate_(None) == native.TERMINATE_LATER
    assert delegate.applicationShouldHandleReopen_hasVisibleWindows_(None, False) is False
    delegate.applicationWillTerminate_(None)
    assert seen == ["terminate", "show", "will_terminate"] and delegate.voicestreamPatched()
    # opened again (the Dock, Finder, Spotlight): the window, with the keyboard focus on the Start control
    assert _wait(lambda: ("request_focus", "start") in host.calls)


def test_the_delegate_is_in_place_before_the_window_opens(host: FakeHost, pyobjc: Any) -> None:
    host.thread.start()
    shell = native.MacShell(host, f"http://127.0.0.1:1/app?token={TOKEN}")
    assert shell.run() == 0
    (started,) = pyobjc.started
    assert started["delegate_class"].__name__ == "VoicestreamAppDelegate" and shell.delegate_error is None
    # the shell also hears NSApplicationWillTerminateNotification itself, whichever delegate the app has
    ((name, (target, selector, notification, _obj)),) = pyobjc.center.calls
    assert (name, selector, notification, target.shell) == ("addObserver_selector_name_object_", "appWillTerminate:",
                                                          "NSApplicationWillTerminateNotification", shell)
    assert host.calls == [("shutdown", False)]  # the run loop stopped without AppKit ending the process


def test_a_failed_delegate_install_is_reported_and_quitting_still_works(
        host: FakeHost, pyobjc: Any, appkit: Recorder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The self-test with no delegate of ours: the failure is in the report, its quit goes through
    pywebview's own handler (the window lets go), and the will-terminate notification still stops the
    host, writes the last report and ends the process with the self-test's status."""
    typed_selector = pyobjc.objc.typedSelector

    def refuse(signature: bytes) -> Any:
        if signature == native.SIG_REOPEN:  # as if this PyObjC disagreed with the runtime
            raise RuntimeError("objc.BadPrototypeError: has signature that is not compatible with ObjC runtime")
        return typed_selector(signature)

    pyobjc.objc.typedSelector = refuse
    report = tmp_path / "selftest.json"
    shell = native.MacShell(host, host.run_in_thread(), selftest=0.0, report_path=report)
    shell.run()
    assert "BadPrototypeError" in (shell.delegate_error or "") and not native._PATCHED
    assert pyobjc.started[0]["delegate_class"] is pyobjc.base  # pywebview's own, still usable
    host.calls.clear()
    loaded = threading.Event()
    loaded.set()
    page = json.dumps({"ready": "complete", "path": "/app", "body_chars": 120})
    shell.window = types.SimpleNamespace(events=types.SimpleNamespace(loaded=loaded), native=Recorder(isMiniaturized=False),
                                         run_js=lambda script: page)
    monkeypatch.setattr(native, "time", types.SimpleNamespace(monotonic=time.monotonic, sleep=lambda s: None))
    try:
        shell._selftest()
        data = json.loads(report.read_text())
        assert data["delegate"]["install_error"] == shell.delegate_error
        assert data["checks"]["app_delegate_patched"] is False and data["ok"] is False and shell.exit_code == 1
        assert data["checks"]["window_loaded"] and data["checks"]["page_rendered"]
        assert appkit.names()[-1] == "terminate_" and shell.quitting  # quit like the menu: the window lets go
        assert shell._on_closing() is None
        assert shell._watchdog is not None and shell._watchdog.interval == native.SELFTEST_QUIT_S  # a deadline on quitting

        exits: list[int] = []
        monkeypatch.setattr(native, "os", types.SimpleNamespace(_exit=exits.append))
        monkeypatch.setattr(native, "_run_exit_handlers", lambda: exits.append(-1))  # not pytest's own!
        shell.will_terminate()  # from the notification: our applicationShouldTerminate: never ran
        shell.will_terminate()  # and again from a delegate: once is enough
        assert host.calls == [("shutdown", False)] and exits == [-1, 1]
        data = json.loads(report.read_text())
        assert data["quit"]["delegate"] is False and data["quit"]["graceful"] is True
        assert (tmp_path / "selftest.json.phase").read_text().strip() == "done"
    finally:
        if shell._watchdog is not None:
            shell._watchdog.cancel()  # it would end this very process


def test_the_selftest_ends_the_process_when_quitting_goes_nowhere(host: FakeHost, tmp_path: Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    exited = threading.Event()
    codes: list[int] = []
    monkeypatch.setattr(native, "os", types.SimpleNamespace(_exit=lambda code: (codes.append(code), exited.set())))
    report = tmp_path / "selftest.json"
    shell = _shell(host, selftest=1.0, report_path=report)
    shell.report = {"ok": True, "checks": {"window_loaded": True}, "errors": []}
    shell._arm_watchdog(30, "never")
    shell._arm_watchdog(0.05, "the app did not quit within 50 s")  # re-armed: only the last deadline counts
    assert exited.wait(5) and codes == [3]
    data = json.loads(report.read_text())
    assert data["ok"] is False and data["errors"] == ["the app did not quit within 50 s"]
    assert (tmp_path / "selftest.json.phase").read_text().strip() == "hung"


def test_login_toggle_goes_through_the_host_prefs(host: FakeHost, appkit: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    host.thread.start()
    shell = _shell(host)
    statuses = iter(["enabled"])
    monkeypatch.setattr(native_hooks, "login_item_status", lambda: next(statuses))
    rendered: list[str] = []
    monkeypatch.setattr(shell, "render", lambda: rendered.append(shell._login))
    shell._login = "disabled"
    shell.on_menu("login")
    assert _wait(lambda: rendered == ["enabled"])
    assert host.calls == [("update_prefs", {"start_at_login": True})]


# ---------------------------------------------------------- native shell: keyboard shortcut


class FakeHotKeys:
    """hotkey.CarbonHotKey without Carbon: records registrations, answers what macOS would."""

    def __init__(self, on_press: Any) -> None:
        self.on_press = on_press
        self.calls: list[tuple[Any, ...]] = []
        self.registered: tuple[str, tuple[str, ...]] | None = None
        self.answers: dict[str, int] = {}  # code -> OSStatus
        self.presses = 0

    def register(self, code: str, mods: Any) -> int:
        self.calls.append(("register", code, tuple(mods)))
        err = self.answers.get(code, 0)
        self.registered = (code, tuple(mods)) if err == 0 else None
        return err

    def unregister(self) -> None:
        self.calls.append(("unregister",))
        self.registered = None

    def close(self) -> None:
        self.calls.append(("close",))

    def send_test_press(self) -> int:  # Carbon calls the handler synchronously, on the main thread
        self.presses += 1
        self.on_press()
        return 0


def _hk_state(state: str = "stopped", *, code: str | None = "KeyR", mods: tuple[str, ...] = ("alt", "cmd"),
              status: str = "pending", error: int | None = None, recording: bool = False,
              sound: bool = True, hotkey_cap: bool = True) -> dict[str, Any]:
    prefs = {"ui_language": "auto", "hotkey_sound": sound,
             "hotkey": {"code": code, "mods": list(mods), "key": code[-1]} if code else None}
    return {**_state(state), "prefs": prefs, "capabilities": {"hotkey": hotkey_cap},
            "hotkey": {"status": status, "error": error, "recording": recording}}


@pytest.fixture
def hotkeys(monkeypatch: pytest.MonkeyPatch) -> list[FakeHotKeys]:
    made: list[FakeHotKeys] = []
    monkeypatch.setattr(native.hotkey, "CarbonHotKey", lambda on_press: made.append(FakeHotKeys(on_press)) or made[-1])
    return made


def _reports(host: FakeHost) -> list[Any]:
    return [args for name, args in host.calls if name == "set_hotkey_status"]


def test_the_shortcut_is_registered_from_the_preferences(host: FakeHost, appkit: Recorder, hotkeys: list[FakeHotKeys],
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    host.thread.start()
    shell = _shell(host)
    shell.state = _hk_state()
    shell._sync_hotkey()
    assert hotkeys == []  # not before the app has finished launching
    shell._launched = True
    shell.state = _hk_state(hotkey_cap=False)  # the browser, or no Carbon
    shell._sync_hotkey()
    assert hotkeys == []

    shell.state = _hk_state()
    shell._sync_hotkey()
    (hk,) = hotkeys
    assert hk.calls == [("register", "KeyR", ("alt", "cmd"))]
    assert _wait(lambda: _reports(host) == [("ok", None)])  # macOS took it: Settings shows it
    shell.state = _hk_state(status="ok")  # the host's state says it now: nothing more to do
    shell._sync_hotkey()
    shell._sync_hotkey()
    time.sleep(0.1)
    assert len(hk.calls) == 1 and len(_reports(host)) == 1

    # Settings records a new one: the current one is off meanwhile, so pressing it reaches the page
    shell.state = _hk_state(status="ok", recording=True)
    shell._sync_hotkey()
    assert hk.calls[-1] == ("unregister",) and _wait(lambda: _reports(host)[-1] == ("paused", None))
    # a new one another app holds: said, and not tried again at every state …
    hk.answers["KeyT"] = hotkey.HOT_KEY_EXISTS
    shell.state = _hk_state(code="KeyT", mods=("ctrl", "alt"), status="pending")
    shell._sync_hotkey()
    assert hk.calls[-1] == ("register", "KeyT", ("ctrl", "alt"))
    assert _wait(lambda: _reports(host)[-1] == ("taken", -9878))
    shell.state = _hk_state(code="KeyT", mods=("ctrl", "alt"), status="taken", error=-9878)
    shell._sync_hotkey()
    assert hk.calls[-1] == ("register", "KeyT", ("ctrl", "alt")) and len(hk.calls) == 3
    # … only when Settings saves it again (pending): the other app may have let it go
    del hk.answers["KeyT"]
    shell.state = _hk_state(code="KeyT", mods=("ctrl", "alt"), status="pending")
    shell._sync_hotkey()
    assert len(hk.calls) == 4 and hk.registered == ("KeyT", ("ctrl", "alt"))
    assert _wait(lambda: _reports(host)[-1] == ("ok", None))

    shell.state = _hk_state(code=None, status="ok")  # turned off
    shell._sync_hotkey()
    assert hk.calls[-1] == ("unregister",) and _wait(lambda: _reports(host)[-1] == ("off", None))
    shell.state = _hk_state(status="off")
    shell._sync_hotkey()
    assert hk.calls[-1] == ("register", "KeyR", ("alt", "cmd"))  # on again
    shell.quitting = True  # quitting: a press must not start listening again
    shell._sync_hotkey()
    assert hk.calls[-1] == ("unregister",) and hk.registered is None
    monkeypatch.setattr(native, "_run_exit_handlers", lambda: None)  # not pytest's own!
    shell.will_terminate()
    assert hk.calls[-1] == ("close",)


def test_a_press_toggles_listening_and_plays_a_sound(host: FakeHost, appkit: Recorder, hotkeys: list[FakeHotKeys],
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    host.thread.start()
    played: list[str] = []

    class Sound:
        def __init__(self, name: str) -> None:
            self.name = name

        def play(self) -> None:
            played.append(self.name)

    sounds = types.SimpleNamespace(soundNamed_=lambda name: Sound(name))
    monkeypatch.setitem(sys.modules, "AppKit", types.SimpleNamespace(NSApplication=sys.modules["AppKit"].NSApplication,
                                                                    NSSound=sounds))
    shell = _shell(host)
    shell._launched = True
    for state, sound in (("stopped", "Tink"), ("listening", "Pop"), ("paused", "Tink"), ("error", "Tink")):
        host._state["state"] = state
        shell.state = _hk_state(state, status="ok")
        before = len(played)
        shell.on_hotkey()  # from Carbon's handler, on the main thread: only posts to the host
        assert _wait(lambda s=sound, n=before: len(played) == n + 1 and played[-1] == s)
    assert [c for c in host.calls if c[0] == "meeting_toggle"] == [("meeting_toggle", None)] * 4
    host._state["state"] = "starting"  # nothing to do: no sound either
    shell.on_hotkey()
    assert _wait(lambda: len([c for c in host.calls if c[0] == "meeting_toggle"]) == 5)
    time.sleep(0.1)
    assert len(played) == 4
    host._state["state"] = "stopped"
    shell.state = _hk_state("stopped", status="ok", sound=False)  # turned off in Settings
    shell.on_hotkey()
    assert _wait(lambda: len([c for c in host.calls if c[0] == "meeting_toggle"]) == 6)
    time.sleep(0.1)
    assert len(played) == 4

    # a start that failed (a missing key, a permission…): not the "it worked" sound, another one
    host.start_fails = True
    for state in ("stopped", "error"):
        host._state["state"] = state
        shell.state = _hk_state(state, status="ok")
        before = len(played)
        shell.on_hotkey()
        assert _wait(lambda n=before: len(played) == n + 1)
        assert played[-1] == "Basso" and native.SOUNDS["error"] not in (native.SOUNDS["start"], native.SOUNDS["stop"])
    host._state["state"] = "listening"  # a stop is said at once, whatever comes of its wrap-up
    shell.state = _hk_state("listening", status="ok")
    shell.on_hotkey()
    assert _wait(lambda: played[-1] == "Pop")


def test_the_selftest_presses_the_shortcut_without_listening(host: FakeHost, appkit: Recorder,
                                                             hotkeys: list[FakeHotKeys]) -> None:
    """A synthetic press reaches the host, which only says what it would do: nothing is started."""
    host.thread.start()
    shell = _shell(host)
    shell._launched = True
    shell.state = _hk_state()
    shell._sync_hotkey()
    shell.state = _hk_state(status="ok")
    listen = Recorder(keyEquivalent="r", keyEquivalentModifierMask=hotkey.appkit_mask(["alt", "cmd"]))
    shell._entries["listen"] = listen
    out = shell._check_hotkey()
    assert out["registered"] == "⌥⌘R" and out["register_result"] == 0 and out["in_menu"] is True
    assert (out["send_result"], out["handled"], out["host_answer"], out["expected"]) == (0, 1, "start", "start")
    assert out["reaches_host"] is True and out["temporary"] is False
    assert not [c for c in host.calls if c[0] == "meeting_toggle"]  # asked, never pressed for real
    assert shell._probe is None

    listen.returns["keyEquivalent"] = ""  # the menu doesn't show it
    assert shell._check_hotkey()["in_menu"] is False
    # macOS took it but the host was never told (its state still says pending): the menu, the Live hint and
    # Settings all lack it — the menu showing nothing must not pass because nothing was expected
    shell.state = _hk_state(status="pending")
    info = shell._hotkey_info()
    assert info["wanted"] is True and info["host_status"] == "pending" and info["menu"]["key"] == ""
    assert info["in_menu"] is False
    # paused: a press resumes, so the shortcut is on Resume listening (and only there)
    shell.state = _hk_state("paused", status="ok")
    pause = Recorder(keyEquivalent="r", keyEquivalentModifierMask=hotkey.appkit_mask(["alt", "cmd"]))
    shell._entries["pause"] = pause
    info = shell._hotkey_info()
    assert info["menu"]["entry"] == "pause" and info["in_menu"] is True
    pause.returns["keyEquivalent"] = ""  # still next to Stop listening instead
    listen.returns["keyEquivalent"] = "r"
    assert shell._hotkey_info()["in_menu"] is False
    listen.returns["keyEquivalent"] = ""
    # turned off in the preferences: the default one is registered for the test, then removed again
    shell.state = _hk_state(code=None)
    shell._sync_hotkey()
    shell.state = _hk_state(code=None, status="off")
    out = shell._check_hotkey()
    assert out["temporary"] is True and out["registered"] == "⌥⌘R" and out["reaches_host"] is True
    assert out["in_menu"] is True  # nothing shown, as nothing is set: right
    assert hotkeys[0].registered is None


AZERTY_KEYS = {"KeyQ": "a", "KeyA": "q", "KeyW": "z", "KeyZ": "w", "Semicolon": "m", "KeyM": ","}


def test_the_shell_tells_the_host_what_the_keys_type(host: FakeHost, appkit: Recorder,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    """Read from macOS on the main thread at launch, and again when Settings starts recording a shortcut
    (the layout may have been switched since); told to the host until its state says it."""
    host.thread.start()
    layouts = [("com.apple.keylayout.French", AZERTY_KEYS)]
    reads: list[str] = []

    def current_layout() -> tuple[str, dict[str, str]]:
        reads.append(threading.current_thread().name)
        return layouts[-1]

    def told() -> list[Any]:
        return [args for name, args in host.calls if name == "set_keyboard_layout"]

    monkeypatch.setattr(native.hotkey, "current_layout", current_layout)
    shell = _shell(host)
    shell.state = _hk_state()
    shell._sync_layout()
    assert reads == []  # not before the app has finished launching
    shell._launched = True
    shell.state = _hk_state(hotkey_cap=False)  # no Carbon: no shortcut to show
    shell._sync_layout()
    assert reads == []

    shell.state = _hk_state()
    shell._sync_layout()
    assert reads == [threading.current_thread().name]  # the caller's thread: the main one, in the app
    assert _wait(lambda: told() == [AZERTY_KEYS])
    shell.state = {**_hk_state(), "keyboard_layout": AZERTY_KEYS}  # the host has it now
    shell._sync_layout()
    shell._sync_layout()
    time.sleep(0.1)
    assert len(reads) == 1 and len(told()) == 1  # not read, nor told, at every state

    layouts.append(("com.apple.keylayout.US", {"KeyQ": "q", "KeyA": "a"}))  # switched to another layout
    shell.state = {**_hk_state(recording=True), "keyboard_layout": AZERTY_KEYS}  # Settings records a shortcut
    shell._sync_layout()
    assert len(reads) == 2 and _wait(lambda: told()[-1] == {"KeyQ": "q", "KeyA": "a"})
    shell.state = {**_hk_state(recording=True), "keyboard_layout": {"KeyQ": "q", "KeyA": "a"}}
    shell._sync_layout()
    assert len(reads) == 2  # still the same recording

    def broken() -> tuple[str, dict[str, str]]:
        raise OSError("the keyboard layout com.apple.keylayout.Old has no 'uchr' data")

    monkeypatch.setattr(native.hotkey, "current_layout", broken)
    shell.state = {**_hk_state(recording=False), "keyboard_layout": {"KeyQ": "q", "KeyA": "a"}}
    shell._sync_layout()
    shell.state = {**_hk_state(recording=True), "keyboard_layout": {"KeyQ": "q", "KeyA": "a"}}
    shell._sync_layout()  # unreadable now: Settings goes by what the page knows (the physical key)
    assert _wait(lambda: told()[-1] == {})


def test_the_selftest_reads_the_keyboard_layout(host: FakeHost, appkit: Recorder,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    us = {code: hotkey.label(code).lower() for code in hotkey.LAYOUT_CODES}
    monkeypatch.setattr(native.hotkey, "current_layout", lambda: ("com.apple.keylayout.US", us))
    shell = _shell(host)
    shell.state = {**_hk_state(), "keyboard_layout": us}
    info = shell._layout_info()
    assert (info["id"], info["keys"]["KeyQ"], info["problem"], info["told_host"]) == ("com.apple.keylayout.US", "q",
                                                                                    None, True)
    shell.state = _hk_state()  # the host was never told
    assert shell._layout_info()["told_host"] is False
    swapped = us | {"KeyQ": "a", "KeyA": "q"}  # the virtual key codes mixed up, say
    monkeypatch.setattr(native.hotkey, "current_layout", lambda: ("com.apple.keylayout.US", swapped))
    assert shell._layout_info()["problem"] == "com.apple.keylayout.US is a US layout, but: KeyA 'q', KeyQ 'a'"


# ---------------------------------------------------------------- macOS hooks


@pytest.fixture
def mac(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")


def _avfoundation(status: int, answer: bool | None = True) -> Any:
    asked: list[Any] = []

    def request(media: Any, handler: Any) -> None:
        asked.append(media)
        if answer is not None:
            threading.Timer(0.05, handler, args=(answer,)).start()

    device = types.SimpleNamespace(authorizationStatusForMediaType_=lambda media: status,
                                   requestAccessForMediaType_completionHandler_=request)
    return types.SimpleNamespace(AVCaptureDevice=device, AVMediaTypeAudio="soun", asked=asked)


def test_microphone_hooks_without_avfoundation(monkeypatch: pytest.MonkeyPatch, mac: None) -> None:
    monkeypatch.setitem(sys.modules, "AVFoundation", None)
    assert native_hooks.microphone_authorization() is None
    assert native_hooks.request_microphone() is None
    with pytest.raises(RuntimeError):  # host.py's hook: an exception means "can't tell"
        native_hooks.microphone_status()
    monkeypatch.setattr(sys, "platform", "linux")
    assert native_hooks.microphone_authorization() is None


@pytest.mark.parametrize(("status", "word"), [(0, "undetermined"), (1, "restricted"), (2, "denied"), (3, "granted")])
def test_microphone_status_words(monkeypatch: pytest.MonkeyPatch, mac: None, status: int, word: str) -> None:
    monkeypatch.setitem(sys.modules, "AVFoundation", _avfoundation(status))
    assert native_hooks.microphone_authorization() == word == native_hooks.microphone_status()


@pytest.mark.parametrize(("answer", "result"), [(True, "granted"), (False, "denied"), (None, "undetermined")])
def test_request_microphone(monkeypatch: pytest.MonkeyPatch, mac: None, answer: bool | None, result: str) -> None:
    av = _avfoundation(0, answer)
    monkeypatch.setitem(sys.modules, "AVFoundation", av)
    assert native_hooks.request_microphone(timeout=0.5) == result
    assert av.asked == ["soun"]


def test_request_microphone_asks_only_once(monkeypatch: pytest.MonkeyPatch, mac: None) -> None:
    av = _avfoundation(2)
    monkeypatch.setitem(sys.modules, "AVFoundation", av)
    assert native_hooks.request_microphone() == "denied" and av.asked == []  # macOS won't ask again anyway


def test_login_item_outside_the_app_bundle() -> None:
    assert native_hooks.bundle_path() is None
    assert native_hooks.login_item_status() == "unsupported"
    assert native_hooks.set_login_item(True) == "unsupported"
    assert native_hooks.refresh_login_item() == "unsupported"
    assert native_hooks.app_location_problem() is None
    with pytest.raises(native_hooks.LoginItemError):
        native_hooks.set_start_at_login(True)


class FakeService:
    def __init__(self) -> None:
        self.status_value = 0
        self.calls: list[str] = []
        self.fail: str | None = None
        self.after_register = 1

    def status(self) -> int:
        return self.status_value

    def registerAndReturnError_(self, err: Any) -> tuple[bool, Any]:  # noqa: N802
        self.calls.append("register")
        if self.fail:
            return False, types.SimpleNamespace(localizedDescription=lambda: self.fail)
        self.status_value = self.after_register
        return True, None

    def unregisterAndReturnError_(self, err: Any) -> tuple[bool, Any]:  # noqa: N802
        self.calls.append("unregister")
        self.status_value = 0
        return True, None


@pytest.fixture
def bundle(monkeypatch: pytest.MonkeyPatch, mac: None) -> Any:
    """A Voicestream.app in ~/Applications, running "frozen", with a fake SMAppService."""
    app = Path.home() / "Applications" / "Voicestream.app"
    exe = app / "Contents" / "MacOS" / "Voicestream"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleVersion": "0.1.0"}))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    svc = FakeService()
    opened: list[str] = []
    sm = types.SimpleNamespace(mainAppService=lambda: svc, openSystemSettingsLoginItems=lambda: opened.append("settings"))
    monkeypatch.setitem(sys.modules, "ServiceManagement", types.SimpleNamespace(SMAppService=sm))
    return types.SimpleNamespace(app=app, exe=exe, svc=svc, opened=opened)


def test_login_item_register_and_unregister(bundle: Any) -> None:
    assert native_hooks.bundle_path() == bundle.app.resolve()
    assert native_hooks.app_location_problem() is None
    assert native_hooks.login_item_status() == "disabled"
    assert native_hooks.set_login_item(True) == "enabled"
    assert native_hooks.set_login_item(True) == "enabled"  # nothing to do the second time
    assert bundle.svc.calls == ["register"]
    state = native_hooks.app_support_dir() / "login-item.json"
    assert json.loads(state.read_text())["build"] == native_hooks.build_identity()
    if os.name == "posix":  # Windows has no permission bits
        assert oct(state.stat().st_mode & 0o777) == "0o600"
    assert native_hooks.set_login_item(False) == "disabled"
    assert bundle.svc.calls == ["register", "unregister"] and not state.exists()
    native_hooks.set_start_at_login(True)  # host.py's hook: no exception when it worked


def test_login_item_is_registered_again_after_an_update(bundle: Any) -> None:
    native_hooks.set_login_item(True)
    assert native_hooks.refresh_login_item() == "enabled" and bundle.svc.calls == ["register"]
    st = bundle.exe.stat()
    os.utime(bundle.exe, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))  # a new build of the app
    assert native_hooks.refresh_login_item() == "enabled"
    assert bundle.svc.calls == ["register", "unregister", "register"]
    assert native_hooks.refresh_login_item() == "enabled" and len(bundle.svc.calls) == 3


def test_login_item_needing_approval_opens_settings(bundle: Any) -> None:
    bundle.svc.after_register = 2
    assert native_hooks.set_login_item(True) == "requires_approval"
    assert bundle.opened == ["settings"]


def test_login_item_refused(bundle: Any) -> None:
    bundle.svc.fail = "Operation not permitted"
    with pytest.raises(native_hooks.LoginItemError, match="Operation not permitted"):
        native_hooks.set_login_item(True)
    with pytest.raises(native_hooks.LoginItemError):
        native_hooks.set_start_at_login(True)


@pytest.mark.skipif(os.name != "posix", reason="a shell script stands in for whisper-server")
def test_the_packaged_selftest_checks_its_own_whisper_server(bundle: Any, monkeypatch: pytest.MonkeyPatch,
                                                            tmp_path: Path) -> None:
    """CI exports VOICESTREAM_WHISPER_SERVER (the build tree's copy) and find_binary reads it first: the
    self-test must find the copy inside the app, and run it (--help: no model needed)."""
    frameworks = bundle.app / "Contents" / "Frameworks"
    monkeypatch.setattr(sys, "_MEIPASS", str(frameworks), raising=False)
    monkeypatch.delenv("VOICESTREAM_WHISPER_SERVER", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))  # nor one on PATH
    assert launcher.whisper_server_check() == {"path": None, "bundled": False, "runs": False, "ok": False,
                                               "error": "no whisper-server found"}
    ws = frameworks / "bin" / "whisper-server"
    ws.parent.mkdir(parents=True)
    ws.write_text("#!/bin/sh\necho 'usage: whisper-server [options]'\n[ \"$1\" = --help ]\n")
    ws.chmod(0o755)
    found = launcher.whisper_server_check()
    assert found["ok"] and found["bundled"] and found["runs"] and found["path"] == str(ws)
    assert found["usage"] == "usage: whisper-server [options]" and found["exit_code"] == 0

    outside = tmp_path / "build" / "whisper-server"
    outside.parent.mkdir()
    outside.write_text(ws.read_text())
    outside.chmod(0o755)
    monkeypatch.setenv("VOICESTREAM_WHISPER_SERVER", str(outside))
    found = launcher.whisper_server_check()
    assert found["runs"] and not found["bundled"] and not found["ok"]  # works, but isn't what users get

    monkeypatch.delenv("VOICESTREAM_WHISPER_SERVER")
    ws.write_text("#!/bin/sh\necho 'dyld: Library not loaded: libggml.dylib' >&2\nexit 134\n")
    found = launcher.whisper_server_check()
    assert found["bundled"] and not found["runs"] and not found["ok"]
    assert found["exit_code"] == 134 and "Library not loaded" in found["usage"]


def test_local_speech_to_text_is_offered_where_it_runs(monkeypatch: pytest.MonkeyPatch, host: FakeHost) -> None:
    monkeypatch.setattr(launcher, "_whisper_server", lambda: "/Applications/Voicestream.app/Contents/Frameworks/bin/whisper-server")
    assert launcher.local_stt_available("darwin", "arm64")
    assert not launcher.local_stt_available("darwin", "x86_64")  # the app's server is arm64; Intel: too slow anyway
    assert launcher.local_stt_available("linux", "x86_64")
    monkeypatch.setattr(launcher, "_whisper_server", lambda: None)
    assert not launcher.local_stt_available("darwin", "arm64")
    launcher._set_capabilities(host)
    assert host.capabilities["local_stt"] is False


def test_app_location_problems(monkeypatch: pytest.MonkeyPatch, mac: None, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    for where, expected in (
        (tmp_path / "Downloads", "Move Voicestream to the Applications folder"),
        (tmp_path / "private/var/folders/x/AppTranslocation/ABC/d", "temporary copy"),
        (Path.home() / "Applications" / "Tools", None),
    ):
        exe = where / "Voicestream.app" / "Contents" / "MacOS" / "Voicestream"
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_text("")
        monkeypatch.setattr(sys, "executable", str(exe))
        problem = native_hooks.app_location_problem()
        assert (problem is None) if expected is None else (expected in (problem or "")), (where, problem)


def test_menu_bar_starts_from_a_fresh_state(host: FakeHost, monkeypatch: pytest.MonkeyPatch) -> None:
    """Listening may start while the window opens: the menu bar reads the state again when it appears,
    and a failure to create the icon never takes the window down."""
    shell = _shell(host)
    shell.state = _state("stopped")
    host._state = _state("listening", minutes=1)
    applied: list[str] = []

    def broken() -> None:
        raise RuntimeError("no status bar")

    monkeypatch.setattr(shell, "_create_status_item", broken)
    monkeypatch.setattr(shell, "apply_state", lambda st: applied.append(st["state"]))
    shell._start_menu_bar()
    assert applied == ["listening"]


def test_half_installed_native_mode_falls_back_to_the_browser(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    hosts: list[FakeHost] = []
    monkeypatch.setattr(launcher, "make_host", lambda config, port: hosts.append(FakeHost()) or hosts[-1])
    monkeypatch.setattr(launcher, "choose_mode", lambda args: "native")

    def missing() -> None:
        raise ImportError("No module named 'webview'")

    monkeypatch.setattr(native, "preflight", missing)
    monkeypatch.setattr(native, "run", lambda *a, **k: pytest.fail("native mode must not start"))
    report = tmp_path / "r.json"
    try:
        # the app still opens (in the browser), but its self-test fails: it was meant to test the window
        assert launcher.main(["--selftest", "0.1", "--selftest-report", str(report)]) == 1
    finally:
        for h in hosts:
            h.close()
    data = json.loads(report.read_text())
    assert data["mode"] == "browser" and data["ok"] is False and data["checks"] == {"app_page": True, "api_app": True}
    assert data["errors"] == ["no native window (No module named 'webview'): fell back to the browser"]


# ------------------------------------------------- native shell: meetings started by hand


def _manual(state: str = "stopped", minutes: float | None = None, **extra: Any) -> dict[str, Any]:
    """A set-up Mac app whose meetings start by hand; ``capabilities``: what the native shell has (none of the
    floating button, call detection and the launcher unless a test says so: each would start real work)."""
    prefs = {"ui_language": "auto", "floating_button": True, "call_suggestions": True, "spotlight_launcher": True,
             **extra.pop("prefs", {})}
    caps = {"floating_button": False, "call_detection": False, "spotlight": False, **extra.pop("capabilities", {})}
    return _state(state, minutes, mode="manual", setup_done=True, prefs=prefs, capabilities=caps, **extra)


class FakeFloatingPanel:
    def __init__(self, on_action: Any, on_moved: Any) -> None:
        self.on_action, self.on_moved = on_action, on_moved
        self.model: floating.PanelModel | None = None
        self.updates: list[tuple[Any, ...]] = []
        self.ticks: list[floating.PanelModel] = []

    def update(self, model: floating.PanelModel | None, *, saved: Any = None, lang: str = "en") -> None:
        self.updates.append((model.kind if model is not None else None, saved, lang))
        self.model = model

    def tick(self, model: floating.PanelModel) -> None:
        self.ticks.append(model)

    def info(self) -> dict[str, Any]:
        return {"created": True, "visible": self.model is not None, "on_active_space": True, "all_spaces": True,
                "full_screen_auxiliary": True, "nonactivating": True, "floating": True, "can_become_key": False,
                "hides_on_deactivate": False}

    def drawn(self) -> dict[str, Any]:
        n = len(self.model.buttons) if self.model is not None else 0
        return {"size": [200.0, 44.0], "buttons": [{"height": 28.0, "width": 40.0, "label": "x"}] * n}


@pytest.fixture
def panels(monkeypatch: pytest.MonkeyPatch) -> list[FakeFloatingPanel]:
    made: list[FakeFloatingPanel] = []

    def make(on_action: Any, on_moved: Any) -> FakeFloatingPanel:
        made.append(FakeFloatingPanel(on_action, on_moved))
        return made[-1]

    monkeypatch.setattr(native.floating, "FloatingPanel", make)
    return made


def test_the_floating_button_follows_the_state(host: FakeHost, appkit: Recorder, panels: list[FakeFloatingPanel]) -> None:
    def st(*args: Any, **kw: Any) -> dict[str, Any]:
        return _manual(*args, capabilities={"floating_button": True}, **kw)

    shell = _shell(host)
    shell.state = st()
    shell._sync_floating()
    assert panels == []  # not before the app has finished launching
    shell._launched = True
    shell.state = {**st(), "setup_done": False}
    shell._sync_floating()
    assert panels == []  # the setup first: nothing to hide either
    shell.state = st(prefs={"floating_position": {"x": 10.0, "y": 20.0, "screen": None}})
    shell._sync_floating()
    (panel,) = panels
    assert panel.updates[-1] == ("idle", {"x": 10.0, "y": 20.0, "screen": None}, "en")
    shell.state = st("listening", minutes=3)
    shell.apply_state(shell.state)  # a state from the host: every part follows
    assert panel.updates[-1][0] == "recording"
    shell.render()  # each second: the timer
    assert panel.ticks and panel.ticks[-1].timer
    shell.state = st(prefs={"floating_button": False})  # hidden from its menu, the status menu or Settings
    shell._sync_floating()
    assert panel.updates[-1][0] is None
    shell._show_sample("suggestion")  # the self-test shows it whatever the state says
    assert panel.updates[-1][0] == "suggestion"
    ticks = len(panel.ticks)
    shell.render()
    assert len(panel.ticks) == ticks  # nor ticked by the state then
    shell._show_sample(None)
    assert panel.updates[-1][0] is None
    shell.state = st()
    shell.quitting = True
    shell._sync_floating()
    assert panel.updates[-1][0] is None and len(panels) == 1
    # the shortcut, once macOS took it, is named in its tooltips
    shell.quitting = False
    shell.state = st(prefs={"hotkey": {"code": "KeyR", "mods": ["alt", "cmd"], "key": "R"}}, hotkey={"status": "ok"})
    assert shell._panel_model().buttons[0].tooltip.endswith("(⌥⌘R)")
    shell.state = st(prefs={"hotkey": {"code": "KeyR", "mods": ["alt", "cmd"], "key": "R"}}, hotkey={"status": "taken"})
    assert "⌥⌘R" not in shell._panel_model().buttons[0].tooltip


def test_floating_button_clicks_go_through_the_host_actions(host: FakeHost, appkit: Recorder) -> None:
    host.thread.start()
    shell = _shell(host)
    ended: list[str] = []

    async def end_meeting() -> str:
        ended.append("end")
        return "stop"

    host.end_meeting = end_meeting  # type: ignore[attr-defined]
    for key in ("start", "pause", "resume", "stop", "accept", "dismiss", "keep", "ok", "retry", "hide", "end"):
        shell.on_floating(key)
    shell._floating_moved({"x": 1.0, "y": 2.0, "screen": "Built-in Retina Display"})
    assert _wait(lambda: len(host.calls) == 11 and ended == ["end"])
    assert host.calls == [("meeting_start", None), ("listen_pause", None), ("listen_resume", None), ("meeting_stop", None),
                          ("suggestion_accept", None), ("suggestion_dismiss", None), ("meeting_keep", None),
                          ("dismiss_notice", None), ("meeting_start", None), ("update_prefs", {"floating_button": False}),
                          ("update_prefs", {"floating_position": {"x": 1.0, "y": 2.0, "screen": "Built-in Retina Display"}})]
    host.calls.clear()
    appkit.calls.clear()
    shell.on_floating("open")  # as opening Voicestream again: the window, the Start control focused
    assert ("setActivationPolicy_", (native.POLICY_REGULAR,)) in appkit.calls
    assert _wait(lambda: host.calls == [("request_focus", "start")])
    ran: list[str] = []
    shell.window = types.SimpleNamespace(native=Recorder(isMiniaturized=False), run_js=lambda js: ran.append(js))
    shell.on_floating("settings")
    assert _wait(lambda: ran == ['location.hash = "#/settings/general"'])  # from another thread: run_js waits on this one
    shell.on_floating("nothing such")  # ignored


class Descriptor:
    def __init__(self, url: str | None) -> None:
        self.url = url

    def paramDescriptorForKeyword_(self, key: int) -> Any:
        assert key == int.from_bytes(b"----", "big")
        return None if self.url is None else types.SimpleNamespace(stringValue=lambda: self.url)


@pytest.fixture
def sounds(monkeypatch: pytest.MonkeyPatch, appkit: Recorder) -> list[str]:
    played: list[str] = []
    sound = types.SimpleNamespace(soundNamed_=lambda name: types.SimpleNamespace(play=lambda: played.append(name)))
    monkeypatch.setitem(sys.modules, "AppKit", types.SimpleNamespace(NSApplication=sys.modules["AppKit"].NSApplication,
                                                                    NSSound=sound))
    return played


def test_links_reach_the_host_and_say_what_they_did(host: FakeHost, appkit: Recorder, sounds: list[str]) -> None:
    host.thread.start()
    shell = _shell(host)
    shell.state = _manual()
    link = f"voicestream://meeting/start?key={host.url_key}"
    shell.on_url_event(Descriptor(link))  # the Apple event handler's part
    assert _wait(lambda: sounds == ["Tink"])  # like the shortcut: you are in another app
    assert host.calls == [("open_url", link)] and shell._urls == 1
    host.url_outcome = "refused"
    shell.open_url("voicestream://meeting/start")
    assert _wait(lambda: sounds == ["Tink", "Basso"])  # the floating button and the window say why
    host.url_outcome = "stop"
    shell.open_url("voicestream://meeting/stop")
    assert _wait(lambda: sounds[-1] == "Pop")
    host.url_outcome = "open"
    shell.open_url("voicestream://open")
    assert _wait(lambda: ("request_focus", "start") in host.calls)
    host.url_outcome = "nothing"
    shell.open_url("voicestream://meeting/toggle?key=x")
    shell.on_url_event(Descriptor(None))  # an event without a link: given to the host as nothing
    assert _wait(lambda: host.calls[-1] == ("open_url", ""))
    time.sleep(0.1)
    assert len(sounds) == 3


def test_the_selftest_link_is_only_asked_about(host: FakeHost, appkit: Recorder) -> None:
    host.thread.start()
    shell = _shell(host)
    answers: list[Any] = []
    shell._url_probe = answers
    for link in (f"voicestream://meeting/toggle?key={host.url_key}", "voicestream://meeting/toggle",
                 "voicestream://meeting/toggle?key=" + "x" * 43, "voicestream://somewhere/else"):
        shell.open_url(link)
    results = [a.result(5) for a in answers]
    assert results == [{"action": "toggle", "key_ok": True, "would": "start"},
                       {"action": "toggle", "key_ok": False, "would": None},
                       {"action": "toggle", "key_ok": False, "would": None},
                       {"action": None, "key_ok": False, "would": None}]
    assert not [c for c in host.calls if c[0] in ("open_url", "meeting_toggle", "meeting_start")]  # nothing done


def test_the_link_handler_is_registered_before_the_run_loop(host: FakeHost, pyobjc: Any,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    registered: list[tuple[Any, ...]] = []
    manager = types.SimpleNamespace(setEventHandler_andSelector_forEventClass_andEventID_=lambda *a: registered.append(a))
    foundation = sys.modules["Foundation"]
    monkeypatch.setattr(foundation, "NSAppleEventManager", types.SimpleNamespace(sharedAppleEventManager=lambda: manager),
                        raising=False)
    host.thread.start()
    shell = native.MacShell(host, f"http://127.0.0.1:1/app?token={TOKEN}")
    assert shell.run() == 0
    ((target, selector, event_class, event_id),) = registered
    assert (selector, event_class, event_id, target.shell) == ("handleURLEvent:withReplyEvent:", 0x4755524C, 0x4755524C, shell)
    assert shell._url_handler == "registered"
    assert native._menu_target_class().handleURLEvent_withReplyEvent_.signature == b"v@:@@"
    seen: list[str] = []
    monkeypatch.setattr(shell, "open_url", seen.append)
    target.handleURLEvent_withReplyEvent_(Descriptor("voicestream://open"), None)
    assert seen == ["voicestream://open"]

    def broken(*a: Any) -> None:
        raise RuntimeError("no Apple events here")

    manager.setEventHandler_andSelector_forEventClass_andEventID_ = broken
    other = native.MacShell(host, f"http://127.0.0.1:1/app?token={TOKEN}")
    other._register_url_handler()
    assert other._url_handler == "RuntimeError: no Apple events here"  # everything else works; the self-test says so


def test_the_selftest_sends_links_as_apple_events(host: FakeHost, appkit: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    """Through a stand-in Foundation whose Apple-event send calls the registered handler, as macOS does for an
    event a process sends itself: the keyed toggle reaches the host, the keyless one is refused, nothing starts,
    and the report holds neither link nor key."""
    host.thread.start()
    shell = _shell(host)
    shell._url_handler = "registered"
    shell.state = _manual()
    sent: list[Any] = []

    class Event:
        def __init__(self) -> None:
            self.params: dict[int, Any] = {}

        def setParamDescriptor_forKeyword_(self, desc: Any, key: int) -> None:
            self.params[key] = desc

        def paramDescriptorForKeyword_(self, key: int) -> Any:
            return self.params.get(key)

        def sendEventWithOptions_timeout_error_(self, options: int, timeout: float, error: Any) -> tuple[Any, Any]:
            sent.append((options, timeout))
            shell.on_url_event(self)  # the handler, called by the Apple Event Manager
            return None, None

    descriptor = types.SimpleNamespace(
        appleEventWithEventClass_eventID_targetDescriptor_returnID_transactionID_=lambda c, i, t, r, x: (
            Event() if (c, i, t, r, x) == (native.URL_EVENT, native.URL_EVENT, "me", -1, 0) else None),
        currentProcessDescriptor=lambda: "me",
        descriptorWithString_=lambda s: types.SimpleNamespace(stringValue=lambda: s))
    info = {"CFBundleURLTypes": [{"CFBundleURLName": "x", "CFBundleURLSchemes": ["voicestream"]}]}
    foundation = types.SimpleNamespace(NSAppleEventDescriptor=descriptor,
                                       NSBundle=types.SimpleNamespace(mainBundle=lambda: types.SimpleNamespace(
                                           infoDictionary=lambda: info)),
                                       NSURL=types.SimpleNamespace(URLWithString_=lambda s: s))
    monkeypatch.setitem(sys.modules, "Foundation", foundation)
    monkeypatch.setitem(sys.modules, "AppKit", types.SimpleNamespace(
        NSApplication=sys.modules["AppKit"].NSApplication,
        NSWorkspace=types.SimpleNamespace(sharedWorkspace=lambda: types.SimpleNamespace(URLForApplicationToOpenURL_=lambda u: None))))
    out = shell._check_url()
    assert out["registered"] and out["scheme_in_plist"] and out["default_handler"] is None
    assert sent == [(native.SEND_NO_REPLY, native.URL_ANSWER_S)] * 2 and out["handled"] == 2
    assert out["reaches_host"] is True and out["state_before"] == out["state_after"] == "stopped"
    assert out["answers"] == [{"action": "toggle", "key_ok": True, "would": "start"},
                              {"action": "toggle", "key_ok": False, "would": None}]
    assert host.url_key not in json.dumps(out) and "voicestream://" not in json.dumps(out)
    assert not [c for c in host.calls if c[0] in ("open_url", "meeting_toggle")]
    info["CFBundleURLTypes"] = []
    assert shell._check_url()["scheme_in_plist"] is False


class FakeWatcher:
    def __init__(self, on_start: Any, on_end: Any) -> None:
        self.on_start, self.on_end = on_start, on_end
        self.running = False
        self.starts: list[bool] = []
        self.stops = 0

    def start(self, *, baseline: bool) -> None:
        self.running = True
        self.starts.append(baseline)

    def stop(self) -> None:
        self.running = False
        self.stops += 1


def test_calls_are_watched_only_while_nothing_is_recorded(host: FakeHost, appkit: Recorder,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    def st(*args: Any, **kw: Any) -> dict[str, Any]:
        return _manual(*args, capabilities={"call_detection": True}, **kw)

    host.thread.start()
    made: list[FakeWatcher] = []
    monkeypatch.setattr(native.calls, "CallWatcher", lambda on_start, on_end: made.append(FakeWatcher(on_start, on_end))
                        or made[-1])
    shell = _shell(host)
    shell._launched = True
    for state in ({**st(), "setup_done": False}, _manual(), st(prefs={"call_suggestions": False})):
        shell.state = state
        shell._sync_calls()
    assert made == []
    shell.state = st()
    shell._sync_calls()
    (w,) = made
    assert w.starts == [False] and _wait(lambda: ("call_ended", (None, None)) in host.calls)  # the host forgets old calls
    shell._sync_calls()
    assert w.starts == [False]  # once
    for current in ("starting", "listening", "paused", "stopping"):
        shell.state = st(current, minutes=1)
        shell._sync_calls()
        assert not w.running
    shell.state = st()
    shell._sync_calls()
    assert w.starts == [False, True]  # after a recording: the call recorded isn't new
    shell.state = st(prefs={"call_suggestions": False})
    shell._sync_calls()
    assert not w.running
    shell.state = st()
    shell._sync_calls()
    assert w.starts == [False, True, False]
    shell.quitting = True
    shell._sync_calls()
    assert not w.running and len(made) == 1

    # what the watcher sees, from its thread: named on the main thread, then given to the host
    shell.quitting = False
    shell._sync_calls()
    assert _wait(lambda: host.calls.count(("call_ended", (None, None))) == 4)
    host.calls.clear()
    w.on_start(calls.Call("zoom", "Zoom", "us.zoom.xos", 501))
    assert _wait(lambda: host.calls == [("suggest_meeting", ("Zoom", "zoom"))])
    w.on_end(calls.Call("zoom", "Zoom", "us.zoom.xos", 501))
    assert _wait(lambda: host.calls[-1] == ("call_ended", ("Zoom", "zoom")))
    w.running = False  # recording started meanwhile: nothing suggested
    w.on_start(calls.Call("zoom", "Zoom", "us.zoom.xos", 501))
    time.sleep(0.1)
    assert len(host.calls) == 2


def test_calls_are_named_with_what_macos_says(monkeypatch: pytest.MonkeyPatch) -> None:
    def running_app(pid: int) -> Any:
        return types.SimpleNamespace(localizedName=lambda: "Loom") if pid == 77 else None

    front = types.SimpleNamespace(bundleIdentifier=lambda: "com.google.Chrome")
    ws = types.SimpleNamespace(frontmostApplication=lambda: front,
                               runningApplications=lambda: [types.SimpleNamespace(bundleIdentifier=lambda: "us.zoom.xos")])
    monkeypatch.setitem(sys.modules, "AppKit", types.SimpleNamespace(
        NSRunningApplication=types.SimpleNamespace(runningApplicationWithProcessIdentifier_=running_app),
        NSWorkspace=types.SimpleNamespace(sharedWorkspace=lambda: ws)))
    assert native.call_name(calls.Call("zoom", "Zoom", "us.zoom.xos", 501)) == "Zoom"
    assert native.call_name(calls.Call("com.loom.desktop", None, "com.loom.desktop", 77)) == "Loom"
    assert native.call_name(calls.Call("pid 78", None, None, 78)) is None  # a process macOS has no app for
    assert native.call_name(calls.Call("mic")) == "Google Chrome"  # no process information: the frontmost call app
    monkeypatch.setitem(sys.modules, "AppKit", None)
    assert native.call_name(calls.Call("mic")) is None  # said, not raised


def test_the_spotlight_launcher_follows_the_setting(host: FakeHost, appkit: Recorder, monkeypatch: pytest.MonkeyPatch,
                                                    tmp_path: Path) -> None:
    def st(*args: Any, **kw: Any) -> dict[str, Any]:
        return _manual(*args, capabilities={"spotlight": True}, **kw)

    host.thread.start()
    folder = tmp_path / "Applications"
    monkeypatch.setattr(native.spotlight, "applications_dir", lambda: folder)
    app = tmp_path / "Apps" / "Voicestream.app"
    (app / "Contents" / "Resources").mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIconFile": "Voicestream.icns"}))
    (app / "Contents" / "Resources" / "Voicestream.icns").write_bytes(b"icon")
    monkeypatch.setattr(native.native_hooks, "bundle_path", lambda: app)
    synced: list[Any] = []
    real_sync = spotlight.sync
    monkeypatch.setattr(native.spotlight, "sync", lambda f, launcher: synced.append(launcher) or real_sync(f, launcher))
    shell = _shell(host)
    shell._launched = True
    for state in ({**st(), "setup_done": False}, _manual()):
        shell.state = state
        shell._sync_spotlight()
    shell.quitting = True
    shell.state = st()
    shell._sync_spotlight()
    shell.quitting = False
    time.sleep(0.1)
    assert synced == []  # the setup asks first; a terminal install has no app to open the link with
    shell._sync_spotlight()
    bundle = folder / "Voicestream – New Meeting.app"
    assert _wait(lambda: (bundle / "Contents" / "MacOS" / "new-meeting").is_file())
    script = (bundle / "Contents" / "MacOS" / "new-meeting").read_text()
    assert f"key={host.url_key}" in script and app.as_posix() in script and "/usr/bin/open -g" in script
    assert (bundle / "Contents" / "Resources" / "AppIcon.icns").read_bytes() == b"icon"
    shell._sync_spotlight()
    shell.apply_state(st())  # the same again: not written again
    time.sleep(0.1)
    assert len(synced) == 1
    shell.state = st(prefs={"ui_language": "fr"})
    shell.lang = "fr"
    shell._sync_spotlight()
    assert _wait(lambda: [p.name for p in folder.iterdir()] == ["Voicestream – Nouvelle réunion.app"])
    shell.state = st(prefs={"ui_language": "fr", "spotlight_launcher": False})
    shell._sync_spotlight()
    assert _wait(lambda: list(folder.iterdir()) == [])  # turned off: removed

    def unwritable(f: Path, launcher: Any) -> str:
        raise PermissionError("~/Applications is read-only")

    monkeypatch.setattr(native.spotlight, "sync", unwritable)
    assert host.call(shell._write_launcher(True, "en", app)).result(5) == "failed"  # logged; the app goes on


def test_the_selftest_writes_a_launcher_somewhere_else_and_probes_the_microphone(
        host: FakeHost, appkit: Recorder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    host.thread.start()
    monkeypatch.setattr(native.spotlight, "applications_dir", lambda: pytest.fail("never ~/Applications"))
    monkeypatch.setattr(native.native_hooks, "bundle_path", lambda: None)
    made: list[Path] = []
    real = native.spotlight.sync
    monkeypatch.setattr(native.spotlight, "sync", lambda f, launcher: made.append(f) or real(f, launcher))
    shell = _shell(host)
    out = shell._check_spotlight(frozen=False)
    assert out["ok"] is True and out["sync"] == "written" and out["removed"] == "removed" and out["check"]["ok"]
    assert host.url_key not in json.dumps(out)
    assert made and not made[0].exists()  # the temporary folder is gone too
    assert shell._check_spotlight(frozen=True)["ok"] is False  # the packaged app's launcher carries its icon

    monkeypatch.setattr(native.calls, "probe", lambda: calls.MicUse(False, ()))
    mic = shell._check_mic_probe()
    assert mic == {"in_use": False, "device_running": False, "processes_known": True, "others": [], "ok": True}
    monkeypatch.setattr(native.calls, "probe", lambda: calls.MicUse(None, None))
    assert shell._check_mic_probe()["ok"] is False  # macOS 14+ names the processes: it must here

    def no_core_audio() -> Any:
        raise OSError("call detection reads Core Audio, on a Mac")

    monkeypatch.setattr(native.calls, "probe", no_core_audio)
    assert shell._check_mic_probe() == {"ok": False, "error": "call detection reads Core Audio, on a Mac"}


def test_the_selftest_checks_the_floating_button(host: FakeHost, appkit: Recorder, panels: list[FakeFloatingPanel],
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    shell = _shell(host)
    shell._launched = True
    shell.state = {**_manual(), "setup_done": False}  # CI: the state would hide it
    monkeypatch.setattr(native, "time", types.SimpleNamespace(monotonic=time.monotonic, sleep=lambda s: None))
    out = shell._check_floating()
    assert out["wanted"] is False and out["ok"] is True and out["states_ok"] is True
    assert set(out["states"]) == set(floating.sample_states())
    assert out["states"]["suggestion"] == {"drawn": True, "kind": "suggestion", "size": [200, 44], "buttons": 2,
                                           "expected_buttons": 2, "min_button_height": 28.0, "labelled": True}
    assert panels[0].updates[-1][0] is None and shell._floating_test is None  # hidden again after the test

    def short(self: Any) -> dict[str, Any]:
        return {"size": [200.0, 44.0], "buttons": [{"height": 22.0, "width": 40.0, "label": ""}]}

    monkeypatch.setattr(FakeFloatingPanel, "drawn", short)
    assert shell._check_floating()["states_ok"] is False  # a 22-point button, unlabelled
    monkeypatch.setattr(FakeFloatingPanel, "info", lambda self: {"created": True, "visible": True, "can_become_key": True})
    assert shell._check_floating()["ok"] is False


def test_the_native_capabilities(host: FakeHost, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native.calls, "available", lambda: True)
    monkeypatch.setattr(native.native_hooks, "bundle_path", lambda: None)  # a terminal install
    monkeypatch.setattr(native.MacShell, "run", lambda self: 0)
    assert native.run(host) == 0
    caps = host.capabilities
    assert caps["native_window"] and caps["floating_button"] and caps["call_detection"]
    assert caps["spotlight"] is False  # the link needs the packaged app's Info.plist
