"""Private, atomic file writes for the files the app edits for you (settings, keys, preferences)."""

from __future__ import annotations

import contextlib
import os
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path

from ..storage import private_dir, replace_file

_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


@contextlib.contextmanager
def editing(path: Path) -> Iterator[None]:
    """Hold this around a read-modify-write of ``path``.

    The app's routes run file edits in worker threads, so two requests at once (two settings saves,
    two keys typed quickly) would each read the file and write back their own version: the first
    change would be lost. One lock per file, re-entrant so a helper may take it again.
    """
    key = os.path.abspath(path)
    with _locks_guard:
        lock = _locks.setdefault(key, threading.RLock())
    with lock:
        yield


def write_private(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` in one step, readable by you only (0600).

    Atomic so a crash or a full disk mid-write never leaves half a settings file (the next start
    would refuse it), and created 0600 from the first byte: the umask of a Finder-launched app is
    not ours to rely on, and these files can hold API keys. The temporary file gets a fresh random
    name (``mkstemp``: ``O_EXCL``, never follows a symlink), so two writers can't truncate each
    other's text, and nobody can plant a link at a predictable name to have us overwrite its target.
    """
    if not path.parent.exists():
        private_dir(path.parent)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        with contextlib.suppress(OSError):  # e.g. a volume without Unix permissions
            os.chmod(tmp, 0o600)
        replace_file(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
