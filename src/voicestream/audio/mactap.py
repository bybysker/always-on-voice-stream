"""macOS 14.2+ system audio via Core Audio process taps (the ``catap`` package),
so no BlackHole/Multi-Output setup is needed.

The first run triggers the "System Audio Recording" permission for your
terminal app; until it is granted (and the terminal restarted) the tap delivers
only silence, which we detect and report.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

from .base import Clock, QueueAudioSource

log = logging.getLogger(__name__)


def available() -> bool:
    try:
        import catap  # type: ignore[import-not-found]  # noqa: F401
    except Exception:  # ImportError on Linux/Windows/older macOS
        return False
    return True


class TapSource(QueueAudioSource):
    """Records the system output mix (optionally excluding some apps, e.g. music players)."""

    def __init__(self, name: str = "system", clock: Clock | None = None, exclude: list[str] | None = None) -> None:
        super().__init__(name, clock)
        self.exclude = exclude or []
        self._session: Any = None

    def describe(self) -> str:
        return f"{self.name}: macOS system audio tap"

    def _on_buffer(self, buf: Any) -> None:
        fmt = buf.format
        dtype = np.float32 if getattr(fmt, "is_float", True) else np.int16
        x = np.frombuffer(buf.data, dtype=dtype)
        channels = int(getattr(fmt, "num_channels", 1) or 1)
        if channels > 1:
            x = x[: len(x) - len(x) % channels].reshape(-1, channels)
        self.push_from_thread(x.copy(), int(fmt.sample_rate))

    def start_capture(self) -> None:
        from catap import record_system_audio  # type: ignore[import-not-found]

        self._session = record_system_audio(
            None, on_buffer=self._on_buffer, mono=True, exclude=self.exclude, max_pending_buffers=256
        )
        self._session.start()
        log.info("%s: recording the macOS system audio tap", self.name)

    def stop_capture(self) -> None:
        session, self._session = self._session, None
        if session is None:
            return
        try:
            session.stop()
        except Exception:  # noqa: BLE001
            log.debug("tap stop failed", exc_info=True)
        if getattr(session, "captured_only_silence", False):
            log.warning(
                "the system audio tap only captured silence: grant your terminal 'System Audio Recording' "
                "permission (System Settings → Privacy & Security) and restart it"
            )
