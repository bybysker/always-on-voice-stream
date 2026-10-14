"""Shared test helpers (import as ``from helpers import seg, bursts, write_wav``)."""

from __future__ import annotations

import wave
from datetime import date
from pathlib import Path

import numpy as np

from voicestream.audio.base import float_to_int16
from voicestream.models import SAMPLE_RATE, TranscriptSegment

WEDNESDAY = date(2026, 10, 14)


def seg(text: str, source: str = "mic", t: float = 0.0, dur: float = 3.0) -> TranscriptSegment:
    speaker = {"mic": "Me", "system": "Others"}.get(source, source)
    return TranscriptSegment(source=source, speaker=speaker, text=text, t_start=t, t_end=t + dur)


def noise(seconds: float, std: float = 0.002, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.normal(0, std, int(seconds * SAMPLE_RATE)).astype(np.float32)


def speechlike(seconds: float, amp: float = 0.2, seed: int = 1) -> np.ndarray:
    """Syllable-like bursts (220 ms on / 80 ms off) of a 180 Hz tone over light noise."""
    n = int(seconds * SAMPLE_RATE)
    t = np.arange(n) / SAMPLE_RATE
    env = ((t % 0.3) < 0.22).astype(np.float32)
    return (amp * env * np.sin(2 * np.pi * 180 * t)).astype(np.float32) + noise(seconds, seed=seed)


def write_wav(path: Path, samples: np.ndarray, rate: int = SAMPLE_RATE) -> Path:
    pcm = float_to_int16(samples) if samples.dtype != np.int16 else samples
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.astype("<i2").tobytes())
    return path


def bursts(n: int, speech_s: float = 2.5, gap_s: float = 1.5) -> np.ndarray:
    parts = [noise(1.0)]
    for i in range(n):
        parts += [speechlike(speech_s, seed=i + 10), noise(gap_s, seed=i + 100)]
    return np.concatenate(parts)
