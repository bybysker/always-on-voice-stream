"""App-only preferences (``~/.voicestream/app.json``): things voicestream.toml has no business holding.

Whether the first-run setup is done, whether to start listening when the app opens, launch at
login, the UI language and the window geometry belong to the Mac app, not to the assistant: the
command line never reads them, and a config file you share or keep in git shouldn't carry them.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from .files import write_private

log = logging.getLogger(__name__)


class Prefs(BaseModel):
    model_config = ConfigDict(extra="ignore")  # a newer app's keys must not break an older one

    setup_done: bool = False
    """The first-run wizard was completed (until then the app opens on it and doesn't listen)."""
    listen_on_launch: bool = True
    """Start listening as soon as the app opens (once setup is done)."""
    start_at_login: bool = False
    ui_language: Literal["auto", "en", "fr"] = "auto"
    """``auto`` follows the system language."""
    window: dict[str, Any] | None = None
    """Last window frame (x, y, width, height), restored by the native shell."""


class PrefsUpdate(BaseModel):
    """What ``PATCH /api/prefs`` may change."""

    model_config = ConfigDict(extra="forbid")

    listen_on_launch: bool | None = None
    start_at_login: bool | None = None
    ui_language: Literal["auto", "en", "fr"] | None = None
    window: dict[str, Any] | None = None


def prefs_path() -> Path:
    return Path("~/.voicestream/app.json").expanduser()


def load_prefs(path: Path | None = None) -> Prefs:
    """The saved preferences; defaults when there are none or the file is unreadable (a broken
    preferences file must never keep the app from opening)."""
    p = path or prefs_path()
    try:
        return Prefs.model_validate(json.loads(p.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return Prefs()
    except (OSError, ValueError, ValidationError) as exc:
        log.warning("ignoring unreadable app preferences %s: %s", p, exc)
        return Prefs()


def save_prefs(prefs: Prefs, path: Path | None = None) -> None:
    write_private(path or prefs_path(), prefs.model_dump_json(indent=1) + "\n")


def update_prefs(changes: dict[str, Any], path: Path | None = None) -> Prefs:
    """Validate ``changes`` (raises pydantic ``ValidationError``), apply them and save."""
    upd = PrefsUpdate.model_validate(changes).model_dump(exclude_unset=True)
    prefs = load_prefs(path).model_copy(update={k: v for k, v in upd.items() if v is not None or k == "window"})
    save_prefs(prefs, path)
    return prefs
