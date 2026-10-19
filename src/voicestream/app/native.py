"""The macOS shell around the app host: the window, the menu-bar icon and quitting.

Imported only in native mode (macOS with pywebview and PyObjC). Every AppKit and pywebview
import is inside a function, so the module also imports on Linux, where its pure helpers (what
the menu bar shows for a given host state) are tested.

Threads. AppKit lives on the main thread: ``webview.start()`` runs the Cocoa run loop there. The
host runs its asyncio loop in a background thread. They meet in two places only: host state
changes reach the main thread through ``AppHelper.callAfter``, and menu clicks reach the host
through ``host.call()``, which returns at once. Nothing on the main thread waits for the host,
so a slow stop can never freeze the menu bar.

pywebview 6.2.1 is pinned because this reaches into its Cocoa backend: it creates its app
delegate from the class attribute ``webview.platforms.cocoa.BrowserView.AppDelegate`` with its
first window, so a subclass put there before ``webview.start()`` adds what that delegate lacks.

* ``applicationShouldHandleReopen:hasVisibleWindows:``: clicking the Dock icon, or opening the
  app again from Finder, shows the hidden window.
* ``applicationShouldTerminate:``: pywebview's own runs the window's ``closing`` handlers, and
  ours hides the window instead of closing it, so the app could never quit. Ours asks first
  while listening, then stops the host gracefully (``NSTerminateLater``, capped), then quits.
* ``applicationWillTerminate:``: AppKit ends the process with ``exit()``, which skips Python's
  own shutdown, so the ``atexit`` handlers (they stop whisper-server children) run here. The
  shell also observes the notification itself, so this runs even without our delegate.

If the delegate can't be installed, the window and the menu bar still work and Quit still quits
(through pywebview's own handler); the self-test reports it.

Closing the window hides it and the Dock icon (activation policy "accessory"): the app keeps
listening from the menu bar. "Open Voicestream", the Dock or Finder brings both back.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import launcher, native_hooks

log = logging.getLogger(__name__)

WINDOW_TITLE = "Voicestream"
WINDOW_SIZE = (1100, 760)
MIN_SIZE = (900, 640)  # the UI is laid out for 900×640 and up
FRAME_NAME = "Voicestream main window"
"""NSWindow frame autosave name: AppKit remembers the window's size and place (and copes with a
screen that is no longer connected) in the app's user defaults."""
BACKGROUND = {"light": "#F8F8F8", "dark": "#0F0F0F"}  # the UI's --ground, shown before the page paints
LOAD_TIMEOUT_S = 45.0
MAIN_THREAD_TIMEOUT_S = 10.0
SELFTEST_PAUSE_S = 3.0  # time for CI's screenshot at each self-test phase
WATCHDOG_EXTRA_S = 90.0
SELFTEST_QUIT_S = launcher.GRACEFUL_STOP_S + launcher.FORCE_STOP_S + 15.0
"""After the self-test asks the app to quit: longer than the capped host shutdown, then the
process ends itself (report written) so the CI job never waits on a quit that went nowhere."""

# AppKit values, spelled out so this module doesn't depend on which constants PyObjC exports
POLICY_REGULAR, POLICY_ACCESSORY = 0, 1  # NSApplicationActivationPolicy
TERMINATE_CANCEL, TERMINATE_NOW, TERMINATE_LATER = 0, 1, 2  # NSApplicationTerminateReply
ALERT_FIRST_BUTTON = 1000  # NSAlertFirstButtonReturn
CONTROL_ON, CONTROL_OFF = 1, 0  # NSControlStateValue
IMAGE_LEFT = 2  # NSImageLeft
VARIABLE_LENGTH = -1.0  # NSVariableStatusItemLength
ACTIVITY_OPTIONS = 0x00EFFFFF
"""NSActivityUserInitiatedAllowingIdleSystemSleep: no App Nap (it would throttle audio capture)
and no sudden termination while listening, but the Mac may still go to sleep when idle."""
QUIT_REASON = int.from_bytes(b"why?", "big")
"""keyAEQuitReason: set on the quit Apple event when macOS quits apps for a logout or shutdown."""
WILL_TERMINATE = "NSApplicationWillTerminateNotification"

# Type encodings of the app delegate methods pywebview's class lacks. PyObjC refuses a whole class
# (objc.BadPrototypeError) when a method overrides one the class already has with another encoding:
# that is how the macOS CI run lost the delegate ("I@:@ != Q@:@", the runtime's first). pywebview's
# own applicationShouldTerminate: got b"I@:@" from PyObjC 12.2's AppKit metadata, ours said
# objc._C_NSUInteger (b"Q" on 64-bit). So that override carries no encoding: PyObjC then copies the
# one of the method it overrides, whatever this PyObjC's metadata says.
SIG_REOPEN = b"Z@:@Z"  # (BOOL)applicationShouldHandleReopen:(NSApplication *) hasVisibleWindows:(BOOL)
SIG_WILL_TERMINATE = b"v@:@"  # (void)applicationWillTerminate:(NSNotification *)

LISTENING_STATES = frozenset({"starting", "listening", "paused"})
SYMBOLS = {
    "listening": "mic.fill",
    "paused": "pause.circle",
    "stopped": "mic.slash",
    "error": "exclamationmark.triangle",
    "starting": "mic",
    "stopping": "mic",
}

STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "listening": "Listening",
        "meeting": "Meeting in progress · {minutes} min",
        "paused": "Paused — nothing is being recorded",
        "stopped": "Not listening",
        "starting": "Starting…",
        "stopping": "Stopping…",
        "error": "Problem: {error}",
        "quitting": "Quitting — saving your meeting…",
        "open": "Open Voicestream",
        "pause": "Pause",
        "resume": "Resume listening",
        "start": "Start listening",
        "stop": "Stop listening",
        "end_meeting": "End meeting now",
        "login": "Open at login",
        "login_approval": "Open at login (allow it in System Settings)",
        "quit": "Quit Voicestream",
        "quit_title": "Stop listening and quit Voicestream?",
        "quit_detail": "Voicestream won't hear your meetings until you open it again.",
        "quit_meeting": "The meeting in progress is wrapped up and its notes are saved first.",
        "quit_button": "Quit",
        "cancel": "Cancel",
        "start_failed": "Voicestream could not start",
    },
    "fr": {
        "listening": "À l'écoute",
        "meeting": "Réunion en cours · {minutes} min",
        "paused": "En pause — rien n'est enregistré",
        "stopped": "Écoute arrêtée",
        "starting": "Démarrage…",
        "stopping": "Arrêt…",
        "error": "Problème : {error}",
        "quitting": "Fermeture — enregistrement de la réunion…",
        "open": "Ouvrir Voicestream",
        "pause": "Mettre en pause",
        "resume": "Reprendre l'écoute",
        "start": "Démarrer l'écoute",
        "stop": "Arrêter l'écoute",
        "end_meeting": "Terminer la réunion",
        "login": "Ouvrir à la connexion",
        "login_approval": "Ouvrir à la connexion (à autoriser dans Réglages Système)",
        "quit": "Quitter Voicestream",
        "quit_title": "Arrêter l'écoute et quitter Voicestream ?",
        "quit_detail": "Voicestream n'entendra plus vos réunions jusqu'à ce que vous le rouvriez.",
        "quit_meeting": "La réunion en cours est d'abord clôturée et ses notes enregistrées.",
        "quit_button": "Quitter",
        "cancel": "Annuler",
        "start_failed": "Voicestream n'a pas pu démarrer",
    },
}

PYWEBVIEW_FR = {
    "global.quitConfirmation": "Voulez-vous vraiment quitter ?",
    "global.ok": "OK",
    "global.quit": "Quitter",
    "global.cancel": "Annuler",
    "global.saveFile": "Enregistrer le fichier",
    "cocoa.menu.about": "À propos de",
    "cocoa.menu.services": "Services",
    "cocoa.menu.view": "Présentation",
    "cocoa.menu.edit": "Édition",
    "cocoa.menu.hide": "Masquer",
    "cocoa.menu.hideOthers": "Masquer les autres",
    "cocoa.menu.showAll": "Tout afficher",
    "cocoa.menu.quit": "Quitter",
    "cocoa.menu.fullscreen": "Passer en mode plein écran",
    "cocoa.menu.cut": "Couper",
    "cocoa.menu.copy": "Copier",
    "cocoa.menu.paste": "Coller",
    "cocoa.menu.selectAll": "Tout sélectionner",
}
"""pywebview's app/Edit menus in French (its keys, from webview/localization.py)."""


# ------------------------------------------------------------- pure helpers


@dataclass(frozen=True, slots=True)
class MenuEntry:
    key: str
    title: str = ""
    enabled: bool = True
    visible: bool = True
    checked: bool = False
    shortcut: str = ""

    @property
    def separator(self) -> bool:
        return self.key.startswith("sep")


def pick_language(pref: str | None, preferred: Iterable[str] = ()) -> str:
    """``"fr"`` or ``"en"``: the app preference, else the Mac's first preferred language."""
    if pref in STRINGS:
        return str(pref)
    for lang in preferred:
        return "fr" if str(lang).lower().startswith("fr") else "en"
    return "en"


def meeting_started(state: dict[str, Any]) -> datetime | None:
    if not state.get("in_meeting"):
        return None
    raw = (state.get("meeting") or {}).get("started_at") or state.get("meeting_started_at")
    if not raw:
        return None
    try:
        started = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return started if started.tzinfo else started.replace(tzinfo=UTC)


def format_elapsed(seconds: float) -> str:
    """``4:05``, ``12:34``, ``1:02:03``."""
    s = max(0, int(seconds))
    h, rest = divmod(s, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def status_symbol(state: dict[str, Any]) -> str:
    """The SF Symbol for the menu-bar icon."""
    return SYMBOLS.get(str(state.get("state") or "stopped"), "mic.slash")


def status_title(state: dict[str, Any], now: datetime) -> str:
    """Text next to the icon: the meeting timer while a meeting is being recorded, else nothing."""
    started = meeting_started(state)
    if started is None or state.get("state") not in ("listening", "paused"):
        return ""
    return format_elapsed((now - started).total_seconds())


def _short(text: Any, limit: int = 70) -> str:
    line = str(text or "").strip().splitlines()[0] if str(text or "").strip() else ""
    return line if len(line) <= limit else line[: limit - 1] + "…"


def state_line(state: dict[str, Any], lang: str, now: datetime, *, quitting: bool = False) -> str:
    """The first (disabled) line of the menu, also the icon's tooltip."""
    s = STRINGS.get(lang, STRINGS["en"])
    if quitting:
        return s["quitting"]
    current = str(state.get("state") or "stopped")
    if current == "error":
        return s["error"].format(error=_short(state.get("error")) or "?")
    started = meeting_started(state)
    if current == "listening" and started is not None:
        return s["meeting"].format(minutes=max(0, int((now - started).total_seconds() // 60)))
    return s.get(current, s["stopped"])


def menu_model(state: dict[str, Any], lang: str, login: str, *, now: datetime | None = None,
               quitting: bool = False) -> list[MenuEntry]:
    """The status item's menu for this state, always the same keys in the same order (the native
    menu is built once and updated in place)."""
    s = STRINGS.get(lang, STRINGS["en"])
    now = now or datetime.now(UTC)
    current = str(state.get("state") or "stopped")
    busy = quitting or current in ("starting", "stopping")
    stopped = current in ("stopped", "error")
    login_on = login in ("enabled", "requires_approval")
    return [
        MenuEntry("status", state_line(state, lang, now, quitting=quitting), enabled=False),
        MenuEntry("sep1"),
        MenuEntry("open", s["open"], enabled=not quitting),
        MenuEntry("sep2"),
        MenuEntry("pause", s["resume"] if current == "paused" else s["pause"], enabled=not busy,
                  visible=current in ("listening", "paused")),
        MenuEntry("listen", s["start"] if stopped else s["stop"], enabled=not busy),
        MenuEntry("end_meeting", s["end_meeting"], enabled=not busy, visible=bool(state.get("in_meeting"))),
        MenuEntry("sep3", visible=login != "unsupported"),
        MenuEntry("login", s["login_approval"] if login == "requires_approval" else s["login"], enabled=not quitting,
                  visible=login != "unsupported", checked=login_on),
        MenuEntry("sep4"),
        MenuEntry("quit", s["quit"], enabled=not quitting, shortcut="q"),
    ]


def needs_quit_confirmation(state: dict[str, Any]) -> bool:
    """Quitting stops listening: ask first, unless nothing is being listened to."""
    return bool(state.get("in_meeting")) or state.get("state") in LISTENING_STATES


# --------------------------------------------------------------- AppKit glue


_SHELL: MacShell | None = None
_PATCHED = False
"""Our app delegate class is the one pywebview creates: we answer ``applicationShouldTerminate:``."""
_TARGET_CLASS: Any = None
_DELEGATE_CLASS: Any = None


def _app() -> Any:
    import AppKit

    return AppKit.NSApplication.sharedApplication()


def _guard(fn: Callable[[], Any]) -> None:
    """Never let a Python exception escape into AppKit (it would abort the app)."""
    try:
        fn()
    except Exception:  # noqa: BLE001
        log.exception("menu-bar action failed")


def _app_delegate_class(base: Any) -> Any:
    """pywebview's app delegate class plus what it lacks (defined once: Objective-C class names are
    process-wide). A subclass, not an ``objc.Category``: pywebview's own class stays as it is."""
    global _DELEGATE_CLASS
    if _DELEGATE_CLASS is None:
        import objc

        class VoicestreamAppDelegate(base):
            # no typedSelector: overrides pywebview's, so it must keep that method's encoding (above)
            def applicationShouldTerminate_(self, app):  # noqa: ANN001, ANN202
                shell = _SHELL
                if shell is None:
                    return TERMINATE_NOW
                try:
                    return shell.should_terminate()
                except Exception:  # noqa: BLE001 - quitting must always remain possible
                    log.exception("quit handler failed")
                    return TERMINATE_NOW

            @objc.typedSelector(SIG_REOPEN)
            def applicationShouldHandleReopen_hasVisibleWindows_(self, app, has_visible):  # noqa: ANN001, ANN202
                shell = _SHELL
                if shell is not None:
                    _guard(shell.show_window)
                return False  # handled: AppKit has nothing more to do

            @objc.typedSelector(SIG_WILL_TERMINATE)
            def applicationWillTerminate_(self, notification):  # noqa: ANN001, ANN202
                shell = _SHELL
                if shell is not None:
                    _guard(shell.will_terminate)

            def voicestreamPatched(self):  # noqa: ANN202 - lets the self-test see it is our delegate
                return True

        _DELEGATE_CLASS = VoicestreamAppDelegate
    return _DELEGATE_CLASS


def install_app_delegate(browser_view: Any) -> None:
    """Make pywebview create our app delegate instead of its own (once).

    pywebview 6.2.1 creates it with its first window, inside ``webview.start()``:
    ``BrowserView._shared_app_delegate = BrowserView.AppDelegate.alloc().init()`` in
    ``BrowserView.__init__``, then ``app.setDelegate_`` (again after a window was closed and a new
    one opened). It reads the class attribute each time, so the subclass must be there before
    ``webview.start()``."""
    global _PATCHED
    if _PATCHED:
        return
    base = browser_view.AppDelegate
    if base.__name__ != "AppDelegate":  # pragma: no cover - pywebview renamed it: unpinned upgrade
        raise RuntimeError(f"unexpected pywebview app delegate {base.__name__!r}")
    browser_view.AppDelegate = _app_delegate_class(base)
    _PATCHED = True


def _menu_target_class() -> Any:
    """The NSObject that receives menu clicks, menuWillOpen:, the timer and the will-terminate
    notification (defined once: Objective-C class names are process-wide)."""
    global _TARGET_CLASS
    if _TARGET_CLASS is None:
        import objc
        from Foundation import NSObject

        class VoicestreamMenuTarget(NSObject):
            @objc.typedSelector(b"v@:@")
            def menuAction_(self, sender):  # noqa: ANN001, ANN202
                _guard(lambda: self.shell.on_menu(str(sender.representedObject())))

            @objc.typedSelector(b"v@:@")
            def menuWillOpen_(self, menu):  # noqa: ANN001, ANN202
                _guard(self.shell.menu_will_open)

            @objc.typedSelector(b"v@:@")
            def tick_(self, timer):  # noqa: ANN001, ANN202
                _guard(self.shell.render)

            @objc.typedSelector(SIG_WILL_TERMINATE)
            def appWillTerminate_(self, notification):  # noqa: ANN001, ANN202
                _guard(self.shell.will_terminate)

        _TARGET_CLASS = VoicestreamMenuTarget
    return _TARGET_CLASS


def _preferred_languages() -> list[str]:
    try:
        from Foundation import NSLocale

        return [str(x) for x in NSLocale.preferredLanguages()]
    except Exception:  # noqa: BLE001
        return []


def _background_color() -> str:
    try:
        from Foundation import NSUserDefaults

        dark = str(NSUserDefaults.standardUserDefaults().stringForKey_("AppleInterfaceStyle") or "") == "Dark"
    except Exception:  # noqa: BLE001
        dark = False
    return BACKGROUND["dark" if dark else "light"]


def _system_is_quitting_us() -> bool:
    """macOS is logging out / shutting down: don't hold that up with a question."""
    try:
        from Foundation import NSAppleEventManager

        event = NSAppleEventManager.sharedAppleEventManager().currentAppleEvent()
        return event is not None and event.attributeDescriptorForKeyword_(QUIT_REASON) is not None
    except Exception:  # noqa: BLE001
        return False


def _run_exit_handlers() -> None:
    import atexit

    try:
        atexit._run_exitfuncs()  # what Python's shutdown would have run; AppKit's exit() won't
    except Exception:  # noqa: BLE001
        log.exception("exit handlers failed")
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.flush()


def _with_timeout(fn: Callable[[], Any], timeout: float) -> Any:
    """Run a blocking pywebview call (e.g. ``run_js``) without risking the self-test hanging on it."""
    box: dict[str, Any] = {}

    def call() -> None:
        try:
            box["value"] = fn()
        except Exception as exc:  # noqa: BLE001
            box["error"] = f"{type(exc).__name__}: {exc}"

    t = threading.Thread(target=call, name="voicestream-selftest-call", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f"no answer after {timeout:.0f} s")
    if "error" in box:
        raise RuntimeError(box["error"])
    return box.get("value")


# ------------------------------------------------------------------ the shell


def preflight() -> None:
    """Raise ImportError unless everything native mode needs imports, before the host starts (the
    launcher then falls back to the browser). Leaves pywebview's Cocoa backend alone: importing it
    creates the NSApplication."""
    import AppKit  # noqa: F401
    import Foundation  # noqa: F401
    import objc  # noqa: F401
    import WebKit  # noqa: F401
    import webview  # noqa: F401
    from PyObjCTools import AppHelper  # noqa: F401


def run(host: launcher.Host, *, selftest: float | None = None, report_path: Path | None = None) -> int:
    """Start the host, then run the Cocoa app on this (the main) thread until the app quits."""
    caps = getattr(host, "capabilities", None)
    if isinstance(caps, dict):
        caps["native_window"] = True
    try:
        url = host.run_in_thread()
    except Exception as exc:  # noqa: BLE001
        log.exception("the app failed to start")
        if selftest is not None:
            launcher.write_report(report_path, {"ok": False, "mode": "native", "errors": [f"start: {exc}"]})
        else:
            _fatal_alert(str(exc))
        return 2
    return MacShell(host, url, selftest=selftest, report_path=report_path).run()


def _fatal_alert(message: str) -> None:
    try:
        import AppKit

        s = STRINGS[pick_language(None, _preferred_languages())]
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_(s["start_failed"])
        alert.setInformativeText_(_short(message, 500))
        _app().activateIgnoringOtherApps_(True)
        alert.runModal()
    except Exception:  # noqa: BLE001
        print(f"error: Voicestream could not start: {message}", file=sys.stderr)


class MacShell:
    """One per process: the pywebview window, the NSStatusItem and the quit sequence."""

    def __init__(self, host: launcher.Host, url: str, *, selftest: float | None = None,
                 report_path: Path | None = None) -> None:
        self.host = host
        self.url = url
        self.selftest = selftest
        self.report_path = report_path
        self.window: Any = None  # webview.Window
        self.state: dict[str, Any] = {}
        self.lang = "en"
        self.quitting = False
        self.exit_code = 0
        self.report: dict[str, Any] = {}
        self.delegate_error: str | None = None  # why our app delegate couldn't be installed
        self._terminating = False
        self._terminated = False
        self._watchdog: threading.Timer | None = None
        self._login = "unsupported"
        self._item: Any = None  # strong refs: AppKit doesn't keep the status item, menu or target alive
        self._menu: Any = None
        self._target: Any = None
        self._entries: dict[str, Any] = {}
        self._images: dict[str, Any] = {}
        self._symbol = ""
        self._timer: Any = None
        self._activity: Any = None
        self._menu_opened = 0
        self._quit_started = 0.0
        self._quit_info: dict[str, Any] = {}

    # ------------------------------------------------------------ startup

    def run(self) -> int:
        global _SHELL
        import webview

        # importing the Cocoa backend creates the NSApplication; we patch its app delegate class
        from webview.platforms import cocoa

        _SHELL = self
        try:
            install_app_delegate(cocoa.BrowserView)
        except Exception as exc:  # noqa: BLE001 - window and menu still work, and Quit still quits (see quit())
            log.exception("could not install the app delegate")
            self.delegate_error = f"{type(exc).__name__}: {exc}"
        self._observe_termination()
        self.state = launcher.safe_state(self.host)
        self.lang = self._language()
        self.window = webview.create_window(
            WINDOW_TITLE,
            self.url,
            width=WINDOW_SIZE[0],
            height=WINDOW_SIZE[1],
            min_size=MIN_SIZE,
            text_select=True,  # transcripts and notes are there to be copied
            background_color=_background_color(),
        )
        self.window.events.before_show += self._restore_frame
        self.window.events.closing += self._on_closing
        self._login = self._login_status(refresh=True)
        from PyObjCTools import AppHelper

        # runs once NSApp.run() has started (inside webview.start), i.e. after the app finished launching;
        # state updates queued later run after it (the main thread's queue is first in, first out)
        AppHelper.callAfter(self._start_menu_bar)
        self.host.add_state_listener(self._on_host_state)
        storage = native_hooks.app_support_dir() / "web"
        try:
            webview.start(
                func=self._selftest if self.selftest is not None else None,
                gui="cocoa",
                private_mode=False,  # keep the page's local storage (UI choices) between launches
                storage_path=str(storage),
                localization=PYWEBVIEW_FR if self.lang == "fr" else {},
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("the window could not be opened")
            if self.selftest is not None:
                launcher.write_report(self.report_path, {"ok": False, "mode": "native", "errors": [f"window: {exc}"]})
            else:
                _fatal_alert(str(exc))
            self.exit_code = 2
        # only reached when the run loop stops without AppKit ending the process
        launcher.shutdown_host(self.host)
        return self.exit_code

    def _menu_target(self) -> Any:
        if self._target is None:
            target = _menu_target_class().alloc().init()
            target.shell = self
            self._target = target
        return self._target

    def _observe_termination(self) -> None:
        """Hear NSApplicationWillTerminateNotification directly too: AppKit posts it to every
        observer, so :meth:`will_terminate` (the exit handlers, the self-test's last report) runs
        even when the app delegate isn't ours — its install failed, or pywebview detached it."""
        try:
            import Foundation

            Foundation.NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
                self._menu_target(), "appWillTerminate:", WILL_TERMINATE, None)
        except Exception:  # noqa: BLE001 - the delegate's applicationWillTerminate: still does it
            log.exception("could not watch for the app quitting")

    def _start_menu_bar(self) -> None:
        try:
            self._create_status_item()
        except Exception:  # noqa: BLE001 - the window still works without it
            log.exception("could not create the menu-bar icon")
        # read again: listening may have started before the state listener was added
        self.apply_state(launcher.safe_state(self.host))

    def _language(self) -> str:
        prefs = self.state.get("prefs") or {}
        return pick_language(prefs.get("ui_language"), _preferred_languages())

    def _login_status(self, *, refresh: bool = False) -> str:
        try:
            return native_hooks.refresh_login_item() if refresh else native_hooks.login_item_status()
        except Exception:  # noqa: BLE001
            log.exception("could not read the login item")
            return "unsupported"

    # -------------------------------------------------------------- window

    def _restore_frame(self) -> None:
        """Runs on the main thread just before the window first shows."""
        native = self.window.native if self.window is not None else None
        if native is None:
            return
        native.setFrameUsingName_(FRAME_NAME)
        native.setFrameAutosaveName_(FRAME_NAME)

    def _on_closing(self) -> bool | None:
        """pywebview's ``closing`` event (main thread, synchronous): returning False keeps the
        window; we hide it instead so the app keeps listening from the menu bar.

        Only pywebview's own ``applicationShouldTerminate:`` (our delegate failed to install) needs
        the window to really close for a quit. While our delegate wraps a quit up (up to ~35 s, the
        window still takes clicks), a close must hide it too: closing the last window makes
        pywebview detach the app delegate, and AppKit would then quit without our
        ``applicationWillTerminate:``."""
        if self.quitting and not _PATCHED:
            return None
        from PyObjCTools import AppHelper

        AppHelper.callAfter(self._hide_now)
        return False

    def _hide_now(self) -> None:
        app = _app()
        native = self.window.native if self.window is not None else None
        if native is not None:
            native.orderOut_(None)
        app.setActivationPolicy_(POLICY_ACCESSORY)  # no Dock icon while there is no window
        app.hide_(None)  # hand the focus back to the app you were in

    def show_window(self) -> None:
        """Main thread only: bring back the Dock icon and the window, in front."""
        app = _app()
        app.setActivationPolicy_(POLICY_REGULAR)
        if app.isHidden():
            app.unhide_(None)
        native = self.window.native if self.window is not None else None
        if native is not None:
            if native.isMiniaturized():
                native.deminiaturize_(None)
            native.makeKeyAndOrderFront_(None)
        app.activateIgnoringOtherApps_(True)

    # ---------------------------------------------------------- status item

    def _create_status_item(self) -> None:
        import AppKit

        item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(VARIABLE_LENGTH)
        with contextlib.suppress(Exception):
            item.setAutosaveName_("Voicestream")  # keeps the place you dragged it to in the menu bar
        button = item.button()
        button.setImagePosition_(IMAGE_LEFT)
        with contextlib.suppress(Exception):  # digits of equal width: the timer doesn't jiggle
            button.setFont_(AppKit.NSFont.monospacedDigitSystemFontOfSize_weight_(AppKit.NSFont.systemFontSize(), 0.0))
        target = self._menu_target()
        menu = AppKit.NSMenu.alloc().initWithTitle_("Voicestream")
        menu.setAutoenablesItems_(False)
        menu.setDelegate_(target)
        for entry in menu_model(self.state, self.lang, self._login):
            if entry.separator:
                mi = AppKit.NSMenuItem.separatorItem()
            else:
                action = None if entry.key == "status" else "menuAction:"
                mi = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(entry.title, action, entry.shortcut)
                mi.setTarget_(target)
                mi.setRepresentedObject_(entry.key)
            menu.addItem_(mi)
            self._entries[entry.key] = mi
        item.setMenu_(menu)
        self._item, self._menu = item, menu

    def _image(self, symbol: str) -> Any:
        if symbol not in self._images:
            import AppKit

            img = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbol, "Voicestream")
            if img is not None:
                img.setTemplate_(True)  # drawn in the menu bar's own colour, light or dark
            self._images[symbol] = img
        return self._images[symbol]

    def apply_state(self, state: dict[str, Any]) -> None:
        """Main thread: a new host state."""
        self.state = state or {}
        self.lang = self._language()
        for step in (self._update_activity, self.render, self._update_timer):
            try:
                step()
            except Exception:  # noqa: BLE001
                log.exception("updating the menu bar failed")

    def _on_host_state(self, state: dict[str, Any]) -> None:
        """Host loop thread: hand the state over to the main thread."""
        from PyObjCTools import AppHelper

        AppHelper.callAfter(self.apply_state, dict(state or {}))

    def render(self) -> None:
        if self._item is None:
            return
        now = datetime.now(UTC)
        button = self._item.button()
        self._symbol = status_symbol(self.state)
        image = self._image(self._symbol)
        title = status_title(self.state, now)
        if image is not None:
            button.setImage_(image)
            button.setTitle_(f" {title}" if title else "")
        else:  # no SF Symbols (shouldn't happen on macOS 11+): words instead of an icon
            button.setTitle_(f"VS {title}".strip())
        button.setToolTip_(state_line(self.state, self.lang, now, quitting=self.quitting))
        for entry in menu_model(self.state, self.lang, self._login, now=now, quitting=self.quitting):
            mi = self._entries.get(entry.key)
            if mi is None:
                continue
            if not entry.separator:
                mi.setTitle_(entry.title)
                mi.setEnabled_(entry.enabled)
                mi.setState_(CONTROL_ON if entry.checked else CONTROL_OFF)
            mi.setHidden_(not entry.visible)

    def _update_timer(self) -> None:
        """Tick the meeting timer once a second, only while it is shown."""
        want = bool(status_title(self.state, datetime.now(UTC)))
        if want and self._timer is None:
            import Foundation

            self._timer = Foundation.NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
                1.0, self._target, "tick:", None, True
            )
            # common modes: keeps ticking while the menu is open
            Foundation.NSRunLoop.mainRunLoop().addTimer_forMode_(self._timer, Foundation.NSRunLoopCommonModes)
        elif not want and self._timer is not None:
            self._timer.invalidate()
            self._timer = None

    def _update_activity(self) -> None:
        """App Nap off while listening (or wrapping up), back on when stopped."""
        import Foundation

        want = self.state.get("state") in LISTENING_STATES | {"stopping"}
        info = Foundation.NSProcessInfo.processInfo()
        if want and self._activity is None:
            self._activity = info.beginActivityWithOptions_reason_(ACTIVITY_OPTIONS, "Listening for meetings")
        elif not want and self._activity is not None:
            info.endActivity_(self._activity)
            self._activity = None

    def menu_will_open(self) -> None:
        self._menu_opened += 1
        self._login = self._login_status()  # it may have been changed in System Settings
        self.render()

    # --------------------------------------------------------------- menu actions

    def on_menu(self, key: str) -> None:
        """Main thread. Only posts work to the host loop: never waits for it."""
        current = self.state.get("state")
        if key == "open":
            self.show_window()
        elif key == "pause":
            self._post(self.host.listen_resume() if current == "paused" else self.host.listen_pause())
        elif key == "listen":
            if current == "error":
                # "error" can leave half the assistant running (the microphone gave up, meeting audio
                # is still recorded): starting is then a no-op, a restart replaces it
                self._post(self.host.listen_restart())
            elif current == "stopped":
                self._post(self.host.listen_start())
            else:
                self._post(self.host.listen_stop())
        elif key == "end_meeting":
            self._post(self._end_meeting())
        elif key == "login":
            self._toggle_login()
        elif key == "quit":
            self.quit()

    def quit(self) -> None:
        """Main thread: Quit in the menu (and the self-test's own quit)."""
        if not _PATCHED:  # pywebview's own quit asks the window, which would only hide itself
            self.quitting = True
        _app().terminate_(None)

    def _post(self, coro: Any) -> None:
        try:
            fut = self.host.call(coro)
        except Exception:  # noqa: BLE001
            coro.close()
            log.exception("the app host is not running")
            return

        def done(f: Any) -> None:
            if not f.cancelled() and f.exception() is not None:
                log.error("menu-bar action failed: %s", f.exception())

        fut.add_done_callback(done)

    async def _end_meeting(self) -> None:
        end = getattr(self.host, "end_meeting", None)
        if end is not None:
            await end()
            return
        assistant = getattr(self.host, "assistant", None)
        if assistant is not None:
            await assistant.end_meeting()

    def _toggle_login(self) -> None:
        """Through the host, like the Settings page: it calls :func:`native_hooks.set_start_at_login`
        and saves the preference, on its own loop so the two never race."""
        from PyObjCTools import AppHelper

        enable = self._login not in ("enabled", "requires_approval")

        async def change() -> None:
            update = getattr(self.host, "update_prefs", None)
            if update is None:
                native_hooks.set_start_at_login(enable)
            else:
                update({"start_at_login": enable})

        try:
            fut = self.host.call(change())
        except Exception:  # noqa: BLE001
            log.exception("the app host is not running")
            return
        fut.add_done_callback(lambda f: AppHelper.callAfter(self._login_changed, f))

    def _login_changed(self, fut: Any) -> None:
        error = None if fut.cancelled() else fut.exception()
        if error is not None:
            self._alert(str(error))
        self._login = self._login_status()
        self.render()

    def _alert(self, message: str) -> None:
        import AppKit

        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_("Voicestream")
        alert.setInformativeText_(message)
        _app().activateIgnoringOtherApps_(True)
        alert.runModal()

    # ------------------------------------------------------------------ quitting

    def should_terminate(self) -> int:
        """``applicationShouldTerminate:`` (main thread)."""
        if self._terminating:
            return TERMINATE_LATER  # a quit is already under way; it will answer for both
        if (self.selftest is None and needs_quit_confirmation(self.state) and not _system_is_quitting_us()
                and not self._confirm_quit()):
            return TERMINATE_CANCEL
        self._terminating = True
        self.quitting = True
        self._quit_started = time.monotonic()
        with contextlib.suppress(Exception):
            self.render()
        threading.Thread(target=self._shutdown_then_reply, name="voicestream-quit", daemon=True).start()
        return TERMINATE_LATER

    def _confirm_quit(self) -> bool:
        import AppKit

        s = STRINGS[self.lang]
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_(s["quit_title"])
        alert.setInformativeText_(s["quit_meeting"] if self.state.get("in_meeting") else s["quit_detail"])
        alert.addButtonWithTitle_(s["quit_button"])
        alert.addButtonWithTitle_(s["cancel"])
        _app().activateIgnoringOtherApps_(True)
        return int(alert.runModal()) == ALERT_FIRST_BUTTON

    def _shutdown_then_reply(self) -> None:
        from PyObjCTools import AppHelper

        graceful = launcher.shutdown_host(self.host)
        self._quit_info = {"graceful": graceful, "shutdown_s": round(time.monotonic() - self._quit_started, 2)}
        AppHelper.callAfter(lambda: _app().replyToApplicationShouldTerminate_(True))

    def will_terminate(self) -> None:
        """``applicationWillTerminate:``: the last Python code before AppKit's ``exit()``. Reached
        twice (our delegate, and the notification observer): only the first does anything."""
        if self._terminated:
            return
        self._terminated = True
        if not self._terminating and not self._quit_info:
            # quitting through pywebview's applicationShouldTerminate: (ours couldn't be installed):
            # nothing stopped the host, so stop it here — a meeting in progress is still saved
            t0 = time.monotonic()
            graceful = launcher.shutdown_host(self.host)
            self._quit_info = {"graceful": graceful, "shutdown_s": round(time.monotonic() - t0, 2), "delegate": False}
        if self._activity is not None:
            with contextlib.suppress(Exception):
                import Foundation

                Foundation.NSProcessInfo.processInfo().endActivity_(self._activity)
                self._activity = None
        if self.selftest is not None and self.report:
            self.report["quit"] = self._quit_info
            launcher.write_report(self.report_path, self.report)
            launcher.write_phase(self.report_path, "done")
        _run_exit_handlers()
        if self.exit_code:
            os._exit(self.exit_code)  # exit() would report success

    # ------------------------------------------------------------------ self-test

    def _on_main(self, fn: Callable[[], Any], timeout: float = MAIN_THREAD_TIMEOUT_S) -> Any:
        """Run ``fn`` on the main thread and wait for its result (self-test thread only)."""
        from PyObjCTools import AppHelper

        box: dict[str, Any] = {}
        done = threading.Event()

        def call() -> None:
            try:
                box["value"] = fn()
            except Exception as exc:  # noqa: BLE001
                box["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                done.set()

        AppHelper.callAfter(call)
        if not done.wait(timeout):
            return {"error": "the main thread did not answer"}
        return {"error": box["error"]} if "error" in box else box.get("value")

    def _selftest(self) -> None:
        """pywebview's ``func`` thread: check the real window, menu bar and lifecycle, then quit."""
        from PyObjCTools import AppHelper

        started = time.monotonic()
        self._arm_watchdog((self.selftest or 0) + WATCHDOG_EXTRA_S, "the self-test did not finish in time")
        report = self.report = launcher.selftest_report(self.host, self.url, mode="native")
        checks = report["checks"]
        try:
            loaded = self.window.events.loaded.wait(LOAD_TIMEOUT_S)
            report["window"] = {"loaded": loaded, "load_s": round(time.monotonic() - started, 2)}
            checks["window_loaded"] = loaded
            if loaded:
                report["page"] = page = self._page_info()
                checks["page_rendered"] = page.get("ready") == "complete" and page.get("path") == "/app"
                checks["page_has_content"] = (page.get("body_chars") or 0) > 0
            report["http"] = launcher.check_http(self.url)
            checks.update(launcher.http_checks(report["http"]))
            report["status_item"] = item = self._on_main(self._status_item_info) or {}
            checks["status_item"] = bool(item.get("created") and item.get("image"))
            report["delegate"] = patches = self._on_main(self._delegate_info) or {}
            patches["install_error"] = self.delegate_error
            checks["app_delegate_patched"] = bool(patches.get("patched") and patches.get("reopen")
                                                  and patches.get("will_terminate"))
            if report["frozen"]:  # the packaged app: its own whisper-server, not one from the build tree
                report["whisper_server_check"] = ws = launcher.whisper_server_check()
                checks["whisper_server_bundled"] = bool(ws.get("ok"))
            report["hide_on_close"] = hidden = self._check_hide_and_reopen()
            checks["hide_on_close_and_reopen"] = bool(hidden.get("ok"))
            launcher.write_phase(self.report_path, "window")
            time.sleep(SELFTEST_PAUSE_S)
            report["menu"] = self._check_menu()
            report["microphone"] = native_hooks.microphone_authorization()
            report["login_item"] = native_hooks.login_item_status()
            report["app_location"] = native_hooks.app_location_problem()
        except Exception as exc:  # noqa: BLE001
            log.exception("self-test failed")
            report["errors"].append(f"{type(exc).__name__}: {exc}")
        launcher.finish_report(report, started)
        launcher.write_report(self.report_path, report)
        self.exit_code = 0 if report["ok"] else 1
        time.sleep(max(0.0, (self.selftest or 0) - (time.monotonic() - started)))
        # quit like the menu does (it also works when our delegate isn't installed), with a deadline
        self._arm_watchdog(SELFTEST_QUIT_S, f"the app did not quit within {SELFTEST_QUIT_S:.0f} s")
        AppHelper.callAfter(self.quit)

    def _arm_watchdog(self, seconds: float, reason: str) -> None:
        """(Re)start the self-test's hard deadline: past it the process ends itself, report written."""
        if self._watchdog is not None:
            self._watchdog.cancel()
        self._watchdog = threading.Timer(seconds, self._selftest_hung, args=(reason,))
        self._watchdog.daemon = True
        self._watchdog.start()

    def _selftest_hung(self, reason: str = "the self-test did not finish in time") -> None:
        self.report.setdefault("errors", []).append(reason)
        self.report["ok"] = False
        if self._quit_info:
            self.report["quit"] = self._quit_info
        launcher.write_report(self.report_path, self.report)
        launcher.write_phase(self.report_path, "hung")
        os._exit(3)

    def _page_info(self) -> dict[str, Any]:
        script = (
            "JSON.stringify({title: document.title, ready: document.readyState, path: location.pathname,"
            " lang: document.documentElement.lang,"
            " body_chars: document.body ? document.body.innerText.length : 0})"
        )
        try:  # run_js, not evaluate_js: the latter wraps the code in eval(), which the page's CSP forbids
            return json.loads(str(_with_timeout(lambda: self.window.run_js(script), 15)))
        except Exception as exc:  # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"}

    def _status_item_info(self) -> dict[str, Any]:
        button = self._item.button() if self._item is not None else None
        return {
            "created": self._item is not None,
            "image": bool(button is not None and button.image() is not None),
            "symbol": self._symbol,
            "title": str(button.title()) if button is not None else None,
            "menu": [str(mi.title()) for mi in (self._menu.itemArray() if self._menu is not None else [])
                     if not mi.isSeparatorItem() and not mi.isHidden()],
        }

    def _delegate_info(self) -> dict[str, Any]:
        delegate = _app().delegate()
        if delegate is None:
            return {"delegate": None}
        return {
            "delegate": str(delegate.className()),
            "patched": bool(delegate.respondsToSelector_("voicestreamPatched")),
            "reopen": bool(delegate.respondsToSelector_("applicationShouldHandleReopen:hasVisibleWindows:")),
            "will_terminate": bool(delegate.respondsToSelector_("applicationWillTerminate:")),
        }

    def _window_info(self) -> dict[str, Any]:
        app = _app()
        return {
            "visible": bool(self.window.native.isVisible()),
            "policy": int(app.activationPolicy()),
            "app_hidden": bool(app.isHidden()),
        }

    def _check_hide_and_reopen(self) -> dict[str, Any]:
        """Close the window like its red button does, then reopen it like a Dock click does."""
        out: dict[str, Any] = {}
        out["close"] = self._on_main(lambda: self.window.native.performClose_(None))
        time.sleep(1.0)
        out["after_close"] = closed = self._on_main(self._window_info) or {}
        out["reopen"] = self._on_main(
            lambda: bool(_app().delegate().applicationShouldHandleReopen_hasVisibleWindows_(_app(), False))
        )
        time.sleep(1.0)
        out["after_reopen"] = shown = self._on_main(self._window_info) or {}
        out["ok"] = (
            closed.get("visible") is False and closed.get("policy") == POLICY_ACCESSORY
            and shown.get("visible") is True and shown.get("policy") == POLICY_REGULAR
        )
        return out

    def _check_menu(self) -> dict[str, Any]:
        """Open the status item's menu for CI's screenshot, then close it again."""
        from PyObjCTools import AppHelper

        before = self._menu_opened
        # performClick: tracks the menu in a nested run loop until it closes: don't wait for it
        AppHelper.callAfter(lambda: self._item.button().performClick_(None))
        time.sleep(1.0)
        launcher.write_phase(self.report_path, "menu")
        time.sleep(SELFTEST_PAUSE_S)
        self._on_main(lambda: self._menu.cancelTracking())
        return {"opened": self._menu_opened > before}
