"""Adversarial review of the Mac app's backend: each test is a concrete attack or failure, kept as a
regression test next to its fix.

Threats: a page in your browser (DNS rebinding, CSRF), a request that got hold of the token (a bug in
the UI), another account on the Mac, a crafted settings value, two requests at once, quitting while
something restarts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from starlette.routing import Route
from starlette.testclient import TestClient
from test_app_host import _factory, _host, _states, _write_config
from test_app_meetings import Rec, StubHost, saved  # noqa: F401 - the fixture
from test_app_routes import BASE, PLANNING, TOKEN

from voicestream import storage
from voicestream.app import desktop, files
from voicestream.app import secrets as keys
from voicestream.app import settings as settings_mod
from voicestream.app.host import AppHost
from voicestream.app.meetings import Meetings
from voicestream.app.settings import save_changes
from voicestream.config import load_config, load_dotenv
from voicestream.dashboard.app_routes import ASSETS
from voicestream.dashboard.server import TOKEN_HEADER, create_app
from voicestream.models import Item, Meeting
from voicestream.storage import load_meeting

PAST = "2026-10-14_110000-planning"


@pytest.fixture
def host(tmp_path: Path) -> AppHost:
    from test_app_meetings import WED, _save

    h = AppHost(_write_config(tmp_path), process_setup=False, prefs_path=tmp_path / "app.json", assistant_factory=_factory())
    _save(h.cfg, PLANNING, started=WED, title="Q4 planning")
    return h


@pytest.fixture
def client(host: AppHost) -> Iterator[TestClient]:
    with TestClient(create_app(host, TOKEN), base_url=BASE, headers={TOKEN_HEADER: TOKEN}) as c:
        yield c


# ------------------------------------------------------------- every route is guarded


def _concrete(path: str) -> str:
    return (path.replace("{folder}", PLANNING).replace("{item_id}", "A1").replace("{action}", "start")
            .replace("{kind}", "microphone").replace("{name}", "base"))  # fmt: skip


def test_every_route_needs_the_token_and_a_loopback_host(host: AppHost) -> None:
    """Enumerated from the app itself, so a route added later can't forget the guard."""
    app = create_app(host, TOKEN)
    public = {"/", "/app", *ASSETS}  # the pages, their script and style sheet, the fonts
    checked = 0
    with TestClient(app, base_url=BASE) as anonymous:
        for route in app.routes:
            assert isinstance(route, Route)
            for method in sorted((route.methods or set()) - {"HEAD"}):
                path = _concrete(route.path)
                # DNS rebinding: evil.example re-resolved to 127.0.0.1 — refused even with the token
                rebound = anonymous.request(method, path, headers={"host": "evil.example", TOKEN_HEADER: TOKEN}, json={})
                assert rebound.status_code == 403, (method, path)
                if path in public:
                    continue
                assert anonymous.request(method, path, json={}).status_code == 401, (method, path)
                # CSRF: a cross-site form can send a query string but not the token it doesn't know
                assert anonymous.request(method, path + "?token=guess", json={}).status_code == 401, (method, path)
                checked += 1
    assert checked >= 35
    assert host.assistant is None and not host.prefs.setup_done  # nothing happened
    assert "access-control-allow-origin" not in TestClient(app, base_url=BASE).options(
        "/api/app", headers={"origin": "https://evil.example", "access-control-request-method": "GET"}).headers


# ------------------------------------------------- secrets: no variable can be planted


@pytest.mark.parametrize("sep", ["\u2028", "\u2029", "\x1e", "\x1c", "\x85", "\x0b", "\x0c"])
def test_a_key_cannot_smuggle_another_variable_into_the_env_file(client: TestClient, sep: str) -> None:
    """``.env`` files are read with ``splitlines()``, which also splits on these: a value with one of
    them used to come back as two lines, the second setting any variable at all (here
    ANTHROPIC_BASE_URL: the Claude key would go to that server), even after deleting the key."""
    os.environ.pop("ANTHROPIC_BASE_URL", None)
    r = client.put("/api/secrets/NOTION_TOKEN", json={"value": f"secret_abc{sep}ANTHROPIC_BASE_URL=https://evil.example"})
    assert r.status_code == 400 and "secret_abc" not in r.text
    assert not keys.env_file().exists() or "evil" not in keys.env_file().read_text(encoding="utf-8")
    load_dotenv(keys.env_file())
    assert "ANTHROPIC_BASE_URL" not in os.environ and "NOTION_TOKEN" not in os.environ


def test_key_values_never_leave_the_store(client: TestClient, host: AppHost, caplog: pytest.LogCaptureFixture,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    caplog.set_level(logging.DEBUG)
    value = "sk-ant-api03-very-secret-value"

    async def failing(key: str, transport: Any) -> dict[str, Any]:
        raise RuntimeError(f"Illegal header value b'x-api-key: {key}'")  # a library error quoting the key

    monkeypatch.setattr(keys, "_check_anthropic", failing)
    assert client.put("/api/secrets/ANTHROPIC_API_KEY", json={"value": value}).status_code == 200
    seen = [client.get(p).text for p in ("/api/settings", "/api/app", "/api/doctor", "/api/models", "/api/cost")]
    checked = client.post("/api/secrets/ANTHROPIC_API_KEY/test").json()
    assert checked["ok"] is False and "Illegal header value" in checked["detail"]
    seen.append(json.dumps(checked))
    seen += [e.model_dump_json() for e in host.bus.history]
    seen += [r.getMessage() for r in caplog.records]
    assert not any(value in s for s in seen)
    assert value in keys.env_file().read_text(encoding="utf-8")  # the store itself
    if os.name == "posix":  # Windows has no permission bits
        assert oct(keys.env_file().stat().st_mode & 0o777) == "0o600"


def test_a_key_saved_in_one_test_is_gone_in_the_next() -> None:
    """Runs right after the test above, which saved ANTHROPIC_API_KEY through the app: the app writes
    os.environ directly, and the key used to stay set for the rest of the run (later tests then built a real
    Claude client with it, and the doctor test failed depending on the order)."""
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert os.environ["VOICESTREAM_SECRET_STORE"] == "file"  # what the autouse fixture sets is still there


# --------------------------------------------------------- two writes at the same time


def test_concurrent_private_writes_never_mix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both writers used the same temporary file (``.<name>.<pid>.tmp``): one could truncate the other's
    half-written text (a corrupt settings file) and the second ``os.replace`` failed."""
    path = tmp_path / "x.toml"
    barrier = threading.Barrier(2, timeout=5)
    real = os.fsync

    def both_open(fd: int) -> None:  # both temporary files are written before either is moved into place
        real(fd)
        with __import__("contextlib").suppress(threading.BrokenBarrierError):
            barrier.wait()

    monkeypatch.setattr(files.os, "fsync", both_open)
    errors: list[BaseException] = []

    def write(text: str) -> None:
        try:
            files.write_private(path, text)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(t,)) for t in ("A" * 1000, "B" * 10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and path.read_text() in ("A" * 1000, "B" * 10)
    assert [p.name for p in tmp_path.iterdir()] == ["x.toml"]  # no temporary file left behind
    if os.name == "posix":  # Windows has no permission bits
        assert oct(path.stat().st_mode & 0o777) == "0o600"


def _refuse_twice(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """``os.replace`` as Windows answers while the file is open elsewhere or replaced at that moment."""
    real, calls = os.replace, []

    def replace(src: Any, dst: Any) -> None:
        calls.append(str(dst))
        if len(calls) <= 2:
            raise PermissionError(13, "Access is denied")
        real(src, dst)

    monkeypatch.setattr(storage.os, "replace", replace)
    monkeypatch.setattr(storage.time, "sleep", lambda s: None)
    return calls


def test_a_write_on_windows_waits_for_a_reader_to_let_go(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """On Windows a replace fails while the dashboard has notes.md open (or another write replaces the
    same file), and the meeting's notes or the settings were then never written."""
    calls = _refuse_twice(monkeypatch)
    monkeypatch.setattr(storage, "_WINDOWS", True)
    files.write_private(tmp_path / "x.toml", "a = 1\n")
    storage.atomic_write(tmp_path / "notes.md", "# Notes\n")
    assert (tmp_path / "x.toml").read_text() == "a = 1\n" and (tmp_path / "notes.md").read_text() == "# Notes\n"
    assert len(calls) == 4  # x.toml refused twice then moved; notes.md moved at once
    assert sorted(p.name for p in tmp_path.iterdir()) == ["notes.md", "x.toml"]


def test_elsewhere_a_refused_replace_is_final(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _refuse_twice(monkeypatch)
    monkeypatch.setattr(storage, "_WINDOWS", False)
    with pytest.raises(PermissionError):
        files.write_private(tmp_path / "x.toml", "a = 1\n")
    assert len(calls) == 1 and list(tmp_path.iterdir()) == []  # and its temporary file is gone


def test_write_private_never_follows_a_planted_temporary_file(tmp_path: Path) -> None:
    """With a predictable temporary name, another account could plant a symlink there (in a shared
    folder, e.g. VOICESTREAM_CONFIG=/tmp/…) and have us overwrite whatever it points to."""
    victim = tmp_path / "victim"
    victim.write_text("precious")
    for pid in (os.getpid(),):
        (tmp_path / f".x.toml.{pid}.tmp").symlink_to(victim)
    files.write_private(tmp_path / "x.toml", "settings")
    assert victim.read_text() == "precious" and (tmp_path / "x.toml").read_text() == "settings"


def test_concurrent_settings_saves_keep_both_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two PATCH /api/settings at once (they run in worker threads) each read the file, then wrote
    their own version: the first change was lost."""
    path = tmp_path / "voicestream.toml"
    path.write_text('user_name = "Alex"\n', encoding="utf-8")
    real = settings_mod.load_document

    def slow(p: Path) -> Any:
        doc = real(p)
        time.sleep(0.2)  # both have read the file before either writes
        return doc

    monkeypatch.setattr(settings_mod, "load_document", slow)
    threads = [threading.Thread(target=save_changes, args=(path, c)) for c in ({"user_name": "Sam"}, {"budget.daily_usd": 9.0})]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    cfg = load_config(path)
    assert cfg.user_name == "Sam" and cfg.budget.daily_usd == 9.0


def test_concurrent_key_saves_keep_both_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    real = keys._read_file

    def slow() -> list[str]:
        lines = real()
        time.sleep(0.2)
        return lines

    monkeypatch.setattr(keys, "_read_file", slow)
    threads = [threading.Thread(target=keys.set_secret, args=(n, f"v-{n}")) for n in ("NOTION_TOKEN", "LINEAR_API_KEY")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    text = keys.env_file().read_text(encoding="utf-8")
    assert "NOTION_TOKEN=v-NOTION_TOKEN" in text and "LINEAR_API_KEY=v-LINEAR_API_KEY" in text


# --------------------------------------------- crafted values that broke the app later


@pytest.mark.parametrize("field", ["data_dir", "transcription.whisper_cpp.models_dir", "sinks.markdown.todo_file",
                                   "sinks.markdown.notes_dir"])
def test_a_path_of_an_unknown_account_is_refused(client: TestClient, host: AppHost, field: str) -> None:
    """``~Documents/notes`` (a typo for ``~/Documents``) means "the home of the account Documents":
    expanding it raises RuntimeError. Saved, it made every later start of the app crash."""
    before = host.config_path.read_text(encoding="utf-8")
    r = client.patch("/api/settings", json={"changes": {field: "~nosuchaccount1234/x"}})
    assert r.status_code == 400 and r.json()["field"] == field and "~/" in r.json()["error"]
    assert host.config_path.read_text(encoding="utf-8") == before
    assert client.get("/api/app").status_code == 200


def test_only_paths_are_expanded(tmp_path: Path) -> None:
    path = _write_config(tmp_path)
    assert save_changes(path, {"capture.mic.label": "~Moi"}).capture.mic.label == "~Moi"  # a label, not a folder
    assert save_changes(path, {"data_dir": "~/Voicestream"}).data_path == Path("~/Voicestream").expanduser()


@pytest.mark.parametrize("raw", [b"NaN", b"Infinity", b"-Infinity", b"1e400"])
def test_non_finite_numbers_are_refused(client: TestClient, host: AppHost, raw: bytes) -> None:
    """Python's JSON reads NaN/Infinity (and 1e400 overflows to inf). Written to the file, they made
    GET /api/settings and /api/cost fail for good: the Settings screen could no longer open to fix it."""
    before = host.config_path.read_text(encoding="utf-8")
    body = b'{"changes": {"budget.daily_usd": ' + raw + b"}}"
    r = client.patch("/api/settings", content=body, headers={"content-type": "application/json"})
    assert r.status_code == 400 and r.json()["field"] == "budget.daily_usd"
    assert host.config_path.read_text(encoding="utf-8") == before
    assert client.get("/api/settings").status_code == 200 and client.get("/api/cost").status_code == 200


async def test_a_hand_edited_unexpandable_path_doesnt_stop_the_app(tmp_path: Path) -> None:
    path = _write_config(tmp_path)
    path.write_text(path.read_text().replace(f'data_dir = "{(tmp_path / "data").as_posix()}"', 'data_dir = "~nosuchaccount1234/vs"'))
    host = AppHost(path, process_setup=False, prefs_path=tmp_path / "app.json", assistant_factory=_factory())
    assert host.status == "error" and "~/" in (host.error or "")
    assert host.state()["state"] == "error"
    await host.listen_start()  # refused with the same message, not an exception
    assert host.assistant is None and host.status == "error"


async def test_a_preflight_that_raises_is_an_error_state_not_a_crash(tmp_path: Path) -> None:
    host = _host(tmp_path)
    path = host.config_path
    path.write_text(path.read_text().replace('backend = "fake"', 'backend = "whisper-cpp"\n[transcription.whisper_cpp]\n'
                                             'binary = "~nosuchaccount1234/whisper-server"'))
    host.reload_config()
    await host.listen_start()
    assert host.status == "error" and host.assistant is None and host.error


# ---------------------------------------- what Voicestream runs: refused or shown to you


@pytest.mark.parametrize(("field", "value"), [
    ("transcription.whisper_cpp.binary", "/tmp/evil"),
    ("agents.allow_bash", True),
    ("agents.allowed_tools", ["Bash"]),
    ("agents.workspace_root", "/tmp"),
    ("dashboard.host", "0.0.0.0"),
    ("sinks.command.pass_env", ["ANTHROPIC_API_KEY"]),
])  # fmt: skip
def test_settings_that_only_run_programs_cant_be_changed_over_http(client: TestClient, host: AppHost, field: str,
                                                                   value: Any) -> None:
    """No app screen sets these; over HTTP they would turn a leaked token (or a bug in the page) into
    running any program, or into handing your keys to one. They stay editable in the file."""
    before = host.config_path.read_text(encoding="utf-8")
    r = client.patch("/api/settings", json={"changes": {field: value}})
    assert r.status_code == 400 and r.json()["field"] == field and "voicestream.toml" in r.json()["error"]
    assert host.config_path.read_text(encoding="utf-8") == before
    # a whole table can't sneak it in either
    parent, _, leaf = field.rpartition(".")
    r = client.patch("/api/settings", json={"changes": {parent: {leaf: value}}})
    assert r.status_code == 400 and r.json()["field"] == field


def test_a_locked_setting_can_go_back_to_its_default(client: TestClient, host: AppHost) -> None:
    host.config_path.write_text(host.config_path.read_text() + "\n[dashboard]\nhost = \"0.0.0.0\"\n", encoding="utf-8")
    assert client.patch("/api/settings", json={"changes": {"user_name": "Sam"}}).status_code == 200  # untouched: fine
    assert client.patch("/api/settings", json={"changes": {"dashboard.host": None}}).status_code == 200
    assert load_config(host.config_path).dashboard.host == "127.0.0.1"


def test_settings_that_run_things_or_send_data_are_shown_and_logged(client: TestClient, host: AppHost,
                                                                    caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    r = client.patch("/api/settings", json={"changes": {
        "sinks.command.enabled": True,
        "sinks.command.item_command": ["/usr/local/bin/promethee", "add", "{title}", "--token=abc123"],
        "transcription.openai.base_url": "https://proxy.example/v1/secret-path",
        "sinks.github.token_env": "MY_GH_TOKEN",
        "user_name": "Sam",
    }})  # fmt: skip
    assert r.status_code == 200
    review = {x["field"]: x["value"] for x in r.json()["review"]}
    assert set(review) == {"sinks.command.item_command", "transcription.openai.base_url", "sinks.github.token_env"}
    assert review["sinks.command.item_command"].startswith("/usr/local/bin/promethee")
    assert "abc123" not in json.dumps(review) and "secret-path" not in json.dumps(review)  # program and server only
    assert review["transcription.openai.base_url"] == "https://proxy.example/…"
    changed = [e for e in host.bus.history if e.type == "settings.changed"][-1]
    assert [x["field"] for x in changed.data["review"]] == [x["field"] for x in r.json()["review"]]
    # a voicestream WARNING: the log file, and the app's warning banner (logs.WarningMirror)
    warned = [rec.getMessage() for rec in caplog.records if rec.levelno == logging.WARNING and rec.name.startswith("voicestream.")]
    assert any("sinks.command.item_command" in m and "promethee" in m and "abc123" not in m for m in warned)
    assert r.json()["values"]["sinks"]["command"]["item_command"][0] == "/usr/local/bin/promethee"  # the full value is in values
    # nothing to review: nothing said
    caplog.clear()
    assert client.patch("/api/settings", json={"changes": {"user_name": "Alex"}}).json()["review"] == []
    assert not [rec for rec in caplog.records if rec.levelno >= logging.WARNING]


async def test_the_review_reaches_the_apps_warning_banner(tmp_path: Path) -> None:
    import httpx

    host = _host(tmp_path)
    url = await host.start()  # the real server, and the WARNING mirror the app installs
    try:
        async with httpx.AsyncClient(base_url=url.partition("/app?")[0], headers={TOKEN_HEADER: host.token}) as http:
            r = await http.patch("/api/settings", json={"changes": {"sinks.markdown.todo_file": "~/.zshrc"}})
        assert r.status_code == 200 and r.json()["review"] == [{"field": "sinks.markdown.todo_file", "value": "~/.zshrc"}]
        assert any("sinks.markdown.todo_file = ~/.zshrc" in w for w in host.state()["warnings"])
        assert any(e.type == "app.warning" and "todo_file" in e.data["message"] for e in host.bus.history)
    finally:
        await host.shutdown(force=True)


# ------------------------------------------------------ start / stop / restart / quit


async def test_a_settings_change_during_stop_doesnt_start_listening_again(tmp_path: Path) -> None:
    """You pressed Stop; the wrap-up takes a while; meanwhile a setting or a key is saved: the restart
    used to wait for the stop and then turn the microphone back on."""
    host = _host(tmp_path)
    await host.listen_start()
    stop = asyncio.ensure_future(host.listen_stop())
    await asyncio.sleep(0)
    assert host.status == "stopping"
    save_changes(host.config_path, {"user_name": "Sam"})
    assert await host.apply_settings() == {"restarting": False, "restart_pending": False}
    await stop
    await asyncio.sleep(0.1)
    assert host.assistant is None and host.status == "stopped" and host.cfg.user_name == "Sam"


async def test_stop_pressed_during_a_restart_stays_stopped(tmp_path: Path) -> None:
    """ "Apply now" ends the meeting (summary, pushes: it can take a minute) and starts again; a Stop
    pressed meanwhile used to be undone as soon as the wrap-up finished."""
    host = _host(tmp_path)
    await host.listen_start()
    restart = asyncio.ensure_future(host.listen_restart())
    await asyncio.sleep(0)
    assert host.status == "stopping"
    await host.listen_stop()
    await restart
    assert host.assistant is None and host.status == "stopped"
    await host.listen_restart()  # a later restart (or Start) works as before
    assert host.status == "listening"
    await host.listen_stop()


async def test_quitting_during_a_restart_doesnt_start_another_assistant(tmp_path: Path) -> None:
    """A restart in flight (a settings change, a pending one after the meeting) went on to build and
    start a new assistant — microphone, whisper-server — while the app was quitting."""
    built: list[Any] = []
    host = _host(tmp_path, factory=_factory(built=built))
    await host.start()
    await host.listen_start()
    await host.apply_settings(wait=False)  # restarts in the background
    await asyncio.sleep(0)
    await host.shutdown()
    assert len(built) == 1 and host.assistant is None
    assert _states(host)[-2:] == ["stopping", "stopped"]
    await host.listen_start()  # e.g. a menu click that arrives during quit
    assert host.assistant is None and len(built) == 1


# ------------------------------------------- past meetings: one copy while work is in flight


async def test_an_edit_during_a_background_sync_is_not_lost(cfg: Any, saved: dict[str, Path],  # noqa: F811
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """Editing a pushed item syncs it in the background; that sync then saved *its* copy of the meeting,
    undoing any edit made meanwhile."""
    gate = asyncio.Event()

    class Slow(Rec):
        async def update_item(self, item: Item, meeting: Meeting, ref: str) -> None:
            await gate.wait()
            await super().update_item(item, meeting, ref)

    rec = Slow(cfg)
    monkeypatch.setattr("voicestream.sinks.base.build_sinks", lambda app, only=None: [rec])
    host = StubHost(cfg)
    m = Meetings(host)
    m.edit_item(PAST, "A2", status="done")  # pushed: synced in the background
    await asyncio.sleep(0.05)
    m.edit_item(PAST, "A1", title="Send the new deck")
    m.add_item(PAST, "action", "Call the venue")
    assert m.detail(PAST)["meeting"]["items"]["A1"]["title"] == "Send the new deck"
    gate.set()
    await asyncio.gather(*host.tasks)
    disk = load_meeting(saved["old"])[0]
    assert disk.items["A1"].title == "Send the new deck" and disk.items["A2"].status.value == "done"
    assert any(i.title == "Call the venue" for i in disk.items.values())
    assert rec.calls == [("update", "A2")]
    assert not m._held  # nothing kept once the work is done: the next read is from disk again


async def test_two_pushes_of_a_past_meeting_create_once(cfg: Any, saved: dict[str, Path],  # noqa: F811
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """A double-click on "Push to my tools" ran two pushes, each on its own copy: two tasks in Notion."""

    class Slow(Rec):
        async def push_item(self, item: Item, meeting: Meeting) -> str:
            await asyncio.sleep(0.1)
            return await super().push_item(item, meeting)

    rec = Slow(cfg)
    monkeypatch.setattr("voicestream.sinks.base.build_sinks", lambda app, only=None: [rec])
    m = Meetings(StubHost(cfg))
    first, second = await asyncio.gather(m.push(PAST, notes=False), m.push(PAST, notes=False))
    assert rec.calls.count(("create", "A1")) == 1
    assert first["items"] == {"A1": {"rec": "rec:A1"}} and second["count"] == 0
    assert load_meeting(saved["old"])[0].items["A1"].pushed == {"rec": "rec:A1"}


async def test_an_edit_during_a_push_is_kept(cfg: Any, saved: dict[str, Path],  # noqa: F811
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    gate = asyncio.Event()

    class Slow(Rec):
        async def push_item(self, item: Item, meeting: Meeting) -> str:
            await gate.wait()
            return await super().push_item(item, meeting)

    rec = Slow(cfg)
    monkeypatch.setattr("voicestream.sinks.base.build_sinks", lambda app, only=None: [rec])
    m = Meetings(StubHost(cfg))
    push = asyncio.ensure_future(m.push(PAST, notes=False))
    await asyncio.sleep(0.05)
    m.edit_item(PAST, "D1", title="Annual billing, from January")
    gate.set()
    await push
    disk = load_meeting(saved["old"])[0]
    assert disk.items["D1"].title == "Annual billing, from January" and disk.items["A1"].pushed == {"rec": "rec:A1"}


# --------------------------------------------------------------------------- desktop


def test_revealing_a_folder_never_launches_an_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``open X.app`` starts the app rather than showing the folder (data_dir can be set in Settings)."""
    monkeypatch.setattr(desktop.sys, "platform", "darwin")
    bundle = tmp_path / "Evil.app"
    bundle.mkdir()
    assert desktop.reveal_command(bundle) == ["open", "-R", str(bundle)]
    plain = tmp_path / "data"
    plain.mkdir()
    assert desktop.reveal_command(plain) == ["open", str(plain)]
