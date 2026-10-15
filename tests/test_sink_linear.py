from __future__ import annotations

import asyncio
import json
import uuid
from datetime import timedelta
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from voicestream.config import Config
from voicestream.models import Item, ItemKind, ItemStatus, Meeting
from voicestream.sinks.base import SinkError, build_sinks, item_markdown
from voicestream.sinks.linear import (
    API_URL,
    ISSUE,
    ISSUE_CREATE,
    ISSUE_UPDATE,
    MEMBERS,
    TEAM,
    LinearConfig,
    LinearSink,
    issue_uuid,
    match_member,
)

ISSUE_URL = "https://linear.app/acme/issue/ENG-42/send-the-pricing-deck"
REF = f"{ISSUE_URL}#issue-uuid"
TEAM_REPLY = {
    "data": {
        "viewer": {"name": "Alex Kim"},
        "teams": {
            "nodes": [
                {
                    "id": "team-1",
                    "key": "ENG",
                    "name": "Engineering",
                    "states": {
                        "nodes": [
                            {"id": "st-todo", "type": "unstarted", "position": 1},
                            {"id": "st-shipped", "type": "completed", "position": 5},
                            {"id": "st-done", "type": "completed", "position": 4},
                            {"id": "st-dupe", "type": "canceled", "position": 7},
                            {"id": "st-canceled", "type": "canceled", "position": 6},
                        ]
                    },
                }
            ]
        },
    }
}
CREATED = {"data": {"issueCreate": {"success": True, "issue": {"id": "issue-uuid", "identifier": "ENG-42", "url": ISSUE_URL}}}}
UPDATED = {"data": {"issueUpdate": {"success": True}}}
NOT_FOUND = {"data": None, "errors": [{"message": "Entity not found: Issue", "extensions": {"code": "INVALID_INPUT"}}]}
MEMBERS_REPLY = {
    "data": {
        "team": {
            "members": {
                "nodes": [
                    {"id": "u-alex", "name": "Alex Kim", "displayName": "alex"},
                    {"id": "u-sam", "name": "Samantha Reyes", "displayName": "sam"},
                    {"id": "u-priya", "name": "Priya Natarajan", "displayName": "pn"},
                    {"id": "u-chris1", "name": "Chris Lee", "displayName": "chrisl"},
                    {"id": "u-chris2", "name": "Chris Park", "displayName": "cpark"},
                ]
            }
        }
    }
}


class FakeApi:
    """Answers requests in order from ``responses`` (dicts become HTTP 200 JSON) and records them."""

    def __init__(self, sink: LinearSink, *responses: dict[str, Any] | httpx.Response) -> None:
        self.calls: list[httpx.Request] = []
        self.responses = list(responses)
        sink._client = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        r = self.responses.pop(0)
        return r if isinstance(r, httpx.Response) else httpx.Response(200, json=r)

    def body(self, i: int) -> dict[str, Any]:
        return json.loads(self.calls[i].content)


@pytest.fixture
def key(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_xyz")
    return "lin_api_xyz"


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return slept


def make(cfg: Config, **kw: Any) -> LinearSink:
    return LinearSink(LinearConfig(enabled=True, **({"team_key": "eng"} | kw)), cfg)


def action(**kw: Any) -> Item:
    fields: dict[str, Any] = dict(
        id="A1",
        kind=ItemKind.ACTION,
        title="Send the pricing deck",
        owner="Sam",
        due_date="2026-10-16",
        priority="high",
        evidence="Sam, can you send the deck by Friday?",
    )
    return Item(**(fields | kw))


# ------------------------------------------------------------------ create


async def test_push_resolves_team_key_and_creates_issue(cfg: Config, meeting: Meeting, key: str) -> None:
    sink = make(cfg, project_id="proj-1", label_ids=["lbl-1"], assignees={"sam": "u-sam"})
    api = FakeApi(sink, TEAM_REPLY, CREATED)

    ref = await sink.push_item(action(), meeting)

    assert ref == REF
    assert all(str(c.url) == API_URL and c.method == "POST" for c in api.calls)
    assert all(c.headers["Authorization"] == key for c in api.calls)  # personal key: no "Bearer"
    assert api.body(0) == {"query": TEAM, "variables": {"filter": {"key": {"eqIgnoreCase": "eng"}}}}
    assert api.body(1) == {
        "query": ISSUE_CREATE,
        "variables": {
            "input": {
                "teamId": "team-1",
                "title": "Send the pricing deck",
                "description": item_markdown(action(), meeting),
                "dueDate": "2026-10-16",
                "priority": 2,
                "assigneeId": "u-sam",
                "projectId": "proj-1",
                "labelIds": ["lbl-1"],
                "id": issue_uuid("team-1", meeting.id, meeting.started_at.isoformat(), "A1"),
            }
        },
    }


def test_issue_ids_are_stable_v4_uuids() -> None:
    first = issue_uuid("team-1", "20261014-090000", "A1")
    assert first == issue_uuid("team-1", "20261014-090000", "A1")
    assert first != issue_uuid("team-1", "20261014-090000", "A2")
    assert uuid.UUID(first).version == 4


async def test_meetings_started_in_the_same_second_get_distinct_issue_ids(cfg: Config, key: str) -> None:
    sink = make(cfg)
    api = FakeApi(sink, TEAM_REPLY, CREATED, CREATED)
    first = Meeting(id="20261014-090000")
    second = Meeting(id=first.id, started_at=first.started_at + timedelta(milliseconds=300))

    await sink.push_item(action(), first)
    await sink.push_item(action(), second)

    assert api.body(1)["variables"]["input"]["id"] != api.body(2)["variables"]["input"]["id"]


async def test_team_lookup_is_cached(cfg: Config, meeting: Meeting, key: str) -> None:
    sink = make(cfg, team_id="team-1", team_key="IGNORED")
    api = FakeApi(sink, TEAM_REPLY, CREATED, CREATED)

    await sink.push_item(action(), meeting)
    await sink.push_item(action(id="A2"), meeting)

    assert [api.body(i)["query"] for i in range(3)] == [TEAM, ISSUE_CREATE, ISSUE_CREATE]
    assert api.body(0)["variables"] == {"filter": {"id": {"eq": "team-1"}}}


@pytest.mark.parametrize(("priority", "expected"), [("high", 2), ("medium", 3), ("low", 4), (None, 0)])
async def test_priority_mapping(cfg: Config, meeting: Meeting, key: str, priority: str | None, expected: int) -> None:
    sink = make(cfg)
    api = FakeApi(sink, TEAM_REPLY, CREATED)

    await sink.push_item(action(priority=priority), meeting)

    assert api.body(1)["variables"]["input"]["priority"] == expected


async def test_unknown_owner_and_due_are_omitted(cfg: Config, meeting: Meeting, key: str) -> None:
    sink = make(cfg)
    api = FakeApi(sink, TEAM_REPLY, CREATED)

    await sink.push_item(action(owner="Priya", due_date=None), meeting)

    sent = api.body(1)["variables"]["input"]
    assert "assigneeId" not in sent and "dueDate" not in sent and "stateId" not in sent
    assert "**Owner:** Priya" in sent["description"]
    assert len(api.calls) == 2  # match_members is off: no member lookup


async def test_own_items_use_the_user_name_mapping(cfg: Config, meeting: Meeting, key: str) -> None:
    sink = make(cfg, assignees={"Alex": "u-alex"})
    api = FakeApi(sink, TEAM_REPLY, CREATED)

    await sink.push_item(action(owner="me"), meeting)

    assert api.body(1)["variables"]["input"]["assigneeId"] == "u-alex"


async def test_member_auto_match(cfg: Config, meeting: Meeting, key: str) -> None:
    sink = make(cfg, match_members=True)
    owners = ["Sam", "Priya", "Chris", "Chris Lee", "I", "Assistant"]
    api = FakeApi(sink, TEAM_REPLY, MEMBERS_REPLY, *[CREATED] * len(owners))

    for owner in owners:
        await sink.push_item(action(owner=owner), meeting)

    assert api.body(1) == {"query": MEMBERS, "variables": {"team": "team-1"}}
    assigned = [api.body(i)["variables"]["input"].get("assigneeId") for i in range(2, len(api.calls))]
    assert assigned == ["u-sam", "u-priya", None, "u-chris1", "u-alex", None]


def test_match_member_rules() -> None:
    members = MEMBERS_REPLY["data"]["team"]["members"]["nodes"]
    assert match_member("SAM", members) == "u-sam"  # display name, case-insensitive
    assert match_member("samantha reyes", members) == "u-sam"  # full name
    assert match_member("Samantha", members) == "u-sam"  # unique first name
    assert match_member("Chris", members) is None  # ambiguous first name
    assert match_member("Priya Smith", members) is None  # no fuzzy matching
    assert match_member("Pri", members) is None
    assert match_member(" ", members) is None


async def test_done_item_is_created_completed(cfg: Config, meeting: Meeting, key: str) -> None:
    sink = make(cfg)
    api = FakeApi(sink, TEAM_REPLY, CREATED)

    await sink.push_item(action(status=ItemStatus.DONE), meeting)

    assert api.body(1)["variables"]["input"]["stateId"] == "st-done"


# ------------------------------------------------------------------ update


@pytest.mark.parametrize(
    ("changes", "state"),
    [
        ({"status": ItemStatus.DONE}, "st-done"),  # first "completed" state by position
        ({"status": ItemStatus.CANCELLED}, "st-canceled"),
        ({"dismissed": True}, "st-canceled"),
        ({}, None),  # open: never moved back out of a state set in Linear
    ],
)
async def test_update_maps_status(cfg: Config, meeting: Meeting, key: str, changes: dict[str, Any], state: str | None) -> None:
    sink = make(cfg)
    api = FakeApi(sink, TEAM_REPLY, UPDATED)

    await sink.update_item(action(**changes), meeting, REF)

    body = api.body(1)
    assert body["query"] == ISSUE_UPDATE and body["variables"]["id"] == "issue-uuid"
    assert body["variables"]["input"].get("stateId") == state


async def test_update_sends_title_due_priority_assignee(cfg: Config, meeting: Meeting, key: str) -> None:
    sink = make(cfg, assignees={"priya": "u-priya"}, label_ids=["lbl-1"], project_id="proj-1")
    api = FakeApi(sink, TEAM_REPLY, UPDATED)
    item = action(title="Send the final deck", owner="Priya", due_date=None, priority="low")

    await sink.update_item(item, meeting, REF)

    assert api.body(1)["variables"]["input"] == {
        "title": "Send the final deck",
        "description": item_markdown(item, meeting),
        "dueDate": None,  # a removed due date is cleared
        "priority": 4,
        "assigneeId": "u-priya",
    }


async def test_update_without_canceled_state_leaves_state_alone(cfg: Config, meeting: Meeting, key: str) -> None:
    sink = make(cfg)
    team = json.loads(json.dumps(TEAM_REPLY))
    team["data"]["teams"]["nodes"][0]["states"]["nodes"] = [{"id": "st-todo", "type": "unstarted", "position": 1}]
    api = FakeApi(sink, team, UPDATED)

    await sink.update_item(action(dismissed=True), meeting, REF)

    assert "stateId" not in api.body(1)["variables"]["input"]


async def test_update_failure_raises(cfg: Config, meeting: Meeting, key: str) -> None:
    sink = make(cfg)
    FakeApi(sink, TEAM_REPLY, {"data": {"issueUpdate": {"success": False}}})

    with pytest.raises(SinkError, match="issueUpdate failed"):
        await sink.update_item(action(), meeting, REF)


# ------------------------------------------------------------------ errors


async def test_graphql_errors_raise_with_message(cfg: Config, meeting: Meeting, key: str) -> None:
    sink = make(cfg)
    error = {
        "message": "Argument Validation Error",
        "extensions": {"code": "INVALID_INPUT", "userPresentableMessage": "dueDate must be a valid date"},
    }
    api = FakeApi(sink, TEAM_REPLY, {"data": None, "errors": [error]}, NOT_FOUND)

    with pytest.raises(SinkError, match=r"dueDate must be a valid date \[INVALID_INPUT\]") as err:
        await sink.push_item(action(), meeting)
    assert err.value.retryable is False
    assert api.body(2) == {"query": ISSUE, "variables": {"id": issue_uuid("team-1", meeting.id, meeting.started_at.isoformat(), "A1")}}


async def test_ratelimited_http_400_is_retryable(cfg: Config, meeting: Meeting, key: str) -> None:
    sink = make(cfg)
    limited = httpx.Response(400, json={"errors": [{"message": "Rate limit exceeded", "extensions": {"code": "RATELIMITED"}}]})
    api = FakeApi(sink, limited)

    with pytest.raises(SinkError, match="RATELIMITED") as err:
        await sink.push_item(action(), meeting)
    assert err.value.retryable is True
    assert len(api.calls) == 1  # left to the dispatcher's retry, not a blocking sleep


async def test_http_429_is_retried(cfg: Config, meeting: Meeting, key: str, no_sleep: list[float]) -> None:
    sink = make(cfg)
    api = FakeApi(sink, TEAM_REPLY, httpx.Response(429, headers={"Retry-After": "2"}), CREATED)

    assert await sink.push_item(action(), meeting) == REF
    assert no_sleep == [2.0] and len(api.calls) == 3


async def test_retried_create_finds_the_issue_made_by_a_lost_attempt(
    cfg: Config, meeting: Meeting, key: str, no_sleep: list[float]
) -> None:
    """The client-chosen id turns a duplicate create into an error, then a lookup."""
    sink = make(cfg)
    exists = {"data": None, "errors": [{"message": "Entity already exists"}]}
    found = {"data": {"issue": {"id": "issue-uuid", "url": ISSUE_URL}}}
    api = FakeApi(sink, TEAM_REPLY, httpx.Response(503, text="unavailable"), exists, found, UPDATED)
    item = action()

    assert await sink.push_item(item, meeting) == REF
    assert api.body(1) == api.body(2)  # same input, same id
    assert api.body(3)["query"] == ISSUE and api.body(3)["variables"]["id"] == api.body(1)["variables"]["input"]["id"]
    # the found issue may predate the item's latest revision, so it's brought up to date
    update = api.body(4)
    assert update["query"] == ISSUE_UPDATE and update["variables"]["id"] == "issue-uuid"
    assert update["variables"]["input"]["description"] == item_markdown(item, meeting)


async def test_non_json_reply_raises(cfg: Config, meeting: Meeting, key: str) -> None:
    sink = make(cfg)
    FakeApi(sink, httpx.Response(400, text="Bad Request"))

    with pytest.raises(SinkError, match="non-JSON") as err:
        await sink.check()
    assert err.value.retryable is False


async def test_unsuccessful_create_raises(cfg: Config, meeting: Meeting, key: str) -> None:
    sink = make(cfg)
    FakeApi(sink, TEAM_REPLY, {"data": {"issueCreate": {"success": False, "issue": None}}})

    with pytest.raises(SinkError, match="issueCreate failed"):
        await sink.push_item(action(), meeting)


async def test_missing_key_fails_at_push_not_at_startup(cfg: Config, meeting: Meeting, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    cfg.sinks = {"linear": {"enabled": True, "team_key": "ENG"}}
    [sink] = build_sinks(cfg)
    assert isinstance(sink, LinearSink)

    with pytest.raises(SinkError, match="LINEAR_API_KEY") as err:
        await sink.push_item(action(), meeting)
    assert err.value.retryable is False
    with pytest.raises(SinkError, match="LINEAR_API_KEY"):
        await sink.check()


def test_team_is_required(cfg: Config) -> None:
    with pytest.raises(ValidationError, match="team_id or team_key"):
        LinearConfig(enabled=True)
    cfg.sinks = {"linear": {"enabled": True}}
    assert build_sinks(cfg) == []


# ------------------------------------------------------------------- check


async def test_check_reports_viewer_and_team(cfg: Config, meeting: Meeting, key: str) -> None:
    sink = make(cfg)
    api = FakeApi(sink, TEAM_REPLY, TEAM_REPLY, CREATED)

    await sink.check()
    assert await sink.check() == "signed in as Alex Kim · team ENG (Engineering)"
    await sink.push_item(action(), meeting)  # reuses the team from check()

    assert [api.body(i)["query"] for i in range(3)] == [TEAM, TEAM, ISSUE_CREATE]


async def test_check_reports_unknown_team(cfg: Config, key: str) -> None:
    sink = make(cfg, team_key="NOPE")
    FakeApi(sink, {"data": {"viewer": {"name": "Alex Kim"}, "teams": {"nodes": []}}})

    with pytest.raises(SinkError, match="team 'NOPE' not found") as err:
        await sink.check()
    assert err.value.retryable is False


async def test_check_reports_bad_key(cfg: Config, key: str) -> None:
    sink = make(cfg)
    error = {"message": "Authentication required, not authenticated", "extensions": {"code": "AUTHENTICATION_ERROR", "statusCode": 401}}
    FakeApi(sink, httpx.Response(400, json={"errors": [error]}))

    with pytest.raises(SinkError, match="not authenticated") as err:
        await sink.check()
    assert err.value.retryable is False
