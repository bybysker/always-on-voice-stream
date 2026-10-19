"""Runs local programs for the sinks that drive macOS apps and your own commands.

Programs always get an argv list, never a shell, so text that came out of a meeting
can't turn into a command line. They also get a trimmed environment, so API keys
stay inside voicestream. Each sink calls its ``run`` attribute, which tests replace
with a fake.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .base import SinkError

BASE_ENV = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "TZ",
    # Windows: without these many programs can't even start (no secrets in them)
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP",
    "USERPROFILE", "USERNAME", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
)  # fmt: skip
"""Environment variables every program gets (plus ``LC_*``)."""


@dataclass(frozen=True, slots=True)
class Result:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[..., Awaitable[Result]]
"""``run``'s signature: ``(argv, *, timeout, stdin=None, env=None) -> Result``."""


def child_env(extra: Iterable[str] = ()) -> dict[str, str]:
    keep = {*BASE_ENV, *extra}
    return {k: v for k, v in os.environ.items() if k in keep or k.startswith("LC_")}


async def run(
    argv: Sequence[str],
    *,
    timeout: float,
    stdin: bytes | None = None,
    env: Mapping[str, str] | None = None,
) -> Result:
    """Run ``argv`` to completion and capture its output (decoded as UTF-8).

    Raises :class:`SinkError`: retryable when it times out (the program is killed),
    not when it can't be started. A non-zero exit is left to the caller.
    """
    name = Path(argv[0]).name
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=dict(env) if env is not None else child_env(),
        )
    except (OSError, ValueError) as exc:
        raise SinkError(f"can't run {name}: {exc}", retryable=False) from exc
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
    except TimeoutError:
        raise SinkError(f"{name} timed out after {timeout:g}s", retryable=True) from None
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    assert proc.returncode is not None
    return Result(proc.returncode, _text(out), _text(err))


def _text(data: bytes) -> str:
    return data.decode("utf-8", "replace").replace("\r\n", "\n")  # Windows line endings


def not_retried(exc: SinkError) -> SinkError:
    """``exc`` for an action that may have gone through before it failed (a timeout, an app that
    stopped answering): killing ``shortcuts`` doesn't stop the shortcut, and the dispatcher's retry
    of a create would make a duplicate. A failure that can't have done anything is returned as is."""
    if not exc.retryable:
        return exc
    return SinkError(
        f"{exc}. It may have gone through anyway, so it isn't retried: check, then push it again from the dashboard if it's missing",
        retryable=False,
        uncertain=True,
    )


def require_macos(what: str) -> None:
    if sys.platform != "darwin":
        raise SinkError(f"{what} needs macOS", retryable=False)


def first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def tail(text: str, limit: int = 400) -> str:
    text = text.strip()
    return text if len(text) <= limit else "…" + text[-limit:]
