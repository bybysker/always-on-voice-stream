"""Twenty CRM sink: your client and prospect meetings file themselves in Twenty (https://twenty.com,
cloud or self-hosted). Other meetings never reach it.

At the wrap-up Claude classifies each meeting (``Meeting.classification``: client, prospect, partner,
internal or other, with the company, the people and where the deal stands). This sink takes only the
kinds in ``meeting_kinds`` (client and prospect), and only when Claude is sure enough
(``min_classification_confidence``) or you set the kind yourself in the app. An unsure one waits on its
meeting page: correct it there, then **Push**.

Auth is an API key in ``TWENTY_API_KEY`` (Twenty → **Settings** → **APIs & Webhooks** → **Create key**).
Its role needs read and write on companies, people, opportunities, notes and tasks; reading the data
model (Settings permission "Data model") lets the sink learn your pipeline's stages and custom fields,
and without it the standard stages are assumed and ``fields`` is skipped.

What it does with a client or prospect meeting:

* **Company.** The company named in the meeting, compared to Twenty's companies by name with case,
  accents and legal forms (SAS, SARL, Inc…) ignored. One match is used; several match nothing. None:
  it is created (``create_records = false``: only records that exist are used).
* **People.** Each contact from the client's side: by e-mail, else by full name (a first name alone
  only among the company's people). Not found: created under the company, with the e-mail and role the
  meeting gave. A person found without a company gets this one; nothing else of theirs changes. Phone
  numbers heard in a meeting are too often misheard to go on a record: they stay in the note.
* **Deal** (an opportunity). The company's most recently updated open deal (not won, not lost); a
  client meeting with none uses its latest won deal. A prospect with no open deal gets a new one, named
  "<company> · <offer>". The deal **only moves forward**: a meeting's step (first call, discovery,
  proposal, won) moves it to the mapped stage when that stage comes later in the pipeline; "lost" moves
  an open deal to your lost stage (``stages.lost``, else a stage called Lost or Perdu). A won or lost
  deal never moves. ``fields`` writes the meeting's facts (offer, budget, objection, lost reason, next
  step) into the deal's fields: a text field takes the words, a select the option whose label they
  name (one match only), a multi-select gains it. A fact the meeting didn't give leaves its field alone.
* **Note.** The meeting notes, headed by the deal facts and where it was filed, on the company, the
  people and the deal. Pushing the notes again rewrites the same note (one deleted in Twenty stays
  deleted). A meeting with nothing to file it on sends nothing.
* **Tasks** (``owners = "mine"``: your follow-ups), assigned to you (the member with ``owner_email``,
  else the workspace's only member), due on the item's date at ``due_time``, linked to the deal, the
  company and the contact who owns it. Updates change the title, body and (when the item's date moves)
  the due date; done completes it, a task is never reopened. Cancelled or dismissed deletes it (to
  Twenty's trash, restorable), unless it is done.

Creates aren't retried after a 5xx or no answer (they may have been applied); searching first makes
pushing again safe for companies, people and deals. Ref formats: ``twenty:notes/<uuid>``,
``twenty:tasks/<uuid>``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from typing import Any, ClassVar, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
from pydantic import field_validator

from ..config import Config
from ..models import Classification, ClientContact, Item, ItemStatus, Meeting
from .base import Sink, SinkConfig, SinkError, env_secret, item_markdown
from .http import HttpSinkMixin

log = logging.getLogger(__name__)

REF_PREFIX = "twenty:"
DEFAULT_STAGES = {"first_call": "SCREENING", "discovery": "MEETING", "proposal": "PROPOSAL", "won": "CUSTOMER"}
"""Twenty's standard pipeline: New, Screening, Meeting, Proposal, Customer."""
STANDARD_ORDER = ("NEW", "SCREENING", "MEETING", "PROPOSAL", "CUSTOMER")
LOST_NAMES = {"lost", "perdu", "perdue", "closed lost", "closedlost", "closed_lost"}
LEGAL_FORMS = {"sas", "sasu", "sarl", "eurl", "sa", "sci", "scop", "snc", "ei", "eirl", "selarl", "inc", "llc",
               "ltd", "plc", "gmbh", "bv", "srl", "spa", "corp", "co"}  # fmt: skip
Fact = Literal["offer", "budget", "objection", "lost_reason", "next_step"]
PAGE = 200
"""Twenty answers at most this many records a request."""
_ERRORS = (400, 401, 403, 404, 409, 422)
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_EMAIL = re.compile(r"[^\s@,;:<>()\[\]\"']+@[^\s@,;:<>()\[\]\"']+\.[^\s@,;:<>()\[\]\"'.]+")


class TwentyConfig(SinkConfig):
    owners: Literal["all", "mine"] = "mine"
    base_url: str = ""
    """Your Twenty's address, e.g. ``https://crm.example.com`` (Twenty's cloud: ``https://api.twenty.com``)."""
    token_env: str = "TWENTY_API_KEY"
    owner_email: str | None = None
    """Your Twenty login e-mail: your tasks are assigned to you. Unset: the workspace's only member."""
    due_time: time = time(9, 0)
    meeting_kinds: list[Literal["client", "prospect"]] = ["client", "prospect"]
    min_classification_confidence: float = 0.7
    """Below this, a meeting Claude called a client meeting waits for you (set its kind in the app, then Push)."""
    create_records: bool = True
    """Create the company, people and deal a meeting names when Twenty doesn't have them."""
    move_stage: bool = True
    """Move the deal forward to the stage the meeting reached (never backward)."""
    stages: dict[Literal["first_call", "discovery", "proposal", "won", "lost"], str] = {}
    """The stage value (not its label) each step moves the deal to; unset ones use Twenty's standard stages."""
    fields: dict[str, Fact] = {}
    """A deal field's API name → the meeting fact it takes, e.g. ``{ formule = "offer", doutePrincipal = "objection" }``."""

    @field_validator("base_url")
    @classmethod
    def _url(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if not v:
            return ""
        parts = urlsplit(v)
        if parts.scheme not in ("https", "http") or not parts.hostname:
            raise ValueError("give the address of your Twenty, e.g. https://crm.example.com")
        if parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("use https:// (your API key travels with every request)")
        return re.sub(r"/rest$", "", v)


class TwentyError(SinkError):
    def __init__(self, message: str, *, status: int) -> None:
        super().__init__(message, retryable=False)
        self.status = status


@dataclass
class Filed:
    """Where a meeting is filed in Twenty."""

    company: dict[str, Any] | None = None
    people: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Folded contact name → person record."""
    deal: dict[str, Any] | None = None
    stage_label: str = ""

    def empty(self) -> bool:
        return self.company is None and not self.people and self.deal is None


@dataclass
class Pipeline:
    order: list[str]
    labels: dict[str, str]
    won: str | None
    lost: str | None

    def rank(self, stage: str | None) -> int:
        return self.order.index(stage) if stage in self.order else -1


class TwentySink(HttpSinkMixin, Sink):
    name = "twenty"
    Config: ClassVar[type[SinkConfig]] = TwentyConfig
    supports_update = True
    supports_notes = True
    cfg: TwentyConfig

    def __init__(self, cfg: TwentyConfig, app: Config) -> None:  # type: ignore[override]
        super().__init__(cfg, app)
        self._member: dict[str, Any] | None = None
        self._member_known = False
        self._schema: dict[str, dict[str, Any]] | None = None
        self._schema_known = False
        self._filed: dict[tuple[str, str], Filed] = {}
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- HTTP

    def _base(self) -> str:
        if not self.cfg.base_url:
            raise SinkError("twenty: set base_url to your Twenty's address (e.g. https://crm.example.com)", retryable=False)
        return self.cfg.base_url

    async def _api(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        resp = await self.request(
            method,
            self._base() + path,
            headers={"Authorization": f"Bearer {env_secret(self.cfg, 'token_env', 'TWENTY_API_KEY')}"},
            ok=(200, 201, 204, *_ERRORS),
            write_safe=method == "PATCH",  # absolute values; a create is never resent after a 5xx
            **kw,
        )
        if resp.status_code in _ERRORS:
            raise _error(resp, method, path, self.cfg.token_env)
        try:
            body = resp.json() if resp.content else {}
        except ValueError:
            raise TwentyError(f"twenty: {method} {path} didn't answer JSON: is base_url your Twenty's address?",
                              status=resp.status_code) from None
        return body if isinstance(body, dict) else {}

    async def _find(self, plural: str, filter_: str, order_by: str | None = None) -> tuple[list[dict[str, Any]], bool]:
        """Records matching ``filter_``, and whether that is all of them."""
        params = {"filter": filter_, "limit": str(PAGE)}
        if order_by:
            params["order_by"] = order_by
        body = await self._api("GET", f"/rest/{plural}", params=params)
        records = [r for r in (body.get("data") or {}).get(plural) or [] if isinstance(r, dict)]
        total = body.get("totalCount")
        return records, not isinstance(total, int) or total <= len(records)

    async def _create(self, plural: str, singular: str, data: dict[str, Any]) -> dict[str, Any]:
        body = await self._api("POST", f"/rest/{plural}", json=data)
        record = (body.get("data") or {}).get(f"create{singular[0].upper()}{singular[1:]}") or {}
        if not _UUID.fullmatch(str(record.get("id") or "")):  # it may exist: never retry this create
            raise SinkError(f"twenty: created a {singular} but got no id back: {json.dumps(body)[:200]}", retryable=False)
        return record

    async def _update(self, plural: str, record_id: str, data: dict[str, Any]) -> None:
        await self._api("PATCH", f"/rest/{plural}/{_uuid(record_id)}", json=data)

    # ------------------------------------------------------------ workspace

    async def _owner(self) -> dict[str, Any] | None:
        """You, as a workspace member: by ``owner_email``, else the workspace's only member."""
        if not self._member_known:
            body = await self._api("GET", "/rest/workspaceMembers", params={"limit": "60"})
            members = [m for m in (body.get("data") or {}).get("workspaceMembers") or [] if isinstance(m, dict)]
            email = (self.cfg.owner_email or "").strip().lower()
            if email:
                members = [m for m in members if str(m.get("userEmail") or "").lower() == email]
                if not members:
                    log.warning("twenty: no workspace member has the e-mail %s (owner_email); tasks stay unassigned", email)
            elif len(members) != 1:
                log.warning("twenty: this workspace has several members; set owner_email so your tasks are assigned to you")
                members = []
            self._member, self._member_known = (members[0] if members else None), True
        return self._member

    async def _opportunity_fields(self) -> dict[str, dict[str, Any]] | None:
        """The deal object's fields by API name (stages, custom fields), ``None`` if the key can't read the
        data model."""
        if self._schema_known:
            return self._schema
        params = {"limit": str(PAGE)}
        try:
            for _ in range(10):
                body = await self._api("GET", "/rest/metadata/objects", params=params)
                data = body.get("data")
                objects = data if isinstance(data, list) else (data or {}).get("objects") or []
                for obj in objects:
                    if isinstance(obj, dict) and obj.get("nameSingular") == "opportunity":
                        fields = obj.get("fields") or []
                        if isinstance(fields, dict):  # a GraphQL-style connection
                            fields = [e.get("node") for e in fields.get("edges") or [] if isinstance(e, dict)]
                        self._schema = {f["name"]: f for f in fields
                                        if isinstance(f, dict) and f.get("name") and f.get("isActive", True)}
                        break
                else:
                    page = body.get("pageInfo") or {}
                    if page.get("hasNextPage") and page.get("endCursor"):
                        params = {"limit": str(PAGE), "starting_after": str(page["endCursor"])}
                        continue
                break
        except TwentyError as exc:
            if exc.status == 401:
                raise
            log.warning("twenty: can't read the data model (%s); assuming the standard stages, skipping `fields`", exc)
        self._schema_known = True
        return self._schema

    async def _pipeline(self) -> Pipeline:
        schema = await self._opportunity_fields()
        options = sorted(((schema or {}).get("stage") or {}).get("options") or [],
                         key=lambda o: o.get("position") if isinstance(o.get("position"), int | float) else 0)
        order = [str(o["value"]) for o in options if o.get("value")] or list(STANDARD_ORDER)
        labels = {str(o["value"]): str(o.get("label") or o["value"]) for o in options if o.get("value")}
        lost = self.cfg.stages.get("lost") or next(
            (str(o["value"]) for o in options if fold(str(o.get("value") or "")) in LOST_NAMES
             or fold(str(o.get("label") or "")) in LOST_NAMES), None)
        won = self.cfg.stages.get("won") or DEFAULT_STAGES["won"]
        return Pipeline(order, labels, won if won in order else None, lost if lost in order else None)

    def _wanted(self, step: str, pipe: Pipeline) -> str | None:
        """The stage a meeting's step moves a deal to, if your pipeline has it."""
        if step == "lost":
            return pipe.lost
        stage = self.cfg.stages.get(step) or DEFAULT_STAGES.get(step)  # type: ignore[call-overload]
        return stage if stage in pipe.order else None

    # -------------------------------------------------------------- matching

    async def _lookup(
        self, plural: str, path: str, word: str, same: Callable[[dict[str, Any]], bool]
    ) -> tuple[list[dict[str, Any]], bool]:
        """The records whose ``path`` contains ``word`` and that ``same`` accepts, and whether the search saw
        all candidates. Twenty's ilike ignores case, not accents: the word is looked for as written and
        without its accents, then (if that found none) with every letter that could carry one left open,
        so "Gaia" finds "GAÏA"."""
        complete = True
        for filter_ in (_any_ilike(path, word), _open_ilike(path, word)):
            if filter_ is None:
                continue
            hits, seen_all = await self._find(plural, filter_)
            if matches := [h for h in hits if same(h)]:
                return matches, seen_all
            complete = complete and seen_all
        return [], complete

    async def _company(self, name: str) -> tuple[dict[str, Any] | None, bool]:
        """``(the company, sure)``: one record whose name matches; ``sure`` is false when several match or
        too many came back to tell (then nothing is created either)."""
        key = company_key(name)
        tokens = sorted(key.split(), key=len, reverse=True)
        if not tokens:
            return None, False
        hits, complete = await self._lookup("companies", "name", _word_in(name, tokens[0]),
                                            lambda c: company_key(str(c.get("name") or "")) == key)
        if len(hits) == 1:
            return hits[0], True
        return None, not hits and complete

    async def _person(self, contact: ClientContact, company_id: str | None) -> dict[str, Any] | None:
        if (email := _EMAIL.fullmatch(contact.email.strip())) is not None:
            hits, _ = await self._find("people", f'emails.primaryEmail[eq]:"{_quote(email.group(0).lower())}"')
            if len(hits) == 1:
                return hits[0]
        words = contact.name.split()
        if len(words) >= 2:
            same, _ = await self._lookup("people", "name.lastName", words[-1],
                                         lambda p: fold(_full_name(p)) == fold(contact.name))
            if len(same) > 1 and company_id:
                same = [p for p in same if p.get("companyId") == company_id]
            return same[0] if len(same) == 1 else None
        if words and company_id:  # a first name alone: only among the company's people
            hits, _ = await self._find("people", f"companyId[eq]:{_uuid(company_id)}")
            same = [p for p in hits if fold(str((p.get("name") or {}).get("firstName") or "")) == fold(words[0])]
            return same[0] if len(same) == 1 else None
        return None

    async def _file(self, meeting: Meeting) -> Filed:
        """Find (or create) the company, people and deal of a client meeting, and move the deal on. Once
        per meeting and classification: tasks and notes are filed on the same records."""
        c = meeting.classification
        assert c is not None
        key = (meeting.id, c.model_dump_json())
        async with self._lock:
            if (done := self._filed.get(key)) is not None:
                return done
            filed = Filed()
            create = self.cfg.create_records
            company, sure = (await self._company(c.company)) if c.company else (None, False)
            if company is None and sure and create:
                company = await self._create("companies", "company", {"name": c.company})
                log.info("twenty: created the company %s", c.company)
            filed.company = company
            for contact in c.contacts:
                person = await self._person(contact, company and company["id"])
                if person is None and create and (company or _EMAIL.fullmatch(contact.email.strip())):
                    person = await self._create("people", "person", _person_data(contact, company and company["id"]))
                    log.info("twenty: created the person %s", contact.name)
                elif person is not None and company and not person.get("companyId"):
                    await self._update("people", person["id"], {"companyId": company["id"]})
                    person = {**person, "companyId": company["id"]}
                if person is not None:
                    filed.people[fold(contact.name)] = person
            if filed.company is None:  # no company named: the one of the people you met, if they share one
                ids = {p.get("companyId") for p in filed.people.values() if p.get("companyId")}
                if len(ids) == 1:
                    filed.company = {"id": ids.pop()}
            if filed.company is not None:
                await self._deal(meeting, c, filed)
            self._filed[key] = filed
            return filed

    async def _deal(self, meeting: Meeting, c: Classification, filed: Filed) -> None:
        assert filed.company is not None
        pipe = await self._pipeline()
        deals, _ = await self._find("opportunities", f"companyId[eq]:{_uuid(filed.company['id'])}",
                                    order_by="updatedAt[DescNullsLast]")
        open_ = [d for d in deals if d.get("stage") not in (pipe.won, pipe.lost)]
        won = [d for d in deals if d.get("stage") == pipe.won]
        deal = open_[0] if open_ else (won[0] if won and c.kind == "client" else None)
        wanted = self._wanted(c.step, pipe) if self.cfg.move_stage else None
        if deal is None:
            if c.kind != "prospect" or not self.cfg.create_records:
                return
            name = str(filed.company.get("name") or c.company)
            data: dict[str, Any] = {"name": f"{name} · {c.offer}" if c.offer else name, "companyId": filed.company["id"],
                                    "stage": wanted or pipe.order[0]}
            if filed.people:
                data["pointOfContactId"] = next(iter(filed.people.values()))["id"]
            if member := await self._owner():
                data["ownerId"] = member["id"]
            deal = await self._create("opportunities", "opportunity", data | await self._facts(c, {}))
            log.info("twenty: created the deal %s at %s", data["name"], data["stage"])
        else:
            changes = await self._facts(c, deal)
            stage = deal.get("stage")
            if wanted and stage not in (pipe.won, pipe.lost) and (wanted == pipe.lost or pipe.rank(wanted) > pipe.rank(stage)):
                changes["stage"] = wanted
            if changes:
                await self._update("opportunities", deal["id"], changes)
                deal = {**deal, **changes}
                log.info("twenty: updated the deal %s: %s", deal.get("name"), ", ".join(changes))
        filed.deal = deal
        filed.stage_label = pipe.labels.get(str(deal.get("stage")), str(deal.get("stage") or ""))

    async def _facts(self, c: Classification, deal: dict[str, Any]) -> dict[str, Any]:
        """The ``fields`` the meeting gives a value to, as Twenty takes them (only those that change)."""
        if not self.cfg.fields:
            return {}
        schema = await self._opportunity_fields()
        if schema is None:
            return {}
        out: dict[str, Any] = {}
        for name, fact in self.cfg.fields.items():
            text = getattr(c, fact, "")
            meta = schema.get(name)
            if not text:
                continue
            if meta is None:
                log.warning("twenty: the deal has no field %r (fields.%s); see Settings → Data model", name, name)
                continue
            kind = str(meta.get("type") or "")
            options = [o for o in meta.get("options") or [] if isinstance(o, dict) and o.get("value")]
            value: Any
            if kind == "TEXT":
                value = text
            elif kind == "SELECT":
                value = _option(options, text)
            elif kind == "MULTI_SELECT":
                current = deal.get(name) if isinstance(deal.get(name), list) else []
                hit = _option(options, text)
                value = current + [hit] if hit and hit not in current else None
            elif kind in ("RICH_TEXT", "RICH_TEXT_V2"):
                if (deal.get(name) or {}).get("markdown") == text:
                    continue
                value = {"markdown": text}
            else:
                log.warning("twenty: fields.%s is a %s field; only text, select and multi-select take a fact", name, kind)
                continue
            if value is not None and value != deal.get(name):
                out[name] = value
        return out

    # -------------------------------------------------------------- sink API

    def accepts_meeting(self, meeting: Meeting) -> bool:
        c = meeting.classification
        return (c is not None and c.kind in self.cfg.meeting_kinds
                and (c.by_you or c.confidence >= self.cfg.min_classification_confidence))

    def skip_reason(self, meeting: Meeting) -> str:
        c = meeting.classification
        if c is None:
            return "the meeting isn't classified yet"
        if c.kind not in self.cfg.meeting_kinds:
            return f"not a client meeting ({c.kind})"
        return f"unsure it is a {c.kind} meeting: set its kind on the meeting's page, then push"

    def accepts(self, item: Item, default_min_conf: float) -> bool:
        return super().accepts(item, default_min_conf) and item.status != ItemStatus.CANCELLED

    async def push_item(self, item: Item, meeting: Meeting) -> str:
        filed = await self._file(meeting)
        data: dict[str, Any] = {
            "title": " ".join(item.title.split())[:250],
            "bodyV2": {"markdown": item_markdown(item, meeting)},
            "status": "DONE" if item.status == ItemStatus.DONE else "TODO",
        }
        if due := _iso_date(item.due_date):
            data["dueAt"] = self._due(due)
        if item.is_mine(self.app.user_name) and (member := await self._owner()):
            data["assigneeId"] = member["id"]
        task = await self._create("tasks", "task", data)
        owner = filed.people.get(fold(item.owner or ""))
        await self._link("taskTargets", "taskId", task["id"], filed, [owner] if owner else [])
        return f"{REF_PREFIX}tasks/{task['id']}"

    async def update_item(self, item: Item, meeting: Meeting, ref: str) -> None:
        task_id = record_id(ref, "tasks")
        try:
            body = await self._api("GET", f"/rest/tasks/{task_id}")
        except TwentyError as exc:
            if exc.status != 404:
                raise
            log.info("twenty: the task for %s is no longer in Twenty; leaving it deleted", item.id)
            return
        task = (body.get("data") or {}).get("task") or {}
        done = task.get("status") == "DONE"
        if item.dismissed or item.status == ItemStatus.CANCELLED:
            if not done:
                await self._api("DELETE", f"/rest/tasks/{task_id}")  # to the trash, restorable
            return
        data: dict[str, Any] = {"title": " ".join(item.title.split())[:250],
                                "bodyV2": {"markdown": item_markdown(item, meeting)}}
        if (due := _iso_date(item.due_date)) and self._local_day(task.get("dueAt")) != due.isoformat():
            data["dueAt"] = self._due(due)
        if item.status == ItemStatus.DONE and not done:
            data["status"] = "DONE"
        await self._update("tasks", task_id, data)

    async def push_notes(self, meeting: Meeting, notes_markdown: str) -> str | None:
        # a note already in Twenty is rewritten even if you have since said it wasn't a client meeting
        filed = await self._file(meeting) if self.accepts_meeting(meeting) else Filed()
        data = {"title": _note_title(meeting), "bodyV2": {"markdown": note_body(meeting, notes_markdown, filed)}}
        previous = meeting.notes_ref.get(self.name)
        if previous and previous.startswith(f"{REF_PREFIX}notes/"):
            try:
                await self._update("notes", record_id(previous, "notes"), data)
            except TwentyError as exc:
                if exc.status != 404:
                    raise
                log.info("twenty: the note %s was deleted in Twenty; leaving it deleted", previous)
            return previous
        if filed.empty():
            log.info("twenty: meeting %s names no company or person Twenty has or could create; not filing it", meeting.id)
            return None
        note = await self._create("notes", "note", data)
        await self._link("noteTargets", "noteId", note["id"], filed, list(filed.people.values()))
        return f"{REF_PREFIX}notes/{note['id']}"

    async def _link(self, plural: str, key: str, record: str, filed: Filed, people: list[dict[str, Any]]) -> None:
        """Show a note or task on the deal, the company and ``people``. A link that fails is logged: the
        note or task exists, and creating it again would make a second one."""
        targets = [("targetOpportunityId", filed.deal), ("targetCompanyId", filed.company)]
        targets += [("targetPersonId", p) for p in people]
        for target, rec in targets:
            if rec is None:
                continue
            try:
                await self._api("POST", f"/rest/{plural}", json={key: record, target: rec["id"]})
            except SinkError as exc:
                log.warning("twenty: couldn't link %s to a record: %s", record, exc)

    async def check(self) -> str:
        parts = [f"Twenty at {urlsplit(self._base()).hostname}"]
        await self._api("GET", "/rest/opportunities", params={"limit": "1"})  # the key, and reading deals
        self._member_known = self._schema_known = False  # always a live lookup
        member = await self._owner()
        if member:
            parts.append(f"your tasks assigned to {member.get('userEmail') or _full_name(member)}")
        else:
            parts.append("tasks unassigned: set owner_email to your Twenty login e-mail")
        schema = await self._opportunity_fields()
        pipe = await self._pipeline()
        steps = [f"{s.replace('_', ' ')} → {pipe.labels.get(st, st)}" for s in ("first_call", "discovery", "proposal", "won", "lost")
                 if (st := self._wanted(s, pipe))]
        if self.cfg.move_stage:
            parts.append("deals move forward: " + (", ".join(steps) or "no stage matches; set `stages`"))
        if schema is None:
            parts.append("can't read the data model (give the key the Data model permission), so standard stages assumed")
        if self.cfg.fields:
            missing = [n for n in self.cfg.fields if schema is not None and n not in schema]
            parts.append(f"fields: {len(self.cfg.fields) - len(missing)} found"
                         + (f", missing: {', '.join(missing)}" if missing else ""))
        kinds = " and ".join(self.cfg.meeting_kinds)
        parts.append(f"files {kinds} meetings Claude is {self.cfg.min_classification_confidence:.0%} sure of")
        return " · ".join(parts)

    # ---------------------------------------------------------------- dates

    def _zone(self) -> ZoneInfo | None:
        return ZoneInfo(self.app.timezone) if self.app.timezone else None

    def _due(self, day: date) -> str:
        naive = datetime.combine(day, self.cfg.due_time)
        local = naive.replace(tzinfo=self._zone()) if self.app.timezone else naive.astimezone()
        return local.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

    def _local_day(self, stamp: Any) -> str | None:
        try:
            moment = datetime.fromisoformat(str(stamp or "").replace("Z", "+00:00"))
        except ValueError:
            return None
        return moment.astimezone(self._zone()).date().isoformat()


# ------------------------------------------------------------------ notes

NOTE_LABELS = {
    "en": {"budget": "Budget", "objection": "Main objection", "lost_reason": "Lost because", "next_step": "Next step",
           "contacts": "Contacts", "filed": "Filed by Voicestream on", "deal": "deal", "meeting": "Meeting"},
    "fr": {"budget": "Budget", "objection": "Doute principal", "lost_reason": "Raison de la perte",
           "next_step": "Prochaine étape", "contacts": "Contacts", "filed": "Classée par Voicestream sur",
           "deal": "opportunité", "meeting": "Réunion"},
}  # fmt: skip


def _lang(meeting: Meeting) -> str:
    return "fr" if (meeting.language or "").startswith("fr") else "en"


def _note_title(meeting: Meeting) -> str:
    label = NOTE_LABELS[_lang(meeting)]["meeting"]
    return " ".join((meeting.title or f"{label} {meeting.started_at.astimezone():%Y-%m-%d %H:%M}").split())[:250]


def note_body(meeting: Meeting, notes_markdown: str, filed: Filed) -> str:
    """The meeting's notes for Twenty: the deal facts first (the "fiche"), then the notes without their
    title (the note has its own), then where it was filed."""
    t = NOTE_LABELS[_lang(meeting)]
    c = meeting.classification or Classification()
    facts = [f"**{t[k]} :** {v}" if _lang(meeting) == "fr" else f"**{t[k]}:** {v}"
             for k in ("budget", "objection", "lost_reason", "next_step") if (v := getattr(c, k))]
    people = [", ".join(filter(None, [x.role, x.email])) for x in c.contacts]
    contacts = "; ".join(f"{x.name} ({p})" if p else x.name for x, p in zip(c.contacts, people, strict=True))
    lines = []
    if facts:
        lines += ["  \n".join(facts), ""]
    if contacts:
        lines += [f"**{t['contacts']}{' :' if _lang(meeting) == 'fr' else ':'}** {contacts}", ""]
    body = re.sub(r"\A\s*# [^\n]*\n", "", notes_markdown or "").strip()
    if lines and body:
        lines += ["---", ""]
    lines += [body, ""] if body else []
    where = [str(filed.company.get("name") or "") if filed.company else ""]
    where += [_full_name(p) for p in filed.people.values()]
    if filed.deal is not None:
        deal = str(filed.deal.get("name") or "")
        where.append(f"{t['deal']} « {deal} » ({filed.stage_label})" if filed.stage_label else f"{t['deal']} « {deal} »")
    if any(where):
        lines.append(f"_{t['filed']} {' · '.join(w for w in where if w)}._")
    return "\n".join(lines).strip()


# ---------------------------------------------------------------- helpers


def _error(resp: httpx.Response, method: str, path: str, token_env: str) -> TwentyError:
    """Twenty's error body: ``{"statusCode", "error", "messages": [...]}`` (or a ``message``)."""
    try:
        body = resp.json()
    except ValueError:
        body = None
    body = body if isinstance(body, dict) else {}
    messages = body.get("messages") if isinstance(body.get("messages"), list) else []
    message = "; ".join(map(str, messages)) or str(body.get("message") or body.get("error") or resp.text[:300])
    hint = {
        401: f"{token_env} is wrong or revoked: create an API key in Twenty (Settings → APIs & Webhooks)",
        403: "the API key's role can't do this: give it read and write on companies, people, opportunities, notes "
             "and tasks (Settings → Roles)",
        404: "it no longer exists in Twenty",
    }.get(resp.status_code)
    return TwentyError(f"twenty: HTTP {resp.status_code} on {method} {path}: {message}" + (f" — {hint}" if hint else ""),
                       status=resp.status_code)


def record_id(ref: str, plural: str) -> str:
    """The uuid in a ``twenty:<plural>/<uuid>`` ref (anything else never reaches a URL)."""
    rid = ref.removeprefix(f"{REF_PREFIX}{plural}/")
    if rid == ref or not _UUID.fullmatch(rid):
        raise SinkError(f"twenty: not a Twenty {plural} reference: {ref!r}", retryable=False)
    return rid


def _uuid(value: Any) -> str:
    if not _UUID.fullmatch(str(value)):
        raise SinkError(f"twenty: unexpected record id {value!r}", retryable=False)
    return str(value)


def plain(text: str) -> str:
    """``text`` without accents ("Gaïa" → "Gaia")."""
    return "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch))


def fold(text: str) -> str:
    """Comparison key: no accents, no case, single spaces."""
    return " ".join(plain(text).casefold().split())


def company_key(name: str) -> str:
    """A company's name for comparing: folded, punctuation and legal forms (SAS, SARL, Inc…) left out."""
    words = re.split(r"[^\w]+", fold(name).replace(".", ""))  # "S.A.S." is SAS
    return " ".join(w for w in words if w and w not in LEGAL_FORMS)


def _word_in(name: str, folded_word: str) -> str:
    """The word of ``name`` (as written, with its accents) that folds to ``folded_word``."""
    for word in re.split(r"[^\w]+", name):
        if fold(word) == folded_word:
            return word
    return folded_word


def _quote(value: str) -> str:
    return value.replace('"', "").replace("\\", "")


def _any_ilike(path: str, word: str) -> str:
    """``path`` contains ``word``, written as said or without its accents (Twenty's ilike doesn't fold them)."""
    variants = list(dict.fromkeys(_quote(v) for v in (word, plain(word)) if v))
    clauses = [f'{path}[ilike]:"%{v}%"' for v in variants]
    return clauses[0] if len(clauses) == 1 else f"or({','.join(clauses)})"


_MAY_CARRY_ACCENT = re.compile(r"[aeiouycn]", re.IGNORECASE)


def _open_ilike(path: str, word: str) -> str | None:
    """``path`` contains ``word`` with each letter that could carry an accent as ilike's one-character
    wildcard ("Gaia" → ``G___``); ``None`` when that leaves nothing to look for."""
    base = _quote(plain(word))
    pattern = _MAY_CARRY_ACCENT.sub("_", base)
    if pattern == base or not pattern.strip("_%"):
        return None
    return f'{path}[ilike]:"%{pattern.replace("%", "")}%"'


def _full_name(record: dict[str, Any]) -> str:
    name = record.get("name") or {}
    if isinstance(name, dict):
        return " ".join(filter(None, [str(name.get("firstName") or ""), str(name.get("lastName") or "")]))
    return str(name)


def _person_data(contact: ClientContact, company_id: str | None) -> dict[str, Any]:
    first, _, last = " ".join(contact.name.split()).partition(" ")
    data: dict[str, Any] = {"name": {"firstName": first, "lastName": last}}
    if company_id:
        data["companyId"] = company_id
    if contact.role:
        data["jobTitle"] = contact.role
    if email := _EMAIL.fullmatch(contact.email.strip()):
        data["emails"] = {"primaryEmail": email.group(0).lower()}
    return data


def _option(options: list[dict[str, Any]], text: str) -> str | None:
    """The select option ``text`` names: its label or value exactly, else the only one whose label is a word
    of it ("Identité, version allégée" → Identité)."""
    t = fold(text)
    exact = [o for o in options if t in (fold(str(o.get("label") or "")), fold(str(o["value"])))]
    if len(exact) == 1:
        return str(exact[0]["value"])
    named = [o for o in options if (lbl := fold(str(o.get("label") or "")))
             and re.search(rf"(?<!\w){re.escape(lbl)}(?!\w)", t)]
    return str(named[0]["value"]) if len(named) == 1 else None


def _iso_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None
