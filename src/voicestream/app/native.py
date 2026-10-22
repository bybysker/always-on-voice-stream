"""The macOS shell around the app host: the window, the menu-bar icon, the floating button, links
and quitting.

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
  app again from Finder or Spotlight, shows the hidden window, its Start control focused.
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

The keyboard shortcut (⌥⌘R unless changed in Settings → General) starts and stops listening from any
app, window open or not: Carbon's hot key (:mod:`.hotkey`), registered on the main thread once the
app has launched and again whenever the preference changes, the result told to the host for the
Settings page, with what the keys type on the current keyboard layout (:func:`.hotkey.current_layout`:
the page can't tell with ⌥ held). A press is like a menu click (it only posts to the host); a short
sound says it worked — once a start is known to have worked, another one when it failed — and the
menu shows the same shortcut next to what a press does (Start/Stop Meeting, Resume while paused).

Meetings start when you start one (``prefs.meeting_start``, "manual" unless you chose "automatic"),
and every way of starting and stopping goes through the host's meeting actions
(``meeting_start``/``meeting_stop``/``meeting_toggle``): the window, the status item's menu, the
"Meeting" menu this shell adds to the app's menu bar, the shortcut, and

* the floating button (:mod:`.floating`): a small non-activating panel above every app, on every
  Space, that follows the host's state (Start meeting; the time, Pause, Stop; a call suggestion;
  "Still in a meeting?") and can be hidden from its own menu;
* ``voicestream://`` links (the Info.plist's CFBundleURLTypes): Launch Services sends them as a "get
  URL" Apple event, whose handler (registered before the run loop starts, for the link that launches
  the app) hands them to the host's ``open_url``, which checks the key of the ones that may record;
* the Spotlight launcher (:mod:`.spotlight`), "Voicestream – New Meeting" in ~/Applications, written
  while its setting is on, which opens the keyed ``meeting/start`` link;
* call suggestions (:mod:`.calls`): while nothing is recorded, a thread looks at whether another app
  captures from the microphone, and the host suggests taking notes — it never starts anything.
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

from . import calls, floating, hotkey, launcher, native_hooks, spotlight
from .floating import format_elapsed, meeting_started

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
SIG_URL_EVENT = b"v@:@@"  # (void)handleURLEvent:(NSAppleEventDescriptor *) withReplyEvent:(NSAppleEventDescriptor *)

# voicestream:// links reach the app as a "get URL" Apple event (CFBundleURLTypes in its Info.plist)
URL_EVENT = int.from_bytes(b"GURL", "big")
"""HIServices' InternetConfig.h: ``kInternetEventClass = 'GURL'`` and ``kAEGetURL = 'GURL'``, the event's class and id."""
DIRECT_OBJECT = int.from_bytes(b"----", "big")  # AE's AppleEvents.h keyDirectObject = '----': the link
AUTO_RETURN_ID, ANY_TRANSACTION = -1, 0  # AE's AEDataModel.h kAutoGenerateReturnID = -1, kAnyTransactionID = 0
SEND_NO_REPLY = 1  # NSAppleEventDescriptor.h NSAppleEventSendNoReply = kAENoReply (AEDataModel.h: 0x00000001)
URL_ANSWER_S = 5.0  # the self-test's wait for the host's answer to a link
SETTINGS_PAGE = "#/settings/general"
"""Settings… in the floating button's menu opens the window there (Settings → General → Starting meetings)."""

SOUNDS = {"start": "Tink", "restart": "Tink", "resume": "Tink", "stop": "Pop", "error": "Basso"}
"""What the keyboard shortcut plays (macOS's own sounds, /System/Library/Sounds) for what it did;
``error``: listening could not start (the menu bar says why)."""
HOTKEY_ANSWER_S = 5.0  # the self-test's wait for the host's answer to a press

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
    # menu items in title case (the macOS convention for menus in English), the state line as a sentence
    "en": {
        "listening": "Listening",
        "meeting": "Meeting in progress · {minutes} min",
        "paused": "Paused — nothing is being recorded",
        "stopped": "Not listening",
        "idle": "No meeting in progress",
        "starting": "Starting…",
        "stopping": "Stopping…",
        "saving": "Saving the meeting…",
        "error": "Problem: {error}",
        "quitting": "Quitting — saving your meeting…",
        "open": "Open Voicestream",
        "pause": "Pause",
        "resume": "Resume Listening",
        "resume_meeting": "Resume Meeting",
        "start": "Start Listening",
        "stop": "Stop Listening",
        "start_meeting": "Start Meeting",
        "stop_meeting": "Stop Meeting",
        "end_meeting": "End Meeting Now",
        "show_floating": "Show Floating Button",
        "hide_floating": "Hide Floating Button",
        "meeting_menu": "Meeting",
        "login": "Open at Login",
        "login_approval": "Open at Login (Allow in System Settings)",
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
        "idle": "Aucune réunion en cours",
        "starting": "Démarrage…",
        "stopping": "Arrêt…",
        # the window's words: « Enregistrement » is the app's French for Recording, wrong just after Stop
        "saving": "Finalisation de la réunion…",
        "error": "Problème : {error}",
        "quitting": "Fermeture — finalisation de la réunion…",
        "open": "Ouvrir Voicestream",
        "pause": "Mettre en pause",
        "resume": "Reprendre l'écoute",
        "resume_meeting": "Reprendre la réunion",
        "start": "Démarrer l'écoute",
        "stop": "Arrêter l'écoute",
        "start_meeting": "Démarrer une réunion",
        "stop_meeting": "Arrêter la réunion",
        "end_meeting": "Terminer la réunion",
        "show_floating": "Afficher le bouton flottant",
        "hide_floating": "Masquer le bouton flottant",
        "meeting_menu": "Réunion",
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
    modifiers: int = hotkey.APPKIT_MODIFIERS["cmd"]  # NSMenuItem's own default: ⌘

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
    if is_manual(state) and current in ("stopped", "stopping"):
        return s["idle" if current == "stopped" else "saving"]  # nothing is listened to between meetings
    return s.get(current, s["stopped"])


def is_manual(state: dict[str, Any]) -> bool:
    """Meetings start when you start one (the default), rather than whenever someone speaks."""
    return floating.meeting_mode(state) == "manual"


def hotkey_wanted(state: dict[str, Any], *, quitting: bool = False) -> tuple[str, tuple[str, ...]] | None:
    """``(code, mods)`` of the shortcut to register for this host state, or ``None``: turned off, Settings
    recording a new one (pressing the current one must reach the page), or quitting."""
    hk = (state.get("prefs") or {}).get("hotkey")
    if quitting or not hk or (state.get("hotkey") or {}).get("recording"):
        return None
    return str(hk["code"]), tuple(hotkey.normalize_mods(hk.get("mods") or ()))


def listen_shortcut(state: dict[str, Any]) -> tuple[str, int]:
    """The key equivalent and modifier mask shown in the menu next to what a press does
    (:func:`shortcut_entry`): the keyboard shortcut, once macOS took it (nothing otherwise: the menu
    would teach a shortcut that does nothing)."""
    hk = (state.get("prefs") or {}).get("hotkey")
    if not hk or (state.get("hotkey") or {}).get("status") != "ok":
        return "", 0
    return hotkey.key_equivalent(str(hk["code"]), str(hk.get("key") or "")), hotkey.appkit_mask(hk.get("mods") or ())


def shortcut_entry(state: dict[str, Any]) -> str:
    """The menu entry the keyboard shortcut is shown next to: the one that does what a press does now
    (:data:`.hotkey.TOGGLE`) — "Resume listening" while paused (a press resumes: next to "Stop
    listening" it would be read as a stop), else Start/Stop listening."""
    return "pause" if hotkey.TOGGLE.get(str(state.get("state") or "stopped")) == "resume" else "listen"


def _floating_entry(key: str, state: dict[str, Any], lang: str, *, quitting: bool) -> MenuEntry:
    """Show / Hide Floating Button (``prefs.floating_button``), where the shell has one and the setup is done."""
    s = STRINGS.get(lang, STRINGS["en"])
    on = bool((state.get("prefs") or {}).get("floating_button", True))
    there = bool((state.get("capabilities") or {}).get("floating_button")) and bool(state.get("setup_done"))
    return MenuEntry(key, s["hide_floating"] if on else s["show_floating"], enabled=not quitting, visible=there)


def menu_model(state: dict[str, Any], lang: str, login: str, *, now: datetime | None = None,
               quitting: bool = False) -> list[MenuEntry]:
    """The status item's menu for this state, always the same keys in the same order (the native
    menu is built once and updated in place). Start/Stop Meeting when meetings start by hand (End
    Meeting Now would be Stop again), Start/Stop Listening and End Meeting Now when automatically."""
    s = STRINGS.get(lang, STRINGS["en"])
    now = now or datetime.now(UTC)
    current = str(state.get("state") or "stopped")
    manual = is_manual(state)
    busy = quitting or current in ("starting", "stopping")
    stopped = current in ("stopped", "error")
    login_on = login in ("enabled", "requires_approval")
    key, mods = listen_shortcut(state)
    shortcut: dict[str, Any] = {"shortcut": key, "modifiers": mods}
    on_pause, on_listen = (shortcut, {}) if shortcut_entry(state) == "pause" else ({}, shortcut)
    start, stop = (s["start_meeting"], s["stop_meeting"]) if manual else (s["start"], s["stop"])
    resume = s["resume_meeting"] if manual else s["resume"]
    floating_item = _floating_entry("floating", state, lang, quitting=quitting)
    return [
        MenuEntry("status", state_line(state, lang, now, quitting=quitting), enabled=False),
        MenuEntry("sep1"),
        MenuEntry("open", s["open"], enabled=not quitting),
        MenuEntry("sep2"),
        MenuEntry("pause", resume if current == "paused" else s["pause"], enabled=not busy,
                  visible=current in ("listening", "paused"), **on_pause),
        # Stop may cancel a start still under way (a speech model downloading), as the host allows
        MenuEntry("listen", start if stopped else stop, enabled=not quitting and current != "stopping", **on_listen),
        MenuEntry("end_meeting", s["end_meeting"], enabled=not busy, visible=bool(state.get("in_meeting")) and not manual),
        MenuEntry("sep3", visible=login != "unsupported" or floating_item.visible),
        floating_item,
        MenuEntry("login", s["login_approval"] if login == "requires_approval" else s["login"], enabled=not quitting,
                  visible=login != "unsupported", checked=login_on),
        MenuEntry("sep4"),
        MenuEntry("quit", s["quit"], enabled=not quitting, shortcut="q"),
    ]


def meeting_menu_model(state: dict[str, Any], lang: str, *, quitting: bool = False) -> list[MenuEntry]:
    """The "Meeting" menu of the app's menu bar (shown while the window is in front): the same commands as
    the status item's, each one always there and disabled when it can't be used (the convention for the
    menu bar), the keyboard shortcut next to the one a press does now."""
    s = STRINGS.get(lang, STRINGS["en"])
    current = str(state.get("state") or "stopped")
    manual = is_manual(state)
    press = hotkey.TOGGLE.get(current)
    key, mods = listen_shortcut(state)

    def shortcut(on: bool) -> dict[str, Any]:
        return {"shortcut": key, "modifiers": mods} if on else {}

    recording = current in ("listening", "paused")
    return [
        MenuEntry("m_start", s["start_meeting"] if manual else s["start"], enabled=not quitting and current in ("stopped", "error"),
                  **shortcut(press in ("start", "restart"))),
        MenuEntry("m_stop", s["stop_meeting"] if manual else s["stop"],
                  enabled=not quitting and current in ("starting", "listening", "paused"), **shortcut(press == "stop")),
        MenuEntry("m_pause", (s["resume_meeting"] if manual else s["resume"]) if current == "paused" else s["pause"],
                  enabled=not quitting and recording, **shortcut(press == "resume")),
        MenuEntry("m_end", s["end_meeting"], enabled=not quitting and recording and bool(state.get("in_meeting")),
                  visible=not manual),
        MenuEntry("sep_m"),
        _floating_entry("m_floating", state, lang, quitting=quitting),
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
                    _guard(shell.reopened)
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

            @objc.typedSelector(SIG_URL_EVENT)
            def handleURLEvent_withReplyEvent_(self, event, reply):
                _guard(lambda: self.shell.on_url_event(event))

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
        caps["hotkey"] = hotkey.available()
        caps["floating_button"] = True
        # the launcher opens a voicestream:// link, which only the packaged app (with its Info.plist) handles
        caps["spotlight"] = native_hooks.bundle_path() is not None
        caps["call_detection"] = calls.available()
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


def call_name(call: calls.Call) -> str | None:
    """Main thread: the app a call is in, for "Call detected" (:func:`.calls.name_call`, with what NSWorkspace
    says: the process's name, else the frontmost and running apps)."""
    pid_name = front = None
    running: list[str] = []
    try:
        import AppKit

        if not call.app and call.pid is not None:
            app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(call.pid)
            pid_name = str(app.localizedName() or "") if app is not None else None
        elif not call.app:
            ws = AppKit.NSWorkspace.sharedWorkspace()
            app = ws.frontmostApplication()
            front = str(app.bundleIdentifier() or "") if app is not None else None
            running = calls.running_bundle_ids(ws.runningApplications() or [])
    except Exception:  # the suggestion just names no app
        log.debug("could not name the app of a call", exc_info=True)
    return calls.name_call(call, pid_name, front, running)


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
        self._launched = False  # the app finished launching: Carbon calls may be made
        self._hotkeys: hotkey.CarbonHotKey | None = None  # kept for the life of the app (Carbon holds its callback)
        self._hotkey_tried: tuple[str, tuple[str, ...]] | None = None  # what was last registered (or tried)
        self._hotkey_status: dict[str, Any] | None = None  # what macOS said of it: told to the host
        self._hotkey_err: int | None = None  # RegisterEventHotKey's OSStatus, for the self-test
        self._layout: dict[str, str] | None = None  # what the keys type on the keyboard layout, as last read
        self._recording = False  # Settings was recording a shortcut at the last state
        self._probe: list[Any] | None = None  # the self-test's press: the host's answers, nothing done
        self._main_menu: tuple[Any, Any] | None = None  # the "Meeting" menu in the app's menu bar (its item, the menu)
        self._main_entries: dict[str, Any] = {}
        self._floating: floating.FloatingPanel | None = None
        self._floating_test: str | None = None  # the self-test shows this sample on it, whatever the state says
        self._watcher: calls.CallWatcher | None = None
        self._recorded = False  # something was recorded since the call watcher last started
        self._spotlight: tuple[Any, ...] | None = None  # what the Spotlight launcher was last made for
        self._url_handler: str | None = None  # "registered", or why the voicestream:// handler is not
        self._urls = 0  # links received (the self-test's check)
        self._url_probe: list[Any] | None = None  # the self-test's links: the host's answers, nothing done

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
        self._register_url_handler()  # before the run loop starts: a link that launched the app is its first event
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

    def _register_url_handler(self) -> None:
        """Hear ``voicestream://`` links: the "get URL" Apple event (NSAppleEventManager.h
        ``setEventHandler:andSelector:forEventClass:andEventID:``), which Launch Services sends when a link
        is opened — by the Spotlight launcher, a Shortcut, a link clicked somewhere — whether the app was
        running or this is its launch. Registered before the run loop starts, as Apple's documentation asks
        (applicationWillFinishLaunching: at the latest): the event that launched the app comes first."""
        try:
            import Foundation

            Foundation.NSAppleEventManager.sharedAppleEventManager().setEventHandler_andSelector_forEventClass_andEventID_(
                self._menu_target(), "handleURLEvent:withReplyEvent:", URL_EVENT, URL_EVENT)
            self._url_handler = "registered"
        except Exception as exc:  # links do nothing; everything else works (the self-test says so)
            log.exception("could not listen for voicestream:// links")
            self._url_handler = f"{type(exc).__name__}: {exc}"

    def _start_menu_bar(self) -> None:
        try:
            self._create_status_item()
        except Exception:  # noqa: BLE001 - the window still works without it
            log.exception("could not create the menu-bar icon")
        try:
            self._create_meeting_menu()
        except Exception:  # the status item's menu has the same commands
            log.exception("could not add the Meeting menu")
        self._launched = True  # the keyboard shortcut is registered by the apply_state below
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
        menu = self._new_menu("Voicestream", menu_model(self.state, self.lang, self._login), self._entries)
        menu.setDelegate_(self._menu_target())
        item.setMenu_(menu)
        self._item, self._menu = item, menu

    def _new_menu(self, title: str, entries: Iterable[MenuEntry], store: dict[str, Any]) -> Any:
        """An NSMenu of these entries, each one kept in ``store`` by its key to be updated in place."""
        import AppKit

        target = self._menu_target()
        menu = AppKit.NSMenu.alloc().initWithTitle_(title)
        menu.setAutoenablesItems_(False)
        for entry in entries:
            if entry.separator:
                mi = AppKit.NSMenuItem.separatorItem()
            else:
                action = None if entry.key == "status" else "menuAction:"
                mi = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(entry.title, action, entry.shortcut)
                mi.setKeyEquivalentModifierMask_(entry.modifiers)
                mi.setTarget_(target)
                mi.setRepresentedObject_(entry.key)
            menu.addItem_(mi)
            store[entry.key] = mi
        return menu

    def _create_meeting_menu(self) -> None:
        """The "Meeting" menu in the app's menu bar, after pywebview's own (which builds the menu bar once, before
        the run loop starts, and builds it again only for a window given menus of its own: ours has none)."""
        import AppKit

        main = _app().mainMenu()
        if main is None:
            return
        title = STRINGS[self.lang]["meeting_menu"]
        menu = self._new_menu(title, meeting_menu_model(self.state, self.lang), self._main_entries)
        holder = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
        holder.setSubmenu_(menu)
        main.addItem_(holder)
        self._main_menu = (holder, menu)

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
        for step in (self._update_activity, self._sync_hotkey, self._sync_layout, self.render, self._update_timer,
                     self._sync_floating, self._sync_calls, self._sync_spotlight):
            try:
                step()
            except Exception:  # noqa: BLE001
                log.exception("updating the menu bar failed")

    def _on_host_state(self, state: dict[str, Any]) -> None:
        """Host loop thread: hand the state over to the main thread."""
        from PyObjCTools import AppHelper

        AppHelper.callAfter(self.apply_state, dict(state or {}))

    def render(self) -> None:
        """The menu-bar icon and its menu, the Meeting menu and the floating button's timer (each second
        while a meeting is recorded, see :meth:`_update_timer`)."""
        now = datetime.now(UTC)
        if self._item is not None:
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
            self._update_entries(self._entries, menu_model(self.state, self.lang, self._login, now=now,
                                                           quitting=self.quitting))
        self._update_entries(self._main_entries, meeting_menu_model(self.state, self.lang, quitting=self.quitting))
        if self._main_menu is not None:  # the language may have changed in Settings
            for part in self._main_menu:
                part.setTitle_(STRINGS[self.lang]["meeting_menu"])
        if self._floating is not None and self._floating.model is not None and self._floating_test is None:
            self._floating.tick(self._panel_model(now))

    @staticmethod
    def _update_entries(store: dict[str, Any], entries: Iterable[MenuEntry]) -> None:
        for entry in entries:
            mi = store.get(entry.key)
            if mi is None:
                continue
            if not entry.separator:
                mi.setTitle_(entry.title)
                mi.setEnabled_(entry.enabled)
                mi.setState_(CONTROL_ON if entry.checked else CONTROL_OFF)
                mi.setKeyEquivalent_(entry.shortcut)  # Start/Stop's follows the keyboard shortcut
                mi.setKeyEquivalentModifierMask_(entry.modifiers)
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

    # ---------------------------------------------------------- keyboard shortcut

    def _hotkey_object(self) -> hotkey.CarbonHotKey | None:
        if self._hotkeys is None:
            try:
                self._hotkeys = hotkey.CarbonHotKey(self.on_hotkey)
            except OSError as exc:  # no Carbon: the capability said so already, Settings shows it
                log.warning("no keyboard shortcut: %s", exc)
        return self._hotkeys

    def _sync_hotkey(self) -> None:
        """Main thread, at each host state: register the shortcut the preferences name (again when they
        change; none while Settings records a new one), then tell the host what macOS said, until the
        host's state says it too. A combination macOS refused is tried again when Settings saves one
        (the host's status is then "pending"), not at every state."""
        caps = self.state.get("capabilities") or {}
        if not self._launched or not caps.get("hotkey"):
            return
        hk = self._hotkey_object()
        if hk is None:
            return
        want = hotkey_wanted(self.state, quitting=self.quitting)
        reported = {k: (self.state.get("hotkey") or {}).get(k) for k in ("status", "error")}
        mine = self._hotkey_status or {}
        retry = reported["status"] == "pending" and mine.get("status") in ("taken", "failed")
        if self._hotkey_status is None or want != self._hotkey_tried or retry:
            self._hotkey_tried = want
            if want is None:
                hk.unregister()
                recording = (self.state.get("hotkey") or {}).get("recording")
                self._hotkey_status = {"status": "paused" if recording else "off", "error": None}
            else:
                self._hotkey_err = err = hk.register(*want)
                self._hotkey_status = {"status": hotkey.status_of(err), "error": err or None}
                if err:
                    log.warning("macOS refused the keyboard shortcut %s (error %s)", hotkey.describe(*want), err)
        if reported != self._hotkey_status and not self.quitting:  # quitting: Settings has nothing to show
            self._report_hotkey(dict(self._hotkey_status))

    def _report_hotkey(self, status: dict[str, Any]) -> None:
        async def report() -> None:
            setter = getattr(self.host, "set_hotkey_status", None)
            if setter is not None:
                setter(status["status"], status["error"])

        self._post(report())

    def _sync_layout(self) -> None:
        """Main thread, at each host state: tell the host what the keys type on the keyboard layout in use
        (:func:`.hotkey.current_layout`), which Settings shows and checks a shortcut by — with ⌥ held the
        page only gets the character ⌥ types. Read at launch, and again each time Settings starts recording
        a shortcut: the layout may have been switched since."""
        caps = self.state.get("capabilities") or {}
        if not self._launched or not caps.get("hotkey") or self.quitting:
            return
        recording = bool((self.state.get("hotkey") or {}).get("recording"))
        if self._layout is None or (recording and not self._recording):
            try:
                self._layout = hotkey.current_layout()[1]
            except Exception as exc:  # noqa: BLE001 - Settings then goes by what the page knows
                log.warning("could not read the keyboard layout: %s", exc)
                self._layout = {}
        self._recording = recording
        if self._layout != (self.state.get("keyboard_layout") or {}):
            layout = dict(self._layout)

            async def report() -> None:
                setter = getattr(self.host, "set_keyboard_layout", None)
                if setter is not None:
                    setter(layout)

            self._post(report())

    def on_hotkey(self) -> None:
        """Main thread (Carbon's handler): the shortcut was pressed. Like a menu click it only posts to the
        host; the sound plays once the host has said what the press did (a start: once it is up, or failed)."""
        if self._probe is not None:  # the self-test's press: ask the host what it would do, change nothing
            try:
                self._probe.append(self.host.call(self._ask_toggle()))
            except RuntimeError:
                log.exception("the app host is not running")
            return
        self._post(self._toggle())

    async def _toggle(self) -> None:
        """On the host's loop. A stop is said at once (its wrap-up takes a while); a start, restart or
        resume once the host knows how it went — ``error`` when it failed, with its own sound: the
        "it worked" one must not play for a start that went to the error state."""
        from PyObjCTools import AppHelper

        action = await self.host.meeting_toggle()
        if action is not None:
            AppHelper.callAfter(_guard, lambda: self._feedback(action))

    async def _ask_toggle(self) -> str | None:
        ask = getattr(self.host, "toggle_action", None)
        return ask() if ask is not None else None

    def _feedback(self, action: str) -> None:
        """Main thread: a short sound, so you know the shortcut worked — or not — without looking (the
        menu-bar icon changes too). Settings → General can turn it off."""
        if not (self.state.get("prefs") or {}).get("hotkey_sound", True):
            return
        import AppKit

        sound = AppKit.NSSound.soundNamed_(SOUNDS[action])
        if sound is not None:
            sound.play()

    # --------------------------------------------------------------- menu actions

    def on_menu(self, key: str) -> None:
        """Main thread. Only posts work to the host loop: never waits for it. Starting and stopping go through
        the host's meeting actions, as the window, the shortcut, the floating button and links do (after a
        problem, Start is a restart there: what failed may have left half the assistant running)."""
        current = self.state.get("state")
        if key == "open":
            self.reopened()
        elif key in ("pause", "m_pause"):
            self._post(self.host.listen_resume() if current == "paused" else self.host.listen_pause())
        elif key == "listen":
            self._post(self.host.meeting_start() if current in ("stopped", "error") else self.host.meeting_stop())
        elif key == "m_start":
            self._post(self.host.meeting_start())
        elif key == "m_stop":
            self._post(self.host.meeting_stop())
        elif key in ("end_meeting", "m_end"):
            self._post(self._end_meeting())
        elif key in ("floating", "m_floating"):
            on = bool((self.state.get("prefs") or {}).get("floating_button", True))
            self._post(self._on_loop("update_prefs", {"floating_button": not on}))
        elif key == "login":
            self._toggle_login()
        elif key == "quit":
            self.quit()

    def reopened(self) -> None:
        """Main thread: Voicestream was opened again (the Dock, Finder, Spotlight, Open Voicestream): the window,
        with the keyboard focus on its Start control (the host tells the page)."""
        self.show_window()
        self._post(self._on_loop("request_focus", "start"))

    async def _on_loop(self, name: str, *args: Any) -> Any:
        """Call one of the host's plain methods on its loop, as they ask (``suggestion_dismiss``, ``update_prefs``…);
        a host without it does nothing."""
        fn = getattr(self.host, name, None)
        return fn(*args) if fn is not None else None

    # ----------------------------------------------------------- floating button

    def _panel_model(self, now: datetime | None = None) -> floating.PanelModel:
        state = floating.sample_states()[self._floating_test] if self._floating_test else self.state
        hk = (self.state.get("prefs") or {}).get("hotkey")
        ok = bool(hk) and (self.state.get("hotkey") or {}).get("status") == "ok"
        shortcut = hotkey.describe(str(hk["code"]), hk.get("mods") or (), str(hk.get("key") or "")) if ok else ""
        return floating.panel_model(state, self.lang, now, shortcut=shortcut)

    def _sync_floating(self) -> None:
        """Main thread, at each host state: show the floating button as the state says (or hide it)."""
        if not self._launched:
            return
        want = self._floating_test is not None or floating.wanted(self.state, quitting=self.quitting)
        if not want and self._floating is None:
            return
        if self._floating is None:
            self._floating = floating.FloatingPanel(on_action=self.on_floating, on_moved=self._floating_moved)
        saved = (self.state.get("prefs") or {}).get("floating_position")
        self._floating.update(self._panel_model() if want else None, saved=saved, lang=self.lang)

    def on_floating(self, key: str) -> None:
        """Main thread: a click on the floating button (or its context menu). Only posts to the host, through the
        meeting actions every other way of starting and stopping uses."""
        if key in ("start", "retry"):
            self._post(self.host.meeting_start())
        elif key == "pause":
            self._post(self.host.listen_pause())
        elif key == "resume":
            self._post(self.host.listen_resume())
        elif key == "stop":
            self._post(self.host.meeting_stop())
        elif key == "accept":
            self._post(self.host.suggestion_accept())
        elif key == "dismiss":
            self._post(self._on_loop("suggestion_dismiss"))
        elif key == "keep":
            self._post(self.host.meeting_keep())
        elif key == "end":
            self._post(self._end_meeting())
        elif key == "ok":
            self._post(self._on_loop("dismiss_notice"))
        elif key == "show":
            self.show_window()
        elif key == "open":
            self.reopened()
        elif key == "settings":
            self.show_settings()
        elif key == "hide":
            self._post(self._on_loop("update_prefs", {"floating_button": False}))

    def _floating_moved(self, position: dict[str, Any]) -> None:
        """Main thread: you dragged the floating button somewhere: remembered for the next launch."""
        self._post(self._on_loop("update_prefs", {"floating_position": position}))

    def show_settings(self) -> None:
        """Main thread: the window, on Settings → General (where Starting meetings is). The page is told from
        another thread: pywebview's run_js waits for the page, which needs this one."""
        self.show_window()
        window = self.window

        def go() -> None:
            try:
                _with_timeout(lambda: window.run_js(f"location.hash = {json.dumps(SETTINGS_PAGE)}"), MAIN_THREAD_TIMEOUT_S)
            except (TimeoutError, RuntimeError) as exc:  # the window is open all the same
                log.warning("could not open Settings: %s", exc)

        threading.Thread(target=go, name="voicestream-settings", daemon=True).start()

    # ----------------------------------------------------------- call suggestions

    def _sync_calls(self) -> None:
        """Main thread, at each host state: look for calls only while nothing is recorded, suggestions are on and
        the setup is done (the host suggests nothing otherwise anyway)."""
        if not self._launched:
            return
        current = str(self.state.get("state") or "stopped")
        prefs = self.state.get("prefs") or {}
        caps = self.state.get("capabilities") or {}
        if current in ("starting", "listening", "paused", "stopping"):
            self._recorded = True
        want = (not self.quitting and bool(caps.get("call_detection")) and bool(prefs.get("call_suggestions", True))
                and bool(self.state.get("setup_done")) and current in ("stopped", "error"))
        if want and (self._watcher is None or not self._watcher.running):
            if self._watcher is None:
                self._watcher = calls.CallWatcher(on_start=self._call_seen, on_end=self._call_gone)
            # the watcher is the memory of the calls under way from now on: the host forgets those it knew
            self._post(self._on_loop("call_ended"))
            self._watcher.start(baseline=self._recorded)  # the call just recorded is not a new one
            self._recorded = False
        elif not want and self._watcher is not None and self._watcher.running:
            self._watcher.stop()

    def _call_seen(self, call: calls.Call) -> None:
        """The watcher's thread: a call started. Named on the main thread (NSWorkspace), then suggested."""
        from PyObjCTools import AppHelper

        AppHelper.callAfter(_guard, lambda: self._suggest(call))

    def _call_gone(self, call: calls.Call) -> None:
        from PyObjCTools import AppHelper

        AppHelper.callAfter(_guard, lambda: self._post(self._on_loop("call_ended", call.app, call.key)))

    def _suggest(self, call: calls.Call) -> None:
        if self._watcher is None or not self._watcher.running:
            return  # recording started meanwhile
        self._post(self._on_loop("suggest_meeting", call_name(call), call.key))

    # ----------------------------------------------------------- Spotlight launcher

    def _sync_spotlight(self) -> None:
        """Main thread, at each host state: keep "Voicestream – New Meeting" in ~/Applications as the preference
        says, once the setup is done (it is one of its questions). Made again when the language or the app's
        place changes, and at each launch (the app may have moved, or been replaced by a new version)."""
        if not self._launched or self.quitting:
            return
        caps = self.state.get("capabilities") or {}
        if not caps.get("spotlight") or not self.state.get("setup_done"):
            return
        on = bool((self.state.get("prefs") or {}).get("spotlight_launcher", True))
        app = native_hooks.bundle_path()
        if app is None:  # a terminal install: no Info.plist, so nothing would open the launcher's link
            return
        wanted = (on, self.lang, str(app))
        if wanted == self._spotlight:
            return
        self._spotlight = wanted
        self._post(self._write_launcher(on, self.lang, app))

    async def _write_launcher(self, on: bool, lang: str, app: Path | None) -> str:
        """On the host's loop (the link's key is made and saved there), the files in a worker thread."""
        import asyncio

        from .. import __version__

        made = None
        if on:
            get = getattr(self.host, "launcher_url", None)
            if get is None:
                return "unsupported"
            made = spotlight.Launcher(url=get(), lang=lang, app=app, icon=spotlight.app_icon(app), version=__version__)
        try:
            result = await asyncio.to_thread(spotlight.sync, spotlight.applications_dir(), made)
        except OSError as exc:
            log.warning("could not update the Spotlight launcher in ~/Applications: %s", exc)
            return "failed"
        log.info("Spotlight launcher: %s", result)
        return result

    # ----------------------------------------------------------- voicestream:// links

    def on_url_event(self, event: Any) -> None:
        """Main thread (the Apple event handler): a ``voicestream://`` link was opened."""
        param = event.paramDescriptorForKeyword_(DIRECT_OBJECT)
        url = str(param.stringValue() or "") if param is not None else ""
        self.open_url(url)

    def open_url(self, url: str) -> None:
        """Main thread. Only posts to the host, which checks the link's key (never logged here); the outcome is
        said like a press of the shortcut: a sound, the window for ``voicestream://open``."""
        self._urls += 1
        if self._url_probe is not None:  # the self-test's link: ask the host what it would do, change nothing
            try:
                self._url_probe.append(self.host.call(self._ask_url(url)))
            except RuntimeError:
                log.exception("the app host is not running")
            return
        self._post(self._follow(url))

    async def _follow(self, url: str) -> None:
        from PyObjCTools import AppHelper

        follow = getattr(self.host, "open_url", None)
        outcome = await follow(url) if follow is not None else "unknown"
        AppHelper.callAfter(_guard, lambda: self._url_done(str(outcome)))

    def _url_done(self, outcome: str) -> None:
        """Main thread: what a link did. The meeting actions sound as the shortcut's do (a link from Spotlight
        leaves you in the app you were in, so the sound and the floating button are the feedback); a link
        refused for its key sounds as a failure (the floating button and the window say why)."""
        if outcome == "open":
            self.reopened()
        elif outcome in SOUNDS:
            self._feedback(outcome)
        elif outcome == "refused":
            self._feedback("error")

    async def _ask_url(self, url: str) -> dict[str, Any]:
        """The self-test's link, on the loop: what it names, whether it carries the launcher's key (compared as
        the host does, in constant time) and what the host would do — without doing it."""
        import hmac

        from .host import parse_url

        parsed = parse_url(url)
        if parsed is None:
            return {"action": None, "key_ok": False, "would": None}
        action, given = parsed
        own = parse_url(self.host.launcher_url())
        key = own[1] if own is not None else None
        ok = bool(given and key) and hmac.compare_digest(str(given).encode(), str(key).encode())
        ask = getattr(self.host, "meeting_action", None)
        return {"action": action, "key_ok": ok, "would": ask(action) if ok and ask is not None else None}

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
        for step in (self.render, self._sync_floating, self._sync_calls):  # the floating button goes, calls aren't watched
            with contextlib.suppress(Exception):
                step()
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
        if self._hotkeys is not None:
            with contextlib.suppress(Exception):  # macOS drops it with the process anyway
                self._hotkeys.close()
        if self._watcher is not None:
            self._watcher.stop()
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
            report["hotkey"] = shortcut = self._check_hotkey()
            checks["hotkey_registered"] = shortcut.get("register_result") == hotkey.NO_ERR and bool(shortcut.get("registered"))
            checks["hotkey_reaches_host"] = bool(shortcut.get("reaches_host"))
            checks["hotkey_in_menu"] = bool(shortcut.get("in_menu"))
            report["keyboard_layout"] = layout = self._on_main(self._layout_info) or {}
            checks["keyboard_layout"] = (bool(layout.get("keys")) and layout.get("problem") is None
                                         and bool(layout.get("told_host")))
            report["floating"] = panel = self._check_floating()
            checks["floating_panel"] = bool(panel.get("ok"))
            checks["floating_panel_states"] = bool(panel.get("states_ok"))
            report["url"] = link = self._check_url()
            checks["url_handler"] = bool(link.get("registered")) and (bool(link.get("scheme_in_plist"))
                                                                      or not report["frozen"])
            checks["url_reaches_host"] = bool(link.get("reaches_host"))
            report["spotlight"] = launch = self._check_spotlight(frozen=bool(report["frozen"]))
            checks["spotlight_launcher"] = bool(launch.get("ok"))
            report["mic_probe"] = mic = self._check_mic_probe()
            checks["mic_probe"] = bool(mic.get("ok"))
            launcher.write_phase(self.report_path, "window")
            time.sleep(SELFTEST_PAUSE_S)
            # the floating button as a call shows it, for CI's screenshot (then back to what the state says)
            self._on_main(lambda: self._show_sample("suggestion"))
            launcher.write_phase(self.report_path, "floating")
            time.sleep(SELFTEST_PAUSE_S)
            self._on_main(lambda: self._show_sample(None))
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

    def _check_hotkey(self) -> dict[str, Any]:
        """The keyboard shortcut as registered at launch (RegisterEventHotKey's answer, the menu showing
        it), then a press as macOS delivers one — a kEventHotKeyPressed event with our EventHotKeyID, sent
        to the application target, where our handler is — and the host's answer to it. The host is only
        asked what the press would do: the self-test never starts listening (CI can't answer permission
        prompts; on your own Mac it would record). Turned off in the preferences, the default shortcut is
        registered for the test, then removed."""
        out: dict[str, Any] = {"available": bool((self.state.get("capabilities") or {}).get("hotkey"))}
        out["temporary"] = temporary = self._on_main(self._register_for_test) is True
        out.update(self._on_main(self._hotkey_info) or {})
        before = launcher.safe_state(self.host).get("state")
        answers: list[Any] = []
        self._probe = answers
        try:
            presses = self._hotkeys.presses if self._hotkeys is not None else 0
            out["send_result"] = self._on_main(lambda: self._hotkeys.send_test_press() if self._hotkeys else None)
            out["handled"] = (self._hotkeys.presses if self._hotkeys is not None else 0) - presses
            out["host_answer"] = answers[0].result(HOTKEY_ANSWER_S) if answers else None
        except Exception as exc:  # noqa: BLE001 - in the report
            out["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            self._probe = None
            if temporary:
                self._on_main(lambda: self._hotkeys.unregister() if self._hotkeys else None)
        after = launcher.safe_state(self.host).get("state")
        out.update(state_before=before, state_after=after, expected=hotkey.TOGGLE.get(str(before)))
        out["reaches_host"] = (out.get("send_result") == hotkey.NO_ERR and out.get("handled") == 1
                               and out["expected"] is not None and out.get("host_answer") == out["expected"]
                               and after == before)  # nothing was started or stopped
        return out

    def _register_for_test(self) -> bool:
        """Main thread: the shortcut is turned off — register the default one for the self-test."""
        hk = self._hotkeys
        if hk is None or hk.registered is not None or (self._hotkey_status or {}).get("status") != "off":
            return False
        self._hotkey_err = hk.register(hotkey.DEFAULT_CODE, hotkey.DEFAULT_MODS)
        return True

    def _hotkey_info(self) -> dict[str, Any]:
        hk = self._hotkeys
        entry = shortcut_entry(self.state)
        item = self._entries.get(entry)
        key, mods = listen_shortcut(self.state)
        shown = (str(item.keyEquivalent()), int(item.keyEquivalentModifierMask())) if item is not None else None
        wanted = (self.state.get("prefs") or {}).get("hotkey") is not None
        return {
            "registered": hotkey.describe(*hk.registered) if hk is not None and hk.registered else None,
            "register_result": self._hotkey_err,
            "status": dict(self._hotkey_status or {}),
            "host_status": (self.state.get("hotkey") or {}).get("status"),
            "wanted": wanted,
            "menu": {"entry": entry, "key": shown[0], "mask": shown[1]} if shown else None,
            # the menu shows what the host's state says macOS took — and shows something when a shortcut
            # is set: a status that never reached the host leaves the menu (and Settings) without it
            "in_menu": (shown is not None and shown[0] == key and (not key or shown[1] == mods)
                        and (bool(key) or not wanted)),
        }

    def _layout_info(self) -> dict[str, Any]:
        """Main thread: the keyboard layout as macOS gives it (Settings shows a shortcut recorded with ⌥
        held by it), whether it looks like one, and whether the host was told it."""
        ident, keys = hotkey.current_layout()  # an OSError goes in the report (_on_main)
        return {"id": ident, "keys": keys, "problem": hotkey.layout_problem(ident, keys),
                "told_host": keys == (self.state.get("keyboard_layout") or {})}

    def _show_sample(self, name: str | None) -> None:
        """Main thread: show the floating button with one of :func:`.floating.sample_states` (``None``: as the
        state says again, hidden if it says so)."""
        self._floating_test = name
        self._sync_floating()

    def _focus_info(self) -> dict[str, Any]:
        """Main thread: whether Voicestream is the active app, and which window has the keyboard."""
        app = _app()
        key = app.keyWindow()
        return {"active": bool(app.isActive()), "key_window": int(key.windowNumber()) if key is not None else None}

    def _draw_samples(self) -> dict[str, Any]:
        """Main thread: draw every look of the floating button once, as the real panel draws it."""
        out: dict[str, Any] = {}
        for name in floating.sample_states():
            self._show_sample(name)
            panel = self._floating
            if panel is None or panel.model is None:
                out[name] = {"drawn": False}
                continue
            drawn = panel.drawn()
            heights = [b["height"] for b in drawn["buttons"]]
            out[name] = {"drawn": True, "kind": panel.model.kind, "size": [round(v) for v in drawn["size"]],
                         "buttons": len(heights), "expected_buttons": len(panel.model.buttons),
                         "min_button_height": min(heights) if heights else None,
                         "labelled": all(b["label"] for b in drawn["buttons"])}
        self._show_sample("idle")
        return out

    def _check_floating(self) -> dict[str, Any]:
        """The floating button as macOS has it: on screen, on every Space and next to full-screen apps, at the
        floating level, non-activating and never key — showing it changed neither the active app nor the key
        window — and every look of it drawn with buttons at least 28 points tall, each with a VoiceOver label.
        The setup isn't done on CI (the state hides it then): it is shown with its idle look for the test."""
        out: dict[str, Any] = {"wanted": floating.wanted(self.state, quitting=self.quitting)}
        try:
            out["focus_before"] = before = self._on_main(self._focus_info) or {}
            self._on_main(lambda: self._show_sample("idle"))
            time.sleep(0.5)
            out["panel"] = info = self._on_main(lambda: self._floating.info() if self._floating else {"created": False}) or {}
            out["focus_after"] = after = self._on_main(self._focus_info) or {}
            out["states"] = drawn = self._on_main(self._draw_samples, timeout=30) or {}
        finally:
            self._on_main(lambda: self._show_sample(None))
        out["ok"] = bool(info.get("created") and info.get("visible") and info.get("all_spaces")
                         and info.get("full_screen_auxiliary") and info.get("nonactivating") and info.get("floating")
                         and info.get("can_become_key") is False and info.get("hides_on_deactivate") is False
                         and "error" not in before and before == after)
        samples = floating.sample_states()
        out["states_ok"] = bool(drawn) and set(drawn) == set(samples) and all(
            d.get("drawn") and d["buttons"] == d["expected_buttons"] and d["labelled"]
            and (d["min_button_height"] is None or d["min_button_height"] >= floating.MIN_TARGET - 0.5)
            and d["size"][1] >= floating.MIN_TARGET for d in drawn.values() if isinstance(d, dict))
        return out

    def _url_scheme_info(self) -> dict[str, Any]:
        """Main thread: the app's Info.plist names the scheme (CFBundleURLTypes), and which app Launch Services
        would open a link with (only said: a build run in place may not be registered yet)."""
        import AppKit
        import Foundation

        info = Foundation.NSBundle.mainBundle().infoDictionary() or {}
        schemes = [str(x) for t in (info.get("CFBundleURLTypes") or []) for x in (t.get("CFBundleURLSchemes") or [])]
        handler = None
        with contextlib.suppress(Exception):
            app = AppKit.NSWorkspace.sharedWorkspace().URLForApplicationToOpenURL_(
                Foundation.NSURL.URLWithString_("voicestream://open"))
            handler = str(app.path()) if app is not None else None
        return {"scheme_in_plist": "voicestream" in schemes, "default_handler": handler}

    def _send_url_event(self, url: str) -> dict[str, Any]:
        """Main thread: a "get URL" Apple event to this very process, as Launch Services sends one
        (NSAppleEventDescriptor.h ``appleEventWithEventClass:eventID:targetDescriptor:returnID:transactionID:``,
        ``currentProcessDescriptor``, ``sendEventWithOptions:timeout:error:``), with the link as its direct object."""
        import Foundation

        d = Foundation.NSAppleEventDescriptor
        event = d.appleEventWithEventClass_eventID_targetDescriptor_returnID_transactionID_(
            URL_EVENT, URL_EVENT, d.currentProcessDescriptor(), AUTO_RETURN_ID, ANY_TRANSACTION)
        event.setParamDescriptor_forKeyword_(d.descriptorWithString_(url), DIRECT_OBJECT)
        _reply, error = event.sendEventWithOptions_timeout_error_(SEND_NO_REPLY, URL_ANSWER_S, None)
        return {"sent": error is None, "error": str(error.localizedDescription()) if error is not None else None}

    def _check_url(self) -> dict[str, Any]:
        """``voicestream://`` links: the handler is registered, the Info.plist names the scheme, and two links
        delivered as Apple events reach the host — the launcher's keyed ``meeting/toggle``, which the host would
        carry out as a press of the shortcut, and the same link without its key, which it would refuse. Like
        the shortcut's test, the host is only asked: nothing starts. Neither the link nor its key is reported."""
        out: dict[str, Any] = {"registered": self._url_handler == "registered", "handler": self._url_handler}
        out.update(self._on_main(self._url_scheme_info) or {})
        try:
            own = str(self.host.call(self._on_loop("launcher_url")).result(URL_ANSWER_S) or "")
        except Exception as exc:  # noqa: BLE001 - in the report
            out["error"] = f"{type(exc).__name__}: {exc}"
            return out
        links = (own.replace("://meeting/start?", "://meeting/toggle?", 1), "voicestream://meeting/toggle")
        before = launcher.safe_state(self.host).get("state")
        answers: list[Any] = []
        received = self._urls
        self._url_probe = answers
        results: list[Any] = []
        try:
            out["sent"] = [self._on_main(lambda u=u: self._send_url_event(u)) for u in links]
            deadline = time.monotonic() + URL_ANSWER_S
            while len(answers) < len(links) and time.monotonic() < deadline:
                time.sleep(0.05)
            out["handled"] = self._urls - received
            results = [a.result(URL_ANSWER_S) for a in answers]
        except Exception as exc:  # noqa: BLE001 - in the report
            out["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            self._url_probe = None
        after = launcher.safe_state(self.host).get("state")
        out.update(state_before=before, state_after=after, expected=hotkey.TOGGLE.get(str(before)),
                   answers=results)
        keyed, keyless = (results + [{}, {}])[:2]
        out["reaches_host"] = (out.get("handled") == len(links) and keyed.get("action") == "toggle"
                               and keyed.get("key_ok") is True and out["expected"] is not None
                               and keyed.get("would") == out["expected"] and keyless.get("key_ok") is False
                               and keyless.get("would") is None and after == before)
        return out

    def _check_spotlight(self, *, frozen: bool) -> dict[str, Any]:
        """The Spotlight launcher, written into a temporary folder (never ~/Applications here) as the setting
        writes it: an Info.plist plutil reads, its own bundle id, LSUIElement, a script that may run (readable by
        you only), the app's icon in the packaged app; then removed as turning the setting off removes it."""
        import shutil
        import tempfile

        from .. import __version__

        folder = Path(tempfile.mkdtemp(prefix="voicestream-launcher-test-"))
        out: dict[str, Any] = {"ok": False}
        try:
            url = str(self.host.call(self._on_loop("launcher_url")).result(URL_ANSWER_S) or "")
            app = native_hooks.bundle_path()
            made = spotlight.Launcher(url=url, lang=self.lang, app=app, icon=spotlight.app_icon(app), version=__version__)
            out["sync"] = spotlight.sync(folder, made)
            bundle = folder / f"{made.name}.app"
            out["check"] = check = spotlight.check(bundle)
            out["icon"] = (bundle / "Contents" / "Resources" / spotlight.ICON).is_file()
            out["removed"] = spotlight.sync(folder, None)
            out["ok"] = (out["sync"] == "written" and bool(check.get("ok")) and (out["icon"] or not frozen)
                         and out["removed"] == "removed" and not bundle.exists())
        except Exception as exc:  # noqa: BLE001 - in the report
            out["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            shutil.rmtree(folder, ignore_errors=True)
        return out

    def _check_mic_probe(self) -> dict[str, Any]:
        """The call detection's look at the microphone: Core Audio answers whether another app captures, and
        which processes (macOS 14+). Only bundle ids are reported."""
        try:
            use = calls.probe()
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        out = calls.describe(use)
        out["ok"] = isinstance(use.in_use, bool) and bool(out["processes_known"])
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
