"""App-only preferences (``~/.voicestream/app.json``): things voicestream.toml has no business holding.

Whether the first-run setup is done, how meetings start (when you say so, or whenever someone
speaks), whether to start listening when the app opens, launch at login, the UI language, the
keyboard shortcut, the floating button, the Spotlight launcher and the window geometry belong to the
Mac app, not to the assistant: the command line never reads them, and a config file you share or
keep in git shouldn't carry them.

The file also keeps the key a ``voicestream://`` link needs to start a recording (see
:meth:`voicestream.app.host.AppHost.open_url`). It is written 0600 like the rest and left out of
every dump of the preferences (the page's state, the replies to ``PATCH /api/prefs``, the log): only
this file and the Spotlight launcher made from it ever hold it.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal

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

MeetingStart = Literal["manual", "automatic"]
"""How meetings start: ``manual``, when you start one (the window, the menu bar, the shortcut, the
floating button, Spotlight), with nothing captured until then; or ``automatic``, always listening, a
meeting opening on the first speech and closing after a silence (``always_on.idle_split_minutes``)."""
MAX_SILENCE_MINUTES = 600
"""The silence thresholds of a meeting started by hand go up to 10 hours."""
URL_KEY = re.compile(r"[A-Za-z0-9_-]{32,128}")
Minutes = Annotated[int, Field(ge=0, le=MAX_SILENCE_MINUTES, strict=True)]
"""Whole minutes, ``0``: never. Strict where the page sends them: ``true`` is not one minute."""


def new_url_key() -> str:
    return secrets.token_urlsafe(32)


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


class FloatingPosition(BaseModel):
    """Where the floating button was left: its frame's origin in screen points (as AppKit counts them,
    from the bottom left of the main screen), and the screen it was on, so that the native shell can
    put it back there, or on the main screen when that one isn't connected any more."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    x: float = Field(ge=-100_000, le=100_000)
    y: float = Field(ge=-100_000, le=100_000)
    screen: str | None = Field(None, max_length=200)
    """The screen's name or id, as the native shell chose to record it (``None``: the main screen)."""


class Prefs(BaseModel):
    model_config = ConfigDict(extra="ignore")  # a newer app's keys must not break an older one

    setup_done: bool = False
    """The first-run wizard was completed (until then the app opens on it and doesn't listen)."""
    listen_on_launch: bool = True
    """Start listening as soon as the app opens (once setup is done), when meetings start automatically."""
    start_at_login: bool = False
    ui_language: Literal["auto", "en", "fr"] = "auto"
    """``auto`` follows the system language."""
    window: dict[str, Any] | None = None
    """Last window frame (x, y, width, height), restored by the native shell."""
    hotkey: Hotkey | None = Field(default_factory=Hotkey)
    """The shortcut that starts and stops listening; ``None``: turned off."""
    hotkey_sound: bool = True
    """A short sound when the shortcut starts or stops listening (you may not be looking)."""
    meeting_start: MeetingStart = "manual"
    """How meetings start (:data:`MeetingStart`): ``manual`` also for a file saved before there was a
    choice, so Voicestream records only when you ask it to. :attr:`listen_on_launch` is for ``automatic``."""
    floating_button: bool = True
    """The small always-on-top button that starts, pauses and stops a meeting from any app."""
    floating_position: FloatingPosition | None = None
    """Where you last dragged the floating button (``None``: its default place)."""
    spotlight_launcher: bool = True
    """Keep a small "Voicestream – New Meeting" app in ~/Applications, so that ⌘Space, "voicestream",
    Return starts a meeting (the native shell writes it while this is on and removes it when it's off)."""
    call_suggestions: bool = True
    """When another app starts using the microphone (a call) and nothing is being recorded, suggest
    taking notes. Only ever a suggestion: nothing starts by itself."""
    silence_ask_minutes: int = Field(15, ge=0, le=MAX_SILENCE_MINUTES)
    """A meeting started by hand with no speech for this long asks "Still in a meeting?" (``0``: never)."""
    silence_end_minutes: int = Field(60, ge=0, le=MAX_SILENCE_MINUTES)
    """… and ends by itself after this long without speech, saying why (``0``: never). Time asleep
    counts, time on Pause doesn't, and answering "Keep" starts both counts over."""
    url_key: str = Field("", exclude=True, repr=False)
    """What a ``voicestream://meeting/start`` link must carry (``?key=``) to start a recording, so that
    another app or a web page can't. Made once (:func:`with_url_key`) and kept in this file only: left
    out of every dump and repr, :func:`save_prefs` writes it on purpose."""

    @field_validator("meeting_start", "floating_button", "floating_position", "spotlight_launcher", "call_suggestions",
                     "silence_ask_minutes", "silence_end_minutes", mode="wrap")
    @classmethod
    def _saved_or_default(cls, value: Any, handler: ValidatorFunctionWrapHandler, info: ValidationInfo) -> Any:
        """A saved value this version refuses (a newer app's, a hand edit) is the default again, rather
        than the whole file being ignored: the setup would start over."""
        try:
            return handler(value)
        except ValidationError as exc:
            name = str(info.field_name)
            default = cls.model_fields[name].get_default(call_default_factory=True)
            log.warning("ignoring the saved %s (%s): back to %r", name, exc.errors()[0]["msg"], default)
            return default

    @field_validator("url_key", mode="before")
    @classmethod
    def _saved_url_key(cls, value: Any) -> str:
        """Anything but a key :func:`new_url_key` could have made (none yet, a hand edit that shortened
        it) is no key: a new one is made when one is needed. Never logged."""
        return value if isinstance(value, str) and URL_KEY.fullmatch(value) else ""

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
    """What ``PATCH /api/prefs`` may change (and the native shell, through the host)."""

    model_config = ConfigDict(extra="forbid")

    listen_on_launch: bool | None = None
    start_at_login: bool | None = None
    ui_language: Literal["auto", "en", "fr"] | None = None
    window: dict[str, Any] | None = None
    hotkey: Hotkey | None = None
    """``null`` turns the shortcut off."""
    hotkey_sound: bool | None = None
    meeting_start: MeetingStart | None = None
    floating_button: bool | None = None
    floating_position: FloatingPosition | None = None
    """``null`` forgets it: the floating button goes back to its default place."""
    spotlight_launcher: bool | None = None
    call_suggestions: bool | None = None
    silence_ask_minutes: Minutes | None = None
    silence_end_minutes: Minutes | None = None


NULLABLE = ("window", "hotkey", "floating_position")
"""The preferences whose ``null`` means something (none, turned off, forgotten), not "unchanged"."""


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
    data = prefs.model_dump(mode="json")
    if prefs.url_key:
        data["url_key"] = prefs.url_key  # left out of every dump: only this file holds it
    write_private(path or prefs_path(), json.dumps(data, indent=1, ensure_ascii=False) + "\n")


def with_url_key(prefs: Prefs, path: Path | None = None) -> Prefs:
    """``prefs`` with a link key: made and saved the first time one is needed, the same one after."""
    if prefs.url_key:
        return prefs
    prefs = prefs.model_copy(update={"url_key": new_url_key()})
    save_prefs(prefs, path)
    return prefs


def update_prefs(changes: dict[str, Any], path: Path | None = None, layout: Mapping[str, str] | None = None) -> Prefs:
    """Validate ``changes`` (raises pydantic ``ValidationError``), apply them and save. ``layout``: what the
    keys type on this keyboard, for a shortcut sent without its label (see :class:`Hotkey`)."""
    context = layout_context(layout)
    upd = PrefsUpdate.model_validate(changes, context=context).model_dump(exclude_unset=True)
    keep = {k: v for k, v in upd.items() if v is not None or k in NULLABLE}  # null: none, off, forgotten
    saved = load_prefs(path)
    # validated again (a Hotkey, not a dict); the link key isn't in the dump, so it is carried over
    prefs = Prefs.model_validate(saved.model_dump() | keep | {"url_key": saved.url_key}, context=context)
    save_prefs(prefs, path)
    return prefs
