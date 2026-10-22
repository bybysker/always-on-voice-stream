"""The app's long-lived host: one per app launch, around a replaceable :class:`~voicestream.session.Assistant`.

::

    AppHost (asyncio loop, own thread in the Mac app)
    ├─ dashboard HTTP server  — started once, fixed token, ephemeral port
    ├─ EventBus + CostMeter   — shared by every assistant this launch
    ├─ assistant (or None)    — new one on start / settings change; paused = digital silence
    ├─ model downloads        — speech-to-text models, with progress events
    └─ Meetings               — past meetings on disk

An :class:`Assistant` is single-use (``run()`` once, then it closes its clients), so "start",
"apply settings" and "restart" build a fresh one with the host's bus and cost meter; the UI's
event stream is subscribed to the host's bus and never notices.

Settings and key changes apply at once when no meeting is in progress, else after it ends
(``restart_pending``): a restart would end the meeting (summary, pushes). The UI offers "apply
now" (``listen_restart``). New settings that can't run yet never stop working listening: a speech
model still downloading is switched to once it is in (:attr:`AppHost.awaiting_model`), anything
else is a warning until it is fixed. ``voicestream.toml`` edited by hand is read again on Start,
Stop, restart and when the Settings screen loads (when the file changed); the reads run in a worker
thread, since loading the keys can wait on a Keychain prompt.

A stop (or restart) waits for the capture to close and the meeting in progress to be wrapped up;
an ended meeting's agents may work on for many minutes after that, so the assistant then finishes
them in the background (:attr:`AppHost.assistants`) while listening can start again at once.

The microphone picked in Settings may be a headset that isn't connected: listening goes on with
the default input (or meeting audio alone) and takes it back when it is (:class:`ChosenMic`).

How meetings start is a preference (``prefs.meeting_start``). ``manual``, the default: nothing is
captured until you start a meeting; starting one starts listening with the meeting open at once,
and stopping it (or ending it, or a long silence) wraps it up and lets go of the microphone and
meeting audio (:attr:`Assistant.manual <voicestream.session.Assistant.manual>`). A meeting left
running asks "Still in a meeting?" after ``silence_ask_minutes`` without speech (:attr:`AppHost.still_there`,
answered with :meth:`AppHost.meeting_keep`) and ends by itself after ``silence_end_minutes``, saying why
(:attr:`AppHost.notice`). ``automatic`` is the always-on listening of ``voicestream run``. Every way of
starting and stopping — the window, the menu bar, the keyboard shortcut, the floating button, the
Spotlight launcher, ``voicestream://`` links (:meth:`AppHost.open_url`) — goes through the same
actions: :meth:`AppHost.meeting_start`, :meth:`AppHost.meeting_stop` and :meth:`AppHost.meeting_toggle`.
When another app starts using the microphone (a call) and nothing is recorded, the shell calls
:meth:`AppHost.suggest_meeting`: the window and the floating button offer to start notes
(:attr:`AppHost.suggestion`), and nothing starts unless you say so.

The native shell talks to the host from its main thread with :meth:`AppHost.run_in_thread`,
:meth:`AppHost.call` and :meth:`AppHost.add_state_listener`. Its global keyboard shortcut
(:mod:`.hotkey`) calls :meth:`AppHost.meeting_toggle`, and the shell says with
:meth:`AppHost.set_hotkey_status` whether macOS took the shortcut, and with
:meth:`AppHost.set_keyboard_layout` what the keys type on the Mac's keyboard. Optional platform glue lives in
``voicestream.app.native_hooks`` when the shell provides it (all functions optional):

* ``microphone_status() -> "granted" | "denied" | "undetermined" | "restricted"`` (AVFoundation);
* ``set_start_at_login(enabled: bool) -> None`` (SMAppService; raises on failure);
* ``watch_audio_devices(callback: Callable[[], None]) -> bool``: call ``callback`` (from any thread)
  when an audio device is connected or removed, so a missing microphone is looked for at once
  rather than at the next periodic look.

The console printer, signal handlers and stdin commands of ``voicestream run`` are not used here.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import functools
import hmac
import importlib
import itertools
import logging
import os
import secrets as stdlib_secrets
import sys
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import parse_qs, urlsplit

from .. import __version__, stt_models
from ..audio import CaptureSetupError, Clock
from ..audio.base import aclose_quietly
from ..audio.mic import MicSource, resolve_device
from ..config import Config, ConfigError, DashboardConfig
from ..costs import CostMeter
from ..events import EventBus
from ..models import AudioFrame, Event, utcnow
from . import logs
from . import secrets as keys
from .hotkey import TOGGLE
from .meetings import Meetings
from .prefs import Prefs, load_prefs, save_prefs, update_prefs, with_url_key
from .settings import default_config_path, load_app_config

if TYPE_CHECKING:
    from ..session import Assistant

log = logging.getLogger(__name__)

State = Literal["stopped", "starting", "listening", "paused", "stopping", "error"]

START_WAIT_S = 10.0
"""``listen_start`` returns once the assistant reports started, or after this long."""
FORCE_STOP_S = 10.0
"""A forced stop waits this long for the assistant to wind down before cancelling it outright."""
GRACEFUL_SHUTDOWN_S = 20.0
"""Quitting: time for a graceful wrap-up (summary of the meeting in progress) before forcing."""
DOWNLOAD_EVENT_S = 0.25
"""``model.download`` progress events: at most ~4 per second."""
MAX_WARNINGS = 10
EXTRA_PATH = ("/opt/homebrew/bin", "/usr/local/bin")
"""A Finder-launched app gets PATH=/usr/bin:/bin:/usr/sbin:/sbin: no ffmpeg, no Homebrew whisper-server."""
MIC_RETRY_S = 15.0
"""The chosen microphone isn't connected and nothing records in its place: look for it this often."""
MIC_RETRY_FALLBACK_S = 60.0
"""… and while the default input records instead: each look closes it for a moment (see :class:`ChosenMic`)."""
MIC_STALL_S = 5.0
"""A device that delivers nothing for this long was unplugged: PortAudio just stops calling back."""
MIC_MISSING = "Your microphone ("
"""How the warnings about a microphone that isn't connected start (replaced, then dropped when it is back)."""
MODEL_WAIT = "The speech-to-text model is still downloading"
"""How the notices about a speech model listening waits for start."""
NOT_APPLIED = "Your new settings aren't used yet"
"""How the warning about saved settings that can't run (listening goes on with the previous ones) starts."""
HOTKEY_RECORDING_S = 30.0
"""Settings records a new shortcut with the current one off (or pressing it would toggle listening, not
reach the page); it is back on after this long even if the page never says it is done."""
START = {"stopped": "start", "error": "restart", "paused": "resume"}
"""What starting a meeting does in each state (:meth:`AppHost.meeting_start`): after a problem, a restart
as Start does then; paused, Resume. Starting or listening, nothing: one is recorded or about to be. While
the last one is wrapped up (stopping), the start waits for it (see :meth:`AppHost.meeting_action`)."""
STOPPABLE = frozenset({"starting", "listening", "paused", "error"})
"""The states :meth:`AppHost.meeting_stop` stops (an error: Stop dismisses it, and stops what still runs)."""
CALL_MEMORY_S = 4 * 3600.0
"""A call is suggested once: one the shell never said was over (:meth:`AppHost.call_ended`) is
forgotten after this long, so the same app's next call is suggested again."""
APP_NAME_MAX = 80
URL_SCHEME = "voicestream"
URL_ACTIONS = {"meeting/start": "start", "meeting/stop": "stop", "meeting/toggle": "toggle", "open": "open"}
"""The ``voicestream://`` links :meth:`AppHost.open_url` follows, and what each does."""
KEYED_ACTIONS = frozenset({"start", "toggle"})
"""The links that may start a recording: they need the key (``?key=``), or any web page could."""

MeetingAction = Literal["start", "restart", "resume", "stop", "error"]
"""What a meeting action did (``error``: a start that failed, the state says why), for the shell's sound."""

_STT_KEYS = {"openai": ("OPENAI_API_KEY", "OpenAI"), "openai-realtime": ("OPENAI_API_KEY", "OpenAI"),
             "deepgram": ("DEEPGRAM_API_KEY", "Deepgram")}  # fmt: skip


def human_error(exc: BaseException | str) -> str:
    """A message for the person, not the log: what is wrong and where to fix it in the app."""
    from ..transcribe import TranscriptionError

    text = str(exc) if not isinstance(exc, str) else exc
    low = text.lower()
    if "microphone" in low and ("digital silence" in low or "permission" in low or "not authorized" in low):
        return "Microphone permission is off: System Settings → Privacy & Security → Microphone → Voicestream"
    if "system audio recording" in low or ("system audio" in low and "silence" in low):
        return ("Meeting audio isn't coming through: System Settings → Privacy & Security → "
                "Screen & System Audio Recording → Voicestream, then reopen Voicestream")
    for key, name in (("OPENAI_API_KEY", "OpenAI"), ("DEEPGRAM_API_KEY", "Deepgram")):
        if key in text and "not set" in low:
            return f"Speech-to-text with {name} needs your {name} key: Settings → Speech-to-text"
    if "not downloaded" in low and "model" in low:
        return "The speech-to-text model isn't downloaded yet: Settings → Speech-to-text → Download"
    if "sounddevice unavailable" in low or "portaudio" in low:
        return "Audio capture is missing from this install (sounddevice/PortAudio): reinstall Voicestream"
    if "whisper-server not found" in low:
        return "Speech-to-text on this Mac is missing from this install (whisper-server not found): reinstall Voicestream"
    if isinstance(exc, ImportError):
        return f"Part of Voicestream is missing ({exc}): reinstall it"
    if isinstance(exc, (CaptureSetupError, TranscriptionError)):
        return text
    return text or type(exc).__name__


def _native_hooks() -> Any:
    try:
        return importlib.import_module("voicestream.app.native_hooks")
    except ImportError:
        return None


def _login_item_supported(hooks: Any) -> bool:
    if not hasattr(hooks, "set_start_at_login"):
        return False
    status = getattr(hooks, "login_item_status", None)
    if status is None:
        return True
    try:
        return status() != "unsupported"
    except Exception:  # noqa: BLE001
        return False


def _prepend_path() -> None:
    parts = os.environ.get("PATH", "").split(os.pathsep) if os.environ.get("PATH") else []
    extra = [d for d in EXTRA_PATH if d not in parts and Path(d).is_dir()]
    if extra:
        os.environ["PATH"] = os.pathsep.join([*extra, *parts])


def _restart_portaudio() -> None:
    """PortAudio lists the devices it saw when it started: restart it to see one plugged in since.
    Closes every PortAudio stream, so only call it with none open."""
    from ..audio.mic import _sd

    sd = _sd()
    try:
        sd._terminate()  # python-sounddevice's own (private) pair, the documented way to rescan
        sd._initialize()
    except Exception as exc:  # noqa: BLE001 - a sounddevice without them: the list stays as it was
        log.debug("could not rescan the audio devices: %s", exc)


def _named_device(device: str | int | None) -> bool:
    """A device chosen by name (``"AirPods"``), the kind that comes and goes; not an index or the default."""
    return isinstance(device, str) and bool(device.strip()) and not device.strip().isdigit()


def parse_url(url: str) -> tuple[str, str | None] | None:
    """``voicestream://meeting/start?key=…`` → ``("start", "…")``: the action (:data:`URL_ACTIONS`) and the
    key it carries (``None``: none, or more than one); ``None`` for a link Voicestream doesn't know."""
    try:
        parts = urlsplit(str(url).strip())
    except ValueError:
        return None
    if parts.scheme.lower() != URL_SCHEME:
        return None
    action = URL_ACTIONS.get(f"{parts.netloc}{parts.path}".strip("/").lower())
    if action is None:
        return None
    given = parse_qs(parts.query, keep_blank_values=True).get("key", [])
    return action, given[0] if len(given) == 1 else None


def _app_name(name: str | None) -> str | None:
    """The calling app's name as the shell read it, fit to be shown (no control characters, not too long)."""
    text = " ".join("".join(c if c.isprintable() else " " for c in str(name or "")).split())
    return text[:APP_NAME_MAX] or None


class ChosenMic(MicSource):
    """The microphone picked in Settings (``capture.mic.device``), which may not be connected: a
    headset, AirPods with a flat battery.

    A plain :class:`MicSource` gives up for good when its device is missing (``CaptureSetupError``):
    no voice of yours for the rest of the day while meeting audio still records. This one goes on —
    on the default input meanwhile (``capture.mic.use_default_when_missing``), else with nothing from
    the microphone — and takes your device back once it is there again: looked for every
    :data:`MIC_RETRY_S` (:data:`MIC_RETRY_FALLBACK_S` while the default input records), at once when
    the native shell reports a device change (:meth:`poke`), and when it goes quiet mid-meeting.
    ``on_change(missing, using)`` tells the host (on the loop) when it goes missing or comes back.

    PortAudio's device list is the one it saw when it started, so each look restarts it, which
    closes every stream it has open: this source closes its own first, and the look is only made
    when no other source uses PortAudio (``may_rescan``, set by :func:`_default_assistant`).
    """

    def __init__(self, device: str, clock: Clock, *, use_default: bool, on_change: Callable[[bool, str | None], None],
                 may_rescan: bool = True) -> None:
        super().__init__("mic", device=device, clock=clock)
        self.chosen = device
        self.use_default = use_default
        self.on_change = on_change
        self.may_rescan = may_rescan
        self.missing = False
        """The chosen device isn't connected (the default input or nothing records instead)."""
        self._reported = False
        self._rescan = False
        self._last_audio = time.monotonic()
        self._last_look = time.monotonic()
        self._wake: asyncio.Event | None = None

    def describe(self) -> str:
        if self.missing and self._stream is None:
            return f"{self.name}: input device {self.chosen} (not connected)"
        return super().describe()

    def push_from_thread(self, data: Any, rate: int) -> None:
        self._last_audio = time.monotonic()
        super().push_from_thread(data, rate)

    def _connected(self, rescan: bool) -> bool:
        if rescan and self.may_rescan:
            _restart_portaudio()
        try:
            resolve_device(self.chosen)
        except CaptureSetupError:
            return False
        return True

    def start_capture(self) -> None:
        """(Worker thread.) Open the chosen device, else the default input or nothing."""
        # a second look (after a stall or a failure) starts from a fresh device list: one unplugged since
        # PortAudio started is still in the old one; the first look only rescans when it needs to
        rescanned, self._rescan = self._rescan, False
        self._last_audio = time.monotonic()  # opening isn't a stall
        found = self._connected(rescan=rescanned)
        if not found and not rescanned and self.may_rescan:
            found = self._connected(rescan=True)  # plugged in since PortAudio started?
        self.missing = not found
        self.device = self.chosen if found else None  # None: the OS default input
        if found or self.use_default:
            super().start_capture()
        self._last_audio = time.monotonic()
        self._report()

    def _report(self) -> None:
        if self.missing == self._reported:
            return
        self._reported = self.missing
        using = self._device_name if self.missing and self._stream is not None else None
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self.on_change, self.missing, using)

    def poke(self) -> None:
        """An audio device was connected or removed: look again now (on the loop thread)."""
        if self._wake is not None:
            self._wake.set()

    async def frames(self) -> AsyncIterator[AudioFrame]:
        self._wake = asyncio.Event()
        watcher = asyncio.ensure_future(self._watch())
        inner = super().frames()
        try:
            async for frame in inner:
                yield frame
        finally:
            watcher.cancel()
            await aclose_quietly(inner)  # closes the device now, not whenever the generator is collected

    async def _watch(self) -> None:
        assert self._wake is not None
        while not self.stopped:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=1.0)
            poked = self._wake.is_set()
            self._wake.clear()
            if self.stopped or self._loop is None:
                return
            now = time.monotonic()
            every = MIC_RETRY_FALLBACK_S if self._stream is not None else MIC_RETRY_S
            stalled = self._stream is not None and now - self._last_audio > MIC_STALL_S
            if stalled or (self.missing and (poked or now - self._last_look >= every)):
                await self._look_again()

    async def _look_again(self) -> None:
        self._last_look = time.monotonic()
        try:
            await asyncio.to_thread(self._reopen)
        except Exception as exc:  # noqa: BLE001 - e.g. the default input failed to open: try again later
            log.warning("%s: could not open the microphone: %s", self.name, exc)
            self.missing = True
            self._report()

    def _reopen(self) -> None:
        self.stop_capture()
        # the next device's frames are stamped on the meeting clock from where they begin
        self._resampler, self._device_rate, self._samples_out, self._t_origin = None, None, 0, None
        self._rescan = True
        self.start_capture()


class HostError(RuntimeError):
    """A request the host can't carry out right now (the message is for the person)."""


class AppHost:
    is_app_host = True
    """The dashboard serves the app's routes (``/app``, ``/api/app``, settings…) for this object."""

    def __init__(
        self,
        config_path: str | os.PathLike[str] | None = None,
        *,
        process_setup: bool = True,
        assistant_factory: Callable[[AppHost], Assistant] | None = None,
        prefs_path: Path | None = None,
        port: int | None = None,
    ) -> None:
        """``process_setup`` does the app-only process hygiene (umask, PATH, log file); tests turn it
        off. ``assistant_factory`` builds each assistant (tests inject fake sources). ``port`` for the
        local server: default any free one (the window is given the URL)."""
        self.port = port
        self.config_path = Path(config_path).expanduser().absolute() if config_path else default_config_path()
        self.process_setup = process_setup
        if process_setup:
            os.umask(0o077)  # transcripts, notes, keys: for your eyes only
            _prepend_path()
        self.prefs_path = prefs_path
        self.prefs: Prefs = load_prefs(prefs_path)
        self.bus = EventBus(history=2000)
        self.warnings: deque[str] = deque(maxlen=MAX_WARNINGS)
        self.config_error: str | None = None
        self._stamp: tuple[int, int, int] | None = None
        """The config file's identity when it was last read: a hand edit changes it."""
        self._warned_keys: set[str] = set()
        self.cfg: Config = self._load_config() or Config()
        self.costs = CostMeter(self.cfg, self.bus)
        self.meetings = Meetings(self)
        self.native_hooks: Any = _native_hooks()
        self.capabilities: dict[str, bool] = {
            "native_window": False,  # the native shell sets it
            "hotkey": False,  # likewise: a global shortcut needs the Mac app
            "floating_button": False,  # … and so do these three: the page shows their settings when they're there
            "spotlight": False,
            "call_detection": False,
            "login_item": _login_item_supported(self.native_hooks),
            "keychain": keys.store_name() == "keychain",
        }
        self.hotkey: dict[str, Any] = {"status": "pending", "error": None}
        """What the native shell says of the shortcut: ``ok`` (works from any app), ``taken`` (by another
        app), ``failed`` (``error``: macOS's code), ``off``, ``paused`` (Settings is recording a new one)
        or, until it has said, ``pending``."""
        self.keyboard_layout: dict[str, str] = {}
        """What each letter and punctuation key types with nothing held on the Mac's keyboard layout
        (``KeyboardEvent.code`` → character, :func:`.hotkey.current_layout`), as the native shell read it;
        empty elsewhere. A shortcut recorded with ⌥ held is shown and checked by it (the page can't tell)."""
        self._hotkey_recording: asyncio.TimerHandle | None = None
        self._toggling: asyncio.Task[Any] | None = None
        """What the last meeting action (the shortcut, the floating button…) started, while it is being carried out."""
        self._toggled: MeetingAction | None = None
        """Which action :attr:`_toggling` carries out."""
        self._start_queued = False
        """A start asked for while the last meeting is being wrapped up (state "stopping"): it starts once that
        is done, unless Stop, Stop now or quitting comes first (:meth:`_start_after_stop`)."""
        self._queued_start: asyncio.Task[bool] | None = None
        self._status_changed = asyncio.Event()
        """Set at every change of :attr:`status` (a start queued behind a stop waits for the stop to be over)."""
        self.suggestion: dict[str, Any] | None = None
        """"Call detected — start notes?" (:meth:`suggest_meeting`): ``{"app", "at"}`` until it is answered,
        the call ends or a recording starts."""
        self._suggestion_key: str | None = None
        self._suggested: dict[str, float] = {}
        """The calls already suggested (their key → when, on :attr:`wall`): one suggestion per call."""
        self.still_there: dict[str, Any] | None = None
        """"Still in a meeting?" (a long silence in a meeting started by hand): ``{"meeting_id",
        "silent_minutes", "ends_at"}`` until speech, Keep or the end of the meeting."""
        self.notice: dict[str, Any] | None = None
        """Something the person should know happened by itself (``kind``: ``ended_after_silence``,
        ``link_refused``), until dismissed or the next meeting starts. ``ended_after_silence`` is ``pending``
        while the meeting's notes are being written, then says where they are (``title``, ``folder``: none
        when nothing was said)."""
        self.wall: Callable[[], float] = time.time
        """The clock the calls already suggested are remembered on (tests replace it)."""
        self.status: State = "stopped"
        self.error: str | None = self.config_error
        if self.error:
            self.status = "error"
        self.restart_pending = False
        self.awaiting_model: str | None = None
        """A speech model the settings name that is still downloading: listening switches to it (or
        starts, see :attr:`_start_when_ready`) once it is in, instead of failing now."""
        self._start_when_ready = False
        """Start was asked for while :attr:`awaiting_model` downloads (state "starting" meanwhile)."""
        self.token = stdlib_secrets.token_urlsafe(24)
        self.url: str | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self._assistant: Assistant | None = None
        self._run_task: asyncio.Task[None] | None = None
        self._stop_requested = False
        self._closing = False
        """Set by :meth:`shutdown`: nothing may start listening any more (a restart in flight, a menu click)."""
        self._stop_pressed = False
        """Stop was asked for (not by a restart): a restart under way must not start listening again."""
        self._last_source_error: str | None = None
        self._dead_sources: set[str] = set()
        """Sources of the running assistant that gave up for good (``source.stopped``)."""
        self._retiring: dict[Assistant, asyncio.Task[None]] = {}
        """Assistants stopped (or replaced by a restart) whose capture is closed, still finishing an
        ended meeting's agents in the background (see :meth:`_stop`)."""
        self._factory = assistant_factory or _default_assistant
        self._background: set[asyncio.Task[Any]] = set()
        self._downloads: dict[str, dict[str, Any]] = {}
        self._state_listeners: list[Callable[[dict[str, Any]], None]] = []
        # every snapshot gets the next number: replies and pushed events reach the page on different
        # connections, and it keeps only the newest (per launch of the app)
        self._boot = stdlib_secrets.token_hex(4)
        self._revs = itertools.count(1)
        self._server_task: asyncio.Task[None] | None = None
        self._log_handlers: list[tuple[logging.Logger, logging.Handler]] = []
        self._closed: asyncio.Event | None = None
        self._unlisten = self.bus.listen(self._on_event)

    # ------------------------------------------------------------------ config

    def _read_config(self) -> tuple[Config | None, str | None]:
        """``(config, None)`` or ``(None, the problem)``. Touches no host state: it runs in a worker
        thread too (loading the keys may wait on a Keychain prompt)."""
        try:
            return load_app_config(self.config_path), None
        except (ConfigError, OSError, ValueError) as exc:
            problem = f"Your settings file has a problem: {exc}"
        except RuntimeError as exc:  # Path.expanduser: "~name/…" of an account that doesn't exist
            problem = f"Your settings file has a problem: {exc} (a path starts with ~name/ — write ~/… for your own folder)"
        log.error("%s", problem)
        return None, problem

    def _config_stamp(self) -> tuple[int, int, int] | None:
        try:
            st = self.config_path.stat()
        except OSError:
            return None
        return st.st_ino, st.st_mtime_ns, st.st_size  # the app's own saves replace the file: a new inode

    def _load_config(self) -> Config | None:
        self._stamp = self._config_stamp()  # before reading: an edit made meanwhile is seen next time
        cfg, self.config_error = self._read_config()
        self._warn_unreadable_keys()
        return cfg

    def _adopt(self, cfg: Config | None) -> None:
        if cfg is None:
            return
        self.cfg = cfg
        self.costs.cfg = cfg  # prices, budget and spend folder follow the new settings
        if self.status == "error" and self.error and self.error.startswith("Your settings file"):
            self.error, self.status = None, "stopped"

    def _warn_unreadable_keys(self) -> None:
        """Keys the Keychain refused (asked once per launch, see ``secrets.load_into_env``): say so in the banner."""
        for name in keys.unreadable():
            if name not in self._warned_keys:
                self._warned_keys.add(name)
                self.add_warning(keys.unreadable_message(name))

    def reload_config(self) -> None:
        """Re-read the config now, on this thread; the running assistant keeps its own until restarted.
        On the host's loop, :meth:`refresh_config` does it without blocking it."""
        self._adopt(self._load_config())

    async def refresh_config(self, force: bool = False) -> bool:
        """Re-read ``voicestream.toml`` if it changed since it was last read — edited by hand, as the app
        asks for the settings it can't change itself — or always with ``force`` (after a save). Off the
        loop: loading the keys can wait on a Keychain prompt, which would freeze the audio, the event
        stream and every request meanwhile. Returns whether it was read."""
        stamp = self._config_stamp()
        if not force and stamp == self._stamp:
            return False
        self._stamp = stamp
        cfg, self.config_error = await asyncio.to_thread(self._read_config)
        self._warn_unreadable_keys()
        self._adopt(cfg)
        return True

    @property
    def assistant(self) -> Assistant | None:
        """The running assistant (``None`` when not listening); live-meeting routes go through it."""
        return self._assistant

    @property
    def assistants(self) -> list[Assistant]:
        """The running assistant first, then earlier ones still finishing an ended meeting's agents
        after a stop or restart: their meetings are still held (and saved) in memory there."""
        return [a for a in (self._assistant, *self._retiring) if a is not None]

    def track(self, task: asyncio.Task[Any]) -> None:
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # ------------------------------------------------------------------- state

    def in_meeting(self) -> bool:
        cur = self._assistant.current if self._assistant is not None else None
        return cur is not None and not cur.finished

    def state(self) -> dict[str, Any]:
        """``GET /api/app`` and every ``app.state`` event. Never holds the link key (``prefs.url_key``)."""
        cur = self._assistant.current if self._assistant is not None else None
        meeting = None
        if cur is not None and not cur.finished:
            m = cur.meeting
            meeting = {"id": m.id, "title": m.title, "started_at": m.started_at.isoformat(), "folder": cur.store.dir.name}
        p = self.prefs
        return {
            "boot": self._boot,
            "rev": next(self._revs),
            "version": __version__,
            "platform": sys.platform,
            "config_path": str(self.config_path),
            "data_dir": str(self.cfg.data_path),
            "setup_done": self.prefs.setup_done,
            "state": self.status,
            "in_meeting": meeting is not None,
            "meeting": meeting,
            "error": self.error,
            "restart_pending": self.restart_pending,
            "waiting_for_model": self.awaiting_model,
            "warnings": list(self.warnings),
            "mode": p.meeting_start,
            "suggestion": dict(self.suggestion) if self.suggestion is not None else None,
            "still_there": dict(self.still_there) if self.still_there is not None else None,
            "notice": dict(self.notice) if self.notice is not None else None,
            "prefs": {
                "listen_on_launch": p.listen_on_launch,
                "start_at_login": p.start_at_login,
                "ui_language": p.ui_language,
                "hotkey": p.hotkey.model_dump() if p.hotkey is not None else None,
                "hotkey_sound": p.hotkey_sound,
                "meeting_start": p.meeting_start,
                "floating_button": p.floating_button,
                "floating_position": p.floating_position.model_dump() if p.floating_position is not None else None,
                "spotlight_launcher": p.spotlight_launcher,
                "call_suggestions": p.call_suggestions,
                "silence_ask_minutes": p.silence_ask_minutes,
                "silence_end_minutes": p.silence_end_minutes,
            },
            "hotkey": {**self.hotkey, "recording": self._hotkey_recording is not None},
            "keyboard_layout": dict(self.keyboard_layout),
            "capabilities": dict(self.capabilities),
            "today_usd": round(self.costs.today.total_usd, 4),
        }

    def add_state_listener(self, fn: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        """``fn(state)`` after every lifecycle change, called on the host's loop thread (the native
        shell marshals it to the main thread). Returns a function that removes it."""
        self._state_listeners.append(fn)
        return lambda: self._state_listeners.remove(fn) if fn in self._state_listeners else None

    def _publish_state(self) -> None:
        state = self.state()
        self.bus.publish("app.state", **state)
        for fn in list(self._state_listeners):
            try:
                fn(state)
            except Exception:  # noqa: BLE001 - a broken listener must not break the host
                log.exception("state listener failed")

    def _set_status(self, status: State, error: str | None = None) -> None:
        self.status, self.error = status, error
        self._status_changed.set()
        self._publish_state()

    def add_warning(self, message: str) -> None:
        """Recent problems for the UI's banner: deduplicated, newest first, at most :data:`MAX_WARNINGS`.

        The same problem often arrives twice, as a ``source.error`` event and as the log line around it
        ("mic: capture/transcription failed: <error>"): those count as one, the shorter wording is kept."""
        message = message.strip()
        if not message:
            return
        for old in list(self.warnings):
            if old == message or old.endswith(f": {message}") or message.endswith(f": {old}"):
                self.warnings.remove(old)
                message = min(old, message, key=len)
        self.warnings.appendleft(message)

    def _drop_warnings(self, prefix: str) -> None:
        """The problem these warnings are about is over (a microphone is back, a download done)."""
        for old in [w for w in self.warnings if w.startswith(prefix)]:
            self.warnings.remove(old)

    def _on_mic_change(self, chosen: str, missing: bool, using: str | None, *, others: bool) -> None:
        """:class:`ChosenMic` went missing (``using``: what records instead) or came back."""
        self._drop_warnings(MIC_MISSING)
        if missing:
            instead = (f"using {using}" if using else "using meeting audio only" if others else "nothing is recorded")
            message = f"{MIC_MISSING}{chosen}) isn't connected — {instead} until it is back"
            log.warning("%s", message)
            self.add_warning(message)
        else:
            log.info("your microphone (%s) is connected again", chosen)
        self._publish_state()

    def audio_devices_changed(self) -> None:
        """A device was connected or removed (the native shell's ``watch_audio_devices``; on the loop):
        a microphone that isn't connected is looked for again at once."""
        for a in self.assistants:
            for src in a.sources:
                if isinstance(src, ChosenMic):
                    src.poke()

    def _on_log_warning(self, message: str) -> None:
        self.add_warning(message)
        self.bus.publish("app.warning", message=message)

    def _on_event(self, ev: Event) -> None:
        if ev.type == "source.error":
            msg = human_error(str(ev.data.get("error") or ""))
            self._last_source_error = msg
            self.add_warning(msg)
        elif ev.type == "source.stopped" and self.status in ("listening", "paused", "starting"):
            # a source gave up for good (a missing package or permission…): its source.error is in the
            # warnings already. While another one still records it stays at that — an error state would
            # say listening stopped, and Start would do nothing — else nothing is recorded any more
            name = str(ev.data.get("source") or "")
            self._dead_sources.add(name)
            a = self._assistant
            if a is not None and any(s.live and s.name not in self._dead_sources for s in a.sources):
                self._publish_state()
            else:
                self._set_status("error", human_error(str(ev.data.get("error") or f"{name} stopped")))
        elif ev.type == "segment":
            if self.still_there is not None and ev.data.get("meeting_id") == self.still_there.get("meeting_id"):
                self.still_there = None  # someone spoke: still in the meeting, the question goes away
                self._publish_state()
        elif ev.type == "meeting.started":
            self.notice = None  # what ended the last one is old news
            self._publish_state()
        elif ev.type in ("assistant.paused", "assistant.resumed"):
            if ev.type == "assistant.paused":
                # its "ends at" no longer holds (time on Pause doesn't count), and paused, the floating button
                # must offer Resume, not Keep/End: asked again after Resume, with the time it ends then
                self.still_there = None
            self._publish_state()
        elif ev.type == "meeting.still_there":
            a = self._assistant
            cur = a.current if a is not None else None
            if cur is not None and cur.meeting.id == ev.data.get("meeting_id") and not self._stop_requested:
                self.still_there = {k: ev.data.get(k) for k in ("meeting_id", "silent_minutes", "ends_at")}
                self._publish_state()
        elif ev.type == "meeting.kept":
            if self.still_there is not None:
                self.still_there = None
                self._publish_state()
        elif ev.type == "assistant.ending":
            self._on_ending(ev)
        elif ev.type == "meeting.ending":
            if self.still_there is not None and self.still_there.get("meeting_id") == ev.data.get("meeting_id"):
                self.still_there = None
            # it is over (no longer current) and wraps up in the background: an automatic one closed on a
            # silence or by "End meeting now" says nothing else until its notes are written, and the shell's
            # floating button and menu bar would show it recording meanwhile
            self._publish_state()
        elif ev.type == "meeting.ended":
            note = self.notice
            if note is not None and note.get("meeting_id") == ev.data.get("meeting_id"):
                where = ev.data.get("dir")  # none when nothing was said: there is no meeting to open
                note.update(title=ev.data.get("title"), folder=Path(str(where)).name if where else None, pending=False)
            self._publish_state()
            if self.restart_pending:
                self.track(asyncio.ensure_future(self._apply_pending()))

    def _on_ending(self, ev: Event) -> None:
        """A meeting started by hand is ending by itself, and listening with it (End meeting on the page,
        a long silence…: ``assistant.ending``): a stop like any other, so the state says so at once and the
        next meeting can start as soon as this one is wrapped up (see :meth:`_stop`)."""
        a = self._assistant
        if a is None or not a.manual:
            return
        self.still_there = None
        if ev.data.get("reason") == "silence":
            # pending: its notes are being written (meeting.ended says whether there are any, and where)
            self.notice = {"kind": "ended_after_silence", "meeting_id": ev.data.get("meeting_id"),
                           "minutes": round(a.end_after_s / 60), "at": utcnow().isoformat(), "title": None,
                           "folder": None, "pending": True}  # fmt: skip
        if not self._stop_requested and not self._closing:
            self.track(asyncio.ensure_future(self.listen_stop()))
        else:
            self._publish_state()

    # --------------------------------------------------------------- listening

    def _preflight(self) -> str | None:
        """Problems that would make the assistant fail at once, said plainly before trying."""
        if self.config_error:
            return self.config_error
        tc = self.cfg.transcription
        if tc.backend in _STT_KEYS:
            key, name = _STT_KEYS[tc.backend]
            if not os.environ.get(key):
                return f"Speech-to-text with {name} needs your {name} key: Settings → Speech-to-text"
        if tc.backend == "whisper-cpp":
            from ..transcribe.whisper_cpp import check as whisper_check

            status, detail = whisper_check(self.cfg)
            if status == "fail":
                return human_error(detail)
        return None

    def _checked_preflight(self) -> str | None:
        try:
            return self._preflight()
        except Exception as exc:  # noqa: BLE001 - e.g. a hand-edited path that can't be expanded: shown, not raised
            log.warning("could not start listening: %s", exc)
            return human_error(exc)

    def _missing_model(self) -> str | None:
        """The on-device speech model the settings name, when it isn't downloaded (else ``None``)."""
        tc = self.cfg.transcription
        name = tc.whisper_cpp.model
        if tc.backend != "whisper-cpp" or name not in stt_models.CATALOG:
            return None
        if stt_models.is_installed(self.cfg, name) or stt_models.model_path(self.cfg, name).is_file():
            return None
        return name

    def _wait_for_model(self, problem: str, then: str) -> bool:
        """The preflight's ``problem`` is that the speech model the settings name isn't downloaded:
        download it (or let the download under way go on) and wait for it rather than fail — choosing
        a model in Settings, or quitting during the first download, must not leave listening off for
        the day. ``then`` says what happens once it is in. Returns whether a download is coming."""
        name = self._missing_model()
        if name is None or problem != human_error(f"model {name} not downloaded"):
            return False  # something else is wrong (whisper-server missing…): that is what to say
        self.model_download(name)  # a no-op while it is downloading already
        if name not in self._downloads:
            return False
        self.awaiting_model = name
        self._drop_warnings(MODEL_WAIT)
        self.add_warning(f"{MODEL_WAIT}: {then} once it's ready (Settings → Speech-to-text shows the progress)")
        return True

    def _done_waiting(self) -> None:
        self.awaiting_model = None
        self._start_when_ready = False
        self._drop_warnings(MODEL_WAIT)

    async def _model_ready(self) -> None:
        """:attr:`awaiting_model` is in: switch to it (between meetings, as any settings change), or start."""
        start = self._start_when_ready
        self._done_waiting()
        if self._closing:
            return
        if self._assistant is not None:
            log.info("the speech model is ready: switching to it")
            await self.apply_settings()
        elif start:
            await self.listen_start()
        else:
            self._publish_state()

    async def listen_start(self, paused: bool = False) -> None:
        """Start listening (idempotent); ``paused`` starts it paused (a restart while paused). A failure
        becomes state ``error`` with a plain message; a speech model still downloading, state
        ``starting`` until it is in."""
        if self._run_task is not None and self._stop_requested:
            await asyncio.wait({self._run_task})  # a stop (or restart) still winding down
        if self._closing:
            return
        if self._assistant is not None:
            if self.status == "error":
                # what failed may have left the rest running (a source gave up): a restart replaces it
                await self.listen_restart()
            return
        await self.refresh_config()  # edited by hand since?
        if self._assistant is not None or self._closing:
            return
        self._withdraw_suggestion()  # a recording is starting: the call suggested is (about to be) taken down
        problem = self._checked_preflight()
        if problem and self._wait_for_model(problem, "listening starts by itself"):
            self._start_when_ready = True
            self._set_status("starting")
            return
        if problem:
            self._set_status("error", problem)
            return
        self._done_waiting()
        self._drop_warnings(NOT_APPLIED)  # what starts now is the current settings
        self._set_status("starting")
        try:
            assistant = self._factory(self)
        except Exception as exc:  # noqa: BLE001 - shown to the person
            log.warning("could not start listening: %s", exc)
            self._set_status("error", human_error(exc))
            return
        self._apply_mode(assistant)
        self._assistant = assistant
        self._stop_requested = False
        self._last_source_error = None
        self._dead_sources = set()
        if paused:
            assistant.pause()
        started = asyncio.Event()
        unlisten = self.bus.listen(lambda ev: started.set() if ev.type == "assistant.started" else None)
        task = self._run_task = asyncio.ensure_future(self._run(assistant))
        waiter = asyncio.ensure_future(started.wait())
        try:
            await asyncio.wait({task, waiter}, timeout=START_WAIT_S, return_when=asyncio.FIRST_COMPLETED)
        finally:
            unlisten()
            waiter.cancel()
        if self._assistant is assistant and not task.done() and self.status == "starting":
            self._set_status("paused" if assistant.paused else "listening")

    def _apply_mode(self, a: Assistant) -> None:
        """How meetings start (``prefs.meeting_start``), for an assistant about to run or already running:
        manual, it records one meeting, opened at once, with the silence thresholds of the preferences."""
        p = self.prefs
        a.manual = p.meeting_start == "manual"
        a.ask_after_s = p.silence_ask_minutes * 60.0
        a.end_after_s = p.silence_end_minutes * 60.0

    async def _run(self, assistant: Assistant) -> None:
        failure: BaseException | None = None
        try:
            await assistant.run()
        except asyncio.CancelledError:
            log.warning("listening was cancelled (forced stop)")
        except Exception as exc:  # noqa: BLE001 - shown to the person, logged
            failure = exc
            log.warning("listening stopped: %s", exc)
        finally:
            for s in [assistant.current, *assistant.finished_meetings]:
                if s is not None and s.meeting.ended_at is None:
                    # its wrap-up was cut short (a forced stop cancelled it): unlocked, its lock file left
                    # in place marks it as cut off, so Meetings finishes it now (not in an hour)
                    s.store.release(ended=False)
            settled = self._settle_notice(assistant)
            if self._retiring.pop(assistant, None) is not None:
                # it had handed listening over already (see _stop): its agents are done now
                if settled:
                    self._publish_state()
            else:
                if self._assistant is assistant:
                    self._assistant = None
                stopped_on_purpose = self._stop_requested
                self._stop_requested = False
                self._run_task = None
                if stopped_on_purpose:
                    self.restart_pending = False  # the next assistant is built from the current settings anyway
                if failure is not None:
                    self._set_status("error", human_error(failure))
                elif not stopped_on_purpose:
                    # run() returned by itself: every capture ended (a device unplugged, a fatal source error)
                    self._set_status("error", self._last_source_error or "Listening stopped unexpectedly (see the log)")
                else:
                    self._set_status("stopped")

    def _settle_notice(self, assistant: Assistant) -> bool:
        """``assistant`` is over, however it ended: a notice still saying one of its meetings' notes are being
        written (``pending``) stops saying so — that wrap-up never said it was done (cut short by a forced
        stop, failed) and won't now. Returns whether it did."""
        note = self.notice
        ids = {s.meeting.id for s in [assistant.current, *assistant.finished_meetings] if s is not None}
        if note is None or not note.get("pending") or note.get("meeting_id") not in ids:
            return False
        note["pending"] = False
        return True

    async def listen_pause(self) -> None:
        a = self._assistant
        if a is None or self._stop_requested:
            return
        a.pause()
        if self.status in ("listening", "starting"):
            self._set_status("paused")

    async def listen_resume(self) -> None:
        a = self._assistant
        if a is None or self._stop_requested:
            return
        a.resume()
        if self.status == "paused":
            self._set_status("listening")

    async def listen_stop(self, force: bool = False, timeout: float | None = None) -> None:
        """Stop listening and wait for it (``timeout``: give up waiting, the stop continues). The meeting
        in progress is wrapped up unless ``force``; a forced stop never takes more than ~:data:`FORCE_STOP_S`.
        Calling it with ``force`` while a normal stop is under way escalates that stop. A restart under
        way (e.g. "apply now") then stays stopped, and so does a start waiting for the stop under way."""
        if force or self._stop_requested:
            self._start_queued = False  # Stop (now) while the last meeting is wrapped up: the next one doesn't start
        self._stop_pressed = True
        await self._stop(force, timeout)
        if not self._closing:
            await self.refresh_config()  # edited by hand since? (and a fixed file clears its error)

    async def _stop(self, force: bool = False, timeout: float | None = None) -> None:
        task, a = self._run_task, self._assistant
        if task is None or task.done() or a is None:
            if self._start_when_ready:  # waiting for a speech model to start: not any more
                self._done_waiting()
                self._set_status("stopped")
            elif self.status == "error":  # Stop dismisses the error
                self._set_status("stopped")
            return
        if not self._stop_requested:
            self._stop_requested = True
            self._set_status("stopping")
        a.stop(force=force)
        if force:
            await asyncio.wait({task}, timeout=FORCE_STOP_S)
            if not task.done():
                log.warning("forced stop: cancelling the assistant")
                task.cancel()
                await asyncio.wait({task}, timeout=FORCE_STOP_S)
            return
        # wait for the capture to close and the meeting in progress to be wrapped up — not for an ended
        # meeting's agents, which may work on for many minutes with nothing being recorded meanwhile
        wound_down = getattr(a, "wound_down", None)
        waiter = asyncio.ensure_future(wound_down.wait()) if wound_down is not None else None
        try:
            await asyncio.wait({task, *([waiter] if waiter else [])}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if waiter is not None:
                waiter.cancel()
        if not task.done() and wound_down is not None and wound_down.is_set() and self._assistant is a:
            self._retire(a, task)

    def _retire(self, a: Assistant, task: asyncio.Task[None]) -> None:
        """``a`` has stopped listening; it finishes its ended meetings' agents in the background (its
        task ends by itself, see :meth:`_run`) and listening can start again meanwhile."""
        log.info("listening stopped; an earlier meeting's agents keep working in the background")
        self._retiring[a] = task
        self._assistant = None
        self._run_task = None
        self._stop_requested = False
        self.restart_pending = False
        self._set_status("stopped")

    async def _finish_retiring(self, timeout: float) -> None:
        """Quitting: give the agents still working after a stop ``timeout`` seconds, then cut them short."""
        tasks = dict(self._retiring)
        if not tasks:
            return
        if timeout > 0:
            await asyncio.wait(set(tasks.values()), timeout=timeout)
        for a, task in tasks.items():
            if not task.done():
                a.stop(force=True)  # agents are cancelled and marked so, the notes say so
        pending = {t for t in tasks.values() if not t.done()}
        if pending:
            await asyncio.wait(pending, timeout=FORCE_STOP_S)
            for t in pending:
                if not t.done():
                    t.cancel()
            await asyncio.wait(pending, timeout=FORCE_STOP_S)

    async def listen_restart(self, force: bool = False) -> None:
        """Stop (ending the meeting in progress) and start again with the current settings."""
        paused = self._assistant.paused if self._assistant is not None else False
        self.restart_pending = False
        self._stop_pressed = False
        if self._assistant is not None:
            await self._stop(force=force)
            if self._stop_pressed:  # you pressed Stop while the meeting was being wrapped up: stay stopped
                return
        await self.listen_start(paused=paused)

    # ---------------------------------------------------------------- meetings

    def meeting_action(self, what: Literal["start", "stop", "toggle"] = "toggle") -> MeetingAction | None:
        """What :meth:`meeting_start`, :meth:`meeting_stop` or :meth:`meeting_toggle` would do now, or
        ``None``: nothing to do, or nothing while listening starts, while the previous action is still
        being carried out (the gap before the state says so) and once the app is quitting. While the last
        meeting is being wrapped up (``stopping``), a start waits for it (:meth:`_start_after_stop`): its
        microphone is closed already, but the summary, pushes and agents can take minutes, and back-to-back
        meetings mustn't lose the next one's start. A stop is never held back by a start under way: Stop
        must always be able to end a recording, or take back a start that waits."""
        if self._closing:
            return None
        if what == "stop":
            return "stop" if self.status in STOPPABLE or self._start_queued else None
        if self._start_waits():
            return "start"
        if self._toggling is not None and not self._toggling.done():
            return None
        return (TOGGLE if what == "toggle" else START).get(self.status)  # type: ignore[return-value]

    def _start_waits(self) -> bool:
        """A start now waits for the last meeting's wrap-up: a stop is under way (or was just asked for, and
        the state doesn't say so yet), or a start waits for one already."""
        busy = self._toggling is not None and not self._toggling.done()
        return self._start_queued or self.status == "stopping" or (busy and self._toggled == "stop")

    def toggle_action(self) -> str | None:
        """What a press of the keyboard shortcut does now (:data:`.hotkey.TOGGLE`): :meth:`meeting_action`."""
        return self.meeting_action("toggle")

    async def _carry_out(self, action: MeetingAction | None) -> MeetingAction | None:
        """Do what a meeting action decided. A stop returns at once: wrapping a meeting up can take a while,
        and goes on in the background. A start, restart or resume once it is carried out (a start returns
        once listening is up, or after :data:`START_WAIT_S`; one that waits for the last meeting's wrap-up,
        after that too), or ``"error"`` when it failed — a missing key, a permission, a microphone that
        gave up: those become state ``error`` rather than an exception, and the shell's sound must not say
        it worked. ``None`` for a start taken back while it waited."""
        if action is None:
            return None
        if action == "start" and self._start_waits():
            return await self._queue_start()
        if action == "stop":
            self._start_queued = False  # a start waiting for the last meeting's wrap-up is taken back
        run = {"start": self.listen_start, "restart": self.listen_restart, "stop": self.listen_stop,
               "resume": self.listen_resume}[action]  # fmt: skip
        task = self._toggling = asyncio.ensure_future(run())
        self._toggled = action
        self.track(task)
        if action == "stop":
            await asyncio.sleep(0)  # its first step: the state says "stopping" by the time this returns
            return action
        await asyncio.wait({task})  # the work isn't cancelled if this is
        return "error" if self.status == "error" else action

    async def _queue_start(self) -> MeetingAction | None:
        """A start while the last meeting is being wrapped up: once that is done (:meth:`_start_after_stop`).
        Pressed again meanwhile, it is the same start (also after a Stop took it back: the last word wins)."""
        busy = self._toggling is not None and not self._toggling.done()
        before = self._toggling if busy and self._toggled == "stop" else None
        self._start_queued = True
        if self._queued_start is None or self._queued_start.done():
            self._queued_start = asyncio.ensure_future(self._start_after_stop(before))
            self.track(self._queued_start)
            log.info("the last meeting is being wrapped up: the next one starts as soon as it is")
        task = self._toggling = self._queued_start
        self._toggled = "start"
        await asyncio.wait({task})  # the work isn't cancelled if this is
        if task.cancelled() or task.exception() is not None or not task.result():
            return None
        return "error" if self.status == "error" else "start"

    async def _start_after_stop(self, before: asyncio.Task[Any] | None) -> bool:
        """Wait for the stop under way — ``before``, a Stop pressed just before that hasn't begun yet, then
        the state ``stopping`` (the capture closed, the meeting wrapped up) — and start listening, unless the
        start was taken back meanwhile (:attr:`_start_queued`: Stop, Stop now) or the app is quitting. Not
        on the run task: it also waits for an ended meeting's agents, and is gone once the stop is over."""
        if before is not None:
            await asyncio.wait({before})
        while self.status == "stopping" and not self._closing:
            self._status_changed.clear()
            await self._status_changed.wait()
        queued, self._start_queued = self._start_queued, False
        if not queued or self._closing:
            log.info("the start waiting for the last meeting's wrap-up was taken back")
            return False
        await self.listen_start()
        return True

    async def meeting_start(self) -> MeetingAction | None:
        """Start a meeting: every Start button (the window, the menu bar, the floating button, the Spotlight
        launcher, a link) comes here. Manual: listening starts with the meeting open at once. Automatic:
        listening starts and the meeting opens on the first speech, as always. After a problem, a restart
        as Start does then; paused, Resume. Returns what it did (:meth:`_carry_out`), ``None`` for nothing."""
        return await self._carry_out(self.meeting_action("start"))

    async def meeting_stop(self) -> MeetingAction | None:
        """Stop: the meeting in progress is wrapped up (summary, notes, pushes) and the microphone and
        meeting audio are let go. Returns ``"stop"`` at once (the wrap-up goes on in the background), or
        ``None`` when nothing is recorded."""
        return await self._carry_out(self.meeting_action("stop"))

    async def meeting_toggle(self) -> MeetingAction | None:
        """The keyboard shortcut (and ``voicestream://meeting/toggle``): start when stopped; after a
        problem, restart as Start does then; stop when listening — the meeting in progress is wrapped up
        and its notes written, as with Stop, but nobody is asked first (you pressed it on purpose, maybe
        in another app); resume when paused. Returns what it did, as :meth:`meeting_start` does."""
        return await self._carry_out(self.meeting_action("toggle"))

    async def listen_toggle(self) -> MeetingAction | None:
        """The shortcut's earlier name: :meth:`meeting_toggle`."""
        return await self.meeting_toggle()

    async def meeting_keep(self) -> bool:
        """The answer "Keep" to "Still in a meeting?" (:attr:`still_there`): the meeting goes on and its
        silence counts start over. Returns whether a meeting was there to keep."""
        a = self._assistant
        # only a meeting started by hand is asked: always-on meetings end on a silence by design
        kept = a is not None and a.manual and not self._stop_requested and a.keep_meeting()
        if self.still_there is not None:
            self.still_there = None
            self._publish_state()
        return kept

    async def end_meeting(self) -> MeetingAction | Literal["end"] | None:
        """The menu bar's "End meeting now". A meeting started by hand: the same as :meth:`meeting_stop`
        (listening ends with it). Automatic: the meeting ends (summary, notes, pushes) in the background
        and listening goes on, the next speech starting the next one (``"end"``). ``None``: no meeting."""
        a = self._assistant
        if a is None or self._stop_requested or self._closing or not self.in_meeting():
            return None
        if a.manual:
            return await self.meeting_stop()
        task = asyncio.ensure_future(a.end_meeting("manual"))
        a.track(task)  # so a stop waits for it
        return "end"

    # ------------------------------------------------------------- suggestions

    def suggest_meeting(self, app_name: str | None = None, call_id: str | None = None) -> bool:
        """(On the loop.) The native shell: another app started using the microphone — a call, most likely
        (``app_name``: which one, if it could tell). Unless something is recorded already, the setup isn't
        done or suggestions are off (``prefs.call_suggestions``), the window and the floating button
        suggest taking notes (:attr:`suggestion`); never more than once for the same call: ``call_id``
        when the shell can tell calls apart, else the app's name, until :meth:`call_ended` (or after
        :data:`CALL_MEMORY_S`). It never starts anything. Returns whether it is suggested."""
        if self._closing or not self.prefs.call_suggestions or not self.prefs.setup_done:
            return False
        if self.status in ("starting", "listening", "paused"):
            return False  # recording already, or about to
        name = _app_name(app_name)
        key = self._call_key(call_id, name)
        now = self.wall()
        self._suggested = {k: t for k, t in self._suggested.items() if now - t < CALL_MEMORY_S}
        if key in self._suggested:
            return False
        self._suggested[key] = now
        self._suggestion_key = key
        self.suggestion = {"app": name, "at": utcnow().isoformat()}
        log.info("a call seems to have started%s: suggesting to take notes", f" ({name})" if name else "")
        self._publish_state()
        return True

    @staticmethod
    def _call_key(call_id: str | None, name: str | None) -> str:
        return (str(call_id or "").strip()[:200] or (name or "").lower()) or "?"

    def call_ended(self, app_name: str | None = None, call_id: str | None = None) -> None:
        """(On the loop.) The native shell: that app stopped using the microphone (with neither, every app
        did). Its suggestion, if still shown, is taken down, and its next call may be suggested again."""
        if app_name is None and call_id is None:
            self._suggested.clear()
            gone = self.suggestion is not None
        else:
            key = self._call_key(call_id, _app_name(app_name))
            self._suggested.pop(key, None)
            gone = self.suggestion is not None and self._suggestion_key == key
        if gone:
            self._withdraw_suggestion()
            self._publish_state()

    def _withdraw_suggestion(self) -> bool:
        had = self.suggestion is not None
        self.suggestion, self._suggestion_key = None, None
        return had

    async def suggestion_accept(self) -> MeetingAction | None:
        """"Start" on the suggestion: :meth:`meeting_start` (also when it was taken down meanwhile: starting
        is what was asked)."""
        if self._withdraw_suggestion():
            # said before the start, which may wait minutes for the last meeting's wrap-up: a suggestion still
            # shown meanwhile would offer a "Not now" that takes back nothing
            self._publish_state()
        return await self.meeting_start()

    def suggestion_dismiss(self) -> bool:
        """(On the loop.) "Not now": the suggestion goes away; this call isn't suggested again."""
        if not self._withdraw_suggestion():
            return False
        self._publish_state()
        return True

    def dismiss_notice(self) -> bool:
        """(On the loop.) The page or the floating button showed :attr:`notice`: it goes away."""
        if self.notice is None:
            return False
        self.notice = None
        self._publish_state()
        return True

    def request_focus(self, target: Literal["start"] = "start") -> None:
        """(On the loop.) The native shell showed the window because you opened Voicestream again (from
        Spotlight, the Dock…): the page puts the keyboard focus on the Start control (event ``app.focus``)."""
        self.bus.publish("app.focus", target=target)

    # ------------------------------------------------------------------- links

    def launcher_url(self) -> str:
        """(On the loop.) The link the Spotlight launcher opens to start a meeting, with the key that lets
        it (made and saved the first time). For the native shell only: never logged, never in the state."""
        self.prefs = with_url_key(self.prefs, self.prefs_path)
        return f"{URL_SCHEME}://meeting/start?key={self.prefs.url_key}"

    def _key_ok(self, given: str | None) -> bool:
        expected = self.prefs.url_key
        if not expected or not given:
            return False
        return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))  # in constant time

    async def open_url(self, url: str) -> str:
        """A ``voicestream://`` link the native shell was asked to open (the Spotlight launcher, a Shortcut,
        a link clicked somewhere). ``meeting/start`` and ``meeting/toggle`` must carry the key (``?key=``,
        see :meth:`launcher_url`): any app or web page can open a link, and must not be able to start a
        recording. ``meeting/stop`` and ``open`` need none: they can't record anything.

        Returns the outcome: what the meeting action did (``start``, ``restart``, ``resume``, ``stop``,
        ``error``), ``nothing`` (nothing to do), ``open`` (the shell shows the window), ``refused`` (no key,
        or not this one: :attr:`notice` says so) or ``unknown`` (not a link Voicestream knows)."""
        parsed = parse_url(url)
        if parsed is None:
            log.info("ignored a voicestream link this version doesn't know")
            return "unknown"
        action, given = parsed
        if action == "open":
            return "open"
        if action in KEYED_ACTIONS and not self._key_ok(given):
            log.info("ignored a link that asked to start recording without Voicestream's key")
            self.notice = {"kind": "link_refused", "at": utcnow().isoformat()}
            self._publish_state()
            return "refused"
        run = {"start": self.meeting_start, "stop": self.meeting_stop, "toggle": self.meeting_toggle}[action]
        return await run() or "nothing"

    async def _apply_pending(self) -> None:
        await asyncio.sleep(0)  # after the rest of the meeting.ended listeners
        if not self.restart_pending or self._stop_requested or self.in_meeting() or self._assistant is None:
            return
        log.info("applying the new settings now that the meeting has ended")
        await self.apply_settings()  # checked again: they may not be usable (yet)

    def _unusable_settings(self) -> str | None:
        """While listening: what would make the new settings fail at once (they are then not applied,
        and listening goes on as it is), or ``None``. A speech model still downloading is switched to
        when it is in; anything else stays a warning until another change fixes it."""
        problem = self._checked_preflight()
        self._drop_warnings(NOT_APPLIED)
        if problem is None:
            if self.awaiting_model is not None:
                self._done_waiting()  # the settings changed again: that model isn't wanted any more
            return None
        self.restart_pending = False
        if not self._wait_for_model(problem, "Voicestream keeps the current one until then and switches to it"):
            self.add_warning(f"{NOT_APPLIED}: {problem}")
        self._publish_state()
        return problem

    async def apply_settings(self, wait: bool = True) -> dict[str, Any]:
        """After a settings or key change: restart now when no meeting is in progress, else when it ends.
        Returns ``{restarting, restart_pending}``; ``wait=False`` restarts in the background. Settings
        that can't run yet (a model still downloading, a missing key) never stop listening."""
        await self.refresh_config(force=True)
        if self._assistant is None or self._stop_requested or self._closing:
            # not listening — or you pressed Stop and it is wrapping up: the next start reads the new
            # settings anyway, and restarting here would turn the microphone back on after your Stop
            failed = self.status == "error" and not self.config_error
            if not self._closing and (self._start_when_ready or (failed and self.prefs.meeting_start == "automatic")):
                # the fix for the failed start may be this very change (or the model waited for changed): try again
                coro = self.listen_start()
            else:
                if failed and self._assistant is None and not self._closing:
                    # meetings start by hand: none starts by itself, but this change may be the fix the
                    # problem shown needs, so it is checked again (Start says the rest)
                    problem = self._checked_preflight()
                    self.status, self.error = ("error", problem) if problem else ("stopped", None)
                self._publish_state()
                return {"restarting": False, "restart_pending": False}
        elif self._unusable_settings() is not None:
            return {"restarting": False, "restart_pending": False, "waiting_for_model": self.awaiting_model}
        elif self.in_meeting():
            self.restart_pending = True
            self._publish_state()
            return {"restarting": False, "restart_pending": True}
        else:
            coro = self.listen_restart()
        if wait:
            await coro
        else:
            self.track(asyncio.ensure_future(coro))
        return {"restarting": True, "restart_pending": False}

    # ------------------------------------------------------------------- prefs

    def update_prefs(self, changes: dict[str, Any]) -> Prefs:
        """Validate and save preference changes (the page's ``PATCH /api/prefs``, the native shell: the
        floating button's place…); launch-at-login goes through the native shell. A new way of starting
        meetings applies to the listening in progress at once (:meth:`_apply_mode`)."""
        if "start_at_login" in changes and changes["start_at_login"] is not None:
            hook = getattr(self.native_hooks, "set_start_at_login", None)
            if hook is not None:
                try:
                    hook(bool(changes["start_at_login"]))
                except Exception as exc:  # noqa: BLE001 - e.g. needs approval in System Settings
                    raise HostError(f"could not change launch at login: {exc}") from None
        old = self.prefs
        mode = old.meeting_start
        self.prefs = update_prefs(changes, self.prefs_path, layout=self.keyboard_layout)
        if "hotkey" in changes:
            # the shell registers it when it sees this state (the same one again: it tries again, as
            # one macOS refused may be free now), then says whether macOS took it
            self.hotkey = {"status": "pending", "error": None}
            self._end_hotkey_recording()
        if not self.prefs.call_suggestions:
            self._withdraw_suggestion()
        to_manual = mode != "manual" and self.prefs.meeting_start == "manual"
        limits = (old.silence_ask_minutes, old.silence_end_minutes) != (self.prefs.silence_ask_minutes,
                                                                        self.prefs.silence_end_minutes)
        a = self._assistant
        if a is not None and not self._stop_requested:
            # the new way (and silence thresholds) apply to the meeting in progress too: to manual, it
            # becomes the one this listening records; to automatic, listening goes on after it
            self._apply_mode(a)
            if limits:
                # "Still in a meeting?" said when the meeting ends by itself: asked again with the new limits
                # (or the meeting ends, or nothing is asked), as they and the silence so far say
                a.ask_again()
                self.still_there = None
            if self.prefs.meeting_start != mode and a.manual and not self.in_meeting():
                # nothing may be captured without a meeting started by hand: always-on listening stops
                self.track(asyncio.ensure_future(self.listen_stop()))
        elif a is not None and to_manual:
            # a restart under way (settings saved, a speech model just in) would start again in manual mode,
            # whose meeting opens at once: nobody started it. It stays stopped, as after a Stop (the restart
            # looks once its stop is done; the stop under way ends any meeting in progress anyway)
            self._stop_pressed = True
        elif a is None and self._start_when_ready and to_manual:
            # always-on listening was waiting for the speech model: once it is in, it would open a meeting
            # nobody started. The start is dropped (the download goes on); one pressed in manual mode waits on
            self._done_waiting()
            self.status, self.error = "stopped", None
        if self.prefs.meeting_start != "manual":
            self.still_there = None
        self._publish_state()
        return self.prefs

    def record_hotkey(self, active: bool) -> None:
        """(On the loop.) Settings is recording a new shortcut (``active``), or stopped: the shell turns the
        current one off meanwhile, so that pressing it reaches the page — for at most :data:`HOTKEY_RECORDING_S`."""
        self._end_hotkey_recording()
        if active:
            self._hotkey_recording = asyncio.get_running_loop().call_later(HOTKEY_RECORDING_S, self.record_hotkey, False)
        self._publish_state()

    def _end_hotkey_recording(self) -> None:
        if self._hotkey_recording is not None:
            self._hotkey_recording.cancel()
            self._hotkey_recording = None

    def set_hotkey_status(self, status: str, error: int | None = None) -> None:
        """The native shell, on the loop: whether macOS took the shortcut (see :attr:`hotkey`)."""
        new = {"status": status, "error": error}
        if new != self.hotkey:
            self.hotkey = new
            self._publish_state()

    def set_keyboard_layout(self, keys: dict[str, str]) -> None:
        """The native shell, on the loop: what the keys type on the Mac's keyboard layout (see
        :attr:`keyboard_layout`), read at launch and when Settings starts recording a shortcut."""
        if keys != self.keyboard_layout:
            self.keyboard_layout = dict(keys)
            self._publish_state()

    def complete_setup(self) -> Prefs:
        self.prefs = self.prefs.model_copy(update={"setup_done": True})
        save_prefs(self.prefs, self.prefs_path)
        self._publish_state()
        return self.prefs

    # ------------------------------------------------------------------ models

    def models(self) -> dict[str, Any]:
        """``GET /api/models``."""
        wc = self.cfg.transcription.whisper_cpp
        rows = []
        for m in stt_models.CATALOG.values():
            dl = self._downloads.get(m.name)
            rows.append({
                "name": m.name, "label": m.label, "size": m.size, "ram_gb": m.ram_gb, "note": m.note, "kind": m.kind,
                "recommended": m.recommended, "installed": stt_models.is_installed(self.cfg, m.name),
                "downloading": {"done": dl["done"], "total": dl["total"]} if dl else None,
            })  # fmt: skip
        # while listening, the model the running assistant loaded — the settings may name the next one
        running = self._running_cfg() or self.cfg
        in_use = running.transcription.whisper_cpp.model if running.transcription.backend == "whisper-cpp" else None
        return {"models": rows, "in_use": in_use, "selected": wc.model, "models_dir": str(stt_models.models_dir(self.cfg))}

    def _running_cfg(self) -> Config | None:
        """The settings the running assistant was built with (``None`` when not listening)."""
        if self._assistant is None:
            return None
        return getattr(self._assistant, "cfg", None) or self.cfg

    def model_download(self, name: str) -> bool:
        """Start downloading ``name`` in the background (no-op if it is already downloading or there).
        Raises ``KeyError`` for a name not in the catalogue. Also fetches the small voice-detection
        model whisper.cpp uses, when it is on and missing."""
        if name not in stt_models.CATALOG:
            raise KeyError(name)
        names = [name]
        info = stt_models.CATALOG[name]
        if info.kind == "whisper" and self.cfg.transcription.whisper_cpp.vad:
            names.append(stt_models.VAD_MODEL)
        started = False
        for n in names:
            if n in self._downloads or stt_models.is_installed(self.cfg, n):
                continue
            state: dict[str, Any] = {"done": 0, "total": stt_models.CATALOG[n].size, "last": 0.0}
            state["task"] = asyncio.ensure_future(self._download(n, state))
            self._downloads[n] = state
            started = True
        return started

    async def _download(self, name: str, state: dict[str, Any]) -> None:
        loop = asyncio.get_running_loop()

        def publish(kind: str, **extra: Any) -> None:
            self.bus.publish("model.download", name=name, done=state["done"], total=state["total"], state=kind, **extra)

        def progress(done: int, total: int | None) -> None:
            state["done"], state["total"] = done, total or state["total"]
            now = loop.time()
            if now - state["last"] >= DOWNLOAD_EVENT_S:
                state["last"] = now
                publish("running")

        publish("running")
        failed: str | None = None
        try:
            await stt_models.download(self.cfg, name, on_progress=progress)
        except asyncio.CancelledError:
            publish("cancelled")  # the .part file stays: the next download resumes it
            failed = human_error(f"model {name} not downloaded")
        except stt_models.DownloadError as exc:
            log.warning("download of %s failed: %s", name, exc)
            publish("error", error=str(exc))
            failed = f"download of {name} failed: {exc}"
        except Exception as exc:  # noqa: BLE001 - disk full, permissions…
            log.warning("download of %s failed: %s", name, exc)
            publish("error", error=f"{type(exc).__name__}: {exc}")
            failed = f"download of {name} failed: {type(exc).__name__}: {exc}"
        else:
            state["done"] = state["total"] = state["total"] or state["done"]
            publish("done")
        finally:
            self._downloads.pop(name, None)
        if self.awaiting_model == name and not self._closing:
            if failed is None:
                self.track(asyncio.ensure_future(self._model_ready()))
            else:  # listening goes on with the current model, or the start waiting for it fails now
                start = self._start_when_ready
                self._done_waiting()
                if start and self._assistant is None:
                    self._set_status("error", failed)
                else:
                    self._publish_state()

    def model_cancel(self, name: str) -> bool:
        if name not in stt_models.CATALOG:
            raise KeyError(name)
        dl = self._downloads.get(name)
        if dl is None:
            return False
        dl["task"].cancel()
        return True

    async def model_remove(self, name: str) -> bool:
        if name not in stt_models.CATALOG:
            raise KeyError(name)
        running = self._running_cfg()  # not self.cfg: a new model may be chosen, the old one still loaded
        tc = running.transcription if running is not None else None
        if tc is not None and tc.backend == "whisper-cpp" and name in (tc.whisper_cpp.model, stt_models.VAD_MODEL):
            raise HostError("this model is in use: stop listening or pick another one first")
        dl = self._downloads.get(name)
        if dl is not None:
            dl["task"].cancel()
            with contextlib.suppress(BaseException):
                await dl["task"]
        return stt_models.remove(self.cfg, name)

    # ---------------------------------------------------------------- lifecycle

    def _install_logging(self) -> None:
        assert self.loop is not None
        mirror = logs.WarningMirror(self._on_log_warning, self.loop)
        vs = logging.getLogger("voicestream")
        vs.addHandler(mirror)  # its own level is WARNING; the logger's stays as configured (INFO for the file)
        self._log_handlers.append((vs, mirror))
        if self.process_setup:
            try:
                handler = logs.install_file_log(logs.log_path(self.cfg.data_path))
                self._log_handlers.append((logging.getLogger(), handler))
            except OSError as exc:
                log.warning("no log file: %s", exc)

    async def start(self, *, auto_listen: bool = True) -> str:
        """Start the dashboard server (and listening, if set up to: :meth:`listen_on_launch`); returns the app
        page's URL with the token. ``auto_listen=False`` leaves listening to the caller: the Mac app asks
        for it once it knows no ``voicestream://`` link launched it (any web page can open a link)."""
        from ..dashboard.server import serve

        self.loop = asyncio.get_running_loop()
        self._closed = asyncio.Event()
        self._install_logging()
        log.info("voicestream %s app starting (config %s)", __version__, self.config_path)
        base, self._server_task = await serve(self, DashboardConfig(host="127.0.0.1", port=self.port or 0), token=self.token)
        self.url = base.replace("/?token=", "/app?token=", 1)
        try:
            # a crash, a force quit or a power cut during a meeting left it open: end it, so it can be
            # finished from Meetings (notes, to-dos) — never one another voicestream is still recording
            for folder in self.meetings.recover_interrupted():
                log.info("ended %s, whose recording was cut off", folder)
        except OSError as exc:
            log.warning("could not look for interrupted meetings: %s", exc)
        watch = getattr(self.native_hooks, "watch_audio_devices", None)
        if watch is not None:
            loop = self.loop
            try:
                watch(lambda: loop.call_soon_threadsafe(self.audio_devices_changed))
            except Exception as exc:  # noqa: BLE001 - the periodic look still finds the microphone
                log.debug("no audio device notifications: %s", exc)
        if not (auto_listen and await self.listen_on_launch()):
            self._publish_state()
        return self.url

    async def listen_on_launch(self) -> bool:
        """"Start listening when Voicestream opens": always-on listening starts, if the setup is done, meetings
        start by themselves, the preference is on, the settings file can be read and nothing runs yet (a link
        that launched the app may have started a meeting). Returns whether listening started (or waits for
        its speech model)."""
        p = self.prefs
        # manual: nothing is captured until you start a meeting, whatever listen_on_launch says
        if not (p.setup_done and p.meeting_start == "automatic" and p.listen_on_launch) or self.config_error:
            return False
        busy = self._toggling is not None and not self._toggling.done()
        if self._closing or busy or self._assistant is not None or self._start_when_ready or self.status != "stopped":
            return False
        await self.listen_start()
        return self.status in ("starting", "listening", "paused")

    async def shutdown(self, force: bool = False) -> None:
        """Stop listening (a graceful wrap-up of up to :data:`GRACEFUL_SHUTDOWN_S`, then forced; at once
        forced with ``force``), downloads and the server. Safe to call again to escalate."""
        self._closing = True  # a restart in flight (settings, pending after a meeting) must not start a new one
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (0 if force else GRACEFUL_SHUTDOWN_S)
        if self._run_task is not None:
            if not force:
                await self.listen_stop(timeout=GRACEFUL_SHUTDOWN_S)
            if self._run_task is not None:
                await self.listen_stop(force=True)
        # agents still working for a meeting that ended before a stop: the rest of the time, then cut short
        await self._finish_retiring(max(0.0, deadline - loop.time()))
        for dl in list(self._downloads.values()):
            dl["task"].cancel()
        pending = [t for t in self._background if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=0 if force else 5)
            for t in pending:
                t.cancel()
        server, self._server_task = self._server_task, None
        if server is not None:
            server.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(server, 5)
        for logger, handler in self._log_handlers:
            logger.removeHandler(handler)
            with contextlib.suppress(Exception):
                handler.close()
        self._log_handlers.clear()
        if self._closed is not None:
            self._closed.set()

    # ------------------------------------------------------- native shell glue

    def run_in_thread(self, timeout: float = 60.0, *, auto_listen: bool = True) -> str:
        """Run the host's loop in a background thread, start it (``auto_listen``: see :meth:`start`), and
        return the URL once the server answers. Raises what :meth:`start` raised, or ``TimeoutError``."""
        ready = threading.Event()
        box: dict[str, Any] = {}

        async def main() -> None:
            try:
                box["url"] = await self.start(auto_listen=auto_listen)
            except BaseException as exc:
                box["error"] = exc
                raise
            finally:
                ready.set()
            assert self._closed is not None
            await self._closed.wait()

        def target() -> None:
            with contextlib.suppress(BaseException):
                asyncio.run(main())
            ready.set()

        self.thread = threading.Thread(target=target, name="voicestream-host", daemon=True)
        self.thread.start()
        if not ready.wait(timeout):
            raise TimeoutError("the voicestream host did not start in time")
        if "error" in box:
            raise box["error"]
        return str(box["url"])

    def call(self, coro: Coroutine[Any, Any, Any]) -> concurrent.futures.Future[Any]:
        """Run ``coro`` on the host's loop from another thread (``host.call(host.listen_pause())``)."""
        if self.loop is None:
            coro.close()
            raise RuntimeError("the host is not running")
        return asyncio.run_coroutine_threadsafe(coro, self.loop)


def _default_assistant(host: AppHost) -> Assistant:
    """An assistant on the host's settings, bus and cost meter, whose microphone picked by name is a
    :class:`ChosenMic` (it may not be connected); the other sources as ``voicestream run`` builds them."""
    from ..audio import build_source
    from ..session import Assistant

    cfg = host.cfg
    clock = Clock()  # the assistant's own: the sources stamp their frames on it
    mic = cfg.capture.mic
    others = [s for s in cfg.capture.sources if s != "mic"]
    sources = []
    for spec in cfg.capture.sources:
        if spec == "mic" and _named_device(mic.device):
            on_change = functools.partial(host._on_mic_change, str(mic.device), others=bool(others))
            sources.append(ChosenMic(str(mic.device), clock, use_default=mic.use_default_when_missing, on_change=on_change))
        else:
            sources.append(build_source(spec, cfg, clock))
    for src in sources:
        if isinstance(src, ChosenMic):  # a PortAudio rescan would close another PortAudio device's stream
            src.may_rescan = not any(isinstance(o, MicSource) and o is not src for o in sources)
    assistant = Assistant(cfg, sources=sources, bus=host.bus, costs=host.costs)
    assistant.clock = clock
    return assistant
