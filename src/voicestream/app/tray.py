"""The Windows app's icon in the notification area (next to the clock), through ``pystray``.

The Mac app has its menu-bar icon (:mod:`.native`); on Windows the app opens in the browser and
lives in this icon:

* a left click opens the app in the browser (the menu's default item, in bold);
* a right click opens the menu: what Voicestream is doing (the same line as on the Mac),
  start or stop a meeting, open the app, quit;
* the icon shows a red dot while a meeting is recorded, and its tooltip says how long it has run.

Quitting stops the host the graceful way (a meeting in progress is wrapped up and saved) before
the icon goes away: the app has no console window, so this is how it ends.

``--selftest SECONDS`` (the Windows CI job): the icon is shown, the page and the API are
checked, then the app quits through the menu's own Quit after SECONDS.

``pystray`` is imported only in :func:`run`, so the module (its menu and icon drawing) also
imports, and is tested, where there is no notification area.
"""

from __future__ import annotations

import contextlib
import logging
import sys
import threading
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import launcher
from .floating import meeting_started
from .native import STRINGS, is_manual, pick_language, state_line

log = logging.getLogger(__name__)

TOOLTIP_MAX = 127
"""Windows cuts a notification-area tooltip at 128 characters (NOTIFYICONDATA.szTip)."""
TICK_S = 30.0
"""How often the tooltip's « Meeting in progress · N min » is written again."""
ICON_PX = 64
TOP, BOTTOM = (99, 102, 241), (14, 165, 233)  # the app icon's gradient (packaging/mac/make_icon.py)
BARS = (0.30, 0.55, 1.00, 0.55, 0.30)
RECORDING_DOT = (226, 69, 47)


@dataclass(frozen=True, slots=True)
class Entry:
    key: str
    title: str = ""
    enabled: bool = True
    visible: bool = True
    default: bool = False

    @property
    def separator(self) -> bool:
        return self.key.startswith("sep")


def recording(state: dict[str, Any]) -> bool:
    """A meeting is being recorded (or paused in the middle of one): the icon gets its red dot."""
    return meeting_started(state) is not None and state.get("state") in ("listening", "paused")


def menu_entries(state: dict[str, Any], lang: str, now: datetime, *, quitting: bool = False) -> list[Entry]:
    """The menu, from the host's state. Start and stop call the same host actions as the Mac menu."""
    s = STRINGS.get(lang, STRINGS["en"])
    current = str(state.get("state") or "stopped")
    in_meeting = bool(state.get("in_meeting")) or current in ("starting", "listening", "paused")
    busy = quitting or current == "stopping" or bool(state.get("start_queued"))
    if is_manual(state):
        start, stop = s["start_meeting"], s["stop_meeting"]
    else:
        start, stop = s["start"], s["stop"]
    return [
        Entry("status", state_line(state, lang, now, quitting=quitting), enabled=False),
        Entry("sep1"),
        Entry("open", s["open"], enabled=not quitting, default=True),
        Entry("start", start, enabled=not busy, visible=not in_meeting),
        Entry("stop", stop, enabled=not busy, visible=in_meeting),
        Entry("sep2"),
        Entry("quit", s["quit"], enabled=not quitting),
    ]


def tooltip(state: dict[str, Any], lang: str, now: datetime, *, quitting: bool = False) -> str:
    line = f"Voicestream · {state_line(state, lang, now, quitting=quitting)}"
    return line if len(line) <= TOOLTIP_MAX else line[: TOOLTIP_MAX - 1] + "…"


def draw_icon(*, dot: bool = False, size: int = ICON_PX) -> Any:
    """The app icon, small: the gradient square with its sound wave; a red dot while recording."""
    from PIL import Image, ImageDraw

    scale = 4  # draw large and scale down: Pillow's shapes have no anti-aliasing
    big = size * scale
    grad = Image.new("RGB", (1, big))
    for y in range(big):
        t = y / max(1, big - 1)
        grad.putpixel((0, y), tuple(round(a + (b - a) * t) for a, b in zip(TOP, BOTTOM, strict=True)))
    grad = grad.resize((big, big))
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, big - 1, big - 1), radius=big // 5, fill=255)
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    img.paste(grad, (0, 0), mask)
    draw = ImageDraw.Draw(img)
    bar_w, gap = big * 0.09, big * 0.055
    total = len(BARS) * bar_w + (len(BARS) - 1) * gap
    x = (big - total) / 2
    for h in BARS:
        bar_h = big * 0.6 * h
        top = (big - bar_h) / 2
        draw.rounded_rectangle((x, top, x + bar_w, top + bar_h), radius=bar_w / 2, fill=(255, 255, 255, 255))
        x += bar_w + gap
    if dot:
        r = big * 0.2
        cx = cy = big - r - big * 0.02
        draw.ellipse((cx - r - big * 0.04, cy - r - big * 0.04, cx + r + big * 0.04, cy + r + big * 0.04),
                     fill=(255, 255, 255, 255))
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(*RECORDING_DOT, 255))
    return img.resize((size, size), Image.Resampling.LANCZOS)


def windows_languages() -> list[str]:
    """The user's language on Windows (``fr-FR``…), for the menu when the app has no preference."""
    with contextlib.suppress(Exception):
        import ctypes

        buf = ctypes.create_unicode_buffer(85)
        if ctypes.windll.kernel32.GetUserDefaultLocaleName(buf, len(buf)):  # type: ignore[attr-defined]
            return [buf.value]
    import locale

    lang = locale.getlocale()[0]
    return [lang] if lang else []


class Tray:
    """The icon and its menu, kept in step with the host's state."""

    def __init__(self, host: launcher.Host, url: str, pystray: Any, *, open_url: Callable[[str], Any] = webbrowser.open,
                 languages: Callable[[], list[str]] = windows_languages) -> None:
        self.host, self.url, self._pystray, self._open_url = host, url, pystray, open_url
        self._languages = languages
        self.quitting = False
        self.graceful: bool | None = None
        self.quit_at: float | None = None
        self._state: dict[str, Any] = launcher.safe_state(host)
        self._dot = recording(self._state)
        self._lock = threading.Lock()
        self._done = threading.Event()
        self.icon = pystray.Icon("Voicestream", icon=draw_icon(dot=self._dot), title=self._tooltip(),
                                 menu=pystray.Menu(lambda: self._items()))

    # ------------------------------------------------------------- what it shows

    @property
    def lang(self) -> str:
        return pick_language((self._state.get("prefs") or {}).get("ui_language"), self._languages())

    def entries(self) -> list[Entry]:
        return menu_entries(self._state, self.lang, datetime.now(UTC), quitting=self.quitting)

    def _tooltip(self) -> str:
        return tooltip(self._state, self.lang, datetime.now(UTC), quitting=self.quitting)

    def _items(self) -> list[Any]:
        """pystray asks for the menu each time it builds it (``Menu`` given a callable)."""
        p = self._pystray
        actions = {"open": self.open, "start": self.start, "stop": self.stop, "quit": self.quit}
        items = []
        for e in self.entries():
            if not e.visible:
                continue
            if e.separator:
                items.append(p.Menu.SEPARATOR)
                continue
            action = actions.get(e.key)
            items.append(p.MenuItem(e.title, (lambda fn: lambda icon, item: fn())(action) if action else None,
                                    enabled=e.enabled, default=e.default))
        return items

    def refresh(self, state: dict[str, Any] | None = None) -> None:
        """A new host state (or the timer): the tooltip, the red dot and the menu follow it."""
        with self._lock:
            if state is not None:
                self._state = state
            dot = recording(self._state)
            with contextlib.suppress(Exception):  # the icon may be going away
                self.icon.title = self._tooltip()
                if dot != self._dot:
                    self._dot = dot
                    self.icon.icon = draw_icon(dot=dot)
                self.icon.update_menu()

    # ------------------------------------------------------------------ actions

    def open(self) -> None:
        self._open_url(self.url)

    def _meeting(self, action: str) -> None:
        coro = self.host.meeting_start() if action == "start" else self.host.meeting_stop()
        try:
            fut = self.host.call(coro)
        except Exception:  # noqa: BLE001
            log.exception("could not %s the meeting", action)
            return
        def done(f: Any) -> None:
            if not f.cancelled() and f.exception() is not None:
                log.warning("%s meeting: %s", action, f.exception())

        fut.add_done_callback(done)

    def start(self) -> None:
        self._meeting("start")

    def stop(self) -> None:
        self._meeting("stop")

    def quit(self) -> None:
        """Wrap the meeting up and stop the host (in a thread: the menu stays responsive), then remove
        the icon. A second Quit while saving does nothing more."""
        if self.quitting:
            return
        self.quitting = True
        self.quit_at = time.monotonic()
        self.refresh()

        def stop_all() -> None:
            try:
                self.graceful = launcher.shutdown_host(self.host)
            finally:
                self._done.set()
                with contextlib.suppress(Exception):
                    self.icon.stop()

        threading.Thread(target=stop_all, name="voicestream-quit", daemon=True).start()

    def wait_stopped(self, timeout: float | None = None) -> bool:
        return self._done.wait(timeout)

    # --------------------------------------------------------------------- loop

    def tick(self) -> None:
        while not self._done.wait(TICK_S):
            self.refresh()


def run(host: launcher.Host, *, selftest: float | None = None, report_path: Path | None = None,
        pystray: Any = None, open_url: Callable[[str], Any] = webbrowser.open) -> int:
    """Start the host, show the icon (and the app in the browser), run until Quit."""
    if pystray is None:
        import pystray  # noqa: PLW0621 - the real one, here only: it picks its backend at import
    started = time.monotonic()
    try:
        url = host.run_in_thread()
    except Exception as exc:  # noqa: BLE001
        log.exception("the app failed to start")
        if selftest is not None:
            launcher.write_report(report_path, {"ok": False, "mode": "tray", "errors": [f"start: {exc}"]})
        return 2
    tray = Tray(host, url, pystray, open_url=open_url)
    host.add_state_listener(tray.refresh)
    report: dict[str, Any] | None = None

    def setup(icon: Any) -> None:
        nonlocal report
        icon.visible = True
        threading.Thread(target=tray.tick, name="voicestream-tray-tick", daemon=True).start()
        if selftest is None:
            tray.open()
            return
        report = launcher.selftest_report(host, url, mode="tray")
        report["http"] = launcher.check_http(url)
        report["checks"].update(launcher.http_checks(report["http"]))
        report["checks"]["tray_icon"] = bool(icon.visible)
        report["tray"] = {"tooltip": tray._tooltip(), "menu": [e.title for e in tray.entries() if e.visible and e.title]}
        report["checks"]["tray_menu"] = len(tray._items()) >= 4
        if report["frozen"] and sys.platform == "win32":  # the packaged app: its own whisper-server
            report["whisper_server_check"] = ws = launcher.whisper_server_check()
            report["checks"]["whisper_server_bundled"] = bool(ws.get("ok"))
        launcher.finish_report(report, started)
        launcher.write_report(report_path, report)
        launcher.write_phase(report_path, "ready")
        while time.monotonic() - started < selftest and not tray.quitting:
            time.sleep(0.25)
        tray.quit()  # the menu's own Quit

    try:
        tray.icon.run(setup=setup)
    except KeyboardInterrupt:
        tray.quit()
    if not tray.wait_stopped(launcher.GRACEFUL_STOP_S + launcher.FORCE_STOP_S + 5):
        log.warning("the app did not stop in time")
    if report is not None:
        quit_at = tray.quit_at if tray.quit_at is not None else time.monotonic()
        report["quit"] = {"graceful": bool(tray.graceful), "shutdown_s": round(time.monotonic() - quit_at, 2)}
        launcher.write_report(report_path, report)
        launcher.write_phase(report_path, "done")
        return 0 if report["ok"] else 1
    return 0
