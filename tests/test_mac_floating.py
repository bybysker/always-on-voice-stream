"""The floating button (voicestream.app.floating): what it shows for each host state, where it sits, and its
AppKit panel driven through a small stand-in for AppKit — enough to run the real code that builds, updates,
places and reports it. The real panel is drawn on the macOS CI runner by the packaged app's ``--selftest``
(on every Space, non-activating, never key, every look drawn with 28-point buttons).
"""

from __future__ import annotations

import sys
import types
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from voicestream.app import floating, hotkey
from voicestream.app.floating import PanelModel, Screen

NOW = datetime(2026, 10, 22, 10, 0, tzinfo=UTC)


def _st(state: str = "stopped", **extra: Any) -> dict[str, Any]:
    return {"state": state, "mode": "manual", "setup_done": True, "in_meeting": False, "meeting": None, "error": None,
            "suggestion": None, "still_there": None, "notice": None, **extra}


def _meeting(minutes: float) -> dict[str, Any]:
    return {"in_meeting": True, "meeting": {"id": "m1", "title": "", "started_at": (NOW - timedelta(minutes=minutes)).isoformat()}}


def _keys(model: PanelModel) -> list[str]:
    return [b.key for b in model.buttons]


# ------------------------------------------------------------------ the model


def test_idle_shows_start_meeting() -> None:
    m = floating.panel_model(_st(), "en", NOW)
    assert m.kind == "idle" and _keys(m) == ["start"] and not m.text and not m.symbol
    start = m.buttons[0]
    assert (start.title, start.symbol, start.tint, start.icon_only) == ("Start meeting", "record.circle", "red", False)
    assert start.tooltip == "Start a meeting: Voicestream records until you stop it"
    with_shortcut = floating.panel_model(_st(), "en", NOW, shortcut="⌥⌘R").buttons[0]
    assert with_shortcut.tooltip.endswith("(⌥⌘R)")  # the shortcut taught where you look
    fr = floating.panel_model(_st(), "fr", NOW).buttons[0]
    assert fr.title == "Démarrer une réunion"
    auto = floating.panel_model(_st(mode="automatic"), "en", NOW).buttons[0]
    assert (auto.title, auto.symbol, auto.tint) == ("Start listening", "mic", "")


def test_recording_paused_and_listening() -> None:
    rec = floating.panel_model(_st("listening", **_meeting(12.5)), "en", NOW)
    assert rec.kind == "recording" and rec.timer == "12:30" and rec.timer_label == "Meeting time 12:30"
    assert (rec.symbol, rec.tint, rec.animate, rec.symbol_label) == ("circle.fill", "red", True, "Recording")
    assert _keys(rec) == ["pause", "stop"] and all(b.icon_only and b.label for b in rec.buttons)
    assert rec.buttons[1].label == "Stop meeting" and "notes are written" in rec.buttons[1].tooltip
    paused = floating.panel_model(_st("paused", **_meeting(3)), "en", NOW)
    assert paused.kind == "paused" and paused.text == "Paused" and _keys(paused) == ["resume", "stop"]
    assert paused.timer == "3:00" and not paused.animate  # nothing is recorded: the dot doesn't breathe
    auto = floating.panel_model(_st("listening", mode="automatic"), "en", NOW)
    assert auto.kind == "listening" and auto.text == "Listening" and auto.buttons[1].label == "Stop listening"
    hour = floating.panel_model(_st("listening", **_meeting(61)), "en", NOW)
    assert hour.timer == "1:01:00"


def test_starting_stopping_and_problems() -> None:
    starting = floating.panel_model(_st("starting"), "en", NOW)
    assert starting.text == "Starting…" and _keys(starting) == ["stop"]  # a start under way may be cancelled
    stopping = floating.panel_model(_st("stopping"), "en", NOW)
    assert stopping.text == "Saving notes…" and stopping.buttons == ()
    assert floating.panel_model(_st("stopping", mode="automatic"), "en", NOW).text == "Stopping…"
    err = floating.panel_model(_st("error", error="Microphone permission is off:\n System Settings"), "en", NOW)
    assert err.kind == "error" and err.tint == "yellow" and _keys(err) == ["show", "retry"]
    assert err.tooltip == "Microphone permission is off: System Settings"  # one line


def test_french_says_recording_only_while_it_records() -> None:
    """« Enregistrement » is the app's French word for Recording (the dot's VoiceOver label, the window's state):
    after Stop, the wrap-up is said as the window says it, or « Enregistrement des notes… » reads as still recording."""
    assert floating.panel_model(_st("listening", **_meeting(1)), "fr", NOW).symbol_label == "Enregistrement"
    stopping = floating.panel_model(_st("stopping"), "fr", NOW)
    assert stopping.text == "Rédaction des notes…"
    for key, text in floating.STRINGS["fr"].items():
        if key not in ("recording", "problem", "link_refused"):  # Recording, Couldn't record, a link that tried to
            assert "nregistrement" not in text, (key, text)


def test_call_suggestion_nudge_and_notices() -> None:
    sug = floating.panel_model(_st(suggestion={"app": "Zoom", "at": "x"}), "en", NOW)
    assert sug.kind == "suggestion" and sug.text == "Call detected — start notes?" and sug.tooltip == "Zoom is using the microphone"
    assert _keys(sug) == ["accept", "dismiss"] and sug.buttons[0].prominent and not sug.buttons[1].prominent
    assert sug.announce == sug.text  # said to VoiceOver: it appears without the focus
    assert floating.panel_model(_st(suggestion={"app": None}), "en", NOW).tooltip == ""
    fr = floating.panel_model(_st(suggestion={"app": "Microsoft Teams"}), "fr", NOW)
    assert (fr.text, fr.tooltip, [b.title for b in fr.buttons]) == (
        "Appel détecté — prendre des notes ?", "Microsoft Teams utilise le micro", ["Démarrer", "Pas maintenant"])
    # the window's word for the same dismiss (sug.dismiss): this call is never suggested again, so not "later"
    assert floating.STRINGS["fr"]["not_now"] == "Pas maintenant"
    still = floating.panel_model(_st("listening", **_meeting(20), still_there={"meeting_id": "m1", "silent_minutes": 15}),
                                 "en", NOW)
    assert still.kind == "still_there" and still.text == "Still in a meeting?" and _keys(still) == ["keep", "end"]
    assert still.tooltip == "No one has spoken for 15 min" and still.announce
    # automatic meetings end on a silence by design: never asked
    auto = _st("listening", mode="automatic", **_meeting(20), still_there={"meeting_id": "m1", "silent_minutes": 15})
    assert floating.panel_model(auto, "en", NOW).kind == "recording"
    ended = floating.panel_model(_st(notice={"kind": "ended_after_silence", "minutes": 60}), "en", NOW)
    assert ended.text == "Meeting ended after 60 min of silence" and _keys(ended) == ["start", "ok"]
    refused = floating.panel_model(_st(notice={"kind": "link_refused"}), "fr", NOW)
    assert refused.text == "Un lien voulait enregistrer — ignoré" and _keys(refused) == ["start", "ok"]
    assert floating.panel_model(_st(notice={"kind": "something new"}), "en", NOW).kind == "idle"
    # while recording, the recording is what shows
    assert floating.panel_model(_st("listening", **_meeting(1), suggestion={"app": "Zoom"}), "en", NOW).kind == "recording"


def test_a_notice_keeps_start_meeting_one_click_away() -> None:
    """A notice stays until OK, for hours after a forgotten meeting ended in the night: the floating button,
    whose job is to start a meeting, keeps its Start next to it (starting clears the notice, host-side)."""
    for mode in ("manual", "automatic"):
        idle = floating.panel_model(_st(mode=mode), "en", NOW, shortcut="⌥⌘R").buttons[0]
        for notice in ({"kind": "ended_after_silence", "minutes": 60}, {"kind": "link_refused"}):
            m = floating.panel_model(_st(mode=mode, notice=notice), "en", NOW, shortcut="⌥⌘R")
            assert m.kind == "notice" and m.buttons[0] == idle and m.buttons[1].key == "ok"
            assert not any(b.prominent for b in m.buttons)  # neither is the one to press


def test_a_meeting_that_ended_by_itself_says_its_notes_are_being_written() -> None:
    """The host's notice says ``pending`` from the moment the meeting ends until its notes are written (a
    summary takes tens of seconds): "its notes are saved" would be said of notes not there yet."""
    writing = {"kind": "ended_after_silence", "minutes": 60, "pending": True, "title": None, "folder": None}
    m = floating.panel_model(_st(notice=writing), "en", NOW)
    assert m.text == "Meeting ended: its notes are being written…" and m.announce == m.text
    assert m.tooltip == "It ended after 60 min of silence. Its notes are being written."
    assert m.symbol == "square.and.arrow.down" and _keys(m) == ["start", "ok"]
    fr = floating.panel_model(_st(notice=writing), "fr", NOW)
    assert fr.text == "Réunion terminée : rédaction des notes…" and "60 min" in fr.tooltip
    done = floating.panel_model(_st(notice={**writing, "pending": False, "folder": "2026-10-22_0900-sync"}), "en", NOW)
    assert (done.text, done.tooltip, done.symbol) == ("Meeting ended after 60 min of silence",
                                                     "Its notes are saved: Voicestream → Meetings", "checkmark.circle")
    empty = floating.panel_model(_st(notice={**writing, "pending": False}), "en", NOW)  # nothing was said
    assert empty.text == done.text and empty.tooltip == "Nothing had been said, so there are no notes"
    assert floating.panel_model(_st(notice={**writing, "pending": False}), "fr", NOW).tooltip == (
        "Rien n'avait été dit, il n'y a donc pas de notes")
    # a notice without the field (as the samples build it) is a finished one, as before
    legacy = floating.panel_model(_st(notice={"kind": "ended_after_silence", "minutes": 60}), "en", NOW)
    assert (legacy.text, legacy.tooltip) == (done.text, done.tooltip)


def test_paused_shows_resume_even_with_a_nudge_up() -> None:
    """Paused, nothing is recorded and paused time isn't silence: Resume must be there, not Keep / End."""
    nudge = {"meeting_id": "m1", "silent_minutes": 16}
    paused = floating.panel_model(_st("paused", **_meeting(20), still_there=nudge), "en", NOW)
    assert paused.kind == "paused" and _keys(paused) == ["resume", "stop"]
    assert floating.panel_model(_st("listening", **_meeting(20), still_there=nudge), "en", NOW).kind == "still_there"


def test_the_shortcut_is_named_only_next_to_what_a_press_does() -> None:
    """hotkey.TOGGLE: a press stops while recording, resumes while paused and does nothing while starting —
    a Stop tooltip naming it while paused would send you to resume a meeting you meant to end."""
    sc = "⌥⌘R"
    rec = floating.panel_model(_st("listening", **_meeting(2)), "en", NOW, shortcut=sc)
    assert rec.buttons[1].key == "stop" and rec.buttons[1].tooltip.endswith("(⌥⌘R)")
    tips = {b.key: b.tooltip for b in floating.panel_model(_st("paused", **_meeting(2)), "en", NOW, shortcut=sc).buttons}
    assert tips["resume"] == "Resume (⌥⌘R)" and sc not in tips["stop"] and tips["stop"].startswith("Stop: the notes")
    starting = floating.panel_model(_st("starting"), "en", NOW, shortcut=sc)
    assert _keys(starting) == ["stop"] and sc not in starting.buttons[0].tooltip
    does = {"start": ("start", "restart"), "accept": ("start", "restart"), "retry": ("restart",),
            "resume": ("resume",), "stop": ("stop",)}
    for lang in ("en", "fr"):
        for name, st in floating.sample_states().items():
            press = hotkey.TOGGLE.get(st.get("state") or "stopped")
            for b in floating.panel_model(st, lang, NOW, shortcut=sc).buttons:
                if sc in b.tooltip:
                    assert press in does[b.key], (name, b.key, press)


def test_both_languages_say_everything() -> None:
    assert set(floating.STRINGS["en"]) == set(floating.STRINGS["fr"])
    for lang in ("en", "fr"):
        for name, st in floating.sample_states().items():
            m = floating.panel_model(st, lang, NOW)
            for b in m.buttons:
                assert b.title and b.label, (lang, name, b)
                assert len(b.title) <= 24, b.title  # a compact panel: short verbs
            # one line of at most TEXT_MAX_WIDTH (300 pt) at 13 pt, cut at its end otherwise: about 45 characters
            assert len(m.text) <= 45, m.text
    assert sorted(set(floating.kinds(floating.sample_states().values()))) == sorted(
        {"idle", "recording", "paused", "listening", "starting", "stopping", "suggestion", "still_there", "notice", "error"})


def test_when_it_shows() -> None:
    on = {"prefs": {"floating_button": True}, "setup_done": True, "capabilities": {"floating_button": True}}
    assert floating.wanted(on)
    assert not floating.wanted(on, quitting=True)
    assert not floating.wanted({**on, "prefs": {"floating_button": False}})
    assert not floating.wanted({**on, "setup_done": False})  # the window (the setup) is all there is until then
    assert not floating.wanted({**on, "capabilities": {}})  # the browser: no native panel
    assert floating.wanted({**on, "prefs": {}})  # on by default


def test_hidden_it_still_asks_what_nothing_else_would_show() -> None:
    """Hidden (the setting off) with the window closed, a call suggestion, "Still in a meeting?" and a meeting
    that ended by itself would be shown nowhere: the floating button appears for them, with that look only."""
    off = {"prefs": {"floating_button": False}, "capabilities": {"floating_button": True}}

    def st(state: str = "stopped", **extra: Any) -> dict[str, Any]:
        return {**_st(state, **extra), **off}

    assert not floating.wanted(st()) and not floating.wanted(st("listening", **_meeting(3)))  # nothing to ask
    sug = st(suggestion={"app": "Zoom", "at": "t1"})
    assert floating.wanted(sug) and floating.panel_model(sug, "en", NOW).kind == "suggestion"
    nudge = {"meeting_id": "m1", "silent_minutes": 15, "ends_at": "t2"}
    still = st("listening", **_meeting(20), still_there=nudge)
    assert floating.wanted(still) and floating.panel_model(still, "en", NOW).kind == "still_there"
    assert not floating.wanted({**still, "state": "paused"})  # paused: nothing to keep or end (Resume is for later)
    assert not floating.wanted({**still, "mode": "automatic"})  # automatic meetings are never asked
    ended = st("stopping", notice={"kind": "ended_after_silence", "minutes": 60, "pending": True, "at": "t3",
                                   "meeting_id": "m1"})
    m = floating.panel_model(ended, "fr", NOW)
    assert floating.wanted(ended) and m.kind == "notice" and m.text == "Réunion terminée : rédaction des notes…"
    assert floating.panel_model({**ended, "prefs": {}}, "en", NOW).kind == "stopping"  # shown anyway: as before
    assert floating.wanted({**ended, "state": "stopped"})
    assert not floating.wanted({**ended, "state": "starting"})  # the next meeting is under way
    assert not floating.wanted(st(notice={"kind": "link_refused", "at": "t4"}))  # the failure sound said that one
    # hidden from its own menu while one is up: not for that one again, but for the next
    key = floating.question_key(sug)
    assert key and floating.question_key(st()) is None and floating.question_key(still) != key
    assert not floating.wanted(sug, hidden_for=key)
    assert floating.wanted(st(suggestion={"app": "Teams", "at": "t5"}), hidden_for=key)
    assert floating.wanted({**sug, "prefs": {}}, hidden_for=key)  # on: always shown
    asked_again = st("listening", **_meeting(70), still_there={**nudge, "ends_at": "t6"})  # asked again after a pause
    assert floating.wanted(asked_again, hidden_for=floating.question_key(still))
    assert not floating.wanted({**sug, "setup_done": False}) and not floating.wanted(sug, quitting=True)
    assert not floating.wanted({**sug, "capabilities": {}})


# ------------------------------------------------------------------ placement

MAIN = Screen("Built-in Retina Display", (0, 0, 1512, 982), (0, 0, 1512, 950))
SIDE = Screen("DELL U2720Q", (1512, 0, 2560, 1440), (1512, 0, 2560, 1415))
SIZE = (180.0, 44.0)


def test_default_place_is_top_right_below_the_banners() -> None:
    assert floating.place(SIZE, None, [MAIN, SIDE]) == (1512 - 180 - 16, 950 - 44 - 96)
    assert floating.place(SIZE, None, []) == (0.0, 0.0)


def test_a_saved_place_is_kept_whole_on_its_screen() -> None:
    saved = {"x": 2000.0, "y": 600.0, "screen": "DELL U2720Q"}
    assert floating.place(SIZE, saved, [MAIN, SIDE]) == (2000.0, 600.0)
    half_off = {"x": 4000.0, "y": 1430.0, "screen": "DELL U2720Q"}
    assert floating.place(SIZE, half_off, [MAIN, SIDE]) == (1512 + 2560 - 180, 1415 - 44)  # pulled back in
    # the external display is gone: back to the default place on the main screen
    assert floating.place(SIZE, saved, [MAIN]) == floating.place(SIZE, None, [MAIN])
    # still connected but arranged elsewhere (now on the left): kept on it
    moved = Screen("DELL U2720Q", (-2560, 0, 2560, 1440), (-2560, 0, 2560, 1415))
    assert floating.place(SIZE, saved, [MAIN, moved]) == (-180.0, 600.0)


def test_resizing_grows_away_from_the_nearer_edge() -> None:
    right = (1300.0, 800.0, 180.0, 44.0)
    assert floating.resized(right, (300.0, 44.0), [MAIN]) == (1180.0, 800.0)  # the right edge stays
    left = (40.0, 800.0, 180.0, 44.0)
    assert floating.resized(left, (300.0, 44.0), [MAIN]) == (40.0, 800.0)
    assert floating.resized(left, (300.0, 60.0), [MAIN]) == (40.0, 784.0)  # the top edge stays
    assert floating.clamp((-50.0, -50.0), SIZE, MAIN.visible) == (0.0, 0.0)


# ------------------------------------------------------------------ a stand-in for AppKit


class Rect:
    def __init__(self, x: float, y: float, w: float, h: float) -> None:
        self.origin = types.SimpleNamespace(x=x, y=y)
        self.size = types.SimpleNamespace(width=w, height=h)


class Fake:
    """Any AppKit object: ``setFoo_(v)`` keeps ``foo``, ``foo()`` gives it back; other calls are recorded."""

    def __init__(self, **values: Any) -> None:
        self.values: dict[str, Any] = dict(values)
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    @classmethod
    def alloc(cls) -> Any:
        return cls()

    def init(self) -> Any:
        return self

    def initWithFrame_(self, rect: Any) -> Any:
        self.values["frame"] = rect
        return self

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__") or name == "values":
            raise AttributeError(name)
        if name.startswith("set") and name.endswith("_") and name.count("_") == 1:
            key = name[3].lower() + name[4:-1]

            def setter(value: Any) -> None:
                self.calls.append((name, (value,)))
                self.values[key] = value

            return setter

        def method(*args: Any) -> Any:
            self.calls.append((name, args))
            if not args and name in self.values:
                return self.values[name]
            return Fake()

        return method

    def names(self) -> list[str]:
        return [n for n, _ in self.calls]


def _width(view: Any) -> float:
    if isinstance(view, FakeButton):
        return 34.0 if not view.values.get("title") else 24.0 + 7 * len(view.values["title"])
    if isinstance(view, FakeLabel):
        return 7.0 * len(view.values.get("stringValue", ""))
    return 18.0


class FakeButton(Fake):
    def frame(self) -> Rect:
        return Rect(0, 0, _width(self), 28.0)

    def isHidden(self) -> bool:
        return bool(self.values.get("hidden"))

    def accessibilityLabel(self) -> str:
        return self.values.get("accessibilityLabel", "")

    def constraints(self) -> list[Any]:
        return []


class FakeLabel(Fake):
    @classmethod
    def labelWithString_(cls, text: str) -> Any:
        label = cls()
        label.values["stringValue"] = text
        return label


class FakeStack(Fake):
    def __init__(self, views: list[Any]) -> None:
        super().__init__()
        self.views = views

    def fittingSize(self) -> Any:
        shown = [v for v in self.views if not v.values.get("hidden")]
        width = 12 + 8 + sum(_width(v) for v in shown) + 8 * max(0, len(shown) - 1)
        return types.SimpleNamespace(width=width, height=44.0)


class FakePanel(Fake):
    def initWithContentRect_styleMask_backing_defer_(self, rect: Any, style: int, backing: int, defer: bool) -> Any:
        self.values.update(styleMask=style, frame=Rect(rect[0][0], rect[0][1], rect[1][0], rect[1][1]), isVisible=False)
        return self

    def orderFrontRegardless(self) -> None:
        self.calls.append(("orderFrontRegardless", ()))
        self.values["isVisible"] = True

    def orderOut_(self, sender: Any) -> None:
        self.calls.append(("orderOut_", (sender,)))
        self.values["isVisible"] = False

    def setFrame_display_(self, rect: Any, display: bool) -> None:
        (x, y), (w, h) = rect
        self.calls.append(("setFrame_display_", (rect, display)))
        self.values["frame"] = Rect(x, y, w, h)
        delegate = self.values.get("delegate")
        if delegate is not None:
            delegate.windowDidMove_(None)  # as AppKit tells its delegate

    def isFloatingPanel(self) -> bool:
        return bool(self.values.get("floatingPanel"))

    def canBecomeKeyWindow(self) -> bool:
        return False  # borderless: AppKit's own answer

    def isOnActiveSpace(self) -> bool:
        return True

    def screen(self) -> Any:
        return types.SimpleNamespace(localizedName=lambda: "Built-in Retina Display")


@pytest.fixture
def appkit(monkeypatch: pytest.MonkeyPatch) -> Any:
    timers: list[tuple[Any, ...]] = []
    announced: list[tuple[Any, ...]] = []
    observers: list[tuple[Any, ...]] = []
    reduce = {"on": False}
    effects: list[str] = []

    def screen(sc: Screen) -> Any:
        return types.SimpleNamespace(localizedName=lambda: sc.name, frame=lambda: Rect(*sc.frame),
                                     visibleFrame=lambda: Rect(*sc.visible))

    def color(name: str) -> Any:
        return lambda: f"color:{name}"

    class Effect:
        @staticmethod
        def effect() -> str:
            return "breathe"

    def look_up(name: str) -> Any:
        if name == "NSSymbolBreatheEffect":
            return Effect
        raise ObjcError(name)

    class ObjcError(Exception):  # objc.error
        pass

    class NSObject(Fake):
        pass

    def typed(signature: bytes) -> Any:
        return lambda fn: fn

    center = Fake()
    center.addObserver_selector_name_object_ = lambda *a: observers.append(a)
    ak = types.SimpleNamespace(
        NSButton=FakeButton, NSTextField=FakeLabel, NSPanel=FakePanel, NSVisualEffectView=Fake, NSImageView=Fake,
        NSStackView=types.SimpleNamespace(stackViewWithViews_=lambda views: FakeStack(list(views))),
        NSLayoutConstraint=types.SimpleNamespace(activateConstraints_=lambda cs: None),
        NSColor=types.SimpleNamespace(clearColor=color("clear"), labelColor=color("label"),
                                      secondaryLabelColor=color("secondary"), systemRedColor=color("red"),
                                      systemYellowColor=color("yellow"), systemGreenColor=color("green"),
                                      controlAccentColor=color("accent")),
        NSFont=types.SimpleNamespace(systemFontSize=lambda: 13.0, systemFontOfSize_=lambda s: ("font", s),
                                     monospacedDigitSystemFontOfSize_weight_=lambda s, w: ("mono", s, w)),
        NSImage=types.SimpleNamespace(imageWithSystemSymbolName_accessibilityDescription_=lambda n, d: Fake(name=n)),
        NSImageSymbolConfiguration=types.SimpleNamespace(configurationWithPointSize_weight_=lambda s, w: Fake(),
                                                         configurationWithPaletteColors_=lambda c: Fake()),
        NSMenu=types.SimpleNamespace(alloc=lambda: types.SimpleNamespace(initWithTitle_=lambda t: FakeMenu())),
        NSMenuItem=types.SimpleNamespace(alloc=lambda: types.SimpleNamespace(
            initWithTitle_action_keyEquivalent_=lambda t, a, k: Fake(title=t, action=a, isSeparatorItem=False)),
            separatorItem=lambda: Fake(isSeparatorItem=True)),
        NSScreen=types.SimpleNamespace(screens=lambda: [screen(MAIN), screen(SIDE)]),
        NSNotificationCenter=types.SimpleNamespace(defaultCenter=lambda: center),
        NSWorkspace=types.SimpleNamespace(sharedWorkspace=lambda: types.SimpleNamespace(
            accessibilityDisplayShouldReduceMotion=lambda: reduce["on"])),
        NSAccessibilityPostNotificationWithUserInfo=lambda el, note, info: announced.append((note, dict(info))),
        NSAccessibilityAnnouncementRequestedNotification="AXAnnouncementRequested",
        NSAccessibilityAnnouncementKey="AXAnnouncementKey", NSAccessibilityPriorityKey="AXPriorityKey",
        NSFloatingWindowLevel=3,
    )
    foundation = types.SimpleNamespace(NSObject=NSObject, NSTimer=types.SimpleNamespace(
        scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_=lambda *a: timers.append(a) or Fake()))
    objc = types.SimpleNamespace(typedSelector=typed, lookUpClass=look_up, error=ObjcError)
    for name, module in (("AppKit", ak), ("Foundation", foundation), ("objc", objc)):
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(floating, "_CLASSES", {})
    original = Fake.__getattr__

    def record_effects(self: Any, name: str) -> Any:
        if name == "addSymbolEffect_":
            return lambda e: effects.append(e)
        return original(self, name)

    monkeypatch.setattr(Fake, "__getattr__", record_effects)
    return types.SimpleNamespace(timers=timers, announced=announced, observers=observers, reduce=reduce, effects=effects)


class FakeMenu(Fake):
    def __init__(self) -> None:
        super().__init__()
        self.items: list[Any] = []

    def addItem_(self, item: Any) -> None:
        self.items.append(item)

    def itemArray(self) -> list[Any]:
        return self.items


def _panel() -> tuple[floating.FloatingPanel, list[str], list[dict[str, Any]]]:
    actions: list[str] = []
    moves: list[dict[str, Any]] = []
    return floating.FloatingPanel(on_action=actions.append, on_moved=moves.append), actions, moves


def _visible_buttons(p: floating.FloatingPanel) -> list[Any]:
    return [b for b in p._buttons if not b.values.get("hidden")]


def test_the_panel_never_takes_the_focus_and_floats_on_every_space(appkit: Any) -> None:
    p, actions, moves = _panel()
    p.update(floating.panel_model(_st(), "en", NOW), lang="en")
    panel = p.panel.values
    assert panel["styleMask"] == floating.STYLE_BORDERLESS | floating.STYLE_NONACTIVATING
    assert panel["collectionBehavior"] & floating.JOIN_ALL_SPACES and panel["collectionBehavior"] & floating.FULL_SCREEN_AUXILIARY
    assert panel["level"] == 3 and panel["floatingPanel"] is True and panel["becomesKeyOnlyIfNeeded"] is True
    assert panel["hidesOnDeactivate"] is False and panel["canHide"] is False  # closing the window hides the app, not it
    assert panel["movableByWindowBackground"] is True and panel["allowsToolTipsWhenApplicationIsInactive"] is True
    assert "orderFrontRegardless" in p.panel.names() and "makeKeyAndOrderFront_" not in p.panel.names()
    assert moves == []  # placing it is not a drag to remember
    effect = p._views["effect"].values
    assert (effect["material"], effect["state"], effect["blendingMode"]) == (13, 1, 0)  # HUD, vibrant though inactive
    info = p.info()
    assert info["visible"] and info["all_spaces"] and info["full_screen_auxiliary"] and info["nonactivating"]
    assert info["floating"] and info["can_become_key"] is False and info["hides_on_deactivate"] is False
    assert info["context_menu"] == ["Hide Floating Button", "Open Voicestream", "Settings…"]
    x, y, w, h = floating._rect(p.panel.frame())
    assert (x, y) == floating.default_origin((w, h), MAIN) and h == floating.HEIGHT
    start = _visible_buttons(p)
    assert len(start) == 1 and start[0].values["title"] == "Start meeting"
    assert start[0].values["accessibilityLabel"] == "Start meeting" and start[0].values["imagePosition"] == floating.IMAGE_LEADING
    assert p.panel.values["accessibilityLabel"] == "Voicestream meeting controls"
    p.clicked(0)
    p.clicked(5)  # no such button: nothing
    assert actions == ["start"]
    for b in p._buttons:
        assert b.acceptsFirstMouse_(None) is True  # the first click acts: the panel is never key
    assert p._views["text"].mouseDownCanMoveWindow() is True  # dragged by its labels too


def test_the_panel_follows_the_state(appkit: Any) -> None:
    p, actions, moves = _panel()
    p.update(floating.panel_model(_st(), "en", NOW), lang="en")
    idle_frame = floating._rect(p.panel.frame())
    p.update(floating.panel_model(_st("listening", **_meeting(2)), "en", NOW), lang="en")
    rec = _visible_buttons(p)
    assert [b.values["accessibilityLabel"] for b in rec] == ["Pause", "Stop meeting"]
    assert all(b.values["title"] == "" and b.values["imagePosition"] == floating.IMAGE_ONLY for b in rec)
    assert all(b.values["toolTip"] for b in rec)  # icon-only: what they do on hover
    assert p._views["timer"].values["stringValue"] == "2:00" and not p._views["timer"].values["hidden"]
    assert appkit.effects == ["breathe"]  # the recording dot breathes
    frame = floating._rect(p.panel.frame())
    assert frame[0] + frame[2] == pytest.approx(idle_frame[0] + idle_frame[2])  # on the right: the right edge stays
    assert frame[1] + frame[3] == pytest.approx(idle_frame[1] + idle_frame[3])
    p.tick(floating.panel_model(_st("listening", **_meeting(2)), "en", NOW + timedelta(seconds=5)))
    assert p._views["timer"].values["stringValue"] == "2:05"
    p.tick(floating.panel_model(_st(), "en", NOW))  # another look: the next state update draws it, not the tick
    assert p.model is not None and p.model.kind == "recording"
    p.clicked(1)
    assert actions == ["stop"]
    p.update(floating.panel_model(_st(suggestion={"app": "Zoom"}), "en", NOW), lang="en")
    assert appkit.announced == [("AXAnnouncementRequested", {"AXAnnouncementKey": "Call detected — start notes?",
                                                             "AXPriorityKey": 90})]
    p.update(floating.panel_model(_st(suggestion={"app": "Zoom"}), "en", NOW), lang="en")
    assert len(appkit.announced) == 1  # once when it appears, not at every state
    assert p._views["effect"].values["toolTip"] == "Zoom is using the microphone"
    accept, dismiss = _visible_buttons(p)
    assert accept.values["bezelColor"] == "color:accent" and dismiss.values["bezelColor"] is None  # one primary
    p.clicked(1)
    assert actions[-1] == "dismiss"
    p.update(None)
    assert p.panel.values["isVisible"] is False and p.model is None
    assert moves == []


def test_reduce_motion_keeps_the_dot_still(appkit: Any) -> None:
    appkit.reduce["on"] = True
    p, _, _ = _panel()
    p.update(floating.panel_model(_st("listening", **_meeting(2)), "en", NOW), lang="en")
    assert appkit.effects == []


def test_dragging_saves_the_place_once_it_settles(appkit: Any) -> None:
    p, _, moves = _panel()
    p.update(floating.panel_model(_st(), "en", NOW), lang="en")
    assert appkit.timers == []
    p.moved()
    p.moved()  # the drag goes on: the timer starts again
    assert len(appkit.timers) == 2 and appkit.timers[0][0] == floating.SAVE_AFTER_S and appkit.timers[0][2] == "savePosition:"
    p.panel.values["frame"] = Rect(900.5, 300.25, 180, 44)
    p.save_position()
    assert moves == [{"x": 900.5, "y": 300.2, "screen": "Built-in Retina Display"}]
    # hidden, then shown again: where it was saved
    p.update(None)
    p.update(floating.panel_model(_st(), "en", NOW), saved={"x": 2000.0, "y": 500.0, "screen": "DELL U2720Q"}, lang="en")
    assert floating._rect(p.panel.frame())[:2] == (2000.0, 500.0)


def test_a_display_going_away_brings_it_back(appkit: Any) -> None:
    p, _, _ = _panel()
    p.update(floating.panel_model(_st(), "en", NOW), saved={"x": 2000.0, "y": 500.0, "screen": "DELL U2720Q"}, lang="en")
    assert ("screensChanged:", "NSApplicationDidChangeScreenParametersNotification") == appkit.observers[0][1:3]
    sys.modules["AppKit"].NSScreen.screens = lambda: [types.SimpleNamespace(
        localizedName=lambda: MAIN.name, frame=lambda: Rect(*MAIN.frame), visibleFrame=lambda: Rect(*MAIN.visible))]
    p.screens_changed()
    x, y, w, h = floating._rect(p.panel.frame())
    assert (x, y) == floating.default_origin((w, h), MAIN)


def test_the_context_menu(appkit: Any) -> None:
    p, actions, _ = _panel()
    p.update(floating.panel_model(_st(), "fr", NOW), lang="fr")
    menu = p._menu
    titles = [i.values.get("title") for i in menu.items if not i.values.get("isSeparatorItem")]
    assert titles == ["Masquer le bouton flottant", "Ouvrir Voicestream", "Réglages…"]
    for view in (*p._views.values(), *p._buttons):
        assert view.values["menu"] is menu  # a right click anywhere on it
    hide = menu.items[0]
    p._target.contextAction_(types.SimpleNamespace(representedObject=lambda: hide.values["representedObject"]))
    assert actions == ["hide"]
    p._target.panelAction_(types.SimpleNamespace(tag=lambda: 0))
    assert actions == ["hide", "start"]
    p.update(floating.panel_model(_st(), "en", NOW), lang="en")
    english = p._menu
    assert english is not menu  # the other language
    p.update(floating.panel_model(_st(), "en", NOW), lang="en")
    assert p._menu is english  # not made again at every state


def test_drawn_reports_every_button(appkit: Any) -> None:
    p, _, _ = _panel()
    for name, st in floating.sample_states().items():
        model = floating.panel_model(st, "en", NOW)
        p.update(model, lang="en")
        drawn = p.drawn()
        assert len(drawn["buttons"]) == len(model.buttons), name
        assert all(b["height"] >= floating.MIN_TARGET and b["label"] for b in drawn["buttons"]), name
        assert drawn["size"][1] >= floating.HEIGHT
    assert floating.FloatingPanel(on_action=print, on_moved=print).info() == {"created": False}


def test_an_exception_never_crosses_into_appkit(appkit: Any) -> None:
    p, _, _ = _panel()
    p.update(floating.panel_model(_st(), "en", NOW), lang="en")

    def broken(key: str) -> None:
        raise RuntimeError("boom")

    p.on_action = broken
    p._target.panelAction_(types.SimpleNamespace(tag=lambda: 0))  # logged, not raised
