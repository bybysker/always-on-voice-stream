"""Apple Reminders, Shortcuts and command sinks, against fake processes.

osascript can't run here, so the Reminders script runs under node against a small
JS model of Reminders' scripting objects (``HARNESS``). Set
``VOICESTREAM_TEST_REMINDERS=1`` on a Mac to also drive the real app.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import textwrap
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from voicestream.config import Config
from voicestream.dispatch import Dispatcher
from voicestream.models import AgentRun, Item, ItemKind, ItemStatus, Meeting
from voicestream.sinks import _proc
from voicestream.sinks._proc import Result
from voicestream.sinks.base import SinkError, build_sinks
from voicestream.sinks.command import (
    SHORTCUTS,
    CommandConfig,
    CommandSink,
    ShortcutsConfig,
    ShortcutsSink,
)
from voicestream.sinks.reminders import (
    OSASCRIPT,
    PERMISSION_HELP,
    SCRIPT,
    RemindersConfig,
    RemindersSink,
)

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is needed to run the JXA script against the mock")

NASTY = [
    'He said "ship it"',
    'back\\slash \\" and a literal \\n',
    "line one\nline two\r\nthree\tfour",
    '" & do shell script "rm -rf ~" & "',
    "'); Application('Finder').doShellScript('rm -rf ~'); ('",
    "`rm -rf ~` $(rm -rf ~) ${HOME} %s %n {title} {json_path}",
    "Réunion déjà prévue — ça marche ? 👍 « guillemets » ﬁ",
    "-rf --help",
    "</script><b>&amp;   ",
]


def action(**kw: Any) -> Item:
    fields: dict[str, Any] = {
        "id": "A1",
        "kind": ItemKind.ACTION,
        "title": "Envoyer le devis à Sophie",
        "detail": "Version révisée, avec la remise de 10 %.",
        "owner": "Alex",
        "due_date": "2026-10-16",
        "due_text": "vendredi",
        "priority": "high",
        "confidence": 0.9,
        "evidence": "Alex, tu peux envoyer le devis d'ici vendredi ?",
    }
    return Item(**(fields | kw))


@dataclass
class Call:
    argv: list[str]
    stdin: bytes | None
    env: dict[str, str] | None
    timeout: float
    input: Any = None
    """Shortcuts only: the payload file's JSON, read while the run was in progress."""


Reply = Result | Exception | Callable[[Call], Result]


class FakeRunner:
    """Stands in for ``_proc.run``: records each call and answers from ``replies`` in order."""

    def __init__(self, *replies: Reply) -> None:
        self.replies = list(replies)
        self.calls: list[Call] = []

    async def __call__(self, argv: list[str], *, timeout: float, stdin: bytes | None = None, env: dict[str, str] | None = None) -> Result:
        call = Call(list(argv), stdin, dict(env) if env is not None else None, timeout)
        self.calls.append(call)
        reply = self.replies.pop(0) if self.replies else Result(0, "", "")
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            return reply(call)
        return reply

    @property
    def request(self) -> dict[str, Any]:
        """The JSON the last osascript call got on stdin."""
        stdin = self.calls[-1].stdin
        assert stdin is not None
        return json.loads(stdin)


def ok(stdout: str = "") -> Result:
    return Result(0, stdout, "")


@pytest.fixture
def on_mac(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_proc, "require_macos", lambda what: None)


def test_registered_and_built_without_touching_anything(cfg: Config) -> None:
    cfg.sinks = {
        "reminders": {"enabled": True},
        "shortcuts": {"enabled": True, "item_shortcut": "Ajouter à Promethee"},
        "command": {"enabled": True, "item_command": "open 'promethee://add?title={title_url}'"},
    }
    sinks = {s.name: s for s in build_sinks(cfg)}
    assert isinstance(sinks["reminders"], RemindersSink)
    assert isinstance(sinks["shortcuts"], ShortcutsSink)
    assert isinstance(sinks["command"], CommandSink)
    assert sinks["command"].cfg.item_command == ["open", "promethee://add?title={title_url}"]


# ================================================================== Reminders


def reminders(cfg: Config, *replies: Reply, **kw: Any) -> tuple[RemindersSink, FakeRunner]:
    sink = RemindersSink(RemindersConfig(enabled=True, **kw), cfg)
    sink.run = runner = FakeRunner(*replies)
    return sink, runner


async def test_reminders_needs_macos(cfg: Config, meeting: Meeting, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    sink, runner = reminders(cfg)
    for attempt in (sink.push_item(action(), meeting), sink.update_item(action(), meeting, "x"), sink.check()):
        with pytest.raises(SinkError, match="Apple Reminders needs macOS") as exc:
            await attempt
        assert not exc.value.retryable
    assert runner.calls == []


@pytest.mark.parametrize("title", NASTY)
async def test_reminders_script_is_constant_and_text_only_travels_as_json(cfg: Config, meeting: Meeting, on_mac: None, title: str) -> None:
    sink, runner = reminders(cfg, ok('{"id":"x-apple-reminder://5F0C1F7E"}\n'))
    item = action(title=title, detail=title, evidence=title)
    meeting.title = title

    assert await sink.push_item(item, meeting) == "x-apple-reminder://5F0C1F7E"
    call = runner.calls[0]
    assert call.argv == [OSASCRIPT, "-l", "JavaScript", "-e", SCRIPT]
    assert call.stdin is not None and call.stdin.isascii()
    req = runner.request
    assert req["name"] == " ".join(title.split())
    assert "\n" not in req["name"] and "\r" not in req["name"]
    assert req["body"].startswith(title + "\n\n")
    assert f"“{title}”" in req["body"]
    assert f"voicestream · {title} · " in req["body"]


def test_reminders_script_snapshot() -> None:
    # the data comes in on stdin as UTF-8 JSON; nothing is ever spliced into the script
    assert "$.NSFileHandle.fileHandleWithStandardInput.readDataToEndOfFile" in SCRIPT
    assert "initWithDataEncoding(input, $.NSUTF8StringEncoding)" in SCRIPT
    assert "JSON.parse(" in SCRIPT
    assert 'Application("Reminders")' in SCRIPT
    # dates from numbers, never parsed from (locale-dependent) text
    assert "new Date(y, m - 1, d, hh, mm)" in SCRIPT
    assert "new Date(y, m - 1, d)" in SCRIPT
    assert "app.reminders.byId(req.id)" in SCRIPT
    for dangerous in ("doShellScript", "eval(", "Function(", "do shell script", "date \"", "%"):
        assert dangerous not in SCRIPT
    # output is ASCII-escaped so osascript's stdout encoding can't mangle it
    assert r'.replace(/[^\x20-\x7e]/g' in SCRIPT


async def test_reminders_request_fields(cfg: Config, meeting: Meeting, on_mac: None) -> None:
    sink, runner = reminders(cfg, ok('{"id":"r1"}'), owners="all", list_name="Réunions", due_time="08:30")
    item = action(owner="Sophie Martin", agent=AgentRun(prompt="draft", result_summary="Brouillon prêt"))
    await sink.push_item(item, meeting)
    req = runner.request
    assert req | {"body": None} == {
        "op": "create",
        "list": "Réunions",
        "id": None,
        "name": "Envoyer le devis à Sophie",
        "body": None,
        "priority": 1,
        "due": [2026, 10, 16, 8, 30],
        "completed": False,
    }
    body = req["body"]
    assert body.split("\n\n")[:4] == [
        "Version révisée, avec la remise de 10 %.",
        "@Sophie Martin",
        "“Alex, tu peux envoyer le devis d'ici vendredi ?”",
        "🤖 Brouillon prêt",
    ]
    assert body.endswith(f"voicestream · Weekly sync · {meeting.started_at.astimezone():%Y-%m-%d %H:%M}")
    assert "**" not in body and "vendredi\n" not in body  # plain text; due phrase only when no date


@pytest.mark.parametrize(
    ("fields", "cfg_kw", "due", "body_has"),
    [
        ({}, {"due_time": None}, [2026, 10, 16], None),
        ({"due_date": None}, {}, None, "⏰ vendredi"),
        ({"due_date": "16/10/2026"}, {}, None, None),
        ({"priority": "low"}, {"priority": {"low": 6}}, [2026, 10, 16, 9, 0], None),
    ],
)
async def test_reminders_due_and_priority(cfg: Config, meeting: Meeting, on_mac: None, fields: dict[str, Any], cfg_kw: dict[str, Any], due: list[int] | None, body_has: str | None) -> None:
    sink, runner = reminders(cfg, ok('{"id":"r1"}'), **cfg_kw)
    await sink.push_item(action(**fields), meeting)
    assert runner.request["due"] == due
    if body_has:
        assert body_has in runner.request["body"]
    if "priority" in fields:
        assert runner.request["priority"] == 6


async def test_reminders_no_priority_is_left_alone(cfg: Config, meeting: Meeting, on_mac: None) -> None:
    sink, runner = reminders(cfg, ok('{"id":"r1"}'))
    await sink.push_item(action(priority=None), meeting)
    assert runner.request["priority"] is None


def test_reminders_config_validation() -> None:
    for bad in ({"due_time": "9h"}, {"due_time": "24:00"}, {"priority": {"high": 10}}, {"priority": {"urgent": 1}}, {"list_name": ""}):
        with pytest.raises(ValidationError):
            RemindersConfig.model_validate(bad)


def test_reminders_accepts_your_open_items(cfg: Config) -> None:
    sink, _ = reminders(cfg)
    assert sink.accepts(action(owner="Alex"), 0.6)
    assert sink.accepts(action(owner="me"), 0.6)
    assert not sink.accepts(action(owner="Sophie"), 0.6)
    assert not sink.accepts(action(owner="Assistant"), 0.6)
    assert not sink.accepts(action(status=ItemStatus.CANCELLED), 0.6)
    assert sink.accepts(action(status=ItemStatus.DONE), 0.6)


@pytest.mark.parametrize(
    ("change", "op", "completed"),
    [
        ({"title": "Envoyer le devis signé"}, "update", False),
        ({"status": ItemStatus.DONE}, "update", True),
        ({"status": ItemStatus.CANCELLED}, "delete", False),
        ({"dismissed": True}, "delete", False),
    ],
)
async def test_reminders_update(cfg: Config, meeting: Meeting, on_mac: None, change: dict[str, Any], op: str, completed: bool) -> None:
    sink, runner = reminders(cfg, ok('{"id":"x-apple-reminder://R"}'))
    await sink.update_item(action(**change), meeting, "x-apple-reminder://R")
    assert runner.request["op"] == op
    assert runner.request["id"] == "x-apple-reminder://R"
    assert runner.request["completed"] is completed
    if "title" in change:
        assert runner.request["name"] == "Envoyer le devis signé"


async def test_reminders_update_of_a_deleted_reminder_is_a_no_op(cfg: Config, meeting: Meeting, on_mac: None, caplog: pytest.LogCaptureFixture) -> None:
    sink, _ = reminders(cfg, ok('{"missing":true}'))
    with caplog.at_level(logging.INFO):
        await sink.update_item(action(), meeting, "x-apple-reminder://gone")
    assert "is gone" in caplog.text


@pytest.mark.parametrize(
    ("stderr", "retryable", "match"),
    [
        ("0:1: execution error: Error: Error: Not authorized to send Apple events to Reminders. (-1743)\n", False, "Privacy & Security → Automation"),
        ("execution error: Error: The Apple event has timed out. (-1712)", True, "timed out"),
        ("execution error: Error: Application isn’t running. (-600)", True, "running"),
        ("execution error: TypeError: undefined is not an object (-2700)", False, "TypeError"),
        ("something odd", False, "something odd"),
    ],
)
async def test_reminders_osascript_errors(cfg: Config, meeting: Meeting, on_mac: None, stderr: str, retryable: bool, match: str) -> None:
    sink, _ = reminders(cfg, Result(1, "", stderr))
    with pytest.raises(SinkError, match=match) as exc:
        await sink.update_item(action(), meeting, "x-apple-reminder://R")
    assert exc.value.retryable is retryable
    if "-1743" in stderr:
        assert str(exc.value) == PERMISSION_HELP
        assert "tccutil reset AppleEvents" in PERMISSION_HELP

    # a create that failed this way may still have made the reminder: never retried
    sink, _ = reminders(cfg, Result(1, "", stderr))
    with pytest.raises(SinkError, match=match) as exc:
        await sink.push_item(action(), meeting)
    assert not exc.value.retryable
    assert ("isn't retried" in str(exc.value)) is retryable


@pytest.mark.parametrize("stdout", ["", "not json", "[1]", '{"id": 42}', '{"id": ""}'])
async def test_reminders_bad_replies(cfg: Config, meeting: Meeting, on_mac: None, stdout: str) -> None:
    sink, _ = reminders(cfg, ok(stdout))
    with pytest.raises(SinkError) as exc:
        await sink.push_item(action(), meeting)
    assert not exc.value.retryable


async def test_reminders_timeout_retries_updates_but_never_a_create(cfg: Config, meeting: Meeting, on_mac: None) -> None:
    timed_out = SinkError("osascript timed out after 12s", retryable=True)
    sink, runner = reminders(cfg, timed_out, timed_out, timeout_s=12)
    with pytest.raises(SinkError) as exc:
        await sink.update_item(action(), meeting, "x-apple-reminder://R")
    assert exc.value.retryable
    with pytest.raises(SinkError, match="It may have gone through anyway, so it isn't retried") as exc:
        await sink.push_item(action(), meeting)
    assert not exc.value.retryable
    assert [c.timeout for c in runner.calls] == [12, 12]


async def test_reminders_a_timed_out_create_is_not_repeated_by_the_dispatcher(cfg: Config, meeting: Meeting, on_mac: None) -> None:
    # Reminders made the reminder, but osascript was killed before it answered
    sink, runner = reminders(cfg, SinkError("osascript timed out after 60s", retryable=True), ok('{"id":"x-apple-reminder://2"}'))
    item = meeting.items["A1"] = action()
    dispatcher = Dispatcher(cfg, meeting, [sink])
    for _ in range(3):
        assert await dispatcher.sync(require_stable=False) == 0
    assert len(runner.calls) == 1 and item.pushed == {}


async def test_reminders_check(cfg: Config, on_mac: None) -> None:
    sink, runner = reminders(cfg, ok('{"list":false,"lists":3}'), list_name="Réunions")
    line = await sink.check()
    assert runner.request == {"op": "check", "list": "Réunions"}
    assert line == "Reminders reachable (3 lists) · new reminders go to “Réunions” (created on first push) · only items you own"
    sink, _ = reminders(cfg, ok('{"list":true,"lists":3}'), owners="all")
    assert (await sink.check()).endswith("go to “Meetings”")


async def test_reminders_through_the_dispatcher(cfg: Config, meeting: Meeting, on_mac: None) -> None:
    sink, runner = reminders(cfg, ok('{"id":"x-apple-reminder://R"}'), ok('{"id":"x-apple-reminder://R"}'))
    item = meeting.items["A1"] = action()
    dispatcher = Dispatcher(cfg, meeting, [sink])
    assert await dispatcher.sync(require_stable=False) == 1
    assert item.pushed == {"reminders": "x-apple-reminder://R"}
    item.status, item.revision = ItemStatus.DONE, 2
    assert await dispatcher.sync(require_stable=False) == 1
    assert runner.request["op"] == "update" and runner.request["completed"] is True


# --------------------------------------------- the script itself, under node

HARNESS = r"""
"use strict";
// A small model of Reminders.app as JXA sees it, so the sink's script can run under node.
const fs = require("fs");
const [scriptPath, statePath] = process.argv.slice(2);
const state = JSON.parse(fs.readFileSync(statePath, "utf8"));

function aeError(message, number) {
  const e = new Error(message);
  e.errorNumber = number;
  return e;
}

function local(d) {
  return [d.getFullYear(), d.getMonth() + 1, d.getDate(), d.getHours(), d.getMinutes()];
}

// An object specifier: reading a property is a call (r.name()), assigning one sets it.
function specifier(resolve, extra = {}) {
  return new Proxy({}, {
    get(_, prop) {
      if (prop in extra) return extra[prop];
      if (prop === "exists") return () => resolve() !== null;
      return () => {
        const target = resolve();
        if (target === null) throw aeError("Can't get object.", -1728);
        return target[prop];
      };
    },
    set(_, prop, value) {
      const target = resolve();
      if (target === null) throw aeError("Can't get object.", -1728);
      target[prop] = value instanceof Date ? local(value) : value;
      return true;
    },
  });
}

// What a class constructor returns; push() makes the object and binds the specifier to it.
function newObject(kind, props) {
  let made = null;
  return specifier(() => made, { kind, props, bind: (obj) => { made = obj; } });
}

function findReminder(id) {
  for (const list of state.lists) for (const r of list.reminders) if (r.id === id) return r;
  return null;
}

function remindersApp() {
  if (state.denied) throw aeError("Error: Not authorized to send Apple events to Reminders.", -1743);
  const listNamed = (name) => state.lists.find((l) => l.name === name) || null;
  return {
    List: (props) => newObject("list", props),
    Reminder: (props) => newObject("reminder", props),
    lists: {
      get length() { return state.lists.length; },
      byName: (name) => specifier(() => listNamed(name), {
        reminders: {
          push(obj) {
            const list = listNamed(name);
            if (list === null) throw aeError("Can't get object.", -1728);
            if (obj.kind !== "reminder") throw aeError("Can't make that.", -2710);
            const r = { id: "x-apple-reminder://" + (state.idPrefix || "") + ++state.next, priority: 0, completed: false, ...obj.props };
            list.reminders.push(r);
            obj.bind(r);
          },
        },
      }),
      push(obj) {
        if (obj.kind !== "list") throw aeError("Can't make that.", -2710);
        const list = { name: obj.props.name, reminders: [] };
        state.lists.push(list);
        obj.bind(list);
      },
    },
    reminders: { byId: (id) => specifier(() => findReminder(id)) },
    delete(spec) {
      const id = spec.id();
      for (const list of state.lists) list.reminders = list.reminders.filter((r) => r.id !== id);
    },
  };
}

globalThis.ObjC = { import() {} };
globalThis.$ = {
  NSUTF8StringEncoding: 4,
  NSFileHandle: { fileHandleWithStandardInput: { get readDataToEndOfFile() { return fs.readFileSync(0); } } },
  NSString: {
    alloc: {
      initWithDataEncoding(data, encoding) {
        if (encoding !== 4) throw new Error("expected NSUTF8StringEncoding");
        return { js: data.toString("utf8") };
      },
    },
  },
};
globalThis.Application = (name) => {
  if (name !== "Reminders") throw new Error("unexpected app " + name);
  return remindersApp();
};

try {
  const result = new Function(fs.readFileSync(scriptPath, "utf8") + "\nreturn run();")();
  fs.writeFileSync(statePath, JSON.stringify(state));
  process.stdout.write(String(result) + "\n");
} catch (e) {
  process.stderr.write("execution error: Error: " + e.message + " (" + (e.errorNumber ?? -2700) + ")\n");
  process.exit(1);
}
"""


class MockReminders:
    """A runner that executes the sink's osascript command under node, against ``HARNESS``."""

    def __init__(self, tmp_path: Path, **state: Any) -> None:
        self.dir = tmp_path
        (tmp_path / "harness.js").write_text(HARNESS, encoding="utf-8")
        self.state_path = tmp_path / "state.json"
        self.state_path.write_text(json.dumps({"lists": [], "next": 0} | state), encoding="utf-8")
        self.stdout: list[str] = []

    @property
    def state(self) -> dict[str, Any]:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    async def __call__(self, argv: list[str], *, timeout: float, stdin: bytes | None = None, env: dict[str, str] | None = None) -> Result:
        assert argv[:4] == [OSASCRIPT, "-l", "JavaScript", "-e"] and len(argv) == 5
        script = self.dir / "script.js"
        script.write_text(argv[4], encoding="utf-8")
        assert NODE is not None
        res = await _proc.run(
            [NODE, str(self.dir / "harness.js"), str(script), str(self.state_path)],
            stdin=stdin,
            timeout=timeout,
            env={"PATH": os.environ.get("PATH", ""), "TZ": "Europe/Amsterdam"},
        )
        self.stdout.append(res.stdout)
        return res


def mocked(cfg: Config, tmp_path: Path, state: dict[str, Any] | None = None, **kw: Any) -> tuple[RemindersSink, MockReminders]:
    sink = RemindersSink(RemindersConfig(enabled=True, **kw), cfg)
    sink.run = mock = MockReminders(tmp_path, **(state or {}))
    return sink, mock


@needs_node
async def test_jxa_lifecycle(cfg: Config, meeting: Meeting, tmp_path: Path, on_mac: None) -> None:
    sink, mock = mocked(cfg, tmp_path, list_name="Réunions")
    item = action(title=NASTY[3], detail=NASTY[6])

    ref = await sink.push_item(item, meeting)
    assert ref == "x-apple-reminder://1"
    [lst] = mock.state["lists"]
    assert lst["name"] == "Réunions"
    [r] = lst["reminders"]
    assert r["name"] == NASTY[3]
    assert r["body"].startswith(NASTY[6] + "\n\n")
    assert r["priority"] == 1
    assert r["dueDate"] == r["remindMeDate"] == [2026, 10, 16, 9, 0]
    assert r["completed"] is False

    item.title, item.priority, item.due_date, item.status = NASTY[4], "medium", "2026-10-30", ItemStatus.DONE
    await sink.update_item(item, meeting, ref)
    [r] = mock.state["lists"][0]["reminders"]
    assert (r["name"], r["priority"], r["dueDate"], r["completed"]) == (NASTY[4], 5, [2026, 10, 30, 9, 0], True)

    item.status = ItemStatus.CANCELLED
    await sink.update_item(item, meeting, ref)
    assert mock.state["lists"][0]["reminders"] == []
    await sink.update_item(item, meeting, ref)  # already gone: no error
    assert mock.stdout[-1].strip() == '{"missing":true}'


@needs_node
async def test_jxa_existing_list_all_day_and_done_on_create(cfg: Config, meeting: Meeting, tmp_path: Path, on_mac: None) -> None:
    sink, mock = mocked(cfg, tmp_path, {"lists": [{"name": "Meetings", "reminders": []}]}, due_time=None)
    await sink.push_item(action(priority=None, status=ItemStatus.DONE), meeting)
    [lst] = mock.state["lists"]
    [r] = lst["reminders"]
    assert r["alldayDueDate"][:3] == [2026, 10, 16]
    assert "dueDate" not in r and "remindMeDate" not in r
    assert r["priority"] == 0 and r["completed"] is True


@needs_node
async def test_jxa_output_is_ascii_and_decodes(cfg: Config, meeting: Meeting, tmp_path: Path, on_mac: None) -> None:
    sink, mock = mocked(cfg, tmp_path, {"idPrefix": "é👍 /"})
    ref = await sink.push_item(action(), meeting)
    assert ref == "x-apple-reminder://é👍 /1"
    assert mock.stdout[0].isascii()


@needs_node
async def test_jxa_check_is_read_only(cfg: Config, tmp_path: Path, on_mac: None) -> None:
    sink, mock = mocked(cfg, tmp_path, {"lists": [{"name": "Courses", "reminders": []}]})
    assert "(1 lists)" in await sink.check() and "created on first push" in await sink.check()
    assert mock.state["lists"] == [{"name": "Courses", "reminders": []}]


@needs_node
async def test_jxa_permission_denied(cfg: Config, meeting: Meeting, tmp_path: Path, on_mac: None) -> None:
    sink, _ = mocked(cfg, tmp_path, {"denied": True})
    with pytest.raises(SinkError) as exc:
        await sink.push_item(action(), meeting)
    assert str(exc.value) == PERMISSION_HELP and not exc.value.retryable


@pytest.mark.skipif(
    sys.platform != "darwin" or not os.environ.get("VOICESTREAM_TEST_REMINDERS"),
    reason="opt-in: set VOICESTREAM_TEST_REMINDERS=1 on a Mac (creates and deletes a reminder in list 'voicestream-test')",
)
async def test_real_reminders_roundtrip(cfg: Config, meeting: Meeting) -> None:
    sink = RemindersSink(RemindersConfig(enabled=True, list_name="voicestream-test", owners="all"), cfg)
    item = action(title=NASTY[3] + " " + NASTY[6], detail="\n".join(NASTY))
    ref = await sink.push_item(item, meeting)
    assert ref.startswith("x-apple-reminder://")
    item.status = ItemStatus.DONE
    await sink.update_item(item, meeting, ref)
    item.status = ItemStatus.CANCELLED
    await sink.update_item(item, meeting, ref)
    assert await sink._osa(sink._request("update", item, meeting, ref)) == {"missing": True}


# ================================================================== Shortcuts


def shortcuts(cfg: Config, *replies: Reply, **kw: Any) -> tuple[ShortcutsSink, FakeRunner]:
    sink = ShortcutsSink(ShortcutsConfig(enabled=True, **kw), cfg)
    sink.run = runner = FakeRunner(*replies)
    return sink, runner


def shortcut_ran(output: str | None = "promethee://task/7\n", code: int = 0, stderr: str = "") -> Callable[[Call], Result]:
    """A reply that reads the payload file and writes ``output`` like a shortcut ending in Stop and Output."""

    def reply(call: Call) -> Result:
        argv = call.argv
        call.input = json.loads(Path(argv[argv.index("--input-path") + 1]).read_bytes())
        if output is not None:
            Path(argv[argv.index("--output-path") + 1]).write_text(output, encoding="utf-8")
        return Result(code, "", stderr)

    return reply


async def test_shortcuts_needs_macos(cfg: Config, meeting: Meeting, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    sink, runner = shortcuts(cfg, item_shortcut="Ajouter")
    for attempt in (sink.push_item(action(), meeting), sink.check()):
        with pytest.raises(SinkError, match="Shortcuts needs macOS"):
            await attempt
    assert runner.calls == []


@pytest.mark.parametrize("title", NASTY)
async def test_shortcuts_run(cfg: Config, meeting: Meeting, on_mac: None, title: str) -> None:
    name = 'Ajouter à "Promethee" ; $(id)'
    sink, runner = shortcuts(cfg, shortcut_ran("\n  promethee://task/7  \nsecond line\n"), item_shortcut=name, timeout_s=20)
    meeting.language = "fr"
    item = action(title=title)

    assert await sink.push_item(item, meeting) == "promethee://task/7"
    call = runner.calls[0]
    src, out = call.argv[4], call.argv[6]
    assert call.argv == [SHORTCUTS, "run", name, "--input-path", src, "--output-path", out, "--output-type", "public.plain-text"]
    assert call.timeout == 20
    assert not Path(src).exists() and not Path(out).parent.exists()  # cleaned up
    assert all(title not in arg for arg in call.argv)
    assert call.input["event"] == "item.created"
    assert call.input["item"]["title"] == title
    assert call.input["item"] == json.loads(item.model_dump_json())
    assert call.input["meeting"]["language"] == "fr"
    assert set(call.input["meeting"]) == {"id", "title", "started_at", "ended_at", "participants", "language", "tldr"}
    assert "ref" not in call.input and "notes_markdown" not in call.input


@pytest.mark.parametrize("output", [None, "", "  \n\n"])
async def test_shortcuts_fallback_ref(cfg: Config, meeting: Meeting, on_mac: None, output: str | None) -> None:
    sink, _ = shortcuts(cfg, shortcut_ran(output), item_shortcut="Ajouter")
    assert await sink.push_item(action(), meeting) == "shortcut:Ajouter:A1"


async def test_shortcuts_payload_file_is_ascii_json(cfg: Config, meeting: Meeting, on_mac: None) -> None:
    raw: list[bytes] = []

    def reply(call: Call) -> Result:
        raw.append(Path(call.argv[4]).read_bytes())
        return ok()

    sink, _ = shortcuts(cfg, reply, item_shortcut="Ajouter")
    await sink.push_item(action(title=NASTY[6]), meeting)
    assert raw[0].isascii() and json.loads(raw[0])["item"]["title"] == NASTY[6]


async def test_shortcuts_update_and_notes(cfg: Config, meeting: Meeting, on_mac: None) -> None:
    sink, runner = shortcuts(cfg, shortcut_ran(), shortcut_ran("notes://1"), update_shortcut="Mettre à jour", notes_shortcut="Compte rendu")
    assert sink.supports_update and sink.supports_notes
    assert not sink.accepts(action(), 0.0)  # no item_shortcut: items aren't pushed
    with pytest.raises(SinkError, match="no item_shortcut"):
        await sink.push_item(action(), meeting)

    await sink.update_item(action(status=ItemStatus.DONE), meeting, "promethee://task/7")
    assert runner.calls[0].argv[2] == "Mettre à jour"
    assert runner.calls[0].input["event"] == "item.updated"
    assert runner.calls[0].input["ref"] == "promethee://task/7"
    assert runner.calls[0].input["item"]["status"] == "done"

    assert await sink.push_notes(meeting, "# Réunion\n\n- point 1") == "notes://1"
    assert runner.calls[1].argv[2] == "Compte rendu"
    assert runner.calls[1].input["event"] == "meeting.notes"
    assert runner.calls[1].input["notes_markdown"] == "# Réunion\n\n- point 1"
    assert "item" not in runner.calls[1].input


async def test_shortcuts_without_update_or_notes(cfg: Config, meeting: Meeting, on_mac: None) -> None:
    sink, runner = shortcuts(cfg, item_shortcut="Ajouter")
    assert not sink.supports_update and not sink.supports_notes
    assert ShortcutsSink.supports_update is False  # the class default is untouched
    await sink.update_item(action(), meeting, "x")
    assert await sink.push_notes(meeting, "notes") is None
    assert runner.calls == []


@pytest.mark.parametrize("kind", ["shortcuts", "command"])
async def test_a_timed_out_run_is_never_repeated(cfg: Config, meeting: Meeting, on_mac: None, kind: str) -> None:
    # the shortcut (or command) created the task but answered too late: running it again would add a second one
    timed_out = SinkError("shortcuts timed out after 60s", retryable=True)
    if kind == "shortcuts":
        sink, runner = shortcuts(cfg, timed_out, ok(), item_shortcut="Ajouter", update_shortcut="Modifier")
    else:
        sink, runner = command(cfg, timed_out, ok(), item_command="promethee add {title}", update_command="promethee set {ref}")
    item = meeting.items["A1"] = action()
    dispatcher = Dispatcher(cfg, meeting, [sink])
    for _ in range(3):
        assert await dispatcher.sync(require_stable=False) == 0
    assert len(runner.calls) == 1 and item.pushed == {}
    runner.replies = [timed_out]
    with pytest.raises(SinkError, match="isn't retried") as exc:
        await sink.update_item(action(), meeting, "promethee://task/7")
    assert not exc.value.retryable


def test_an_item_cancelled_before_its_push_is_never_sent(cfg: Config) -> None:
    # dispatch.mode = "end": "never mind, skip the deck" mid-meeting must not become a Promethee task at the end
    for sink in (shortcuts(cfg, item_shortcut="Ajouter")[0], command(cfg, item_command="promethee add {title}")[0]):
        assert sink.accepts(action(), 0.5)
        assert sink.accepts(action(status=ItemStatus.DONE), 0.5)
        assert not sink.accepts(action(status=ItemStatus.CANCELLED), 0.5)
        assert not sink.accepts(action(dismissed=True), 0.5)


async def test_shortcuts_failure(cfg: Config, meeting: Meeting, on_mac: None) -> None:
    sink, _ = shortcuts(cfg, shortcut_ran(None, code=1, stderr="Error: The operation couldn’t be completed. " + "x" * 600 + " last words"), item_shortcut="Ajouter")
    with pytest.raises(SinkError, match="last words") as exc:
        await sink.push_item(action(), meeting)
    assert not exc.value.retryable
    assert len(str(exc.value)) < 500


async def test_shortcuts_check(cfg: Config, on_mac: None) -> None:
    import unicodedata

    listing = "Ajouter à Promethee\n" + unicodedata.normalize("NFD", "Compte rendu réunion") + "\nAutre\n"
    sink, runner = shortcuts(cfg, ok(listing), item_shortcut="Ajouter à Promethee", notes_shortcut="Compte rendu réunion")
    assert await sink.check() == "“Ajouter à Promethee” for new items · “Compte rendu réunion” for notes"
    assert runner.calls[0].argv == [SHORTCUTS, "list"]

    sink, _ = shortcuts(cfg, ok(listing), item_shortcut="Ajouter à Promethee", update_shortcut="Absent")
    with pytest.raises(SinkError, match="no shortcut named “Absent”"):
        await sink.check()
    sink, _ = shortcuts(cfg, Result(1, "", "boom"), item_shortcut="x")
    with pytest.raises(SinkError, match="boom"):
        await sink.check()


def test_shortcuts_config_needs_a_shortcut() -> None:
    with pytest.raises(ValidationError, match="item_shortcut"):
        ShortcutsConfig(enabled=True)


# ==================================================================== command


def command(cfg: Config, *replies: Reply, **kw: Any) -> tuple[CommandSink, FakeRunner]:
    sink = CommandSink(CommandConfig(enabled=True, **kw), cfg)
    sink.run = runner = FakeRunner(*replies)
    return sink, runner


def test_command_string_is_split_like_a_shell_but_never_run_by_one() -> None:
    c = CommandConfig(
        item_command="""open "promethee://add?title={title_url}&due={due_url}" --args 'two words' {title}""",
        update_command=["promethee", "set", "--", "{ref}"],
    )
    assert c.item_command == ["open", "promethee://add?title={title_url}&due={due_url}", "--args", "two words", "{title}"]
    assert c.update_command == ["promethee", "set", "--", "{ref}"]


@pytest.mark.parametrize(
    ("command", "match"),
    [
        ("", "empty"),
        ([], "empty"),
        ("{title} --x", "program"),
        ("/bin/{owner}", "program"),
        ("promethee {titel}", "unknown placeholder"),
        ("sh -c 'echo {title}'", "run meeting text as code"),
        ("bash -lc 'promethee add {title}'", "run meeting text as code"),
        ("perl -ne 'print \"{owner}\"'", "run meeting text as code"),
        (["osascript", "-e", 'display notification "{title}"'], "run meeting text as code"),
        ("python3 -c 'print(\"{detail_url}\")'", "run meeting text as code"),
        ('open "promethee://add?title={title}"', "_url form"),
        ("unterminated 'quote", "quotation"),
    ],
)
def test_command_config_rejects(command: Any, match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        CommandConfig(item_command=command)


def test_command_config_accepts_safe_forms() -> None:
    CommandConfig(item_command=["sh", "-c", 'promethee add "$1"', "sh", "{title}"])
    CommandConfig(item_command=["promethee", "--force", "{title}", "--note", "{detail}"])
    CommandConfig(item_command=["python3", "-c", "import json,sys; json.load(open('{json_path}'))"])
    CommandConfig(item_command='open "promethee://add?title={title_url}&file={json_path_url}"')
    with pytest.raises(ValidationError, match="item_command"):
        CommandConfig(enabled=True)


@pytest.mark.parametrize("title", NASTY)
async def test_command_placeholders_fill_whole_arguments(cfg: Config, meeting: Meeting, title: str) -> None:
    seen: list[str] = []

    def reply(call: Call) -> Result:
        path = call.argv[-1]
        seen.append(Path(path).read_text(encoding="ascii"))
        return ok("promethee://task/9\nmore\n")

    sink, runner = command(cfg, reply, item_command=["promethee", "add", "--title={title}", "--", "{title}", "{owner}", "{due}", "{id}", "{json_path}"])
    item = action(title=title)

    assert await sink.push_item(item, meeting) == "promethee://task/9"
    call = runner.calls[0]
    clean = title.replace("\x00", "")
    assert call.argv[:-1] == ["promethee", "add", f"--title={clean}", "--", clean, "Alex", "2026-10-16", "A1"]
    assert call.stdin is not None and call.stdin.isascii()
    assert seen == [call.stdin.decode("ascii")]  # {json_path} holds the same payload as stdin
    assert not Path(call.argv[-1]).exists()  # and is gone afterwards
    data = json.loads(call.stdin)
    assert data["event"] == "item.created" and data["item"]["title"] == title


async def test_command_values_are_not_expanded_twice(cfg: Config, meeting: Meeting) -> None:
    sink, runner = command(cfg, item_command=["promethee", "{title}|{detail}"])
    await sink.push_item(action(title="{detail}", detail="{json_path}"), meeting)
    assert runner.calls[0].argv == ["promethee", "{detail}|{json_path}"]


async def test_command_nul_bytes_are_dropped(cfg: Config, meeting: Meeting) -> None:
    sink, runner = command(cfg, item_command=["promethee", "{title}", "{title_url}"])
    await sink.push_item(action(title="a\x00b"), meeting)
    assert runner.calls[0].argv == ["promethee", "ab", "ab"]


async def test_command_url_placeholders(cfg: Config, meeting: Meeting) -> None:
    sink, runner = command(cfg, item_command='open "promethee://add?title={title_url}&who={owner_url}&due={due_url}&m={meeting_title_url}"')
    meeting.title = "Point hebdo #3"
    await sink.push_item(action(title="Réunion & co/?#=+ 100% ok", owner="Zoë"), meeting)
    assert runner.calls[0].argv == [
        "open",
        "promethee://add?title=R%C3%A9union%20%26%20co%2F%3F%23%3D%2B%20100%25%20ok&who=Zo%C3%AB&due=2026-10-16&m=Point%20hebdo%20%233",
    ]


async def test_command_env_keeps_secrets_out(cfg: Config, meeting: Meeting, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("PROMETHEE_TOKEN", "pt-123")
    monkeypatch.setenv("LC_ALL", "fr_FR.UTF-8")
    sink, runner = command(cfg, item_command="promethee add {title}")
    await sink.push_item(action(), meeting)
    env = runner.calls[0].env
    assert env is not None
    assert "ANTHROPIC_API_KEY" not in env and "PROMETHEE_TOKEN" not in env
    assert env["LC_ALL"] == "fr_FR.UTF-8" and env.get("PATH") == os.environ.get("PATH")

    sink, runner = command(cfg, item_command="promethee add {title}", pass_env=["PROMETHEE_TOKEN"])
    await sink.push_item(action(), meeting)
    env = runner.calls[0].env
    assert env is not None and env["PROMETHEE_TOKEN"] == "pt-123" and "ANTHROPIC_API_KEY" not in env


async def test_command_ref_fallback_and_failure(cfg: Config, meeting: Meeting) -> None:
    sink, _ = command(cfg, ok("\n \n"), item_command="promethee add {title}")
    assert await sink.push_item(action(), meeting) == "cmd:A1"

    sink, _ = command(cfg, Result(3, "", "usage: promethee add TITLE\n" + "y" * 900 + "\nError: unknown flag"), item_command="/opt/bin/promethee add {title}")
    with pytest.raises(SinkError, match=r"(?s)promethee exited with 3: …y+\nError: unknown flag$") as exc:
        await sink.push_item(action(), meeting)
    assert not exc.value.retryable
    assert "Envoyer" not in str(exc.value)  # no argv in the error


async def test_command_update_and_notes(cfg: Config, meeting: Meeting) -> None:
    sink, runner = command(
        cfg,
        ok(),
        ok(),
        ok("file:///notes/1.md"),
        item_command="promethee add {title}",
        update_command="promethee set --status={status} -- {ref} {title}",
        notes_command="promethee notes --title={title} {id} {meeting_title} {owner}",
    )
    assert sink.supports_update and sink.supports_notes
    await sink.update_item(action(status=ItemStatus.DONE), meeting, "promethee://task/9")
    assert runner.calls[0].argv == ["promethee", "set", "--status=done", "--", "promethee://task/9", "Envoyer le devis à Sophie"]
    assert json.loads(runner.calls[0].stdin or b"")["ref"] == "promethee://task/9"
    await sink.update_item(action(dismissed=True), meeting, "promethee://task/9")
    assert runner.calls[1].argv[2] == "--status=dismissed"

    meeting.tldr = "On a validé le devis."
    assert await sink.push_notes(meeting, "# Notes") == "file:///notes/1.md"
    assert runner.calls[2].argv == ["promethee", "notes", "--title=Weekly sync", meeting.id, "Weekly sync", ""]
    data = json.loads(runner.calls[2].stdin or b"")
    assert data["event"] == "meeting.notes" and data["notes_markdown"] == "# Notes" and data["meeting"]["tldr"] == meeting.tldr


async def test_command_without_item_command(cfg: Config, meeting: Meeting) -> None:
    sink, runner = command(cfg, notes_command="promethee notes {json_path}")
    assert not sink.accepts(action(), 0.0) and not sink.supports_update
    with pytest.raises(SinkError, match="no item_command"):
        await sink.push_item(action(), meeting)
    await sink.update_item(action(), meeting, "x")
    assert runner.calls == []


async def test_command_check_runs_nothing(cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tool = tmp_path / "bin" / "promethee"
    tool.parent.mkdir()
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", str(tool.parent))
    sink, runner = command(cfg, item_command="promethee add {title}", notes_command=[sys.executable, "-V"])
    assert await sink.check() == f"promethee for new items · {Path(sys.executable).name} for notes (not run)"
    assert runner.calls == []

    sink, _ = command(cfg, item_command="promethee-nope add {title}")
    with pytest.raises(SinkError, match="promethee-nope"):
        await sink.check()


async def test_command_end_to_end_with_real_processes(cfg: Config, meeting: Meeting, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    record = tmp_path / "record.json"
    script = tmp_path / "fake_promethee.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import json, os, sys
            stdin = sys.stdin.buffer.read().decode("ascii")
            with open(sys.argv[-1], encoding="ascii") as fh:
                from_file = fh.read()
            with open({str(record)!r}, "w", encoding="utf-8") as fh:
                json.dump({{"argv": sys.argv[1:], "stdin": stdin, "file": from_file, "env": dict(os.environ)}}, fh)
            print("promethee://task/" + sys.argv[1].replace(" ", "-")[:5])
            """
        )
    )
    title = NASTY[3] + " " + NASTY[6]
    sink = CommandSink(CommandConfig(enabled=True, item_command=[sys.executable, str(script), "{id}", "{title}", "{title_url}", "{json_path}"]), cfg)

    assert await sink.push_item(action(title=title), meeting) == "promethee://task/A1"
    seen = json.loads(record.read_text(encoding="utf-8"))
    assert seen["argv"][:3] == ["A1", title, sink_url(title)]
    assert seen["stdin"] == seen["file"] and json.loads(seen["stdin"])["item"]["title"] == title
    assert "ANTHROPIC_API_KEY" not in seen["env"]


def sink_url(text: str) -> str:
    from urllib.parse import quote

    return quote(text, safe="")


# =============================================================== _proc.run


async def test_run_captures_output_and_exit_code() -> None:
    res = await _proc.run(
        [sys.executable, "-c", "import sys; d = sys.stdin.read(); print(d.upper()); print('é', file=sys.stderr); sys.exit(4)"],
        stdin=b"hello",
        timeout=30,
        env={"PYTHONIOENCODING": "utf-8"},
    )
    assert res == Result(4, "HELLO\n", "é\n")


async def test_run_timeout_kills_and_is_retryable() -> None:
    with pytest.raises(SinkError, match="timed out after 0.3s") as exc:
        await _proc.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.3)
    assert exc.value.retryable


async def test_run_missing_program_is_not_retryable(tmp_path: Path) -> None:
    with pytest.raises(SinkError, match="can't run nope") as exc:
        await _proc.run([str(tmp_path / "nope")], timeout=5)
    assert not exc.value.retryable


def test_child_env_and_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk")
    monkeypatch.setenv("LC_CTYPE", "UTF-8")
    monkeypatch.setenv("HUBSPOT_TOKEN", "h")
    env = _proc.child_env(["HUBSPOT_TOKEN"])
    assert "OPENAI_API_KEY" not in env and env["LC_CTYPE"] == "UTF-8" and env["HUBSPOT_TOKEN"] == "h"
    assert _proc.first_line("\n  \n  hello \nworld") == "hello"
    assert _proc.first_line("") == ""
    assert _proc.tail("  short  ") == "short"
    assert _proc.tail("x" * 10 + "end", limit=3) == "…end"
