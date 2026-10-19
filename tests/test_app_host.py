"""The app host (voicestream/app/host.py) and the assistant's pause / forced stop (session.py)."""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import threading
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
from voicestream.app.host import AppHost, HostError, human_error
from voicestream.app.settings import save_changes
from voicestream.audio import CaptureSetupError
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


def _host(tmp_path: Path, idle: float = 10, factory: Callable[[AppHost], Assistant] | None = None, **kw: Any) -> AppHost:
    return AppHost(_write_config(tmp_path, idle), process_setup=False, prefs_path=tmp_path / "app.json",
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
    await host.listen_start()
    assert host.status == "error" and "isn't downloaded" in host.error
    # adding what was missing tries again by itself
    stt_models.models_dir(host.cfg).mkdir(parents=True, exist_ok=True)
    stt_models.model_path(host.cfg, "large-v3-turbo-q5_0").write_bytes(stt_models.GGML_MAGIC + b"\0" * 64)
    host._factory = _factory()
    await host.apply_settings()
    assert host.status == "listening"
    await host.listen_stop()


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
