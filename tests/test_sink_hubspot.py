"""HubSpot sink against a fake CRM (httpx.MockTransport): exact endpoints and payloads,
owner lookup, unique-match rules with accents, meeting logging, updates and errors."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest

from voicestream.config import Config
from voicestream.dispatch import Dispatcher
from voicestream.models import FollowUpEmail, Item, ItemKind, ItemStatus, Meeting
from voicestream.sinks import hubspot
from voicestream.sinks.base import SinkError, build_sinks, item_markdown
from voicestream.sinks.hubspot import (
    BODY_MAX,
    CONTACT_PROPERTIES,
    HubSpotConfig,
    HubSpotSink,
    fold,
    rich_text,
)

TOKEN = "pat-eu1-00000000-test"
OWNERS = "/crm/owners/2026-09"
OBJ = "/crm/objects/2026-09"
OWNER = {
    "id": "77",
    "email": "Alex.Martin@gmail.com",
    "firstName": "Alex",
    "lastName": "Martin",
    "userId": 4242,
    "type": "PERSON",
    "archived": False,
}
LEA = {
    "id": "101",
    "properties": {"firstname": "Lea", "lastname": "Dupont", "email": "lea@acme.fr", "associatedcompanyid": "9001"},
}
PAUL = {
    "id": "102",
    "properties": {"firstname": "Paul", "lastname": "Dupont", "email": "paul@dupont.fr", "associatedcompanyid": None},
}
HELENE = {"id": "103", "properties": {"firstname": "Helene", "lastname": "Lefevre", "email": None, "associatedcompanyid": "9002"}}
ACME = {"id": "9001", "properties": {"name": "ACME"}}
ACME_LABS = {"id": "9003", "properties": {"name": "Acme Labs"}}

Reply = dict[str, Any] | httpx.Response
Route = Reply | list[Reply] | Callable[[httpx.Request], Reply]


def crm_search(records: list[dict[str, Any]]) -> Callable[[httpx.Request], Reply]:
    """HubSpot search semantics: OR of filter groups, AND inside one, EQ ignores case (not accents)."""

    def handle(request: httpx.Request) -> Reply:
        body = json.loads(request.content)
        hits = [
            r
            for r in records
            if any(
                all(str(r["properties"].get(f["propertyName"]) or "").lower() == f["value"].lower() for f in g["filters"])
                for g in body["filterGroups"]
            )
        ]
        return {"total": len(hits), "results": hits[: body["limit"]]}

    return handle


class FakeHubSpot:
    """Routes ``(method, path)`` to replies (a list is answered in order, its last entry repeats)."""

    def __init__(self, sink: HubSpotSink, routes: dict[tuple[str, str], Route]) -> None:
        self.routes = routes
        self.calls: list[httpx.Request] = []
        sink._client = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        route = self.routes.get((request.method, request.url.path))
        if route is None:
            return httpx.Response(404, json={"status": "error", "message": "no route", "category": "OBJECT_NOT_FOUND"})
        if callable(route):
            reply = route(request)
        elif isinstance(route, list):
            reply = route.pop(0) if len(route) > 1 else route[0]
        else:
            reply = route
        if isinstance(reply, httpx.Response):  # a fresh copy: one reply object may answer several requests
            return httpx.Response(reply.status_code, headers=reply.headers, content=reply.content)
        return httpx.Response(200, json=reply)

    def made(self, method: str, path: str) -> list[httpx.Request]:
        return [c for c in self.calls if c.method == method and c.url.path == path]

    def body(self, method: str, path: str, i: int = -1) -> dict[str, Any]:
        return json.loads(self.made(method, path)[i].content)


def routes(
    contacts: list[dict[str, Any]] | None = None, companies: list[dict[str, Any]] | None = None, **extra: Route
) -> dict[tuple[str, str], Route]:
    base: dict[tuple[str, str], Route] = {
        ("GET", OWNERS): {"results": [OWNER]},
        ("POST", f"{OBJ}/contacts/search"): crm_search(contacts if contacts is not None else [LEA, PAUL, HELENE]),
        ("POST", f"{OBJ}/companies/search"): crm_search(companies if companies is not None else [ACME, ACME_LABS]),
        ("POST", f"{OBJ}/tasks"): httpx.Response(201, json={"id": "555", "properties": {}}),
        ("POST", f"{OBJ}/meetings"): httpx.Response(201, json={"id": "888", "properties": {}}),
        ("POST", f"{OBJ}/notes"): httpx.Response(201, json={"id": "999", "properties": {}}),
    }
    return base | {tuple(k.split(" ", 1)): v for k, v in extra.items()}  # type: ignore[misc]


@pytest.fixture
def token(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("HUBSPOT_TOKEN", TOKEN)
    return TOKEN


@pytest.fixture
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        calls.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return calls


@pytest.fixture
def app(cfg: Config) -> Config:
    cfg.user_name = "Alex"
    cfg.timezone = "Europe/Amsterdam"
    return cfg


@pytest.fixture
def fr_meeting() -> Meeting:
    return Meeting(
        id="20261015-140000",
        title="Point hebdo avec Acme",
        language="fr",
        started_at=datetime(2026, 10, 15, 12, 0, tzinfo=UTC),
        ended_at=datetime(2026, 10, 15, 12, 45, tzinfo=UTC),
        participants=["Léa Dupont", "Marc"],
    )


def make(app: Config, **kw: Any) -> HubSpotSink:
    return HubSpotSink(HubSpotConfig(enabled=True, **({"owner_email": "alex.martin@gmail.com"} | kw)), app)


def action(**kw: Any) -> Item:
    fields: dict[str, Any] = {
        "id": "A1",
        "kind": ItemKind.ACTION,
        "title": "Envoyer le devis à Léa",
        "detail": "Inclure l'option « premium » & les délais <urgent>",
        "owner": "Alex",
        "due_date": "2026-10-16",
        "priority": "high",
        "evidence": "Alex, tu peux envoyer le devis d'ici vendredi ?",
    }
    return Item(**(fields | kw))


def associations(*pairs: tuple[str, int]) -> list[dict[str, Any]]:
    return [{"to": {"id": i}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": t}]} for i, t in pairs]


def search_body(prop: str, *values: str, properties: list[str] = CONTACT_PROPERTIES) -> dict[str, Any]:
    return {
        "filterGroups": [{"filters": [{"propertyName": prop, "operator": "EQ", "value": v}]} for v in values],
        "properties": properties,
        "limit": 200,
    }


# ------------------------------------------------------------------ tasks


async def test_push_creates_task_assigned_due_and_associated(
    app: Config, fr_meeting: Meeting, token: str, slept: list[float]
) -> None:
    sink = make(app)
    api = FakeHubSpot(sink, routes())

    ref = await sink.push_item(action(), fr_meeting)

    assert ref == "hubspot:tasks/555"
    assert all(str(c.url).startswith("https://api.hubapi.com/") for c in api.calls)
    assert all(c.headers["Authorization"] == f"Bearer {TOKEN}" for c in api.calls)
    assert dict(api.made("GET", OWNERS)[0].url.params) == {"email": "alex.martin@gmail.com"}
    # "Léa Dupont" is looked up by last name, then matched to "Lea Dupont" ignoring the accent
    assert api.body("POST", f"{OBJ}/contacts/search") == search_body("lastname", "Dupont")
    # "Marc" is one word: only a company could match it
    assert api.body("POST", f"{OBJ}/companies/search") == search_body("name", "Marc", properties=["name"])
    task = api.body("POST", f"{OBJ}/tasks")
    assert task == {
        "properties": {
            "hs_task_subject": "Envoyer le devis à Léa",
            "hs_task_body": rich_text(item_markdown(action(), fr_meeting)),
            "hs_task_priority": "HIGH",
            "hs_timestamp": "2026-10-16T07:00:00Z",  # 09:00 in Amsterdam (CEST)
            "hs_task_type": "TODO",
            "hs_task_status": "NOT_STARTED",
            "hubspot_owner_id": "77",
        },
        "associations": associations(("101", 204), ("9001", 192)),
    }
    body = task["properties"]["hs_task_body"]
    assert "&lt;urgent&gt;" in body and "« premium » &amp; les délais" in body and "<urgent>" not in body
    assert "<blockquote>" in body  # the evidence quote


async def test_owner_is_the_only_user_or_nobody(app: Config, fr_meeting: Meeting, token: str, slept: list[float]) -> None:
    solo = make(app, owner_email=None)
    api = FakeHubSpot(solo, routes(**{"GET " + OWNERS: {"results": [OWNER], "paging": {}}}))
    await solo.push_item(action(), fr_meeting)
    assert dict(api.made("GET", OWNERS)[0].url.params) == {"limit": "100"}
    assert api.body("POST", f"{OBJ}/tasks")["properties"]["hubspot_owner_id"] == "77"

    team = make(app, owner_email=None)
    sam = OWNER | {"id": "78", "email": "sam@acme.fr"}
    queue = OWNER | {"id": "79", "type": "QUEUE"}
    api = FakeHubSpot(team, routes(**{"GET " + OWNERS: {"results": [OWNER, sam, queue]}}))
    await team.push_item(action(), fr_meeting)
    await team.push_item(action(id="A2"), fr_meeting)
    assert "hubspot_owner_id" not in api.body("POST", f"{OBJ}/tasks")["properties"]
    assert len(api.made("GET", OWNERS)) == 1  # resolved once

    wrong = make(app, owner_email="nobody@gmail.com")
    api = FakeHubSpot(wrong, routes(**{"GET " + OWNERS: {"results": []}}))
    await wrong.push_item(action(), fr_meeting)
    assert "hubspot_owner_id" not in api.body("POST", f"{OBJ}/tasks")["properties"]


async def test_someone_elses_item_is_unassigned_and_its_owner_matched(
    app: Config, fr_meeting: Meeting, token: str, slept: list[float]
) -> None:
    sink = make(app, owners="all")
    fr_meeting.participants = []
    api = FakeHubSpot(sink, routes())
    await sink.push_item(action(owner="Léa Dupont", detail="Copie à paul@dupont.fr"), fr_meeting)
    assert not api.made("GET", OWNERS)
    task = api.body("POST", f"{OBJ}/tasks")
    assert "hubspot_owner_id" not in task["properties"]
    assert api.body("POST", f"{OBJ}/contacts/search", -1) == search_body("email", "paul@dupont.fr")
    assert task["associations"] == associations(("101", 204), ("102", 204), ("9001", 192))


async def test_agent_and_own_items_are_not_looked_up(app: Config, fr_meeting: Meeting, token: str, slept: list[float]) -> None:
    sink = make(app, owners="all")
    fr_meeting.participants = ["alex", "ASSISTANT", " "]
    api = FakeHubSpot(sink, routes())
    await sink.push_item(action(owner="Assistant"), fr_meeting)
    await sink.push_item(action(id="A2", owner="Alex"), fr_meeting)
    assert not api.made("POST", f"{OBJ}/contacts/search") and not api.made("POST", f"{OBJ}/companies/search")


async def test_undated_item_is_due_next_workday_at_due_time(
    app: Config, fr_meeting: Meeting, token: str, slept: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = make(app, due_time="08:30", match="off")
    api = FakeHubSpot(sink, routes())
    monkeypatch.setattr(hubspot, "today", lambda tz: date(2026, 10, 16) if tz == "Europe/Amsterdam" else None)
    await sink.push_item(action(due_date=None, due_text="dès que possible", priority=None), fr_meeting)
    props = api.body("POST", f"{OBJ}/tasks")["properties"]
    assert props["hs_timestamp"] == "2026-10-19T06:30:00Z"  # Friday → Monday 08:30 CEST
    assert "hs_task_priority" not in props
    assert "dès que possible" in props["hs_task_body"]

    monkeypatch.setattr(hubspot, "today", lambda tz: date(2026, 10, 30))
    await sink.push_item(action(due_date="not a date"), fr_meeting)
    assert api.body("POST", f"{OBJ}/tasks")["properties"]["hs_timestamp"] == "2026-11-02T07:30:00Z"  # CET after DST


async def test_done_item_is_created_completed_and_cancelled_is_skipped(
    app: Config, fr_meeting: Meeting, token: str, slept: list[float]
) -> None:
    sink = make(app, match="off")
    api = FakeHubSpot(sink, routes())
    await sink.push_item(action(status=ItemStatus.DONE), fr_meeting)
    assert api.body("POST", f"{OBJ}/tasks")["properties"]["hs_task_status"] == "COMPLETED"
    assert sink.accepts(action(), 0.6) and not sink.accepts(action(status=ItemStatus.CANCELLED), 0.6)
    assert not sink.accepts(action(owner="Sam"), 0.6)  # owners = "mine" by default


# --------------------------------------------------------------- matching


async def test_accents_match_both_ways(app: Config, fr_meeting: Meeting, token: str, slept: list[float]) -> None:
    sink = make(app)
    lea = LEA | {"properties": LEA["properties"] | {"firstname": "Léa"}}
    fr_meeting.participants = ["lea dupont", "Hélène Lefèvre"]
    api = FakeHubSpot(sink, routes(contacts=[lea, PAUL, HELENE]))
    await sink.push_item(action(), fr_meeting)
    # the accented last name is searched as spoken and without accents
    assert api.body("POST", f"{OBJ}/contacts/search", 1) == search_body("lastname", "Lefèvre", "Lefevre")
    assert api.body("POST", f"{OBJ}/tasks")["associations"] == associations(
        ("101", 204), ("103", 204), ("9001", 192), ("9002", 192)
    )
    assert fold("  Léa   DUPONT ") == fold("lea dupont") == "lea dupont"


async def test_ambiguous_or_partial_names_link_nothing(app: Config, fr_meeting: Meeting, token: str, slept: list[float]) -> None:
    sink = make(app)
    twin = {"id": "104", "properties": {"firstname": "Léa", "lastname": "DUPONT", "email": "lea2@other.fr"}}
    fr_meeting.participants = ["Léa Dupont", "Léa", "Dupont Paul"]
    api = FakeHubSpot(sink, routes(contacts=[LEA, PAUL, twin], companies=[]))
    await sink.push_item(action(), fr_meeting)
    assert api.body("POST", f"{OBJ}/tasks")["associations"] == []
    # an ambiguous person is not tried as a company; one word and a non-matching name are
    company_lookups = [
        json.loads(c.content)["filterGroups"][0]["filters"][0]["value"] for c in api.made("POST", f"{OBJ}/companies/search")
    ]
    assert company_lookups == ["Léa", "Dupont Paul"]


async def test_too_many_namesakes_to_tell(app: Config, fr_meeting: Meeting, token: str, slept: list[float]) -> None:
    sink = make(app)
    api = FakeHubSpot(sink, routes(**{f"POST {OBJ}/contacts/search": {"total": 201, "results": [LEA] * 200}}))
    fr_meeting.participants = ["Léa Dupont"]
    await sink.push_item(action(), fr_meeting)
    assert api.body("POST", f"{OBJ}/tasks")["associations"] == []
    assert not api.made("POST", f"{OBJ}/companies/search")


async def test_companies_match_by_exact_name(app: Config, fr_meeting: Meeting, token: str, slept: list[float]) -> None:
    sink = make(app)
    fr_meeting.participants = ["Acme", "Énergie Verte"]
    api = FakeHubSpot(sink, routes(companies=[ACME, ACME_LABS, {"id": "9004", "properties": {"name": "Energie verte"}}]))
    await sink.push_item(action(), fr_meeting)
    assert api.body("POST", f"{OBJ}/companies/search", 1) == search_body(
        "name", "Énergie Verte", "Energie Verte", properties=["name"]
    )
    assert api.body("POST", f"{OBJ}/tasks")["associations"] == associations(("9001", 192), ("9004", 192))

    twins = make(app)
    api = FakeHubSpot(twins, routes(companies=[ACME, {"id": "9005", "properties": {"name": "acme"}}]))
    fr_meeting.participants = ["Acme"]
    await twins.push_item(action(), fr_meeting)
    assert api.body("POST", f"{OBJ}/tasks")["associations"] == []


async def test_followup_recipients_match_by_email(app: Config, fr_meeting: Meeting, token: str, slept: list[float]) -> None:
    sink = make(app)
    fr_meeting.participants = ["Léa Dupont <LEA@acme.fr>"]
    fr_meeting.followup_email = FollowUpEmail(subject="Suite", body_markdown="Merci", to=["lea@acme.fr", "inconnu@nulle-part.fr"])
    api = FakeHubSpot(sink, routes())
    await sink.push_item(action(), fr_meeting)
    lookups = [json.loads(c.content) for c in api.made("POST", f"{OBJ}/contacts/search")]
    assert lookups == [
        search_body("email", "LEA@acme.fr"),
        search_body("email", "lea@acme.fr"),
        search_body("email", "inconnu@nulle-part.fr"),
    ]
    assert api.body("POST", f"{OBJ}/tasks")["associations"] == associations(("101", 204), ("9001", 192))


async def test_lookups_are_cached_per_meeting_and_spaced_out(
    app: Config, fr_meeting: Meeting, token: str, slept: list[float]
) -> None:
    sink = make(app)
    api = FakeHubSpot(sink, routes())
    await sink.push_item(action(), fr_meeting)
    await sink.push_item(action(id="A2"), fr_meeting)
    await sink.push_notes(fr_meeting, "# Point hebdo")
    assert len(api.made("POST", f"{OBJ}/contacts/search")) == 1 and len(api.made("POST", f"{OBJ}/companies/search")) == 1
    assert slept and all(0 < s <= hubspot.SEARCH_INTERVAL_S for s in slept)

    other = fr_meeting.model_copy(update={"id": "20261016-090000"})
    await sink.push_item(action(), other)
    assert len(api.made("POST", f"{OBJ}/contacts/search")) == 2


async def test_match_off_searches_nothing(app: Config, fr_meeting: Meeting, token: str, slept: list[float]) -> None:
    sink = make(app, match="off")
    api = FakeHubSpot(sink, routes())
    await sink.push_item(action(), fr_meeting)
    assert [c.url.path for c in api.calls] == [OWNERS, f"{OBJ}/tasks"]
    assert api.body("POST", f"{OBJ}/tasks")["associations"] == []


async def test_lookup_errors_are_best_effort(app: Config, fr_meeting: Meeting, token: str, slept: list[float]) -> None:
    sink = make(app)
    denied = httpx.Response(403, json={"status": "error", "message": "missing scopes", "category": "MISSING_SCOPES"})
    api = FakeHubSpot(sink, routes(**{f"POST {OBJ}/companies/search": denied}))
    await sink.push_item(action(), fr_meeting)
    await sink.push_item(action(id="A2"), fr_meeting)
    assert api.body("POST", f"{OBJ}/tasks")["associations"] == associations(("101", 204), ("9001", 192))
    assert len(api.made("POST", f"{OBJ}/companies/search")) == 1


async def test_untrusted_text_stays_data(app: Config, fr_meeting: Meeting, token: str, slept: list[float]) -> None:
    sink = make(app)
    evil = 'Robert "}]}, {"filters": [{"propertyName": "email", "operator": "HAS_PROPERTY"}]}'
    fr_meeting.participants = [evil]
    api = FakeHubSpot(sink, routes())
    title = '<img src=x onerror="alert(1)">\nDevis'
    await sink.push_item(action(title=title, detail="[clic](javascript:void) <script>x</script>"), fr_meeting)
    groups = api.body("POST", f"{OBJ}/contacts/search")["filterGroups"]
    assert groups[0]["filters"] == [{"propertyName": "lastname", "operator": "EQ", "value": evil.split(maxsplit=1)[1]}]
    assert all(g["filters"][0]["propertyName"] == "lastname" and len(g["filters"]) == 1 for g in groups)
    props = api.body("POST", f"{OBJ}/tasks")["properties"]
    assert props["hs_task_subject"] == '<img src=x onerror="alert(1)"> Devis'  # a plain-text field, sent as JSON data
    body = props["hs_task_body"]
    assert "<script" not in body and "&lt;script&gt;" in body
    assert "<p>clic &lt;script&gt;" in body and "javascript" not in body and "href" not in body  # unsafe link: label only

    before = len(api.calls)
    for ref in ("hubspot:tasks/1/../../owners", "hubspot:tasks/١٢", "https://evil.example/tasks/1", "hubspot:meetings/1"):
        with pytest.raises(SinkError, match="not a HubSpot tasks reference"):
            await sink.update_item(action(), fr_meeting, ref)
    assert len(api.calls) == before


# ---------------------------------------------------------------- updates


async def test_update_edits_and_completes_without_moving_the_time(
    app: Config, fr_meeting: Meeting, token: str, slept: list[float]
) -> None:
    sink = make(app)
    current = {"id": "555", "properties": {"hs_task_status": "IN_PROGRESS", "hs_timestamp": "2026-10-16T12:30:00.000Z"}}
    api = FakeHubSpot(sink, routes(**{f"GET {OBJ}/tasks/555": current, f"PATCH {OBJ}/tasks/555": current}))
    edited = action(title="Envoyer le devis révisé", priority="low", status=ItemStatus.DONE, revision=2)

    await sink.update_item(edited, fr_meeting, "hubspot:tasks/555")

    assert dict(api.made("GET", f"{OBJ}/tasks/555")[0].url.params) == {"properties": "hs_task_status,hs_timestamp"}
    assert api.body("PATCH", f"{OBJ}/tasks/555") == {
        "properties": {
            "hs_task_subject": "Envoyer le devis révisé",
            "hs_task_body": rich_text(item_markdown(edited, fr_meeting)),
            "hs_task_priority": "LOW",
            "hs_task_status": "COMPLETED",
        }
    }
    await sink.update_item(
        edited.model_copy(update={"due_date": "2026-10-20", "status": ItemStatus.OPEN}), fr_meeting, "hubspot:tasks/555"
    )
    moved = api.body("PATCH", f"{OBJ}/tasks/555")["properties"]
    assert moved["hs_timestamp"] == "2026-10-20T07:00:00Z" and "hs_task_status" not in moved  # never reopened
    assert not api.made("POST", f"{OBJ}/contacts/search")  # associations are made once, at creation


async def test_cancelled_or_dismissed_archives_unless_completed(
    app: Config, fr_meeting: Meeting, token: str, slept: list[float]
) -> None:
    sink = make(app)
    open_task = {"id": "555", "properties": {"hs_task_status": "NOT_STARTED", "hs_timestamp": "2026-10-16T07:00:00Z"}}
    done_task = {"id": "556", "properties": {"hs_task_status": "COMPLETED", "hs_timestamp": "2026-10-16T07:00:00Z"}}
    api = FakeHubSpot(
        sink,
        routes(
            **{
                f"GET {OBJ}/tasks/555": open_task,
                f"GET {OBJ}/tasks/556": done_task,
                f"DELETE {OBJ}/tasks/555": httpx.Response(204),
            }
        ),
    )
    await sink.update_item(action(dismissed=True), fr_meeting, "hubspot:tasks/555")
    await sink.update_item(action(status=ItemStatus.CANCELLED), fr_meeting, "hubspot:tasks/555")
    await sink.update_item(action(status=ItemStatus.CANCELLED), fr_meeting, "hubspot:tasks/556")
    assert len(api.made("DELETE", f"{OBJ}/tasks/555")) == 2
    assert not api.made("DELETE", f"{OBJ}/tasks/556") and not [c for c in api.calls if c.method == "PATCH"]


async def test_update_of_a_task_deleted_in_hubspot_is_a_no_op(
    app: Config, fr_meeting: Meeting, token: str, slept: list[float]
) -> None:
    sink = make(app)
    gone = httpx.Response(404, json={"status": "error", "message": "Object not found.", "category": "OBJECT_NOT_FOUND"})
    api = FakeHubSpot(sink, routes(**{f"GET {OBJ}/tasks/555": gone}))
    await sink.update_item(action(revision=3), fr_meeting, "hubspot:tasks/555")
    assert [c.method for c in api.calls] == ["GET"]


async def test_dispatcher_round_trip(app: Config, fr_meeting: Meeting, token: str, slept: list[float]) -> None:
    app.sinks = {"hubspot": {"enabled": True, "owner_email": "alex.martin@gmail.com", "match": "off"}}
    [sink] = build_sinks(app)
    assert isinstance(sink, HubSpotSink)
    task = {"id": "555", "properties": {"hs_task_status": "NOT_STARTED", "hs_timestamp": "2026-10-16T07:00:00Z"}}
    api = FakeHubSpot(sink, routes(**{f"GET {OBJ}/tasks/555": task, f"PATCH {OBJ}/tasks/555": task}))  # type: ignore[arg-type]
    item = action()
    fr_meeting.items = {item.id: item}
    dispatcher = Dispatcher(app, fr_meeting, [sink])
    assert await dispatcher.sync(require_stable=False) == 1
    assert item.pushed == {"hubspot": "hubspot:tasks/555"}
    item.status, item.revision = ItemStatus.DONE, 2
    assert await dispatcher.sync(require_stable=False) == 1
    assert api.body("PATCH", f"{OBJ}/tasks/555")["properties"]["hs_task_status"] == "COMPLETED"


# ------------------------------------------------------------------ notes


async def test_push_notes_logs_a_meeting_on_matched_records(
    app: Config, fr_meeting: Meeting, token: str, slept: list[float]
) -> None:
    sink = make(app)
    api = FakeHubSpot(sink, routes(**{f"PATCH {OBJ}/meetings/888": {"id": "888"}}))
    notes = "# Point hebdo avec Acme\n\n> Devis à envoyer\n\n- [ ] **Envoyer le devis** — Alex\n\n<b>pas du HTML</b>"

    ref = await sink.push_notes(fr_meeting, notes)

    assert ref == "hubspot:meetings/888"
    assert api.body("POST", f"{OBJ}/meetings") == {
        "properties": {
            "hs_timestamp": "2026-10-15T12:00:00Z",
            "hs_meeting_title": "Point hebdo avec Acme",
            "hs_meeting_body": rich_text(notes),
            "hs_meeting_start_time": "2026-10-15T12:00:00Z",
            "hs_meeting_end_time": "2026-10-15T12:45:00Z",
            "hs_meeting_outcome": "COMPLETED",
            "hubspot_owner_id": "77",
        },
        "associations": associations(("101", 200), ("9001", 188)),
    }
    assert "&lt;b&gt;pas du HTML&lt;/b&gt;" in rich_text(notes) and "<h1>" in rich_text(notes)

    fr_meeting.notes_ref["hubspot"] = ref
    assert await sink.push_notes(fr_meeting, notes + "\n\nAjout") == ref
    assert "Ajout" in api.body("PATCH", f"{OBJ}/meetings/888")["properties"]["hs_meeting_body"]
    assert len(api.made("POST", f"{OBJ}/meetings")) == 1


async def test_push_notes_untitled_and_deleted_meeting(app: Config, fr_meeting: Meeting, token: str, slept: list[float]) -> None:
    sink = make(app, log_unmatched=True, match="off")
    gone = httpx.Response(404, json={"status": "error", "message": "Object not found.", "category": "OBJECT_NOT_FOUND"})
    api = FakeHubSpot(sink, routes(**{f"PATCH {OBJ}/meetings/888": gone}))
    untitled = fr_meeting.model_copy(update={"title": None, "ended_at": None})
    untitled.segments = []
    assert await sink.push_notes(untitled, "Notes") == "hubspot:meetings/888"
    props = api.body("POST", f"{OBJ}/meetings")["properties"]
    assert props["hs_meeting_title"] == "Réunion 2026-10-15 14:00"  # local time, meeting language
    assert props["hs_meeting_end_time"] == props["hs_meeting_start_time"]
    assert api.body("POST", f"{OBJ}/meetings")["associations"] == []

    untitled.notes_ref["hubspot"] = "hubspot:meetings/888"
    assert await sink.push_notes(untitled, "Notes") == "hubspot:meetings/888"  # deleted in HubSpot: stays deleted
    assert len(api.made("POST", f"{OBJ}/meetings")) == 1


async def test_unmatched_meetings_are_not_logged_by_default(
    app: Config, fr_meeting: Meeting, token: str, slept: list[float]
) -> None:
    sink = make(app)
    fr_meeting.participants = ["Quelqu'un d'Inconnu"]
    api = FakeHubSpot(sink, routes())
    assert await sink.push_notes(fr_meeting, "# Notes") is None
    assert not api.made("POST", f"{OBJ}/meetings") and not api.made("GET", OWNERS)


async def test_notes_can_be_logged_as_a_note(app: Config, fr_meeting: Meeting, token: str, slept: list[float]) -> None:
    sink = make(app, log_meeting=False)
    api = FakeHubSpot(sink, routes())
    assert await sink.push_notes(fr_meeting, "# Point hebdo\n\nTout va bien") == "hubspot:notes/999"
    assert api.body("POST", f"{OBJ}/notes") == {
        "properties": {
            "hs_timestamp": "2026-10-15T12:00:00Z",
            "hs_note_body": rich_text("# Point hebdo\n\nTout va bien"),
            "hubspot_owner_id": "77",
        },
        "associations": associations(("101", 202), ("9001", 190)),
    }


def test_rich_text_stays_under_the_cap() -> None:
    html = rich_text("# Titre\n\n" + "Ça & là <x> " * 20_000)
    assert len(html) <= BODY_MAX and html.endswith("…</p>")
    assert rich_text("**gras**") == "<p><strong>gras</strong></p>"


# ----------------------------------------------------- errors and limits


async def test_rate_limits_are_retried(app: Config, fr_meeting: Meeting, token: str, slept: list[float]) -> None:
    sink = make(app)
    limited = httpx.Response(
        429,
        headers={"Retry-After": "2"},
        json={
            "status": "error",
            "message": "You have reached your ten_secondly_rolling limit.",
            "errorType": "RATE_LIMIT",
            "policyName": "TEN_SECONDLY_ROLLING",
        },
    )
    created = httpx.Response(201, json={"id": "555"})
    contacts = crm_search([LEA])
    search_replies = [limited, None]

    def contacts_search(request: httpx.Request) -> Reply:
        reply = search_replies.pop(0)
        return reply if reply is not None else contacts(request)

    api = FakeHubSpot(sink, routes(**{f"POST {OBJ}/tasks": [limited, created], f"POST {OBJ}/contacts/search": contacts_search}))
    assert await sink.push_item(action(), fr_meeting) == "hubspot:tasks/555"
    assert len(api.made("POST", f"{OBJ}/tasks")) == 2 and len(api.made("POST", f"{OBJ}/contacts/search")) == 2
    assert slept.count(2.0) == 2
    assert api.body("POST", f"{OBJ}/tasks")["associations"] == associations(("101", 204), ("9001", 192))


@pytest.mark.parametrize("failure", [httpx.Response(502, text="Bad gateway"), httpx.ReadTimeout("no answer")])
async def test_a_create_that_may_have_happened_is_never_resent(
    app: Config, fr_meeting: Meeting, token: str, slept: list[float], failure: httpx.Response | Exception
) -> None:
    # HubSpot applied the first create but the answer was lost; resending would make a second task
    app.sinks = {"hubspot": {"enabled": True, "owner_email": "alex.martin@gmail.com", "match": "off"}}
    [sink] = build_sinks(app)
    assert isinstance(sink, HubSpotSink)
    attempts: list[httpx.Request] = []

    def create(request: httpx.Request) -> Reply:
        attempts.append(request)
        if len(attempts) == 1:
            if isinstance(failure, Exception):
                raise failure
            return failure
        return httpx.Response(201, json={"id": str(len(attempts))})

    FakeHubSpot(sink, routes(**{f"POST {OBJ}/tasks": create}))
    item = action()
    fr_meeting.items = {item.id: item}
    dispatcher = Dispatcher(app, fr_meeting, [sink])
    for _ in range(3):
        assert await dispatcher.sync(require_stable=False) == 0
    assert len(attempts) == 1 and item.pushed == {}
    assert await dispatcher.push_now(item.id) == {"hubspot": "hubspot:tasks/2"}  # you checked, and push it by hand

    attempts.clear()  # the next create fails the same way: the error itself says "don't retry"
    with pytest.raises(SinkError) as err:
        await sink.push_item(action(), fr_meeting)
    assert not err.value.retryable and len(attempts) == 1


async def test_a_create_that_never_reached_hubspot_is_retried(app: Config, fr_meeting: Meeting, token: str, slept: list[float]) -> None:
    def refused(request: httpx.Request) -> Reply:
        raise httpx.ConnectError("connection refused")

    sink = make(app, match="off")
    api = FakeHubSpot(sink, routes(**{f"POST {OBJ}/tasks": refused}))
    with pytest.raises(SinkError) as err:
        await sink.push_item(action(), fr_meeting)
    assert err.value.retryable
    assert len(api.made("POST", f"{OBJ}/tasks")) == 4  # the mixin's own retries


async def test_errors_are_explained(app: Config, fr_meeting: Meeting, token: str, slept: list[float]) -> None:
    unauthorized = httpx.Response(
        401,
        json={
            "status": "error",
            "message": "Authentication credentials not found.",
            "category": "INVALID_AUTHENTICATION",
            "correlationId": "c-1",
        },
    )
    sink = make(app)
    FakeHubSpot(sink, routes(**{"GET " + OWNERS: unauthorized}))
    with pytest.raises(SinkError) as err:
        await sink.push_item(action(), fr_meeting)
    assert not err.value.retryable
    assert "HTTP 401 INVALID_AUTHENTICATION" in str(err.value) and "HUBSPOT_TOKEN is wrong, revoked or rotated" in str(err.value)

    missing = httpx.Response(
        403,
        json={
            "status": "error",
            "message": "This app hasn't been granted all required scopes to make this call.",
            "correlationId": "c-2",
            "errors": [
                {
                    "message": "One or more of the following scopes are required.",
                    "context": {"requiredGranularScopes": ["crm.objects.contacts.write"]},
                }
            ],
            "links": {"scopes": "https://developers.hubspot.com/scopes"},
            "category": "MISSING_SCOPES",
        },
    )
    sink = make(app, match="off")
    FakeHubSpot(sink, routes(**{f"POST {OBJ}/tasks": missing}))
    with pytest.raises(SinkError, match=r"MISSING_SCOPES .*give the key the scope\(s\) crm\.objects\.contacts\.write") as err:
        await sink.push_item(action(), fr_meeting)
    assert not err.value.retryable

    invalid = httpx.Response(
        400,
        json={"status": "error", "message": "Property values were not valid: hs_task_priority", "category": "VALIDATION_ERROR"},
    )
    sink = make(app, match="off")
    FakeHubSpot(sink, routes(**{f"POST {OBJ}/tasks": invalid}))
    with pytest.raises(SinkError, match="VALIDATION_ERROR .*hs_task_priority"):
        await sink.push_item(action(), fr_meeting)

    sink = make(app, match="off")
    FakeHubSpot(sink, routes(**{f"POST {OBJ}/tasks": httpx.Response(201, json={})}))
    with pytest.raises(SinkError, match="got no id back") as err:
        await sink.push_item(action(), fr_meeting)
    assert not err.value.retryable


async def test_missing_token_fails_at_call_time(app: Config, fr_meeting: Meeting, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HUBSPOT_TOKEN", raising=False)
    app.sinks = {"hubspot": {"enabled": True}}
    [sink] = build_sinks(app)
    with pytest.raises(SinkError, match="HUBSPOT_TOKEN is not set"):
        await sink.check()
    with pytest.raises(SinkError, match="HUBSPOT_TOKEN is not set"):
        await sink.push_item(action(), fr_meeting)


# ------------------------------------------------------------------ doctor


async def test_check_reports_portal_owner_and_scopes(app: Config, token: str) -> None:
    account = {
        "portalId": 146_000_123,
        "uiDomain": "app-eu1.hubspot.com",
        "dataHostingLocation": "eu1",
        "timeZone": "Europe/Amsterdam",
    }
    sink = make(app)
    api = FakeHubSpot(
        sink,
        routes(
            **{
                "GET /account-info/2026-09/details": account,
                f"GET {OBJ}/contacts": {"results": []},
                f"GET {OBJ}/companies": {"results": []},
            }
        ),
    )
    line = await sink.check()
    assert line == (
        "portal 146000123 on app-eu1.hubspot.com · tasks assigned to Alex.Martin@gmail.com (owner 77) · "
        "meetings logged as Meetings on matched contacts"
    )
    assert [c.url.path for c in api.calls] == ["/account-info/2026-09/details", OWNERS, f"{OBJ}/contacts", f"{OBJ}/companies"]
    assert dict(api.made("GET", f"{OBJ}/contacts")[0].url.params) == {"limit": "1"}
    await sink.check()
    assert len(api.made("GET", OWNERS)) == 2  # always live

    nobody = make(app, owner_email="typo@gmail.con", match="off")
    FakeHubSpot(
        nobody,
        routes(
            **{
                "GET /account-info/2026-09/details": account,
                f"GET {OBJ}/contacts": {"results": []},
                "GET " + OWNERS: {"results": []},
            }
        ),
    )
    assert await nobody.check() == (
        "portal 146000123 on app-eu1.hubspot.com · tasks unassigned: no HubSpot user has the e-mail typo@gmail.con · "
        "no contact matching, so meetings aren't logged"
    )

    no_scope = make(app)
    denied = httpx.Response(
        403,
        json={
            "status": "error",
            "message": "missing scopes",
            "category": "MISSING_SCOPES",
            "errors": [{"context": {"requiredScopes": ["crm.objects.companies.read"]}}],
        },
    )
    FakeHubSpot(
        no_scope,
        routes(
            **{
                "GET /account-info/2026-09/details": account,
                f"GET {OBJ}/contacts": {"results": []},
                f"GET {OBJ}/companies": denied,
            }
        ),
    )
    with pytest.raises(SinkError, match=r"scope\(s\) crm\.objects\.companies\.read"):
        await no_scope.check()

    # account details are informational: a key without the basic "oauth" scope still passes
    no_oauth = make(app)
    FakeHubSpot(
        no_oauth,
        routes(
            **{
                "GET /account-info/2026-09/details": httpx.Response(403, json={"category": "MISSING_SCOPES", "message": "oauth"}),
                f"GET {OBJ}/contacts": {"results": []},
                f"GET {OBJ}/companies": {"results": []},
            }
        ),
    )
    assert (await no_oauth.check()).startswith("tasks assigned to Alex.Martin@gmail.com (owner 77)")
