"""Call suggestions: which app a capture belongs to, and when a call starts and ends (voicestream.app.calls).

Core Audio is replaced by a fake probe on a fake clock; the real one is called on the macOS CI runner by
the packaged app's ``--selftest`` (its mic-in-use probe must answer).
"""

from __future__ import annotations

import ctypes
import itertools
import sys
import threading
import time
from typing import Any

import pytest

from voicestream.app import calls
from voicestream.app.calls import Call, Capture


def test_four_character_codes_are_the_headers() -> None:
    """AudioHardware.h / AudioHardwareBase.h, as the SDK writes them."""
    assert calls.SCOPE_GLOBAL == 0x676C6F62  # 'glob'
    assert calls.DEFAULT_INPUT == 0x64496E20  # 'dIn '
    assert calls.RUNNING_SOMEWHERE == 0x676F6E65  # 'gone'
    assert calls.PROCESS_LIST == 0x70727323  # 'prs#'
    assert (calls.PROCESS_PID, calls.PROCESS_BUNDLE_ID, calls.PROCESS_RUNNING_INPUT) == (0x70706964, 0x70626964, 0x70697269)
    assert (calls.SYSTEM_OBJECT, calls.UNKNOWN_OBJECT, calls.ELEMENT_MAIN, calls.NO_ERR) == (1, 0, 0, 0)
    assert ctypes.sizeof(calls.PropertyAddress) == 12  # three UInt32


@pytest.mark.parametrize(
    ("bundle_id", "name"),
    [("us.zoom.xos", "Zoom"), ("com.microsoft.teams2", "Microsoft Teams"), ("com.apple.FaceTime", "FaceTime"),
     ("com.apple.avconferenced", "FaceTime"), ("com.tinyspeck.slackmacgap", "Slack"), ("com.hnc.Discord", "Discord"),
     ("com.google.Chrome.helper", "Google Chrome"), ("com.google.Chrome.app.kjgfgldnnfoeklkmfkjfagphfepbbdan", "Google Chrome"),
     ("com.apple.WebKit.GPU", "Safari"), ("com.cisco.webexmeetingsapp", "Webex"), ("org.example.recorder", None), (None, None)],
)
def test_app_names(bundle_id: str | None, name: str | None) -> None:
    assert calls.app_name(bundle_id) == name


def test_what_is_never_a_call() -> None:
    for bid in ("com.apple.SiriNCService", "com.apple.corespeechd", "com.apple.VoiceMemos", "com.superduper.superwhisper",
                "com.goodsnooze.MacWhisper", "com.prakashjoshipax.VoiceInk"):
        assert calls.is_background(bid), bid
    for bid in ("com.apple.FaceTime", "com.apple.WebKit.GPU", "us.zoom.xos", "org.example.recorder", None):
        assert not calls.is_background(bid), bid


def test_likely_app_when_core_audio_cannot_say() -> None:
    assert calls.likely_app("com.google.Chrome", ["us.zoom.xos"]) == "Google Chrome"  # Meet in the frontmost browser
    assert calls.likely_app("com.apple.finder", ["com.google.Chrome", "us.zoom.xos"]) == "Zoom"  # a browser only in front
    assert calls.likely_app(None, ["com.apple.finder"]) is None


def test_calls_in_a_look() -> None:
    caps = (Capture(10, "com.google.Chrome.helper"), Capture(11, "com.google.Chrome.helper"), Capture(12, "com.apple.corespeechd"),
            Capture(13, "org.example.recorder"), Capture(14, None))
    found = calls.calls_in(caps, True)
    assert set(found) == {"google chrome", "org.example.recorder", "pid 14"}  # one per app, Siri left out
    assert found["google chrome"] == Call("google chrome", "Google Chrome", "com.google.Chrome.helper", 10)
    assert calls.calls_in(None, True) == {"mic": Call("mic")} and calls.calls_in(None, False) == {}


class FakeProbe:
    """Core Audio: the default input's flag, and the processes capturing — counted, as each read costs."""

    def __init__(self) -> None:
        self.running: bool | None = False
        self.caps: tuple[Capture, ...] | None = ()
        self.reads = 0

    def device_running(self) -> bool | None:
        return self.running

    def captures(self) -> tuple[Capture, ...] | None:
        self.reads += 1
        return self.caps


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _watcher() -> tuple[calls.CallWatcher, FakeProbe, Clock, list[tuple[str, Call]]]:
    seen: list[tuple[str, Call]] = []
    probe, clock = FakeProbe(), Clock()
    w = calls.CallWatcher(on_start=lambda c: seen.append(("start", c)), on_end=lambda c: seen.append(("end", c)),
                          probe=probe, clock=clock)
    w.reset(baseline=False)
    return w, probe, clock, seen


def _run(w: calls.CallWatcher, clock: Clock, seconds: float, step: float = 2.0) -> None:
    end = clock.now + seconds
    while clock.now < end:
        w.step()
        clock.now += step


ZOOM = Capture(501, "us.zoom.xos")


def test_a_call_is_announced_once_then_ended() -> None:
    w, probe, clock, seen = _watcher()
    _run(w, clock, 10)
    assert seen == [] and probe.reads == 0  # nothing runs: the processes are never read
    probe.running, probe.caps = True, (ZOOM,)
    w.step()
    assert seen == [] and probe.reads == 1  # read when the device starts running …
    clock.now += calls.START_AFTER_S
    w.step()
    assert seen == [("start", Call("zoom", "Zoom", "us.zoom.xos", 501))]
    _run(w, clock, 60)
    assert len(seen) == 1  # once per call
    assert probe.reads <= 2 + 60 / calls.ATTRIBUTE_S  # … then at most every ATTRIBUTE_S, not at every look
    probe.running, probe.caps = False, ()
    _run(w, clock, calls.END_AFTER_S - 4)
    assert len(seen) == 1  # a moment without the microphone (switching headsets) is the same call
    _run(w, clock, 6)
    assert seen[-1] == ("end", Call("zoom", "Zoom", "us.zoom.xos", 501))


def test_short_captures_are_not_calls() -> None:
    w, probe, clock, seen = _watcher()
    probe.running, probe.caps = True, (ZOOM,)
    w.step()
    clock.now += 1
    probe.running, probe.caps = False, ()
    _run(w, clock, 60)
    assert seen == []  # Zoom's microphone test
    # an app Voicestream doesn't know (a dictation, a voice note) must capture much longer
    probe.running, probe.caps = True, (Capture(77, "org.example.recorder"),)
    _run(w, clock, calls.UNKNOWN_AFTER_S - 2)
    assert seen == []
    _run(w, clock, 4)
    assert seen == [("start", Call("org.example.recorder", None, "org.example.recorder", 77))]


def test_dictation_and_siri_never_count() -> None:
    w, probe, clock, seen = _watcher()
    probe.running, probe.caps = True, (Capture(5, "com.superduper.superwhisper"), Capture(6, "com.apple.corespeechd"))
    _run(w, clock, 120)
    assert seen == []


def test_our_own_capture_lingering_is_not_a_call() -> None:
    """The probe leaves our pid out: the device running for us alone shows no capture."""
    w, probe, clock, seen = _watcher()
    probe.running, probe.caps = True, ()
    _run(w, clock, 60)
    assert seen == []


def test_after_recording_the_call_in_progress_is_not_new() -> None:
    w, probe, clock, seen = _watcher()
    probe.running, probe.caps = True, (ZOOM,)
    w.reset(baseline=True)  # Voicestream just stopped recording this very call
    _run(w, clock, 60)
    assert seen == []
    probe.running, probe.caps = False, ()
    _run(w, clock, calls.END_AFTER_S + 4)
    assert seen == [("end", Call("zoom", "Zoom", "us.zoom.xos", 501))]  # its end is still said: the host forgets it
    probe.running, probe.caps = True, (ZOOM,)
    _run(w, clock, 10)
    assert seen[-1][0] == "start"  # the next call is new


def test_without_process_information_the_device_must_go_from_idle_to_running() -> None:
    """Before macOS 14 (no process objects) the device flag counts our own capture: what runs at the start
    may be ours, lingering after a stop."""
    w, probe, clock, seen = _watcher()
    probe.running, probe.caps = True, None
    _run(w, clock, 30)
    assert seen == []
    probe.running = False
    _run(w, clock, 4)
    probe.running = True
    _run(w, clock, 6)
    assert seen == [("start", Call("mic"))]


def test_a_new_app_joining_while_the_device_runs_is_seen_at_the_next_reading() -> None:
    w, probe, clock, seen = _watcher()
    probe.running, probe.caps = True, (Capture(5, "com.superduper.superwhisper"),)
    _run(w, clock, 30)
    probe.caps = (Capture(5, "com.superduper.superwhisper"), ZOOM)
    _run(w, clock, calls.ATTRIBUTE_S + calls.START_AFTER_S + 2)
    assert [c.app for kind, c in seen if kind == "start"] == ["Zoom"]


def test_the_thread_backs_off_and_stops() -> None:
    class Broken(FakeProbe):
        def device_running(self) -> bool | None:
            self.reads += 1
            raise OSError("coreaudiod is restarting")

    probe = Broken()
    w = calls.CallWatcher(on_start=lambda c: None, on_end=lambda c: None, probe=probe, poll_s=0.01)
    w.start(baseline=False)
    try:
        assert w.running
        deadline = time.monotonic() + 5
        while probe.reads < 4 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert probe.reads >= 4 and w.errors >= 4
    finally:
        w.stop()
    assert not w.running
    reads = probe.reads
    time.sleep(0.3)
    assert probe.reads <= reads + 1  # a look under way may finish, no more


def _stuck_once() -> tuple[FakeProbe, threading.Event, threading.Event]:
    """A probe whose first look at the device hangs until released, as Core Audio does while coreaudiod restarts
    or a Bluetooth headset switches profile — and then says what was true when it was asked: nothing ran."""
    entered, release = threading.Event(), threading.Event()
    looks = itertools.count()

    class Stuck(FakeProbe):
        def device_running(self) -> bool | None:
            if next(looks) == 0:
                entered.set()
                release.wait(5)
                return False
            return self.running

    probe = Stuck()
    probe.running, probe.caps = True, (ZOOM,)
    return probe, entered, release


def test_restarting_never_waits_for_a_look_stuck_in_core_audio() -> None:
    """The shell stops the watcher when a start begins and starts it again, on the main thread, when the state goes
    back to stopped or error: that must not wait for the old run's look, stuck in Core Audio — the menu bar, the
    floating button and the window would freeze for as long."""
    probe, entered, release = _stuck_once()
    w = calls.CallWatcher(on_start=lambda c: None, on_end=lambda c: None, probe=probe, poll_s=0.01)
    w.start(baseline=False)
    try:
        assert entered.wait(5)
        w.stop()  # "starting": the look under way goes on, on its own thread
        threading.Timer(1.5, release.set).start()
        t0 = time.monotonic()
        w.start(baseline=True)  # "error" (the start failed fast): started again at once
        took = time.monotonic() - t0
    finally:
        release.set()
        w.stop()
    assert took < 0.5


def test_a_stopped_runs_late_look_changes_nothing() -> None:
    """That old look, once Core Audio answers, must not use up the new run's baseline nor fill in its calls: the call
    just recorded would be suggested as a new one."""
    probe, entered, release = _stuck_once()
    seen: list[tuple[str, Call]] = []
    clock = Clock()
    w = calls.CallWatcher(on_start=lambda c: seen.append(("start", c)), on_end=lambda c: seen.append(("end", c)),
                          probe=probe, clock=clock)
    w.reset(baseline=False)
    old = threading.Event()
    look = threading.Thread(target=w.step, args=(old,), daemon=True)
    look.start()
    assert entered.wait(5)
    old.set()  # that run was stopped …
    w.reset(baseline=True)  # … and a new one started: Voicestream just stopped recording this very call
    release.set()
    look.join(5)
    new = threading.Event()
    for _ in range(30):
        w.step(new)
        clock.now += 2
    assert seen == []


def test_the_thread_announces_from_its_own_thread() -> None:
    probe = FakeProbe()
    probe.running, probe.caps = True, (ZOOM,)
    got: list[str] = []
    started = threading.Event()

    def on_start(call: Call) -> None:
        got.append(threading.current_thread().name)
        started.set()

    w = calls.CallWatcher(on_start=on_start, on_end=lambda c: None, probe=probe, poll_s=0.01,
                          clock=iter(range(0, 10**6, 2)).__next__)
    w.start(baseline=False)
    try:
        assert started.wait(5)
    finally:
        w.stop()
    assert got[0] == "voicestream-calls"


def test_off_macos_there_is_no_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(calls, "_LIBS", None)
    assert calls.available() is False
    with pytest.raises(OSError):
        calls.probe()


def test_launch_asks_core_audio_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """native.run reads available() on the main thread before the window shows: loading Core Audio says it is a Mac
    where calls can be watched; a call into it there could stall the launch while coreaudiod restarts."""
    monkeypatch.setattr(calls, "_libs", lambda: (object(), object()))

    def asked(self: Any) -> bool | None:
        raise AssertionError("Core Audio was called at launch")

    monkeypatch.setattr(calls.CoreAudioProbe, "device_running", asked)
    assert calls.available() is True


def test_describe_keeps_pids_out() -> None:
    use = calls.MicUse(True, (Capture(501, "us.zoom.xos"), Capture(502, None)))
    out = calls.describe(use)
    assert out == {"in_use": True, "device_running": True, "processes_known": True, "others": ["?", "us.zoom.xos"]}
    assert calls.MicUse(None, None).in_use is False and calls.MicUse(True, None).in_use is True
    assert calls.MicUse(True, ()).in_use is False  # only ours


def test_running_bundle_ids() -> None:
    class App:
        def __init__(self, bid: Any) -> None:
            self.bid = bid

        def bundleIdentifier(self) -> Any:
            if self.bid == "boom":
                raise RuntimeError("gone")
            return self.bid

    assert calls.running_bundle_ids([App("us.zoom.xos"), App(None), App("boom"), App("com.apple.finder")]) == [
        "us.zoom.xos", "com.apple.finder"]
