"""System audio ("what the other participants say") via the ``soundcard`` library:
WASAPI loopback of the default output on Windows, the PulseAudio/PipeWire
monitor source on Linux. (On macOS see :mod:`.mactap` or a BlackHole device.)

Notes (SoundCard 0.4.6):
* the loopback "microphone" must be selected by *id* — on Linux names are sink
  descriptions that the real microphone often shares;
* WASAPI records garbage in mono, so Windows captures 2 channels and downmixes;
* both backends resample server-side, so we ask for 16 kHz directly.
"""

from __future__ import annotations

import logging
import sys
import threading
import warnings
from typing import Any

import numpy as np

from ..models import SAMPLE_RATE
from .base import Clock, QueueAudioSource

log = logging.getLogger(__name__)

warnings.filterwarnings("ignore", message="data discontinuity")  # harmless gaps under load


def _soundcard() -> Any:
    import soundcard as sc  # type: ignore[import-not-found]

    return sc


def loopback_microphone(sc: Any, device: str | int | None = None) -> Any:
    """The loopback capture endpoint for ``device`` (a speaker id/name) or the default output."""
    if device not in (None, ""):
        if sys.platform.startswith("linux") and not str(device).endswith(".monitor"):
            spk = sc.get_speaker(str(device))
            return sc.get_microphone(id=f"{spk.id}.monitor", include_loopback=True)
        return sc.get_microphone(id=str(device), include_loopback=True)
    spk = sc.default_speaker()
    if sys.platform.startswith("linux"):
        return sc.get_microphone(id=f"{spk.id}.monitor", include_loopback=True)
    return sc.get_microphone(id=str(spk.id), include_loopback=True)


class LoopbackSource(QueueAudioSource):
    def __init__(
        self, name: str = "system", device: str | int | None = None, clock: Clock | None = None, block_s: float = 0.1
    ) -> None:
        super().__init__(name, clock)
        self.device = device
        self.block_s = block_s
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self._label = "default output"
        self._sc = _soundcard()  # fail fast with ImportError so the factory can fall back

    def describe(self) -> str:
        return f"{self.name}: loopback of {self._label}"

    def start_capture(self) -> None:
        mic = loopback_microphone(self._sc, self.device)
        self._label = str(getattr(mic, "name", self._label))
        channels = 2 if sys.platform == "win32" else 1
        self._running.set()
        ready = threading.Event()
        errors: list[BaseException] = []

        def worker() -> None:
            if sys.platform == "win32":  # each thread needs COM; soundcard only initializes its import thread
                try:
                    import ctypes

                    ctypes.windll.ole32.CoInitializeEx(None, 0)  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001
                    pass
            frames = int(SAMPLE_RATE * self.block_s)
            try:
                with mic.recorder(samplerate=SAMPLE_RATE, channels=channels, blocksize=frames * 2) as rec:
                    ready.set()
                    while self._running.is_set():
                        data = rec.record(numframes=frames)
                        if data is not None and len(data):
                            self.push_from_thread(np.asarray(data, dtype=np.float32), SAMPLE_RATE)
            except BaseException as exc:  # surface device errors to the pipeline
                errors.append(exc)
                ready.set()
                if self._running.is_set():
                    log.error("%s: loopback capture stopped: %r", self.name, exc)
            finally:
                self.signal_end()

        self._thread = threading.Thread(target=worker, name=f"loopback-{self.name}", daemon=True)
        self._thread.start()
        ready.wait(timeout=10)
        if errors:
            raise RuntimeError(f"could not open loopback of {self._label}: {errors[0]}")
        log.info("%s: capturing loopback of %r", self.name, self._label)

    def stop_capture(self) -> None:
        self._running.clear()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
