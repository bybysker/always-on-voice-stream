"""GitHub, Slack, webhook and Todoist sinks against mocked HTTP APIs."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from voicestream.config import Config
from voicestream.dispatch import Dispatcher
from voicestream.models import (
    AgentRun,
    AgentStatus,
    Item,
    ItemKind,
    ItemStatus,
    Meeting,
)
from voicestream.sinks.base import SinkError, build_sinks, item_markdown
from voicestream.sinks.github import (
    BODY_MAX,
    TITLE_MAX,
    GitHubConfig,
    GitHubSink,
    issue_api_path,
    last_pushed_state,
)
from voicestream.sinks.slack import (
    BLOCKS_MAX,
    HEADER_MAX,
    SECTION_MAX,
    SlackConfig,
    SlackSink,
)
from voicestream.sinks.todoist import CONTENT_MAX, TASK_URL, TodoistConfig, TodoistSink
from voicestream.sinks.webhook import MEETING_FIELDS, WebhookConfig, WebhookSink

Reply = dict[str, Any] | list[Any] | str | httpx.Response | Exception


class FakeApi:
    """Answers requests in order from ``replies`` and records them.

    A dict/list becomes HTTP 200 JSON, a str HTTP 200 text; an exception is raised."""

    def __init__(self, sink: Any, *replies: Reply) -> None:
        self.calls: list[httpx.Request] = []
        self.replies = list(replies)
        sink._client = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, httpx.Response):
            return reply
        if isinstance(reply, str):
            return httpx.Response(200, text=reply)
        return httpx.Response(200, json=reply)

    def body(self, i: int = -1) -> Any:
        return json.loads(self.calls[i].content)


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return slept


def action(**kw: Any) -> Item:
    fields: dict[str, Any] = {
        "id": "A1",
        "kind": ItemKind.ACTION,
        "title": "Send the pricing deck",
        "owner": "Sam",
        "due_date": "2026-10-16",
        "priority": "high",
        "confidence": 0.9,
        "evidence": "Sam, can you send the deck by Friday?",
    }
    return Item(**(fields | kw))


# ===================================================================== GitHub

GH_API = "https://api.github.com"
ISSUE_HTML = "https://github.com/acme/roadmap/issues/42"
ISSUE_API = f"{GH_API}/repos/acme/roadmap/issues/42"


def gh_issue(*, title: str = "Send the pricing deck", body: str = "", state: str = "open", labels: tuple[str, ...] = (), assignees: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "number": 42,
        "url": ISSUE_API,
        "html_url": ISSUE_HTML,
        "title": title,
        "body": body,
        "state": state,
        "labels": [{"id": i, "name": n} for i, n in enumerate(labels)],
        "assignees": [{"login": a} for a in assignees],
    }


def gh_created(**kw: Any) -> httpx.Response:
    return httpx.Response(201, json=gh_issue(**kw))


def marker(meeting: Meeting, state: str, item_id: str = "A1") -> str:
    return f"<!-- voicestream meeting={meeting.id} item={item_id} state={state} -->"


@pytest.fixture
def gh_token(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_xyz")
    return "github_pat_xyz"


def github(cfg: Config, **kw: Any) -> GitHubSink:
    return GitHubSink(GitHubConfig(enabled=True, repo="acme/roadmap", **kw), cfg)


async def test_github_create_sends_issue_with_headers_labels_and_assignee(cfg: Config, meeting: Meeting, gh_token: str) -> None:
    sink = github(cfg, assignees={"SAM": "samdev"}, priority_labels={"high": "priority: high"})
    api = FakeApi(sink, gh_created())
    item = action()

    ref = await sink.push_item(item, meeting)

    assert ref == ISSUE_HTML
    req = api.calls[0]
    assert (req.method, str(req.url)) == ("POST", f"{GH_API}/repos/acme/roadmap/issues")
    assert req.headers["Authorization"] == f"Bearer {gh_token}"
    assert req.headers["Accept"] == "application/vnd.github+json"
    assert req.headers["X-GitHub-Api-Version"] == "2026-03-10"
    body = api.body()
    assert body["title"] == "Send the pricing deck"
    assert body["body"].startswith(item_markdown(item, meeting))
    assert body["body"].endswith(marker(meeting, "open"))
    assert body["labels"] == ["meeting-action", "priority: high"]
    assert body["assignees"] == ["samdev"]
    assert "assignee" not in body  # removed in API version 2026-03-10


async def test_github_owner_mapping_is_explicit(cfg: Config, meeting: Meeting, gh_token: str) -> None:
    sink = github(cfg, assignees={"me": "alexk"})
    api = FakeApi(sink, gh_created(), gh_created())

    await sink.push_item(action(owner="Alex"), meeting)  # cfg.user_name is Alex
    await sink.push_item(action(owner="Ashley"), meeting)

    assert api.body(0)["assignees"] == ["alexk"]
    assert "assignees" not in api.body(1)
    assert "**Owner:** Ashley" in api.body(1)["body"]


async def test_github_rejected_assignee_falls_back_to_unassigned(cfg: Config, meeting: Meeting, gh_token: str) -> None:
    sink = github(cfg, assignees={"sam": "not-a-collaborator"})
    invalid = {"message": "Validation Failed", "errors": [{"resource": "Issue", "field": "assignees", "code": "invalid"}]}
    api = FakeApi(sink, httpx.Response(422, json=invalid), gh_created())

    assert await sink.push_item(action(), meeting) == ISSUE_HTML

    assert api.body(0)["assignees"] == ["not-a-collaborator"]
    assert "assignees" not in api.body(1) and api.body(1)["title"] == "Send the pricing deck"


async def test_github_other_validation_errors_are_not_retried(cfg: Config, meeting: Meeting, gh_token: str) -> None:
    sink = github(cfg)
    api = FakeApi(sink, httpx.Response(422, json={"message": "Validation Failed", "errors": [{"field": "title", "code": "missing"}]}))

    with pytest.raises(SinkError, match="title missing") as err:
        await sink.push_item(action(), meeting)

    assert not err.value.retryable and len(api.calls) == 1


async def test_github_item_already_done_is_closed_after_create(cfg: Config, meeting: Meeting, gh_token: str) -> None:
    sink = github(cfg)
    api = FakeApi(sink, gh_created(), gh_issue(state="closed"))
    item = action(status=ItemStatus.DONE)

    assert await sink.push_item(item, meeting) == ISSUE_HTML

    assert (api.calls[1].method, str(api.calls[1].url)) == ("PATCH", ISSUE_API)
    patch = api.body(1)
    assert patch["state"] == "closed" and patch["state_reason"] == "completed"
    assert patch["body"].endswith(marker(meeting, "completed"))


async def test_github_failed_close_after_create_still_returns_the_ref(cfg: Config, meeting: Meeting, gh_token: str) -> None:
    sink = github(cfg)
    FakeApi(sink, gh_created(), httpx.Response(404, json={"message": "Not Found"}))

    # raising would make the dispatcher create a second issue
    assert await sink.push_item(action(status=ItemStatus.DONE), meeting) == ISSUE_HTML


async def test_github_update_swaps_only_managed_labels_and_assignees(cfg: Config, meeting: Meeting, gh_token: str) -> None:
    sink = github(cfg, assignees={"sam": "samdev", "ashley": "ashdev"}, priority_labels={"high": "priority: high", "low": "priority: low"})
    current = gh_issue(
        body=f"old body\n\n{marker(meeting, 'open')}",
        labels=("meeting-action", "bug", "Priority: High"),
        assignees=("samdev", "octocat"),
    )
    api = FakeApi(sink, current, gh_issue())
    item = action(title="Send the revised pricing deck", owner="Ashley", priority="low", due_date="2026-10-20", status=ItemStatus.DONE)

    await sink.update_item(item, meeting, ISSUE_HTML)

    assert [(c.method, str(c.url)) for c in api.calls] == [("GET", ISSUE_API), ("PATCH", ISSUE_API)]
    patch = api.body(1)
    assert patch["title"] == "Send the revised pricing deck"
    assert "**Due:** 2026-10-20" in patch["body"] and "**Owner:** Ashley" in patch["body"]
    assert patch["labels"] == ["meeting-action", "bug", "priority: low"]  # a person's "bug" label survives
    assert patch["assignees"] == ["octocat", "ashdev"]  # so does a person's assignee
    assert patch["state"] == "closed" and patch["state_reason"] == "completed"


async def test_github_unchanged_issue_gets_no_patch(cfg: Config, meeting: Meeting, gh_token: str) -> None:
    sink = github(cfg)
    item = action()
    body = f"{item_markdown(item, meeting)}\n\n{marker(meeting, 'open')}"
    api = FakeApi(sink, gh_issue(body=body, labels=("meeting-action",)))

    await sink.update_item(item, meeting, ISSUE_HTML)

    assert len(api.calls) == 1


@pytest.mark.parametrize(
    ("status", "dismissed", "reason"),
    [(ItemStatus.CANCELLED, False, "not_planned"), (ItemStatus.OPEN, True, "not_planned"), (ItemStatus.DONE, False, "completed")],
)
async def test_github_closing_states(
    cfg: Config, meeting: Meeting, gh_token: str, status: ItemStatus, dismissed: bool, reason: str
) -> None:
    sink = github(cfg)
    api = FakeApi(sink, gh_issue(body=marker(meeting, "open")), gh_issue())

    await sink.update_item(action(status=status, dismissed=dismissed), meeting, ISSUE_HTML)

    patch = api.body(1)
    assert patch["state"] == "closed" and patch["state_reason"] == reason
    assert last_pushed_state(patch["body"]) == reason
    assert all(c.method != "DELETE" for c in api.calls)


async def test_github_reopens_only_what_voicestream_closed(cfg: Config, meeting: Meeting, gh_token: str) -> None:
    sink = github(cfg)
    api = FakeApi(
        sink,
        gh_issue(state="closed", body=marker(meeting, "completed")),  # we closed it; the item is open again
        gh_issue(),
        gh_issue(state="closed", body=marker(meeting, "open")),  # a person closed it in GitHub
        gh_issue(),
    )

    await sink.update_item(action(), meeting, ISSUE_HTML)
    await sink.update_item(action(title="Send the deck v2"), meeting, ISSUE_HTML)

    assert api.body(1)["state"] == "open"
    assert "state" not in api.body(3) and api.body(3)["title"] == "Send the deck v2"


async def test_github_secondary_rate_limit_403_is_retried(cfg: Config, meeting: Meeting, gh_token: str, no_sleep: list[float]) -> None:
    sink = github(cfg)
    limited = {"message": "You have exceeded a secondary rate limit. Please wait a few minutes before you try again."}
    FakeApi(
        sink,
        httpx.Response(403, json=limited, headers={"retry-after": "30"}),
        httpx.Response(403, json=limited),  # no headers: wait at least a minute
        gh_created(),
    )

    assert await sink.push_item(action(), meeting) == ISSUE_HTML

    assert no_sleep == [30.0, 120.0]


async def test_github_long_primary_rate_limit_is_left_to_the_dispatcher(
    cfg: Config, meeting: Meeting, gh_token: str, no_sleep: list[float]
) -> None:
    sink = github(cfg)
    reset = str(int(datetime.now(UTC).timestamp()) + 1800)
    api = FakeApi(
        sink,
        httpx.Response(403, json={"message": "API rate limit exceeded"}, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": reset}),
    )

    with pytest.raises(SinkError, match="rate limited") as err:
        await sink.push_item(action(), meeting)

    assert err.value.retryable and no_sleep == [] and len(api.calls) == 1


async def test_github_permission_403_is_not_retryable(cfg: Config, meeting: Meeting, gh_token: str, no_sleep: list[float]) -> None:
    sink = github(cfg)
    FakeApi(sink, httpx.Response(403, json={"message": "Resource not accessible by personal access token"}))

    with pytest.raises(SinkError, match="Issues: read and write") as err:
        await sink.push_item(action(), meeting)

    assert not err.value.retryable and no_sleep == []


async def test_github_bad_token_is_not_retryable(cfg: Config, meeting: Meeting, gh_token: str) -> None:
    FakeApi(sink := github(cfg), httpx.Response(401, json={"message": "Bad credentials"}))

    with pytest.raises(SinkError, match="Bad credentials — check GITHUB_TOKEN") as err:
        await sink.push_item(action(), meeting)

    assert not err.value.retryable


async def test_github_follows_transferred_issues(cfg: Config, meeting: Meeting, gh_token: str) -> None:
    moved = f"{GH_API}/repos/acme/platform/issues/7"
    api = FakeApi(
        sink := github(cfg),
        httpx.Response(301, headers={"location": moved}),
        gh_issue(body=marker(meeting, "open")) | {"url": moved},
        gh_issue(),
    )

    await sink.update_item(action(title="Moved"), meeting, ISSUE_HTML)

    assert [(c.method, str(c.url)) for c in api.calls] == [("GET", ISSUE_API), ("GET", moved), ("PATCH", moved)]


async def test_github_truncates_to_api_limits(cfg: Config, meeting: Meeting, gh_token: str) -> None:
    sink = github(cfg)
    api = FakeApi(sink, gh_created())
    agent = AgentRun(prompt="draft it", status=AgentStatus.DONE, result_summary="x" * 70_000)

    await sink.push_item(action(title="t" * 300, agent=agent), meeting)

    body = api.body()
    assert len(body["title"]) == TITLE_MAX and body["title"].endswith("…")
    assert len(body["body"]) <= BODY_MAX and body["body"].endswith(marker(meeting, "open"))


async def test_github_check(cfg: Config, gh_token: str) -> None:
    sink = github(cfg)
    repo = {"full_name": "acme/roadmap", "has_issues": True, "archived": False, "permissions": {"push": True}}
    api = FakeApi(
        sink,
        repo,
        repo | {"permissions": {"push": False}},
        repo | {"has_issues": False},
        repo | {"archived": True},
        httpx.Response(404, json={"message": "Not Found"}),
    )

    assert await sink.check() == "acme/roadmap · API 2026-03-10"
    assert "no push access" in await sink.check()
    with pytest.raises(SinkError, match="issues are disabled"):
        await sink.check()
    with pytest.raises(SinkError, match="archived"):
        await sink.check()
    with pytest.raises(SinkError, match="exists and the token can access it"):
        await sink.check()
    assert all(c.method == "GET" and str(c.url) == f"{GH_API}/repos/acme/roadmap" for c in api.calls)


async def test_github_missing_token_fails_at_push_not_startup(cfg: Config, meeting: Meeting, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    cfg.sinks = {"github": {"enabled": True, "repo": "acme/roadmap"}}
    [sink] = build_sinks(cfg)
    api = FakeApi(sink)

    for call in (sink.check(), sink.push_item(action(), meeting)):
        with pytest.raises(SinkError, match="GITHUB_TOKEN") as err:
            await call
        assert not err.value.retryable
    assert api.calls == []


def test_github_config_and_refs() -> None:
    with pytest.raises(ValidationError):
        GitHubConfig(repo="just-a-name")
    assert GitHubConfig(repo="acme/roadmap").labels == ["meeting-action"]
    assert issue_api_path(ISSUE_HTML) == "/repos/acme/roadmap/issues/42"
    assert issue_api_path("https://ghe.example.com/team/app/issues/7") == "/repos/team/app/issues/7"
    with pytest.raises(SinkError):
        issue_api_path("https://github.com/acme/roadmap/pull/42")


async def test_github_ref_round_trips_through_the_dispatcher(cfg: Config, meeting: Meeting, gh_token: str) -> None:
    sink = github(cfg)
    item = action()
    meeting.items[item.id] = item
    api = FakeApi(sink, gh_created(), gh_issue(body=marker(meeting, "open")), gh_issue())
    dispatcher = Dispatcher(cfg, meeting, [sink])

    assert await dispatcher.sync(require_stable=False) == 1
    item.status, item.revision = ItemStatus.DONE, item.revision + 1
    assert await dispatcher.sync(require_stable=False) == 1

    assert item.pushed == {"github": ISSUE_HTML}
    assert [c.method for c in api.calls] == ["POST", "GET", "PATCH"]
    assert api.body(2)["state"] == "closed"


# ====================================================================== Slack

SLACK_URL = "https://hooks.slack.com/services/T0001/B0001/XXXXsecretXXXX"


@pytest.fixture
def slack_url(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("SLACK_WEBHOOK_URL", SLACK_URL)
    return SLACK_URL


def slack(cfg: Config, **kw: Any) -> SlackSink:
    return SlackSink(SlackConfig(enabled=True, **kw), cfg)


def full_meeting() -> Meeting:
    start = datetime(2026, 10, 14, 14, 0, tzinfo=UTC)
    m = Meeting(title="Weekly sync", started_at=start, ended_at=start + timedelta(minutes=45), participants=["Alex", "Sam"])
    m.tldr = "Pricing goes live Monday; Sam owns the deck."
    items = [
        action(pushed={"github": ISSUE_HTML, "markdown": "file:///tmp/TODO.md#x"}),
        action(id="A2", title="Draft the launch email", owner="Assistant", due_date=None, priority=None,
               agent=AgentRun(prompt="draft", status=AgentStatus.DONE, result_summary="done"), status=ItemStatus.DONE),
        action(id="A3", title="Book the venue", owner=None, due_date=None, status=ItemStatus.CANCELLED),
        action(id="A4", title="Ignored", dismissed=True),
        Item(id="D1", kind=ItemKind.DECISION, title="Ship pricing on Monday"),
        Item(id="Q1", kind=ItemKind.QUESTION, title="Who pays for travel?"),
    ]
    m.items = {i.id: i for i in items}
    m.notes_ref = {
        "notion": "https://www.notion.so/Weekly-sync-abc",
        "markdown": "file:///tmp/notes.md",
        "webhook": "https://hooks.zapier.com#20261014-140000",  # the webhook's fallback ref: not a page
    }
    return m


async def test_slack_digest(cfg: Config, slack_url: str) -> None:
    sink = slack(cfg, users={"sam": "U0SAM"})
    api = FakeApi(sink, "ok")
    m = full_meeting()

    assert await sink.push_notes(m, "# full notes") == f"slack:{m.id}"

    req = api.calls[0]
    assert (req.method, str(req.url)) == ("POST", SLACK_URL)
    msg = api.body()
    assert msg["text"].startswith("Weekly sync: 3 action items, 1 decision. Pricing goes live Monday")
    blocks = msg["blocks"]
    assert blocks[0] == {"type": "header", "text": {"type": "plain_text", "text": "Weekly sync"}}
    assert blocks[1]["elements"][0]["text"] == "<!date^1791986400^{date_short_pretty} at {time}|" + (
        m.started_at.astimezone().strftime("%Y-%m-%d %H:%M") + "> · 45 min · Alex, Sam"
    )
    texts = [b["text"]["text"] for b in blocks if b["type"] == "section"]
    assert texts[0] == m.tldr
    actions, decisions = texts[1].splitlines(), texts[2].splitlines()
    assert actions[0] == "*Action items (3)*"
    assert actions[1] == f"• <{ISSUE_HTML}|Send the pricing deck> · <@U0SAM> · due 2026-10-16"
    assert actions[2] == "• ✅ Draft the launch email · @Assistant · 🤖 done"
    assert actions[3] == "• ~Book the venue~"
    assert decisions == ["*Decisions (1)*", "• Ship pricing on Monday"]
    assert "Ignored" not in json.dumps(msg)  # dismissed
    # no file:// links, nor the webhook's ref
    assert blocks[-1]["elements"][0]["text"] == "Full notes: <https://www.notion.so/Weekly-sync-abc|Notion>"


async def test_slack_escapes_mrkdwn(cfg: Config, slack_url: str) -> None:
    sink = slack(cfg)
    api = FakeApi(sink, "ok")
    m = Meeting(title="R&D <sync>", tldr="Use <!channel> & co")

    await sink.push_notes(m, "")

    msg = api.body()
    assert msg["blocks"][2]["text"]["text"] == "Use &lt;!channel&gt; &amp; co"
    assert msg["blocks"][0]["text"]["text"] == "R&D <sync>"  # plain_text header: no escaping
    assert "<!channel>" not in msg["text"]


def test_slack_digest_stays_within_block_kit_limits(cfg: Config) -> None:
    sink = slack(cfg)
    m = Meeting(title="T" * 400, tldr="x" * 5000)
    m.items = {f"A{n}": action(id=f"A{n}", title=f"{n} " + "y" * 400) for n in range(1, 500)}
    m.notes_ref = {"notion": "https://www.notion.so/page"}

    msg = sink.digest(m)

    assert msg is not None
    blocks = msg["blocks"]
    assert len(blocks) == BLOCKS_MAX
    assert len(blocks[0]["text"]["text"]) == HEADER_MAX
    assert all(len(b["text"]["text"]) <= SECTION_MAX for b in blocks if b["type"] == "section")
    assert "50-block limit" in blocks[-2]["elements"][0]["text"]
    assert "notion.so" in blocks[-1]["elements"][0]["text"]  # the notes link survives the cut
    assert len(msg["text"]) <= 3000


async def test_slack_posts_each_digest_once(cfg: Config, slack_url: str) -> None:
    sink = slack(cfg)
    api = FakeApi(sink, "ok")
    m = full_meeting()

    m.notes_ref["slack"] = await sink.push_notes(m, "")
    assert await sink.push_notes(m, "") == f"slack:{m.id}"  # e.g. a later `voicestream push`

    assert len(api.calls) == 1


async def test_slack_skips_meetings_with_nothing_to_report(cfg: Config, slack_url: str, meeting: Meeting) -> None:
    sink = slack(cfg)
    api = FakeApi(sink)
    meeting.items = {"Q1": Item(id="Q1", kind=ItemKind.QUESTION, title="Anyone?")}

    assert await sink.push_notes(meeting, "# notes") is None
    assert api.calls == []


async def test_slack_posts_per_item_only_when_kinds_are_configured(cfg: Config, slack_url: str, meeting: Meeting) -> None:
    item = action()
    assert not slack(cfg).accepts(item, 0.5)

    sink = slack(cfg, kinds=["action"])
    api = FakeApi(sink, "ok")
    assert sink.accepts(item, 0.5)

    ref = await sink.push_item(item, meeting)

    assert ref == f"slack:{meeting.id}/A1" and "hooks.slack.com" not in ref
    assert api.body() == {"text": "*Action item* from _Weekly sync_\n• Send the pricing deck · @Sam · due 2026-10-16"}
    assert not SlackSink.supports_update


async def test_slack_names_your_own_items_after_you(cfg: Config, slack_url: str, meeting: Meeting) -> None:
    api = FakeApi(sink := slack(cfg, kinds=["action"]), "ok")
    await sink.push_item(action(owner="I", due_date=None), meeting)
    assert api.body()["text"].endswith("• Send the pricing deck · @Alex")

    api = FakeApi(sink := slack(cfg, kinds=["action"], users={"alex": "U0ALEX"}), "ok")
    await sink.push_item(action(owner="me", due_date=None), meeting)
    assert api.body()["text"].endswith("• Send the pricing deck · <@U0ALEX>")


async def test_slack_rejected_blocks_fall_back_to_plain_text(cfg: Config, slack_url: str) -> None:
    sink = slack(cfg)
    api = FakeApi(sink, httpx.Response(400, text="invalid_blocks"), "ok")

    await sink.push_notes(full_meeting(), "")

    assert "blocks" in api.body(0)
    assert set(api.body(1)) == {"text"}
    lines = api.body(1)["text"].splitlines()
    assert lines[1:3] == ["*Action items (3)*", f"• <{ISSUE_HTML}|Send the pricing deck> · @Sam · due 2026-10-16"]


@pytest.mark.parametrize(
    ("reply", "retryable", "match"),
    [
        (httpx.Response(404, text="no_service"), False, "no_service .*create a new one"),
        (httpx.Response(403, text="invalid_token"), False, "invalid_token"),
        (httpx.Response(410, text="channel_is_archived"), False, "archived"),
        (httpx.Response(500, text="rollup_error"), False, "HTTP 500.*may have been applied anyway"),
    ],
)
async def test_slack_errors(cfg: Config, slack_url: str, reply: httpx.Response, retryable: bool, match: str) -> None:
    sink = slack(cfg)
    api = FakeApi(sink, reply)

    with pytest.raises(SinkError, match=match) as err:
        await sink.push_notes(full_meeting(), "")

    assert err.value.retryable is retryable
    assert "XXXXsecretXXXX" not in str(err.value)
    assert len(api.calls) == 1  # a POST is never blindly retried on 5xx


async def test_slack_transport_errors_never_leak_the_url(cfg: Config, slack_url: str, no_sleep: list[float]) -> None:
    sink = slack(cfg)
    FakeApi(sink, *[httpx.ConnectError("connection refused")] * 4)

    with pytest.raises(SinkError) as err:
        await sink.push_notes(full_meeting(), "")

    assert err.value.retryable
    assert "XXXXsecretXXXX" not in str(err.value) and "https://hooks.slack.com/…" in str(err.value)


async def test_slack_429_honours_retry_after(cfg: Config, slack_url: str, no_sleep: list[float]) -> None:
    sink = slack(cfg)
    api = FakeApi(sink, httpx.Response(429, text="rate_limited", headers={"retry-after": "2"}), "ok")

    await sink.push_notes(full_meeting(), "")

    assert no_sleep == [2.0] and len(api.calls) == 2


async def test_slack_check_validates_the_url_without_posting(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    sink = slack(cfg)
    api = FakeApi(sink)

    with pytest.raises(SinkError, match="SLACK_WEBHOOK_URL"):
        await sink.check()
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://example.com/hook")
    with pytest.raises(SinkError, match="doesn't look like an incoming-webhook URL"):
        await sink.check()
    monkeypatch.setenv("SLACK_WEBHOOK_URL", SLACK_URL)
    assert "not test-posted" in await sink.check()
    assert api.calls == []


# ==================================================================== webhook

HOOK_URL = "https://hooks.zapier.com/hooks/catch/123/abcsecret/"


def webhook(cfg: Config, **kw: Any) -> WebhookSink:
    return WebhookSink(WebhookConfig(enabled=True, **kw), cfg)


async def test_webhook_item_created_is_signed(cfg: Config, meeting: Meeting, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOICESTREAM_WEBHOOK_SECRET", "s3cret")
    sink = webhook(cfg, url=HOOK_URL)
    api = FakeApi(sink, httpx.Response(204))
    item = action()

    ref = await sink.push_item(item, meeting)

    assert ref == "https://hooks.zapier.com#A1"
    req = api.calls[0]
    assert (req.method, str(req.url)) == ("POST", HOOK_URL)
    assert req.headers["Content-Type"] == "application/json"
    assert req.headers["X-Voicestream-Event"] == "item.created"
    expected = "sha256=" + hmac.new(b"s3cret", req.content, hashlib.sha256).hexdigest()
    assert hmac.compare_digest(req.headers["X-Voicestream-Signature"], expected)
    body = api.body()
    assert body["event"] == "item.created"
    assert body["item"] == json.loads(item.model_dump_json())
    assert body["item_markdown"] == item_markdown(item, meeting)
    assert set(body["meeting"]) == MEETING_FIELDS and body["meeting"]["title"] == "Weekly sync"
    assert "notes_markdown" not in body and "ref" not in body


async def test_webhook_unsigned_without_secret(cfg: Config, meeting: Meeting, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VOICESTREAM_WEBHOOK_SECRET", raising=False)
    sink = webhook(cfg, url=HOOK_URL)
    api = FakeApi(sink, "ok")

    await sink.push_item(action(), meeting)

    assert "X-Voicestream-Signature" not in api.calls[0].headers
    assert "unsigned" in await sink.check()


@pytest.mark.parametrize(
    ("reply", "ref"),
    [({"url": "https://crm.example.com/tasks/9", "id": 9}, "https://crm.example.com/tasks/9"), ({"id": 42}, "42"), ([1, 2], "https://hooks.zapier.com#A1")],
)
async def test_webhook_ref_from_response(cfg: Config, meeting: Meeting, reply: Any, ref: str) -> None:
    sink = webhook(cfg, url=HOOK_URL)
    FakeApi(sink, reply)

    assert await sink.push_item(action(), meeting) == ref


async def test_webhook_update_and_notes_events(cfg: Config, meeting: Meeting) -> None:
    sink = webhook(cfg, url=HOOK_URL)
    api = FakeApi(sink, "ok", "ok", "ok", {"id": "n-1"})
    item = action()

    await sink.push_item(item, meeting)
    item.title, item.revision = "Send the final deck", 2
    await sink.update_item(item, meeting, "crm-77")
    await sink.update_item(item, meeting, "crm-77")  # a retry of the same revision
    notes_ref = await sink.push_notes(meeting, "# Weekly sync\n\n- notes")

    assert [c.headers["X-Voicestream-Event"] for c in api.calls] == ["item.created", "item.updated", "item.updated", "meeting.notes"]
    deliveries = [c.headers["X-Voicestream-Delivery"] for c in api.calls]
    assert deliveries[1] == deliveries[2] and len(set(deliveries)) == 3
    assert api.body(1)["ref"] == "crm-77" and api.body(1)["item"]["title"] == "Send the final deck"
    notes = api.body(3)
    assert notes["event"] == "meeting.notes" and notes["notes_markdown"] == "# Weekly sync\n\n- notes"
    assert "item" not in notes and set(notes["meeting"]) == MEETING_FIELDS
    assert notes_ref == "n-1"


async def test_webhook_url_from_env_is_redacted_in_errors(cfg: Config, meeting: Meeting, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOICESTREAM_WEBHOOK_URL", HOOK_URL)
    sink = webhook(cfg)
    api = FakeApi(sink, httpx.Response(503, text="busy"), httpx.Response(400, text="bad"))

    with pytest.raises(SinkError) as busy:
        await sink.push_item(action(), meeting)
    with pytest.raises(SinkError) as bad:
        await sink.push_item(action(), meeting)

    assert not busy.value.retryable and not bad.value.retryable  # the 503'd delivery may have been processed
    for err in (busy, bad):
        assert "abcsecret" not in str(err.value) and "https://hooks.zapier.com/…" in str(err.value)
    assert len(api.calls) == 2  # the 503 wasn't retried: the receiver may not be idempotent


@pytest.mark.parametrize(
    "lost",
    [httpx.ReadTimeout("no answer"), httpx.ReadError("reset"), httpx.RemoteProtocolError("closed"), httpx.WriteError("broken pipe")],
)
async def test_a_delivery_that_may_have_arrived_is_never_resent(
    cfg: Config, meeting: Meeting, no_sleep: list[float], lost: Exception
) -> None:
    sink = webhook(cfg, url=HOOK_URL)
    FakeApi(sink, lost)
    with pytest.raises(SinkError, match="may have been applied anyway.*Push button") as err:
        await sink.push_item(action(), meeting)
    assert not err.value.retryable and "abcsecret" not in str(err.value)

    api = FakeApi(sink, lost, "ok")
    item = action()
    meeting.items = {item.id: item}
    dispatcher = Dispatcher(cfg, meeting, [sink])
    for _ in range(3):
        assert await dispatcher.sync(require_stable=False) == 0  # neither the mixin nor the dispatcher resends it
    assert len(api.calls) == 1 and item.pushed == {} and no_sleep == []


async def test_a_delivery_that_never_left_is_retried(cfg: Config, meeting: Meeting, no_sleep: list[float]) -> None:
    sink = webhook(cfg, url=HOOK_URL)
    api = FakeApi(sink, httpx.ConnectError("refused"), httpx.ConnectTimeout("no route"), "ok")
    assert await sink.push_item(action(), meeting)
    assert len(api.calls) == 3


async def test_webhook_config_and_check(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VOICESTREAM_WEBHOOK_URL", raising=False)
    assert WebhookConfig().kinds == ["action", "decision", "question", "note"]
    assert WebhookSink.supports_update and WebhookSink.supports_notes
    with pytest.raises(ValidationError):
        WebhookConfig(url="ftp://example.com")
    with pytest.raises(SinkError, match="VOICESTREAM_WEBHOOK_URL"):
        await webhook(cfg).check()
    sink = webhook(cfg, url="http://localhost:8080/voicestream")
    api = FakeApi(sink)
    status = await sink.check()
    assert "http://localhost:8080/…" in status and "/voicestream" not in status and api.calls == []


# ==================================================================== Todoist

TD_API = "https://api.todoist.com/api/v1"
TASK = {"id": "6X7rM8997g3RQmvh", "content": "Send the pricing deck", "priority": 4, "checked": False}


@pytest.fixture
def td_token(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("TODOIST_TOKEN", "td-123")
    return "td-123"


def todoist(cfg: Config, **kw: Any) -> TodoistSink:
    return TodoistSink(TodoistConfig(enabled=True, **kw), cfg)


async def test_todoist_create(cfg: Config, meeting: Meeting, td_token: str) -> None:
    sink = todoist(cfg, project_id="6Jf8VQXxpwv56VQ7", labels=["meetings"])
    api = FakeApi(sink, TASK)
    item = action(owner="Alex")

    ref = await sink.push_item(item, meeting)

    assert ref == f"{TASK_URL}6X7rM8997g3RQmvh" == "https://app.todoist.com/app/task/6X7rM8997g3RQmvh"
    req = api.calls[0]
    assert (req.method, str(req.url)) == ("POST", f"{TD_API}/tasks")
    assert req.headers["Authorization"] == f"Bearer {td_token}"
    assert uuid.UUID(req.headers["X-Request-Id"]).version == 4
    assert api.body() == {
        "content": "Send the pricing deck",
        "description": item_markdown(item, meeting),
        "priority": 4,
        "due_date": "2026-10-16",
        "project_id": "6Jf8VQXxpwv56VQ7",
        "labels": ["meetings"],
    }


@pytest.mark.parametrize(("priority", "api_priority"), [("high", 4), ("medium", 3), ("low", 2), (None, 1)])
async def test_todoist_priority_is_inverted(cfg: Config, meeting: Meeting, td_token: str, priority: str | None, api_priority: int) -> None:
    sink = todoist(cfg)
    api = FakeApi(sink, TASK)

    await sink.push_item(action(priority=priority), meeting)

    assert api.body()["priority"] == api_priority


def test_todoist_defaults_to_my_items(cfg: Config) -> None:
    sink = todoist(cfg)
    assert TodoistConfig().owners == "mine"
    assert sink.accepts(action(owner="Alex"), 0.5) and sink.accepts(action(owner="me"), 0.5)
    assert not sink.accepts(action(owner="Sam"), 0.5)


async def test_todoist_create_retries_5xx_with_the_same_request_id(cfg: Config, meeting: Meeting, td_token: str, no_sleep: list[float]) -> None:
    sink = todoist(cfg)
    api = FakeApi(sink, httpx.Response(503, text="unavailable"), TASK)

    await sink.push_item(action(), meeting)

    assert len(api.calls) == 2 and len(no_sleep) == 1
    assert api.calls[0].headers["X-Request-Id"] == api.calls[1].headers["X-Request-Id"]


async def test_todoist_unparseable_due_phrase_is_dropped(cfg: Config, meeting: Meeting, td_token: str) -> None:
    cfg.transcription.language = "en"
    sink = todoist(cfg)
    api = FakeApi(sink, httpx.Response(400, json={"error": "Invalid date"}), TASK)

    await sink.push_item(action(due_date=None, due_text="after the offsite"), meeting)

    assert api.body(0)["due_string"] == "after the offsite" and api.body(0)["due_lang"] == "en"
    assert "due_string" not in api.body(1) and "due_lang" not in api.body(1)
    assert "after the offsite" in api.body(1)["description"]
    assert api.calls[0].headers["X-Request-Id"] != api.calls[1].headers["X-Request-Id"]


async def test_todoist_other_400s_are_not_retried(cfg: Config, meeting: Meeting, td_token: str) -> None:
    sink = todoist(cfg, project_id="nope")
    api = FakeApi(sink, httpx.Response(400, json={"error": "Invalid project_id"}))

    with pytest.raises(SinkError, match="Invalid project_id") as err:
        await sink.push_item(action(), meeting)

    assert not err.value.retryable and len(api.calls) == 1


async def test_todoist_update_fields(cfg: Config, meeting: Meeting, td_token: str) -> None:
    sink = todoist(cfg)
    api = FakeApi(sink, TASK, TASK)
    item = action(title="Send the final deck", priority="low", due_date=None, due_text="tomorrow")

    await sink.update_item(item, meeting, f"{TASK_URL}6X7rM8997g3RQmvh")
    item.due_date = "2026-10-15"
    await sink.update_item(item, meeting, f"{TASK_URL}6X7rM8997g3RQmvh")

    assert [(c.method, str(c.url)) for c in api.calls] == [("POST", f"{TD_API}/tasks/6X7rM8997g3RQmvh")] * 2
    first, resolved = api.body(0), api.body(1)
    assert (first["content"], first["priority"]) == ("Send the final deck", 2)
    # re-parsed on a later day, "tomorrow" would move the task: phrases only go up on create
    assert not {"due_string", "due_lang", "due_date"} & set(first)
    assert resolved["due_date"] == "2026-10-15" and "due_string" not in resolved


async def test_todoist_due_phrase_uses_the_meeting_language(cfg: Config, meeting: Meeting, td_token: str) -> None:
    assert cfg.transcription.language is None  # auto-detect, the default
    sink = todoist(cfg)
    api = FakeApi(sink, TASK, TASK)

    await sink.push_item(action(due_date=None, due_text="vendredi prochain"), meeting)
    meeting.language = "fr"
    await sink.push_item(action(id="A2", due_date=None, due_text="vendredi prochain"), meeting)

    assert "due_lang" not in api.body(0)
    assert (api.body(1)["due_string"], api.body(1)["due_lang"]) == ("vendredi prochain", "fr")


async def test_todoist_never_clears_a_due_date(cfg: Config, meeting: Meeting, td_token: str) -> None:
    sink = todoist(cfg)
    api = FakeApi(sink, TASK)

    await sink.update_item(action(due_date=None), meeting, f"{TASK_URL}abc")

    assert not {"due_date", "due_string", "due_datetime"} & set(api.body())


@pytest.mark.parametrize(
    ("status", "dismissed"), [(ItemStatus.DONE, False), (ItemStatus.CANCELLED, False), (ItemStatus.OPEN, True)]
)
async def test_todoist_settled_items_close_the_task(
    cfg: Config, meeting: Meeting, td_token: str, status: ItemStatus, dismissed: bool
) -> None:
    sink = todoist(cfg)
    api = FakeApi(sink, TASK, httpx.Response(204))

    await sink.update_item(action(status=status, dismissed=dismissed), meeting, f"{TASK_URL}abc")

    assert [(c.method, str(c.url)) for c in api.calls] == [("POST", f"{TD_API}/tasks/abc"), ("POST", f"{TD_API}/tasks/abc/close")]
    assert all(c.method != "DELETE" for c in api.calls)


async def test_todoist_closing_an_already_completed_task_is_fine(cfg: Config, meeting: Meeting, td_token: str) -> None:
    sink = todoist(cfg)
    FakeApi(sink, TASK, httpx.Response(404, json={"error": "Task not found"}))

    await sink.update_item(action(status=ItemStatus.DONE), meeting, f"{TASK_URL}abc")


async def test_todoist_item_already_done_is_created_then_closed(cfg: Config, meeting: Meeting, td_token: str) -> None:
    sink = todoist(cfg)
    api = FakeApi(sink, TASK, httpx.Response(403, text="Forbidden"))

    # the close failing must not fail the create, or the dispatcher would add it twice
    assert await sink.push_item(action(status=ItemStatus.DONE), meeting) == f"{TASK_URL}{TASK['id']}"
    assert str(api.calls[1].url) == f"{TD_API}/tasks/{TASK['id']}/close"


async def test_todoist_create_without_an_id_back_is_an_error(cfg: Config, meeting: Meeting, td_token: str) -> None:
    FakeApi(sink := todoist(cfg), httpx.Response(200))

    with pytest.raises(SinkError, match="no id") as err:
        await sink.push_item(action(), meeting)

    assert not err.value.retryable


async def test_todoist_auth_errors(cfg: Config, meeting: Meeting, td_token: str, monkeypatch: pytest.MonkeyPatch) -> None:
    sink = todoist(cfg)
    api = FakeApi(sink, httpx.Response(401, text="Unauthorized"))

    with pytest.raises(SinkError, match="check TODOIST_TOKEN") as err:
        await sink.push_item(action(), meeting)
    assert not err.value.retryable

    monkeypatch.delenv("TODOIST_TOKEN")
    with pytest.raises(SinkError, match="TODOIST_TOKEN is not set"):
        await sink.check()
    assert len(api.calls) == 1


async def test_todoist_truncates_content(cfg: Config, meeting: Meeting, td_token: str) -> None:
    sink = todoist(cfg)
    api = FakeApi(sink, TASK)

    await sink.push_item(action(title="z" * 900), meeting)

    assert len(api.body()["content"]) == CONTENT_MAX


async def test_todoist_check_is_read_only(cfg: Config, td_token: str) -> None:
    api = FakeApi(sink := todoist(cfg), {"results": [{"id": "1", "name": "Inbox"}, {"id": "2", "name": "Work"}], "next_cursor": None})
    assert await sink.check() == "token ok · 2 projects · new tasks go to your Inbox · only items you own"
    assert (api.calls[0].method, str(api.calls[0].url)) == ("GET", f"{TD_API}/projects")

    api = FakeApi(sink := todoist(cfg, project_id="2", owners="all"), {"id": "2", "name": "Work"}, httpx.Response(404, text="Not found"))
    assert await sink.check() == "token ok · new tasks go to project Work"
    with pytest.raises(SinkError, match="404"):
        await sink.check()
    assert all(c.method == "GET" and str(c.url) == f"{TD_API}/projects/2" for c in api.calls)
