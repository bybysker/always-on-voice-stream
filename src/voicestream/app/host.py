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
now" (``listen_restart``).

The native shell talks to the host from its main thread with :meth:`AppHost.run_in_thread`,
:meth:`AppHost.call` and :meth:`AppHost.add_state_listener`. Optional platform glue lives in
``voicestream.app.native_hooks`` when the shell provides it (all functions optional):

* ``microphone_status() -> "granted" | "denied" | "undetermined" | "restricted"`` (AVFoundation);
* ``set_start_at_login(enabled: bool) -> None`` (SMAppService; raises on failure).

The console printer, signal handlers and stdin commands of ``voicestream run`` are not used here.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import importlib
import logging
import os
import secrets as stdlib_secrets
import sys
import threading
from collections import deque
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from .. import __version__, stt_models
from ..config import Config, ConfigError, DashboardConfig
from ..costs import CostMeter
from ..events import EventBus
from ..models import Event
from . import logs
from . import secrets as keys
from .meetings import Meetings
from .prefs import Prefs, load_prefs, save_prefs, update_prefs
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

_STT_KEYS = {"openai": ("OPENAI_API_KEY", "OpenAI"), "openai-realtime": ("OPENAI_API_KEY", "OpenAI"),
             "deepgram": ("DEEPGRAM_API_KEY", "Deepgram")}  # fmt: skip


def human_error(exc: BaseException | str) -> str:
    """A message for the person, not the log: what is wrong and where to fix it in the app."""
    from ..audio import CaptureSetupError
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
        self.config_error: str | None = None
        self.cfg: Config = self._load_config() or Config()
        self.costs = CostMeter(self.cfg, self.bus)
        self.meetings = Meetings(self)
        self.native_hooks: Any = _native_hooks()
        self.capabilities: dict[str, bool] = {
            "native_window": False,  # the native shell sets it
            "login_item": _login_item_supported(self.native_hooks),
            "keychain": keys.store_name() == "keychain",
        }
        self.status: State = "stopped"
        self.error: str | None = self.config_error
        if self.error:
            self.status = "error"
        self.restart_pending = False
        self.warnings: deque[str] = deque(maxlen=MAX_WARNINGS)
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
        self._factory = assistant_factory or _default_assistant
        self._background: set[asyncio.Task[Any]] = set()
        self._downloads: dict[str, dict[str, Any]] = {}
        self._state_listeners: list[Callable[[dict[str, Any]], None]] = []
        self._server_task: asyncio.Task[None] | None = None
        self._log_handlers: list[tuple[logging.Logger, logging.Handler]] = []
        self._closed: asyncio.Event | None = None
        self._unlisten = self.bus.listen(self._on_event)

    # ------------------------------------------------------------------ config

    def _load_config(self) -> Config | None:
        try:
            cfg = load_app_config(self.config_path)
        except (ConfigError, OSError, ValueError) as exc:
            self.config_error = f"Your settings file has a problem: {exc}"
            log.error("%s", self.config_error)
            return None
        except RuntimeError as exc:  # Path.expanduser: "~name/…" of an account that doesn't exist
            self.config_error = (f"Your settings file has a problem: {exc} "
                                 "(a path starts with ~name/ — write ~/… for your own folder)")
            log.error("%s", self.config_error)
            return None
        self.config_error = None
        return cfg

    def reload_config(self) -> None:
        """Re-read the config (after a settings change); the running assistant keeps its own until restarted."""
        cfg = self._load_config()
        if cfg is None:
            return
        self.cfg = cfg
        self.costs.cfg = cfg  # prices, budget and spend folder follow the new settings
        if self.status == "error" and self.error and self.error.startswith("Your settings file"):
            self.error, self.status = None, "stopped"

    @property
    def assistant(self) -> Assistant | None:
        """The running assistant (``None`` when not listening); live-meeting routes go through it."""
        return self._assistant

    def track(self, task: asyncio.Task[Any]) -> None:
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # ------------------------------------------------------------------- state

    def in_meeting(self) -> bool:
        cur = self._assistant.current if self._assistant is not None else None
        return cur is not None and not cur.finished

    def state(self) -> dict[str, Any]:
        """``GET /api/app`` and every ``app.state`` event."""
        cur = self._assistant.current if self._assistant is not None else None
        meeting = None
        if cur is not None and not cur.finished:
            m = cur.meeting
            meeting = {"id": m.id, "title": m.title, "started_at": m.started_at.isoformat(), "folder": cur.store.dir.name}
        return {
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
            "warnings": list(self.warnings),
            "prefs": {
                "listen_on_launch": self.prefs.listen_on_launch,
                "start_at_login": self.prefs.start_at_login,
                "ui_language": self.prefs.ui_language,
            },
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

    def _on_log_warning(self, message: str) -> None:
        self.add_warning(message)
        self.bus.publish("app.warning", message=message)

    def _on_event(self, ev: Event) -> None:
        if ev.type == "source.error":
            msg = human_error(str(ev.data.get("error") or ""))
            self._last_source_error = msg
            self.add_warning(msg)
        elif ev.type == "source.stopped" and ev.data.get("source") == "mic" and self.status in ("listening", "paused", "starting"):
            # your microphone gave up for good (missing device, package or permission): not really listening
            self._set_status("error", human_error(str(ev.data.get("error") or "the microphone stopped")))
        elif ev.type in ("meeting.started", "assistant.paused", "assistant.resumed"):
            self._publish_state()
        elif ev.type == "meeting.ended":
            self._publish_state()
            if self.restart_pending:
                self.track(asyncio.ensure_future(self._apply_pending()))

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

    async def listen_start(self, paused: bool = False) -> None:
        """Start listening (idempotent); ``paused`` starts it paused (a restart while paused). A failure
        becomes state ``error`` with a plain message."""
        if self._run_task is not None and self._stop_requested:
            await asyncio.wait({self._run_task})  # a stop (or restart) still winding down
        if self._assistant is not None or self._closing:
            return
        try:
            problem = self._preflight()
        except Exception as exc:  # noqa: BLE001 - e.g. a hand-edited path that can't be expanded: shown, not raised
            log.warning("could not start listening: %s", exc)
            problem = human_error(exc)
        if problem:
            self._set_status("error", problem)
            return
        self._set_status("starting")
        try:
            assistant = self._factory(self)
        except Exception as exc:  # noqa: BLE001 - shown to the person
            log.warning("could not start listening: %s", exc)
            self._set_status("error", human_error(exc))
            return
        self._assistant = assistant
        self._stop_requested = False
        self._last_source_error = None
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
        way (e.g. "apply now") then stays stopped."""
        self._stop_pressed = True
        await self._stop(force, timeout)

    async def _stop(self, force: bool = False, timeout: float | None = None) -> None:
        task, a = self._run_task, self._assistant
        if task is None or task.done() or a is None:
            if self.status == "error":  # Stop dismisses the error
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
        else:
            await asyncio.wait({task}, timeout=timeout)

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

    async def _apply_pending(self) -> None:
        await asyncio.sleep(0)  # after the rest of the meeting.ended listeners
        if not self.restart_pending or self._stop_requested or self.in_meeting() or self._assistant is None:
            return
        log.info("applying the new settings now that the meeting has ended")
        await self.listen_restart()

    async def apply_settings(self, wait: bool = True) -> dict[str, bool]:
        """After a settings or key change: restart now when no meeting is in progress, else when it ends.
        Returns ``{restarting, restart_pending}``; ``wait=False`` restarts in the background."""
        self.reload_config()
        if self._assistant is None or self._stop_requested or self._closing:
            # not listening — or you pressed Stop and it is wrapping up: the next start reads the new
            # settings anyway, and restarting here would turn the microphone back on after your Stop
            if self.status == "error" and not self.config_error and not self._closing:
                # the fix for the failed start may be this very change: try again
                coro = self.listen_start()
            else:
                self._publish_state()
                return {"restarting": False, "restart_pending": False}
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
        """Validate and save preference changes; launch-at-login goes through the native shell."""
        if "start_at_login" in changes and changes["start_at_login"] is not None:
            hook = getattr(self.native_hooks, "set_start_at_login", None)
            if hook is not None:
                try:
                    hook(bool(changes["start_at_login"]))
                except Exception as exc:  # noqa: BLE001 - e.g. needs approval in System Settings
                    raise HostError(f"could not change launch at login: {exc}") from None
        self.prefs = update_prefs(changes, self.prefs_path)
        self._publish_state()
        return self.prefs

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
        in_use = wc.model if self.cfg.transcription.backend == "whisper-cpp" else None
        return {"models": rows, "in_use": in_use, "selected": wc.model, "models_dir": str(stt_models.models_dir(self.cfg))}

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
        try:
            await stt_models.download(self.cfg, name, on_progress=progress)
        except asyncio.CancelledError:
            publish("cancelled")  # the .part file stays: the next download resumes it
        except stt_models.DownloadError as exc:
            log.warning("download of %s failed: %s", name, exc)
            publish("error", error=str(exc))
        except Exception as exc:  # noqa: BLE001 - disk full, permissions…
            log.warning("download of %s failed: %s", name, exc)
            publish("error", error=f"{type(exc).__name__}: {exc}")
        else:
            state["done"] = state["total"] = state["total"] or state["done"]
            publish("done")
        finally:
            self._downloads.pop(name, None)

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
        tc = self.cfg.transcription
        if self._assistant is not None and tc.backend == "whisper-cpp" and name in (tc.whisper_cpp.model, stt_models.VAD_MODEL):
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

    async def start(self) -> str:
        """Start the dashboard server (and listening, if set up to); returns the app page's URL with the token."""
        from ..dashboard.server import serve

        self.loop = asyncio.get_running_loop()
        self._closed = asyncio.Event()
        self._install_logging()
        log.info("voicestream %s app starting (config %s)", __version__, self.config_path)
        base, self._server_task = await serve(self, DashboardConfig(host="127.0.0.1", port=self.port or 0), token=self.token)
        self.url = base.replace("/?token=", "/app?token=", 1)
        if self.prefs.setup_done and self.prefs.listen_on_launch and not self.config_error:
            await self.listen_start()
        else:
            self._publish_state()
        return self.url

    async def shutdown(self, force: bool = False) -> None:
        """Stop listening (a graceful wrap-up of up to :data:`GRACEFUL_SHUTDOWN_S`, then forced; at once
        forced with ``force``), downloads and the server. Safe to call again to escalate."""
        self._closing = True  # a restart in flight (settings, pending after a meeting) must not start a new one
        if self._run_task is not None:
            if not force:
                await self.listen_stop(timeout=GRACEFUL_SHUTDOWN_S)
            if self._run_task is not None:
                await self.listen_stop(force=True)
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

    def run_in_thread(self, timeout: float = 60.0) -> str:
        """Run the host's loop in a background thread, start it, and return the URL once the server
        answers. Raises what :meth:`start` raised, or ``TimeoutError``."""
        ready = threading.Event()
        box: dict[str, Any] = {}

        async def main() -> None:
            try:
                box["url"] = await self.start()
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
    from ..session import Assistant

    return Assistant(host.cfg, bus=host.bus, costs=host.costs)
