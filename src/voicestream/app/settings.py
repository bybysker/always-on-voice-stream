"""Reading and writing ``voicestream.toml`` from the app's Settings screen.

Changes arrive as dotted paths (``{"transcription.backend": "whisper-cpp", "sinks.reminders.enabled":
true}``; ``null`` removes the setting, i.e. back to its default). They are applied to the file with
``tomlkit``, which keeps your comments and layout, then the *whole* result is validated — the
config and every enabled sink's own settings — before anything is written. A change that doesn't
validate is refused with the field it is about, and the file is left exactly as it was.

Writes are atomic and 0600. With no file yet, the first save starts from the commented template
``voicestream init`` writes, so the file stays readable by hand.

Some settings decide what Voicestream *runs* or where it sends your keys and meetings. The app's
API is protected by a token, but a token can leak (a bug in the page, a pasted URL), so:

* the ones no app screen sets and that only matter for running programs or exposing keys
  (:data:`LOCKED`: the whisper-server path, keys passed to a command, agents' Bash and tools…) are
  refused over HTTP — edit ``voicestream.toml`` by hand (putting one back to its default is fine);
* the others (:data:`REVIEW`: the command a tool runs, a server address, which variable holds a
  tool's key, where files go…) are saved, but listed back to you (``review`` in the answer and in
  the ``settings.changed`` event) and logged as a warning, which the app shows in its banner.
"""

from __future__ import annotations

import fnmatch
import json
import math
import os
import re
import shlex
import tomllib
from collections.abc import Collection
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from pydantic import ValidationError

from ..config import Config, load_config, load_dotenv, load_keychain
from .files import editing, write_private

if TYPE_CHECKING:
    import tomlkit.container

_KEY = re.compile(r"[A-Za-z0-9_-]+")

SINK_LABELS = {
    "markdown": "Local files (TODO.md and notes)",
    "reminders": "Apple Reminders",
    "google": "Google (Calendar, Docs, Gmail)",
    "hubspot": "HubSpot",
    "shortcuts": "Shortcuts (e.g. Promethee)",
    "command": "A command or app link",
    "webhook": "Webhook (Zapier, Make, n8n…)",
    "notion": "Notion",
    "monday": "monday.com",
    "linear": "Linear",
    "github": "GitHub Issues",
    "slack": "Slack",
    "todoist": "Todoist",
}
MAC_ONLY_SINKS = frozenset({"reminders", "shortcuts"})

LOCKED = (
    "transcription.whisper_cpp.binary",  # a program run with your audio
    "agents.allow_bash",  # agents act on what is said in meetings
    "agents.allowed_tools",
    "agents.workspace_root",
    "dashboard.host",  # `voicestream run --dashboard` on every network interface
    "sinks.*.pass_env",  # your keys handed to a command
)
"""Refused by :func:`save_checked` (the app's PATCH /api/settings) unless set back to the default."""
REVIEW = (
    "data_dir",
    "transcription.*url",
    "agents.mode",
    "agents.engine",
    "sinks.*.*_command",
    "sinks.*.*_shortcut",
    "sinks.*.*_env",
    "sinks.*.*url",
    "sinks.*.*_file",
    "sinks.*.*_dir",
)
"""Saved, but listed back and logged: what Voicestream runs, and where it sends or writes things."""


class SettingsError(ValueError):
    """A change that can't be applied; ``field`` is the dotted setting it is about (``None``: the file)."""

    def __init__(self, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


def default_config_path() -> Path:
    """The app's config file: ``$VOICESTREAM_CONFIG``, else ``~/.voicestream/voicestream.toml`` (never
    one in the working directory: a Finder-launched app's is ``/``)."""
    env = os.environ.get("VOICESTREAM_CONFIG")
    return Path(env).expanduser() if env else Path("~/.voicestream/voicestream.toml").expanduser()


def load_app_config(path: Path) -> Config:
    """The config at ``path``, or the defaults while there is no file yet (the app must open before
    anything is set up). Keys are loaded into the environment either way. Raises ``ConfigError``."""
    if path.is_file():
        return load_config(path)
    cfg = Config()
    load_keychain()
    for env in (path.parent / ".env", cfg.data_path / ".env", Path("~/.voicestream/.env").expanduser()):
        load_dotenv(env)
    return cfg


# ----------------------------------------------------------------------- editing


def parse_path(dotted: str) -> list[str]:
    keys = dotted.split(".") if isinstance(dotted, str) else []
    if not keys or not all(_KEY.fullmatch(k) for k in keys):
        raise SettingsError(f"not a setting name: {dotted!r}", field=str(dotted))
    return keys


def load_document(path: Path) -> tomlkit.TOMLDocument:
    """The file as an editable document, or the template while there is none."""
    import tomlkit

    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = resources.files("voicestream").joinpath("config.example.toml").read_text(encoding="utf-8")
    try:
        return tomlkit.parse(text)
    except Exception as exc:  # tomlkit's ParseError and friends
        raise SettingsError(f"{path} can't be read as TOML: {exc} — fix or remove it", field=None) from None


def _table(parent: tomlkit.container.Container | Any, key: str, dotted: str, created: list[Any]) -> Any:
    import tomlkit
    from tomlkit.items import InlineTable, Table

    current = parent.get(key)
    if current is None:
        parent[key] = tomlkit.table()
        created.append(parent[key])
        return parent[key]
    if not isinstance(current, (Table, InlineTable, dict)):
        raise SettingsError(f"{dotted} is a single value, not a group of settings", field=dotted)
    return current


def _value(value: Any) -> Any:
    """JSON → TOML: tables for objects (``[sinks.notion]`` rather than an inline ``{…}``)."""
    import tomlkit

    if isinstance(value, dict):
        table = tomlkit.table()
        for k, v in value.items():
            if v is not None:
                table[str(k)] = _value(v)
        return table
    if isinstance(value, list):
        if any(v is None for v in value):
            raise SettingsError("a list can't contain null")
        if any(isinstance(v, dict) for v in value):
            return [_value(v) for v in value]
        return value
    return value


def apply_changes(doc: tomlkit.TOMLDocument, changes: dict[str, Any]) -> None:
    import tomlkit

    created: list[Any] = []
    for dotted, value in changes.items():
        keys = parse_path(dotted)
        node: Any = doc
        for i, key in enumerate(keys[:-1]):
            if value is None and key not in node:
                node = None  # removing something that isn't there: nothing to do
                break
            node = _table(node, key, ".".join(keys[: i + 1]), created)
        if node is None:
            continue
        last = keys[-1]
        if value is None:
            if last in node:
                del node[last]
        else:
            try:
                node[last] = _value(value)
            except SettingsError as exc:
                raise SettingsError(str(exc), field=dotted) from None
            except (TypeError, ValueError) as exc:
                raise SettingsError(f"{dotted}: unsupported value ({exc})", field=dotted) from None
    for table in created:
        if table.values() and not any(isinstance(v, dict) for v in table.values()):  # a leaf: [sinks.notion]
            table.add(tomlkit.nl())  # a blank line before whatever header follows, as in the template


def _loc(loc: tuple[Any, ...]) -> str:
    return ".".join(str(p) for p in loc)


_MISSING = object()


def flatten(data: Any, prefix: str = "") -> dict[str, Any]:
    """``{"a": {"b": 1}}`` → ``{"a.b": 1}`` (lists are values, not tables)."""
    if not isinstance(data, dict):
        return {prefix: data}
    out: dict[str, Any] = {}
    for key, value in data.items():
        out |= flatten(value, f"{prefix}.{key}" if prefix else str(key))
    return out


def _is_path(field: str) -> bool:
    """A setting that holds a file or folder (``data_dir``, ``…_dir``, ``…_file``, a binary or a model file)."""
    last = field.rpartition(".")[2]
    return last in ("binary", "model") or last.endswith(("_dir", "_file", "_root", "_path"))


def _check_values(data: dict[str, Any]) -> None:
    """What the config's types let through but would break the app later.

    * ``~name/…`` is *the home folder of the account "name"* (``~Documents/notes``, a typo for
      ``~/Documents``): expanding it raises, and saved as ``data_dir`` it stopped the app from opening;
    * NaN and ±infinity (JSON's NaN/Infinity, or 1e400) aren't JSON: GET /api/settings then failed for
      good, so the Settings screen couldn't even open to fix it.
    """
    for field, value in flatten(data).items():
        for v in value if isinstance(value, list) else [value]:
            if isinstance(v, float) and not math.isfinite(v):
                raise SettingsError(f"{field}: must be a finite number", field=field)
        if isinstance(value, str) and value.startswith("~") and _is_path(field):
            try:
                Path(value).expanduser()
            except RuntimeError:
                raise SettingsError(f"{field}: {value!r} names another account's home folder: write ~/… for yours",
                                    field=field) from None


def validate_text(text: str) -> Config:
    """What :func:`~voicestream.config.load_config` would make of ``text`` — the config and every
    enabled sink — or :class:`SettingsError` naming the first problem."""
    from ..sinks.base import sink_class

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:  # pragma: no cover - tomlkit wrote it
        raise SettingsError(f"the settings would not be valid TOML: {exc}") from None
    _check_values(data)
    try:
        cfg = Config.model_validate(data)
    except ValidationError as exc:
        e = exc.errors()[0]
        field = _loc(e["loc"])
        raise SettingsError(f"{field or 'settings'}: {e['msg']}", field=field or None) from None
    for name, raw in cfg.sinks.items():
        if not isinstance(raw, dict) or not raw.get("enabled", name == "markdown"):
            continue
        try:
            cls = sink_class(name)
        except KeyError as exc:
            raise SettingsError(str(exc.args[0]), field=f"sinks.{name}") from None
        except ImportError as exc:
            raise SettingsError(f"[sinks.{name}] is unavailable: {exc}", field=f"sinks.{name}") from None
        try:
            cls.Config.model_validate({**raw, "enabled": True})
        except ValidationError as exc:
            e = exc.errors()[0]
            field = f"sinks.{name}" + (f".{_loc(e['loc'])}" if e["loc"] else "")
            raise SettingsError(f"[sinks.{name}] {_loc(e['loc']) or name}: {e['msg']}", field=field) from None
    return cfg


def save_changes(path: Path, changes: dict[str, Any]) -> Config:
    """Apply ``changes`` to the file at ``path`` (or the template), validate, write; returns the new config.
    Raises :class:`SettingsError` and then writes nothing. Nothing is locked: see :func:`save_checked`."""
    return save_checked(path, changes, locked=())[0]


def save_checked(
    path: Path, changes: dict[str, Any], *, locked: Collection[str] = LOCKED
) -> tuple[Config, list[dict[str, str]]]:
    """:func:`save_changes` for the app's API: a change to a ``locked`` setting is refused, and the
    changed :data:`REVIEW` settings are returned as ``[{field, value}]`` (values shortened for showing)."""
    import tomlkit

    if not isinstance(changes, dict) or not changes:
        raise SettingsError("no changes given")
    with editing(path):  # two saves at once: the second reads what the first wrote
        doc = load_document(path)
        try:
            before = flatten(tomllib.loads(tomlkit.dumps(doc)))
        except tomllib.TOMLDecodeError as exc:  # tomlkit is more lenient than the loader
            raise SettingsError(f"{path} can't be read as TOML: {exc} — fix or remove it", field=None) from None
        apply_changes(doc, changes)
        text = tomlkit.dumps(doc)
        cfg = validate_text(text)
        changed = _changed(before, flatten(tomllib.loads(text)))
        _refuse_locked(changed, locked)
        write_private(path, text)
    return cfg, [{"field": f, "value": shown(f, v)} for f, v in changed.items() if _matches(f, REVIEW)]


def _changed(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Settings whose value in the file differs: ``{field: new value}`` (:data:`_MISSING` when removed)."""
    return {f: after.get(f, _MISSING) for f in dict.fromkeys([*before, *after]) if before.get(f, _MISSING) != after.get(f, _MISSING)}


def _matches(field: str, patterns: Collection[str]) -> bool:
    return any(fnmatch.fnmatchcase(field, p) for p in patterns)


def _refuse_locked(changed: dict[str, Any], locked: Collection[str]) -> None:
    if not locked:
        return
    defaults = flatten(Config().model_dump(mode="json"))
    for field, value in changed.items():
        if not _matches(field, locked):
            continue
        # back to the default (or removed) is always fine; sink settings have no core default: empty is theirs
        if value is _MISSING or (value == defaults[field] if field in defaults else value in (None, [], "", False)):
            continue
        raise SettingsError(
            f"{field} decides what Voicestream runs or who gets your keys, so it can't be changed from the app: "
            "edit voicestream.toml (Settings → Advanced → Open the settings file)",
            field=field,
        )


def shown(field: str, value: Any) -> str:
    """A setting's new value as the review list shows it: a command's program, a server's address —
    never the arguments or the path of a URL, which may carry a token."""
    if value is _MISSING or value is None:
        return "(default)"
    if field.endswith("_command"):
        try:
            argv = shlex.split(value) if isinstance(value, str) else [str(a) for a in value]
        except ValueError:  # unbalanced quotes in a sink that is off (so not validated)
            argv = str(value).split()
        return f"{argv[0]} …" if len(argv) > 1 else (argv[0] if argv else "")
    if field.endswith("url") and isinstance(value, str):
        parts = urlsplit(value)
        return f"{parts.scheme}://{parts.netloc.rpartition('@')[2]}/…" if parts.netloc else "(not a URL)"
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= 120 else text[:119] + "…"


# ----------------------------------------------------------------------- viewing


def sink_catalog() -> list[dict[str, Any]]:
    from ..sinks.base import REGISTRY, sink_class

    out = []
    for name in REGISTRY:
        entry: dict[str, Any] = {"name": name, "label": SINK_LABELS.get(name, name.capitalize()), "mac_only": name in MAC_ONLY_SINKS}
        try:
            cls = sink_class(name)
        except Exception as exc:  # noqa: BLE001 - listed, but marked unavailable
            entry.update(available=False, error=str(exc), secrets=[], supports_notes=False, schema=None)
        else:
            fields = cls.Config.model_fields
            entry.update(
                available=True,
                secrets=[f.default for k, f in fields.items() if k.endswith("_env") and isinstance(f.default, str) and f.default],
                supports_notes=cls.supports_notes,
                schema=cls.Config.model_json_schema(),
            )
        out.append(entry)
    return out


def sink_settings(cfg: Config) -> dict[str, dict[str, Any]]:
    """Each known sink's effective settings (defaults filled in) and whether it is on."""
    from ..sinks.base import REGISTRY, describe_config_error, sink_class

    out: dict[str, dict[str, Any]] = {}
    for name in dict.fromkeys([*REGISTRY, *cfg.sinks]):
        raw = dict(cfg.sinks.get(name) or {})
        enabled = bool(raw.get("enabled", name == "markdown"))
        entry: dict[str, Any] = {"enabled": enabled, "values": raw, "error": None}
        try:
            cls = sink_class(name)
            entry["values"] = cls.Config.model_validate({**raw, "enabled": enabled}).model_dump(mode="json")
        except ValidationError as exc:
            entry["error"] = describe_config_error(name, exc)
        except (KeyError, ImportError) as exc:
            entry["error"] = str(exc.args[0] if isinstance(exc, KeyError) else exc)
        out[name] = entry
    return out


def settings_view(cfg: Config) -> dict[str, Any]:
    """``GET /api/settings``: every value, the sinks, which keys are set — never a key itself."""
    from . import secrets

    return {
        "values": cfg.model_dump(mode="json"),
        "sinks": sink_settings(cfg),
        "secrets": secrets.status(),
        "catalog": {"sinks": sink_catalog()},
    }
