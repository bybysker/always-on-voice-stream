"""The app's settings file, keys and preferences: voicestream/app/settings.py, secrets.py, prefs.py."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

import httpx
import pytest

from voicestream.agents.runner import scrubbed_env
from voicestream.app import prefs as prefs_mod
from voicestream.app import secrets
from voicestream.app.settings import SettingsError, default_config_path, load_app_config, save_changes, settings_view
from voicestream.config import Config, load_config

USER_FILE = """\
# my voicestream settings — keep this comment
user_name = "Alex"   # me

[transcription]
backend = "openai"   # cloud for now

[sinks.markdown]
enabled = true
"""


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


# ----------------------------------------------------------------------- settings


def test_changes_keep_comments_and_layout(tmp_path: Path) -> None:
    path = tmp_path / "voicestream.toml"
    path.write_text(USER_FILE, encoding="utf-8")
    cfg = save_changes(path, {
        "transcription.backend": "whisper-cpp",
        "transcription.whisper_cpp.model": "small",
        "brain.vocabulary": ["Acme", "OKR"],
        "sinks.reminders.enabled": True,
        "sinks.reminders.list_name": "Réunions",
    })  # fmt: skip
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# my voicestream settings — keep this comment\nuser_name = \"Alex\"   # me\n")
    assert 'backend = "whisper-cpp"   # cloud for now' in text  # the value changes, its comment stays
    assert cfg.transcription.backend == "whisper-cpp" and cfg.transcription.whisper_cpp.model == "small"
    assert load_config(path).sinks["reminders"] == {"enabled": True, "list_name": "Réunions"}
    assert load_config(path).brain.vocabulary == ["Acme", "OKR"]
    if os.name == "posix":
        assert _mode(path) == 0o600


def test_null_goes_back_to_the_default(tmp_path: Path) -> None:
    path = tmp_path / "voicestream.toml"
    path.write_text(USER_FILE, encoding="utf-8")
    cfg = save_changes(path, {"transcription.backend": None, "never.set": None})
    assert cfg.transcription.backend == Config().transcription.backend
    assert "backend" not in path.read_text(encoding="utf-8")
    assert "never" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"transcription.backend": "telepathy"}, "transcription.backend"),
        ({"timezone": "Mars/Olympus"}, "timezone"),
        ({"vad.margin_db": "loud"}, "vad.margin_db"),
        ({"capture.mic": 3}, "capture.mic"),
        ({"user_name.first": "Alex"}, "user_name"),  # a value, not a table
        ({"sinks.reminders.enabled": True, "sinks.reminders.list": "x"}, "sinks.reminders.list"),  # the sink's own check
        ({"sinks.carrier_pigeon.enabled": True}, "sinks.carrier_pigeon"),
        ({"../../etc/passwd": "x"}, "../../etc/passwd"),
        ({"bogus_setting": 1}, "bogus_setting"),
    ],
)
def test_invalid_changes_write_nothing(tmp_path: Path, changes: dict[str, Any], field: str) -> None:
    path = tmp_path / "voicestream.toml"
    path.write_text(USER_FILE, encoding="utf-8")
    before = path.stat().st_mtime_ns
    with pytest.raises(SettingsError) as err:
        save_changes(path, changes)
    assert err.value.field == field
    assert path.read_text(encoding="utf-8") == USER_FILE and path.stat().st_mtime_ns == before
    assert not list(tmp_path.glob(".*tmp"))


def test_a_disabled_sink_may_hold_unfinished_settings(tmp_path: Path) -> None:
    path = tmp_path / "voicestream.toml"
    cfg = save_changes(path, {"sinks.hubspot.enabled": False, "sinks.hubspot.owner_email": "alex@example.com"})
    assert cfg.sinks["hubspot"]["owner_email"] == "alex@example.com"


def test_first_save_starts_from_the_template(tmp_path: Path) -> None:
    path = tmp_path / "new" / "voicestream.toml"
    save_changes(path, {"user_name": "Alex Martin"})
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# voicestream configuration.")  # the commented template, not a bare dump
    assert 'user_name = "Alex Martin"' in text and "[sinks.markdown]" in text
    assert load_config(path).user_name == "Alex Martin"
    if os.name == "posix":
        assert _mode(path.parent) == 0o700


def test_new_tables_are_readable(tmp_path: Path) -> None:
    path = tmp_path / "voicestream.toml"
    path.write_text('user_name = "x"\n\n[transcription]\nbackend = "openai"\n\n[echo]\nenabled = true\n', encoding="utf-8")
    save_changes(path, {"transcription.whisper_cpp.model": "small"})
    # the new table goes with its parent, and a blank line still separates it from the next one
    assert '[transcription.whisper_cpp]\nmodel = "small"\n\n[echo]' in path.read_text(encoding="utf-8")


def test_a_broken_file_is_reported_not_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "voicestream.toml"
    path.write_text("user_name = Alex\n", encoding="utf-8")  # unquoted
    with pytest.raises(SettingsError) as err:
        save_changes(path, {"user_name": "Alex"})
    assert err.value.field is None and path.read_text(encoding="utf-8") == "user_name = Alex\n"


def test_app_config_without_a_file_is_the_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VOICESTREAM_CONFIG", raising=False)
    assert default_config_path() == Path("~/.voicestream/voicestream.toml").expanduser()
    monkeypatch.setenv("VOICESTREAM_CONFIG", str(tmp_path / "x.toml"))
    assert default_config_path() == tmp_path / "x.toml"
    monkeypatch.chdir(tmp_path)
    (tmp_path / "voicestream.toml").write_text('user_name = "from the working dir"\n', encoding="utf-8")
    assert load_app_config(tmp_path / "missing.toml").user_name == Config().user_name  # never the cwd's file


def test_settings_view_never_contains_a_key(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-very-secret")
    cfg.sinks["notion"] = {"enabled": True, "tasks_database_id": "d1"}
    cfg.sinks["reminders"] = {"enabled": True, "bogus": 1}
    view = settings_view(cfg)
    assert "sk-ant-very-secret" not in repr(view)
    assert view["secrets"]["ANTHROPIC_API_KEY"] is True and view["secrets"]["OPENAI_API_KEY"] is False
    assert view["sinks"]["notion"]["enabled"] and view["sinks"]["notion"]["values"]["token_env"] == "NOTION_TOKEN"
    assert view["sinks"]["reminders"]["error"].startswith("[sinks.reminders] bogus")
    assert view["sinks"]["markdown"]["enabled"] is True  # on by default
    names = {s["name"]: s for s in view["catalog"]["sinks"]}
    assert names["reminders"]["label"] == "Apple Reminders" and names["reminders"]["mac_only"]
    assert names["hubspot"]["secrets"] == ["HUBSPOT_TOKEN"]
    assert view["values"]["user_name"] == "Alex"


# ------------------------------------------------------------------------- secrets


def test_known_names_come_from_the_code() -> None:
    names = secrets.known_names()
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPGRAM_API_KEY", "HUBSPOT_TOKEN", "VOICESTREAM_GOOGLE_URL",
                 "VOICESTREAM_GOOGLE_SECRET", "NOTION_TOKEN", "LINEAR_API_KEY", "SLACK_WEBHOOK_URL"):
        assert name in names
    for name in ("PATH", "HOME", "DYLD_INSERT_LIBRARIES", "PYTHONPATH", "VOICESTREAM_CONFIG"):
        assert name not in names
    # agents never inherit a key saved from the app (they keep their own Claude credentials)
    blanked = scrubbed_env()
    assert all(n in blanked for n in names if not n.startswith("ANTHROPIC_"))


@pytest.mark.parametrize("name", ["PATH", "DYLD_INSERT_LIBRARIES", "anthropic_api_key", "ANTHROPIC_API_KEY\n", ""])
def test_only_known_names_can_be_stored(name: str) -> None:
    with pytest.raises(secrets.SecretError):
        secrets.set_secret(name, "x")
    assert os.environ.get("PATH")  # untouched


def test_file_store_is_private_keeps_other_lines_and_overrides_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    env = secrets.env_file()
    env.parent.mkdir(parents=True, exist_ok=True)
    env.write_text("# my keys\nNOTION_TOKEN=keep-me\nexport OPENAI_API_KEY=old\n", encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "old")
    assert secrets.store_name() == "file"  # the test suite never uses a real Keychain
    result = secrets.set_secret("OPENAI_API_KEY", "  sk-new  ")
    assert result == {"set": True, "store": "file"}
    assert os.environ["OPENAI_API_KEY"] == "sk-new"  # load_dotenv's setdefault would have kept "old"
    text = env.read_text(encoding="utf-8")
    assert text.startswith("# my keys\nNOTION_TOKEN=keep-me\n") and text.count("OPENAI_API_KEY") == 1
    assert "OPENAI_API_KEY=sk-new" in text
    if os.name == "posix":
        assert _mode(env) == 0o600
    assert secrets.delete_secret("OPENAI_API_KEY") == {"set": False, "store": "file"}
    assert "OPENAI_API_KEY" not in os.environ and "OPENAI_API_KEY" not in env.read_text(encoding="utf-8")
    assert "NOTION_TOKEN=keep-me" in env.read_text(encoding="utf-8")
    assert secrets.set_secret("NOTION_TOKEN", "")["set"] is False  # empty = delete
    assert "NOTION_TOKEN" not in env.read_text(encoding="utf-8")


def test_a_saved_value_reads_back_exactly(monkeypatch: pytest.MonkeyPatch) -> None:
    for value in ('"quoted"', "has spaces inside", "x=y=z"):
        secrets.set_secret("VOICESTREAM_GOOGLE_SECRET", value)
        monkeypatch.delenv("VOICESTREAM_GOOGLE_SECRET")
        load_config(None)  # reads ~/.voicestream/.env
        assert os.environ["VOICESTREAM_GOOGLE_SECRET"] == value
    with pytest.raises(secrets.SecretError):
        secrets.set_secret("VOICESTREAM_GOOGLE_SECRET", "two\nlines")


class FakeKeychain:
    priority = 5

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, name: str) -> str | None:
        return self.items.get((service, name))

    def set_password(self, service: str, name: str, value: str) -> None:
        self.items[(service, name)] = value

    def delete_password(self, service: str, name: str) -> None:
        self.items.pop((service, name))


def test_keychain_store_and_loading_never_overrides_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    kc = FakeKeychain()
    monkeypatch.setattr(secrets, "_keyring", lambda: kc)
    env = secrets.env_file()
    env.parent.mkdir(parents=True, exist_ok=True)
    env.write_text("DEEPGRAM_API_KEY=stale\n", encoding="utf-8")
    assert secrets.set_secret("DEEPGRAM_API_KEY", "dg-new") == {"set": True, "store": "keychain"}
    assert kc.items[("voicestream", "DEEPGRAM_API_KEY")] == "dg-new"
    assert "DEEPGRAM_API_KEY" not in env.read_text(encoding="utf-8")  # no stale copy left behind
    kc.items[("voicestream", "ANTHROPIC_API_KEY")] = "from-keychain"
    monkeypatch.delenv("DEEPGRAM_API_KEY")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-the-shell")
    loaded = secrets.load_into_env()
    assert loaded == ["DEEPGRAM_API_KEY"]
    assert os.environ["ANTHROPIC_API_KEY"] == "from-the-shell" and os.environ["DEEPGRAM_API_KEY"] == "dg-new"
    secrets.delete_secret("DEEPGRAM_API_KEY")
    assert ("voicestream", "DEEPGRAM_API_KEY") not in kc.items and "DEEPGRAM_API_KEY" not in os.environ


def test_load_config_reads_the_keychain_before_the_env_files(monkeypatch: pytest.MonkeyPatch) -> None:
    kc = FakeKeychain()
    kc.items[("voicestream", "HUBSPOT_TOKEN")] = "pat-keychain"
    monkeypatch.setattr(secrets, "_keyring", lambda: kc)
    env = secrets.env_file()
    env.parent.mkdir(parents=True, exist_ok=True)
    env.write_text("HUBSPOT_TOKEN=pat-file\n", encoding="utf-8")
    monkeypatch.delenv("HUBSPOT_TOKEN", raising=False)
    load_config(None)
    assert os.environ["HUBSPOT_TOKEN"] == "pat-keychain"


def test_a_failing_keychain_falls_back_to_the_file(monkeypatch: pytest.MonkeyPatch) -> None:
    class Refused(FakeKeychain):
        def set_password(self, service: str, name: str, value: str) -> None:
            raise PermissionError("the user clicked Deny")

    monkeypatch.setattr(secrets, "_keyring", lambda: Refused())
    assert secrets.set_secret("LINEAR_API_KEY", "lin-1")["store"] == "file"
    assert "LINEAR_API_KEY=lin-1" in secrets.env_file().read_text(encoding="utf-8")


def test_keyring_without_a_backend_is_the_file(monkeypatch: pytest.MonkeyPatch) -> None:
    keyring = pytest.importorskip("keyring")
    from keyring.backends import fail

    monkeypatch.delenv(secrets.STORE_ENV)
    monkeypatch.setattr(keyring, "get_keyring", lambda: fail.Keyring())
    assert secrets.store_name() == "file"
    monkeypatch.setenv(secrets.STORE_ENV, "file")
    monkeypatch.setattr(keyring, "get_keyring", lambda: FakeKeychain())
    assert secrets.store_name() == "file"  # forced


def _anthropic_transport(handler: Any) -> Any:
    """anthropic 1.x speaks httpx2, older versions httpx: a mock transport of the matching kind."""
    import anthropic

    try:
        import httpx2

        if issubclass(anthropic.DefaultAsyncHttpxClient, httpx2.AsyncClient):
            return httpx2.MockTransport(lambda r: httpx2.Response(**handler(r)))
    except ImportError:
        pass
    return httpx.MockTransport(lambda r: httpx.Response(**handler(r)))


async def test_verify_claude_key(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def ok(request: Any) -> dict[str, Any]:
        seen.append(request.headers["x-api-key"])
        return {"status_code": 200, "json": {"data": [{"id": "claude-haiku-5-5", "type": "model", "display_name": "Haiku",
                                                       "created_at": "2026-10-01T00:00:00Z"}], "has_more": False}}

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    result = await secrets.verify("ANTHROPIC_API_KEY", cfg, transport=_anthropic_transport(ok))
    assert result["ok"] and seen == ["sk-ant-secret"] and "sk-ant-secret" not in repr(result)

    def refused(request: Any) -> dict[str, Any]:
        return {"status_code": 401, "json": {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}}

    result = await secrets.verify("ANTHROPIC_API_KEY", cfg, transport=_anthropic_transport(refused))
    assert not result["ok"] and "refused" in result["detail"]


@pytest.mark.parametrize(
    ("name", "url", "auth"),
    [
        ("OPENAI_API_KEY", "https://api.openai.com/v1/models", "Bearer k-1"),
        ("DEEPGRAM_API_KEY", "https://api.deepgram.com/v1/projects", "Token k-1"),
        ("HUBSPOT_TOKEN", "https://api.hubapi.com/crm/v3/owners?limit=1", "Bearer k-1"),
    ],
)
async def test_verify_cloud_keys(cfg: Config, monkeypatch: pytest.MonkeyPatch, name: str, url: str, auth: str) -> None:
    monkeypatch.setenv(name, "k-1")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200 if len(calls) == 1 else 401, json={})

    good = await secrets.verify(name, cfg, transport=httpx.MockTransport(handler))
    bad = await secrets.verify(name, cfg, transport=httpx.MockTransport(handler))
    assert good["ok"] and not bad["ok"] and "refused" in bad["detail"]
    assert str(calls[0].url) == url and calls[0].headers["authorization"] == auth and calls[0].method == "GET"
    assert "k-1" not in repr([good, bad])


async def test_verify_unset_and_sink_keys(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    assert await secrets.verify("OPENAI_API_KEY", cfg) == {"ok": False, "detail": "OPENAI_API_KEY is not set"}
    monkeypatch.setenv("VOICESTREAM_WEBHOOK_URL", "https://hooks.example/x")
    result = await secrets.verify("VOICESTREAM_WEBHOOK_URL", cfg)  # the webhook sink's own check (no request)
    assert result["ok"] and "hooks.example" in result["detail"]


# --------------------------------------------------------------------------- prefs


def test_prefs_defaults_save_and_validation(tmp_path: Path) -> None:
    path = tmp_path / "app.json"
    p = prefs_mod.load_prefs(path)
    assert (p.setup_done, p.listen_on_launch, p.start_at_login, p.ui_language) == (False, True, False, "auto")
    p = prefs_mod.update_prefs({"ui_language": "fr", "listen_on_launch": False}, path)
    assert prefs_mod.load_prefs(path).ui_language == "fr" and not prefs_mod.load_prefs(path).listen_on_launch
    if os.name == "posix":
        assert _mode(path) == 0o600
    from pydantic import ValidationError

    for bad in ({"ui_language": "de"}, {"setup_done": True}, {"listen_on_launch": "maybe"}):
        with pytest.raises(ValidationError):
            prefs_mod.update_prefs(bad, path)
    path.write_text("{not json", encoding="utf-8")
    assert prefs_mod.load_prefs(path) == prefs_mod.Prefs()  # a broken file never stops the app
    assert prefs_mod.prefs_path() == Path("~/.voicestream/app.json").expanduser()


def test_a_key_the_keychain_refuses_is_asked_for_once_per_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    class Refusing(FakeKeychain):
        """After an update macOS asks again for each item; Deny was clicked for one."""

        def __init__(self) -> None:
            super().__init__()
            self.asked: list[str] = []

        def get_password(self, service: str, name: str) -> str | None:
            self.asked.append(name)
            if name == "HUBSPOT_TOKEN":
                raise PermissionError("User canceled the operation")
            return super().get_password(service, name)

    kc = Refusing()
    monkeypatch.setattr(secrets, "_keyring", lambda: kc)
    monkeypatch.setattr(secrets, "_UNREADABLE", set())
    secrets.load_into_env()
    secrets.load_into_env()  # every settings save reloads: the prompt must not come back each time
    assert kc.asked.count("HUBSPOT_TOKEN") == 1 and secrets.unreadable() == ["HUBSPOT_TOKEN"]
    assert "HUBSPOT_TOKEN" in secrets.unreadable_message("HUBSPOT_TOKEN")
    secrets.set_secret("HUBSPOT_TOKEN", "pat-again")  # pasted again in Settings: this build may read it
    assert secrets.unreadable() == [] and os.environ["HUBSPOT_TOKEN"] == "pat-again"
