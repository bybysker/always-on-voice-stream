"""API keys and tokens set from the app: the macOS Keychain, else ``~/.voicestream/.env``.

Everything in voicestream reads its credentials from environment variables (the LLM and the
transcribers when they are built, the sinks on every request). So the stores below only feed
``os.environ``:

* :func:`load_into_env` copies stored keys into the environment at start-up, never replacing a
  variable that is already set (your shell's exports win, as with the ``.env`` files);
* :func:`set_secret` / :func:`delete_secret` change the store **and** ``os.environ`` directly —
  :func:`~voicestream.config.load_dotenv` uses ``setdefault`` and would ignore a changed key.

Only the names in :func:`known_names` can be stored, so a request to the local API can't plant
an arbitrary variable (``PATH``, ``DYLD_INSERT_LIBRARIES``…) into the process. Values are never
logged, returned or written anywhere but the store itself.

The Keychain is reached through ``keyring`` (service ``voicestream``). Without a usable backend
(Linux CI, a headless box, ``keyring`` not installed) the keys go into ``~/.voicestream/.env``
(0600) instead, which every voicestream command already reads and agents are denied
(``agents.runner.secret_files``). ``VOICESTREAM_SECRET_STORE=file|keychain`` forces either one;
the test suite forces ``file`` so it never touches a real Keychain.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import os
from pathlib import Path
from typing import Any, Literal

from .files import editing, write_private

log = logging.getLogger(__name__)

SERVICE = "voicestream"
STORE_ENV = "VOICESTREAM_SECRET_STORE"
Store = Literal["keychain", "file"]

CORE_SECRETS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "DEEPGRAM_API_KEY",
    "HUBSPOT_TOKEN",
    "VOICESTREAM_GOOGLE_URL",
    "VOICESTREAM_GOOGLE_SECRET",
)
"""The keys the app's setup screens ask for (Claude, cloud speech-to-text, HubSpot, the Google bridge)."""


class SecretError(ValueError):
    """An unknown name or an unusable value (the message never contains the value)."""


@functools.cache
def known_names() -> tuple[str, ...]:
    """Every credential voicestream reads: :data:`CORE_SECRETS`, the variables agents are always
    denied (``agents.runner.SECRET_ENV_VARS``) and each registered sink's ``*_env`` defaults
    (``NOTION_TOKEN``, ``LINEAR_API_KEY``…). Derived from the code so a new sink is covered."""
    from ..agents.runner import SECRET_ENV_VARS
    from ..sinks.base import REGISTRY, sink_class

    names = dict.fromkeys([*CORE_SECRETS, *SECRET_ENV_VARS])
    for sink in REGISTRY:
        try:
            fields = sink_class(sink).Config.model_fields
        except Exception:  # noqa: BLE001 - a sink whose optional dependency is missing still has no secret we know of
            continue
        for key, field in fields.items():
            if key.endswith("_env") and isinstance(field.default, str) and field.default:
                names.setdefault(field.default)
    return tuple(names)


def check_name(name: str) -> str:
    if name not in known_names():
        raise SecretError(f"unknown key name {name!r}")
    return name


def env_file() -> Path:
    """The fallback store: the ``.env`` that ``voicestream init`` writes and every command reads."""
    return Path("~/.voicestream/.env").expanduser()


# --------------------------------------------------------------------- backends


def _keyring() -> Any | None:
    """The ``keyring`` backend when it can really store things, else ``None`` (→ the file)."""
    choice = os.environ.get(STORE_ENV, "").strip().lower()
    if choice == "file":
        return None
    try:
        import keyring
        from keyring.backends import fail
    except ImportError:
        return None
    try:
        backend = keyring.get_keyring()
    except Exception:  # noqa: BLE001 - a broken keyring config must not stop the app
        return None
    if isinstance(backend, fail.Keyring) or (getattr(backend, "priority", 0) or 0) <= 0:
        return None
    if type(backend).__name__ == "ChainerBackend" and not getattr(backend, "backends", None):
        return None
    return backend


def store_name() -> Store:
    return "keychain" if _keyring() is not None else "file"


def _read_file() -> list[str]:
    try:
        return env_file().read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []


def _line_name(line: str) -> str | None:
    s = line.strip()
    if not s or s.startswith("#") or "=" not in s:
        return None
    if s.startswith("export "):
        s = s[len("export ") :]
    return s.partition("=")[0].strip()


def _quoted(value: str) -> str:
    """As :func:`~voicestream.config.load_dotenv` will read it back: it strips one pair of matching
    quotes and surrounding whitespace, nothing else."""
    if value != value.strip() or (len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'"):
        quote = "'" if '"' in value else '"'
        return f"{quote}{value}{quote}"
    return value


def _write_file(name: str, value: str | None) -> bool:
    """Set (or with ``None`` remove) ``name`` in the ``.env``, keeping every other line. Returns
    whether the file had the name. Locked: two keys saved at once must both end up in the file."""
    with editing(env_file()):
        lines = _read_file()
        kept = [line for line in lines if _line_name(line) != name]
        had = len(kept) != len(lines)
        if value is not None:
            kept.append(f"{name}={_quoted(value)}")
        elif not had:
            return False
        write_private(env_file(), "\n".join(kept) + "\n")
        return had


def _file_value(name: str) -> str | None:
    for line in _read_file():
        if _line_name(line) == name:
            raw = line.strip().removeprefix("export ").partition("=")[2].strip()
            if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
                raw = raw[1:-1]
            return raw or None
    return None


# ------------------------------------------------------------------------- API


def load_into_env() -> list[str]:
    """Copy stored keys into ``os.environ`` for the variables that aren't set yet; returns their names.

    Reads the Keychain only (the ``.env`` files are loaded by ``load_config`` already). Each lookup
    is a separate Keychain query, but only for names missing from the environment.
    """
    backend = _keyring()
    if backend is None:
        return []
    loaded = []
    for name in known_names():
        if os.environ.get(name):
            continue
        try:
            value = backend.get_password(SERVICE, name)
        except Exception as exc:  # noqa: BLE001 - locked or denied Keychain: run without that key
            log.warning("could not read %s from the Keychain: %s", name, type(exc).__name__)
            continue
        if value:
            os.environ[name] = value
            loaded.append(name)
    return loaded


def is_set(name: str) -> bool:
    return bool(os.environ.get(name))


def status() -> dict[str, bool]:
    """``{NAME: is it available}`` for every known name — never the values."""
    return {name: is_set(name) for name in known_names()}


def set_secret(name: str, value: str) -> dict[str, Any]:
    """Store ``value`` under ``name`` and put it in ``os.environ``; an empty value deletes it.

    Returns ``{"set": bool, "store": "keychain"|"file"}``.
    """
    check_name(name)
    value = (value or "").strip()
    if not value:
        return delete_secret(name)
    if not value.isprintable():
        # not only \n and \r: the .env files are read with splitlines(), which also breaks lines at
        # \x0b \x0c \x1c-\x1e \x85 \u2028 \u2029 — the rest of such a value would become a line of
        # its own, setting any variable it names (ANTHROPIC_BASE_URL=…, PATH…) on every start
        raise SecretError(f"{name} must be a single line of visible characters (no line breaks, tabs or control characters)")
    backend = _keyring()
    store: Store = "file"
    if backend is not None:
        try:
            backend.set_password(SERVICE, name, value)
            store = "keychain"
            # a stale copy in the .env would come back if the Keychain entry is removed later
            if _file_value(name) is not None:
                _write_file(name, None)
        except Exception as exc:  # noqa: BLE001 - e.g. the Keychain prompt was refused
            log.warning("could not save %s in the Keychain (%s); saving it in %s", name, type(exc).__name__, env_file())
    if store == "file":
        _write_file(name, value)
    os.environ[name] = value
    log.info("%s saved (%s)", name, store)
    return {"set": True, "store": store}


def delete_secret(name: str) -> dict[str, Any]:
    """Remove ``name`` from both stores and from ``os.environ``."""
    check_name(name)
    backend = _keyring()
    if backend is not None:
        with contextlib.suppress(Exception):  # not there (PasswordDeleteError) or refused
            backend.delete_password(SERVICE, name)
    _write_file(name, None)
    os.environ.pop(name, None)
    log.info("%s removed", name)
    return {"set": False, "store": "keychain" if backend is not None else "file"}


# ------------------------------------------------------------------- key checks

CHECK_TIMEOUT_S = 10.0


async def verify(name: str, cfg: Any, *, transport: Any = None) -> dict[str, Any]:
    """``{"ok": bool, "detail": str}``: does the stored key work? One cheap read-only request
    (nothing is billed), :data:`CHECK_TIMEOUT_S` at most. The detail never contains the key."""
    check_name(name)
    value = os.environ.get(name)
    if not value:
        return {"ok": False, "detail": f"{name} is not set"}
    try:
        if name == "ANTHROPIC_API_KEY":
            return await _check_anthropic(value, transport)
        if name in _HTTP_CHECKS:
            return await _check_http(name, value, cfg, transport)
        return await _check_with_sink(name, cfg)
    except Exception as exc:  # noqa: BLE001 - reported to the person, not raised
        return {"ok": False, "detail": _scrub(f"{type(exc).__name__}: {exc}", value)}


def _scrub(text: str, value: str) -> str:
    return text.replace(value, "•••") if value else text


async def _check_anthropic(key: str, transport: Any) -> dict[str, Any]:
    import anthropic

    kwargs: dict[str, Any] = {"api_key": key, "max_retries": 0, "timeout": CHECK_TIMEOUT_S}
    if transport is not None:
        kwargs["http_client"] = anthropic.DefaultAsyncHttpxClient(transport=transport)
    client = anthropic.AsyncAnthropic(**kwargs)
    try:
        page = await client.models.list(limit=1)  # free, and needs a valid key
        model = page.data[0].id if page.data else None
        return {"ok": True, "detail": "Claude accepted the key" + (f" (e.g. {model})" if model else "")}
    except anthropic.AuthenticationError:
        return {"ok": False, "detail": "Claude refused the key: copy it again from console.anthropic.com"}
    except anthropic.PermissionDeniedError:
        return {"ok": False, "detail": "the key is valid but not allowed to use the API (check the Console)"}
    except anthropic.APIConnectionError:
        return {"ok": False, "detail": "could not reach api.anthropic.com (offline?)"}
    except anthropic.APIStatusError as exc:
        return {"ok": False, "detail": f"Claude answered HTTP {exc.status_code}"}
    finally:
        await client.close()


_HTTP_CHECKS = {"OPENAI_API_KEY", "DEEPGRAM_API_KEY", "HUBSPOT_TOKEN"}


async def _check_http(name: str, key: str, cfg: Any, transport: Any) -> dict[str, Any]:
    import httpx

    if name == "OPENAI_API_KEY":
        base = cfg.transcription.openai.base_url.rstrip("/")
        url, headers, service = f"{base}/models", {"Authorization": f"Bearer {key}"}, "OpenAI"
    elif name == "DEEPGRAM_API_KEY":
        url, headers, service = "https://api.deepgram.com/v1/projects", {"Authorization": f"Token {key}"}, "Deepgram"
    else:
        url, headers, service = "https://api.hubapi.com/crm/v3/owners?limit=1", {"Authorization": f"Bearer {key}"}, "HubSpot"
    async with httpx.AsyncClient(timeout=CHECK_TIMEOUT_S, transport=transport) as http:
        try:
            resp = await http.get(url, headers=headers)
        except httpx.TransportError:
            return {"ok": False, "detail": f"could not reach {service} (offline?)"}
    if resp.status_code < 300:
        return {"ok": True, "detail": f"{service} accepted the key"}
    if resp.status_code in (401, 403):
        return {"ok": False, "detail": f"{service} refused the key (HTTP {resp.status_code}): copy it again"}
    return {"ok": False, "detail": f"{service} answered HTTP {resp.status_code}"}


async def _check_with_sink(name: str, cfg: Any) -> dict[str, Any]:
    """The other keys belong to a sink (``NOTION_TOKEN`` → notion…): its own ``check()`` knows what
    to call, with the settings you gave it."""
    from ..sinks.base import REGISTRY, build_sink, sink_class

    for sink_name in REGISTRY:
        try:
            fields = sink_class(sink_name).Config.model_fields
        except Exception:  # noqa: BLE001
            continue
        raw = dict(cfg.sinks.get(sink_name) or {})
        uses = {raw.get(k, f.default) for k, f in fields.items() if k.endswith("_env")}
        if name not in uses:
            continue
        sink = build_sink(sink_name, {**raw, "enabled": True}, cfg)
        assert sink is not None
        try:
            return {"ok": True, "detail": await sink.check()}
        finally:
            await sink.aclose()
    return {"ok": False, "detail": f"nothing uses {name} in your settings yet"}
