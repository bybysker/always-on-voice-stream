"""Deepgram streaming, local Whisper, screen watcher and device discovery (all offline)."""

from __future__ import annotations

import asyncio
import json
import sys
import types
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
import websockets

from helpers import bursts
from voicestream.audio.base import float_to_int16
from voicestream.config import Config, DeepgramConfig
from voicestream.models import SAMPLE_RATE, AudioFrame, Utterance


async def _frames(sig: np.ndarray, name: str = "mic"):
    for k in range(0, len(sig), 1600):
        yield AudioFrame(name, sig[k : k + 1600], k / SAMPLE_RATE)
        await asyncio.sleep(0)


# ------------------------------------------------------------------ deepgram


class _DeepgramMock:
    """Answers each Finalize with a final result covering the audio since the last one."""

    def __init__(self, drop_first_after_audio: bool = False) -> None:
        self.paths: list[str] = []
        self.messages: list[str] = []
        self.connections = 0
        self.drop_first = drop_first_after_audio

    async def handler(self, ws: Any) -> None:
        self.connections += 1
        self.paths.append(ws.request.path)
        assert ws.request.headers["Authorization"] == "Token k"
        received = last = 0.0
        async for raw in ws:
            if isinstance(raw, bytes):
                received += len(raw) / 2 / SAMPLE_RATE
                if self.drop_first and self.connections == 1 and received > 1.0:
                    await ws.close()
                    return
                continue
            msg = json.loads(raw)
            self.messages.append(msg["type"])
            if msg["type"] == "Finalize":
                await ws.send(json.dumps({"type": "Results", "start": last, "duration": received - last, "is_final": False,
                                          "channel": {"alternatives": [{"transcript": "interim"}]}}))
                await ws.send(json.dumps({"type": "Results", "start": last, "duration": received - last, "is_final": True,
                                          "from_finalize": True,
                                          "channel": {"alternatives": [{"transcript": f"conn {self.connections} {received:.1f}", "confidence": 0.8}]}}))
                last = received
            elif msg["type"] == "CloseStream":
                await ws.close()
                return


async def _run_deepgram(mock: _DeepgramMock, n: int = 3, **kw: Any):
    from voicestream.transcribe.deepgram import DeepgramTranscriber

    async with websockets.serve(mock.handler, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        partials: list[str] = []
        t = DeepgramTranscriber(
            DeepgramConfig(url=f"ws://127.0.0.1:{port}/v1/listen"), api_key="k",
            on_partial=lambda s, sp, p: partials.append(p), **kw,
        )
        segs = [s async for s in t.stream("mic", "Me", _frames(float_to_int16(bursts(n))))]
        return t, segs, partials


async def test_deepgram_one_segment_per_utterance_and_only_speech_billed() -> None:
    mock = _DeepgramMock()
    t, segs, partials = await _run_deepgram(mock, keywords=["Notion"])
    assert len(segs) == 3
    assert [s.t_start for s in segs] == sorted(s.t_start for s in segs)
    assert all(s.speaker == "Me" and s.confidence == pytest.approx(0.8) for s in segs)
    assert partials and partials[0] == "interim"
    total = len(bursts(3)) / SAMPLE_RATE
    assert 0 < t.billed_seconds < total * 0.9  # silence between bursts is never sent
    assert "keyterm=Notion" in mock.paths[0] and "encoding=linear16" in mock.paths[0]
    assert mock.messages.count("Finalize") == 3 and mock.messages[-1] == "CloseStream"


async def test_deepgram_reconnects_and_resends_lost_utterance() -> None:
    mock = _DeepgramMock(drop_first_after_audio=True)
    t, segs, _ = await _run_deepgram(mock, n=2)
    assert mock.connections >= 2
    assert len(segs) == 2  # nothing lost, nothing duplicated
    assert segs[0].text.startswith("conn 2")


def test_deepgram_url_older_model_uses_keywords() -> None:
    from voicestream.transcribe.deepgram import DeepgramTranscriber

    t = DeepgramTranscriber(DeepgramConfig(model="nova-2", diarize=True), api_key="k", language="en", keywords=["Acme"])
    url = t.url()
    assert "keywords=Acme" in url and "keyterm" not in url and "diarize_model=latest" in url and "language=en" in url


def test_deepgram_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from voicestream.transcribe.base import TranscriptionError
    from voicestream.transcribe.deepgram import DeepgramTranscriber

    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    with pytest.raises(TranscriptionError):
        DeepgramTranscriber()


# -------------------------------------------------------------- local whisper


class _FakeWhisper:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def transcribe(self, audio: np.ndarray, **kw: Any):
        self.calls.append({"n": len(audio), "dtype": audio.dtype, **kw})
        segs = [
            SimpleNamespace(text=" Hello there.", avg_logprob=-0.2, no_speech_prob=0.1),
            SimpleNamespace(text=" (noise)", avg_logprob=-1.5, no_speech_prob=0.95),
        ]
        return iter(segs), SimpleNamespace(language="en")


async def test_local_whisper_filters_no_speech_and_passes_options() -> None:
    from voicestream.transcribe.local_whisper import LocalWhisperTranscriber

    model = _FakeWhisper()
    t = LocalWhisperTranscriber(language="en", prompt="Acme, Sam", model=model)
    utt = Utterance("mic", np.zeros(16000, np.int16), 0.0, 1.0, 1.0)
    res = await t.transcribe_utterance(utt, t.build_prompt("earlier words"))
    assert res.text == "Hello there." and res.language == "en"
    assert 0.7 < (res.confidence or 0) < 0.9
    call = model.calls[0]
    assert call["dtype"] == np.float32 and call["language"] == "en" and call["vad_filter"] is False
    assert "Acme, Sam" in call["initial_prompt"] and "earlier words" in call["initial_prompt"]


def test_local_whisper_missing_package_message(monkeypatch: pytest.MonkeyPatch) -> None:
    from voicestream.config import LocalWhisperConfig
    from voicestream.transcribe import local_whisper
    from voicestream.transcribe.base import TranscriptionError

    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    local_whisper._MODELS.clear()
    with pytest.raises(TranscriptionError, match="voicestream\\[local\\]"):
        local_whisper._load(LocalWhisperConfig(model="tiny"))


# ------------------------------------------------------------------- screen


async def test_screen_watcher_describes_only_meaningful_changes(cfg: Config) -> None:
    from PIL import Image

    from voicestream.brain.llm import ScriptedLLM
    from voicestream.screen.capture import ScreenWatcher

    frames = [
        Image.new("RGB", (1920, 1080), "white"),
        Image.new("RGB", (1920, 1080), "white"),  # unchanged → no call
        Image.new("RGB", (1920, 1080), "black"),  # changed → described
        Image.new("RGB", (1920, 1080), "white"),  # changed but the model says nothing relevant
    ]
    llm = ScriptedLLM(["Slide: Q4 pricing — Acme renewal due Oct 31", "Slide 2: roadmap", "nothing relevant"])
    cfg.screen.save_images = True
    w = ScreenWatcher(cfg, llm, grabber=lambda mon: frames.pop(0))
    notes = [await w.step() for _ in range(4)]
    assert notes[0] is not None and "Acme renewal" in notes[0].description
    assert notes[1] is None
    assert notes[2] is not None and notes[2].description == "Slide 2: roadmap"
    assert notes[3] is None
    assert w.captures == 4 and w.described == 3
    sent = llm.calls[0]["content"]
    assert sent[0]["type"] == "image" and sent[0]["source"]["media_type"] == "image/jpeg"
    assert notes[0].image_path and notes[0].image_path.endswith(".jpg")


def test_screen_watcher_needs_llm(cfg: Config) -> None:
    from voicestream.screen.capture import ScreenWatcher

    with pytest.raises(RuntimeError):
        ScreenWatcher(cfg, None)


# ------------------------------------------------------------ device discovery


def _fake_sounddevice(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    sd = types.ModuleType("sounddevice")
    devices = [
        {"name": "MacBook Pro Microphone", "max_input_channels": 1, "max_output_channels": 0, "default_samplerate": 48000.0},
        {"name": "MacBook Pro Speakers", "max_input_channels": 0, "max_output_channels": 2, "default_samplerate": 48000.0},
        {"name": "BlackHole 2ch", "max_input_channels": 2, "max_output_channels": 2, "default_samplerate": 48000.0},
    ]

    def query_devices(device: Any = None, kind: str | None = None):
        if device is None and kind is None:
            return devices
        return devices[0 if device is None else device]

    sd.query_devices = query_devices  # type: ignore[attr-defined]
    sd.default = SimpleNamespace(device=(0, 1))  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    return sd


def test_device_resolution_and_loopback_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    from voicestream.audio import CaptureSetupError
    from voicestream.audio import mic

    _fake_sounddevice(monkeypatch)
    assert mic.resolve_device(None) is None
    assert mic.resolve_device(2) == 2 and mic.resolve_device("2") == 2
    assert mic.resolve_device("blackhole") == 2
    assert mic.resolve_device("MacBook Pro Microphone") == 0
    with pytest.raises(CaptureSetupError, match="BlackHole 2ch"):
        mic.resolve_device("speakers")  # output-only device is not an input
    assert mic.find_loopback_device() == "BlackHole 2ch"
    text = mic.describe_devices()
    assert "BlackHole 2ch" in text and "loopback" in text


def test_build_source_specs(cfg: Config, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from voicestream.audio import CaptureSetupError, Clock, build_source
    from voicestream.audio.ffmpeg import FFmpegSource
    from voicestream.audio.file import WavFileSource
    from voicestream.audio.mic import MicSource
    from helpers import write_wav

    _fake_sounddevice(monkeypatch)
    clock = Clock()
    wav = write_wav(tmp_path / "x.wav", bursts(1))
    src = build_source(f"file:{wav}@system", cfg, clock)
    assert isinstance(src, WavFileSource) and src.name == "system" and not src.live
    ff = build_source("ffmpeg:-f lavfi -i sine@system", cfg, clock)
    assert isinstance(ff, FFmpegSource) and ff.input_args == ["-f", "lavfi", "-i", "sine"]
    assert isinstance(build_source("mic", cfg, clock), MicSource)
    cfg.capture.system.method = "device"
    sys_src = build_source("system", cfg, clock)
    assert isinstance(sys_src, MicSource) and sys_src.device == "BlackHole 2ch"
    cfg.capture.system.method = "ffmpeg"
    cfg.capture.system.ffmpeg_input = "-f pulse -i x.monitor"
    assert build_source("system", cfg, clock).input_args[-1] == "x.monitor"  # type: ignore[attr-defined]
    with pytest.raises(CaptureSetupError):
        build_source("bluetooth", cfg, clock)


async def test_ffmpeg_source_decodes_lavfi_tone(tmp_path) -> None:
    import shutil

    from voicestream.audio.ffmpeg import FFmpegSource

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    src = FFmpegSource("system", "-f lavfi -i sine=frequency=440:duration=1.5:sample_rate=44100")
    frames = [f async for f in src.frames()]
    total = sum(f.samples.size for f in frames)
    assert abs(total - 1.5 * SAMPLE_RATE) < SAMPLE_RATE * 0.05
    assert frames[1].t_start > frames[0].t_start
    assert np.abs(np.concatenate([f.samples for f in frames])).max() > 1000


def test_loopback_selects_endpoint_by_id(monkeypatch: pytest.MonkeyPatch) -> None:
    from voicestream.audio import loopback

    calls: list[tuple[str, bool]] = []
    spk = types.SimpleNamespace(id="alsa_output.pci.analog-stereo", name="Built-in Audio Analog Stereo")
    sc = types.SimpleNamespace(
        default_speaker=lambda: spk,
        get_speaker=lambda name: spk,
        get_microphone=lambda id, include_loopback=False: calls.append((id, include_loopback)) or types.SimpleNamespace(name=id),
    )
    monkeypatch.setattr(loopback.sys, "platform", "linux")
    loopback.loopback_microphone(sc)
    loopback.loopback_microphone(sc, "Built-in")
    monkeypatch.setattr(loopback.sys, "platform", "win32")
    loopback.loopback_microphone(sc)
    assert calls == [
        ("alsa_output.pci.analog-stereo.monitor", True),  # never by description: the mic may share it
        ("alsa_output.pci.analog-stereo.monitor", True),
        ("alsa_output.pci.analog-stereo", True),
    ]


def test_default_system_ffmpeg_input(monkeypatch: pytest.MonkeyPatch) -> None:
    from voicestream.audio import mic

    monkeypatch.setattr(mic.sys, "platform", "linux")
    monkeypatch.setattr(mic.shutil, "which", lambda name: None)
    assert mic.default_system_ffmpeg_input().endswith("-i @DEFAULT_MONITOR@")
    monkeypatch.setattr(mic.sys, "platform", "darwin")
    assert mic.default_system_ffmpeg_input() is None


def test_mactap_converts_buffers() -> None:
    from voicestream.audio.mactap import TapSource

    src = TapSource()
    got: list[tuple[np.ndarray, int]] = []
    src.push_from_thread = lambda data, rate: got.append((data, rate))  # type: ignore[method-assign]
    stereo = np.array([[0.1, 0.3], [0.5, 0.7]], dtype=np.float32)
    fmt = types.SimpleNamespace(sample_rate=48000.0, num_channels=2, is_float=True)
    src._on_buffer(types.SimpleNamespace(format=fmt, data=stereo.tobytes()))
    assert got[0][1] == 48000 and got[0][0].shape == (2, 2)


async def test_realtime_missing_completed_falls_back_to_deltas() -> None:
    """gpt-live-transcribe sometimes never sends ``.completed``: later results must not wait for it."""
    from voicestream.config import OpenAIRealtimeConfig
    from voicestream.transcribe.openai_realtime import OpenAIRealtimeTranscriber

    async def handler(ws: Any) -> None:
        n = 0
        async for raw in ws:
            ev = json.loads(raw)
            if ev["type"] != "input_audio_buffer.commit":
                continue
            n += 1
            iid = f"item_{n}"
            await ws.send(json.dumps({"type": "input_audio_buffer.committed", "item_id": iid}))
            await ws.send(json.dumps({"type": "conversation.item.input_audio_transcription.delta", "item_id": iid, "delta": f"words {n}"}))
            if n != 1:  # the first utterance never gets its final event
                await ws.send(json.dumps({"type": "conversation.item.input_audio_transcription.completed", "item_id": iid, "transcript": f"final {n}"}))

    async with websockets.serve(handler, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        t = OpenAIRealtimeTranscriber(
            OpenAIRealtimeConfig(url=f"ws://127.0.0.1:{port}/v1/realtime?intent=transcription"),
            api_key="k", completion_timeout_s=0.5,
        )
        segs = [s async for s in t.stream("system", "Others", _frames(float_to_int16(bursts(3)), "system"))]
    assert [s.text for s in segs] == ["words 1", "final 2", "final 3"]
