"""One Voicestream app at a time (Windows: :mod:`.tray`).

On a Mac, macOS itself starts an app only once. On Windows each double-click on
``Voicestream.exe`` starts a new process: two would listen to the same microphone and write
two transcripts of the same meeting. So the app takes a lock first (``~/.voicestream/app.lock``,
the same OS lock the meeting folders use, see :mod:`voicestream.storage`), and writes its address
next to it (``app.json``). A second copy finds the lock taken, opens the first one's address in
the browser, and ends: double-clicking the app again just shows it.

The OS releases the lock when the process ends, even after a crash, so a stale ``app.json`` is
never followed: it is read only while the lock is held.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from pathlib import Path

from ..storage import _try_lock, _unlock

log = logging.getLogger(__name__)

LOCK_NAME = "app.lock"
INFO_NAME = "app.json"
WAIT_URL_S = 20.0
"""How long a second copy waits for the first one's address (it may be starting at the same moment)."""


def default_folder() -> Path:
    return Path.home() / ".voicestream"


class Instance:
    def __init__(self, folder: Path | None = None) -> None:
        self.folder = folder or default_folder()
        self.lock_path = self.folder / LOCK_NAME
        self.info_path = self.folder / INFO_NAME
        self._fd: int | None = None

    def acquire(self) -> bool:
        """``True``: this is the only copy (it holds the lock until :meth:`release`)."""
        self.folder.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        if not _try_lock(fd):
            os.close(fd)
            return False
        self._fd = fd
        with contextlib.suppress(OSError):  # a copy that crashed left its address: never follow it
            self.info_path.unlink()
        return True

    @property
    def held(self) -> bool:
        return self._fd is not None

    def publish(self, url: str) -> None:
        """Write this copy's address for the next one. It holds the app's key: readable by you only."""
        tmp = self.info_path.with_name(f".{INFO_NAME}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "url": url}, f)
        os.replace(tmp, self.info_path)

    def running_url(self, wait: float = WAIT_URL_S) -> str | None:
        """The address of the copy that holds the lock, waiting up to ``wait`` seconds for it to write it."""
        deadline = time.monotonic() + wait
        while True:
            with contextlib.suppress(OSError, ValueError):
                info = json.loads(self.info_path.read_text(encoding="utf-8"))
                url = info.get("url") if isinstance(info, dict) else None
                if isinstance(url, str) and url.startswith(("http://127.0.0.1:", "http://localhost:")):
                    return url
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.25)

    def release(self) -> None:
        if self._fd is None:
            return
        with contextlib.suppress(OSError):
            self.info_path.unlink()
        with contextlib.suppress(OSError):
            _unlock(self._fd)
        with contextlib.suppress(OSError):
            os.close(self._fd)
        self._fd = None
