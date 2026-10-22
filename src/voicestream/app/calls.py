"""Noticing a call: another app starts using the microphone while Voicestream records nothing.

Nothing here starts a recording. When a call seems to begin, the native shell asks the host to
*suggest* taking notes (:meth:`voicestream.app.host.AppHost.suggest_meeting`): the floating button
and the window show "Call detected — start notes?" with Start and Not now, once per call; when it
ends the shell says so (:meth:`~voicestream.app.host.AppHost.call_ended`).

How a call is seen, from Core Audio (``CoreAudio.framework``, through ``ctypes``; reading either
needs no permission):

* the default input device's ``kAudioDevicePropertyDeviceIsRunningSomewhere``: some process on the
  Mac is running it. The default device is read again at each look, so switching to AirPods is
  followed. It is the cheap question, asked every :data:`POLL_S`;
* only while that device runs, the HAL's process objects (``kAudioHardwarePropertyProcessObjectList``,
  macOS 14): which processes capture (``kAudioProcessPropertyIsRunningInput``), with their pid and
  bundle id. That leaves Voicestream's own capture out (its pid) and names the app: Zoom, Microsoft
  Teams, FaceTime, a browser (a Meet call)… It is read when the device starts running and then at
  most every :data:`ATTRIBUTE_S`: each process read makes ``coreaudiod`` run privacy checks for the
  caller, and a poll of every process every second was measured making a dozen of them a second.

A poll rather than a property listener: a listener is called on one of Core Audio's own threads,
and other apps found that ``kAudioProcessPropertyIsRunningInput`` sends no notification anyway. The
poll runs on a thread of its own (a Core Audio call can stall while ``coreaudiod`` restarts, which
must not freeze the menu bar), and only while Voicestream records nothing and suggestions are on.

What counts as a call (:class:`CallWatcher`): an app people hold calls in that keeps capturing for
:data:`START_AFTER_S` (a quick microphone check is not a call), any other app for
:data:`UNKNOWN_AFTER_S` (dictating a sentence is not one either), until it has stopped for
:data:`END_AFTER_S` (a call that switches microphones is still the same call). Apple's own
background capture (Siri, dictation) and dictation apps are never calls; FaceTime and Safari's web
calls are.

Every constant and signature below comes from the macOS SDK headers, quoted by name: CoreAudio's
``AudioHardware.h`` and ``AudioHardwareBase.h`` (``CoreAudio.framework/Headers``), ``MacTypes.h`` for
the base types and ``sys/types.h`` for ``pid_t``.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

from . import hotkey

log = logging.getLogger(__name__)

POLL_S = 2.0
"""How often the default input device is asked whether it runs."""
ATTRIBUTE_S = 10.0
"""While it runs, how often the processes capturing are read again (see the module's docstring)."""
START_AFTER_S = 3.0
"""An app people hold calls in must capture this long to be a call: opening the microphone to check it is not."""
UNKNOWN_AFTER_S = 15.0
"""… any other app this long: a dictation of a sentence or two is not a call."""
END_AFTER_S = 20.0
"""… and stop this long for the call to be over: changing microphones mid-call stops capture a moment."""
MAX_BACKOFF = 30
"""A look that keeps failing is tried again less and less often, up to every ``POLL_S * MAX_BACKOFF``."""

# ------------------------------------------------------------------ which app

APPS: tuple[tuple[str, str], ...] = (
    # (bundle id, or the start of one: helpers share their app's, as "com.google.Chrome.helper"), name
    ("us.zoom.", "Zoom"),
    ("com.microsoft.teams", "Microsoft Teams"),  # com.microsoft.teams2 (new Teams), com.microsoft.teams (classic)
    ("com.apple.FaceTime", "FaceTime"),
    ("com.apple.avconferenced", "FaceTime"),  # the daemon FaceTime's audio runs in
    ("com.tinyspeck.slackmacgap", "Slack"),
    ("com.cisco.webexmeetingsapp", "Webex"),
    ("com.webex.", "Webex"),
    ("Cisco-Systems.Spark", "Webex"),
    ("com.hnc.Discord", "Discord"),
    ("com.skype.", "Skype"),
    ("net.whatsapp.", "WhatsApp"),
    ("org.whispersystems.signal-desktop", "Signal"),
    ("com.google.Chrome", "Google Chrome"),  # Meet in Chrome, and Chrome's web apps (com.google.Chrome.app.…)
    ("com.apple.Safari", "Safari"),
    ("com.apple.WebKit.GPU", "Safari"),  # where Safari's (and WebKit apps') capture runs
    ("company.thebrowser.Browser", "Arc"),
    ("org.mozilla.firefox", "Firefox"),
    ("com.microsoft.edgemac", "Microsoft Edge"),
    ("com.brave.Browser", "Brave"),
    ("com.operasoftware.Opera", "Opera"),
    ("com.vivaldi.Vivaldi", "Vivaldi"),
)
"""Apps people hold calls in (a browser: Google Meet and the like), by bundle id."""
CALL_APPS = frozenset({"Zoom", "Microsoft Teams", "FaceTime", "Slack", "Webex", "Discord", "Skype", "WhatsApp",
                       "Signal"})  # fmt: skip
"""The ones that are only for calls: when Core Audio can't say which app it is, one of these running
is the likely one; a browser only if it is the frontmost app."""
NOT_CALLS: tuple[str, ...] = (
    "com.superduper.superwhisper",  # Superwhisper
    "com.electron.wispr-flow",  # Wispr Flow
    "com.goodsnooze.MacWhisper", "com.goodsnooze.macwhisper",  # MacWhisper
    "com.prakashjoshipax.VoiceInk",  # VoiceInk
)  # fmt: skip
"""Dictation apps: they hold the microphone while you talk to them, which is never a meeting."""


def app_name(bundle_id: str | None) -> str | None:
    """The app a bundle id belongs to, by :data:`APPS` (``None``: not one people hold calls in, as far as
    Voicestream knows)."""
    bid = str(bundle_id or "")
    for prefix, name in APPS:
        if bid.startswith(prefix):
            return name
    return None


def is_background(bundle_id: str | None) -> bool:
    """Capture that is never a call: Apple's own (Siri listening, dictation, Voice Control: any
    ``com.apple.`` process but FaceTime's and WebKit's, see :data:`APPS`) and dictation apps."""
    bid = str(bundle_id or "")
    if bid.startswith(NOT_CALLS):
        return True
    return bid.startswith("com.apple.") and app_name(bid) is None


def likely_app(frontmost: str | None, running: Iterable[str | None]) -> str | None:
    """The app a call is probably in when Core Audio can't say: the frontmost app if people hold calls
    in it (a browser included), else the first call-only app running (:data:`CALL_APPS`)."""
    front = app_name(frontmost)
    if front is not None:
        return front
    for bid in running:
        name = app_name(bid)
        if name in CALL_APPS:
            return name
    return None


# ------------------------------------------------------------------ Core Audio


def _fourcc(text: bytes) -> int:
    """A ``FourCharCode`` (``'glob'`` in C): the four bytes read as a big-endian number."""
    return int.from_bytes(text, "big")


NO_ERR = 0  # AudioHardwareBase.h kAudioHardwareNoError = 0: "The function call completed successfully"
SYSTEM_OBJECT = 1
"""AudioHardware.h ``kAudioObjectSystemObject = 1``: "The AudioObjectID that always refers to the one and only
instance of the AudioSystemObject class"."""
UNKNOWN_OBJECT = 0  # AudioHardwareBase.h kAudioObjectUnknown = 0: "No object will have an ID whose value is 0"
SCOPE_GLOBAL = _fourcc(b"glob")  # AudioHardwareBase.h kAudioObjectPropertyScopeGlobal = 'glob'
ELEMENT_MAIN = 0  # AudioHardwareBase.h kAudioObjectPropertyElementMain = 0 (kAudioObjectPropertyElementMaster before 12.0)
DEFAULT_INPUT = _fourcc(b"dIn ")
"""AudioHardware.h ``kAudioHardwarePropertyDefaultInputDevice = 'dIn '``: "The AudioObjectID of the default input
AudioDevice"."""
RUNNING_SOMEWHERE = _fourcc(b"gone")
"""AudioHardware.h ``kAudioDevicePropertyDeviceIsRunningSomewhere = 'gone'``: "A UInt32 where 1 means that the
AudioDevice is running in at least one process on the system and 0 means that it isn't running at all"."""
PROCESS_LIST = _fourcc(b"prs#")
"""AudioHardware.h ``kAudioHardwarePropertyProcessObjectList = 'prs#'``: "An array of AudioObjectIDs that represent
the Process objects for all client processes currently connected to the system"."""
PROCESS_PID = _fourcc(b"ppid")
"""AudioHardware.h ``kAudioProcessPropertyPID = 'ppid'``: "A pid_t indicating the process ID associated with the
process"."""
PROCESS_BUNDLE_ID = _fourcc(b"pbid")
"""AudioHardware.h ``kAudioProcessPropertyBundleID = 'pbid'``: "A CFString that contains the bundle ID of the
process. The caller is responsible for releasing the returned CFObject"."""
PROCESS_RUNNING_INPUT = _fourcc(b"piri")
"""AudioHardware.h ``kAudioProcessPropertyIsRunningInput = 'piri'``: "A UInt32 where a value of 0 indicates that
the process is not running any IO or there are not any active input streams", 1 that it is and there is."""

COREAUDIO_PATHS = (
    "/System/Library/Frameworks/CoreAudio.framework/CoreAudio",
    "/System/Library/Frameworks/CoreAudio.framework/Versions/A/CoreAudio",
)
"""dlopen() finds these in the system's shared cache even where the files aren't on disk (macOS 11+)."""


class PropertyAddress(ctypes.Structure):
    """AudioHardwareBase.h: ``struct AudioObjectPropertyAddress { AudioObjectPropertySelector mSelector;
    AudioObjectPropertyScope mScope; AudioObjectPropertyElement mElement; }``, three ``UInt32`` typedefs of
    the same header."""

    _fields_ = [("mSelector", ctypes.c_uint32), ("mScope", ctypes.c_uint32), ("mElement", ctypes.c_uint32)]


def _declare(lib: Any) -> Any:
    """AudioHardware.h's prototypes. AudioHardwareBase.h: ``typedef UInt32 AudioObjectID``; MacTypes.h: OSStatus
    an SInt32, Boolean an ``unsigned char``."""
    u32, p = ctypes.c_uint32, ctypes.c_void_p
    addr = ctypes.POINTER(PropertyAddress)
    for name, restype, argtypes in (
        # Boolean AudioObjectHasProperty(AudioObjectID inObjectID, const AudioObjectPropertyAddress* inAddress)
        ("AudioObjectHasProperty", ctypes.c_ubyte, [u32, addr]),
        # OSStatus AudioObjectGetPropertyDataSize(AudioObjectID inObjectID, const AudioObjectPropertyAddress*
        #   inAddress, UInt32 inQualifierDataSize, const void* __nullable inQualifierData, UInt32* outDataSize)
        ("AudioObjectGetPropertyDataSize", ctypes.c_int32, [u32, addr, u32, p, ctypes.POINTER(u32)]),
        # OSStatus AudioObjectGetPropertyData(AudioObjectID inObjectID, const AudioObjectPropertyAddress* inAddress,
        #   UInt32 inQualifierDataSize, const void* __nullable inQualifierData, UInt32* ioDataSize, void* outData)
        ("AudioObjectGetPropertyData", ctypes.c_int32, [u32, addr, u32, p, ctypes.POINTER(u32), p]),
    ):  # fmt: skip
        fn = getattr(lib, name)
        fn.restype, fn.argtypes = restype, argtypes
    return lib


_LIBS: Any = None  # (CoreAudio, CoreFoundation), once loaded


def _libs() -> tuple[Any, Any]:
    global _LIBS
    if _LIBS is None:
        if sys.platform != "darwin":
            raise OSError("call detection reads Core Audio, on a Mac")
        _LIBS = (hotkey._open(COREAUDIO_PATHS, _declare), hotkey._open(hotkey.CORE_FOUNDATION_PATHS, hotkey._declare_cf))
    return _LIBS


def _address(selector: int) -> PropertyAddress:
    return PropertyAddress(selector, SCOPE_GLOBAL, ELEMENT_MAIN)


@dataclass(frozen=True, slots=True)
class Capture:
    """A process capturing from a microphone."""

    pid: int
    bundle_id: str | None = None


@dataclass(frozen=True, slots=True)
class MicUse:
    """One full look at the microphone (:func:`probe`). ``device_running``: the default input runs in some
    process, ours included (``None``: no input device, or it couldn't be read); ``others``: the other
    processes capturing (``None``: this macOS can't say which)."""

    device_running: bool | None
    others: tuple[Capture, ...] | None = None

    @property
    def in_use(self) -> bool:
        """Another process is capturing (or, where processes can't be told apart, the default input runs)."""
        if self.others is not None:
            return bool(self.others)
        return bool(self.device_running)


class Probe(Protocol):
    """What :class:`CallWatcher` asks (:class:`CoreAudioProbe`; tests give a fake)."""

    def device_running(self) -> bool | None: ...
    def captures(self) -> tuple[Capture, ...] | None: ...


class CoreAudioProbe:
    """Core Audio's answers, from any thread. Raises ``OSError`` off macOS, or where Core Audio can't be
    loaded. ``own_pid``: the process whose capture doesn't count (default: this one)."""

    def __init__(self, own_pid: int | None = None) -> None:
        self._ca, self._cf = _libs()
        self.own_pid = os.getpid() if own_pid is None else own_pid

    def _u32(self, obj: int, selector: int) -> int | None:
        """A UInt32 property (an AudioObjectID, a flag) of ``obj``; ``None`` when Core Audio says no."""
        value, size = ctypes.c_uint32(0), ctypes.c_uint32(ctypes.sizeof(ctypes.c_uint32))
        err = self._ca.AudioObjectGetPropertyData(obj, ctypes.byref(_address(selector)), 0, None, ctypes.byref(size),
                                                  ctypes.byref(value))
        return int(value.value) if err == NO_ERR else None

    def _pid(self, obj: int) -> int | None:
        pid, size = ctypes.c_int32(0), ctypes.c_uint32(ctypes.sizeof(ctypes.c_int32))  # sys/types.h: pid_t, an __int32_t
        err = self._ca.AudioObjectGetPropertyData(obj, ctypes.byref(_address(PROCESS_PID)), 0, None, ctypes.byref(size),
                                                  ctypes.byref(pid))
        return int(pid.value) if err == NO_ERR else None

    def _string(self, obj: int, selector: int) -> str | None:
        """A CFString property, released here (the header: the caller is responsible for releasing it)."""
        ref, size = ctypes.c_void_p(), ctypes.c_uint32(ctypes.sizeof(ctypes.c_void_p))
        err = self._ca.AudioObjectGetPropertyData(obj, ctypes.byref(_address(selector)), 0, None, ctypes.byref(size),
                                                  ctypes.byref(ref))
        if err != NO_ERR or not ref.value:
            return None
        try:
            return hotkey._cf_string(self._cf, ref.value) or None
        finally:
            self._cf.CFRelease(ref.value)

    def default_input(self) -> int | None:
        """The default input device's AudioObjectID (``None``: there is none)."""
        device = self._u32(SYSTEM_OBJECT, DEFAULT_INPUT)
        return None if device in (None, UNKNOWN_OBJECT) else device

    def device_running(self) -> bool | None:
        device = self.default_input()
        if device is None:
            return None
        flag = self._u32(device, RUNNING_SOMEWHERE)
        return None if flag is None else bool(flag)

    def _process_objects(self) -> list[int] | None:
        """The HAL's process objects; ``None`` before macOS 14, where there are none to read."""
        ca, addr = self._ca, _address(PROCESS_LIST)
        if not ca.AudioObjectHasProperty(SYSTEM_OBJECT, ctypes.byref(addr)):
            return None
        size = ctypes.c_uint32(0)
        if ca.AudioObjectGetPropertyDataSize(SYSTEM_OBJECT, ctypes.byref(addr), 0, None, ctypes.byref(size)) != NO_ERR:
            return None
        count = size.value // ctypes.sizeof(ctypes.c_uint32)
        if count == 0:
            return []
        ids = (ctypes.c_uint32 * count)()
        size = ctypes.c_uint32(ctypes.sizeof(ids))
        if ca.AudioObjectGetPropertyData(SYSTEM_OBJECT, ctypes.byref(addr), 0, None, ctypes.byref(size), ids) != NO_ERR:
            return None
        return list(ids[: size.value // ctypes.sizeof(ctypes.c_uint32)])

    def captures(self) -> tuple[Capture, ...] | None:
        """The other processes capturing from any input now (``None``: this macOS can't say)."""
        objects = self._process_objects()
        if objects is None:
            return None
        out: list[Capture] = []
        for obj in objects:
            if not self._u32(obj, PROCESS_RUNNING_INPUT):
                continue
            pid = self._pid(obj)
            if pid is None or pid == self.own_pid:
                continue
            out.append(Capture(pid, self._string(obj, PROCESS_BUNDLE_ID)))
        return tuple(out)


def probe(own_pid: int | None = None) -> MicUse:
    """One full look at the microphone now (any thread): the self-test's. Raises ``OSError`` where Core Audio
    can't be read."""
    p = CoreAudioProbe(own_pid)
    return MicUse(p.device_running(), p.captures())


def available() -> bool:
    """Core Audio can be read here (a Mac)."""
    try:
        CoreAudioProbe().device_running()
    except OSError as exc:
        log.debug("no call detection: %s", exc)
        return False
    return True


def describe(use: MicUse) -> dict[str, Any]:
    """For the self-test report: what a look said (bundle ids only, no pids)."""
    return {
        "in_use": use.in_use,
        "device_running": use.device_running,
        "processes_known": use.others is not None,
        "others": sorted({c.bundle_id or "?" for c in use.others}) if use.others is not None else None,
    }


# ------------------------------------------------------------------ the watcher


@dataclass(frozen=True, slots=True)
class Call:
    """What the host is told: ``key`` tells calls apart (the app, or the process), ``app`` names it when the
    bundle id says which app it is, ``pid`` lets the shell ask macOS for the name of one it doesn't know."""

    key: str
    app: str | None = None
    bundle_id: str | None = None
    pid: int | None = None


@dataclass(slots=True)
class _Seen:
    call: Call
    since: float
    announced: bool = False
    gone_since: float | None = None


def calls_in(captures: tuple[Capture, ...] | None, device_running: bool) -> dict[str, Call]:
    """The calls a look shows, by key: one per app (its helpers capture too), what is never a call left
    out (:func:`is_background`). Without process information, the default input running is one call ("mic")."""
    if captures is None:
        return {"mic": Call("mic")} if device_running else {}
    out: dict[str, Call] = {}
    for cap in captures:
        if is_background(cap.bundle_id):
            continue
        name = app_name(cap.bundle_id)
        key = (name or cap.bundle_id or f"pid {cap.pid}").lower()
        out.setdefault(key, Call(key, name, cap.bundle_id, cap.pid))
    return out


@dataclass
class CallWatcher:
    """Turns looks at the microphone (a :class:`Probe`) into calls starting and ending.

    ``on_start(call)`` and ``on_end(call)`` are called on the watcher's thread (the shell hands them to the
    main thread, then the host). :meth:`step` is the whole logic, on the clock it is given: the thread only
    calls it every :attr:`poll_s`."""

    on_start: Callable[[Call], None]
    on_end: Callable[[Call], None]
    probe: Probe | None = None
    clock: Callable[[], float] = time.monotonic
    poll_s: float = POLL_S
    errors: int = 0
    _seen: dict[str, _Seen] = field(default_factory=dict)
    _captures: tuple[Capture, ...] | None = ()
    _read_at: float | None = None
    _was_running: bool = False
    _device_was_idle: bool = False
    _baseline: bool = False
    _thread: threading.Thread | None = None
    _stop: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def reset(self, *, baseline: bool) -> None:
        """Start afresh. ``baseline``: what captures now is not a new call (Voicestream just stopped recording,
        in the middle of the call it was recording: suggesting notes for it would be odd)."""
        with self._lock:
            self._seen.clear()
            self._captures, self._read_at = (), None
            self._was_running = self._device_was_idle = False
            self._baseline = baseline

    def _look(self, now: float) -> dict[str, Call]:
        """What captures now: the device flag at each look, the processes only when it runs (see the module)."""
        assert self.probe is not None
        running = bool(self.probe.device_running())
        if not running:
            self._device_was_idle = True
            self._was_running = False
            self._captures, self._read_at = (), None
            return {}
        if not self._was_running or self._read_at is None or now - self._read_at >= ATTRIBUTE_S:
            self._captures, self._read_at = self.probe.captures(), now
        self._was_running = True
        if self._captures is None and not self._device_was_idle:
            # no process information: the flag counts our own capture too, which may linger a moment after a
            # stop — only the device going from idle to running while we capture nothing is a call
            return {}
        return calls_in(self._captures, running)

    def step(self) -> None:
        """One look: announce the calls that have lasted long enough, end those gone for :data:`END_AFTER_S`."""
        if self.probe is None:
            self.probe = CoreAudioProbe()
        now = self.clock()
        starts: list[Call] = []
        ends: list[Call] = []
        with self._lock:
            current = self._look(now)
            baseline, self._baseline = self._baseline, False
            for key, call in current.items():
                seen = self._seen.get(key)
                if seen is None:
                    self._seen[key] = seen = _Seen(call, now, announced=baseline)
                seen.gone_since = None
                wait = START_AFTER_S if call.app is not None or call.key == "mic" else UNKNOWN_AFTER_S
                if not seen.announced and now - seen.since >= wait:
                    seen.announced = True
                    starts.append(seen.call)
            for key, seen in list(self._seen.items()):
                if key in current:
                    continue
                if not seen.announced:
                    del self._seen[key]  # too short to be a call
                    continue
                if seen.gone_since is None:
                    seen.gone_since = now
                elif now - seen.gone_since >= END_AFTER_S:
                    del self._seen[key]
                    ends.append(seen.call)
        for call in starts:
            self.on_start(call)
        for call in ends:
            self.on_end(call)

    # ------------------------------------------------------------- the thread

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._stop.is_set()

    def start(self, *, baseline: bool) -> None:
        """Look every :attr:`poll_s` seconds on a thread of its own, until :meth:`stop`."""
        if self.running:
            return
        self.reset(baseline=baseline)
        self.errors = 0
        self._stop = stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, args=(stop,), name="voicestream-calls", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop looking (returns at once: a look under way finishes on its own, and says nothing more)."""
        self._stop.set()
        self._thread = None

    def _loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.step()
                self.errors = 0
            except Exception:  # Core Audio away a moment (coreaudiod restarting): try again later
                self.errors += 1
                if self.errors == 1:  # once, not every two seconds
                    log.exception("looking at the microphone failed")
            stop.wait(self.poll_s * min(MAX_BACKOFF, 2 ** max(0, self.errors - 1)))


def running_bundle_ids(apps: Iterable[Any]) -> list[str]:
    """The bundle ids of NSRunningApplication objects (main thread), for :func:`likely_app`."""
    out = []
    for app in apps:
        try:
            bid = app.bundleIdentifier()
        except Exception:  # an odd entry must not cost the rest
            log.debug("an app without a bundle id", exc_info=True)
            continue
        if bid:
            out.append(str(bid))
    return out
