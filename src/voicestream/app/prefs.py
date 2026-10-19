"""App-only preferences (``~/.voicestream/app.json``): things voicestream.toml has no business holding.

Whether the first-run setup is done, whether to start listening when the app opens, launch at
login, the UI language, the keyboard shortcut and the window geometry belong to the Mac app, not to
the assistant: the command line never reads them, and a config file you share or keep in git
shouldn't carry them.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    ValidatorFunctionWrapHandler,
    field_validator,
    model_validator,
)

from . import hotkey as hk
from .files import write_private

log = logging.getLogger(__name__)


class Hotkey(BaseModel):
    """The keyboard shortcut that starts and stops listening from any app (see :mod:`.hotkey`)."""

    model_config = ConfigDict(extra="forbid")

    code: str = hk.DEFAULT_CODE
    """The physical key, as ``KeyboardEvent.code`` names it (``"KeyR"``): the same place whatever the layout."""
    mods: list[Literal["ctrl", "alt", "shift", "cmd"]] = Field(default_factory=lambda: list(hk.DEFAULT_MODS))
    key: str = Field("", max_length=16)
    """What is printed on that key on this keyboard (``KeyboardEvent.key``), only ever shown; when the page
    couldn't tell, what the key types on the current layout (the validation context's ``layout``), else
    what a US keyboard prints there."""

    @model_validator(mode="after")
    def _usable(self, info: ValidationInfo) -> Hotkey:
        """Refused as the Settings page refuses it: a key with nothing held, or one apps or macOS use.

        With ⌥ held a Mac gives the page the character ⌥ types, so it may send no ``key``: the one this
        keyboard's layout has for the code (:func:`.hotkey.current_layout`, from the native shell, passed
        as ``context={"layout": …}``) is then what is checked and shown, as the page does."""
        self.mods = hk.normalize_mods(self.mods)  # type: ignore[assignment]
        key = self.key or layout_key(info.context, self.code)
        reason = hk.problem(self.code, self.mods, key)
        if reason is not None:
            raise ValueError(hk.explain(reason, self.code, self.mods, key))
        self.key = hk.label(self.code, key)
        return self


def layout_key(context: Any, code: str) -> str:
    """What ``code`` types on the keyboard layout a validation ``context`` names (``""``: none)."""
    layout = context.get("layout") if isinstance(context, Mapping) else None
    char = layout.get(code) if isinstance(layout, Mapping) else None
    return char if isinstance(char, str) else ""


def layout_context(layout: Mapping[str, str] | None) -> dict[str, Any] | None:
    """The validation context for a :class:`Hotkey` recorded on a keyboard with this layout."""
    return {"layout": dict(layout)} if layout else None


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
    hotkey: Hotkey | None = Field(default_factory=Hotkey)
    """The shortcut that starts and stops listening; ``None``: turned off."""
    hotkey_sound: bool = True
    """A short sound when the shortcut starts or stops listening (you may not be looking)."""

    @field_validator("hotkey", mode="wrap")
    @classmethod
    def _saved_hotkey(cls, value: Any, handler: ValidatorFunctionWrapHandler) -> Hotkey | None:
        """A saved shortcut this version refuses (a newer app's, a hand edit) is the default again,
        rather than the whole file being ignored: the setup would start over."""
        try:
            return handler(value)
        except ValidationError as exc:
            log.warning("ignoring the saved keyboard shortcut (%s): back to %s", exc.errors()[0]["msg"],
                        hk.describe(hk.DEFAULT_CODE, hk.DEFAULT_MODS))
            return Hotkey()


class PrefsUpdate(BaseModel):
    """What ``PATCH /api/prefs`` may change."""

    model_config = ConfigDict(extra="forbid")

    listen_on_launch: bool | None = None
    start_at_login: bool | None = None
    ui_language: Literal["auto", "en", "fr"] | None = None
    window: dict[str, Any] | None = None
    hotkey: Hotkey | None = None
    """``null`` turns the shortcut off."""
    hotkey_sound: bool | None = None


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


def update_prefs(changes: dict[str, Any], path: Path | None = None, layout: Mapping[str, str] | None = None) -> Prefs:
    """Validate ``changes`` (raises pydantic ``ValidationError``), apply them and save. ``layout``: what the
    keys type on this keyboard, for a shortcut sent without its label (see :class:`Hotkey`)."""
    context = layout_context(layout)
    upd = PrefsUpdate.model_validate(changes, context=context).model_dump(exclude_unset=True)
    keep = {k: v for k, v in upd.items() if v is not None or k in ("window", "hotkey")}  # null: none, off
    prefs = Prefs.model_validate(load_prefs(path).model_dump() | keep, context=context)  # again: a Hotkey, not a dict
    save_prefs(prefs, path)
    return prefs
