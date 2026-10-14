"""Audio source abstraction, meeting clock and DSP helpers (resampling, format conversion)."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

import numpy as np

from ..models import SAMPLE_RATE, AudioFrame

log = logging.getLogger(__name__)


class Clock:
    """Meeting clock: seconds since the meeting started (monotonic)."""

    def __init__(self) -> None:
        self.t0 = time.monotonic()

    def now(self) -> float:
        return time.monotonic() - self.t0

    def reset(self) -> None:
        self.t0 = time.monotonic()


class AudioSource(ABC):
    """Produces :class:`AudioFrame` blocks (int16 mono @ ``SAMPLE_RATE``) until stopped."""

    live: bool = True
    """False for recordings (replay): errors are fatal instead of retried."""

    def __init__(self, name: str, clock: Clock | None = None) -> None:
        self.name = name
        self.clock = clock or Clock()
        self._stop = asyncio.Event()

    @abstractmethod
    def frames(self) -> AsyncIterator[AudioFrame]:
        """Async iterator of frames. Must end promptly after :meth:`stop`."""

    def stop(self) -> None:
        self._stop.set()

    def reset(self) -> None:
        """Re-arm after a failure so :meth:`frames` can be called again."""
        self._stop = asyncio.Event()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def describe(self) -> str:
        return self.name


class QueueAudioSource(AudioSource):
    """Base for sources whose audio arrives on a foreign thread (driver callbacks).

    Subclasses start their capture in :meth:`start_capture` and call
    :meth:`push_from_thread` with raw float/int samples at the device rate.
    """

    max_backlog_s = 30.0

    def __init__(self, name: str, clock: Clock | None = None) -> None:
        super().__init__(name, clock)
        self._queue: asyncio.Queue[AudioFrame | None] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._resampler: StreamResampler | None = None
        self._device_rate: int | None = None
        self._samples_out = 0
        self._t_origin: float | None = None
        self._dropped = 0

    @abstractmethod
    def start_capture(self) -> None:
        """Open the device and start delivering audio via :meth:`push_from_thread`."""

    @abstractmethod
    def stop_capture(self) -> None:
        """Close the device. Called from the event loop thread."""

    def push_from_thread(self, data: np.ndarray, rate: int) -> None:
        """Called from the capture thread with shape (n,) or (n, channels) audio."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        mono = to_mono_float(data)
        if self._resampler is None or self._device_rate != rate:
            self._resampler = StreamResampler(rate, SAMPLE_RATE)
            self._device_rate = rate
        out = float_to_int16(self._resampler.process(mono))
        if out.size == 0:
            return
        if self._t_origin is None:
            self._t_origin = self.clock.now()
        frame = AudioFrame(
            source=self.name,
            samples=out,
            t_start=self._t_origin + self._samples_out / SAMPLE_RATE,
        )
        self._samples_out += out.size
        try:
            loop.call_soon_threadsafe(self._enqueue, frame)
        except RuntimeError:  # loop closed during shutdown
            pass

    def _enqueue(self, frame: AudioFrame | None) -> None:
        if frame is not None:
            backlog = self._queue.qsize() * frame.duration
            if backlog > self.max_backlog_s:
                self._dropped += 1
                if self._dropped in (1, 10, 100) or self._dropped % 1000 == 0:
                    log.warning("%s: consumer too slow, dropped %d frames", self.name, self._dropped)
                return
        self._queue.put_nowait(frame)

    async def frames(self) -> AsyncIterator[AudioFrame]:
        self._loop = asyncio.get_running_loop()
        await asyncio.to_thread(self.start_capture)
        stopper = asyncio.ensure_future(self._stop.wait())
        try:
            while True:
                getter = asyncio.ensure_future(self._queue.get())
                done, _ = await asyncio.wait({getter, stopper}, return_when=asyncio.FIRST_COMPLETED)
                if getter in done:
                    frame = getter.result()
                    if frame is None:
                        break
                    yield frame
                else:
                    getter.cancel()
                    break
        finally:
            stopper.cancel()
            await asyncio.to_thread(self.stop_capture)

    def signal_end(self) -> None:
        """Capture thread hit end-of-stream or a fatal error."""
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._enqueue, None)


# ------------------------------------------------------------------------------ DSP


def to_mono_float(data: np.ndarray) -> np.ndarray:
    """Any-dtype (n,) or (n, ch) array -> float32 mono in [-1, 1]."""
    arr = np.asarray(data)
    if arr.dtype == np.int16:
        arr = arr.astype(np.float32) / 32768.0
    elif arr.dtype == np.int32:
        arr = arr.astype(np.float32) / 2147483648.0
    elif arr.dtype == np.uint8:
        arr = (arr.astype(np.float32) - 128.0) / 128.0
    else:
        arr = arr.astype(np.float32, copy=False)
    if arr.ndim == 2:
        arr = arr.mean(axis=1) if arr.shape[1] > 1 else arr[:, 0]
    return np.ascontiguousarray(arr, dtype=np.float32)


def float_to_int16(x: np.ndarray) -> np.ndarray:
    return (np.clip(x, -1.0, 1.0) * 32767.0).astype(np.int16)


def int16_to_float(x: np.ndarray) -> np.ndarray:
    return x.astype(np.float32) / 32768.0


def rms_dbfs(x: np.ndarray) -> float:
    """RMS level in dBFS of an int16 or float block."""
    if x.size == 0:
        return -120.0
    f = int16_to_float(x) if x.dtype == np.int16 else x.astype(np.float32, copy=False)
    rms = float(np.sqrt(np.mean(np.square(f, dtype=np.float64))))
    return 20.0 * math.log10(rms) if rms > 1e-6 else -120.0


def _lowpass_taps(cutoff: float, ntaps: int) -> np.ndarray:
    """Windowed-sinc low-pass. ``cutoff`` in cycles/sample (0..0.5)."""
    n = np.arange(ntaps) - (ntaps - 1) / 2.0
    h = 2 * cutoff * np.sinc(2 * cutoff * n) * np.hamming(ntaps)
    return (h / h.sum()).astype(np.float32)


class StreamResampler:
    """Stateful streaming resampler (anti-alias FIR + linear interpolation).

    Good enough for speech, has no dependencies beyond numpy, and is continuous
    across block boundaries so it can be fed arbitrary chunk sizes.
    """

    def __init__(self, src_rate: int, dst_rate: int, ntaps: int = 63) -> None:
        if src_rate <= 0 or dst_rate <= 0:
            raise ValueError("sample rates must be positive")
        self.src_rate = src_rate
        self.dst_rate = dst_rate
        self.step = src_rate / dst_rate  # input samples per output sample
        self._taps = _lowpass_taps(0.45 * dst_rate / src_rate, ntaps) if dst_rate < src_rate else None
        self._hist = np.zeros(ntaps - 1, dtype=np.float32) if self._taps is not None else None
        self._pending = np.zeros(0, dtype=np.float32)
        self._pos = 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float32)
        if self.src_rate == self.dst_rate:
            return x.copy()
        if self._taps is not None and self._hist is not None:
            xin = np.concatenate([self._hist, x])
            y = np.convolve(xin, self._taps, mode="valid").astype(np.float32)
            self._hist = xin[-(len(self._taps) - 1) :]
        else:
            y = x
        buf = np.concatenate([self._pending, y])
        last = len(buf) - 1
        if last < self._pos:
            self._pending = buf
            return np.zeros(0, dtype=np.float32)
        n = int(math.floor((last - self._pos) / self.step)) + 1
        t = self._pos + np.arange(n, dtype=np.float64) * self.step
        out = np.interp(t, np.arange(len(buf), dtype=np.float64), buf).astype(np.float32)
        new_pos = self._pos + n * self.step
        drop = min(int(math.floor(new_pos)), len(buf))
        self._pending = buf[drop:]
        self._pos = new_pos - drop
        return out


def resample(x: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """One-shot resample of a float32 array."""
    return StreamResampler(src_rate, dst_rate).process(x)


def pcm16_bytes(samples: np.ndarray) -> bytes:
    return np.asarray(samples, dtype="<i2").tobytes()


def wav_bytes(samples: np.ndarray, rate: int = SAMPLE_RATE) -> bytes:
    """Encode int16 mono samples as an in-memory WAV file."""
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm16_bytes(samples))
    return buf.getvalue()


async def aclose_quietly(it: object) -> None:
    aclose = getattr(it, "aclose", None)
    if aclose is not None:
        try:
            await aclose()
        except Exception:  # pragma: no cover - best effort
            pass
