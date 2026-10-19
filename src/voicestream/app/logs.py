"""App-mode logging: a rotating log file, and warnings mirrored to the UI.

A Finder-launched app has no terminal, so whatever voicestream logs would be lost:
``<data_dir>/logs/voicestream.log`` keeps it (5 MB × 3, private like the rest of the data
folder; Settings → Advanced → "Show log" reveals it). WARNING and worse from voicestream's own
loggers — a sink that was skipped, Claude unavailable, a capture that keeps failing — are also
handed to the host, which shows them in the app (``app.warning`` events, ``warnings`` in
``/api/app``), at most once a second per message.
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import threading
import time
from collections.abc import Callable
from pathlib import Path

from ..storage import private_dir

LOG_NAME = "voicestream.log"
MAX_BYTES = 5 * 1024 * 1024
BACKUPS = 3
MIRROR_INTERVAL_S = 1.0
NOISY = ("httpx", "httpcore", "httpx2", "websockets", "uvicorn", "uvicorn.error", "uvicorn.access", "anthropic")


def log_path(data_dir: Path) -> Path:
    return data_dir / "logs" / LOG_NAME


def install_file_log(path: Path) -> logging.Handler:
    """Log INFO and up from every logger to ``path`` (rotating); returns the handler to remove later."""
    private_dir(path.parent)
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8", delay=True)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.setLevel(logging.INFO)
    root = logging.getLogger()
    root.addHandler(handler)
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
    for name in NOISY:  # one line per HTTP request would bury everything else
        logging.getLogger(name).setLevel(logging.WARNING)
    return handler


class WarningMirror(logging.Handler):
    """Hands WARNING+ records of the ``voicestream`` loggers to ``deliver(message)`` on the host's loop.

    Records can come from any thread (audio callbacks, ``asyncio.to_thread``), and the event bus
    is not thread-safe, so delivery always goes through ``loop.call_soon_threadsafe`` from other
    threads. The same message is delivered at most once per :data:`MIRROR_INTERVAL_S`.
    """

    def __init__(self, deliver: Callable[[str], None], loop: asyncio.AbstractEventLoop) -> None:
        super().__init__(logging.WARNING)
        self.deliver = deliver
        self.loop = loop
        self._loop_thread = threading.get_ident()
        self._last: dict[str, float] = {}
        self._busy = threading.local()

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self._busy, "on", False):
            return  # a warning logged while delivering one: don't loop
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a bad format string must not break logging
            return
        now = time.monotonic()
        if now - self._last.get(message, float("-inf")) < MIRROR_INTERVAL_S:
            return
        if len(self._last) > 500:
            self._last.clear()
        self._last[message] = now
        if threading.get_ident() == self._loop_thread:
            self._deliver(message)
        elif not self.loop.is_closed():
            try:
                self.loop.call_soon_threadsafe(self._deliver, message)
            except RuntimeError:  # the loop closed meanwhile
                pass

    def _deliver(self, message: str) -> None:
        self._busy.on = True
        try:
            self.deliver(message)
        except Exception:  # noqa: BLE001 - never let the UI mirror break the code that logged
            pass
        finally:
            self._busy.on = False
