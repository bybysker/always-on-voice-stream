"""Twenty CRM sink against a fake Twenty (httpx.MockTransport) that keeps its records: a client meeting
files its company, people and deal (found, or created once), moves the deal forward only, fills the
mapped fields, and carries the notes and your tasks; other meetings never reach it."""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from voicestream.config import Config
from voicestream.dispatch import Dispatcher, pending_review
from voicestream.models import Classification, ClientContact, Item, ItemKind, ItemStatus, Meeting
from voicestream.sinks.base import SinkError, build_sinks
from voicestream.sinks.twenty import TwentyConfig, TwentySink, company_key, note_body, record_id, Filed

KEY = "twenty-test-key"
BASE = "https://crm.example.com"
ME = {"id": str(uuid.uuid4()), "userEmail": "alex@example.com", "name": {"firstName": "Alex", "lastName": "Martin"}}
SINGULAR = {"companies": "company", "people": "person", "opportunities": "opportunity", "notes": "note",
            "tasks": "task", "noteTargets": "noteTarget", "taskTargets": "taskTarget"}  # fmt: skip


def stage_options(*values: tuple[str, str]) -> list[dict[str, Any]]:
    return [{"value": v, "label": label, "position": i} for i, (v, label) in enumerate(values)]


FRENCH_STAGES = stage_options(("NEW", "Nouveau"), ("SCREENING", "Rappel"), ("MEETING", "Rendez-vous"),
                              ("PROPOSAL", "Proposition"), ("CUSTOMER", "Client"), ("LOST", "Perdu"))
OPPORTUNITY_FIELDS = [  # crm.socreagency.com's deal fields, as its data model listed them on 2026-11-20
    {"name": "name", "type": "TEXT", "label": "Name"},
    {"name": "stage", "type": "SELECT", "label": "Stage", "options": FRENCH_STAGES},
    {"name": "formule", "type": "SELECT", "label": "Formule",
     "options": stage_options(("VISIBILITE", "Visibilité"), ("FORMATION", "Formation"), ("IDENTITE", "Identité"))},
    {"name": "versionProposee", "type": "SELECT", "label": "Version proposée",
     "options": stage_options(("COMPLETE", "Complète"), ("ALLEGEE", "Allégée"), ("MINIMUM", "Le minimum"))},
    {"name": "doutePrincipal", "type": "SELECT", "label": "Doute principal",
     "options": stage_options(("PAS_LE_TEMPS", "Pas le temps"), ("QUOI_PUBLIER", "Quoi publier ensuite"),
                              ("EQUIPES", "Équipes ne regarderont pas"), ("AUCUNE_PISTE", "Aucune piste ne plaira"),
                              ("TROP_CHER", "Trop cher"), ("AUTRE", "Autre"))},
    {"name": "raisonPerte", "type": "SELECT", "label": "Raison de la perte",
     "options": stage_options(("PRIX", "Prix"), ("PAS_LE_MOMENT", "Pas le bon moment"), ("SANS_REPONSE", "Sans réponse"),
                              ("AUTRE_PRESTATAIRE", "Autre prestataire"), ("PAS_DE_BESOIN", "Pas de besoin"),
                              ("AUTRE", "Autre"))},
    {"name": "notes", "type": "TEXT", "label": "Notes"},
    {"name": "amount", "type": "CURRENCY", "label": "Amount"},
]
MAPPING = {"formule": "Formule", "versionProposee": "Version proposée", "doutePrincipal": "Doute principal",
           "raisonPerte": "Raison de la perte"}


def _ilike(pattern: str, text: str) -> bool:
    """Postgres ILIKE: ``%`` any run, ``_`` one character, case folded, accents kept."""
    rx = "".join(".*" if ch == "%" else "." if ch == "_" else re.escape(ch) for ch in pattern)
    return re.fullmatch(rx, text, flags=re.IGNORECASE | re.DOTALL) is not None


def _get(record: dict[str, Any], path: list[str]) -> Any:
    for p in path:
        record = (record or {}).get(p) if isinstance(record, dict) else None
    return record


def _matches(record: dict[str, Any], expr: str) -> bool:
    expr = expr.strip()
    if m := re.fullmatch(r"(or|and)\((.*)\)", expr, flags=re.DOTALL):
        parts = re.split(r',(?=(?:[^"]*"[^"]*")*[^"]*$)', m.group(2))
        results = [_matches(record, p) for p in parts]
        return any(results) if m.group(1) == "or" else all(results)
    m = re.fullmatch(r"([\w.]+)\[(\w+)\]:(.*)", expr, flags=re.DOTALL)
    assert m, f"filter the fake doesn't know: {expr}"
    path, op, value = m.group(1).split("."), m.group(2), m.group(3)
    value = value[1:-1] if value[:1] == value[-1:] == '"' else value
    actual = _get(record, path)
    if op == "eq":
        return str(actual) == value
    if op == "ilike":
        return actual is not None and _ilike(value, str(actual))
    raise AssertionError(f"operator the fake doesn't know: {op}")


class FakeTwenty:
    """Twenty's REST API over a dict of records (only what the sink uses)."""

    def __init__(self, sink: TwentySink, *, legacy_metadata: bool = False, metadata_status: int = 200,
                 members: list[dict[str, Any]] | None = None) -> None:
        self.db: dict[str, dict[str, dict[str, Any]]] = {k: {} for k in SINGULAR}
        self.calls: list[httpx.Request] = []
        self.legacy_metadata = legacy_metadata
        self.metadata_status = metadata_status
        self.members = [ME] if members is None else members
        self.clock = 0
        sink._client = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def add(self, plural: str, record: dict[str, Any]) -> dict[str, Any]:
        self.clock += 1
        rec = {"id": str(uuid.uuid4()), "updatedAt": f"2026-11-01T00:00:{self.clock:02d}Z", **record}
        self.db[plural][rec["id"]] = rec
        return rec

    def writes(self, method: str | None = None) -> list[tuple[str, str, Any]]:
        return [(r.method, urlsplit(str(r.url)).path, json.loads(r.content) if r.content else None)
                for r in self.calls if r.method != "GET" and (method is None or r.method == method)]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        url = urlsplit(str(request.url))
        assert f"{url.scheme}://{url.netloc}" == BASE
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        parts = url.path.removeprefix("/rest/").split("/")
        if parts[:2] == ["metadata", "objects"]:
            if self.metadata_status != 200:
                return httpx.Response(self.metadata_status, json={"statusCode": self.metadata_status, "messages": ["Forbidden"]})
            objects = [{"nameSingular": "company", "fields": []},
                       {"nameSingular": "opportunity", "fields": OPPORTUNITY_FIELDS}]
            data: Any = {"objects": objects} if self.legacy_metadata else objects
            return httpx.Response(200, json={"data": data, "pageInfo": {"hasNextPage": False}, "totalCount": 2})
        if parts == ["workspaceMembers"]:
            return httpx.Response(200, json={"data": {"workspaceMembers": self.members}})
        plural = parts[0]
        table, singular = self.db[plural], SINGULAR[plural]
        if len(parts) == 1 and request.method == "GET":
            rows = [r for r in table.values() if "filter" not in query or _matches(r, query["filter"])]
            if query.get("order_by") == "updatedAt[DescNullsLast]":
                rows.sort(key=lambda r: r["updatedAt"], reverse=True)
            limit = int(query.get("limit", 60))
            return httpx.Response(200, json={"data": {plural: rows[:limit]}, "totalCount": len(rows)})
        if len(parts) == 1 and request.method == "POST":
            rec = self.add(plural, json.loads(request.content))
            return httpx.Response(201, json={"data": {f"create{singular[0].upper()}{singular[1:]}": rec}})
        rec = table.get(parts[1])
        if rec is None:
            return httpx.Response(404, json={"statusCode": 404, "messages": ["Record not found"], "error": "NotFound"})
        if request.method == "GET":
            return httpx.Response(200, json={"data": {singular: rec}})
        if request.method == "PATCH":
            self.clock += 1
            rec.update(json.loads(request.content), updatedAt=f"2026-11-01T00:01:{self.clock:02d}Z")
            return httpx.Response(200, json={"data": {f"update{singular[0].upper()}{singular[1:]}": rec}})
        if request.method == "DELETE":
            # Twenty destroys a record for good unless asked for its trash: the sink must always ask
            assert query.get("soft_delete") == "true", "a DELETE without soft_delete=true destroys the record"
            del table[parts[1]]
            return httpx.Response(200, json={"data": {f"delete{singular[0].upper()}{singular[1:]}": rec}})
        raise AssertionError(f"unexpected {request.method} {url.path}")


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TWENTY_API_KEY", KEY)


def make_sink(**overrides: Any) -> TwentySink:
    app = Config(user_name="Alex Martin", timezone="Europe/Paris")
    settings: dict[str, Any] = {
        "enabled": True, "base_url": BASE, "owner_email": "alex@example.com",
        "fields": MAPPING,
    }
    cfg = TwentyConfig(**(settings | overrides))
    return TwentySink(cfg, app)


FIELDS = {"Formule": "Identité", "Version proposée": "", "Doute principal": "Aucune piste ne plaira",
          "Raison de la perte": "", "Budget": "5 000 €", "Prochaine étape": "Proposition le 6 novembre"}


def gaia_meeting(fields: dict[str, str] | None = None, **classification: Any) -> Meeting:
    """A discovery meeting with Julie of Gaïa: the proposal to send (A1), a follow-up (A2), the AI fields answered."""
    c = {"kind": "prospect", "confidence": 0.9, "company": "Gaïa", "step": "discovery", "proposal_item": "A1",
         "followup_items": ["A2"],
         "contacts": [ClientContact(name="Julie Martin", email="Julie@Gaia.fr", role="directrice")]} | classification
    m = Meeting(id="20261103-140000-000000", title="Rendez-vous découverte GAIA", language="fr",
                started_at=datetime(2026, 11, 3, 13, 0, tzinfo=UTC), participants=["Julie Martin"],
                classification=Classification(**c), fields=FIELDS if fields is None else fields)
    m.items["A1"] = Item(id="A1", kind=ItemKind.ACTION, title="Envoyer la proposition à Julie", owner="Alex Martin",
                         due_date="2026-11-06", confidence=0.9)
    m.items["A2"] = Item(id="A2", kind=ItemKind.ACTION, title="Relancer Julie si pas de réponse", owner="Alex Martin",
                         due_date="2026-11-13", confidence=0.9)
    return m


NOTES = "# Rendez-vous découverte GAIA\n\n*lundi 3 novembre 2026, 14:00* · 30:00 enregistrées\n\n## Résumé\n\n- Besoin : une identité."


async def test_a_new_prospect_files_company_person_deal_note_and_task() -> None:
    sink = make_sink()
    fake = FakeTwenty(sink)
    m = gaia_meeting()
    ref = await sink.push_item(m.items["A1"], m)
    note = await sink.push_notes(m, NOTES)

    [company] = fake.db["companies"].values()
    [person] = fake.db["people"].values()
    [deal] = fake.db["opportunities"].values()
    assert company["name"] == "Gaïa"
    assert person["name"] == {"firstName": "Julie", "lastName": "Martin"}
    assert person["emails"] == {"primaryEmail": "julie@gaia.fr"} and person["jobTitle"] == "directrice"
    assert person["companyId"] == company["id"]
    assert "phones" not in person  # misheard numbers never go on a record
    assert deal["name"] == "Gaïa" and deal["stage"] == "MEETING"
    assert deal["companyId"] == company["id"] and deal["pointOfContactId"] == person["id"] and deal["ownerId"] == ME["id"]
    assert deal["formule"] == "IDENTITE" and deal["doutePrincipal"] == "AUCUNE_PISTE"
    assert "versionProposee" not in deal and "raisonPerte" not in deal  # what the meeting didn't answer

    [task] = fake.db["tasks"].values()
    assert ref == f"twenty:tasks/{task['id']}"
    assert task["title"] == "Envoyer la proposition à Julie" and task["status"] == "TODO"
    assert task["assigneeId"] == ME["id"]
    assert task["dueAt"] == "2026-11-06T08:00:00Z"  # 09:00 in Paris
    assert {k for t in fake.db["taskTargets"].values() for k in t if k.startswith("target")} == {
        "targetOpportunityId", "targetCompanyId"}

    [n] = fake.db["notes"].values()
    assert note == f"twenty:notes/{n['id']}"
    assert n["title"] == "Rendez-vous découverte GAIA"
    body = n["bodyV2"]["markdown"]
    assert body.startswith("**Contacts :** Julie Martin (directrice, Julie@Gaia.fr)\n\n")
    assert "# Rendez-vous" not in body and "## Résumé" in body
    assert body.endswith("_Classée par Voicestream sur Gaïa · Julie Martin · opportunité « Gaïa » (Rendez-vous)._")
    targets = list(fake.db["noteTargets"].values())
    assert {(k, v) for t in targets for k, v in t.items() if k.startswith("target")} == {
        ("targetOpportunityId", deal["id"]), ("targetCompanyId", company["id"]), ("targetPersonId", person["id"])}


async def test_existing_records_are_found_despite_accents_case_and_legal_form() -> None:
    sink = make_sink()
    fake = FakeTwenty(sink)
    company = fake.add("companies", {"name": "GAÏA SAS"})
    julie = fake.add("people", {"name": {"firstName": "Julie", "lastName": "Martin"}, "companyId": None,
                                "emails": {"primaryEmail": None}})
    fake.add("opportunities", {"name": "old lost", "companyId": company["id"], "stage": "LOST"})
    deal = fake.add("opportunities", {"name": "GAIA", "companyId": company["id"], "stage": "SCREENING"})
    m = gaia_meeting(company="Gaia", contacts=[ClientContact(name="julie martin")])
    await sink.push_notes(m, NOTES)

    assert len(fake.db["companies"]) == 1 and len(fake.db["people"]) == 1 and len(fake.db["opportunities"]) == 2
    assert julie["companyId"] == company["id"]  # found without a company: gets this one
    assert deal["stage"] == "MEETING" and deal["formule"] == "IDENTITE"
    assert ("PATCH", f"/rest/people/{julie['id']}", {"companyId": company["id"]}) in fake.writes("PATCH")


async def test_the_deal_never_moves_backward_and_closed_deals_stay_closed() -> None:
    sink = make_sink()
    fake = FakeTwenty(sink)
    company = fake.add("companies", {"name": "Gaïa"})
    deal = fake.add("opportunities", {"name": "Gaïa", "companyId": company["id"], "stage": "PROPOSAL"})
    await sink.push_notes(gaia_meeting(step="first_call", fields={}), NOTES)
    assert deal["stage"] == "PROPOSAL"
    assert not [w for w in fake.writes("PATCH") if w[1].startswith("/rest/opportunities")]

    sink = make_sink()
    fake = FakeTwenty(sink)
    company = fake.add("companies", {"name": "Gaïa"})
    won = fake.add("opportunities", {"name": "Gaïa", "companyId": company["id"], "stage": "CUSTOMER"})
    await sink.push_notes(gaia_meeting(kind="client", step="lost", fields={}), NOTES)
    assert won["stage"] == "CUSTOMER"  # a client meeting files on the won deal and never reopens or loses it
    assert len(fake.db["opportunities"]) == 1 and len(fake.db["notes"]) == 1


async def test_lost_moves_an_open_deal_to_the_lost_stage_with_its_reason() -> None:
    sink = make_sink()
    fake = FakeTwenty(sink)
    company = fake.add("companies", {"name": "Gaïa"})
    deal = fake.add("opportunities", {"name": "Gaïa", "companyId": company["id"], "stage": "PROPOSAL"})
    await sink.push_notes(gaia_meeting(step="lost", fields={"Raison de la perte": "Prix (trop cher pour cette année)"}), NOTES)
    assert deal["stage"] == "LOST" and deal["raisonPerte"] == "PRIX"  # the option the answer names


async def test_a_won_prospect_with_a_won_deal_gets_a_new_deal() -> None:
    sink = make_sink()
    fake = FakeTwenty(sink)
    company = fake.add("companies", {"name": "Gaïa"})
    old = fake.add("opportunities", {"name": "Gaïa · Visibilité", "companyId": company["id"], "stage": "CUSTOMER"})
    await sink.push_notes(gaia_meeting(step="proposal"), NOTES)
    new = [d for d in fake.db["opportunities"].values() if d["id"] != old["id"]]
    assert len(new) == 1 and new[0]["stage"] == "PROPOSAL" and new[0]["name"] == "Gaïa" and old["stage"] == "CUSTOMER"


async def test_no_records_are_created_when_create_records_is_off_or_the_name_is_ambiguous() -> None:
    sink = make_sink(create_records=False)
    fake = FakeTwenty(sink)
    assert await sink.push_notes(gaia_meeting(), NOTES) is None  # nothing to file it on
    assert fake.writes() == []

    sink = make_sink()
    fake = FakeTwenty(sink)
    fake.add("companies", {"name": "Gaïa"})
    fake.add("companies", {"name": "GAIA SARL"})
    m = gaia_meeting(contacts=[])
    assert await sink.push_notes(m, NOTES) is None
    assert len(fake.db["companies"]) == 2 and fake.db["opportunities"] == {} and fake.db["notes"] == {}


async def test_a_first_name_alone_matches_only_among_the_company_people() -> None:
    sink = make_sink()
    fake = FakeTwenty(sink)
    company = fake.add("companies", {"name": "Gaïa"})
    fake.add("people", {"name": {"firstName": "Julie", "lastName": "Durand"}, "companyId": None})
    mine = fake.add("people", {"name": {"firstName": "Julie", "lastName": "Martin"}, "companyId": company["id"]})
    m = gaia_meeting(contacts=[ClientContact(name="Julie")])
    await sink.push_notes(m, NOTES)
    assert len(fake.db["people"]) == 2
    assert ("targetPersonId", mine["id"]) in {(k, v) for t in fake.db["noteTargets"].values() for k, v in t.items()}


async def test_pushing_again_rewrites_the_note_and_creates_nothing_twice() -> None:
    sink = make_sink()
    fake = FakeTwenty(sink)
    m = gaia_meeting()
    m.notes_ref["twenty"] = await sink.push_notes(m, NOTES)  # type: ignore[assignment]
    again = make_sink()  # a new process: nothing cached
    again._client = sink._client
    assert await again.push_notes(m, NOTES + "\n- ajouté") == m.notes_ref["twenty"]
    assert len(fake.db["companies"]) == len(fake.db["people"]) == len(fake.db["opportunities"]) == len(fake.db["notes"]) == 1
    [n] = fake.db["notes"].values()
    assert n["bodyV2"]["markdown"].count("- ajouté") == 1

    del fake.db["notes"][n["id"]]  # deleted in Twenty: stays deleted
    assert await again.push_notes(m, NOTES) == m.notes_ref["twenty"]
    assert fake.db["notes"] == {}


async def test_task_updates_complete_move_and_delete() -> None:
    sink = make_sink()
    fake = FakeTwenty(sink)
    m = gaia_meeting()
    item = m.items["A1"]
    ref = await sink.push_item(item, m)
    task = fake.db["tasks"][record_id(ref, "tasks")]

    item.title, item.due_date, item.status = "Envoyer la proposition révisée", "2026-11-10", ItemStatus.DONE
    await sink.update_item(item, m, ref)
    assert task["title"] == "Envoyer la proposition révisée" and task["status"] == "DONE"
    assert task["dueAt"] == "2026-11-10T08:00:00Z"

    item.dismissed = True
    await sink.update_item(item, m, ref)
    assert task["id"] in fake.db["tasks"]  # done work stays

    task["status"] = "TODO"
    await sink.update_item(item, m, ref)
    assert fake.db["tasks"] == {}
    await sink.update_item(item, m, ref)  # gone: left deleted, no error


async def test_only_sure_client_and_prospect_meetings_reach_the_crm() -> None:
    cfg = Config(user_name="Alex Martin", sinks={"twenty": {"enabled": True, "base_url": BASE}})
    [sink] = build_sinks(cfg, only=["twenty"])
    assert isinstance(sink, TwentySink)
    m = gaia_meeting()
    assert sink.accepts_meeting(m)
    m.classification = m.classification.model_copy(update={"confidence": 0.5})  # type: ignore[union-attr]
    assert not sink.accepts_meeting(m) and "unsure" in sink.skip_reason(m)
    m.classification = m.classification.model_copy(update={"by_you": True})  # type: ignore[union-attr]
    assert sink.accepts_meeting(m)
    m.classification = Classification(kind="internal", confidence=1.0)
    assert not sink.accepts_meeting(m) and "internal" in sink.skip_reason(m)
    m.classification = None
    assert not sink.accepts_meeting(m)


async def test_the_dispatcher_holds_items_and_notes_of_other_meetings() -> None:
    cfg = Config(user_name="Alex Martin", sinks={"twenty": {"enabled": True, "base_url": BASE, "review": True}})
    [sink] = build_sinks(cfg, only=["twenty"])
    fake = FakeTwenty(sink)  # type: ignore[arg-type]
    m = gaia_meeting(kind="internal", confidence=0.95, company="", contacts=[], step="none")
    d = Dispatcher(cfg, m, [sink])
    assert pending_review(m, [sink], 0.6) == []  # not for the CRM: nothing waits for review either
    await d.finalize(NOTES)
    assert fake.writes() == []
    assert (await d.push_now("A1"))["twenty"].startswith("skipped (not a client meeting")

    m.classification = gaia_meeting().classification
    assert pending_review(m, [sink], 0.6) == ["A1", "A2"]
    await d.review(["A1"])
    assert len(fake.db["tasks"]) == 1
    await d.push_notes(NOTES)
    assert m.notes_ref["twenty"].startswith("twenty:notes/")


async def test_metadata_formats_and_a_key_without_the_data_model_permission() -> None:
    sink = make_sink()
    FakeTwenty(sink, legacy_metadata=True)
    assert "formule" in (await sink._opportunity_fields() or {})

    sink = make_sink()
    fake = FakeTwenty(sink, metadata_status=403)
    company = fake.add("companies", {"name": "Gaïa"})
    deal = fake.add("opportunities", {"name": "Gaïa", "companyId": company["id"], "stage": "NEW"})
    await sink.push_notes(gaia_meeting(), NOTES)
    assert deal["stage"] == "MEETING"  # the standard stages, assumed
    assert "formule" not in deal  # no data model: `fields` skipped


async def test_check_says_what_it_will_do() -> None:
    sink = make_sink()
    sink.app.brain.fields = list(MAPPING.values())  # the AI fields the mapping reads, asked of every meeting
    FakeTwenty(sink)
    status = await sink.check()
    assert "Twenty at crm.example.com" in status
    assert "your tasks assigned to alex@example.com" in status
    assert "discovery → Rendez-vous" in status and "lost → Perdu" in status  # LOST found by itself
    assert "fields: 4 found" in status and "asks for" not in status

    sink = make_sink(fields={"versionProposee": "Version proposée", "budgetEstime": "Budget"})
    FakeTwenty(sink, members=[ME, {**ME, "id": str(uuid.uuid4()), "userEmail": "sam@example.com"}])
    sink.cfg.owner_email = None
    status = await sink.check()
    assert "tasks unassigned" in status and "missing: budgetEstime" in status
    assert "no template or setting asks for the AI field(s) Version proposée" in status  # Budget: the sales template's


async def test_errors_name_the_fix() -> None:
    sink = make_sink()
    sink._client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(401, json={"statusCode": 401, "messages": ["Token invalid."], "error": "UNAUTHENTICATED"})))
    with pytest.raises(SinkError, match="TWENTY_API_KEY is wrong or revoked"):
        await sink.check()
    no_url = TwentySink(TwentyConfig(enabled=True), Config())
    with pytest.raises(SinkError, match="set base_url"):
        await no_url.check()


def test_config_checks_the_address() -> None:
    assert TwentyConfig(base_url="https://crm.example.com/rest/").base_url == "https://crm.example.com"
    assert TwentyConfig(base_url="http://localhost:3000").base_url == "http://localhost:3000"
    for bad in ("crm.example.com", "http://crm.example.com", "ftp://x"):
        with pytest.raises(ValueError):
            TwentyConfig(base_url=bad)


def test_company_key_ignores_case_accents_punctuation_and_legal_forms() -> None:
    assert company_key("GAÏA SAS") == company_key("Gaïa") == company_key("gaia, s.a.s.") == "gaia"
    assert company_key("Boulangerie Dupont & Fils") == "boulangerie dupont fils"


def test_note_body_in_english_without_facts() -> None:
    m = Meeting(language="en", classification=Classification(kind="client", confidence=1, company="Acme"))
    body = note_body(m, "# Title\n\nSome notes.", Filed(company={"id": str(uuid.uuid4()), "name": "Acme"}))
    assert body == "Some notes.\n\n_Filed by Voicestream on Acme._"


async def test_ticking_the_proposal_to_do_moves_the_deal_to_proposition() -> None:
    sink = make_sink()
    fake = FakeTwenty(sink)
    m = gaia_meeting()
    proposal = m.items["A1"]
    ref = await sink.push_item(proposal, m)
    await sink.push_item(m.items["A2"], m)
    [deal] = fake.db["opportunities"].values()
    assert deal["stage"] == "MEETING"

    m.items["A2"].status = ItemStatus.DONE  # another to-do done: nothing moves
    await sink.update_item(m.items["A2"], m, ref.replace(record_id(ref, "tasks"), next(
        t["id"] for t in fake.db["tasks"].values() if t["title"].startswith("Relancer"))))
    assert deal["stage"] == "MEETING"

    proposal.status = ItemStatus.DONE
    again = make_sink()  # a later process: the meeting is filed again from the records, nothing created
    again._client = sink._client
    await again.update_item(proposal, m, ref)
    assert deal["stage"] == "PROPOSAL" and len(fake.db["opportunities"]) == 1
    assert fake.db["tasks"][record_id(ref, "tasks")]["status"] == "DONE"

    deal["stage"] = "CUSTOMER"  # already won: ticking it again never moves it back
    await again.update_item(proposal, m, ref)
    assert deal["stage"] == "CUSTOMER"


async def test_a_proposal_to_do_sent_already_done_moves_the_deal_unless_stages_stay() -> None:
    sink = make_sink()
    fake = FakeTwenty(sink)
    m = gaia_meeting()
    m.items["A1"].status = ItemStatus.DONE
    await sink.push_item(m.items["A1"], m)
    assert next(iter(fake.db["opportunities"].values()))["stage"] == "PROPOSAL"

    sink = make_sink(move_stage=False)
    fake = FakeTwenty(sink)
    await sink.push_item(m.items["A1"], m)
    assert next(iter(fake.db["opportunities"].values()))["stage"] == "NEW"


async def test_proposal_follow_ups_stay_out_when_the_crm_sends_its_own() -> None:
    cfg = Config(user_name="Alex Martin", sinks={"twenty": {"enabled": True, "base_url": BASE, "skip_proposal_followups": True}})
    [sink] = build_sinks(cfg, only=["twenty"])
    fake = FakeTwenty(sink)  # type: ignore[arg-type]
    m = gaia_meeting()
    await Dispatcher(cfg, m, [sink]).finalize(None)
    assert [t["title"] for t in fake.db["tasks"].values()] == ["Envoyer la proposition à Julie"]
    assert "twenty" not in m.items["A2"].pushed

    cfg.sinks["twenty"]["skip_proposal_followups"] = False  # the default: every to-do of yours goes
    [sink] = build_sinks(cfg, only=["twenty"])
    fake = FakeTwenty(sink)  # type: ignore[arg-type]
    m = gaia_meeting()
    await Dispatcher(cfg, m, [sink]).finalize(None)
    assert len(fake.db["tasks"]) == 2
