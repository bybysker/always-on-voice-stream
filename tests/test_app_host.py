"""The app host (voicestream/app/host.py) and the assistant's pause / forced stop (session.py)."""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import sys
import threading
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest
from test_session import FakeEngine, IdleSource
from test_whisper_cpp import _catalog, _model_bytes

from helpers import seg
from voicestream import session as session_mod
from voicestream import stt_models
from voicestream.app import host as host_mod
from voicestream.app import logs
from voicestream.app import secrets as keys_mod
from voicestream.app.host import AppHost, HostError, human_error
from voicestream.app.prefs import load_prefs
from voicestream.app.settings import save_changes
from voicestream.audio import CaptureSetupError
from voicestream.audio import mic as mic_mod
from voicestream.audio.base import AudioSource
from voicestream.config import Config
from voicestream.models import AgentStatus, AudioFrame, Event, TranscriptSegment
from voicestream.session import Assistant
from voicestream.sinks.markdown import MarkdownConfig, MarkdownSink
from voicestream.storage import list_sessions, load_meeting
from voicestream.transcribe.base import FakeTranscriber, Transcriber, TranscriptionError

CONFIG = """\
user_name = "Alex"
data_dir = "{data}"

[transcription]
backend = "fake"

[brain]
provider = "heuristic"

[agents]
mode = "off"

[always_on]
idle_split_minutes = {idle}
"""


class Loud(AudioSource):
    """A live source that always hears something (a steady tone), quickly."""

    def __init__(self, name: str = "mic", step_s: float = 0.02, frame_s: float = 0.05) -> None:
        super().__init__(name)
        self.step_s, self.frame_s = step_s, frame_s

    async def frames(self) -> AsyncIterator[AudioFrame]:
        n = int(16000 * self.frame_s)
        while not self.stopped:
            yield AudioFrame(self.name, np.full(n, 3000, dtype=np.int16), self.clock.now())
            await asyncio.sleep(self.step_s)


class Spy(Transcriber):
    """Records the loudest sample of every frame it is given; transcribes nothing."""

    def __init__(self) -> None:
        super().__init__("spy")
        self.peaks: list[int] = []

    async def stream(self, source: str, speaker: str, frames: AsyncIterator[AudioFrame]) -> AsyncIterator[TranscriptSegment]:
        async for frame in frames:
            self.peaks.append(int(np.abs(frame.samples).max()))
        return
        yield  # pragma: no cover - an async generator


def _write_config(tmp_path: Path, idle: float = 10, extra: str = "") -> Path:
    path = tmp_path / "voicestream.toml"
    path.write_text(CONFIG.format(data=(tmp_path / "data").as_posix(), idle=idle) + extra, encoding="utf-8")
    return path


def _factory(sources: Callable[[], list[AudioSource]] | None = None, built: list[Assistant] | None = None,
             **kw: Any) -> Callable[[AppHost], Assistant]:
    def make(host: AppHost) -> Assistant:
        a = Assistant(
            host.cfg,
            sources=sources() if sources else [IdleSource("mic")],
            transcriber_factory=lambda s: FakeTranscriber([], vad=host.cfg.vad, on_usage=host.costs.add_stt),
            llm=None,
            sinks=[MarkdownSink(MarkdownConfig(), host.cfg)],
            bus=host.bus,
            costs=host.costs,
            **kw,
        )
        a.idle_poll_s = 0.05
        if built is not None:
            built.append(a)
        return a

    return make


def _prefs(tmp_path: Path, mode: str | None, **prefs: Any) -> Path:
    """The app's preferences file for a test: meetings start ``mode`` (``None``: the default, manual).
    Written once: a second host in the same test (the app opened again) reads what the first one saved."""
    path = tmp_path / "app.json"
    if not path.exists() and (mode is not None or prefs):
        path.write_text(json.dumps({**({"meeting_start": mode} if mode else {}), **prefs}), encoding="utf-8")
    return path


def _host(tmp_path: Path, idle: float = 10, factory: Callable[[AppHost], Assistant] | None = None,
          mode: str | None = "automatic", **kw: Any) -> AppHost:
    """A host on fake capture. Meetings start automatically unless ``mode`` says otherwise: most tests
    here were written for always-on listening, which stays as it was; manual mode has its own tests."""
    return AppHost(_write_config(tmp_path, idle), process_setup=False, prefs_path=_prefs(tmp_path, mode),
                   assistant_factory=factory or _factory(), **kw)


async def _until(pred: Callable[[], bool], timeout: float = 10.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not pred():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


def _states(host: AppHost) -> list[str]:
    return [e.data["state"] for e in host.bus.history if e.type == "app.state"]


# --------------------------------------------------------------------- lifecycle


async def test_start_pause_resume_stop(tmp_path: Path) -> None:
    host = _host(tmp_path)
    seen: list[dict[str, Any]] = []
    host.add_state_listener(seen.append)
    assert host.state()["state"] == "stopped" and host.assistant is None
    await host.listen_start()
    assert host.status == "listening" and host.assistant is not None
    first = host.assistant
    await host.listen_start()  # idempotent
    assert host.assistant is first
    await host.listen_pause()
    assert host.status == "paused" and first.paused and first.state()["listening"] == "paused"
    assert any(e.type == "assistant.paused" for e in host.bus.history)
    await host.listen_resume()
    assert host.status == "listening" and not first.paused and first.state()["listening"] == "listening"
    await host.listen_stop()
    assert host.status == "stopped" and host.assistant is None
    states = _states(host)
    changes = [s for i, s in enumerate(states) if i == 0 or s != states[i - 1]]
    assert changes == ["starting", "listening", "paused", "listening", "stopping", "stopped"]
    assert [s["state"] for s in seen] == _states(host)  # the native shell hears every change
    state = host.state()
    for key in ("version", "platform", "config_path", "data_dir", "setup_done", "state", "in_meeting", "meeting", "error",
                "restart_pending", "warnings", "prefs", "capabilities"):
        assert key in state
    assert state["capabilities"]["keychain"] is False  # tests never use a real Keychain
    # numbered, so the page can tell an older snapshot (a slow reply) from the one it shows
    later = host.state()
    assert later["rev"] > state["rev"] > seen[-1]["rev"] > seen[0]["rev"] and later["boot"] == state["boot"]
    assert _host(tmp_path).state()["boot"] != state["boot"]  # a new launch of the app counts afresh


async def test_pause_turns_capture_into_digital_silence(cfg: Config) -> None:
    spy = Spy()
    a = Assistant(cfg, sources=[Loud("mic")], transcriber_factory=lambda s: spy, llm=None, sinks=[], always_on=True)
    run = asyncio.ensure_future(a.run())
    try:
        await _until(lambda: len(spy.peaks) > 3)
        assert all(p == 3000 for p in spy.peaks)
        assert a.pause() and not a.pause()
        mark = len(spy.peaks)
        await _until(lambda: len(spy.peaks) > mark + 5)
        assert set(spy.peaks[mark + 1 :]) == {0}  # the same frames, only silent: the clock runs on
        a.resume()
        mark = len(spy.peaks)
        await _until(lambda: len(spy.peaks) > mark + 3)
        assert spy.peaks[-1] == 3000
        types = [e.type for e in a.bus.history]
        assert types.count("assistant.paused") == 1 and types.count("assistant.resumed") == 1
    finally:
        a.stop()
        await asyncio.wait_for(run, 30)


async def test_a_long_pause_is_not_a_missing_permission(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_mod, "SILENT_SYSTEM_AFTER_S", 3.0)
    spy = Spy()
    # 0.5 s frames delivered fast: many seconds of (paused) audio in a moment
    a = Assistant(cfg, sources=[Loud("mic", step_s=0.001, frame_s=0.5), Loud("system", step_s=0.001, frame_s=0.5)],
                  transcriber_factory=lambda s: spy, llm=None, sinks=[], always_on=True)
    a.pause()
    run = asyncio.ensure_future(a.run())
    try:
        await _until(lambda: len(spy.peaks) > 80)  # > 20 s of each source, all silenced
        assert set(spy.peaks) == {0}
        for t in range(0, 12, 2):  # lines still arriving from before the pause
            await a._route(seg("I was saying something", "mic", t=float(t), dur=2.0))
        assert not [e for e in a.bus.history if e.type == "source.error"]  # neither the mic nor the S5 warning
        assert not a.echo_unprotected
    finally:
        a.stop()
        await asyncio.wait_for(run, 30)


async def test_settings_change_restarts_at_once_between_meetings(tmp_path: Path) -> None:
    built: list[Assistant] = []
    host = _host(tmp_path, factory=_factory(built=built))
    await host.listen_start()
    save_changes(host.config_path, {"user_name": "Alex Martin"})
    result = await host.apply_settings()
    assert result == {"restarting": True, "restart_pending": False}
    assert len(built) == 2 and host.assistant is built[1] and host.status == "listening"
    assert host.assistant.cfg.user_name == "Alex Martin" and host.cfg.user_name == "Alex Martin"
    await host.listen_stop()


async def test_settings_change_waits_for_the_meeting_to_end(tmp_path: Path) -> None:
    built: list[Assistant] = []
    host = _host(tmp_path, idle=0, factory=_factory(built=built))  # idle 0: one meeting from the start
    await host.listen_start()
    await _until(lambda: host.in_meeting())
    assert host.state()["in_meeting"] and host.state()["meeting"]["id"] == built[0].current.meeting.id
    await host.listen_pause()
    save_changes(host.config_path, {"user_name": "Sam"})
    assert await host.apply_settings() == {"restarting": False, "restart_pending": True}
    assert host.state()["restart_pending"] and host.assistant is built[0]
    await built[0].end_meeting("manual")  # the meeting ends: the new settings apply by themselves
    await _until(lambda: len(built) == 2 and host.status in ("listening", "paused"))
    assert not host.restart_pending and host.assistant.cfg.user_name == "Sam"
    assert host.status == "paused" and host.assistant.paused  # a restart keeps the pause
    await host.listen_stop()


async def test_apply_now_ends_the_meeting(tmp_path: Path) -> None:
    built: list[Assistant] = []
    host = _host(tmp_path, idle=0, factory=_factory(built=built))
    await host.listen_start()
    await _until(lambda: host.in_meeting())
    built[0].current.add_segment(seg("I'll send the deck by Friday.", "mic", t=1.0))
    host.restart_pending = True
    await host.listen_restart()
    assert len(built) == 2 and host.assistant is built[1] and not host.restart_pending
    assert any(e.type == "meeting.ended" and e.data["items"] >= 0 for e in host.bus.history)
    meeting, _ = load_meeting(list_sessions(host.cfg.data_path)[-1])
    assert meeting.ended_at is not None  # wrapped up and saved
    await host.listen_stop()


async def test_stopping_during_a_pending_restart_does_not_start_again(tmp_path: Path) -> None:
    built: list[Assistant] = []
    host = _host(tmp_path, idle=0, factory=_factory(built=built))
    await host.listen_start()
    await _until(lambda: host.in_meeting())
    host.restart_pending = True
    await host.listen_stop()  # ends the meeting: meeting.ended must not restart what you just stopped
    await asyncio.sleep(0.2)
    assert host.status == "stopped" and host.assistant is None and len(built) == 1 and not host.restart_pending


async def test_forced_stop_never_waits_for_agents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_mod, "FORCE_GRACE_S", 0.2)
    engine = FakeEngine(gate=asyncio.Event())  # an agent that never finishes
    built: list[Assistant] = []
    host = _host(tmp_path, idle=0, factory=_factory(built=built, agent_engine=engine))
    save_changes(host.config_path, {"agents.mode": "commands", "agents.engine": "api"})
    host.reload_config()
    await host.listen_start()
    await _until(lambda: host.in_meeting())
    a = built[0]
    a.current.add_segment(seg("Hey Claude, compare Globex and Initech pricing for the Acme email.", "mic", t=1.0))
    await a.extract_now()
    await _until(lambda: engine.prompts != [])
    loop = asyncio.get_running_loop()
    await host.listen_stop(timeout=0.5)  # a normal stop waits for the agent (up to 3 min)…
    assert host.status == "stopping" and host.assistant is a
    started = loop.time()
    await host.listen_stop(force=True)  # …quitting can't
    assert loop.time() - started < 5 and host.status == "stopped" and host.assistant is None
    meeting, _ = load_meeting(list_sessions(host.cfg.data_path)[-1])
    assert meeting.ended_at is not None
    runs = [i.agent for i in meeting.items.values() if i.agent is not None]
    assert runs and all(r.status == AgentStatus.FAILED and r.error == "cancelled" for r in runs)


async def test_unless_forced(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_mod, "FORCE_GRACE_S", 0.1)
    a = Assistant(cfg, sources=[], llm=None, sinks=[])

    async def slow() -> str:
        await asyncio.sleep(30)
        return "late"

    assert await a.unless_forced(asyncio.sleep(0, "x")) == (True, "x")
    waiting = asyncio.ensure_future(a.unless_forced(slow()))
    await asyncio.sleep(0.05)
    a.stop(force=True)
    assert await asyncio.wait_for(waiting, 2) == (False, None)  # cut short after the grace period
    assert await a.unless_forced(slow()) == (False, None)  # not even started once forced


# ------------------------------------------------------------------ failures


async def test_failures_to_start_are_plain_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(host: AppHost) -> Assistant:
        raise TranscriptionError("DEEPGRAM_API_KEY is not set", fatal=True)

    host = _host(tmp_path, factory=broken)
    await host.listen_start()
    assert host.status == "error" and host.error == "Speech-to-text with Deepgram needs your Deepgram key: Settings → Speech-to-text"
    await host.listen_stop()  # Stop dismisses the error
    assert host.status == "stopped" and host.error is None
    # the preflight says it before anything is built
    save_changes(host.config_path, {"transcription.backend": "whisper-cpp", "transcription.whisper_cpp.binary": str(tmp_path / "ws")})
    (tmp_path / "ws").write_text("#!/bin/sh\n")
    (tmp_path / "ws").chmod(0o755)
    host.reload_config()
    monkeypatch.setattr(stt_models, "download", _failing_download)
    await host.listen_start()  # the model isn't there: it is fetched (here the download fails)…
    await _until(lambda: host.status == "error")
    assert host.error.startswith("download of large-v3-turbo-q5_0 failed") and host.awaiting_model is None
    # adding what was missing tries again by itself
    stt_models.models_dir(host.cfg).mkdir(parents=True, exist_ok=True)
    stt_models.model_path(host.cfg, "large-v3-turbo-q5_0").write_bytes(stt_models.GGML_MAGIC + b"\0" * 64)
    host._factory = _factory()
    await host.apply_settings()
    assert host.status == "listening"
    await host.listen_stop()


async def _failing_download(cfg: Config, name: str, *, on_progress: Any = None, **kw: Any) -> Path:
    raise stt_models.DownloadError("HTTP 404")


async def test_a_microphone_that_fails_for_good_is_an_error(tmp_path: Path) -> None:
    class NoMic(AudioSource):
        async def frames(self) -> AsyncIterator[AudioFrame]:
            raise CaptureSetupError("no microphone found")
            yield  # pragma: no cover

    host = _host(tmp_path, factory=_factory(sources=lambda: [NoMic("mic")]))
    await host.listen_start()
    await _until(lambda: host.status == "error")
    assert host.error == "no microphone found" and "no microphone found" in host.state()["warnings"][0]
    await _until(lambda: host.assistant is None)  # its only source gave up: the run ended
    assert host.status == "error"


def test_human_errors() -> None:
    assert human_error("microphone delivers only digital silence — check the microphone permission").startswith(
        "Microphone permission is off: System Settings → Privacy & Security → Microphone")
    assert "Screen & System Audio Recording" in human_error(
        "system audio has been pure digital silence while you talked for 2 min. … System Audio Recording …")
    assert human_error(ImportError("No module named 'catap'")).startswith("Part of Voicestream is missing")
    assert human_error("model small not downloaded: `voicestream models download small`").startswith("The speech-to-text model")
    assert human_error(RuntimeError("")) == "RuntimeError"


def test_warnings_are_deduplicated_newest_first(tmp_path: Path) -> None:
    host = _host(tmp_path)
    host.add_warning("ffmpeg exited with 1")
    host.add_warning("system: capture/transcription failed: ffmpeg exited with 1")  # the same problem, logged
    host.add_warning("Claude unavailable")
    assert host.warnings[0] == "Claude unavailable" and list(host.warnings).count("ffmpeg exited with 1") == 1
    for i in range(20):
        host.add_warning(f"problem {i}")
    assert len(host.warnings) == 10 and host.warnings[0] == "problem 19"


# -------------------------------------------------------------- model downloads


async def test_model_download_progress_is_throttled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_download(cfg: Config, name: str, *, on_progress: Any = None, **kw: Any) -> Path:
        for i in range(1, 201):
            on_progress(i * 1000, 200_000)
            if i % 20 == 0:
                await asyncio.sleep(0.03)
        path = stt_models.model_path(cfg, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(stt_models.GGML_MAGIC + b"\0" * 10)
        return path

    monkeypatch.setattr(stt_models, "download", fake_download)
    monkeypatch.setattr(host_mod, "DOWNLOAD_EVENT_S", 0.1)
    host = _host(tmp_path)
    assert host.model_download("base")
    assert {"base", "silero-v6.2.0"} <= set(host._downloads)  # the voice detector comes along
    rows = {m["name"]: m for m in host.models()["models"]}
    assert rows["base"]["downloading"] is not None and not rows["base"]["installed"]
    assert not host.model_download("base")  # already downloading
    await _until(lambda: not host._downloads)
    events = [e.data for e in host.bus.history if e.type == "model.download" and e.data["name"] == "base"]
    assert events[-1]["state"] == "done" and events[-1]["done"] == events[-1]["total"]
    assert 2 <= len(events) < 40  # 200 progress calls, a handful of events
    assert all(e["state"] == "running" for e in events[:-1])
    rows = {m["name"]: m for m in host.models()["models"]}
    assert rows["base"]["installed"] and rows["base"]["downloading"] is None
    with pytest.raises(KeyError):
        host.model_download("../../etc/passwd")


async def test_cancelling_a_download_keeps_the_partial_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = _model_bytes(3 * 1024 * 1024)
    _catalog(monkeypatch, data)
    release = asyncio.Event()

    class Stalls(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield data[: 2 * 1024 * 1024]
            await release.wait()  # the connection stalls
            yield data[2 * 1024 * 1024 :]

    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, stream=Stalls(), headers={"content-length": str(len(data))})))
    monkeypatch.setattr(stt_models, "download", functools.partial(stt_models.download, client=client))
    host = _host(tmp_path)
    save_changes(host.config_path, {"transcription.whisper_cpp.vad": False})
    host.reload_config()
    host.model_download("base")
    part = stt_models.model_path(host.cfg, "base").with_name("ggml-base.bin.part")
    await _until(lambda: part.exists() and part.stat().st_size >= 2 * 1024 * 1024)
    assert host.model_cancel("base")
    await _until(lambda: not host._downloads)
    states = [e.data["state"] for e in host.bus.history if e.type == "model.download"]
    assert states[-1] == "cancelled" and part.stat().st_size == 2 * 1024 * 1024  # kept for a resume
    assert await host.model_remove("base") and not part.exists()
    await client.aclose()


async def test_a_model_in_use_is_not_removed(tmp_path: Path) -> None:
    host = _host(tmp_path)
    save_changes(host.config_path, {"transcription.backend": "whisper-cpp"})
    host.reload_config()
    host._assistant = object()  # type: ignore[assignment]
    with pytest.raises(HostError):
        await host.model_remove("large-v3-turbo-q5_0")
    host._assistant = None


# ---------------------------------------------------------------- prefs, launch


async def test_listening_on_launch_waits_for_the_setup(tmp_path: Path) -> None:
    host = _host(tmp_path)
    url = await host.start()
    try:
        assert "/app?token=" in url and host.status == "stopped"  # not set up yet
        host.complete_setup()
        assert host.prefs.setup_done and host.state()["setup_done"]
    finally:
        await host.shutdown()
    again = _host(tmp_path)
    await again.start()
    try:
        assert again.status == "listening"
        again.update_prefs({"listen_on_launch": False})
    finally:
        await again.shutdown()
    third = _host(tmp_path)
    await third.start()
    try:
        assert third.status == "stopped"
    finally:
        await third.shutdown()


def test_launch_at_login_goes_through_the_native_hook(tmp_path: Path) -> None:
    calls: list[bool] = []

    class Hooks:
        @staticmethod
        def set_start_at_login(enabled: bool) -> None:
            calls.append(enabled)
            if enabled and len(calls) > 1:
                raise RuntimeError("needs approval in System Settings")

        @staticmethod
        def login_item_status() -> str:
            return "disabled"

    host = _host(tmp_path)
    host.native_hooks = Hooks()
    assert host.update_prefs({"start_at_login": True}).start_at_login and calls == [True]
    with pytest.raises(HostError):
        host.update_prefs({"start_at_login": True})
    assert host_mod._login_item_supported(Hooks()) and not host_mod._login_item_supported(object())


# ------------------------------------------------------------- thread and logs


def test_run_in_thread_call_and_shutdown(tmp_path: Path) -> None:
    host = _host(tmp_path)
    url = host.run_in_thread()
    threads: list[str] = []
    host.add_state_listener(lambda s: threads.append(threading.current_thread().name))
    try:
        base, token = url.split("/app?token=")
        with httpx.Client(trust_env=False, timeout=5) as http:
            assert http.get(url).status_code == 200
            assert http.get(f"{base}/api/app", headers={"x-voicestream-token": token}).json()["state"] == "stopped"
        host.call(host.listen_start()).result(10)
        assert host.status == "listening" and threads and set(threads) == {"voicestream-host"}
    finally:
        host.call(host.shutdown()).result(30)
    assert host.thread is not None
    host.thread.join(5)
    assert not host.thread.is_alive() and host.assistant is None


def test_the_mac_app_starts_the_host_and_asks_for_listening_itself(tmp_path: Path) -> None:
    """The native shell starts the host with ``auto_listen=False`` and posts :meth:`AppHost.listen_on_launch`
    once it knows no link launched the app (see test_app_meeting_start)."""
    _prefs(tmp_path, "automatic", setup_done=True, listen_on_launch=True)
    host = _host(tmp_path)
    host.run_in_thread(auto_listen=False)
    try:
        assert host.status == "stopped" and host.assistant is None
        assert host.call(host.listen_on_launch()).result(10) is True and host.status == "listening"
    finally:
        host.call(host.shutdown()).result(30)


async def test_warnings_are_mirrored_to_the_app_once_a_second(tmp_path: Path) -> None:
    host = _host(tmp_path)
    await host.start()
    try:
        logger = logging.getLogger("voicestream.sinks.base")
        logger.warning("sink notion unavailable: no token")
        logger.warning("sink notion unavailable: no token")  # within the second: not again
        logging.getLogger("other.library").warning("not ours")
        await asyncio.sleep(0)
        mirrored = [e.data["message"] for e in host.bus.history if e.type == "app.warning"]
        assert mirrored == ["sink notion unavailable: no token"]
        assert host.state()["warnings"][0] == "sink notion unavailable: no token"
        done = threading.Event()
        threading.Thread(target=lambda: (logger.error("from a capture thread"), done.set())).start()
        done.wait(5)
        await _until(lambda: "from a capture thread" in host.warnings)
    finally:
        await host.shutdown()
    assert not any(isinstance(h, logs.WarningMirror) for h in logging.getLogger("voicestream").handlers)


def test_the_log_file_is_private_and_rotates(tmp_path: Path) -> None:
    path = logs.log_path(tmp_path / "data")
    level = logging.getLogger().level
    handler = logs.install_file_log(path)
    try:
        logging.getLogger("voicestream.test").info("hello from the app")
        handler.flush()
        assert "hello from the app" in path.read_text(encoding="utf-8")
        assert handler.maxBytes == 5 * 1024 * 1024 and handler.backupCount == 3  # type: ignore[attr-defined]
        if os.name == "posix":
            assert (path.parent.stat().st_mode & 0o777) == 0o700
    finally:
        logging.getLogger().removeHandler(handler)
        logging.getLogger().setLevel(level)
        handler.close()


def test_process_setup_prepends_homebrew_to_a_finder_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin", "/bin"]))
    monkeypatch.setattr(host_mod, "EXTRA_PATH", (str(tmp_path), str(tmp_path / "does-not-exist")))
    host_mod._prepend_path()
    assert os.environ["PATH"].split(os.pathsep) == [str(tmp_path), "/usr/bin", "/bin"]  # only folders that exist
    host_mod._prepend_path()
    assert os.environ["PATH"].split(os.pathsep) == [str(tmp_path), "/usr/bin", "/bin"]  # once


def test_events_listener_types(tmp_path: Path) -> None:
    host = _host(tmp_path)
    host._on_event(Event(type="source.error", data={"source": "mic", "error": "microphone delivers only digital silence"}))
    assert host.warnings[0].startswith("Microphone permission is off")


# ----------------------------------------------------- what can't run (yet) never stops listening


def _gated_download(gate: asyncio.Event, calls: list[str]) -> Callable[..., Any]:
    async def download(cfg: Config, name: str, *, on_progress: Any = None, **kw: Any) -> Path:
        calls.append(name)
        await gate.wait()
        path = stt_models.model_path(cfg, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(stt_models.GGML_MAGIC + b"\0" * 64)
        return path

    return download


def _on_this_mac(host: AppHost, tmp_path: Path, model: str, installed: tuple[str, ...] = ()) -> None:
    """Speech-to-text on this computer with ``model`` (the fake whisper-server passes the preflight)."""
    ws = tmp_path / "ws"
    ws.write_text("#!/bin/sh\n")
    ws.chmod(0o755)
    save_changes(host.config_path, {"transcription.backend": "whisper-cpp", "transcription.whisper_cpp.binary": str(ws),
                                    "transcription.whisper_cpp.model": model, "transcription.whisper_cpp.vad": False})
    host.reload_config()
    for name in installed:
        path = stt_models.model_path(host.cfg, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(stt_models.GGML_MAGIC + b"\0" * 64)


async def test_a_model_still_downloading_is_switched_to_once_it_is_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gate, calls = asyncio.Event(), []
    monkeypatch.setattr(stt_models, "download", _gated_download(gate, calls))
    built: list[Assistant] = []
    host = _host(tmp_path, factory=_factory(built=built))
    _on_this_mac(host, tmp_path, "base", installed=("base",))
    await host.listen_start()
    assert host.status == "listening"
    save_changes(host.config_path, {"transcription.whisper_cpp.model": "small"})  # picked in Settings, not downloaded
    result = await host.apply_settings()
    # listening goes on with the model it has, the new one downloads
    assert not result["restarting"] and host.status == "listening" and host.assistant is built[0] and len(built) == 1
    assert host.awaiting_model == "small" and host.state()["waiting_for_model"] == "small"
    await _until(lambda: calls == ["small"])
    assert any(w.startswith(host_mod.MODEL_WAIT) for w in host.warnings)
    assert host.models()["in_use"] == "base"  # what the running assistant loaded
    with pytest.raises(HostError):
        await host.model_remove("base")
    gate.set()  # downloaded: switched to between meetings, by itself
    await _until(lambda: len(built) == 2 and host.status == "listening")
    assert host.assistant.cfg.transcription.whisper_cpp.model == "small" and host.awaiting_model is None
    assert not any(w.startswith(host_mod.MODEL_WAIT) for w in host.warnings)
    await host.listen_stop()


async def test_listening_asked_for_during_the_download_starts_once_it_is_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gate, calls = asyncio.Event(), []
    monkeypatch.setattr(stt_models, "download", _gated_download(gate, calls))
    host = _host(tmp_path)
    _on_this_mac(host, tmp_path, "base")  # e.g. the app quit during the setup's download: it resumes
    await host.listen_start()
    assert host.status == "starting" and host.assistant is None and host.awaiting_model == "base"
    await _until(lambda: calls == ["base"])
    gate.set()
    await _until(lambda: host.status == "listening")
    await host.listen_stop()
    # Stop while waiting: it stays stopped when the download is done
    stt_models.remove(host.cfg, "base")
    gate.clear()
    await host.listen_start()
    assert host.status == "starting"
    await host.listen_stop()
    assert host.status == "stopped" and host.awaiting_model is None
    gate.set()
    await _until(lambda: not host._downloads)
    await asyncio.sleep(0.1)
    assert host.status == "stopped" and host.assistant is None


async def test_new_settings_that_cant_run_keep_the_current_ones_listening(tmp_path: Path) -> None:
    built: list[Assistant] = []
    host = _host(tmp_path, factory=_factory(built=built))
    await host.listen_start()
    save_changes(host.config_path, {"transcription.backend": "deepgram"})  # no Deepgram key yet
    assert (await host.apply_settings())["restarting"] is False
    assert host.status == "listening" and host.assistant is built[0]
    assert any(w.startswith(host_mod.NOT_APPLIED) and "Deepgram key" in w for w in host.warnings)
    os.environ["DEEPGRAM_API_KEY"] = "dg-test"  # the key pasted: now it can run
    await host.apply_settings()
    assert len(built) == 2 and host.status == "listening" and not any(w.startswith(host_mod.NOT_APPLIED) for w in host.warnings)
    await host.listen_stop()


async def test_a_restart_doesnt_wait_for_an_ended_meetings_agents(tmp_path: Path) -> None:
    gate = asyncio.Event()
    engine = FakeEngine(gate=gate)  # an agent that works on long after its meeting
    built: list[Assistant] = []
    host = _host(tmp_path, idle=0, factory=_factory(built=built, agent_engine=engine))
    save_changes(host.config_path, {"agents.mode": "commands", "agents.engine": "api", "agents.finish_wait_s": 0})
    host.reload_config()
    await host.listen_start()
    await _until(lambda: host.in_meeting())
    old = built[0]
    old.current.add_segment(seg("Hey Claude, compare Globex and Initech pricing for the Acme email.", "mic", t=1.0))
    await old.extract_now()
    await _until(lambda: engine.prompts != [])
    await old.end_meeting("manual")
    ended = old.finished_meetings[0]
    assert ended.agents.busy
    save_changes(host.config_path, {"user_name": "Sam"})  # a setting saved between meetings
    loop = asyncio.get_running_loop()
    started = loop.time()
    await asyncio.wait_for(host.apply_settings(), 10)
    # listening again at once, with the new settings; the old assistant finishes its agent meanwhile
    assert loop.time() - started < 5 and len(built) == 2 and host.assistant is built[1] and host.status == "listening"
    assert host.assistant.cfg.user_name == "Sam" and host.assistants == [built[1], old]
    item_id = next(iter(ended.meeting.items))
    host.meetings.edit_item(ended.store.dir.name, item_id, title="Compare pricing")  # still held in memory there
    assert ended.meeting.items[item_id].title == "Compare pricing"
    gate.set()
    await _until(lambda: host.assistants == [built[1]])
    runs = [i.agent for i in ended.meeting.items.values() if i.agent is not None]
    assert runs and all(r.status == AgentStatus.DONE for r in runs)
    assert host.status == "listening"
    await host.listen_stop()


async def test_quitting_cuts_short_agents_left_working_by_a_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host_mod, "GRACEFUL_SHUTDOWN_S", 0.5)
    engine = FakeEngine(gate=asyncio.Event())  # never finishes
    built: list[Assistant] = []
    host = _host(tmp_path, idle=0, factory=_factory(built=built, agent_engine=engine))
    save_changes(host.config_path, {"agents.mode": "commands", "agents.engine": "api", "agents.finish_wait_s": 0})
    host.reload_config()
    await host.listen_start()
    await _until(lambda: host.in_meeting())
    built[0].current.add_segment(seg("Hey Claude, compare Globex and Initech pricing for the Acme email.", "mic", t=1.0))
    await built[0].extract_now()
    await _until(lambda: engine.prompts != [])
    await built[0].end_meeting("manual")
    await asyncio.wait_for(host.listen_restart(), 10)
    assert len(host.assistants) == 2
    await asyncio.wait_for(host.shutdown(), 30)
    assert host.assistants == [] and built[0].forced


# ------------------------------------------------------------------- the chosen microphone


class _Stream:
    """A PortAudio input stream: calls back from its own thread until stopped, or until its device is
    unplugged (``dead``: PortAudio then just stops calling back)."""

    def __init__(self, sd: _FakeSD, device: int | None, callback: Callable[..., None]) -> None:
        self.sd, self.device, self.callback = sd, device, callback
        self.name = sd.query_devices(device, "input")["name"]
        self._stop = threading.Event()

    def start(self) -> None:
        def run() -> None:
            while not self._stop.wait(0.01):
                if self.name in self.sd.names:
                    self.callback(np.full((160, 1), 0.01, dtype=np.float32), 160, None, None)

        threading.Thread(target=run, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def close(self) -> None:
        self._stop.set()


class _FakeSD:
    """python-sounddevice with hot-plugging: ``names`` is what is plugged in, ``listed`` what PortAudio
    saw when it (re)started."""

    def __init__(self, names: list[str]) -> None:
        self.names = list(names)
        self.listed = list(names)
        self.opened: list[str] = []
        self.default = type("Default", (), {"device": [0, 0]})()

    def query_devices(self, index: int | None = None, kind: str | None = None) -> Any:
        devs = [{"name": n, "max_input_channels": 1, "default_samplerate": 16000} for n in self.listed]
        if index is None and kind is None:
            return devs
        return devs[index or 0]

    def _terminate(self) -> None:
        pass

    def _initialize(self) -> None:
        self.listed = list(self.names)

    def InputStream(self, *, device: int | None, callback: Callable[..., None], **kw: Any) -> _Stream:  # noqa: N802
        stream = _Stream(self, device, callback)
        self.opened.append(stream.name)
        return stream


def _mic_host(tmp_path: Path, extra: str) -> AppHost:
    """A host with the real assistant factory (so the real microphone source) on a fake sounddevice."""
    return AppHost(_write_config(tmp_path, extra=extra), process_setup=False, prefs_path=tmp_path / "app.json")


async def test_a_chosen_microphone_that_isnt_connected_doesnt_stop_listening(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sd = _FakeSD(["MacBook Pro Microphone"])  # the AirPods are in their case
    monkeypatch.setattr(mic_mod, "_sd", lambda: sd)
    monkeypatch.setattr(host_mod, "MIC_STALL_S", 0.3)
    host = _mic_host(tmp_path, '\n[capture]\nsources = ["mic"]\n\n[capture.mic]\ndevice = "AirPods"\n')
    await host.listen_start()
    missing = "Your microphone (AirPods) isn't connected — using MacBook Pro Microphone until it is back"
    await _until(lambda: missing in host.warnings)
    assert host.status == "listening" and sd.opened == ["MacBook Pro Microphone"]  # the default input meanwhile
    sd.names.append("AirPods")  # taken out of the case
    host.audio_devices_changed()
    await _until(lambda: sd.opened[-1] == "AirPods")
    await _until(lambda: not any(w.startswith(host_mod.MIC_MISSING) for w in host.warnings))
    assert host.status == "listening"
    sd.names.remove("AirPods")  # flat battery mid-meeting: its stream just goes quiet
    await _until(lambda: missing in host.warnings)
    assert sd.opened[-1] == "MacBook Pro Microphone" and host.status == "listening"
    await host.listen_stop()
    assert host.status == "stopped"


async def test_without_the_fallback_only_meeting_audio_is_recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sd = _FakeSD(["MacBook Pro Microphone"])
    monkeypatch.setattr(mic_mod, "_sd", lambda: sd)
    monkeypatch.setattr(host_mod, "MIC_RETRY_S", 0.2)
    host = _mic_host(tmp_path, '\n[capture]\nsources = ["mic"]\n\n[capture.mic]\ndevice = "AirPods"\n'
                               "use_default_when_missing = false\n")
    await host.listen_start()
    await _until(lambda: any(w.startswith("Your microphone (AirPods) isn't connected — nothing is recorded") for w in host.warnings))
    assert host.status == "listening" and sd.opened == []
    sd.names.append("AirPods")  # found by the periodic look, no notification needed
    await _until(lambda: sd.opened == ["AirPods"])
    await _until(lambda: not any(w.startswith(host_mod.MIC_MISSING) for w in host.warnings))
    await host.listen_stop()


async def test_a_source_that_gives_up_while_another_records_is_a_warning(tmp_path: Path) -> None:
    class GoneMic(AudioSource):
        async def frames(self) -> AsyncIterator[AudioFrame]:
            raise CaptureSetupError("Microphone permission is off for Voicestream")
            yield  # pragma: no cover

    host = _host(tmp_path, factory=_factory(sources=lambda: [GoneMic("mic"), IdleSource("system")]))
    await host.listen_start()
    await _until(lambda: any(e.type == "source.stopped" for e in host.bus.history))
    await asyncio.sleep(0.1)
    # meeting audio is still recorded: listening, with the problem in the banner — not an error state
    # where Start did nothing and Dismiss ended the meeting
    assert host.status == "listening" and host.error is None and host.assistant is not None
    assert any("Microphone permission is off" in w for w in host.warnings)
    await host.listen_stop()


async def test_start_in_an_error_state_with_an_assistant_restarts_it(tmp_path: Path) -> None:
    built: list[Assistant] = []
    host = _host(tmp_path, factory=_factory(built=built))
    await host.listen_start()
    host._set_status("error", "something gave up")  # half of it still running
    await host.listen_start()
    assert len(built) == 2 and host.assistant is built[1] and host.status == "listening"
    await host.listen_stop()


# --------------------------------------------------------------- the keyboard shortcut


async def _toggled(host: AppHost, expect: str | None, *settled: str) -> None:
    """Press the shortcut: it says what it did (a stop at once, a start once it is up or failed), then
    the state gets there."""
    assert await host.listen_toggle() == expect
    if expect in ("start", "restart", "resume", "error"):
        assert host.status in settled  # said once it was known how it went, not before
    if settled:
        await _until(lambda: host.status in settled and host.toggle_action() is not None)


async def test_the_shortcut_starts_stops_resumes_and_restarts(tmp_path: Path) -> None:
    built: list[Assistant] = []
    host = _host(tmp_path, idle=0, factory=_factory(built=built))  # idle 0: a meeting from the start
    await _toggled(host, "start", "listening")
    await _until(lambda: host.in_meeting())
    built[0].current.add_segment(seg("I'll send the deck by Friday.", "mic", t=1.0))
    # listening in a meeting: a stop, with the meeting wrapped up and its notes written, as Stop does
    await _toggled(host, "stop", "stopped")
    assert host.assistant is None and any(e.type == "meeting.ended" for e in host.bus.history)
    folder = list_sessions(host.cfg.data_path)[-1]
    meeting, _ = load_meeting(folder)
    assert meeting.ended_at is not None and (folder / "notes.md").is_file()

    await _toggled(host, "start", "listening")
    await host.listen_pause()
    await _toggled(host, "resume", "listening")  # paused: Resume, not a stop
    assert not host.assistant.paused and len(built) == 2

    host._set_status("error", "something gave up")  # half of it may still run: what Start does then
    await _toggled(host, "restart", "listening")
    assert len(built) == 3 and host.assistant is built[2]
    await host.listen_stop()


async def test_the_shortcut_does_nothing_while_starting_or_quitting(tmp_path: Path) -> None:
    built: list[Assistant] = []
    host = _host(tmp_path, factory=_factory(built=built))
    host.status = "starting"
    assert host.toggle_action() is None and await host.listen_toggle() is None
    host.status = "stopping"  # a start, once the last meeting is wrapped up (test_app_meeting_start)
    assert host.toggle_action() == "start"
    host.status = "stopped"
    # two presses in a row (each its own call on the loop, as the shell posts them): the second comes
    # while the first one's start is being carried out, before it has changed the state. It is the same
    # start, and says so when it is up: "nothing" would be the shell's failure sound for a press that records
    first = asyncio.ensure_future(host.listen_toggle())
    await asyncio.sleep(0)
    assert host.status == "stopped" and host.toggle_action() == "start" and host.meeting_action("start") == "start"
    assert await host.listen_toggle() == "start"
    assert await first == "start" and len(built) == 1
    await _until(lambda: host.status == "listening" and host.toggle_action() == "stop")
    await host.listen_stop()
    # on the same turn of the loop (the shortcut and the floating button at once): one start, both say it
    assert await asyncio.gather(host.meeting_toggle(), host.meeting_start()) == ["start", "start"]
    assert host.status == "listening" and len(built) == 2
    await host.shutdown()
    assert host.toggle_action() is None and await host.listen_toggle() is None and host.status == "stopped"


async def test_the_shortcut_after_a_failed_start_tries_again(tmp_path: Path) -> None:
    def broken(host: AppHost) -> Assistant:
        raise TranscriptionError("DEEPGRAM_API_KEY is not set", fatal=True)

    host = _host(tmp_path, factory=broken)
    # two presses at once: the second joins the first, and both say it failed
    assert await asyncio.gather(host.meeting_toggle(), host.meeting_toggle()) == ["error", "error"]
    host._set_status("stopped")
    await _toggled(host, "error", "error")  # a start that failed: said so (the shell's sound must not say it worked)
    assert host.error.startswith("Speech-to-text with Deepgram needs your Deepgram key")
    assert host.toggle_action() == "restart"
    await _toggled(host, "error", "error")  # the restart fails the same way
    host._factory = _factory()  # fixed meanwhile
    await _toggled(host, "restart", "listening")
    await host.listen_stop()


async def test_a_start_that_raises_says_it_failed_and_why(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                         caplog: pytest.LogCaptureFixture) -> None:
    """A start that raises rather than going to the error state (a bug): the presses say it failed — the shell's
    failure sound, not its "recording" one — and the log says why, not asyncio once the task is collected."""
    caplog.set_level(logging.WARNING)
    host = _host(tmp_path)

    async def broken(paused: bool = False) -> None:
        await asyncio.sleep(0)
        raise RuntimeError("the start went wrong")

    monkeypatch.setattr(host, "listen_start", broken)
    assert await asyncio.gather(host.meeting_toggle(), host.meeting_start()) == ["error", "error"]
    assert "could not start: the start went wrong" in caplog.text


async def test_the_shortcut_preference_and_what_the_shell_says(tmp_path: Path) -> None:
    host = _host(tmp_path)
    seen: list[dict[str, Any]] = []
    host.add_state_listener(seen.append)
    state = host.state()
    assert state["prefs"]["hotkey"] == {"code": "KeyR", "mods": ["alt", "cmd"], "key": "R"}
    assert state["prefs"]["hotkey_sound"] is True and state["capabilities"]["hotkey"] is False  # no native shell
    assert state["hotkey"] == {"status": "pending", "error": None, "recording": False}
    host.set_hotkey_status("taken", -9878)
    host.set_hotkey_status("taken", -9878)  # the same answer again: nothing new to tell
    assert [s["hotkey"]["status"] for s in seen] == ["taken"]
    # a new shortcut: pending until the shell has registered it — also the same one, to try again
    host.update_prefs({"hotkey": {"code": "KeyR", "mods": ["alt", "cmd"]}})
    assert host.state()["hotkey"]["status"] == "pending"
    host.set_hotkey_status("ok")
    host.update_prefs({"listen_on_launch": False})  # another preference: the shortcut is left alone
    assert host.state()["hotkey"]["status"] == "ok"
    host.record_hotkey(True)
    assert host.state()["hotkey"]["recording"] is True
    host.update_prefs({"hotkey": None})  # saving one ends the recording
    assert host.state()["hotkey"] == {"status": "pending", "error": None, "recording": False}
    assert host.state()["prefs"]["hotkey"] is None and load_prefs(tmp_path / "app.json").hotkey is None
    with pytest.raises(ValueError, match="every Mac app has"):
        host.update_prefs({"hotkey": {"code": "KeyW", "mods": ["cmd"]}})

    # what the keys type on the Mac's keyboard (French AZERTY here), from the shell: in the state for the
    # page, and what a shortcut sent without its label is checked and shown by
    assert host.state()["keyboard_layout"] == {}
    seen.clear()
    host.set_keyboard_layout({"KeyQ": "a", "KeyA": "q", "KeyZ": "w", "KeyW": "z"})
    host.set_keyboard_layout({"KeyQ": "a", "KeyA": "q", "KeyZ": "w", "KeyW": "z"})  # the same: nothing new to tell
    assert [s["keyboard_layout"] for s in seen] == [{"KeyQ": "a", "KeyA": "q", "KeyZ": "w", "KeyW": "z"}]
    assert host.update_prefs({"hotkey": {"code": "KeyQ", "mods": ["ctrl", "cmd"]}}).hotkey.key == "A"
    with pytest.raises(ValueError, match="⌥⌘W is a command every Mac app has"):
        host.update_prefs({"hotkey": {"code": "KeyZ", "mods": ["alt", "cmd"]}})
    assert load_prefs(tmp_path / "app.json").hotkey.key == "A"  # what was saved: the label, read back as such


# ----------------------------------------------------------- settings edited by hand, the Keychain


async def test_hand_edits_are_read_on_start_and_stop(tmp_path: Path) -> None:
    built: list[Assistant] = []
    host = _host(tmp_path, factory=_factory(built=built))
    path = host.config_path
    await host.listen_start()
    path.write_text(path.read_text(encoding="utf-8").replace('user_name = "Alex"', 'user_name = "Camille"'), encoding="utf-8")
    await host.listen_stop()
    assert host.cfg.user_name == "Camille"
    path.write_text(path.read_text(encoding="utf-8").replace('"Camille"', '"Dominique Martin"'), encoding="utf-8")
    await host.listen_start()
    assert built[-1].cfg.user_name == "Dominique Martin"
    await host.listen_stop()
    good = path.read_text(encoding="utf-8")
    path.write_text(good + "\n[broken\n", encoding="utf-8")  # a typo while editing it…
    await host.listen_start()
    assert host.status == "error" and host.error.startswith("Your settings file has a problem")
    path.write_text(good, encoding="utf-8")  # …fixed: Start starts, rather than repeating the old problem
    await host.listen_start()
    assert host.status == "listening"
    await host.listen_stop()


class _SlowKeychain:
    """The Keychain after an update of the app: a prompt for every item, and Deny clicked for one."""

    priority = 5

    def __init__(self) -> None:
        self.asked: list[tuple[str, int]] = []

    def get_password(self, service: str, name: str) -> str | None:
        self.asked.append((name, threading.get_ident()))
        if name == "HUBSPOT_TOKEN":
            time.sleep(0.2)  # macOS's prompt, waiting for an answer
            raise PermissionError("User canceled the operation")
        return None

    def set_password(self, service: str, name: str, value: str) -> None:
        pass


async def test_the_keychain_is_never_read_on_the_loop_and_a_refusal_is_remembered(tmp_path: Path,
                                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    kc = _SlowKeychain()
    monkeypatch.setattr(keys_mod, "_keyring", lambda: kc)
    monkeypatch.setattr(keys_mod, "_UNREADABLE", set())
    host = _host(tmp_path)  # reads the keys once, at launch (before its loop runs)
    assert [n for n, _ in kc.asked].count("HUBSPOT_TOKEN") == 1
    warning = keys_mod.unreadable_message("HUBSPOT_TOKEN")
    assert warning in host.warnings and "won't ask again" in warning
    kc.asked.clear()
    loop_thread = threading.get_ident()
    for change in ({"user_name": "Sam"}, {"user_name": "Alex"}):  # every save reloads the settings
        save_changes(host.config_path, change)
        await host.apply_settings()
    assert kc.asked and all(t != loop_thread for _, t in kc.asked)  # in a worker thread: nothing froze
    assert "HUBSPOT_TOKEN" not in [n for n, _ in kc.asked]  # refused once: not asked again this launch
    keys_mod.set_secret("HUBSPOT_TOKEN", "pat-new")  # saved again from the app: readable from now on
    assert keys_mod.unreadable() == []


async def test_the_app_ends_a_meeting_a_crash_left_open_when_it_starts(tmp_path: Path) -> None:
    from test_app_meetings import crashed_meeting

    host = _host(tmp_path)
    folder = crashed_meeting(host.cfg)
    await host.start()
    try:
        meeting, _ = load_meeting(host.cfg.data_path / "sessions" / folder)
        assert meeting.ended_at is not None and meeting.interrupted and len(meeting.segments) == 5
        assert host.meetings.list()["meetings"][0]["interrupted"]
    finally:
        await host.shutdown()


async def test_a_meeting_whose_wrap_up_a_forced_stop_cut_short_can_be_finished_at_once(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Quitting while the wrap-up hangs cancels it: the meeting has no end. Its lock file must stay
    (unlocked), or it would pass for another voicestream's recording, read-only for an hour."""
    monkeypatch.setattr(host_mod, "FORCE_STOP_S", 0.3)

    async def hangs(self: Any, reason: str = "ended") -> None:
        self.finished = True
        await asyncio.Event().wait()

    built: list[Assistant] = []
    host = _host(tmp_path, idle=0, factory=_factory(built=built))
    await host.listen_start()
    await _until(lambda: host.in_meeting())
    await built[0]._route(seg("I'll send the deck by Friday.", "mic", t=1.0))
    await built[0].extract_now()
    assert built[0].current is not None
    built[0].current.flush_save()
    monkeypatch.setattr(session_mod.MeetingSession, "finish", hangs)
    await host.listen_stop(force=True)
    await _until(lambda: host.assistant is None and host._run_task is None)
    (row,) = host.meetings.list()["meetings"]
    assert not row["in_progress"] and row["interrupted"]
    assert host.meetings.edit_item(row["folder"], "A1", status="done")["status"] == "done"


async def test_audio_device_changes_reach_the_microphone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from voicestream.app import native_hooks

    blocks: list[Callable[[Any], None]] = []
    center = SimpleNamespace(addObserverForName_object_queue_usingBlock_=lambda name, obj, queue, block: blocks.append(block))
    monkeypatch.setitem(sys.modules, "Foundation", SimpleNamespace(NSNotificationCenter=SimpleNamespace(defaultCenter=lambda: center)))
    monkeypatch.setattr(native_hooks, "_avfoundation", lambda: SimpleNamespace(
        AVCaptureDeviceWasConnectedNotification="connected", AVCaptureDeviceWasDisconnectedNotification="disconnected"))
    host = _host(tmp_path)
    host.native_hooks = native_hooks
    poked: list[bool] = []
    monkeypatch.setattr(host, "audio_devices_changed", lambda: poked.append(True))
    await host.start()
    try:
        assert len(blocks) == 2  # connected, disconnected
        threading.Thread(target=blocks[0], args=(None,)).start()  # AppKit calls from its own thread
        await _until(lambda: poked == [True])
    finally:
        await host.shutdown()
    blocks[1](None)  # after quitting: the loop is gone, and nothing raises into Objective-C
