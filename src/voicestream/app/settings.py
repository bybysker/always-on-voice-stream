"""Reading and writing ``voicestream.toml`` from the app's Settings screen.

Changes arrive as dotted paths (``{"transcription.backend": "whisper-cpp", "sinks.reminders.enabled":
true}``; ``null`` removes the setting, i.e. back to its default). They are applied to the file with
``tomlkit``, which keeps your comments and layout, then the *whole* result is validated — the
config and every enabled sink's own settings — before anything is written. A change that doesn't
validate is refused with the field it is about, and the file is left exactly as it was.

Writes are atomic and 0600. With no file yet, the first save starts from the commented template
``voicestream init`` writes, so the file stays readable by hand.
"""

from __future__ import annotations

import os
import re
import tomllib
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from ..config import Config, load_config, load_dotenv, load_keychain
from .files import write_private

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


def validate_text(text: str) -> Config:
    """What :func:`~voicestream.config.load_config` would make of ``text`` — the config and every
    enabled sink — or :class:`SettingsError` naming the first problem."""
    from ..sinks.base import sink_class

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:  # pragma: no cover - tomlkit wrote it
        raise SettingsError(f"the settings would not be valid TOML: {exc}") from None
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
    Raises :class:`SettingsError` and then writes nothing."""
    import tomlkit

    if not isinstance(changes, dict) or not changes:
        raise SettingsError("no changes given")
    doc = load_document(path)
    apply_changes(doc, changes)
    text = tomlkit.dumps(doc)
    cfg = validate_text(text)
    write_private(path, text)
    return cfg


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
