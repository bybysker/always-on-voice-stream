"""``voicestream app`` and the packaged Voicestream.app: start the app host, then show it.

The host (:class:`~voicestream.app.host.AppHost`) runs the dashboard server and the assistant on
an asyncio loop in a background thread. This module only decides how you see it:

* **native** (macOS with pywebview + PyObjC): a window and a menu-bar icon (:mod:`.native`);
* **browser** (``--browser``, and wherever pywebview isn't available): the default browser;
* **headless** (``--no-open``): serve only and print the URL (also how the UI is tested on Linux).

In browser and headless mode the main thread just waits for Ctrl+C or SIGTERM, then stops the
host gracefully (a meeting in progress is wrapped up and saved) with a bounded wait; a second
Ctrl+C, or the time running out, forces it so quitting never hangs.

``--selftest SECONDS`` is the packaged app's smoke test, run by the macOS CI job: start
everything, check that the UI loads and the API answers, write a JSON report, quit after
SECONDS. It never starts listening (CI has no meeting to hear and can't answer permission
prompts). The exit status is 0 only when every check passed.

Nothing here imports more than the standard library at module level: ``voicestream --help``
builds this parser too.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import signal
import sys
import tempfile
import threading
import time
import urllib.parse
import webbrowser
from collections.abc import Callable
from concurrent.futures import Future
from datetime import UTC, datetime
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

GRACEFUL_STOP_S = 25.0
"""How long quitting waits for the graceful stop (the host itself wraps a meeting up for at most
20 s, then forces) before forcing it from here too."""
FORCE_STOP_S = 10.0
HTTP_TIMEOUT_S = 10.0


class Host(Protocol):
    """What the shells need from :class:`~voicestream.app.host.AppHost` (tests use a fake)."""

    def run_in_thread(self) -> str: ...
    def call(self, coro: Any) -> Future[Any]: ...
    def state(self) -> dict[str, Any]: ...
    def add_state_listener(self, fn: Callable[[dict[str, Any]], None]) -> None: ...
    async def shutdown(self, force: bool = False) -> None: ...
    async def listen_start(self) -> None: ...
    async def listen_pause(self) -> None: ...
    async def listen_resume(self) -> None: ...
    async def listen_stop(self, force: bool = False) -> None: ...


# ------------------------------------------------------------------- arguments


def add_arguments(p: argparse.ArgumentParser, *, standalone: bool = True) -> None:
    """The app's options. ``standalone=False`` when ``voicestream app`` adds them under the main
    CLI, which already has ``--config`` and ``-v``."""
    if standalone:
        p.add_argument("-c", "--config", help="path to voicestream.toml (default: ~/.voicestream/voicestream.toml)")
        p.add_argument("-v", "--verbose", action="count", default=0)
    show = p.add_mutually_exclusive_group()
    show.add_argument("--browser", action="store_true", help="open the app in your default browser instead of a window")
    show.add_argument("--no-open", action="store_true", help="don't open anything: serve the app and print its address")
    p.add_argument("--port", type=int, help="port for the app's local server (default: any free port)")
    p.add_argument("--selftest", type=float, metavar="SECONDS", help=argparse.SUPPRESS)
    p.add_argument("--selftest-report", metavar="PATH", help=argparse.SUPPRESS)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="voicestream app",
        description="Open Voicestream: a window and a menu-bar icon on a Mac, your browser elsewhere.",
    )
    add_arguments(p)
    return p


def _clean_argv(argv: list[str]) -> list[str]:
    """Drop what macOS may add when it launches an app (``-psn_0_12345`` process serial numbers)."""
    return [a for a in argv if not a.startswith("-psn_")]


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    argv = _clean_argv(sys.argv[1:] if argv is None else list(argv))
    if getattr(sys, "frozen", False):
        # Finder, Xcode or a login launch can add flags of their own (-NSDocumentRevisionsDebugMode …):
        # the app must open anyway rather than exit with a usage error nobody sees
        args, unknown = parser.parse_known_args(argv)
        if unknown:
            log.info("ignoring unknown arguments: %s", " ".join(unknown))
    else:
        args = parser.parse_args(argv)
    return run(args)


def run(args: argparse.Namespace) -> int:
    """Start the host and show it as ``args`` asks (also called by ``voicestream app``)."""
    _ensure_std_streams()
    _setup_logging(getattr(args, "verbose", 0))
    mode = choose_mode(args)
    try:
        host = make_host(getattr(args, "config", None), args.port)
    except ImportError as exc:
        print(f"error: the app needs more packages ({exc}): pip install 'voicestream[dashboard,app]'", file=sys.stderr)
        return 2
    _set_capabilities(host)
    report = Path(args.selftest_report).expanduser() if args.selftest_report else None
    if args.selftest is not None and report is None:
        report = Path(tempfile.gettempdir()) / "voicestream-selftest.json"
    if mode == "native":
        try:
            from . import native

            native.preflight()
        except ImportError as exc:  # pywebview or PyObjC half-installed: still usable in the browser
            log.warning("no native window (%s): opening the app in the browser", exc)
            mode = "browser"
        else:
            return native.run(host, selftest=args.selftest, report_path=report)
    return serve(host, mode=mode, selftest=args.selftest, report_path=report)


def choose_mode(args: argparse.Namespace, platform_name: str | None = None) -> str:
    """``"native"``, ``"browser"`` or ``"headless"``."""
    if getattr(args, "no_open", False):
        return "headless"
    if getattr(args, "browser", False):
        return "browser"
    if (platform_name or sys.platform) == "darwin" and native_available():
        return "native"
    return "browser"


def native_available() -> bool:
    """pywebview and PyObjC are installed. Only looks: importing pywebview's Cocoa backend
    already changes the process (it creates the NSApplication), so that waits for native mode."""
    try:
        return all(find_spec(name) is not None for name in ("webview", "objc", "AppKit", "WebKit"))
    except (ImportError, ValueError):
        return False


def make_host(config: str | os.PathLike[str] | None, port: int | None) -> Host:
    import inspect

    from .host import AppHost

    path = Path(config).expanduser() if config else None
    if port is not None and "port" in inspect.signature(AppHost).parameters:
        return AppHost(config_path=path, port=port)  # type: ignore[call-arg]
    host = AppHost(config_path=path)
    if port is not None:
        if hasattr(host, "port"):
            host.port = port  # type: ignore[attr-defined]
        else:
            log.warning("--port is not supported by this version of the app host: using any free port")
    return host


def _set_capabilities(host: Any) -> None:
    """What the UI may offer: launch at login only exists for the installed app on macOS."""
    caps = getattr(host, "capabilities", None)
    if not isinstance(caps, dict):
        return
    from . import native_hooks

    caps["login_item"] = native_hooks.login_item_status() != "unsupported"


def _ensure_std_streams() -> None:
    """A windowed app may have no stdout/stderr: printing or logging must not crash it."""
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))  # noqa: SIM115 - lives as long as the process


def _setup_logging(verbose: int) -> None:
    """Terminal logging when nobody configured any (the CLI does it before calling :func:`run`);
    the host adds the log file under ``<data_dir>/logs`` itself."""
    root = logging.getLogger()
    if root.handlers:
        return
    level = logging.WARNING if not verbose else logging.INFO if verbose == 1 else logging.DEBUG
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")


# ----------------------------------------------------------- browser / headless


class StopRequest:
    """Ctrl+C / SIGTERM on the main thread: the first asks for a graceful stop, the second forces it."""

    def __init__(self) -> None:
        self.requested = threading.Event()
        self.force = threading.Event()
        self._previous: dict[int, Any] = {}

    def __call__(self, signum: int, frame: Any = None) -> None:
        if self.requested.is_set():
            self.force.set()
        else:
            self.requested.set()

    def install(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                self._previous[sig] = signal.signal(sig, self)
            except (ValueError, OSError):  # pragma: no cover - e.g. an embedded interpreter
                pass

    def restore(self) -> None:
        for sig, handler in self._previous.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):  # pragma: no cover
                pass
        self._previous.clear()


def serve(
    host: Host,
    *,
    mode: str = "browser",
    selftest: float | None = None,
    report_path: Path | None = None,
    stop: StopRequest | None = None,
    open_url: Callable[[str], Any] = webbrowser.open,
) -> int:
    """Browser and headless mode: keep the host alive until asked to stop, then stop it."""
    stop = stop or StopRequest()
    started = time.monotonic()
    try:
        url = host.run_in_thread()
    except Exception as exc:  # noqa: BLE001 - whatever stops the host from starting is the message
        log.debug("the app failed to start", exc_info=True)
        print(f"error: Voicestream could not start: {exc}", file=sys.stderr)
        if selftest is not None:
            write_report(report_path, {"ok": False, "mode": mode, "errors": [f"start: {exc}"]})
        return 2
    print(f"Voicestream is running at {url}", flush=True)
    if mode == "browser" and selftest is None:
        open_url(url)
    if mode != "headless" or selftest is None:
        print("Press Ctrl+C to quit.", flush=True)
    stop.install()
    report: dict[str, Any] | None = None
    try:
        if selftest is not None:
            report = selftest_report(host, url, mode=mode)
            report["http"] = check_http(url)
            report["checks"].update(http_checks(report["http"]))
            finish_report(report, started)
            write_report(report_path, report)
            write_phase(report_path, "ready")
        while not stop.requested.wait(0.25):
            if selftest is not None and time.monotonic() - started >= selftest:
                break
    finally:
        stop.restore()
        print("Stopping Voicestream…", flush=True)
        t0 = time.monotonic()
        graceful = shutdown_host(host, force_event=stop.force)
        if report is not None:
            report["quit"] = {"graceful": graceful, "shutdown_s": round(time.monotonic() - t0, 2)}
            write_report(report_path, report)
            write_phase(report_path, "done")
    if report is not None:
        return 0 if report["ok"] else 1
    return 0


def shutdown_host(
    host: Host,
    *,
    grace: float = GRACEFUL_STOP_S,
    force_after: float = FORCE_STOP_S,
    force_event: threading.Event | None = None,
) -> bool:
    """Stop the host from any thread but its own: gracefully for up to ``grace`` seconds (or until
    ``force_event`` is set), then by force. True when the graceful stop finished in time."""
    try:
        fut = host.call(host.shutdown())
    except Exception:  # noqa: BLE001 - its loop is already gone: nothing left to stop
        log.debug("host already stopped", exc_info=True)
        return True
    deadline = time.monotonic() + grace
    while True:
        try:
            fut.result(timeout=0.2)
            return True
        except TimeoutError:
            if time.monotonic() >= deadline or (force_event is not None and force_event.is_set()):
                break
        except Exception:  # noqa: BLE001
            log.exception("stopping the app failed")
            break
    log.warning("Voicestream is still stopping after %.0f s: forcing it", time.monotonic() - deadline + grace)
    try:
        host.call(host.shutdown(force=True)).result(timeout=force_after)
    except Exception:  # noqa: BLE001
        log.warning("the forced stop did not finish either", exc_info=True)
    return False


# --------------------------------------------------------------------- selftest


def selftest_report(host: Host, url: str, *, mode: str) -> dict[str, Any]:
    """The common part of a self-test report. Never contains the URL's token."""
    from .. import __version__

    report: dict[str, Any] = {
        "ok": False,
        "mode": mode,
        "version": __version__,
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": sys.version.split()[0],
        "frozen": bool(getattr(sys, "frozen", False)),
        "executable": sys.executable,
        "url": redact(url),
        "host_state": safe_state(host),
        "whisper_server": _whisper_server(),
        "checks": {},
        "errors": [],
    }
    return report


def finish_report(report: dict[str, Any], started: float) -> dict[str, Any]:
    report["ok"] = bool(report["checks"]) and all(report["checks"].values()) and not report["errors"]
    report["duration_s"] = round(time.monotonic() - started, 2)
    return report


def safe_state(host: Host) -> dict[str, Any]:
    try:
        return json.loads(json.dumps(host.state(), default=str))
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def _whisper_server() -> str | None:
    try:
        from ..config import WhisperCppConfig
        from ..transcribe.whisper_cpp import find_binary
    except ImportError:
        return None
    found = find_binary(WhisperCppConfig())
    return str(found) if found else None


def redact(url: str) -> str:
    """The URL without its token (reports and logs are shared; the token opens the app)."""
    parts = urllib.parse.urlsplit(url)
    query = [(k, "redacted" if k == "token" else v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def check_http(url: str, timeout: float = HTTP_TIMEOUT_S) -> dict[str, Any]:
    """Fetch the app page and ``GET /api/app`` the way the window does."""
    import httpx

    parts = urllib.parse.urlsplit(url)
    token = urllib.parse.parse_qs(parts.query).get("token", [""])[0]
    base = f"{parts.scheme}://{parts.netloc}"
    out: dict[str, Any] = {}
    # trust_env=False: a CI proxy (HTTPS_PROXY/ALL_PROXY) must not be asked for 127.0.0.1
    try:
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            page = client.get(url)
            out["page"] = {
                "status": page.status_code,
                "content_type": page.headers.get("content-type", ""),
                "bytes": len(page.content),
                "html": b"<html" in page.content[:4096].lower(),
            }
            api = client.get(f"{base}/api/app", headers={"X-Voicestream-Token": token})
            body: Any = None
            if api.headers.get("content-type", "").startswith("application/json"):
                body = api.json()
            info = body if isinstance(body, dict) else {}
            out["api_app"] = {
                "status": api.status_code,
                "state": info.get("state"),
                "version": info.get("version"),
                "setup_done": info.get("setup_done"),
            }
    except (httpx.HTTPError, ValueError) as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def http_checks(http: dict[str, Any]) -> dict[str, bool]:
    page, api = http.get("page") or {}, http.get("api_app") or {}
    return {
        "app_page": page.get("status") == 200 and bool(page.get("html")),
        "api_app": api.get("status") == 200 and api.get("state") is not None,
    }


def write_report(path: Path | None, report: dict[str, Any]) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("could not write the self-test report %s: %s", path, exc)


def write_phase(report_path: Path | None, phase: str) -> None:
    """``<report>.phase`` holds how far the self-test got, so CI can take its screenshots at the
    right moments (window shown, menu open) without guessing delays."""
    if report_path is None:
        return
    try:
        report_path.with_name(report_path.name + ".phase").write_text(phase + "\n", encoding="utf-8")
    except OSError:
        pass
