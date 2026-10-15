"""Audio front end: DSP helpers, the VAD (what decides which audio is paid for) and file replay.

Timing assertions use the known layout of ``helpers.bursts``: 1 s of noise, then
bursts of 2.5 s speech + 1.5 s gap, so burst ``k`` is spoken from ``1 + 4k`` to ``3.5 + 4k``.
"""

from __future__ import annotations

import asyncio
import io
import shutil
import subprocess
import time
import types
import wave
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest

from helpers import bursts, noise, speechlike, write_wav
from voicestream.audio import build_source
from voicestream.audio.base import (
    Clock,
    QueueAudioSource,
    StreamResampler,
    float_to_int16,
    resample,
    rms_dbfs,
    to_mono_float,
    wav_bytes,
)
from voicestream.audio.ffmpeg import FFmpegSource
from voicestream.audio.file import WavFileSource, file_source
from voicestream.audio.vad import GateEvent, SpeechGate, VADSegmenter
from voicestream.config import Config, VADConfig
from voicestream.models import SAMPLE_RATE, AudioFrame, Utterance
from voicestream.transcribe.base import FakeTranscriber, TranscribeResult

FRAME_S = VADConfig().frame_ms / 1000
FRAME_LEN = int(SAMPLE_RATE * FRAME_S)
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")


def _tone(seconds: float, freq: float, rate: int, amp: float = 0.5) -> np.ndarray:
    t = np.arange(int(seconds * rate)) / rate
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _onset(k: int) -> float:
    return 1.0 + 4.0 * k


def _offset(k: int) -> float:
    return 3.5 + 4.0 * k


def _frames(x: np.ndarray, block_s: float = 0.1, t0: float = 0.0, source: str = "mic") -> Iterator[AudioFrame]:
    pcm = x if x.dtype == np.int16 else float_to_int16(x)
    n = max(1, int(block_s * SAMPLE_RATE))
    for i in range(0, len(pcm), n):
        yield AudioFrame(source, pcm[i : i + n], t0 + i / SAMPLE_RATE)


def _segment(
    x: np.ndarray, cfg: VADConfig | None = None, block_s: float = 0.1, t0: float = 0.0, flush: bool = True
) -> list[Utterance]:
    vad = VADSegmenter("mic", cfg)
    out = [u for f in _frames(x, block_s, t0) for u in vad.push(f)]
    return out + (vad.flush() if flush else [])


def _gate(x: np.ndarray, cfg: VADConfig | None = None, flush: bool = True) -> list[GateEvent]:
    gate = SpeechGate("mic", cfg)
    out = [e for f in _frames(x) for e in gate.push(f)]
    return out + (gate.flush() if flush else [])


def _speech_bursts(n: int, background: float) -> np.ndarray:
    """Like ``helpers.bursts`` but over a chosen background level (0 = exact digital silence)."""
    bg = lambda s, seed: noise(s, std=background, seed=seed)  # noqa: E731
    parts = [bg(1.0, 0)]
    for i in range(n):
        parts += [speechlike(2.5, seed=i + 10) + bg(2.5, i + 50), bg(1.5, i + 100)]
    return np.concatenate(parts)


# ----------------------------------------------------------------------------- DSP


@pytest.mark.parametrize(("src", "dst"), [(48_000, 16_000), (44_100, 16_000), (22_050, 16_000), (16_000, 24_000), (8_000, 16_000)])
def test_resampler_is_continuous_across_random_chunk_sizes(src: int, dst: int) -> None:
    rng = np.random.default_rng(src + dst)
    x = rng.normal(0, 0.3, 2 * src).astype(np.float32)
    one_shot = resample(x, src, dst)
    r = StreamResampler(src, dst)
    pieces, i = [], 0
    while i < len(x):
        n = int(rng.choice([1, 7, int(rng.integers(2, 3000))]))  # incl. blocks shorter than the FIR
        pieces.append(r.process(x[i : i + n]))
        i += n
    chunked = np.concatenate(pieces)
    assert len(chunked) == len(one_shot)
    np.testing.assert_allclose(chunked, one_shot, atol=1e-5)


def test_resampler_empty_block_is_a_no_op() -> None:
    x = np.random.default_rng(1).normal(0, 0.3, 4800).astype(np.float32)
    ref = StreamResampler(48_000, 16_000)
    expected = np.concatenate([ref.process(x[:2400]), ref.process(x[2400:])])
    r = StreamResampler(48_000, 16_000)
    a = r.process(x[:2400])
    assert r.process(np.zeros(0, dtype=np.float32)).size == 0  # drivers can deliver empty buffers
    np.testing.assert_allclose(np.concatenate([a, r.process(x[2400:])]), expected, atol=1e-6)


def test_resampler_attenuates_tones_above_the_new_nyquist() -> None:
    aliased = resample(_tone(1.0, 12_000, 48_000), 48_000, 16_000)  # would fold to 4 kHz
    passband = resample(_tone(1.0, 1_000, 48_000), 48_000, 16_000)
    assert rms_dbfs(aliased[100:]) < -50
    assert rms_dbfs(passband[100:]) == pytest.approx(rms_dbfs(_tone(1.0, 1_000, 48_000)), abs=0.5)
    assert rms_dbfs(resample(_tone(1.0, 10_000, 44_100), 44_100, 16_000)[100:]) < -45


def test_upsampling_16k_to_24k_keeps_duration_and_pitch() -> None:
    y = resample(_tone(1.0, 1_000, 16_000), 16_000, 24_000)
    assert abs(len(y) - 24_000) <= 2
    spectrum = np.abs(np.fft.rfft(y))
    assert np.argmax(spectrum) * 24_000 / len(y) == pytest.approx(1_000, abs=5)


def test_resampler_same_rate_is_a_copy_and_rejects_bad_rates() -> None:
    x = np.arange(10, dtype=np.float32)
    y = StreamResampler(16_000, 16_000).process(x)
    np.testing.assert_array_equal(x, y)
    assert y is not x
    with pytest.raises(ValueError):
        StreamResampler(0, 16_000)


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (np.array([-32768, 0, 16384], dtype=np.int16), [-1.0, 0.0, 0.5]),
        (np.array([-(2**31), 0, 2**30], dtype=np.int32), [-1.0, 0.0, 0.5]),
        (np.array([0, 128, 192], dtype=np.uint8), [-1.0, 0.0, 0.5]),
        (np.array([-0.25, 0.75], dtype=np.float64), [-0.25, 0.75]),
    ],
)
def test_to_mono_float_scales_each_dtype(data: np.ndarray, expected: list[float]) -> None:
    out = to_mono_float(data)
    assert out.dtype == np.float32
    np.testing.assert_allclose(out, expected, atol=1e-6)


def test_to_mono_float_downmixes_channels() -> None:
    stereo = np.array([[16384, 0], [-16384, -16384], [32767, -32768]], dtype=np.int16)
    np.testing.assert_allclose(to_mono_float(stereo), [0.25, -0.5, 0.0], atol=1e-4)
    single = np.array([[0.5], [-0.5]], dtype=np.float32)
    out = to_mono_float(single)
    assert out.shape == (2,) and out.flags.c_contiguous
    np.testing.assert_array_equal(out, [0.5, -0.5])


def test_float_to_int16_clips_instead_of_wrapping() -> None:
    np.testing.assert_array_equal(float_to_int16(np.array([2.0, -2.0, 0.5])), [32767, -32767, 16383])


@pytest.mark.parametrize("rate", [16_000, 24_000])
def test_wav_bytes_round_trip(rate: int) -> None:
    pcm = float_to_int16(_tone(0.25, 440, rate))
    with wave.open(io.BytesIO(wav_bytes(pcm, rate)), "rb") as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, rate)
        back = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    np.testing.assert_array_equal(back, pcm)


def test_rms_dbfs_levels() -> None:
    full = _tone(1.0, 440, 16_000, amp=1.0)
    assert rms_dbfs(full) == pytest.approx(-3.01, abs=0.02)
    half = float_to_int16(_tone(1.0, 440, 16_000, amp=0.5))
    assert rms_dbfs(half) == pytest.approx(-9.03, abs=0.02)
    assert rms_dbfs(half) == pytest.approx(rms_dbfs(half.astype(np.float32) / 32768.0), abs=1e-3)
    assert rms_dbfs(np.zeros(480, dtype=np.int16)) == -120.0
    assert rms_dbfs(np.zeros(0, dtype=np.float32)) == -120.0


# ------------------------------------------------------------------- VADSegmenter


def test_vad_cuts_one_utterance_per_burst_with_pre_roll_and_trimmed_tail() -> None:
    cfg = VADConfig()
    utts = _segment(bursts(3))
    assert len(utts) == 3
    for k, u in enumerate(utts):
        # pre-roll: starts ~pre_roll_s before the first speech frame
        assert u.t_start == pytest.approx(_onset(k) - cfg.pre_roll_s, abs=2 * FRAME_S)
        # trailing silence trimmed to a short tail, not the whole 0.7 s hangover
        assert _offset(k) <= u.t_end <= _offset(k) + 0.25
        assert u.t_end - u.t_start == pytest.approx(u.duration)
        assert 1.5 <= u.speech_seconds <= 2.5
        assert rms_dbfs(u.samples[: int(0.2 * SAMPLE_RATE)]) < -40  # pre-roll audio is the quiet lead-in
        assert u.source == "mic"


@pytest.mark.parametrize("block_s", [0.037, 0.5, 2.0])
def test_vad_result_does_not_depend_on_frame_block_size(block_s: float) -> None:
    ref = _segment(bursts(2))
    got = _segment(bursts(2), block_s=block_s)
    assert len(got) == len(ref) == 2
    for a, b in zip(got, ref):
        assert (a.t_start, a.t_end) == pytest.approx((b.t_start, b.t_end))
        np.testing.assert_array_equal(a.samples, b.samples)


@pytest.mark.parametrize("pre_roll_s", [0.15, 0.6])
def test_vad_pre_roll_length_follows_config(pre_roll_s: float) -> None:
    (u,) = _segment(bursts(1), VADConfig(pre_roll_s=pre_roll_s))
    assert u.t_start == pytest.approx(_onset(0) - pre_roll_s, abs=2 * FRAME_S)


def test_vad_timestamps_follow_the_frame_clock() -> None:
    utts = _segment(bursts(2), t0=100.0)
    assert [round(u.t_start - 100.0, 3) for u in utts] == [round(u.t_start, 3) for u in _segment(bursts(2))]


def test_vad_merges_short_utterances_within_merge_gap() -> None:
    x = np.concatenate([noise(1.0), speechlike(0.6, seed=3), noise(1.0, seed=4), speechlike(0.6, seed=5), noise(5.0, seed=6)])
    (u,) = _segment(x)
    assert u.t_start < 1.0 and u.t_end > 3.2  # covers both pieces
    assert u.speech_seconds == pytest.approx(0.96, abs=0.15)
    assert u.duration < u.t_end - u.t_start  # the silence between them is shortened, not sent in full


def test_vad_holds_a_short_utterance_then_releases_it_after_the_gap() -> None:
    cfg = VADConfig()
    x = np.concatenate([noise(1.0), speechlike(0.6, seed=3), noise(5.0, seed=6)])
    vad = VADSegmenter("mic", cfg)
    emitted: list[tuple[float, Utterance]] = []
    for f in _frames(x):
        emitted += [(f.t_end, u) for u in vad.push(f)]
    assert len(emitted) == 1
    at, u = emitted[0]
    assert u.duration < cfg.min_chunk_s
    assert u.t_end + cfg.merge_gap_s < at <= u.t_end + cfg.merge_gap_s + 0.2
    assert vad.flush() == []


def test_vad_does_not_merge_across_a_long_gap() -> None:
    x = np.concatenate([noise(1.0), speechlike(0.6, seed=3), noise(2.6, seed=6), speechlike(2.5, seed=7), noise(2.0, seed=8)])
    short, long = _segment(x, flush=False)
    assert short.t_end < 2.0 and long.t_start > 3.5
    assert long.t_start - short.t_end > VADConfig().merge_gap_s


def test_vad_flush_releases_a_held_short_utterance() -> None:
    x = np.concatenate([noise(1.0), speechlike(0.6, seed=3), noise(0.5, seed=6)])
    vad = VADSegmenter("mic")
    assert [u for f in _frames(x) for u in vad.push(f)] == []
    (u,) = vad.flush()
    assert u.t_start < 1.0 < u.t_end


def test_vad_splits_long_speech_at_a_quiet_frame_without_losing_audio() -> None:
    cfg = VADConfig(max_utterance_s=8.0)
    utts = _segment(np.concatenate([noise(1.0), speechlike(30.0, seed=3), noise(2.0, seed=6)]), cfg)
    assert len(utts) >= 4
    assert all(u.duration <= cfg.max_utterance_s for u in utts)
    for a, b in zip(utts, utts[1:]):
        assert b.t_start == pytest.approx(a.t_end)  # contiguous pieces
        assert rms_dbfs(b.samples[:FRAME_LEN]) < -40  # cut in a pause, not mid-syllable
    assert sum(len(u.samples) for u in utts) / SAMPLE_RATE == pytest.approx(utts[-1].t_end - utts[0].t_start)


def test_vad_default_max_utterance_split() -> None:
    first, second = _segment(np.concatenate([noise(1.0), speechlike(30.0, seed=3), noise(2.0, seed=6)]))
    assert first.duration <= VADConfig().max_utterance_s
    assert second.t_start == pytest.approx(first.t_end)
    assert second.t_end > 31.0


def test_vad_flush_closes_speech_still_in_progress() -> None:
    x = np.concatenate([noise(1.0), speechlike(2.5, seed=3)])
    vad = VADSegmenter("mic")
    assert [u for f in _frames(x) for u in vad.push(f)] == []
    assert vad.in_speech
    (u,) = vad.flush()
    assert u.t_start < 1.0 and u.t_end == pytest.approx(len(x) / SAMPLE_RATE, abs=0.3)
    assert vad.flush() == []


def test_vad_restarts_timing_after_a_discontinuity_in_silence() -> None:
    vad = VADSegmenter("mic")
    fl = list(_frames(bursts(1))) + list(_frames(bursts(1), t0=50.0))
    first, second = [u for f in fl for u in vad.push(f)] + vad.flush()
    pre_roll = VADConfig().pre_roll_s
    assert first.t_start == pytest.approx(_onset(0) - pre_roll, abs=0.1)
    assert second.t_start == pytest.approx(50.0 + _onset(0) - pre_roll, abs=0.1)


def test_vad_utterance_spanning_a_discontinuity_ends_on_the_new_clock() -> None:
    x = float_to_int16(bursts(1))
    cut = int(2.0 * SAMPLE_RATE)  # mid-speech; the device then resumes 48 s later
    fl = list(_frames(x[:cut])) + list(_frames(x[cut:], t0=50.0))
    vad = VADSegmenter("mic")
    utts = [u for f in fl for u in vad.push(f)] + vad.flush()
    assert max(u.t_end for u in utts) > 51.0  # speech resumed at 50 s and ran until 51.5 s


def test_vad_spots_a_discontinuity_when_frames_end_on_a_frame_boundary() -> None:
    x = float_to_int16(bursts(1))
    cut = 67 * FRAME_LEN  # mid-speech; 30 ms blocks leave the VAD's buffer empty at the jump
    fl = list(_frames(x[:cut], block_s=FRAME_S)) + list(_frames(x[cut:], block_s=FRAME_S, t0=50.0))
    vad = VADSegmenter("mic")
    utts = [u for f in fl for u in vad.push(f)] + vad.flush()
    assert max(u.t_end for u in utts) > 51.0
    assert all(u.t_end <= cut / SAMPLE_RATE + 0.05 or u.t_start >= 50.0 for u in utts)  # none spans the gap


def test_vad_pre_roll_does_not_carry_across_a_discontinuity() -> None:
    x = float_to_int16(bursts(1))
    cut = int(_onset(0) * SAMPLE_RATE)  # the device resumes 49 s later just as someone starts talking
    fl = list(_frames(x[:cut])) + list(_frames(x[cut:], t0=50.0))
    vad = VADSegmenter("mic")
    (u,) = [u for f in fl for u in vad.push(f)] + vad.flush()
    assert u.t_start >= 50.0  # its lead-in isn't audio from before the gap
    assert u.t_end == pytest.approx(50.0 + _offset(0) - _onset(0), abs=0.25)


@pytest.mark.parametrize("background", [0.0, 0.0005, 0.005, 0.02])
def test_vad_works_over_different_noise_floors(background: float) -> None:
    """0.0 = exact zeros between bursts, which is what system loopback delivers in silence."""
    utts = _segment(_speech_bursts(3, background))
    assert len(utts) == 3
    for k, u in enumerate(utts):
        assert u.t_start < _onset(k) < _offset(k) <= u.t_end + 0.05


def test_vad_hears_a_quiet_remote_speaker_over_digital_silence() -> None:
    t = np.arange(int(2.5 * SAMPLE_RATE)) / SAMPLE_RATE
    quiet = (0.01 * ((t % 0.3) < 0.22) * np.sin(2 * np.pi * 180 * t)).astype(np.float32)  # ~-43 dBFS
    zeros = lambda s: np.zeros(int(s * SAMPLE_RATE), dtype=np.float32)  # noqa: E731
    assert len(_segment(np.concatenate([zeros(1.0), quiet, zeros(1.5), quiet, zeros(2.0)]))) == 2


def test_vad_ignores_steady_background_noise() -> None:
    assert _segment(noise(20.0, std=0.02, seed=7)) == []


def test_vad_handles_a_stream_that_starts_mid_speech() -> None:
    x = np.concatenate([speechlike(2.5, seed=3), noise(2.0, seed=6), speechlike(2.5, seed=4), noise(2.0, seed=7)])
    first, second = _segment(x)
    assert first.t_start == pytest.approx(0.0, abs=0.05)
    assert 2.5 <= first.t_end <= 2.8
    assert second.t_start == pytest.approx(4.5 - 0.3, abs=0.1)


@pytest.mark.parametrize(
    ("scenario", "cfg"),
    [
        ("noise_step", VADConfig()),  # a fan switches on: trimming later drops the "speech" frames
        ("long_speech", VADConfig(max_utterance_s=8.0)),  # each split piece is credited the whole run
    ],
)
def test_vad_speech_seconds_never_exceed_duration(scenario: str, cfg: VADConfig) -> None:
    if scenario == "noise_step":
        x = np.concatenate([noise(5.0), noise(20.0, std=0.02, seed=7)])
    else:
        x = np.concatenate([noise(1.0), speechlike(30.0, seed=3), noise(2.0, seed=6)])
    utts = _segment(x, cfg)
    # is_hallucination() trusts speech_seconds to spot "Thank you." on near-silent clips
    assert all(u.speech_seconds <= u.duration + 1e-9 for u in utts), [(u.duration, u.speech_seconds) for u in utts]


def test_vad_reports_levels() -> None:
    levels: list[tuple[float, bool]] = []
    vad = VADSegmenter("mic", on_level=lambda db, sp: levels.append((db, sp)))
    for f in _frames(bursts(1)):
        vad.push(f)
    assert len(levels) == len(bursts(1)) // FRAME_LEN
    assert any(sp for _, sp in levels) and not all(sp for _, sp in levels)


# ---------------------------------------------------------------------- SpeechGate


def test_speech_gate_event_sequence_and_timing() -> None:
    cfg = VADConfig()
    events = _gate(bursts(2))
    kinds = [e.kind for e in events]
    assert [k for k in kinds if k != "audio"] == ["start", "end", "start", "end"]
    assert kinds[0] == "start" and kinds[-1] == "end"
    starts = [i for i, k in enumerate(kinds) if k == "start"]
    ends = [i for i, k in enumerate(kinds) if k == "end"]
    for k, (si, ei) in enumerate(zip(starts, ends, strict=True)):
        start, end = events[si], events[ei]
        assert start.samples is not None
        assert start.t == pytest.approx(_onset(k) - cfg.pre_roll_s, abs=2 * FRAME_S)
        # start carries pre-roll + the frames that confirmed speech
        assert len(start.samples) == int(cfg.pre_roll_s / FRAME_S + cfg.start_frames) * FRAME_LEN
        assert rms_dbfs(start.samples[: int(0.2 * SAMPLE_RATE)]) < -40
        # ends after hangover_s of silence
        assert _offset(k) + cfg.hangover_s - 0.05 <= end.t <= _offset(k) + cfg.hangover_s + 0.1
        assert end.t_start == start.t
        assert 1.5 <= end.speech_seconds <= 2.5
        # audio frames are contiguous: nothing between start and end is lost or repeated
        audio = events[si + 1 : ei]
        assert all(e.kind == "audio" and e.samples is not None and len(e.samples) == FRAME_LEN for e in audio)
        assert audio[0].t == pytest.approx(start.t + len(start.samples) / SAMPLE_RATE)
        assert all(b.t == pytest.approx(a.t + FRAME_S) for a, b in zip(audio, audio[1:]))
        assert end.t == pytest.approx(audio[-1].t + FRAME_S)


def test_speech_gate_ends_at_max_length_and_restarts_without_a_gap() -> None:
    cfg = VADConfig()
    events = _gate(np.concatenate([noise(1.0), speechlike(30.0, seed=3), noise(2.0, seed=6)]))
    marks = [e for e in events if e.kind != "audio"]
    assert [e.kind for e in marks] == ["start", "end", "start", "end"]
    first_start, first_end, second_start, _ = marks
    assert first_end.t - first_start.t == pytest.approx(cfg.max_utterance_s, abs=2 * FRAME_S)
    assert second_start.t == pytest.approx(first_end.t)  # continues where the cut was made


def test_speech_gate_flush() -> None:
    gate = SpeechGate("mic")
    x = np.concatenate([noise(1.0), speechlike(2.0, seed=3)])
    events = [e for f in _frames(x) for e in gate.push(f)]
    assert [e.kind for e in events if e.kind != "audio"] == ["start"]
    (end,) = gate.flush()
    assert end.kind == "end" and end.t == pytest.approx(len(x) / SAMPLE_RATE, abs=FRAME_S)
    assert not gate.active and gate.flush() == []
    quiet = SpeechGate("mic")
    for f in _frames(noise(2.0)):
        assert quiet.push(f) == []
    assert quiet.flush() == []


# ------------------------------------------------------------------ driver sources


class _ManualClock(Clock):
    def __init__(self) -> None:
        super().__init__()
        self.t = 0.0

    def now(self) -> float:
        return self.t


class _DriverSource(QueueAudioSource):
    """Stand-in for a driver-callback source: delivers 0.1 s of 48 kHz audio per start; with
    ``signals_end`` its capture thread reports end-of-stream when stopped, as ``LoopbackSource``'s does."""

    def __init__(self, clock: Clock, signals_end: bool = False) -> None:
        super().__init__("system", clock)
        self.signals_end = signals_end

    def start_capture(self) -> None:
        self.push_from_thread(np.zeros(4800, dtype=np.float32), 48_000)

    def stop_capture(self) -> None:
        if self.signals_end:
            self.signal_end()


async def _one_frame_then_fail(src: QueueAudioSource) -> AudioFrame:
    """One run of the source that ends early, as when transcription fails downstream."""
    frames = src.frames()
    frame = await anext(frames)
    await frames.aclose()
    await asyncio.sleep(0)  # let the capture thread's callbacks land
    return frame


async def test_queue_source_restarted_after_a_failure_delivers_audio_again() -> None:
    src = _DriverSource(Clock(), signals_end=True)
    await _one_frame_then_fail(src)
    src.reset()  # what Assistant._pipe does before retrying a live source
    frames = src.frames()
    frame = await asyncio.wait_for(anext(frames, None), 1.0)
    await frames.aclose()
    assert frame is not None


async def test_queue_source_restarted_later_is_stamped_on_the_meeting_clock() -> None:
    clock = _ManualClock()
    src = _DriverSource(clock)
    clock.t = 5.0
    assert (await _one_frame_then_fail(src)).t_start == pytest.approx(5.0)
    clock.t = 65.0  # the device was gone for a minute
    src.reset()
    assert (await _one_frame_then_fail(src)).t_start == pytest.approx(65.0, abs=0.2)


async def test_queue_source_stamps_frames_by_sample_count_from_the_first_buffer() -> None:
    clock = _ManualClock()
    src = _DriverSource(clock)
    clock.t = 2.0
    frames = src.frames()
    first = await anext(frames)
    src.push_from_thread(np.zeros(4800, dtype=np.float32), 48_000)
    clock.t = 9.0  # callback jitter must not move timestamps
    second = await anext(frames)
    await frames.aclose()
    assert first.t_start == pytest.approx(2.0) and second.t_start == pytest.approx(first.t_end)
    assert first.samples.dtype == np.int16 and len(first.samples) == 1600


class _DyingDriver(_DriverSource):
    """Its capture thread dies after the first buffer, as ``LoopbackSource``'s worker does on a device error."""

    def start_capture(self) -> None:
        super().start_capture()
        self.signal_end()


async def test_queue_source_whose_capture_thread_dies_fails_instead_of_ending() -> None:
    frames = _DyingDriver(Clock()).frames()
    assert len((await anext(frames)).samples) == 1600
    with pytest.raises(RuntimeError, match="ended unexpectedly"):  # a normal end makes _pipe give the source up
        await asyncio.wait_for(anext(frames), 1.0)


class _SpeakingDriver(QueueAudioSource):
    """A driver source whose capture thread hears one sentence (then silence) per start, in 0.1 s buffers,
    and reports end-of-stream when stopped, as ``LoopbackSource``'s does."""

    def __init__(self, clock: Clock) -> None:
        super().__init__("system", clock)
        self.stops = 0

    def start_capture(self) -> None:
        x = np.concatenate([noise(0.5), speechlike(2.0, seed=3), noise(1.5, seed=4)])
        for i in range(0, len(x), 1600):
            self.push_from_thread(x[i : i + 1600], SAMPLE_RATE)

    def stop_capture(self) -> None:
        self.signal_end()
        self.stops += 1


class _FailsOnce(FakeTranscriber):
    """The first request dies with an error that ends the stream (as a streaming backend's does when its
    socket can't be re-established; a per-utterance error would only skip that utterance); later ones work."""

    async def _transcribe_with_retry(self, utt: Utterance, prompt: str | None) -> TranscribeResult | None:
        if not self.calls:
            self.calls.append(utt)
            raise ConnectionResetError("socket is dead")
        return await super()._transcribe_with_retry(utt, prompt)


async def test_assistant_restarts_a_failed_driver_source_on_the_meeting_clock(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The restart path end to end: Assistant._pipe resets the source after a transcription failure, and
    the next sentence is heard again and stamped on the meeting clock."""
    from voicestream import session as session_mod
    from voicestream.session import Assistant

    clock = _ManualClock()
    src = _SpeakingDriver(clock)
    real_sleep = asyncio.sleep

    async def backoff(delay: float, result: object = None) -> object:  # _pipe's wait before retrying
        while src.stops < 1:  # the failed run's capture is torn down meanwhile...
            await real_sleep(0.01)
        await real_sleep(0.05)  # ...and its end-of-stream marker lands
        clock.t += 60.0  # the device was gone for a minute
        return result

    monkeypatch.setattr(session_mod, "asyncio", types.SimpleNamespace(**{**vars(asyncio), "sleep": backoff}))
    stt = _FailsOnce(["Je reviens."], vad=cfg.vad)
    a = Assistant(cfg, sources=[src], transcriber_factory=lambda s: stt, llm=None, sinks=[], always_on=True)
    a.transcribers[src.name] = stt
    clock.t = 5.0
    pipe = asyncio.ensure_future(a._pipe(src))
    got = asyncio.ensure_future(a._merge_q.get())
    try:
        await asyncio.wait({got, pipe}, timeout=5.0, return_when=asyncio.FIRST_COMPLETED)
        assert got.done(), "system audio never came back after the restart"
    finally:
        a._stop.set()
        src.stop()
        got.cancel()
        await asyncio.wait_for(pipe, 5.0)
    seg = got.result()
    assert seg.text == "Je reviens." and len(stt.calls) == 2
    assert 65.0 <= seg.t_start < 66.0  # heard right after the restart, not stamped a minute in the past


# ----------------------------------------------------------------------- file replay


def _write_stereo_48k(path: Path, seconds: float = 2.0) -> Path:
    left = _tone(seconds, 440, 48_000, amp=0.5)
    pcm = float_to_int16(np.stack([left, np.zeros_like(left)], axis=1))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(48_000)
        w.writeframes(pcm.astype("<i2").tobytes())
    return path


async def test_wav_source_resamples_48k_stereo_to_16k_mono(tmp_path: Path) -> None:
    src = WavFileSource(_write_stereo_48k(tmp_path / "call.wav"), name="system", clock=Clock(), speed=0)
    t0 = time.monotonic()
    frames = [f async for f in src.frames()]
    assert time.monotonic() - t0 < 1.0  # speed=0: as fast as possible
    assert all(f.source == "system" and f.samples.dtype == np.int16 for f in frames)
    assert len(frames[0].samples) == int(0.1 * SAMPLE_RATE)
    assert all(b.t_start == pytest.approx(a.t_end) for a, b in zip(frames, frames[1:]))
    pcm = np.concatenate([f.samples for f in frames])
    assert len(pcm) / SAMPLE_RATE == pytest.approx(2.0, abs=0.01)
    assert frames[-1].t_end - frames[0].t_start == pytest.approx(2.0, abs=0.01)
    # left = 0.5 sine, right = silence -> mono downmix is a 0.25 sine
    assert np.abs(pcm[1000:]).max() / 32768 == pytest.approx(0.25, abs=0.01)


@pytest.mark.parametrize("width", [1, 2, 4])
async def test_wav_source_reads_each_pcm_width(tmp_path: Path, width: int) -> None:
    x = _tone(0.5, 300, SAMPLE_RATE, amp=0.5)
    if width == 1:
        raw = np.round(x * 127 + 128).astype(np.uint8).tobytes()
    elif width == 2:
        raw = float_to_int16(x).astype("<i2").tobytes()
    else:
        raw = np.round(x * 2**31 * 0.999).astype("<i4").tobytes()
    path = tmp_path / f"w{width}.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(width)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(raw)
    pcm = np.concatenate([f.samples async for f in WavFileSource(path, speed=0).frames()])
    np.testing.assert_allclose(pcm / 32768, x, atol=0.01)


async def test_wav_source_stops_promptly(tmp_path: Path) -> None:
    src = WavFileSource(write_wav(tmp_path / "long.wav", noise(30.0)), speed=0)
    got = 0
    async for _ in src.frames():
        got += 1
        src.stop()
    assert got == 1


def test_file_source_picks_reader_by_format(tmp_path: Path) -> None:
    wav = write_wav(tmp_path / "a.wav", noise(0.5))
    assert isinstance(file_source(wav, speed=0), WavFileSource)
    mp3 = file_source(tmp_path / "a.mp3", name="system", speed=0)
    assert isinstance(mp3, FFmpegSource) and mp3.live is False and mp3.name == "system"
    assert "-re" not in mp3.input_args
    assert file_source(tmp_path / "a.mp3", speed=1.0).input_args[:1] == ["-re"]  # real-time replay


def test_file_source_hands_wavs_it_cannot_read_to_ffmpeg(tmp_path: Path) -> None:
    path = tmp_path / "w24.wav"
    with wave.open(str(path), "wb") as w:  # 24-bit PCM: wave opens it, numpy has no dtype for it
        w.setnchannels(1)
        w.setsampwidth(3)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(bytes(3 * 1600))
    src = file_source(path, name="system", speed=0)
    assert isinstance(src, FFmpegSource) and src.live is False
    assert (src.name, src.input_args) == ("system", ["-i", str(path)])


@needs_ffmpeg
async def test_24_bit_wav_is_decoded(tmp_path: Path) -> None:
    x = _tone(0.5, 300, SAMPLE_RATE, amp=0.5)
    pcm24 = np.round(x * (2**23 - 1)).astype("<i4").view(np.uint8).reshape(-1, 4)[:, :3]
    path = tmp_path / "w24.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(3)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm24.tobytes())
    pcm = np.concatenate([f.samples async for f in file_source(path, speed=0).frames()])
    assert len(pcm) / SAMPLE_RATE == pytest.approx(0.5, abs=0.05)
    assert rms_dbfs(pcm) == pytest.approx(rms_dbfs(x), abs=0.5)


def test_file_spec_keeps_an_at_sign_inside_the_path(cfg: Config) -> None:
    path = "/Users/alex/Library/CloudStorage/GoogleDrive-alex@gmail.com/My Drive/standup.m4a"
    named = build_source(f"file:{path}@system", cfg, Clock(), speed=0)  # what `voicestream replay` builds
    assert isinstance(named, FFmpegSource) and (named.name, named.input_args) == ("system", ["-i", path])
    plain = build_source(f"file:{path}", cfg, Clock(), speed=0)
    assert isinstance(plain, FFmpegSource) and (plain.name, plain.input_args) == ("mic", ["-i", path])


def test_ffmpeg_spec_takes_only_a_plain_name_after_the_last_at_sign(cfg: Config) -> None:
    path = "/Users/alex/Library/CloudStorage/GoogleDrive-alex@gmail.com/My Drive/standup.m4a"
    src = build_source(f"ffmpeg:-i '{path}'", cfg, Clock())
    assert isinstance(src, FFmpegSource) and (src.name, src.input_args) == ("system", ["-i", path])
    dshow = "audio=@device_cm_{33D9A762-90C8-11D0-BD43-00A0C911CE86}\\wave_{5C6A0F7E}"  # DirectShow's alternative name
    src = build_source(f'ffmpeg:-f dshow -i "{dshow}"', cfg, Clock())
    assert (src.name, src.input_args) == ("system", ["-f", "dshow", "-i", dshow])
    named = build_source(f"ffmpeg:-i '{path}'@mic", cfg, Clock())
    assert (named.name, named.input_args) == ("mic", ["-i", path])


@needs_ffmpeg
@pytest.mark.parametrize("ext", [".mp3", ".ogg"])
async def test_ffmpeg_decodes_compressed_files(tmp_path: Path, ext: str) -> None:
    out = tmp_path / f"call{ext}"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(_write_stereo_48k(tmp_path / "in.wav")), str(out)],
        check=True,
        timeout=30,
    )
    src = file_source(out, speed=0)
    assert isinstance(src, FFmpegSource)
    frames = [f async for f in src.frames()]
    pcm = np.concatenate([f.samples for f in frames])
    assert pcm.dtype == np.int16
    assert len(pcm) / SAMPLE_RATE == pytest.approx(2.0, abs=0.1)
    assert rms_dbfs(pcm[2000:-2000]) == pytest.approx(rms_dbfs(_tone(1.0, 440, SAMPLE_RATE, amp=0.25)), abs=1.5)


@needs_ffmpeg
async def test_float_wav_falls_back_to_ffmpeg(tmp_path: Path) -> None:
    out = tmp_path / "float.wav"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(write_wav(tmp_path / "i.wav", speechlike(1.0))),
         "-c:a", "pcm_f32le", str(out)],
        check=True,
        timeout=30,
    )
    src = file_source(out, speed=0)
    assert isinstance(src, FFmpegSource)
    pcm = np.concatenate([f.samples async for f in src.frames()])
    assert len(pcm) / SAMPLE_RATE == pytest.approx(1.0, abs=0.05)


async def test_vad_over_replayed_file_matches_in_memory(tmp_path: Path) -> None:
    src = WavFileSource(write_wav(tmp_path / "b.wav", bursts(2)), clock=Clock(), speed=0)
    vad = VADSegmenter("mic")
    utts: list[Utterance] = []
    t_first: float | None = None
    async for f in src.frames():
        t_first = f.t_start if t_first is None else t_first
        utts += vad.push(f)
    utts += vad.flush()
    assert t_first is not None
    assert [round(u.t_start - t_first, 2) for u in utts] == [round(u.t_start, 2) for u in _segment(bursts(2))]
