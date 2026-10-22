"""The floating button: a small panel that stays above every app, to start, pause and stop a meeting.

It is an ``NSPanel`` that never takes the focus: borderless and non-activating (clicking it leaves the
app you are typing in active, with your cursor where it was), at the floating window level, on every
Space and next to full-screen apps (``canJoinAllSpaces`` + ``fullScreenAuxiliary``), not hidden when
Voicestream isn't the active app. It is moved by dragging its background; where you leave it is
remembered (``prefs.floating_position``: the frame's origin and the screen's name) and kept on a
screen when displays come and go. Its own context menu hides it, opens Voicestream or its Settings.
Hidden, it still appears for a call suggestion, "Still in a meeting?" or a meeting that ended by itself
(with the window closed nothing else shows them), with that look alone, until it is answered.

What it shows follows the host's state (:func:`panel_model`, pure, tested on Linux), drawn with
AppKit's own controls: a vibrant HUD background (``NSVisualEffectView``) and semantic colours, so it
reads in light and dark mode; SF Symbols; push buttons at least 28 points tall (the HIG's minimum
pointer target on a Mac), each with a VoiceOver label and a tooltip:

* idle: Start meeting (Start listening when meetings start automatically);
* recording: a red dot that breathes (still, with Reduce Motion on), the time since the meeting
  started, Pause / Resume and Stop;
* a call another app started: "Call detected — start notes?" with Start and Not now;
* a long silence: "Still in a meeting?" with Keep and End;
* what happened by itself (a meeting ended after a long silence, a link refused), until OK, with Start
  meeting next to it;
* a problem: what it is (in the tooltip), Show and Try again.

It acts only through the shell (``on_action(key)``), which calls the host's meeting actions: the same
ones as the window, the menu bar and the keyboard shortcut. A suggestion or a nudge appearing is also
said to VoiceOver (it appears without the focus, so it would go unheard otherwise).

AppKit on the main thread only. Every AppKit import is inside a function, so the module imports on
Linux. The AppKit values used are spelled out below, from the macOS SDK headers quoted by name
(``NSWindow.h``, ``NSPanel.h``, ``NSVisualEffectView.h``, ``NSButton.h``/``NSButtonCell.h``, ``NSCell.h``,
``NSStackView.h``, ``NSLayoutConstraint.h``, ``NSParagraphStyle.h``), and checked against PyObjC 12.2's
AppKit metadata, which is generated from the same headers.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)

# ------------------------------------------------------------- AppKit values

STYLE_BORDERLESS = 0  # NSWindow.h NSWindowStyleMaskBorderless = 0
STYLE_NONACTIVATING = 1 << 7
"""NSWindow.h ``NSWindowStyleMaskNonactivatingPanel = 1 << 7``: "a panel that does not activate the owning
application" (NSPanel only)."""
JOIN_ALL_SPACES = 1 << 0  # NSWindow.h NSWindowCollectionBehaviorCanJoinAllSpaces = 1 << 0
IGNORES_CYCLE = 1 << 6  # NSWindow.h NSWindowCollectionBehaviorIgnoresCycle = 1 << 6: not in ⌘` window cycling
FULL_SCREEN_AUXILIARY = 1 << 8
"""NSWindow.h ``NSWindowCollectionBehaviorFullScreenAuxiliary = 1 << 8``: "Windows with this collection behavior
can be shown with the fullscreen window"."""
FLOATING_LEVEL = 3
"""NSWindow.h ``NSFloatingWindowLevel = kCGFloatingWindowLevel`` (CGWindowLevel.h, CGWindowLevelForKey of
kCGFloatingWindowLevelKey): 3, as PyObjC's metadata has it. AppKit's own constant is used when PyObjC exports it."""
BACKING_BUFFERED = 2  # NSGraphics.h NSBackingStoreBuffered = 2
ANIMATION_UTILITY = 4  # NSWindow.h NSWindowAnimationBehaviorUtilityWindow = 4
MATERIAL_HUD = 13  # NSVisualEffectView.h NSVisualEffectMaterialHUDWindow = 13: "The material for a HUD window"
BLEND_BEHIND_WINDOW = 0  # NSVisualEffectView.h NSVisualEffectBlendingModeBehindWindow = 0
EFFECT_ACTIVE = 1
"""NSVisualEffectView.h ``NSVisualEffectStateActive = 1``: vibrant even though the app is never the active one
(the default follows the window's active state, and this panel's window is never key)."""
BEZEL_PUSH = 1  # NSButtonCell.h NSBezelStylePush = 1 (NSBezelStyleRounded before macOS 14)
BUTTON_MOMENTARY_PUSH_IN = 7  # NSButtonCell.h NSButtonTypeMomentaryPushIn = 7
CONTROL_LARGE = 3  # NSCell.h NSControlSizeLarge = 3 (macOS 11)
IMAGE_ONLY, IMAGE_LEADING = 1, 7  # NSCell.h NSImageOnly = 1, NSImageLeading = 7
ORIENTATION_HORIZONTAL = 0  # NSLayoutConstraint.h NSUserInterfaceLayoutOrientationHorizontal = 0
ALIGN_CENTER_Y = 10  # NSLayoutConstraint.h NSLayoutAttributeCenterY = 10
TRUNCATE_TAIL = 4  # NSParagraphStyle.h NSLineBreakByTruncatingTail = 4
PRIORITY_LOW = 250.0  # NSLayoutConstraint.h NSLayoutPriorityDefaultLow = 250
WEIGHT_MEDIUM = 0.23  # NSFontDescriptor.h NSFontWeightMedium (an extern CGFloat; AppKit's own is used when exported)

MIN_TARGET = 28.0
"""The HIG's minimum pointer target on a Mac, in points: every button is at least this tall."""
ICON_BUTTON_WIDTH = 34.0
HEIGHT = 44.0  # MIN_TARGET plus the insets below
INSETS = (8.0, 12.0, 8.0, 8.0)  # top, leading, bottom, trailing
SPACING = 8.0
CORNER_RADIUS = 14.0
TEXT_MAX_WIDTH = 300.0
MARGIN = 16.0
"""How far from the screen's edges the panel goes by default (and how close it may be dragged: none)."""
TOP_GAP = 96.0
"""By default it sits at the right, this far below the menu bar: notification banners show at the very top."""
SAVE_AFTER_S = 0.6
"""A drag ends, then the place is saved (once, not at every point of the drag)."""
MENU_KEYS = ("hide", "sep", "open", "settings")

STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "panel": "Voicestream meeting controls",
        "start_meeting": "Start meeting",
        "start_listening": "Start listening",
        "start_tip": "Start a meeting: Voicestream records until you stop it",
        "start_tip_auto": "Start listening: meetings open and close by themselves",
        "shortcut_tip": "{tip} ({shortcut})",
        "recording": "Recording",
        "listening": "Listening",
        "paused": "Paused",
        "pause": "Pause",
        "resume": "Resume",
        "stop_meeting": "Stop meeting",
        "stop_listening": "Stop listening",
        "stop_tip": "Stop: the notes are written and the microphone is let go",
        "starting": "Starting…",
        "saving": "Saving notes…",
        "stopping": "Stopping…",
        "elapsed": "Meeting time {time}",
        "suggestion": "Call detected — start notes?",
        "suggestion_app": "{app} is using the microphone",
        "suggest_start": "Start",
        "not_now": "Not now",
        "still": "Still in a meeting?",
        "still_tip": "No one has spoken for {minutes} min",
        "keep": "Keep",
        "end": "End",
        "ended_silence": "Meeting ended after {minutes} min of silence",
        "ended_tip": "Its notes are saved: Voicestream → Meetings",
        "ended_writing": "Meeting ended: its notes are being written…",
        "ended_writing_tip": "It ended after {minutes} min of silence. Its notes are being written.",
        "ended_empty_tip": "Nothing had been said, so there are no notes",
        "link_refused": "A link tried to start recording — ignored",
        "link_tip": "Only Voicestream's own launcher can start a meeting from a link",
        "ok": "OK",
        "problem": "Couldn't record",
        "show": "Show",
        "retry": "Try again",
        "hide": "Hide Floating Button",
        "open": "Open Voicestream",
        "settings": "Settings…",
    },
    "fr": {
        "panel": "Commandes de réunion Voicestream",
        "start_meeting": "Démarrer une réunion",
        "start_listening": "Démarrer l'écoute",
        "start_tip": "Démarrer une réunion : Voicestream enregistre jusqu'à ce que vous l'arrêtiez",
        "start_tip_auto": "Démarrer l'écoute : les réunions s'ouvrent et se ferment d'elles-mêmes",
        "shortcut_tip": "{tip} ({shortcut})",
        "recording": "Enregistrement",
        "listening": "À l'écoute",
        "paused": "En pause",
        "pause": "Mettre en pause",
        "resume": "Reprendre",
        "stop_meeting": "Arrêter la réunion",
        "stop_listening": "Arrêter l'écoute",
        "stop_tip": "Arrêter : les notes sont rédigées et le micro est libéré",
        "starting": "Démarrage…",
        # not « Enregistrement » (the app's French for Recording): just after Stop, it would read as still recording
        "saving": "Rédaction des notes…",
        "stopping": "Arrêt…",
        "elapsed": "Durée de la réunion {time}",
        "suggestion": "Appel détecté — prendre des notes ?",
        "suggestion_app": "{app} utilise le micro",
        "suggest_start": "Démarrer",
        "not_now": "Pas maintenant",  # the window's word; and this call is never suggested again: not "later"
        "still": "Toujours en réunion ?",
        "still_tip": "Personne n'a parlé depuis {minutes} min",
        "keep": "Continuer",
        "end": "Terminer",
        "ended_silence": "Réunion terminée après {minutes} min de silence",
        "ended_tip": "Ses notes sont enregistrées : Voicestream → Réunions",
        "ended_writing": "Réunion terminée : rédaction des notes…",
        "ended_writing_tip": "Elle s'est terminée après {minutes} min de silence. Ses notes sont en cours de rédaction.",
        "ended_empty_tip": "Rien n'avait été dit, il n'y a donc pas de notes",
        "link_refused": "Un lien voulait enregistrer — ignoré",  # one line of the panel: « ignoré » must show
        "link_tip": "Seul le lanceur de Voicestream peut démarrer une réunion depuis un lien",
        "ok": "OK",
        "problem": "Enregistrement impossible",
        "show": "Afficher",
        "retry": "Réessayer",
        "hide": "Masquer le bouton flottant",
        "open": "Ouvrir Voicestream",
        "settings": "Réglages…",
    },
}

# ------------------------------------------------------------- pure helpers


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


def meeting_mode(state: dict[str, Any]) -> str:
    """``"manual"`` or ``"automatic"``: the host's ``mode`` (the ``meeting_start`` preference)."""
    mode = state.get("mode") or (state.get("prefs") or {}).get("meeting_start")
    return "automatic" if mode == "automatic" else "manual"


@dataclass(frozen=True, slots=True)
class PanelButton:
    key: str
    """What a click does, for the shell: start, pause, resume, stop, accept, dismiss, keep, end, ok, show, retry."""
    title: str
    label: str
    """What VoiceOver says (the title, or what an icon-only button does)."""
    symbol: str = ""
    icon_only: bool = False
    prominent: bool = False
    tooltip: str = ""
    tint: str = ""
    """The symbol's colour when it carries meaning (``red``: recording), else it follows the title."""


@dataclass(frozen=True, slots=True)
class PanelModel:
    kind: str
    """idle, recording, paused, listening, starting, stopping, suggestion, still_there, notice, error."""
    text: str = ""
    symbol: str = ""
    tint: str = "secondary"
    """The symbol's colour: red (recording), secondary, yellow (a problem), green (a call)."""
    symbol_label: str = ""
    animate: bool = False
    timer: str = ""
    timer_label: str = ""
    tooltip: str = ""
    buttons: tuple[PanelButton, ...] = ()
    announce: str = ""
    """Said to VoiceOver when this appears (a suggestion, a nudge, a notice)."""


def _question(state: dict[str, Any]) -> tuple[str, str] | None:
    """``(look, key)`` of what must reach you even with the floating button hidden: a call suggestion, "Still in a
    meeting?" (asked of a meeting started by hand, while it records) or a meeting that ended by itself after a long
    silence — with the window closed, nothing else shows them. ``key`` stays the same for the same question (a
    nudge asked again after a pause, a new call: another one). ``None``: none is up."""
    current = str(state.get("state") or "stopped")
    still = state.get("still_there")
    if current == "listening" and still and meeting_mode(state) == "manual":
        return "still_there", f"still_there:{still.get('meeting_id')}:{still.get('ends_at')}"
    suggestion = state.get("suggestion")
    if current in ("stopped", "error") and suggestion:
        return "suggestion", f"suggestion:{suggestion.get('at')}"
    notice = state.get("notice") or {}
    if current in ("stopped", "error", "stopping") and notice.get("kind") == "ended_after_silence":
        return "notice", f"ended:{notice.get('meeting_id')}:{notice.get('at')}"
    return None


def question_key(state: dict[str, Any]) -> str | None:
    """Which question the floating button is up for while hidden (see :func:`wanted`), ``None``: none."""
    asked = _question(state)
    return asked[1] if asked is not None else None


def shown_by_setting(state: dict[str, Any]) -> bool:
    """The floating button is turned on (``prefs.floating_button``, the default)."""
    return bool((state.get("prefs") or {}).get("floating_button", True))


def wanted(state: dict[str, Any], *, quitting: bool = False, hidden_for: str | None = None) -> bool:
    """The floating button is shown: the setup is done (until then the window is all there is), the shell has
    one (``capabilities.floating_button``), not while quitting, and turned on (``prefs.floating_button``) —
    or, turned off, while a question is up that nothing else would show with the window closed
    (:func:`question_key`), with that look only, until it is answered or withdrawn. ``hidden_for``: the question
    it was hidden for from its menu (it isn't shown again for that one)."""
    caps = state.get("capabilities") or {}
    if quitting or not state.get("setup_done") or not caps.get("floating_button"):
        return False
    if shown_by_setting(state):
        return True
    key = question_key(state)
    return key is not None and key != hidden_for


def _short(text: Any, limit: int = 200) -> str:
    line = " ".join(str(text or "").split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def panel_model(state: dict[str, Any], lang: str, now: datetime | None = None, *, shortcut: str = "") -> PanelModel:
    """What the floating button shows for this host state. ``shortcut``: the keyboard shortcut as macOS took
    it (``⌥⌘R``), named in the Start and Stop tooltips; empty when there is none."""
    s = STRINGS.get(lang, STRINGS["en"])
    now = now or datetime.now(UTC)
    manual = meeting_mode(state) == "manual"
    current = str(state.get("state") or "stopped")

    def tip(text: str) -> str:
        return s["shortcut_tip"].format(tip=text, shortcut=shortcut) if shortcut else text

    title = s["start_meeting"] if manual else s["start_listening"]
    start = PanelButton("start", title, title, "record.circle" if manual else "mic", tint="red" if manual else "",
                        tooltip=tip(s["start_tip"] if manual else s["start_tip_auto"]))

    def still_there() -> PanelModel:
        minutes = int((state.get("still_there") or {}).get("silent_minutes") or 0)
        return PanelModel("still_there", s["still"], "hourglass", "secondary", tooltip=s["still_tip"].format(minutes=minutes),
                          buttons=(PanelButton("keep", s["keep"], s["keep"], prominent=True),
                                   PanelButton("end", s["end"], s["end"])),
                          announce=s["still"])

    def suggestion() -> PanelModel:
        app = _short((state.get("suggestion") or {}).get("app"), 80)
        return PanelModel("suggestion", s["suggestion"], "phone.fill", "green",
                          tooltip=s["suggestion_app"].format(app=app) if app else "",
                          buttons=(PanelButton("accept", s["suggest_start"], s["suggest_start"], prominent=True,
                                               tooltip=tip(s["start_tip"])),
                                   PanelButton("dismiss", s["not_now"], s["not_now"])),
                          announce=s["suggestion"])

    # a notice stays until OK (for hours after a meeting forgotten overnight): Start stays one click away next to it
    after = (start, PanelButton("ok", s["ok"], s["ok"]))

    def ended(notice: dict[str, Any]) -> PanelModel:
        minutes = int(notice.get("minutes") or 0)
        # the host says ``pending`` until its notes are written (``is True``: a notice without the field is done)
        if notice.get("pending") is True:
            text, symbol = s["ended_writing"], "square.and.arrow.down"
            tooltip = s["ended_writing_tip"].format(minutes=minutes)
        else:
            text, symbol = s["ended_silence"].format(minutes=minutes), "checkmark.circle"
            # written, and no folder: nothing had been said (only a host that says pending says that much)
            tooltip = s["ended_empty_tip"] if notice.get("pending") is False and not notice.get("folder") else s["ended_tip"]
        return PanelModel("notice", text, symbol, "secondary", tooltip=tooltip, buttons=after, announce=text)

    notice = state.get("notice") or {}
    asked = _question(state)
    if asked is not None and not shown_by_setting(state):
        # hidden by the setting and shown for this question alone (see wanted): its look, whatever else goes on
        return still_there() if asked[0] == "still_there" else suggestion() if asked[0] == "suggestion" else ended(notice)

    stop_title = s["stop_meeting"] if manual else s["stop_listening"]
    # the shortcut is named only where a press stops (hotkey.TOGGLE): paused it resumes, starting it does nothing
    stop_tip = tip(s["stop_tip"]) if current == "listening" else s["stop_tip"]
    stop = PanelButton("stop", stop_title, stop_title, "stop.fill", icon_only=True, tooltip=stop_tip)
    if current in ("listening", "paused"):
        # paused, nothing is recorded (nor counted as silence): Resume shows, not Keep / End
        if state.get("still_there") and manual and current == "listening":
            return still_there()
        started = meeting_started(state)
        timer = format_elapsed((now - started).total_seconds()) if started is not None else ""
        timer_label = s["elapsed"].format(time=timer) if timer else ""
        if current == "paused":
            return PanelModel("paused", s["paused"], "pause.circle.fill", "secondary", s["paused"], timer=timer,
                              timer_label=timer_label,
                              buttons=(PanelButton("resume", s["resume"], s["resume"], "play.fill", icon_only=True,
                                                   tooltip=tip(s["resume"])), stop))
        pause = PanelButton("pause", s["pause"], s["pause"], "pause.fill", icon_only=True, tooltip=s["pause"])
        if started is None:  # automatic: listening, no meeting yet (the next speech opens one)
            return PanelModel("listening", s["listening"], "mic.fill", "secondary", s["listening"], buttons=(pause, stop))
        return PanelModel("recording", "", "circle.fill", "red", s["recording"], animate=True, timer=timer,
                          timer_label=timer_label, buttons=(pause, stop))
    if current == "starting":
        return PanelModel("starting", s["starting"], "circle.dotted", "secondary", buttons=(stop,))
    if current == "stopping":
        return PanelModel("stopping", s["saving"] if manual else s["stopping"], "square.and.arrow.down", "secondary")
    if state.get("suggestion"):
        return suggestion()
    if notice.get("kind") == "ended_after_silence":
        return ended(notice)
    if notice.get("kind") == "link_refused":
        return PanelModel("notice", s["link_refused"], "hand.raised", "secondary", tooltip=s["link_tip"], buttons=after,
                          announce=s["link_refused"])
    if current == "error":
        return PanelModel("error", s["problem"], "exclamationmark.triangle.fill", "yellow", tooltip=_short(state.get("error")),
                          buttons=(PanelButton("show", s["show"], s["show"]),
                                   PanelButton("retry", s["retry"], s["retry"], tooltip=tip(s["retry"]))))
    return PanelModel("idle", buttons=(start,))


def sample_states() -> dict[str, dict[str, Any]]:
    """A host state for each thing the panel can show: the self-test draws every one of them."""
    base: dict[str, Any] = {"setup_done": True, "mode": "manual", "in_meeting": False, "meeting": None, "error": None,
                            "suggestion": None, "still_there": None, "notice": None}
    meeting = {"in_meeting": True, "meeting": {"id": "m", "title": "", "started_at": "2026-01-01T09:00:00+00:00"}}
    return {
        "idle": base,
        "idle_automatic": {**base, "mode": "automatic"},
        "recording": {**base, **meeting, "state": "listening"},
        "paused": {**base, **meeting, "state": "paused"},
        "listening": {**base, "mode": "automatic", "state": "listening"},
        "starting": {**base, "state": "starting"},
        "stopping": {**base, "state": "stopping"},
        "suggestion": {**base, "suggestion": {"app": "Microsoft Teams", "at": "2026-01-01T09:00:00+00:00"}},
        "still_there": {**base, **meeting, "state": "listening", "still_there": {"meeting_id": "m", "silent_minutes": 15}},
        "notice": {**base, "notice": {"kind": "ended_after_silence", "minutes": 60}},
        "notice_writing": {**base, "notice": {"kind": "ended_after_silence", "minutes": 60, "pending": True}},
        "link_refused": {**base, "notice": {"kind": "link_refused"}},
        "error": {**base, "state": "error", "error": "Microphone permission is off: System Settings → Privacy & Security"},
    }


# ------------------------------------------------------------------ placement

Rect = tuple[float, float, float, float]
"""``(x, y, width, height)``, as AppKit counts screen points: from the bottom left of the main screen."""


@dataclass(frozen=True, slots=True)
class Screen:
    name: str | None
    frame: Rect
    visible: Rect
    """The part windows may use: without the menu bar and the Dock."""


def _inside(point: tuple[float, float], r: Rect) -> bool:
    return r[0] <= point[0] <= r[0] + r[2] and r[1] <= point[1] <= r[1] + r[3]


def clamp(origin: tuple[float, float], size: tuple[float, float], visible: Rect) -> tuple[float, float]:
    """``origin`` moved as little as it takes for the whole panel to be on ``visible``."""
    vx, vy, vw, vh = visible
    w, h = size
    x = min(max(origin[0], vx), vx + max(0.0, vw - w))
    y = min(max(origin[1], vy), vy + max(0.0, vh - h))
    return x, y


def default_origin(size: tuple[float, float], screen: Screen) -> tuple[float, float]:
    vx, vy, vw, vh = screen.visible
    return clamp((vx + vw - size[0] - MARGIN, vy + vh - size[1] - TOP_GAP), size, screen.visible)


def screen_of(origin: tuple[float, float], size: tuple[float, float], screens: Sequence[Screen]) -> Screen | None:
    """The screen the panel's centre is on (``None``: none any more)."""
    centre = (origin[0] + size[0] / 2, origin[1] + size[1] / 2)
    return next((sc for sc in screens if _inside(centre, sc.frame)), None)


def place(size: tuple[float, float], saved: dict[str, Any] | None, screens: Sequence[Screen]) -> tuple[float, float]:
    """Where the panel goes: where it was left (``saved``, the ``floating_position`` preference), kept whole on
    the screen it is on; on the screen it was on if that one is still connected but arranged elsewhere; else
    (no place saved, or its screen is gone) at its default place on the main screen (``screens[0]``)."""
    if not screens:
        return (float(saved["x"]), float(saved["y"])) if saved else (0.0, 0.0)
    main = screens[0]
    if not saved:
        return default_origin(size, main)
    origin = (float(saved["x"]), float(saved["y"]))
    on = screen_of(origin, size, screens)
    if on is not None:
        return clamp(origin, size, on.visible)
    named = next((sc for sc in screens if saved.get("screen") and sc.name == saved.get("screen")), None)
    if named is not None:
        return clamp(origin, size, named.visible)
    return default_origin(size, main)


def resized(frame: Rect, size: tuple[float, float], screens: Sequence[Screen]) -> tuple[float, float]:
    """The new origin when the content changes size: the top edge stays, and the side nearer the screen's edge
    (the right one when the panel is on the right half), so it grows away from it; kept whole on its screen."""
    x, y, w, h = frame
    top = y + h
    on = screen_of((x, y), (w, h), screens)
    right = on is not None and x + w / 2 > on.visible[0] + on.visible[2] / 2
    origin = (x + w - size[0] if right else x, top - size[1])
    return clamp(origin, size, on.visible) if on is not None else origin


# ----------------------------------------------------------------- AppKit glue

_CLASSES: dict[str, Any] = {}
"""The Objective-C classes, defined once (their names are process-wide)."""


def _classes() -> dict[str, Any]:
    if not _CLASSES:
        import objc
        from AppKit import NSButton, NSTextField
        from Foundation import NSObject

        class VoicestreamPanelButton(NSButton):
            # no typedSelector on these overrides: PyObjC keeps the encodings of the methods they override
            def acceptsFirstMouse_(self, event):  # the first click acts: the panel is never key
                return True

        class VoicestreamPanelLabel(NSTextField):
            def mouseDownCanMoveWindow(self):  # the panel is dragged by its labels too
                return True

        class VoicestreamPanelTarget(NSObject):
            @objc.typedSelector(b"v@:@")
            def panelAction_(self, sender):
                _guard(lambda: self.panel.clicked(int(sender.tag())))

            @objc.typedSelector(b"v@:@")
            def contextAction_(self, sender):
                _guard(lambda: self.panel.on_action(str(sender.representedObject())))

            @objc.typedSelector(b"v@:@")
            def windowDidMove_(self, notification):
                _guard(self.panel.moved)

            @objc.typedSelector(b"v@:@")
            def screensChanged_(self, notification):
                _guard(self.panel.screens_changed)

            @objc.typedSelector(b"v@:@")
            def savePosition_(self, timer):
                _guard(self.panel.save_position)

        _CLASSES.update(button=VoicestreamPanelButton, label=VoicestreamPanelLabel, target=VoicestreamPanelTarget)
    return _CLASSES


def _guard(fn: Callable[[], Any]) -> None:
    """Never let a Python exception escape into AppKit (it would abort the app)."""
    try:
        fn()
    except Exception:  # whatever went wrong is logged: the panel and the app go on
        log.exception("floating button action failed")


def _rect(r: Any) -> Rect:
    return float(r.origin.x), float(r.origin.y), float(r.size.width), float(r.size.height)


def screens() -> list[Screen]:
    """The connected screens, the main one (with the menu bar) first. Main thread."""
    import AppKit

    out = []
    for sc in AppKit.NSScreen.screens() or []:
        name = None
        with contextlib.suppress(Exception):  # localizedName: macOS 10.15
            name = str(sc.localizedName())
        out.append(Screen(name, _rect(sc.frame()), _rect(sc.visibleFrame())))
    return out


def reduce_motion() -> bool:
    try:
        import AppKit

        return bool(AppKit.NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceMotion())
    except Exception:  # can't tell: no animation
        log.debug("could not read Reduce Motion", exc_info=True)
        return True


@dataclass
class FloatingPanel:
    """The panel and its views, made once and updated in place. Main thread only.

    ``on_action(key)``: a button or a context menu item was clicked (the keys of :class:`PanelButton` and
    :data:`MENU_KEYS`). ``on_moved(position)``: it was dragged somewhere else (``{"x", "y", "screen"}``)."""

    on_action: Callable[[str], None]
    on_moved: Callable[[dict[str, Any]], None]
    lang: str = "en"
    model: PanelModel | None = None
    panel: Any = None
    shown: int = 0
    """How many times it was ordered on screen (the self-test's check)."""
    _views: dict[str, Any] = field(default_factory=dict)
    _buttons: list[Any] = field(default_factory=list)
    _keys: list[str] = field(default_factory=list)
    _target: Any = None
    _menu: Any = None
    _menu_lang: str = ""
    _placing: bool = False
    _save_timer: Any = None
    _observer: bool = False
    _animating: bool = False
    _images: dict[tuple[str, str, float], Any] = field(default_factory=dict)

    # ------------------------------------------------------------ building

    def _build(self) -> None:
        import AppKit

        cls = _classes()
        target = self._target = cls["target"].alloc().init()
        target.panel = self
        rect = ((0.0, 0.0), (200.0, HEIGHT))
        panel = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, STYLE_BORDERLESS | STYLE_NONACTIVATING, BACKING_BUFFERED, False)
        panel.setFloatingPanel_(True)
        panel.setLevel_(getattr(AppKit, "NSFloatingWindowLevel", FLOATING_LEVEL))
        panel.setCollectionBehavior_(JOIN_ALL_SPACES | FULL_SCREEN_AUXILIARY | IGNORES_CYCLE)
        panel.setHidesOnDeactivate_(False)  # Voicestream is rarely the active app: it must stay
        panel.setCanHide_(False)  # … and closing the window hides the app (NSApp hide:): not this panel
        panel.setBecomesKeyOnlyIfNeeded_(True)
        panel.setMovableByWindowBackground_(True)
        panel.setAllowsToolTipsWhenApplicationIsInactive_(True)  # the icon-only buttons say what they do on hover
        panel.setOpaque_(False)
        panel.setBackgroundColor_(AppKit.NSColor.clearColor())
        panel.setHasShadow_(True)
        panel.setReleasedWhenClosed_(False)
        panel.setExcludedFromWindowsMenu_(True)
        panel.setAnimationBehavior_(ANIMATION_UTILITY)
        panel.setTitle_("Voicestream")
        panel.setDelegate_(target)

        effect = AppKit.NSVisualEffectView.alloc().initWithFrame_(rect)
        effect.setMaterial_(MATERIAL_HUD)
        effect.setBlendingMode_(BLEND_BEHIND_WINDOW)
        effect.setState_(EFFECT_ACTIVE)
        effect.setWantsLayer_(True)
        layer = effect.layer()
        layer.setCornerRadius_(CORNER_RADIUS)
        layer.setMasksToBounds_(True)
        with contextlib.suppress(Exception):  # macOS 11: the continuous corners of the system's own controls
            layer.setCornerCurve_("continuous")
        panel.setContentView_(effect)

        icon = AppKit.NSImageView.alloc().initWithFrame_(((0.0, 0.0), (18.0, 18.0)))
        text = cls["label"].labelWithString_("")
        text.setLineBreakMode_(TRUNCATE_TAIL)
        text.setMaximumNumberOfLines_(1)
        text.setTextColor_(AppKit.NSColor.labelColor())
        text.setFont_(AppKit.NSFont.systemFontOfSize_(AppKit.NSFont.systemFontSize()))
        text.setContentCompressionResistancePriority_forOrientation_(PRIORITY_LOW, ORIENTATION_HORIZONTAL)
        timer = cls["label"].labelWithString_("0:00")
        timer.setTextColor_(AppKit.NSColor.labelColor())
        weight = getattr(AppKit, "NSFontWeightMedium", WEIGHT_MEDIUM)
        timer.setFont_(AppKit.NSFont.monospacedDigitSystemFontOfSize_weight_(AppKit.NSFont.systemFontSize(), weight))
        views = [icon, text, timer]
        for i in range(3):
            b = cls["button"].alloc().initWithFrame_(((0.0, 0.0), (ICON_BUTTON_WIDTH, MIN_TARGET)))
            b.setBezelStyle_(BEZEL_PUSH)
            b.setButtonType_(BUTTON_MOMENTARY_PUSH_IN)
            with contextlib.suppress(Exception):
                b.setControlSize_(CONTROL_LARGE)
            b.setTarget_(target)
            b.setAction_("panelAction:")
            b.setTag_(i)
            b.setTranslatesAutoresizingMaskIntoConstraints_(False)
            b.heightAnchor().constraintGreaterThanOrEqualToConstant_(MIN_TARGET).setActive_(True)
            b.setHidden_(True)
            self._buttons.append(b)
            views.append(b)
        text.widthAnchor().constraintLessThanOrEqualToConstant_(TEXT_MAX_WIDTH).setActive_(True)
        stack = AppKit.NSStackView.stackViewWithViews_(views)
        stack.setOrientation_(ORIENTATION_HORIZONTAL)
        stack.setAlignment_(ALIGN_CENTER_Y)
        stack.setSpacing_(SPACING)
        stack.setEdgeInsets_(INSETS)
        stack.setTranslatesAutoresizingMaskIntoConstraints_(False)
        effect.addSubview_(stack)
        AppKit.NSLayoutConstraint.activateConstraints_([
            stack.leadingAnchor().constraintEqualToAnchor_(effect.leadingAnchor()),
            stack.trailingAnchor().constraintEqualToAnchor_(effect.trailingAnchor()),
            stack.topAnchor().constraintEqualToAnchor_(effect.topAnchor()),
            stack.bottomAnchor().constraintEqualToAnchor_(effect.bottomAnchor()),
        ])
        self._views = {"effect": effect, "stack": stack, "icon": icon, "text": text, "timer": timer}
        self.panel = panel
        if not self._observer:
            AppKit.NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
                target, "screensChanged:", "NSApplicationDidChangeScreenParametersNotification", None)
            self._observer = True

    def _context_menu(self) -> Any:
        """Hide Floating Button, Open Voicestream, Settings…: the right click (or Control-click) menu."""
        import AppKit

        if self._menu is not None and self._menu_lang == self.lang:
            return self._menu
        s = STRINGS.get(self.lang, STRINGS["en"])
        menu = AppKit.NSMenu.alloc().initWithTitle_("Voicestream")
        for key in MENU_KEYS:
            if key == "sep":
                menu.addItem_(AppKit.NSMenuItem.separatorItem())
                continue
            item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(s[key], "contextAction:", "")
            item.setTarget_(self._target)
            item.setRepresentedObject_(key)
            menu.addItem_(item)
        for view in (*self._views.values(), *self._buttons):
            view.setMenu_(menu)
        self._menu, self._menu_lang = menu, self.lang
        return menu

    def _image(self, symbol: str, label: str, size: float, tint: str = "") -> Any:
        """An SF Symbol at ``size`` points; with ``tint`` drawn in that colour whatever the control's (``red``
        for record), else a template image the control colours like its title."""
        key = (symbol, tint, size)
        if key not in self._images:
            import AppKit

            img = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbol, label or None)
            if img is not None:
                config = AppKit.NSImageSymbolConfiguration.configurationWithPointSize_weight_(
                    size, getattr(AppKit, "NSFontWeightMedium", WEIGHT_MEDIUM))
                if tint:
                    with contextlib.suppress(Exception):  # palette colours: macOS 12
                        config = config.configurationByApplyingConfiguration_(
                            AppKit.NSImageSymbolConfiguration.configurationWithPaletteColors_([_color(tint)]))
                img = img.imageWithSymbolConfiguration_(config) or img
            self._images[key] = img
        return self._images[key]

    # ------------------------------------------------------------ showing

    def update(self, model: PanelModel | None, *, saved: dict[str, Any] | None = None, lang: str = "en") -> None:
        """Show ``model`` (``None``: hide the panel). ``saved``: the place to put it the first time it shows."""
        self.lang = lang
        if model is None:
            if self.panel is not None and self.panel.isVisible():
                self.panel.orderOut_(None)
            self._stop_animation()
            self.model = None
            return
        first = self.panel is None
        if first:
            self._build()
        self._context_menu()
        appears = self.model is None or self.model.kind != model.kind
        self._render(model)
        size = self._fitting_size()
        if first or not self.panel.isVisible():
            self._place(place(size, saved, screens()), size)
            self.panel.orderFrontRegardless()  # shown without making Voicestream the active app
            self.shown += 1
        else:
            current = _rect(self.panel.frame())
            if (round(current[2]), round(current[3])) != (round(size[0]), round(size[1])):
                self._place(resized(current, size, screens()), size)
        if appears and model.announce:
            _announce(self.panel, model.announce)

    def _render(self, model: PanelModel) -> None:
        import AppKit

        v = self._views
        self.model = model
        icon, text, timer = v["icon"], v["text"], v["timer"]
        icon.setHidden_(not model.symbol)
        if model.symbol:
            size = 9.0 if model.symbol == "circle.fill" else 15.0  # the recording dot is a dot, not a disc
            icon.setImage_(self._image(model.symbol, model.symbol_label, size))
            icon.setContentTintColor_(_color(model.tint))
            icon.setAccessibilityLabel_(model.symbol_label or None)
            icon.setAccessibilityElement_(bool(model.symbol_label))  # decorative otherwise: VoiceOver skips it
        self._animate(model.animate and not reduce_motion())
        text.setHidden_(not model.text)
        text.setStringValue_(model.text)
        timer.setHidden_(not model.timer)
        timer.setStringValue_(model.timer)
        timer.setAccessibilityLabel_(model.timer_label or None)
        self._keys = [b.key for b in model.buttons]
        for i, view in enumerate(self._buttons):
            spec = model.buttons[i] if i < len(model.buttons) else None
            view.setHidden_(spec is None)
            if spec is None:
                continue
            view.setTitle_("" if spec.icon_only else spec.title)
            view.setImage_(self._image(spec.symbol, spec.label, 13.0, spec.tint) if spec.symbol else None)
            view.setImagePosition_(IMAGE_ONLY if spec.icon_only else IMAGE_LEADING)
            view.setToolTip_(spec.tooltip or (spec.label if spec.icon_only else None))
            view.setAccessibilityLabel_(spec.label)
            with contextlib.suppress(Exception):  # bezelColor: macOS 10.12.2
                view.setBezelColor_(AppKit.NSColor.controlAccentColor() if spec.prominent else None)
            width = ICON_BUTTON_WIDTH if spec.icon_only else 0.0
            for c in list(view.constraints()):
                if c.identifier() == "voicestream.width":
                    view.removeConstraint_(c)
            if width:
                c = view.widthAnchor().constraintGreaterThanOrEqualToConstant_(width)
                c.setIdentifier_("voicestream.width")
                c.setActive_(True)
        tip = model.tooltip or None
        v["effect"].setToolTip_(tip)
        text.setToolTip_(tip)
        self.panel.setAccessibilityLabel_(STRINGS.get(self.lang, STRINGS["en"])["panel"])

    def tick(self, model: PanelModel) -> None:
        """Once a second while recording: only the timer changes (the rest is drawn already)."""
        if self.model is None or self.panel is None or model.kind != self.model.kind:
            return
        if model.timer != self.model.timer:
            self._views["timer"].setStringValue_(model.timer)
            self._views["timer"].setAccessibilityLabel_(model.timer_label or None)
            self.model = model

    def _animate(self, on: bool) -> None:
        """The recording dot breathes (macOS 15; pulses on 14) — unless Reduce Motion is on."""
        if on == self._animating:
            return
        icon = self._views["icon"]
        if not on:
            self._stop_animation()
            return
        try:
            import objc

            effect = None
            for name in ("NSSymbolBreatheEffect", "NSSymbolPulseEffect"):
                with contextlib.suppress(objc.error):
                    effect = objc.lookUpClass(name).effect()
                    break
            if effect is not None:
                icon.addSymbolEffect_(effect)
                self._animating = True
        except Exception:  # an older macOS: the dot stays still
            log.debug("no symbol effect", exc_info=True)

    def _stop_animation(self) -> None:
        if self._animating:
            with contextlib.suppress(Exception):
                self._views["icon"].removeAllSymbolEffects()
            self._animating = False

    def _fitting_size(self) -> tuple[float, float]:
        effect = self._views["effect"]
        effect.layoutSubtreeIfNeeded()
        fit = self._views["stack"].fittingSize()
        return float(max(fit.width, HEIGHT)), float(max(fit.height, HEIGHT))

    def _place(self, origin: tuple[float, float], size: tuple[float, float]) -> None:
        self._placing = True  # our own move: not a drag to remember
        try:
            self.panel.setFrame_display_((origin, size), True)
            self.panel.invalidateShadow()
        finally:
            self._placing = False

    # ------------------------------------------------------------ events

    def clicked(self, index: int) -> None:
        if 0 <= index < len(self._keys):
            self.on_action(self._keys[index])

    def moved(self) -> None:
        """``windowDidMove:``: after a drag, save the place once it has settled."""
        if self._placing or self.panel is None:
            return
        import Foundation

        if self._save_timer is not None:
            self._save_timer.invalidate()
        self._save_timer = Foundation.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            SAVE_AFTER_S, self._target, "savePosition:", None, False)

    def save_position(self) -> None:
        self._save_timer = None
        if self.panel is None:
            return
        x, y, _w, _h = _rect(self.panel.frame())
        name = None
        with contextlib.suppress(Exception):
            sc = self.panel.screen()
            name = str(sc.localizedName()) if sc is not None else None
        self.on_moved({"x": round(x, 1), "y": round(y, 1), "screen": name[:200] if name else None})

    def screens_changed(self) -> None:
        """A display was connected, removed or rearranged: keep the panel whole on one."""
        if self.panel is None or not self.panel.isVisible():
            return
        x, y, w, h = _rect(self.panel.frame())
        self._place(place((w, h), {"x": x, "y": y, "screen": None}, screens()), (w, h))

    # ------------------------------------------------------------ self-test

    def info(self) -> dict[str, Any]:
        """What the self-test checks: on screen, on every Space and next to full-screen apps, floating,
        never key (so never takes the focus), not hidden when the app is inactive, and its context menu."""
        p = self.panel
        if p is None:
            return {"created": False}
        behavior, style = int(p.collectionBehavior()), int(p.styleMask())
        _x, _y, w, h = _rect(p.frame())
        return {
            "created": True,
            "visible": bool(p.isVisible()),
            "on_active_space": bool(p.isOnActiveSpace()),
            "all_spaces": bool(behavior & JOIN_ALL_SPACES),
            "full_screen_auxiliary": bool(behavior & FULL_SCREEN_AUXILIARY),
            "nonactivating": bool(style & STYLE_NONACTIVATING),
            "floating": bool(p.isFloatingPanel()) and int(p.level()) >= FLOATING_LEVEL,
            "can_become_key": bool(p.canBecomeKeyWindow()),
            "hides_on_deactivate": bool(p.hidesOnDeactivate()),
            "size": [round(w), round(h)],
            "context_menu": [str(i.title()) for i in (self._menu.itemArray() if self._menu is not None else [])
                             if not i.isSeparatorItem()],
            "kind": self.model.kind if self.model is not None else None,
        }

    def drawn(self) -> dict[str, Any]:
        """The model on show as drawn: its size and every visible button's (each at least :data:`MIN_TARGET`
        tall, with a VoiceOver label)."""
        out: dict[str, Any] = {"size": list(self._fitting_size()), "buttons": []}
        self._views["effect"].layoutSubtreeIfNeeded()
        for view in self._buttons:
            if view.isHidden():
                continue
            frame = _rect(view.frame())
            out["buttons"].append({"height": frame[3], "width": frame[2], "label": str(view.accessibilityLabel() or "")})
        return out


def _color(tint: str) -> Any:
    import AppKit

    c = AppKit.NSColor
    return {"red": c.systemRedColor, "yellow": c.systemYellowColor, "green": c.systemGreenColor,
            "accent": c.controlAccentColor}.get(tint, c.secondaryLabelColor)()


def _announce(element: Any, text: str) -> None:
    """Say ``text`` to VoiceOver (NSAccessibility.h: NSAccessibilityAnnouncementRequestedNotification, at
    high priority: it appeared without the focus)."""
    try:
        import AppKit

        AppKit.NSAccessibilityPostNotificationWithUserInfo(
            element, AppKit.NSAccessibilityAnnouncementRequestedNotification,
            {AppKit.NSAccessibilityAnnouncementKey: text,
             AppKit.NSAccessibilityPriorityKey: getattr(AppKit, "NSAccessibilityPriorityHigh", 90)})
    except Exception:  # VoiceOver misses it; the panel still shows it
        log.debug("could not announce %r", text, exc_info=True)


def kinds(states: Iterable[dict[str, Any]], lang: str = "en") -> list[str]:
    """The kinds of model the given states draw (the self-test's: it draws each of :func:`sample_states`)."""
    return [panel_model(st, lang).kind for st in states]
