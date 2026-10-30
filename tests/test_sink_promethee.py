"""The Promethee sink, against a fake MCP server (Streamable HTTP): which tool it calls, how the to-do's
fields are fitted to the tool's arguments, what comes back as the ref, and how failures are reported."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from voicestream.config import Config
from voicestream.models import Item, ItemKind, ItemStatus, Meeting
from voicestream.sinks.base import SinkError, build_sink, item_markdown
from voicestream.sinks.promethee import DEFAULT_URL, PrometheeConfig, PrometheeSink, ref_id, task_ref

CREATE = {
    "name": "create_task",
    "description": "Create a task",
    "inputSchema": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "description": {"type": "string"},
            "due_date": {"type": "string"},
            "priority": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH"]},
            "assignee": {"type": "string"},
            "project_id": {"type": "string"},
        },
        "required": ["title"],
    },
}
UPDATE = {
    "name": "update_task",
    "inputSchema": {
        "type": "object",
        "properties": {"task_id": {"type": "string"}, "title": {"type": "string"}, "completed": {"type": "boolean"}},
        "required": ["task_id"],
    },
}


class FakeMcp:
    """An MCP server: answers initialize, tools/list and tools/call; records every tool call."""

    def __init__(self, tools: list[dict[str, Any]], *, sse: bool = False, result: dict[str, Any] | None = None) -> None:
        self.tools = tools
        self.sse = sse
        self.result = result or {"content": [{"type": "text", "text": "created"}], "structuredContent": {"id": "T-42", "url": "https://app.promethee.io/tasks/T-42"}}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.requests: list[httpx.Request] = []
        self.sessions = 0
        self.forget_session = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        msg = json.loads(request.content)
        method = msg.get("method")
        if method == "initialize":
            self.sessions += 1
            return self._reply(msg, {"protocolVersion": "2025-06-18", "serverInfo": {"name": "Promethee"}, "capabilities": {"tools": {}}},
                               headers={"Mcp-Session-Id": f"s{self.sessions}"})
        if "id" not in msg:
            return httpx.Response(202)
        if request.headers.get("mcp-session-id") != f"s{self.sessions}" or self.forget_session:
            self.forget_session = False
            return httpx.Response(404, text="unknown session")
        if method == "tools/list":
            return self._reply(msg, {"tools": self.tools})
        if method == "tools/call":
            self.calls.append((msg["params"]["name"], msg["params"]["arguments"]))
            return self._reply(msg, self.result)
        return self._reply(msg, None, error={"code": -32601, "message": "unknown method"})

    def _reply(self, msg: dict[str, Any], result: Any, *, error: Any = None, headers: dict[str, str] | None = None) -> httpx.Response:
        body = {"jsonrpc": "2.0", "id": msg["id"], **({"error": error} if error else {"result": result})}
        if self.sse:
            text = f"event: message\ndata: {json.dumps(body)}\n\n"
            return httpx.Response(200, text=text, headers={"content-type": "text/event-stream", **(headers or {})})
        return httpx.Response(200, json=body, headers=headers)


@pytest.fixture
def token(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("PROMETHEE_TOKEN", "pm-123")
    return "pm-123"


def promethee(cfg: Config, server: FakeMcp, **kw: Any) -> PrometheeSink:
    sink = PrometheeSink(PrometheeConfig(enabled=True, **kw), cfg)
    sink._client = httpx.AsyncClient(transport=httpx.MockTransport(server.handler))
    return sink


def action(**kw: Any) -> Item:
    fields: dict[str, Any] = {"title": "Send the pricing deck", "owner": "Alex", "due_date": "2026-10-16", "priority": "high"}
    return Item(id="A1", kind=ItemKind.ACTION, **(fields | kw))


async def test_defaults_wait_for_review_and_build_from_config(cfg: Config) -> None:
    sink = build_sink("promethee", {"enabled": True}, cfg)
    assert isinstance(sink, PrometheeSink)
    assert sink.cfg.review is True and sink.cfg.url == DEFAULT_URL == "https://mcp.promethee.io/mcp"


@pytest.mark.parametrize("sse", [False, True])
async def test_create_fits_the_todo_to_the_tools_arguments(cfg: Config, meeting: Meeting, token: str, sse: bool) -> None:
    server = FakeMcp([{"name": "list_tasks"}, CREATE, UPDATE], sse=sse)
    sink = promethee(cfg, server, arguments={"project_id": "P1"})
    item = action()

    ref = await sink.push_item(item, meeting)

    assert ref == "https://app.promethee.io/tasks/T-42"
    assert server.calls == [("create_task", {
        "title": "Send the pricing deck",
        "description": item_markdown(item, meeting),
        "due_date": "2026-10-16",
        "priority": "HIGH",  # the server's own spelling
        "assignee": "Alex",
        "project_id": "P1",
    })]
    assert all(r.headers["Authorization"] == f"Bearer {token}" for r in server.requests)
    assert server.requests[-1].headers["Mcp-Session-Id"] == "s1"
    assert server.requests[-1].headers["MCP-Protocol-Version"] == "2025-06-18"


async def test_update_marks_the_task_done(cfg: Config, meeting: Meeting, token: str) -> None:
    server = FakeMcp([CREATE, UPDATE])
    sink = promethee(cfg, server)
    await sink.update_item(action(status=ItemStatus.DONE), meeting, "https://app.promethee.io/tasks/T-42")
    assert server.calls == [("update_task", {"task_id": "T-42", "title": "Send the pricing deck", "completed": True})]


async def test_without_an_update_tool_tasks_stay_as_sent(cfg: Config, meeting: Meeting, token: str) -> None:
    server = FakeMcp([CREATE])
    sink = promethee(cfg, server)
    await sink.update_item(action(), meeting, "T-42")
    assert server.calls == []


async def test_a_forgotten_session_is_started_again_for_reads_only(cfg: Config, meeting: Meeting, token: str) -> None:
    server = FakeMcp([CREATE])
    sink = promethee(cfg, server)
    await sink.check()
    server.forget_session = True
    assert "create_task" in await sink.check()  # re-initialized, then asked again
    server.forget_session = True
    with pytest.raises(SinkError) as exc:  # a create is never sent twice on its own
        await sink.push_item(action(), meeting)
    assert exc.value.retryable and server.calls == []


async def test_required_arguments_nobody_fills_are_reported(cfg: Config, meeting: Meeting, token: str) -> None:
    tool = {**CREATE, "inputSchema": {**CREATE["inputSchema"], "required": ["title", "project_id"]}}
    sink = promethee(cfg, FakeMcp([tool]))
    with pytest.raises(SinkError, match=r"needs project_id.*\[sinks\.promethee\.arguments\]") as exc:
        await sink.check()
    assert not exc.value.retryable


async def test_check_says_which_tools_are_used(cfg: Config, token: str) -> None:
    line = await promethee(cfg, FakeMcp([CREATE, UPDATE])).check()
    assert line.startswith("connected to Promethee · 2 tools · new to-dos go through create_task")
    assert "updates through update_task" in line and "validate the list" in line


async def test_errors(cfg: Config, meeting: Meeting, token: str, monkeypatch: pytest.MonkeyPatch) -> None:
    sink = promethee(cfg, FakeMcp([{"name": "search"}]))
    with pytest.raises(SinkError, match="none of the server's tools adds a task"):
        await sink.push_item(action(), meeting)
    failing = FakeMcp([CREATE], result={"isError": True, "content": [{"type": "text", "text": "quota exceeded"}]})
    with pytest.raises(SinkError, match="quota exceeded"):
        await promethee(cfg, failing).push_item(action(), meeting)
    refused = PrometheeSink(PrometheeConfig(enabled=True), cfg)
    refused._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401, text="bad token")))
    with pytest.raises(SinkError, match="refused the credentials") as exc:
        await refused.check()
    assert not exc.value.retryable
    monkeypatch.delenv("PROMETHEE_TOKEN")  # no token and no sign-in: says how to connect
    with pytest.raises(SinkError, match="not connected to Promethee: click Connect"):
        await sink.check()


@pytest.mark.parametrize(("result", "ref"), [
    ({"structuredContent": {"task": {"id": 7, "url": "https://p.io/t/abc"}}}, "https://p.io/t/abc#7"),
    ({"structuredContent": {"id": "T-1"}}, "T-1"),
    ({"content": [{"type": "text", "text": '{"id": "T-2"}'}]}, "T-2"),
    ({"content": [{"type": "text", "text": "Done: https://app.promethee.io/t/9."}]}, "https://app.promethee.io/t/9"),
    ({"content": [{"type": "text", "text": "Task created (id: x9y8z7)"}]}, "x9y8z7"),
    ({"content": [{"type": "text", "text": "ok"}]}, None),
])  # fmt: skip
def test_task_ref(result: dict[str, Any], ref: str | None) -> None:
    assert task_ref(result) == ref


def test_ref_id() -> None:
    assert ref_id("https://p.io/t/abc#7") == "7"
    assert ref_id("https://app.promethee.io/tasks/T-42/") == "T-42"
    assert ref_id("T-1") == "T-1"
    assert ref_id("promethee:20261030/A1") is None
