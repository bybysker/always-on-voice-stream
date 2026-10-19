"""Private, atomic file writes for the files the app edits for you (settings, keys, preferences)."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

from ..storage import private_dir


def write_private(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` in one step, readable by you only (0600).

    Atomic so a crash or a full disk mid-write never leaves half a settings file (the next start
    would refuse it), and created 0600 from the first byte: the umask of a Finder-launched app is
    not ours to rely on, and these files can hold API keys.
    """
    if not path.parent.exists():
        private_dir(path.parent)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        with contextlib.suppress(OSError):  # e.g. a volume without Unix permissions
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
