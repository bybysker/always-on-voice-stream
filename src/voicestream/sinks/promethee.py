"""Promethee sink: the meeting's to-do list lands in Promethee, through its MCP server
(``https://mcp.promethee.io/mcp``).

By default the to-dos wait for you (``review = true``): when the meeting ends, the app shows the list
and you choose which ones go to Promethee, or none. ``review = false`` sends them like any other tool.

Voicestream talks to the server as any MCP client does: it lists the server's tools and calls the
one that creates a task. Which tool that is, and what its arguments are called, is read from the
server's own description of its tools, so nothing has to be set by hand:

* the creating tool is ``create_tool``, else the first whose name says it adds a task or a to-do
  (``create_task``, ``add_todo``…); the updating one is ``update_tool``, else one called like
  ``update_task``. Without one, Promethee keeps the task as it was first sent;
* each of the tool's arguments gets the matching field of the to-do, recognised by its name: the
  title (``title``, ``name``, ``content``…), the description (``description``, ``notes``…), the
  deadline (``due_date``, ``deadline``…), the priority, the person in charge (``assignee``,
  ``owner``…), and for an update the task's id and whether it is done;
* ``arguments`` adds fixed ones (a project, a list…) and wins over the guessed ones. A required
  argument that nothing fills is reported by **Test**, before anything is sent.

The key is the bearer token Promethee gives you for its MCP server, saved as ``PROMETHEE_TOKEN``
(Settings → Your tools → Promethee, or ``~/.voicestream/.env``).

Ref format: the task's URL when the server gives one, else its id.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, ClassVar, Literal

from pydantic import Field

from ..models import Item, ItemStatus, Meeting
from .base import Sink, SinkConfig, SinkError, env_secret, item_markdown
from .http import HttpSinkMixin
from .mcp import McpClient, result_text

log = logging.getLogger(__name__)

DEFAULT_URL = "https://mcp.promethee.io/mcp"
_CREATE = re.compile(r"(create|add|new|insert)[_\-\s]?.*(task|todo|to_do|action)", re.I)
_UPDATE = re.compile(r"(update|edit|modify|set|complete|close)[_\-\s]?.*(task|todo|to_do|action)", re.I)

# the to-do's fields, by the names a tool's arguments may give them (compared lower-case, without _ or -)
FIELDS: dict[str, tuple[str, ...]] = {
    "title": ("title", "name", "content", "task", "summary", "text", "label", "subject"),
    "description": ("description", "notes", "note", "details", "detail", "body", "comment", "markdown"),
    "due": ("duedate", "due", "deadline", "dueon", "duedatetime", "enddate", "date"),
    "priority": ("priority",),
    "assignee": ("assignee", "owner", "assignedto", "responsible", "assigneename"),
    "id": ("id", "taskid", "todoid", "actionid", "itemid"),
    "done": ("completed", "done", "iscompleted", "isdone", "checked"),
    "status": ("status", "state"),
}


class PrometheeConfig(SinkConfig):
    review: bool = True
    url: str = DEFAULT_URL
    """The Promethee MCP server."""
    token_env: str = "PROMETHEE_TOKEN"
    """The environment variable (or saved key) holding your Promethee MCP token."""
    create_tool: str | None = None
    """The tool that adds a task (default: found among the server's tools)."""
    update_tool: str | None = None
    """The tool that changes one (default: found among the server's tools; none: tasks aren't updated)."""
    arguments: dict[str, Any] = Field(default_factory=dict)
    """Fixed arguments for every new task, e.g. ``{ project = "Clients" }``; they win over the guessed ones."""
    done_status: str = "done"
    """What a finished task's ``status`` argument is set to, for a server that takes a status."""


class PrometheeSink(HttpSinkMixin, Sink):
    name = "promethee"
    Config: ClassVar[type[SinkConfig]] = PrometheeConfig
    supports_update = True
    cfg: PrometheeConfig

    def __init__(self, cfg: SinkConfig, app: Any) -> None:
        super().__init__(cfg, app)
        self.mcp = McpClient(self.cfg.url, self.request, self._auth, name="promethee")

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {env_secret(self.cfg, 'token_env', 'PROMETHEE_TOKEN')}"}

    # ------------------------------------------------------------------ tools

    async def _tool(self, kind: Literal["create", "update"]) -> dict[str, Any] | None:
        tools = await self.mcp.tools()
        wanted = self.cfg.create_tool if kind == "create" else self.cfg.update_tool
        if wanted:
            for t in tools:
                if t["name"] == wanted:
                    return t
            raise SinkError(f"promethee: the server has no tool {wanted!r} (it has: {_names(tools)})", retryable=False)
        pattern = _CREATE if kind == "create" else _UPDATE
        found = next((t for t in tools if pattern.search(t["name"])), None)
        if found is None and kind == "create":
            raise SinkError(f"promethee: none of the server's tools adds a task (it has: {_names(tools)}); "
                            "set create_tool under [sinks.promethee]", retryable=False)
        return found

    def _arguments(self, tool: dict[str, Any], values: dict[str, Any], *, fixed: dict[str, Any]) -> dict[str, Any]:
        """The tool's arguments, each filled with the to-do field its name matches; ``fixed`` wins."""
        schema = tool.get("inputSchema") or {}
        props: dict[str, Any] = schema.get("properties") or {}
        args: dict[str, Any] = {}
        taken: set[str] = set()
        for field, names in FIELDS.items():
            if values.get(field) is None:
                continue
            for name in names:  # in order of preference: "title" before "name" before "text"…
                prop = next((p for p in props if _key(p) == name and p not in taken), None)
                if prop is None:
                    continue
                value = _fit(values[field], props[prop] or {})
                if value is not None:
                    args[prop] = value
                    taken.add(prop)
                break
        args.update(fixed)
        missing = [p for p in schema.get("required") or [] if p not in args]
        if missing:
            raise SinkError(f"promethee: {tool['name']} needs {', '.join(missing)}, which voicestream can't fill: "
                            "set them under [sinks.promethee.arguments]", retryable=False)
        return args

    def _values(self, item: Item, meeting: Meeting) -> dict[str, Any]:
        return {
            "title": item.title,
            "description": item_markdown(item, meeting),
            "due": item.due_date,
            "priority": item.priority,
            "assignee": item.owner,
        }

    # -------------------------------------------------------------- sink API

    async def push_item(self, item: Item, meeting: Meeting) -> str:
        tool = await self._tool("create")
        assert tool is not None
        args = self._arguments(tool, self._values(item, meeting), fixed=self.cfg.arguments)
        result = await self.mcp.call_tool(tool["name"], args)
        ref = task_ref(result)
        if not ref:
            # it exists, but without an id it can't be updated; sending it again would make a second one
            log.warning("promethee: %s gave no task id back: %s", tool["name"], result_text(result)[:200])
            return f"promethee:{meeting.id}/{item.id}"
        if is_closed(item):
            try:
                await self.update_item(item, meeting, ref)
            except SinkError as exc:
                log.warning("promethee: created %s but couldn't mark it done: %s", ref, exc)
        return ref

    async def update_item(self, item: Item, meeting: Meeting, ref: str) -> None:
        task_id = ref_id(ref)
        if task_id is None:
            return
        tool = await self._tool("update")
        if tool is None:
            log.info("promethee: the server has no tool to update a task; %s stays as it was sent", ref)
            return
        values: dict[str, Any] = {**self._values(item, meeting), "id": task_id}
        if is_closed(item):
            values |= {"done": True, "status": self.cfg.done_status}
        args = self._arguments(tool, values, fixed={})
        await self.mcp.call_tool(tool["name"], args, safe=True)  # sets values: harmless to repeat

    async def check(self) -> str:
        await self.mcp.initialize()
        tools = await self.mcp.tools(refresh=True)
        create = await self._tool("create")
        assert create is not None
        sample = {"title": "-", "description": "-", "due": "2030-01-01", "priority": "medium", "assignee": "-"}
        args = self._arguments(create, sample, fixed=self.cfg.arguments)
        update = await self._tool("update")
        server = self.mcp.server.get("name") or "Promethee"
        line = f"connected to {server} · {len(tools)} tools · new to-dos go through {create['name']} ({', '.join(sorted(args))})"
        line += f" · updates through {update['name']}" if update else " · no tool to update them"
        return line + (" · you validate the list at the end of each meeting" if self.cfg.review else "")

    async def aclose(self) -> None:
        self.mcp.close()
        await super().aclose()


# ---------------------------------------------------------------------- helpers


def _key(name: str) -> str:
    return re.sub(r"[_\-\s]", "", name).lower()


def _names(tools: list[dict[str, Any]]) -> str:
    return ", ".join(t["name"] for t in tools[:20]) or "none"


def _fit(value: Any, prop: dict[str, Any]) -> Any:
    """``value`` in the shape the argument takes, or ``None`` when it can't take it."""
    types = prop.get("type")
    types = set(types) if isinstance(types, list) else {types} if types else set()
    enum = prop.get("enum")
    if enum:
        if isinstance(value, bool):
            return value if value in enum else None
        for option in enum:
            if str(option).lower() == str(value).lower():
                return option
        return None
    if isinstance(value, bool):
        if not types or "boolean" in types:
            return value
        return None
    if isinstance(value, str) and types and not types & {"string"}:
        return None
    return value


def is_closed(item: Item) -> bool:
    return item.dismissed or item.status in (ItemStatus.DONE, ItemStatus.CANCELLED)


_ID_KEYS = ("id", "task_id", "taskId", "todo_id", "uuid")
_URL_KEYS = ("url", "link", "permalink", "web_url", "webUrl", "href")
_URL = re.compile(r"https?://[^\s)\"'<>]+")


def task_ref(result: dict[str, Any]) -> str | None:
    """The created task's URL or id, from the tool's structured result or its text."""
    data: Any = result.get("structuredContent")
    if data is None:
        text = result_text(result)
        try:
            data = json.loads(text)
        except ValueError:
            url = _URL.search(text)
            if url:
                return url.group(0).rstrip(".,;")
            found = re.search(r"\b(?:id|ID|Id)\b\W{0,3}([A-Za-z0-9_\-]{3,})", text)
            return found.group(1) if found else None
    for obj in _objects(data):
        url = next((obj[k] for k in _URL_KEYS if isinstance(obj.get(k), str) and obj[k].startswith("http")), None)
        task_id = next((obj[k] for k in _ID_KEYS if isinstance(obj.get(k), str | int) and not isinstance(obj.get(k), bool)), None)
        if url and task_id is not None and str(task_id) not in url:
            return f"{url}#{task_id}"  # the update needs the id, and it isn't in the URL
        if url or task_id is not None:
            return str(url or task_id)
    return None


def _objects(data: Any) -> list[dict[str, Any]]:
    """``data`` and the objects one level in (``{"task": {...}}``, ``{"data": {...}}``…)."""
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        return []
    return [data, *(v for v in data.values() if isinstance(v, dict))]


def ref_id(ref: str) -> str | None:
    """The task id inside a ref: after ``#``, the URL's last segment, or the ref itself."""
    if ref.startswith("promethee:"):
        return None  # created without an id coming back
    if "#" in ref:
        return ref.rpartition("#")[2]
    if ref.startswith("http"):
        return ref.split("?")[0].rstrip("/").rpartition("/")[2] or None
    return ref
