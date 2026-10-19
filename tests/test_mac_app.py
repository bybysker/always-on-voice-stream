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
from voicestream.app import launcher, native, native_hooks

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
    meeting = None
    if minutes is not None:
        meeting = {"id": "m1", "title": "Sync", "started_at": (NOW - timedelta(minutes=minutes)).isoformat()}
    return {"state": state, "in_meeting": meeting is not None, "meeting": meeting, "error": None,
            "prefs": {"ui_language": "auto"}, **extra}


def test_native_modules_import_without_pyobjc() -> None:
    """In a process where none of the macOS frameworks can be imported, both modules import and
    the hooks degrade to "can't tell" instead of raising."""
    code = (
        "import sys\n"
        "for m in ('objc', 'AppKit', 'Foundation', 'WebKit', 'AVFoundation', 'ServiceManagement', 'PyObjCTools', 'webview'):\n"
        "    sys.modules[m] = None\n"
        "sys.platform = 'darwin'\n"
        "from voicestream.app import native, native_hooks\n"
        "assert native.status_symbol({'state': 'listening'}) == 'mic.fill'\n"
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


def _menu(state: dict[str, Any], login: str = "disabled", **kw: Any) -> dict[str, native.MenuEntry]:
    return {e.key: e for e in native.menu_model(state, "en", login, now=NOW, **kw)}


def test_menu_model() -> None:
    keys = [e.key for e in native.menu_model(_state("stopped"), "en", "disabled", now=NOW)]
    for st in (_state("listening", 4), _state("paused", 4), _state("error"), _state("starting")):
        assert [e.key for e in native.menu_model(st, "fr", "enabled", now=NOW)] == keys  # built once, updated in place

    stopped = _menu(_state("stopped"))
    assert stopped["listen"].title == "Start listening" and not stopped["pause"].visible
    assert not stopped["end_meeting"].visible and not stopped["status"].enabled
    assert stopped["quit"].shortcut == "q" and not stopped["login"].checked

    meeting = _menu(_state("listening", minutes=4))
    assert meeting["pause"].visible and meeting["pause"].title == "Pause"
    assert meeting["listen"].title == "Stop listening" and meeting["end_meeting"].visible

    paused = _menu(_state("paused", minutes=4))
    assert paused["pause"].title == "Resume listening"

    starting = _menu(_state("starting"))
    assert not starting["listen"].enabled

    assert not _menu(_state("stopped"), "unsupported")["login"].visible
    approval = _menu(_state("stopped"), "requires_approval")["login"]
    assert approval.checked and "System Settings" in approval.title

    quitting = _menu(_state("listening", minutes=4), quitting=True)
    assert not any(e.enabled for e in quitting.values() if not e.separator)


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
    assert shell._on_closing() is None  # when quitting, the window may really close


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
    assert host.calls == [("listen_pause", None), ("listen_resume", None), ("listen_start", None),
                          ("listen_start", None), ("listen_stop", False)]
    shell.on_menu("quit")
    assert appkit.names() == ["terminate_"]


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
        assert launcher.main(["--selftest", "0.1", "--selftest-report", str(report)]) == 0
    finally:
        for h in hosts:
            h.close()
    assert json.loads(report.read_text())["mode"] == "browser"
