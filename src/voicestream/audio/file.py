"""Replay a recording as if it were live (testing, demos, post-meeting processing)."""

from __future__ import annotations

import asyncio
import wave
from collections.abc import AsyncIterator
from pathlib import Path

import numpy as np

from ..models import SAMPLE_RATE, AudioFrame
from .base import AudioSource, Clock, StreamResampler, float_to_int16, to_mono_float
from .ffmpeg import FFmpegSource


class WavFileSource(AudioSource):
    """Streams a PCM WAV file in ``block_s`` frames.

    ``speed`` controls pacing: 1.0 = real time, 0 = as fast as possible.
    """

    live = False

    def __init__(
        self,
        path: str | Path,
        name: str = "mic",
        clock: Clock | None = None,
        speed: float = 1.0,
        block_s: float = 0.1,
    ) -> None:
        super().__init__(name, clock)
        self.path = Path(path)
        self.speed = speed
        self.block_s = block_s

    def describe(self) -> str:
        return f"{self.name}: file {self.path.name}"

    async def frames(self) -> AsyncIterator[AudioFrame]:
        with wave.open(str(self.path), "rb") as w:
            rate = w.getframerate()
            channels = w.getnchannels()
            width = w.getsampwidth()
            if width not in (1, 2, 4):
                raise ValueError(f"unsupported WAV sample width {width} in {self.path}")
            dtype = {1: np.uint8, 2: np.int16, 4: np.int32}[width]
            resampler = StreamResampler(rate, SAMPLE_RATE)
            block = max(1, int(rate * self.block_s))
            t_out = 0.0
            loop = asyncio.get_running_loop()
            t_wall0 = loop.time()
            while not self.stopped:
                raw = w.readframes(block)
                if not raw:
                    break
                data = np.frombuffer(raw, dtype=dtype)
                if channels > 1:
                    data = data.reshape(-1, channels)
                out = float_to_int16(resampler.process(to_mono_float(data)))
                if out.size:
                    yield AudioFrame(source=self.name, samples=out, t_start=t_out)
                    t_out += out.size / SAMPLE_RATE
                if self.speed > 0:
                    delay = t_wall0 + t_out / self.speed - loop.time()
                    if delay > 0:
                        await asyncio.sleep(delay)
                else:
                    await asyncio.sleep(0)


def file_source(
    path: str | Path, name: str = "mic", clock: Clock | None = None, speed: float = 1.0
) -> AudioSource:
    """WAV files are read natively; anything else (mp3, m4a, webm, mp4...) via ffmpeg."""
    p = Path(path)
    if p.suffix.lower() == ".wav":
        try:
            with wave.open(str(p), "rb"):
                pass
            return WavFileSource(p, name=name, clock=clock, speed=speed)
        except wave.Error:
            pass  # e.g. float WAV; let ffmpeg decode it
    args = (["-re"] if speed == 1.0 else []) + ["-i", str(p)]
    src = FFmpegSource(name=name, input_args=args, clock=clock, describe_as=f"file {p.name}")
    src.live = False
    return src
