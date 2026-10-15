"""Notion sink against an in-memory fake of the 2026-03-11 REST API (httpx.MockTransport)."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from voicestream.config import Config
from voicestream.models import AgentRun, AgentStatus, Item, ItemKind, ItemStatus, Meeting
from voicestream.sinks.base import SinkError, build_sinks, item_markdown, sink_class
from voicestream.sinks.notion import MD_CHUNK_CHARS, MD_CHUNK_LINES, NotionConfig, NotionSink, _md_chunks, _page_id, _ref, _rt

DB = "1f2e3d4c5b6a49788796a5b4c3d2e1f0"
DS = "248104cd-477e-80af-bc30-000bd5b2c4d9"
NOTES_DB = "9a8b7c6d5e4f43218765fedcba987654"
NOTES_DS = "5c4b3a29-1807-4f6e-8d5c-4b3a29180706"
NOTES_PAGE = "aa11bb22cc33dd44ee55ff6677889900"
SAM = "6d1c7f2e-0d55-4e6b-9a51-3c1a2f9d8e70"
ALEX = "0e9d8c7b-6a59-4483-9271-605f4e3d2c1b"
PAGE1 = uuid.UUID(int=1).hex
URL1 = f"https://www.notion.so/Page-{PAGE1}"


def opts(*names: str) -> list[dict[str, Any]]:
    return [{"id": f"o-{n}", "name": n, "color": "default", "description": None} for n in names]


def prop(type_: str, **config: Any) -> dict[str, Any]:
    return {"id": type_[:4], "type": type_, type_: config}


def status(groups: dict[str, list[str]]) -> dict[str, Any]:
    options = [o for names in groups.values() for o in opts(*names)]
    return prop(
        "status",
        options=options,
        groups=[{"id": f"g-{g}", "name": g, "color": "gray", "option_ids": [f"o-{n}" for n in names]} for g, names in groups.items()],
    )


# Notion's "Tasks" template, plus columns the sink must ignore
TASKS = {
    "Task name": prop("title"),
    "Status": status({"To-do": ["Not started"], "In progress": ["In progress"], "Complete": ["Done", "Cancelled"]}),
    "Assignee": prop("people"),
    "Due date": prop("date"),
    "Priority": prop("select", options=opts("Low", "Medium", "High")),
    "Meeting": prop("rich_text"),
    "Tags": prop("multi_select", options=opts("ops")),
    "Created": prop("created_time"),
}


def text(s: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": {"content": s}}]


class FakeNotion:
    """Canned objects, recorded requests and scripted failures (``fail[(method, path)]``)."""

    def __init__(self, properties: dict[str, Any] = TASKS, data_sources: list[dict[str, str]] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.fail: dict[tuple[str, str], list[httpx.Response]] = {}
        self.page_markdown = ""
        self.created = 0
        self.objects: dict[tuple[str, str], dict[str, Any]] = {
            ("GET", "users/me"): {"object": "user", "id": "b0t", "name": "voicestream", "type": "bot", "bot": {"workspace_name": "Acme"}},
            ("GET", f"databases/{DB}"): {"object": "database", "id": DB, "data_sources": data_sources or [{"id": DS, "name": "Tasks"}]},
            ("GET", f"data_sources/{DS}"): {"object": "data_source", "id": DS, "title": [{"plain_text": "Tasks"}], "properties": properties},
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        method, path = request.method, request.url.path.removeprefix("/v1/")
        if self.fail.get((method, path)):
            return self.fail[(method, path)].pop(0)
        if (method, path) in self.objects:
            return httpx.Response(200, json=self.objects[(method, path)])
        if (method, path) == ("POST", "pages"):
            self.created += 1
            pid = uuid.UUID(int=self.created)
            return httpx.Response(200, json={"object": "page", "id": str(pid), "url": f"https://www.notion.so/Page-{pid.hex}", "in_trash": False})
        if path.startswith("pages/") and path.endswith("/markdown"):
            page = {"object": "page_markdown", "id": path.split("/")[1], "markdown": self.page_markdown, "truncated": False}
            return httpx.Response(200, json=page | {"unknown_block_ids": []})
        if method == "PATCH" and path.startswith("pages/"):
            return httpx.Response(200, json={"object": "page", "id": path.split("/")[1]})
        return error(404, "object_not_found", "Could not find object. Make sure it is shared with your integration.")

    def calls(self) -> list[tuple[str, str]]:
        return [(r.method, r.url.path.removeprefix("/v1/")) for r in self.requests]

    def bodies(self, method: str, path: str) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests if (r.method, r.url.path) == (method, f"/v1/{path}")]


def error(status_code: int, code: str, message: str, **headers: str) -> httpx.Response:
    body = {"object": "error", "status": status_code, "code": code, "message": message}
    return httpx.Response(status_code, json=body, headers=headers)


@pytest.fixture
def api() -> FakeNotion:
    return FakeNotion()


@pytest.fixture
def make(cfg: Config, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NOTION_TOKEN", "secret_abc")

    def _make(api: FakeNotion, **settings: Any) -> NotionSink:
        sink = NotionSink(NotionConfig.model_validate({"enabled": True, "tasks_database_id": DB, "users": {"Sam": SAM}} | settings), cfg)
        sink._client = httpx.AsyncClient(transport=httpx.MockTransport(api))
        return sink

    return _make


def action(**kw: Any) -> Item:
    fields = {
        "id": "A1",
        "kind": ItemKind.ACTION,
        "title": "Send the pricing deck to Acme",
        "detail": "Include the Q4 discount tiers.",
        "owner": "Sam",
        "due_text": "Friday",
        "due_date": "2026-10-16",
        "priority": "high",
        "evidence": "Sam, can you send Acme the deck by Friday?",
    }
    return Item(**(fields | kw))


# ------------------------------------------------------------------- create


async def test_create_payload_is_the_2026_03_11_shape(make, api: FakeNotion, meeting: Meeting) -> None:
    sink = make(api)
    item = action()
    assert await sink.push_item(item, meeting) == URL1
    assert api.calls() == [("GET", f"databases/{DB}"), ("GET", f"data_sources/{DS}"), ("POST", "pages")]
    assert api.bodies("POST", "pages") == [
        {
            "parent": {"type": "data_source_id", "data_source_id": DS},
            "properties": {
                "Task name": {"title": text("Send the pricing deck to Acme")},
                "Status": {"status": {"name": "Not started"}},
                "Due date": {"date": {"start": "2026-10-16"}},
                "Assignee": {"people": [{"object": "user", "id": SAM}]},
                "Priority": {"select": {"name": "High"}},
                "Meeting": {"rich_text": text("Weekly sync")},
            },
            "markdown": item_markdown(item, meeting),
        }
    ]
    body = api.bodies("POST", "pages")[0]["markdown"]
    assert "Include the Q4 discount tiers." in body and "> Sam, can you send Acme" in body


async def test_headers_carry_bearer_token_and_pinned_version(make, api: FakeNotion, meeting: Meeting) -> None:
    await make(api).push_item(action(), meeting)
    for r in api.requests:
        assert r.headers["Authorization"] == "Bearer secret_abc"
        assert r.headers["Notion-Version"] == "2026-03-11"
    assert api.requests[-1].headers["Content-Type"] == "application/json"

    other = FakeNotion()
    await make(other, notion_version="2026-09-01").push_item(action(), meeting)
    assert {r.headers["Notion-Version"] for r in other.requests} == {"2026-09-01"}


async def test_data_source_is_resolved_once_and_cached(make, api: FakeNotion, meeting: Meeting) -> None:
    sink = make(api)
    await sink.push_item(action(), meeting)
    await sink.push_item(action(id="A2", title="Book the venue"), meeting)
    assert api.calls().count(("GET", f"databases/{DB}")) == 1
    assert api.calls().count(("GET", f"data_sources/{DS}")) == 1
    assert api.calls().count(("POST", "pages")) == 2


async def test_explicit_data_source_skips_the_database_lookup(make, api: FakeNotion, meeting: Meeting) -> None:
    await make(api, tasks_database_id=None, tasks_data_source_id=DS).push_item(action(), meeting)
    assert api.calls() == [("GET", f"data_sources/{DS}"), ("POST", "pages")]


async def test_multi_source_database_uses_the_first_and_warns(make, meeting: Meeting, caplog) -> None:
    api = FakeNotion(data_sources=[{"id": DS, "name": "Tasks"}, {"id": NOTES_DS, "name": "Archive"}])
    with caplog.at_level(logging.WARNING, logger="voicestream.sinks.notion"):
        await make(api).push_item(action(), meeting)
    assert api.bodies("POST", "pages")[0]["parent"]["data_source_id"] == DS
    assert "2 data sources" in caplog.text and "tasks_data_source_id" in caplog.text


async def test_missing_tasks_target_or_data_source(make, api: FakeNotion, meeting: Meeting) -> None:
    with pytest.raises(SinkError, match="set tasks_database_id") as exc:
        await make(api, tasks_database_id=None, notes_parent_page_id=NOTES_PAGE).push_item(action(), meeting)
    assert not exc.value.retryable and api.requests == []
    api.objects[("GET", f"databases/{DB}")]["data_sources"] = []
    with pytest.raises(SinkError, match="no data sources"):
        await make(api).push_item(action(), meeting)


# ------------------------------------------------------------------- schema


async def test_detects_other_names_and_types(make, meeting: Meeting) -> None:
    api = FakeNotion(
        {
            "Name": prop("title"),
            "State": prop("select", options=opts("Todo", "Doing", "Done", "Won't do")),
            "Due": prop("rich_text"),  # incompatible type: ignored
            "Deadline": prop("date"),
            "Assigned to": prop("rich_text"),
            "Prio": prop("select", options=opts("P1", "P2", "P3")),
            "Meeting": prop("select", options=opts("Standup", "Weekly sync")),
        }
    )
    await make(api, priorities={"high": "P1", "medium": "P2", "low": "P3"}).push_item(action(), meeting)
    assert api.bodies("POST", "pages")[0]["properties"] == {
        "Name": {"title": text("Send the pricing deck to Acme")},
        "State": {"select": {"name": "Todo"}},
        "Deadline": {"date": {"start": "2026-10-16"}},
        "Assigned to": {"rich_text": text("Sam")},
        "Prio": {"select": {"name": "P1"}},
        "Meeting": {"select": {"name": "Weekly sync"}},
    }


async def test_only_existing_options_are_sent(make, meeting: Meeting) -> None:
    api = FakeNotion(
        {
            "Title": prop("title"),
            "Priority": prop("select", options=opts("Urgent", "Normal")),
            "Meeting": prop("select", options=opts("Standup")),
        }
    )
    sink = make(api)
    ref = await sink.push_item(action(), meeting)
    assert api.bodies("POST", "pages")[0]["properties"] == {"Title": {"title": text("Send the pricing deck to Acme")}}
    await sink.update_item(action(status=ItemStatus.DONE, priority=None), meeting, ref)
    assert api.bodies("PATCH", f"pages/{PAGE1}")[0]["properties"] == {
        "Title": {"title": text("[done] Send the pricing deck to Acme")},  # no status property to show it
        "Priority": {"select": None},
    }


async def test_status_groups_find_options_whatever_their_names(make, meeting: Meeting) -> None:
    api = FakeNotion({"Title": prop("title"), "Progress": status({"To-do": ["Inbox"], "In progress": ["Doing"], "Complete": ["Shipped"]})})
    sink = make(api)
    item = action()
    ref = await sink.push_item(item, meeting)
    assert api.bodies("POST", "pages")[0]["properties"]["Progress"] == {"status": {"name": "Inbox"}}
    item.status = ItemStatus.DONE
    await sink.update_item(item, meeting, ref)
    item.status = ItemStatus.CANCELLED  # no such option: shown in the title instead
    await sink.update_item(item, meeting, ref)
    done, cancelled = (b["properties"] for b in api.bodies("PATCH", f"pages/{PAGE1}"))
    assert done["Progress"] == {"status": {"name": "Shipped"}}
    assert "Progress" not in cancelled and cancelled["Title"] == {"title": text("[cancelled] Send the pricing deck to Acme")}


async def test_configured_status_names_win(make, meeting: Meeting) -> None:
    api = FakeNotion({"Title": prop("title"), "Status": prop("select", options=opts("Not started", "Next up", "Done"))})
    await make(api, status_open="next up").push_item(action(), meeting)
    assert api.bodies("POST", "pages")[0]["properties"]["Status"] == {"select": {"name": "Next up"}}


async def test_done_checkbox(make, meeting: Meeting) -> None:
    api = FakeNotion({"Title": prop("title"), "Done": prop("checkbox")})
    sink = make(api)
    item = action()
    ref = await sink.push_item(item, meeting)
    assert "Done" not in api.bodies("POST", "pages")[0]["properties"]  # unchecked is the default
    item.status = ItemStatus.DONE
    await sink.update_item(item, meeting, ref)
    item.status, item.dismissed = ItemStatus.OPEN, True
    await sink.update_item(item, meeting, ref)
    done, dismissed = (b["properties"] for b in api.bodies("PATCH", f"pages/{PAGE1}"))
    assert done["Done"] == {"checkbox": True}
    assert "Done" not in dismissed and dismissed["Title"] == {"title": text("[dismissed] Send the pricing deck to Acme")}


async def test_property_overrides(make, meeting: Meeting, caplog) -> None:
    api = FakeNotion({"Task": prop("title"), "Assignee": prop("people"), "Who": prop("people"), "Due date": prop("date")})
    with caplog.at_level(logging.WARNING, logger="voicestream.sinks.notion"):
        await make(api, properties={"owner": "who", "due": "Deadline"}).push_item(action(), meeting)
    props = api.bodies("POST", "pages")[0]["properties"]
    assert props["Who"] == {"people": [{"object": "user", "id": SAM}]}
    assert "Assignee" not in props and "Due date" not in props  # an explicit name is never second-guessed
    assert "'Deadline'" in caplog.text


# ------------------------------------------------------------------- owners


async def test_owner_mapping_for_people_properties(make, api: FakeNotion, meeting: Meeting) -> None:
    sink = make(api, users={"Sam": SAM, "me": ALEX})
    await sink.push_item(action(owner="alex"), meeting)  # cfg.user_name is "Alex"
    await sink.push_item(action(id="A2", owner="Jordan"), meeting)
    await sink.push_item(action(id="A3", owner="  "), meeting)
    mine, unmapped, blank = api.bodies("POST", "pages")
    assert mine["properties"]["Assignee"] == {"people": [{"object": "user", "id": ALEX}]}
    assert "Assignee" not in unmapped["properties"] and "Assignee" not in blank["properties"]
    assert "**Owner:** Jordan" in unmapped["markdown"]


# ------------------------------------------------------------------- limits


def test_rich_text_is_split_at_2000_chars_and_100_items() -> None:
    assert [len(t["text"]["content"]) for t in _rt("x" * 4500)] == [2000, 2000, 500]
    assert len(_rt("x" * 300_000)) == 100
    assert _rt("") == []


def test_markdown_chunks_cut_at_blank_lines_outside_fences() -> None:
    code = "```python\n" + "\n\n".join(f"print({i})" for i in range(400)) + "\n```"
    paragraphs = [f"Paragraph {i}: " + "word " * 60 for i in range(120)]
    md = "\n\n".join(paragraphs[:60] + [code] + paragraphs[60:])
    chunks = _md_chunks(md)
    assert len(chunks) > 2
    assert "\n\n".join(chunks) == md  # nothing lost but the blank lines cut at
    assert all(c.count("```") % 2 == 0 for c in chunks)
    assert all(len(c) < 2 * MD_CHUNK_CHARS for c in chunks)
    assert _md_chunks("short **notes**\n\n\n") == ["short **notes**\n\n\n"]


def test_markdown_chunks_split_long_lists_anywhere() -> None:
    md = "\n".join(f"- [ ] item {i}" for i in range(3 * MD_CHUNK_LINES))
    chunks = _md_chunks(md)
    assert "\n".join(chunks) == md
    assert max(c.count("\n") + 1 for c in chunks) == 2 * MD_CHUNK_LINES


async def test_long_body_is_appended_in_chunks(make, api: FakeNotion, meeting: Meeting) -> None:
    item = action(agent=AgentRun(prompt="draft", status=AgentStatus.DONE, result_summary="\n\n".join(["lorem ipsum " * 80] * 40)))
    await make(api).push_item(item, meeting)
    chunks = _md_chunks(item_markdown(item, meeting))
    assert len(chunks) > 1
    assert api.bodies("POST", "pages")[0]["markdown"] == chunks[0]
    assert api.bodies("PATCH", f"pages/{PAGE1}/markdown") == [
        {"type": "insert_content", "insert_content": {"content": c, "position": {"type": "end"}}} for c in chunks[1:]
    ]


async def test_failed_append_keeps_the_created_page(make, api: FakeNotion, meeting: Meeting, caplog) -> None:
    api.fail[("PATCH", f"pages/{PAGE1}/markdown")] = [error(403, "restricted_resource", "Insufficient permissions")]
    item = action(detail="x " * MD_CHUNK_CHARS)
    with caplog.at_level(logging.WARNING, logger="voicestream.sinks.notion"):
        assert await make(api).push_item(item, meeting) == URL1  # a retry would duplicate the page
    assert "appending" in caplog.text and "lacks a capability" in caplog.text


# -------------------------------------------------------------------- notes

NOTES = "# Weekly sync\n\n*Wednesday 14 October 2026, 09:00* · 32:10 recorded\n\n## Action items\n\n- [ ] **Send the deck** — Sam\n"


async def test_notes_child_page(make, api: FakeNotion, meeting: Meeting) -> None:
    sink = make(api, tasks_database_id=None, notes_parent_page_id=NOTES_PAGE)
    assert await sink.push_notes(meeting, NOTES) == URL1
    assert api.bodies("POST", "pages") == [
        {
            "parent": {"type": "page_id", "page_id": NOTES_PAGE},
            "properties": {"title": {"title": text("Weekly sync")}},
            "markdown": NOTES.split("\n\n", 1)[1],  # the H1 duplicates the page title
        }
    ]


async def test_notes_database_page_gets_title_and_date(make, api: FakeNotion, meeting: Meeting) -> None:
    api.objects[("GET", f"databases/{NOTES_DB}")] = {"object": "database", "data_sources": [{"id": NOTES_DS, "name": "Meetings"}]}
    api.objects[("GET", f"data_sources/{NOTES_DS}")] = {
        "object": "data_source",
        "title": [{"plain_text": "Meetings"}],
        "properties": {"Meeting": prop("title"), "Date": prop("date"), "Attendees": prop("people")},
    }
    sink = make(api, tasks_database_id=None, notes_database_id=NOTES_DB)
    await sink.push_notes(meeting, NOTES)
    body = api.bodies("POST", "pages")[0]
    assert body["parent"] == {"type": "data_source_id", "data_source_id": NOTES_DS}
    assert body["properties"] == {
        "Meeting": {"title": text("Weekly sync")},
        "Date": {"date": {"start": meeting.started_at.isoformat()}},
    }
    assert await sink.check() == "connected as voicestream (Acme); notes → Meetings"


async def test_long_notes_are_appended(make, api: FakeNotion, meeting: Meeting) -> None:
    long_notes = NOTES + "\n" + "\n\n".join(f"## Topic {i}\n\n" + "discussion " * 200 for i in range(30))
    await make(api, notes_parent_page_id=NOTES_PAGE).push_notes(meeting, long_notes)
    first = api.bodies("POST", "pages")[0]["markdown"]
    rest = [b["insert_content"]["content"] for b in api.bodies("PATCH", f"pages/{PAGE1}/markdown")]
    assert rest and "\n\n".join([first, *rest]) == long_notes.split("\n\n", 1)[1].rstrip("\n")


async def test_repushed_notes_replace_the_existing_page(make, api: FakeNotion, meeting: Meeting) -> None:
    meeting.notes_ref["notion"] = URL1
    assert await make(api, notes_parent_page_id=NOTES_PAGE).push_notes(meeting, NOTES) == URL1
    assert api.calls() == [("PATCH", f"pages/{PAGE1}/markdown")]
    assert api.bodies("PATCH", f"pages/{PAGE1}/markdown") == [
        {"type": "replace_content", "replace_content": {"new_str": NOTES.split("\n\n", 1)[1]}}
    ]


async def test_no_notes_target_means_no_notes(make, api: FakeNotion, meeting: Meeting) -> None:
    assert await make(api).push_notes(meeting, NOTES) is None
    assert api.requests == []


# ------------------------------------------------------------------ updates


async def test_update_patches_every_mapped_property(make, api: FakeNotion, meeting: Meeting) -> None:
    sink = make(api)
    item = action()
    ref = await sink.push_item(item, meeting)
    item.title, item.status, item.owner, item.due_date, item.priority = "Send the final deck", ItemStatus.DONE, None, None, None
    await sink.update_item(item, meeting, ref)
    assert api.bodies("PATCH", f"pages/{PAGE1}") == [
        {
            "properties": {
                "Task name": {"title": text("Send the final deck")},
                "Status": {"status": {"name": "Done"}},
                "Due date": {"date": None},
                "Assignee": {"people": []},
                "Priority": {"select": None},
                "Meeting": {"rich_text": text("Weekly sync")},
            }
        }
    ]


async def test_dismissed_uses_the_cancelled_option(make, api: FakeNotion, meeting: Meeting) -> None:
    sink = make(api)
    item = action()
    ref = await sink.push_item(item, meeting)
    item.dismissed = True
    await sink.update_item(item, meeting, ref)
    (body,) = api.bodies("PATCH", f"pages/{PAGE1}")
    assert body["properties"]["Status"] == {"status": {"name": "Cancelled"}}
    assert body["properties"]["Task name"] == {"title": text(item.title)}
    assert "in_trash" not in body


async def test_dismissed_without_cancel_option_is_prefixed_and_trashed(make, meeting: Meeting) -> None:
    api = FakeNotion({"Task": prop("title"), "Status": status({"To-do": ["Not started"], "Complete": ["Done"]})})
    sink = make(api, trash_dismissed=True)
    item = action()
    ref = await sink.push_item(item, meeting)
    item.dismissed = True
    await sink.update_item(item, meeting, ref)
    item.dismissed = False  # undo
    await sink.update_item(item, meeting, ref)
    dismissed, restored = api.bodies("PATCH", f"pages/{PAGE1}")
    assert dismissed == {"properties": {"Task": {"title": text(f"[dismissed] {item.title}")}}, "in_trash": True}
    assert restored == {
        "properties": {"Task": {"title": text(item.title)}, "Status": {"status": {"name": "Not started"}}},
        "in_trash": False,
    }


async def test_agent_result_is_appended_once(make, api: FakeNotion, meeting: Meeting) -> None:
    sink = make(api)
    item = action()
    ref = await sink.push_item(item, meeting)
    item.agent = AgentRun(prompt="draft the deck", status=AgentStatus.DONE, result_summary="Drafted `deck.md` with 3 tiers.")
    api.page_markdown = "Include the Q4 discount tiers."
    await sink.update_item(item, meeting, ref)
    api.page_markdown += "\n\n**Agent result:**\n\nDrafted `deck.md` with 3 tiers."
    await sink.update_item(item, meeting, ref)
    assert api.calls().count(("GET", f"pages/{PAGE1}/markdown")) == 2
    assert api.bodies("PATCH", f"pages/{PAGE1}/markdown") == [
        {
            "type": "insert_content",
            "insert_content": {"content": "**Agent result:**\n\nDrafted `deck.md` with 3 tiers.", "position": {"type": "end"}},
        }
    ]


def test_refs_encode_the_page_id() -> None:
    dashed = str(uuid.UUID(PAGE1))
    assert _ref({"id": dashed, "url": URL1}) == URL1
    assert _ref({"id": dashed, "url": "https://app.notion.com/p/send-deck"}) == f"https://app.notion.com/p/send-deck#{PAGE1}"
    assert _ref({"id": dashed}) == PAGE1
    assert _page_id(f"https://app.notion.com/p/send-deck#{PAGE1}") == PAGE1
    assert _page_id(f"https://www.notion.so/acme/Add-beef-{PAGE1}") == PAGE1
    assert _page_id(dashed) == PAGE1
    with pytest.raises(SinkError):
        _page_id("file:///tmp/TODO.md#A1")


# ------------------------------------------------------------------- errors


async def test_rate_limit_waits_for_retry_after(make, api: FakeNotion, meeting: Meeting, monkeypatch) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("voicestream.sinks.http.asyncio.sleep", fake_sleep)
    api.fail[("POST", "pages")] = [error(429, "rate_limited", "You have been rate limited.", **{"Retry-After": "7"})]
    assert await make(api).push_item(action(), meeting) == URL1
    assert slept == [7.0]
    assert api.calls().count(("POST", "pages")) == 2


async def test_overload_is_retried_for_creates_but_5xx_is_not(make, api: FakeNotion, meeting: Meeting, monkeypatch) -> None:
    async def no_sleep(seconds: float) -> None:
        pass

    monkeypatch.setattr("voicestream.sinks.http.asyncio.sleep", no_sleep)
    sink = make(api)
    # 529 service_overload is rejected before processing (Notion's SDK retries it for every method)
    api.fail[("POST", "pages")] = [error(529, "service_overload", "Overloaded")]
    await sink.push_item(action(), meeting)
    assert api.calls().count(("POST", "pages")) == 2
    # a 500 on a create may have been applied: surface it instead of risking a duplicate page
    api.fail[("POST", "pages")] = [error(500, "internal_server_error", "Oops")]
    with pytest.raises(SinkError) as exc:
        await sink.push_item(action(), meeting)
    assert exc.value.retryable and api.calls().count(("POST", "pages")) == 3
    api.fail[("PATCH", f"pages/{PAGE1}")] = [error(529, "service_overload", "Overloaded")]
    await sink.update_item(action(status=ItemStatus.DONE), meeting, URL1)
    assert api.calls().count(("PATCH", f"pages/{PAGE1}")) == 2


async def test_conflict_is_retryable_and_validation_error_rereads_schema(make, api: FakeNotion, meeting: Meeting) -> None:
    sink = make(api)
    api.fail[("POST", "pages")] = [
        error(409, "conflict_error", "Transaction could not be completed"),
        error(400, "validation_error", "Priority is not a property that exists."),
    ]
    with pytest.raises(SinkError) as conflict:
        await sink.push_item(action(), meeting)
    with pytest.raises(SinkError, match="Priority is not a property") as invalid:
        await sink.push_item(action(), meeting)
    assert conflict.value.retryable and not invalid.value.retryable
    await sink.push_item(action(), meeting)
    assert api.calls().count(("GET", f"data_sources/{DS}")) == 2


async def test_missing_token_fails_at_push_not_construction(make, api: FakeNotion, meeting: Meeting, monkeypatch) -> None:
    monkeypatch.delenv("NOTION_TOKEN")
    sink = make(api)
    with pytest.raises(SinkError, match="NOTION_TOKEN") as exc:
        await sink.push_item(action(), meeting)
    assert not exc.value.retryable
    with pytest.raises(SinkError, match="my-integrations"):
        await sink.check()
    assert api.requests == []
    monkeypatch.setenv("TEAM_NOTION", "secret_team")
    await make(api, token_env="TEAM_NOTION").push_item(action(), meeting)
    assert api.requests[-1].headers["Authorization"] == "Bearer secret_team"


# -------------------------------------------------------------------- check


async def test_check_reports_identity_and_mapping(make, api: FakeNotion) -> None:
    api.objects[("GET", f"pages/{NOTES_PAGE}")] = {"object": "page", "id": NOTES_PAGE, "url": f"https://www.notion.so/Meetings-{NOTES_PAGE}"}
    status_line = await make(api, notes_parent_page_id=NOTES_PAGE).check()
    assert status_line == (
        "connected as voicestream (Acme); "
        "tasks → Tasks [title=Task name, status=Status, due=Due date, owner=Assignee, priority=Priority, meeting=Meeting]; "
        f"notes → under https://www.notion.so/Meetings-{NOTES_PAGE}"
    )
    assert all(r.method == "GET" for r in api.requests)


async def test_check_explains_unshared_database(make, api: FakeNotion) -> None:
    del api.objects[("GET", f"databases/{DB}")]
    with pytest.raises(SinkError, match=f"the tasks database {DB}: share it with your integration") as exc:
        await make(api).check()
    assert not exc.value.retryable


async def test_check_explains_bad_token(make, api: FakeNotion) -> None:
    api.fail[("GET", "users/me")] = [error(401, "unauthorized", "API token is invalid.")]
    with pytest.raises(SinkError, match=r"rejected the token in \$NOTION_TOKEN: API token is invalid"):
        await make(api).check()
    api.fail[("GET", "users/me")] = [httpx.Response(401, text="<html>Unauthorized</html>")]  # e.g. from a proxy
    with pytest.raises(SinkError, match="<html>Unauthorized"):
        await make(api).check()


# ------------------------------------------------------------------- config


def test_config_needs_a_target_and_rejects_unknown_keys() -> None:
    with pytest.raises(ValidationError, match="tasks_database_id"):
        NotionConfig(enabled=True)
    with pytest.raises(ValidationError):
        NotionConfig.model_validate({"tasks_database_id": DB, "database": DB})
    with pytest.raises(ValidationError):
        NotionConfig.model_validate({"tasks_database_id": DB, "properties": {"assignee": "Who"}})


def test_registry_builds_the_sink(cfg: Config) -> None:
    cfg.sinks = {"notion": {"enabled": True, "tasks_database_id": DB}}
    assert sink_class("notion") is NotionSink
    (sink,) = build_sinks(cfg)
    assert isinstance(sink, NotionSink) and sink.supports_update and sink.supports_notes
