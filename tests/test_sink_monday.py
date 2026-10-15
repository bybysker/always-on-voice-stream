from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from voicestream.config import Config
from voicestream.models import Item, ItemKind, ItemStatus, Meeting
from voicestream.sinks.base import SinkError, build_sinks, item_markdown
from voicestream.sinks.monday import (
    API_URL,
    CHANGE_COLUMNS,
    CHECK,
    CREATE_ITEM,
    CREATE_UPDATE,
    UPDATE_CHUNK,
    MondayConfig,
    MondaySink,
    chunks,
    to_html,
)

ITEM_URL = "https://acme.monday.com/boards/123/pulses/456"
CREATED = {"data": {"create_item": {"id": "456", "url": ITEM_URL}}}
UPDATED = {"data": {"create_update": {"id": "u1"}}}
COLUMNS = {"data": {"change_multiple_column_values": {"id": "456"}}}


class FakeApi:
    """Answers requests in order from ``responses`` (dicts become HTTP 200 JSON) and records them."""

    def __init__(self, sink: MondaySink, *responses: dict[str, Any] | httpx.Response) -> None:
        self.calls: list[httpx.Request] = []
        self.responses = list(responses)
        sink._client = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        r = self.responses.pop(0)
        return r if isinstance(r, httpx.Response) else httpx.Response(200, json=r)

    def body(self, i: int) -> dict[str, Any]:
        return json.loads(self.calls[i].content)

    def columns(self, i: int) -> dict[str, Any] | None:
        raw = self.body(i)["variables"]["columns"]
        assert raw is None or isinstance(raw, str)  # column_values goes over the wire as a JSON *string*
        return None if raw is None else json.loads(raw)


@pytest.fixture
def token(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("MONDAY_TOKEN", "tok-123")
    return "tok-123"


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return slept


MAPPED = dict(
    group_id="topics",
    status_column="status",
    date_column="date4",
    owner_column="person",
    owner_ids={"sam": 42, "Alex": 7},
    owner_text_column="text0",
    priority_column="priority",
    details_column="long_text",
)


def make(cfg: Config, **kw: Any) -> MondaySink:
    return MondaySink(MondayConfig(enabled=True, board_id=123, **kw), cfg)


def action(**kw: Any) -> Item:
    fields: dict[str, Any] = dict(
        id="A1",
        kind=ItemKind.ACTION,
        title="Send the pricing deck",
        detail="Include the Q4 numbers",
        owner="Sam",
        due_date="2026-10-16",
        priority="high",
        evidence="Sam, can you send the deck by Friday?",
    )
    return Item(**(fields | kw))


# ------------------------------------------------------------------ create


async def test_push_creates_item_with_mapped_columns_and_details_update(cfg: Config, meeting: Meeting, token: str) -> None:
    sink = make(cfg, **MAPPED)
    api = FakeApi(sink, CREATED, UPDATED)

    ref = await sink.push_item(action(), meeting)

    assert ref == f"{ITEM_URL}#456"
    create = api.calls[0]
    assert str(create.url) == API_URL and create.method == "POST"
    assert create.headers["Authorization"] == token  # raw token, no "Bearer"
    assert create.headers["API-Version"] == "2026-10"
    assert len(create.headers["Idempotency-Key"]) == 36
    body = api.body(0)
    assert body["query"] == CREATE_ITEM
    assert {k: v for k, v in body["variables"].items() if k != "columns"} == {
        "board": "123",
        "group": "topics",
        "name": "Send the pricing deck",
        "labels": True,
    }
    assert api.columns(0) == {
        "status": {"label": "Working on it"},
        "date4": {"date": "2026-10-16"},
        "person": {"personsAndTeams": [{"id": 42, "kind": "person"}]},
        "text0": "Sam",
        "priority": {"label": "High"},
        "long_text": {"text": "Include the Q4 numbers"},
    }
    update = api.body(1)
    assert update["query"] == CREATE_UPDATE
    assert update["variables"]["item"] == "456" and update["variables"]["parent"] is None
    html_body = update["variables"]["body"]
    assert html_body == to_html(item_markdown(action(), meeting))
    assert "<blockquote>Sam, can you send the deck by Friday?</blockquote>" in html_body
    assert "<b>Owner:</b> Sam" in html_body
    assert api.calls[1].headers["Idempotency-Key"] != create.headers["Idempotency-Key"]


async def test_people_column_only_for_explicitly_mapped_owners(cfg: Config, meeting: Meeting, token: str) -> None:
    sink = make(cfg, **MAPPED)
    api = FakeApi(sink, CREATED, UPDATED, CREATED, UPDATED)

    await sink.push_item(action(owner="Priya"), meeting)
    await sink.push_item(action(owner="me"), meeting)  # the user's own item → their user_name

    assert "person" not in api.columns(0) and api.columns(0)["text0"] == "Priya"
    assert api.columns(2)["person"] == {"personsAndTeams": [{"id": 7, "kind": "person"}]}


async def test_push_without_column_mapping_sends_no_column_values(cfg: Config, meeting: Meeting, token: str) -> None:
    sink = make(cfg)
    api = FakeApi(sink, {"data": {"create_item": {"id": "456", "url": None}}}, UPDATED)

    assert await sink.push_item(action(), meeting) == "456"
    assert api.columns(0) is None
    assert api.body(0)["variables"]["group"] is None


async def test_push_truncates_to_monday_limits(cfg: Config, meeting: Meeting, token: str) -> None:
    sink = make(cfg, details_column="long_text")
    api = FakeApi(sink, CREATED, UPDATED)

    await sink.push_item(action(title="x" * 300, detail="d" * 3000), meeting)

    assert len(api.body(0)["variables"]["name"]) == 255
    assert len(api.columns(0)["long_text"]["text"]) == 2000


async def test_done_item_is_created_in_done_state(cfg: Config, meeting: Meeting, token: str) -> None:
    sink = make(cfg, status_column="status", status_labels={"done": "Complete"})
    api = FakeApi(sink, CREATED, UPDATED)

    await sink.push_item(action(status=ItemStatus.DONE), meeting)

    assert api.columns(0) == {"status": {"label": "Complete"}}


async def test_failed_details_update_still_returns_ref(cfg: Config, meeting: Meeting, token: str) -> None:
    """The item exists by then; raising would make the dispatcher create a duplicate."""
    sink = make(cfg)
    api = FakeApi(sink, CREATED, {"errors": [{"message": "Update body is invalid"}]})

    assert await sink.push_item(action(), meeting) == f"{ITEM_URL}#456"
    assert len(api.calls) == 2


async def test_writes_are_retried_on_5xx_with_the_same_idempotency_key(
    cfg: Config, meeting: Meeting, token: str, no_sleep: list[float]
) -> None:
    sink = make(cfg)
    api = FakeApi(sink, httpx.Response(502, text="bad gateway"), CREATED, httpx.Response(503), UPDATED, httpx.Response(500), COLUMNS)

    assert await sink.push_item(action(), meeting) == f"{ITEM_URL}#456"
    await sink.update_item(action(), meeting, f"{ITEM_URL}#456")  # column changes are idempotent

    keys = [c.headers.get("Idempotency-Key") for c in api.calls]
    assert keys[0] == keys[1] and keys[2] == keys[3] and keys[0] != keys[2]
    assert keys[4] is None and keys[5] is None


# ------------------------------------------------------------------ update


async def test_update_changes_name_and_columns(cfg: Config, meeting: Meeting, token: str) -> None:
    sink = make(cfg, **MAPPED)
    api = FakeApi(sink, COLUMNS)

    await sink.update_item(
        action(title="Send the final deck", status=ItemStatus.DONE, due_date="2026-10-17", priority="low"), meeting, f"{ITEM_URL}#456"
    )

    body = api.body(0)
    assert body["query"] == CHANGE_COLUMNS
    assert {k: v for k, v in body["variables"].items() if k != "columns"} == {"board": "123", "item": "456", "labels": True}
    assert api.columns(0) == {
        "name": "Send the final deck",
        "status": {"label": "Done"},
        "date4": {"date": "2026-10-17"},
        "person": {"personsAndTeams": [{"id": 42, "kind": "person"}]},
        "text0": "Sam",
        "priority": {"label": "Low"},
        "long_text": {"text": "Include the Q4 numbers"},
    }


async def test_update_marks_dismissed_and_cancelled_without_deleting(cfg: Config, meeting: Meeting, token: str) -> None:
    sink = make(cfg, status_column="status")
    api = FakeApi(sink, COLUMNS, COLUMNS)

    await sink.update_item(action(dismissed=True), meeting, f"{ITEM_URL}#456")
    await sink.update_item(action(status=ItemStatus.CANCELLED), meeting, "456")  # bare-id ref

    assert api.columns(0) == {"name": "[Dismissed] Send the pricing deck", "status": {"label": "Stuck"}}
    assert api.columns(1) == {"name": "Send the pricing deck", "status": {"label": "Stuck"}}
    assert api.body(1)["variables"]["item"] == "456"
    assert all("delete" not in api.body(i)["query"] and "archive" not in api.body(i)["query"] for i in range(2))


async def test_update_of_open_item_leaves_board_status_alone(cfg: Config, meeting: Meeting, token: str) -> None:
    sink = make(cfg, status_column="status", date_column="date4")
    api = FakeApi(sink, COLUMNS)

    await sink.update_item(action(due_date=None), meeting, f"{ITEM_URL}#456")

    assert api.columns(0) == {"name": "Send the pricing deck"}


# ------------------------------------------------------------------ errors


async def test_graphql_error_array_on_http_200_raises(cfg: Config, meeting: Meeting, token: str) -> None:
    sink = make(cfg)
    FakeApi(
        sink,
        {
            "errors": [{"message": "Column value is invalid", "extensions": {"code": "ColumnValueException", "status_code": 200}}],
            "account_id": 1,
        },
    )

    with pytest.raises(SinkError, match=r"Column value is invalid \[ColumnValueException\]") as err:
        await sink.push_item(action(), meeting)
    assert err.value.retryable is False


@pytest.mark.parametrize(
    "error",
    [
        {
            "message": "Complexity budget exhausted",
            "extensions": {"code": "COMPLEXITY_BUDGET_EXHAUSTED", "status_code": 429, "retry_in_seconds": 13},
        },
        {"message": "Too many concurrent requests", "extensions": {"code": "maxConcurrencyExceeded"}},
        {"message": "Internal server error", "extensions": {"status_code": 500}},
    ],
)
async def test_rate_limit_and_complexity_errors_are_retryable(cfg: Config, meeting: Meeting, token: str, error: dict[str, Any]) -> None:
    sink = make(cfg)
    FakeApi(sink, {"data": {"create_item": None}, "errors": [error]})

    with pytest.raises(SinkError) as err:
        await sink.push_item(action(), meeting)
    assert err.value.retryable is True


async def test_legacy_error_shape_raises(cfg: Config, meeting: Meeting, token: str) -> None:
    sink = make(cfg)
    FakeApi(sink, {"error_code": "ComplexityException", "error_message": "Complexity budget exhausted", "status_code": 429})

    with pytest.raises(SinkError, match="Complexity budget exhausted") as err:
        await sink.push_item(action(), meeting)
    assert err.value.retryable is True


async def test_http_429_is_retried_after_retry_after(cfg: Config, meeting: Meeting, token: str, no_sleep: list[float]) -> None:
    sink = make(cfg)
    limited = httpx.Response(429, headers={"Retry-After": "3"}, json={"errors": [{"message": "Rate Limit Exceeded"}]})
    api = FakeApi(sink, limited, CREATED, UPDATED)

    assert await sink.push_item(action(), meeting) == f"{ITEM_URL}#456"
    assert no_sleep == [3.0] and len(api.calls) == 3


async def test_http_400_with_errors_is_not_retryable(cfg: Config, meeting: Meeting, token: str) -> None:
    sink = make(cfg)
    FakeApi(sink, httpx.Response(400, json={"errors": [{"message": 'Parse error on "}"'}]}))

    with pytest.raises(SinkError, match="Parse error") as err:
        await sink.update_item(action(), meeting, "456")
    assert err.value.retryable is False


@pytest.mark.parametrize(
    ("reply", "match", "retryable"),
    [
        (httpx.Response(200, text="<html>proxy</html>"), "non-JSON", True),
        ({"data": {"create_item": None}}, "returned no item", False),
    ],
)
async def test_malformed_replies_raise(
    cfg: Config, meeting: Meeting, token: str, reply: dict[str, Any] | httpx.Response, match: str, retryable: bool
) -> None:
    sink = make(cfg)
    FakeApi(sink, reply)

    with pytest.raises(SinkError, match=match) as err:
        await sink.push_item(action(), meeting)
    assert err.value.retryable is retryable


async def test_missing_token_fails_at_push_not_at_startup(cfg: Config, meeting: Meeting) -> None:
    cfg.sinks = {"monday": {"enabled": True, "board_id": 123}}
    [sink] = build_sinks(cfg)
    assert isinstance(sink, MondaySink) and sink.cfg.board_id == "123"

    with pytest.raises(SinkError, match="MONDAY_TOKEN") as err:
        await sink.push_item(action(), meeting)
    assert err.value.retryable is False
    with pytest.raises(SinkError, match="MONDAY_TOKEN"):
        await sink.check()


def test_board_id_is_required(cfg: Config) -> None:
    cfg.sinks = {"monday": {"enabled": True}}
    assert build_sinks(cfg) == []


# ------------------------------------------------------------------- notes


async def test_no_notes_without_notes_group(cfg: Config, meeting: Meeting, token: str) -> None:
    sink = make(cfg)
    api = FakeApi(sink)

    assert await sink.push_notes(meeting, "# Notes") is None
    assert api.calls == []


async def test_notes_become_an_item_with_threaded_updates(cfg: Config, meeting: Meeting, token: str) -> None:
    sink = make(cfg, notes_group_id="notes", status_column="status")
    notes = "# Weekly sync\n\n" + "a" * (UPDATE_CHUNK - 100) + "\n\n" + "b" * 500
    api = FakeApi(sink, CREATED, UPDATED, {"data": {"create_update": {"id": "u2"}}})

    ref = await sink.push_notes(meeting, notes)

    assert ref == f"{ITEM_URL}#456"
    create = api.body(0)["variables"]
    assert create["group"] == "notes" and create["name"] == "Meeting notes: Weekly sync"
    assert create["columns"] is None
    first, reply = api.body(1)["variables"], api.body(2)["variables"]
    assert first["item"] == "456" and first["parent"] is None and first["body"].startswith("<p><b>Weekly sync</b></p>")
    assert reply["item"] == "456" and reply["parent"] == "u1" and reply["body"] == "<p>" + "b" * 500 + "</p>"


async def test_notes_repush_adds_update_to_the_same_item(cfg: Config, meeting: Meeting, token: str) -> None:
    sink = make(cfg, notes_group_id="notes")
    meeting.notes_ref["monday"] = f"{ITEM_URL}#456"
    api = FakeApi(sink, UPDATED)

    assert await sink.push_notes(meeting, "Updated notes") == f"{ITEM_URL}#456"
    assert [api.body(0)["query"]] == [CREATE_UPDATE]
    assert api.body(0)["variables"]["item"] == "456"


# ------------------------------------------------------------------- check


def board(**kw: Any) -> dict[str, Any]:
    return {
        "data": {
            "me": {"name": "Alex Doe"},
            "boards": [
                {
                    "name": "Team tasks",
                    "groups": [{"id": "topics"}, {"id": "notes"}],
                    "columns": [
                        {"id": "name", "title": "Name", "type": "name"},
                        {"id": "status", "title": "Status", "type": "status"},
                        {"id": "date4", "title": "Due", "type": "date"},
                    ],
                }
            ],
            **kw,
        }
    }


async def test_check_reports_user_and_board(cfg: Config, token: str) -> None:
    sink = make(cfg, group_id="topics", notes_group_id="notes", status_column="status", date_column="date4")
    api = FakeApi(sink, board())

    assert await sink.check() == "signed in as Alex Doe · board 'Team tasks' · 2 column(s) mapped"
    assert api.body(0) == {"query": CHECK, "variables": {"board": "123"}}
    assert api.calls[0].headers["API-Version"] == "2026-10"


async def test_check_rejects_wrong_column_type_and_lists_options(cfg: Config, token: str) -> None:
    sink = make(cfg, date_column="status")
    FakeApi(sink, board())

    with pytest.raises(SinkError, match=r"date_column 'status' is not a date column.*date4 \(Due\)"):
        await sink.check()


async def test_check_rejects_unknown_group(cfg: Config, token: str) -> None:
    sink = make(cfg, group_id="nope")
    FakeApi(sink, board())

    with pytest.raises(SinkError, match="group_id 'nope'.*notes, topics"):
        await sink.check()


async def test_check_reports_missing_board(cfg: Config, token: str) -> None:
    sink = make(cfg)
    FakeApi(sink, board(boards=[]))

    with pytest.raises(SinkError, match="board 123 not found"):
        await sink.check()


async def test_check_reports_bad_token(cfg: Config, token: str) -> None:
    sink = make(cfg)
    FakeApi(sink, httpx.Response(401, json={"errors": [{"message": "Not Authenticated"}]}))

    with pytest.raises(SinkError, match="401") as err:
        await sink.check()
    assert err.value.retryable is False


# ------------------------------------------------------------------- helpers


def test_to_html_renders_the_markdown_sinks_produce() -> None:
    md = "# Notes\n\n- [ ] Ship it\n- [x] Book room\n- plain & <safe>\n\n> quoted\n\n**Owner:** Sam · _from [deck](https://x.test/a?b=1&c=2)_"
    assert to_html(md) == (
        "<p><b>Notes</b></p>"
        "<p>☐ Ship it<br>☑ Book room<br>• plain &amp; &lt;safe&gt;</p>"
        "<blockquote>quoted</blockquote>"
        '<p><b>Owner:</b> Sam · <i>from <a href="https://x.test/a?b=1&amp;c=2">deck</a></i></p>'
    )
    assert to_html("snake_case_name stays") == "<p>snake_case_name stays</p>"


def test_chunks_respect_size_and_paragraphs() -> None:
    assert chunks("a\n\nb\n\nc", 4) == ["a\n\nb", "c"]
    assert chunks("x" * 10, 4) == ["xxxx", "xxxx", "xx"]
    assert all(len(c) <= 50 for c in chunks("\n\n".join(["word " * 7] * 20), 50))
