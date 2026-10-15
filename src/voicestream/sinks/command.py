"""Bridges to anything on your computer: macOS Shortcuts, and any local command.

Both sinks hand over the same JSON a webhook gets::

    {"event": "item.created" | "item.updated" | "meeting.notes",
     "item": {...},           # Item.model_dump(mode="json"); item events only
     "item_markdown": "...",  # item events only
     "ref": "...",            # what item.created returned; item.updated only
     "meeting": {"id", "title", "started_at", "ended_at", "participants", "language", "tldr"},
     "notes_markdown": "..."} # meeting.notes only

The JSON is ASCII-escaped, so accents arrive intact whatever encoding the reader
assumes. Titles and notes are in the meeting's language. An item cancelled before
it was pushed is never sent; later changes (``item.status``, ``item.dismissed``) go
to the update shortcut or command.

``shortcuts`` runs a shortcut per event with ``shortcuts run <name> --input-path
<payload.json>``. That reaches any app with Shortcuts actions (App Intents), such
as Promethee if it has them; ``command`` covers apps with a URL scheme or a CLI. In the shortcut, use *Get Dictionary from Input* on
the Shortcut Input, then *Get Dictionary Value* for ``item`` and then ``title``. If
the shortcut ends with output (*Stop and Output*), its first line becomes the
item's ref, e.g. the new object's id or URL. The shortcut must not ask questions:
the run waits for an answer until ``timeout_s``.

Neither sink retries a failed run, a timed-out one included: it may have done its
work anyway (killing ``shortcuts`` doesn't stop the shortcut), and running it again
could create the same task twice. Push it again from the dashboard instead.

``command`` runs a program per event, never through a shell. A command is a list
of arguments, or a string split like a shell would (``shlex``). Placeholders are
filled in per argument after splitting, so meeting text can't add arguments or
run anything::

    item_command = 'open "promethee://add?title={title_url}&notes={detail_url}"'
    update_command = ["/opt/homebrew/bin/promethee", "set", "--status={status}", "--", "{ref}"]

Placeholders: ``{title}`` ``{detail}`` ``{owner}`` ``{due}`` (ISO date or empty)
``{due_text}`` ``{status}`` (open, done, cancelled or dismissed) ``{id}`` ``{ref}``
(updates only) ``{meeting_title}`` ``{json_path}`` (a file with the JSON payload,
deleted when the command exits). Each has a percent-encoded ``_url`` form, e.g.
``{title_url}``; in an argument containing ``://`` only those are accepted. For
notes, ``{id}`` is the meeting id and ``{title}`` the meeting title. The payload is
also written to the command's stdin. A value may start with ``-``, so put
placeholders after ``--`` or inside an option (``--title={title}``) if the program
takes options. A placeholder right after ``-c`` or ``-e`` (``sh -c``, ``bash -lc``,
``osascript -e``…) is refused: it would run meeting text as code. Pass it as a
separate argument instead (``sh -c 'promethee add "$1"' sh {title}``).

The first line the program prints becomes the ref (default ``cmd:<id>``). A
non-zero exit fails the push with the end of its stderr. Programs only see
``PATH``, ``HOME``, the locale and the like from voicestream's environment, plus
the variables named in ``pass_env``. If voicestream runs from launchd, ``PATH`` is
short: give programs as absolute paths. ``voicestream doctor`` checks they exist.
"""

from __future__ import annotations

import itertools
import json
import logging
import re
import shlex
import shutil
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import quote

from pydantic import Field, field_validator, model_validator

from ..models import Item, ItemStatus, Meeting
from . import _proc
from .base import Sink, SinkConfig, SinkError, item_markdown

log = logging.getLogger(__name__)

SHORTCUTS = "/usr/bin/shortcuts"
MEETING_FIELDS = {"id", "title", "started_at", "ended_at", "participants", "language", "tldr"}
FIELDS = ("title", "detail", "owner", "due", "due_text", "status", "id", "ref", "meeting_title", "json_path")
PLACEHOLDERS = {*FIELDS, *(f"{f}_url" for f in FIELDS)}
_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
_CODE_FLAG = re.compile(r"-[A-Za-z]*[ce]")
"""``-c``/``-e`` and clusters ending in them (``bash -lc``, ``perl -ne``): the next argument is code."""


def payload(event: str, meeting: Meeting, item: Item | None = None, *, ref: str | None = None, notes_markdown: str | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {"event": event}
    if item is not None:
        data["item"] = item.model_dump(mode="json")
        data["item_markdown"] = item_markdown(item, meeting)
    if ref is not None:
        data["ref"] = ref
    data["meeting"] = meeting.model_dump(mode="json", include=MEETING_FIELDS)
    if notes_markdown is not None:
        data["notes_markdown"] = notes_markdown
    return data


def _encode(data: dict[str, Any]) -> bytes:
    return json.dumps(data, separators=(",", ":")).encode("ascii")


# ================================================================== Shortcuts


class ShortcutsConfig(SinkConfig):
    item_shortcut: str | None = None
    """Runs for each new item."""
    update_shortcut: str | None = None
    """Runs when a pushed item changes (title, owner, due date, status…)."""
    notes_shortcut: str | None = None
    """Runs with the meeting notes when a meeting ends."""
    timeout_s: float = 60.0

    @model_validator(mode="after")
    def _some(self) -> ShortcutsConfig:
        if not (self.item_shortcut or self.update_shortcut or self.notes_shortcut):
            raise ValueError("set item_shortcut, update_shortcut and/or notes_shortcut")
        return self


class ShortcutsSink(Sink):
    name = "shortcuts"
    Config: ClassVar[type[SinkConfig]] = ShortcutsConfig
    cfg: ShortcutsConfig

    def __init__(self, cfg: ShortcutsConfig, app) -> None:  # type: ignore[override]
        super().__init__(cfg, app)
        self.supports_update = bool(cfg.update_shortcut)  # type: ignore[misc]
        self.supports_notes = bool(cfg.notes_shortcut)  # type: ignore[misc]
        self.run: _proc.Runner = _proc.run

    def accepts(self, item: Item, default_min_conf: float) -> bool:
        return bool(self.cfg.item_shortcut) and item.status != ItemStatus.CANCELLED and super().accepts(item, default_min_conf)

    async def _shortcut(self, name: str, data: dict[str, Any]) -> str:
        """Run shortcut ``name`` on ``data``; return the first line of its output. Never retried: a
        shortcut does what it likes (create a task…), and one that timed out may still finish."""
        _proc.require_macos("Shortcuts")
        with tempfile.TemporaryDirectory(prefix="voicestream-") as tmp:
            src, out = Path(tmp) / "payload.json", Path(tmp) / "output.txt"
            src.write_bytes(_encode(data))
            try:
                res = await self.run(
                    [SHORTCUTS, "run", name, "--input-path", str(src), "--output-path", str(out), "--output-type", "public.plain-text"],
                    timeout=self.cfg.timeout_s,
                )
            except SinkError as exc:
                raise _proc.not_retried(exc) from None
            output = out.read_text(encoding="utf-8", errors="replace") if out.exists() else ""
        if res.returncode != 0:
            raise SinkError(f"shortcut “{name}” failed (exit {res.returncode}): {_proc.tail(res.stderr)}", retryable=False)
        return _proc.first_line(output)

    # -------------------------------------------------------------- sink API

    async def push_item(self, item: Item, meeting: Meeting) -> str:
        name = self.cfg.item_shortcut
        if not name:
            raise SinkError("shortcuts: no item_shortcut configured", retryable=False)
        return await self._shortcut(name, payload("item.created", meeting, item)) or f"shortcut:{name}:{item.id}"

    async def update_item(self, item: Item, meeting: Meeting, ref: str) -> None:
        if self.cfg.update_shortcut:
            await self._shortcut(self.cfg.update_shortcut, payload("item.updated", meeting, item, ref=ref))

    async def push_notes(self, meeting: Meeting, notes_markdown: str) -> str | None:
        name = self.cfg.notes_shortcut
        if not name:
            return None
        data = payload("meeting.notes", meeting, notes_markdown=notes_markdown)
        return await self._shortcut(name, data) or f"shortcut:{name}:{meeting.id}"

    async def check(self) -> str:
        _proc.require_macos("Shortcuts")
        res = await self.run([SHORTCUTS, "list"], timeout=self.cfg.timeout_s)
        if res.returncode != 0:
            raise SinkError(f"`shortcuts list` failed (exit {res.returncode}): {_proc.tail(res.stderr)}", retryable=False)
        have = {_nfc(line.strip()) for line in res.stdout.splitlines()}
        wanted = {
            event: name
            for event, name in (
                ("new items", self.cfg.item_shortcut),
                ("updates", self.cfg.update_shortcut),
                ("notes", self.cfg.notes_shortcut),
            )
            if name
        }
        if missing := sorted({n for n in wanted.values() if _nfc(n) not in have}):
            raise SinkError(f"no shortcut named {', '.join(f'“{n}”' for n in missing)} (see `shortcuts list`)", retryable=False)
        return " · ".join(f"“{name}” for {event}" for event, name in wanted.items())


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


# ==================================================================== command


class CommandConfig(SinkConfig):
    item_command: list[str] | None = None
    """Runs for each new item."""
    update_command: list[str] | None = None
    """Runs when a pushed item changes (title, owner, due date, status…)."""
    notes_command: list[str] | None = None
    """Runs with the meeting notes when a meeting ends."""
    timeout_s: float = 30.0
    pass_env: list[str] = Field(default_factory=list)
    """Environment variables to pass on (by default your API keys and tokens aren't)."""

    @field_validator("item_command", "update_command", "notes_command", mode="before")
    @classmethod
    def _split(cls, value: Any) -> Any:
        return shlex.split(value) if isinstance(value, str) else value

    @field_validator("item_command", "update_command", "notes_command")
    @classmethod
    def _safe(cls, argv: list[str] | None) -> list[str] | None:
        if argv is None:
            return None
        if not argv or not argv[0]:
            raise ValueError("command is empty")
        if _PLACEHOLDER.search(argv[0]):
            raise ValueError("the program (first word) can't contain placeholders")
        for prev, arg in itertools.pairwise(argv):
            names = set(_PLACEHOLDER.findall(arg))
            if unknown := names - PLACEHOLDERS:
                raise ValueError(f"unknown placeholder(s) {', '.join(sorted(unknown))}; known: {', '.join(FIELDS)} (+ _url forms)")
            meeting_text = names - {"json_path", "json_path_url"}
            if meeting_text and _CODE_FLAG.fullmatch(prev):
                raise ValueError(f"{arg!r} after {prev} would run meeting text as code; pass placeholders as separate arguments")
            if "://" in arg and any(not n.endswith("_url") for n in meeting_text):
                raise ValueError(f"{arg!r}: inside a URL use the _url form of placeholders, e.g. {{title_url}}")
        return argv

    @model_validator(mode="after")
    def _some(self) -> CommandConfig:
        if not (self.item_command or self.update_command or self.notes_command):
            raise ValueError("set item_command, update_command and/or notes_command")
        return self


class CommandSink(Sink):
    name = "command"
    Config: ClassVar[type[SinkConfig]] = CommandConfig
    cfg: CommandConfig

    def __init__(self, cfg: CommandConfig, app) -> None:  # type: ignore[override]
        super().__init__(cfg, app)
        self.supports_update = bool(cfg.update_command)  # type: ignore[misc]
        self.supports_notes = bool(cfg.notes_command)  # type: ignore[misc]
        self.run: _proc.Runner = _proc.run

    def accepts(self, item: Item, default_min_conf: float) -> bool:
        return bool(self.cfg.item_command) and item.status != ItemStatus.CANCELLED and super().accepts(item, default_min_conf)

    async def _exec(self, argv: list[str], data: dict[str, Any], values: dict[str, str]) -> str:
        """Run ``argv`` with placeholders filled in; return the first line it prints. Never retried:
        a command that timed out may have done its work."""
        body = _encode(data)
        with tempfile.TemporaryDirectory(prefix="voicestream-") as tmp:
            path = Path(tmp) / "payload.json"
            path.write_bytes(body)
            filled = fill_values(values | {"json_path": str(path)})
            try:
                res = await self.run(
                    [_PLACEHOLDER.sub(lambda m: filled[m.group(1)], arg) for arg in argv],
                    stdin=body,
                    env=_proc.child_env(self.cfg.pass_env),
                    timeout=self.cfg.timeout_s,
                )
            except SinkError as exc:
                raise _proc.not_retried(exc) from None
        if res.returncode != 0:
            raise SinkError(f"{Path(argv[0]).name} exited with {res.returncode}: {_proc.tail(res.stderr)}", retryable=False)
        return _proc.first_line(res.stdout)

    # -------------------------------------------------------------- sink API

    async def push_item(self, item: Item, meeting: Meeting) -> str:
        if not self.cfg.item_command:
            raise SinkError("command: no item_command configured", retryable=False)
        data = payload("item.created", meeting, item)
        return await self._exec(self.cfg.item_command, data, item_values(item, meeting)) or f"cmd:{item.id}"

    async def update_item(self, item: Item, meeting: Meeting, ref: str) -> None:
        if self.cfg.update_command:
            data = payload("item.updated", meeting, item, ref=ref)
            await self._exec(self.cfg.update_command, data, item_values(item, meeting, ref))

    async def push_notes(self, meeting: Meeting, notes_markdown: str) -> str | None:
        if not self.cfg.notes_command:
            return None
        data = payload("meeting.notes", meeting, notes_markdown=notes_markdown)
        values = dict.fromkeys(FIELDS, "") | {"id": meeting.id, "title": meeting.title or "", "meeting_title": meeting.title or ""}
        return await self._exec(self.cfg.notes_command, data, values) or f"cmd:{meeting.id}"

    async def check(self) -> str:
        path = _proc.child_env(self.cfg.pass_env).get("PATH")
        lines = []
        for event, argv in (("new items", self.cfg.item_command), ("updates", self.cfg.update_command), ("notes", self.cfg.notes_command)):
            if not argv:
                continue
            if not shutil.which(argv[0], path=path):
                raise SinkError(f"{argv[0]!r} (for {event}) isn't an executable on PATH; use an absolute path", retryable=False)
            lines.append(f"{Path(argv[0]).name} for {event}")
        return " · ".join(lines) + " (not run)"


def item_values(item: Item, meeting: Meeting, ref: str = "") -> dict[str, str]:
    return {
        "title": item.title,
        "detail": item.detail,
        "owner": item.owner or "",
        "due": item.due_date or "",
        "due_text": item.due_text or "",
        "status": "dismissed" if item.dismissed else item.status.value,
        "id": item.id,
        "ref": ref,
        "meeting_title": meeting.title or "",
    }


def fill_values(values: dict[str, str]) -> dict[str, str]:
    """Every placeholder's text: NULs dropped (they can't be in an argument), plus ``_url`` forms."""
    clean = {k: v.replace("\x00", "") for k, v in values.items()}
    return clean | {f"{k}_url": quote(v, safe="") for k, v in clean.items()}
