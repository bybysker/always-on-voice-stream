"""HubSpot sink: your action items become HubSpot tasks assigned to you, linked to the
contacts and companies you met, and each meeting is logged on their timelines.

Auth is a bearer token in ``HUBSPOT_TOKEN``. Use a *service key*, HubSpot's credential
for single-account, data-only integrations (public beta since February 2026). A legacy
private-app token (``pat-…``) works the same way, but HubSpot stops letting existing
accounts create new private apps on 2026-10-26. The calls use the date-versioned API
(``2026-09``): HubSpot stops supporting the ``/v3/`` paths in September 2027.

Scopes the key needs: ``crm.objects.contacts.write`` (tasks, meetings and notes are
activities, which HubSpot files under the contacts scope), ``crm.objects.contacts.read``
and ``crm.objects.companies.read`` (matching participants), ``crm.objects.companies.write``
(linking an activity to a company; companies themselves are never changed) and
``crm.objects.owners.read`` (finding you).

Setup, once:

1. In HubSpot open **Development** → **Keys** → **Service keys** → **Create service key**
   (some accounts have it under **Settings** → **Integrations** → **Service Keys**; you
   need Super Admin or Developer Tools access). Name it "voicestream", click **Add new
   scope**, tick the five scopes above, **Update**, then **Create** and confirm.
2. Open the key, click **Show**, copy it and add ``HUBSPOT_TOKEN=<the key>`` to
   ``~/.voicestream/.env``.
3. In ``voicestream.toml`` add ``[sinks.hubspot]`` with ``enabled = true`` and
   ``owner_email = "<the address you sign in to HubSpot with>"``, then run
   ``voicestream doctor``. It should show ``hubspot: portal … · tasks assigned to …``.

What it does:

* **Tasks** (``owners = "mine"``: your follow-ups). A to-do assigned to you: the HubSpot
  user with ``owner_email`` or, when that's unset, the account's only user. With several
  users and no ``owner_email``, tasks stay unassigned. Due on the item's date at
  ``due_time`` (``timezone`` from the config, else your Mac's). HubSpot requires a due
  date, so an undated item is due the next workday. The body is the item as HTML, since
  HubSpot's rich-text fields drop plain line breaks.
* **Matching** (``match = "participants"``). Each participant, follow-up recipient and
  item owner other than you is looked up: an e-mail address by contact e-mail, a full
  name ("Léa Dupont") by last name, then compared to the contacts' first + last name
  ignoring case and accents. Anything else (one word like "Acme") is compared to company
  names. Only a single exact match counts: two "Léa Dupont"s link neither. A matched
  contact also brings its primary company. Contacts are never created. The lookups
  happen once per meeting; CRM search allows 5 requests per second, so they're spaced
  out. HubSpot doesn't document whether search ignores accents, so a last name that
  only the CRM writes with accents ("Lefèvre" heard as "Lefevre") may not be found.
* **Meeting log** at the end: a Meeting activity (``log_meeting = false``: a Note) with
  the notes as its body, the start and end times and outcome "Completed", on the
  matched contacts and companies. A meeting nobody in the CRM attended isn't logged
  unless ``log_unmatched = true``: this assistant records every meeting, internal ones
  and personal calls too, and an activity on no record shows on no timeline, so it
  would only clutter the CRM with private notes. Pushing the notes again rewrites the
  same activity, unless it was deleted in HubSpot, in which case it stays deleted.
* **Updates** change the subject, body, priority and (only when the item's due date
  moves) the due date, so a time you set in HubSpot survives other edits. Done items
  complete the task; a task is never reopened or reassigned. Cancelled and dismissed
  items archive the task, unless HubSpot shows it completed: then it's a record of work
  done and stays. Archiving is HubSpot's delete: the task goes to the recycle bin, and
  for 90 days a Super Admin can restore it from a contact or company it's linked to
  (**Actions** → **Restore activity**). HubSpot tasks have no "cancelled" state, and
  completing a task that was never done would be worse.

Going over a rate limit (100 requests per 10 s per app on Free/Starter, 190 on
Professional/Enterprise, plus a daily cap per account) gets a 429. Those are retried,
and so are 5xx answers, except to a create: one that got a 5xx or no answer at all may
have been applied, and there's no idempotency key to make resending it safe. It fails
without a retry instead: check HubSpot and push it again by hand if it's missing.

Ref formats: ``hubspot:tasks/<id>``; ``hubspot:meetings/<id>`` or ``hubspot:notes/<id>``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from datetime import UTC, date, datetime, time, timedelta
from time import monotonic
from typing import Any, ClassVar, Literal
from zoneinfo import ZoneInfo

import httpx

from ..brain.dates import today
from ..config import Config
from ..models import Item, ItemStatus, Meeting
from ..render import markdown_to_html
from .base import Sink, SinkConfig, SinkError, env_secret, item_markdown
from .http import HttpSinkMixin

log = logging.getLogger(__name__)

API = "https://api.hubapi.com"
VERSION = "2026-09"
OBJECTS = f"/crm/objects/{VERSION}"
REF_PREFIX = "hubspot:"
SCOPES = (
    "crm.objects.contacts.write",
    "crm.objects.contacts.read",
    "crm.objects.companies.read",
    "crm.objects.companies.write",
    "crm.objects.owners.read",
)
ASSOCIATION_TYPES = {"tasks": (204, 192), "meetings": (200, 188), "notes": (202, 190)}
"""HUBSPOT_DEFINED association type ids: activity → contact, activity → company."""
PRIORITY = {"high": "HIGH", "medium": "MEDIUM", "low": "LOW"}
BODY_MAX = 65_536
"""HubSpot's cap on a note body; applied to every rich-text body."""
SEARCH_LIMIT = 200
MAX_FILTER_GROUPS = 5
SEARCH_INTERVAL_S = 0.25
"""CRM search allows 5 requests per second per account."""
CONTACT_PROPERTIES = ["firstname", "lastname", "email", "associatedcompanyid"]
_ERRORS = (400, 401, 403, 404, 409)
"""Statuses :func:`_error` explains (the mixin retries 429 and 5xx)."""
_EMAIL = re.compile(r"[^\s@,;:<>()\[\]\"']+@[^\s@,;:<>()\[\]\"']+\.[^\s@,;:<>()\[\]\"'.]+")


class HubSpotConfig(SinkConfig):
    owners: Literal["all", "mine"] = "mine"
    token_env: str = "HUBSPOT_TOKEN"
    owner_email: str | None = None
    """Your HubSpot login e-mail; tasks of yours are assigned to that user. Unset: the account's only user."""
    due_time: time = time(9, 0)
    match: Literal["participants", "off"] = "participants"
    log_meeting: bool = True
    """Log each meeting as a Meeting activity; ``false`` logs its notes as a Note instead."""
    log_unmatched: bool = False
    """Also log meetings where no participant matched a contact or company (they'd be on no record)."""


class HubSpotError(SinkError):
    def __init__(self, message: str, *, status: int) -> None:
        super().__init__(message, retryable=False)
        self.status = status


class HubSpotSink(HttpSinkMixin, Sink):
    name = "hubspot"
    Config: ClassVar[type[SinkConfig]] = HubSpotConfig
    supports_update = True
    supports_notes = True
    cfg: HubSpotConfig

    def __init__(self, cfg: HubSpotConfig, app: Config) -> None:  # type: ignore[override]
        super().__init__(cfg, app)
        self._owner: dict[str, Any] | None = None
        self._owner_known = False
        self._matches: dict[str, tuple[str | None, str | None]] = {}
        self._matches_for: str | None = None
        self._last_search = 0.0

    async def _api(self, method: str, path: str, **kw: Any) -> httpx.Response:
        resp = await self.request(
            method,
            API + path,
            headers={"Authorization": f"Bearer {env_secret(self.cfg, 'token_env', 'HUBSPOT_TOKEN')}"},
            ok=(200, 201, 204, *_ERRORS),
            # updates set absolute values and searches only read; a create is never resent after a 5xx
            write_safe=method == "PATCH" or path.endswith("/search"),
            **kw,
        )
        if resp.status_code in _ERRORS:
            raise _error(resp, method, path, self.cfg.token_env)
        return resp

    # ------------------------------------------------------------ lookups

    async def _owner_record(self) -> dict[str, Any] | None:
        """You, as a HubSpot owner: by ``owner_email``, else the account's only user."""
        if not self._owner_known:
            email = (self.cfg.owner_email or "").strip()
            params: dict[str, Any] = {"email": email} if email else {"limit": 100}
            page = (await self._api("GET", f"/crm/owners/{VERSION}", params=params)).json()
            people = [o for o in page.get("results") or [] if o.get("type", "PERSON") == "PERSON"]
            if email:
                people = [o for o in people if (o.get("email") or "").lower() == email.lower()]
                if not people:
                    log.warning("hubspot: no HubSpot user has the e-mail %s (owner_email); tasks stay unassigned", email)
            elif len(people) != 1 or (page.get("paging") or {}).get("next"):
                log.warning("hubspot: this account has several users; set owner_email so tasks are assigned to you")
                people = []
            self._owner, self._owner_known = (people[0] if people else None), True
        return self._owner

    async def _search(
        self, object_type: str, filters: list[dict[str, str]], properties: list[str]
    ) -> list[dict[str, Any]] | None:
        """Records matching any of ``filters``; ``None`` when there are too many to tell a unique match."""
        wait = self._last_search + SEARCH_INTERVAL_S - monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_search = monotonic()
        body = {"filterGroups": [{"filters": [f]} for f in filters], "properties": properties, "limit": SEARCH_LIMIT}
        data = (await self._api("POST", f"{OBJECTS}/{object_type}/search", json=body)).json()
        results = data.get("results") or []
        return results if int(data.get("total") or 0) <= len(results) else None

    async def _lookup(self, name: str) -> tuple[str | None, str | None]:
        """``(contact id, company id)`` for one participant; ``None`` unless exactly one record matches."""
        if email := _EMAIL.search(name):
            return _unique_contact(await self._search("contacts", [_eq("email", email.group(0))], CONTACT_PROPERTIES))
        words = name.split()
        if len(words) >= 2:
            last_names = [" ".join(words[i:]) for i in range(1, len(words))]
            values = list(dict.fromkeys(v for last in last_names for v in (last, plain(last))))[:MAX_FILTER_GROUPS]
            hits = await self._search("contacts", [_eq("lastname", v) for v in values], CONTACT_PROPERTIES)
            if hits is None:
                return None, None
            wanted = fold(name)
            hits = [h for h in hits if fold(f"{_prop(h, 'firstname')} {_prop(h, 'lastname')}") == wanted]
            if hits:
                return _unique_contact(hits)
        hits = await self._search("companies", [_eq("name", v) for v in dict.fromkeys((name, plain(name)))], ["name"])
        hits = [h for h in hits or [] if fold(_prop(h, "name")) == fold(name)]
        return None, (str(hits[0]["id"]) if len(hits) == 1 else None)

    async def _associations(self, kind: str, meeting: Meeting, item: Item | None = None) -> list[dict[str, Any]]:
        if self.cfg.match == "off":
            return []
        if self._matches_for != meeting.id:
            self._matches_for, self._matches = meeting.id, {}
        names = [*meeting.participants, *(meeting.followup_email.to if meeting.followup_email else [])]
        if item is not None:
            if item.owner and not item.is_mine(self.app.user_name):
                names.append(item.owner)
            names += _EMAIL.findall(item.detail)
        contacts: set[str] = set()
        companies: set[str] = set()
        for name in names:
            key = fold(name)
            if not key or key in (fold(self.app.user_name), "assistant"):
                continue
            if key not in self._matches:
                try:
                    self._matches[key] = await self._lookup(name.strip())
                except HubSpotError as exc:  # e.g. a missing read scope: link nothing rather than fail the push
                    log.warning("hubspot: couldn't look up a participant: %s", exc)
                    self._matches[key] = (None, None)
            contact, company = self._matches[key]
            contacts.update(filter(None, [contact]))
            companies.update(filter(None, [company]))
        to_contact, to_company = ASSOCIATION_TYPES[kind]
        return [_association(c, to_contact) for c in sorted(contacts)] + [_association(c, to_company) for c in sorted(companies)]

    # -------------------------------------------------------------- fields

    def _local(self, day: date) -> datetime:
        naive = datetime.combine(day, self.cfg.due_time)
        return naive.replace(tzinfo=ZoneInfo(self.app.timezone)) if self.app.timezone else naive.astimezone()

    def _local_day(self, stamp: str | None) -> str | None:
        try:
            moment = datetime.fromisoformat(stamp or "")
        except ValueError:
            return None
        return moment.astimezone(ZoneInfo(self.app.timezone) if self.app.timezone else None).date().isoformat()

    def _task_fields(self, item: Item, meeting: Meeting) -> dict[str, str]:
        props = {"hs_task_subject": " ".join(item.title.split()), "hs_task_body": rich_text(item_markdown(item, meeting))}
        if item.priority:
            props["hs_task_priority"] = PRIORITY[item.priority]
        return props

    def _log_fields(self, kind: str, meeting: Meeting, notes_markdown: str) -> dict[str, str]:
        start = meeting.started_at
        body = rich_text(notes_markdown)
        if kind == "notes":
            return {"hs_timestamp": _utc(start), "hs_note_body": body}
        end = meeting.ended_at or start + timedelta(seconds=meeting.segments[-1].t_end if meeting.segments else 0)
        local = start.astimezone(ZoneInfo(self.app.timezone) if self.app.timezone else None)
        label = "Réunion" if (meeting.language or "").lower().startswith("fr") else "Meeting"
        return {
            "hs_timestamp": _utc(start),
            "hs_meeting_title": " ".join((meeting.title or f"{label} {local:%Y-%m-%d %H:%M}").split()),
            "hs_meeting_body": body,
            "hs_meeting_start_time": _utc(start),
            "hs_meeting_end_time": _utc(max(end, start)),
            "hs_meeting_outcome": "COMPLETED",
        }

    async def _create(self, kind: str, props: dict[str, str], associations: list[dict[str, Any]]) -> str:
        resp = await self._api("POST", f"{OBJECTS}/{kind}", json={"properties": props, "associations": associations})
        record_id = str(resp.json().get("id") or "")
        if not record_id:  # the record may exist: never retry this create
            raise SinkError(f"hubspot: created {kind} but got no id back: {resp.text[:200]}", retryable=False)
        return f"{REF_PREFIX}{kind}/{record_id}"

    # -------------------------------------------------------------- sink API

    def accepts(self, item: Item, default_min_conf: float) -> bool:
        return super().accepts(item, default_min_conf) and item.status != ItemStatus.CANCELLED

    async def push_item(self, item: Item, meeting: Meeting) -> str:
        due = _iso_date(item.due_date)
        if due is None:  # HubSpot requires a due date
            due = today(self.app.timezone) + timedelta(days=1)
            while due.weekday() >= 5:
                due += timedelta(days=1)
        props = self._task_fields(item, meeting) | {
            "hs_timestamp": _utc(self._local(due)),
            "hs_task_type": "TODO",
            "hs_task_status": "COMPLETED" if item.status == ItemStatus.DONE else "NOT_STARTED",
        }
        if item.is_mine(self.app.user_name) and (owner := await self._owner_record()):
            props["hubspot_owner_id"] = str(owner["id"])
        return await self._create("tasks", props, await self._associations("tasks", meeting, item))

    async def update_item(self, item: Item, meeting: Meeting, ref: str) -> None:
        path = f"{OBJECTS}/tasks/{object_id(ref, 'tasks')}"
        try:
            current = (await self._api("GET", path, params={"properties": "hs_task_status,hs_timestamp"})).json()
        except HubSpotError as exc:
            if exc.status != 404:
                raise
            log.info("hubspot: the task for %s is no longer in HubSpot; leaving it deleted", item.id)
            return
        props = current.get("properties") or {}
        completed = props.get("hs_task_status") == "COMPLETED"
        if item.dismissed or item.status == ItemStatus.CANCELLED:
            if not completed:
                await self._api("DELETE", path)  # archive: to the recycle bin, restorable for 90 days
            return
        fields = self._task_fields(item, meeting)
        if (due := _iso_date(item.due_date)) and self._local_day(props.get("hs_timestamp")) != due.isoformat():
            fields["hs_timestamp"] = _utc(self._local(due))
        if item.status == ItemStatus.DONE and not completed:
            fields["hs_task_status"] = "COMPLETED"
        await self._api("PATCH", path, json={"properties": fields})

    async def push_notes(self, meeting: Meeting, notes_markdown: str) -> str | None:
        kind = "meetings" if self.cfg.log_meeting else "notes"
        props = self._log_fields(kind, meeting, notes_markdown)
        previous = meeting.notes_ref.get(self.name)
        if previous and previous.startswith(f"{REF_PREFIX}{kind}/"):
            try:
                await self._api("PATCH", f"{OBJECTS}/{kind}/{object_id(previous, kind)}", json={"properties": props})
            except HubSpotError as exc:
                if exc.status != 404:
                    raise
                log.info("hubspot: the logged meeting %s was deleted in HubSpot; leaving it deleted", previous)
            return previous
        associations = await self._associations(kind, meeting)
        if not associations and not self.cfg.log_unmatched:
            log.info("hubspot: nobody in meeting %s matched a HubSpot contact or company; not logging it", meeting.id)
            return None
        if owner := await self._owner_record():
            props["hubspot_owner_id"] = str(owner["id"])
        return await self._create(kind, props, associations)

    async def check(self) -> str:
        parts: list[str] = []
        try:
            account = (await self._api("GET", f"/account-info/{VERSION}/details")).json()
            parts.append(f"portal {account.get('portalId')} on {account.get('uiDomain')}")
        except HubSpotError as exc:
            if exc.status != 403:  # some API rollouts want the basic "oauth" scope here; it's only informational
                raise
        self._owner_known = False  # always a live lookup
        owner = await self._owner_record()
        # read scopes; the write scopes show on the first push
        for object_type in ("contacts", "companies") if self.cfg.match != "off" else ("contacts",):
            await self._api("GET", f"{OBJECTS}/{object_type}", params={"limit": 1})
        if owner:
            parts.append(f"tasks assigned to {owner.get('email')} (owner {owner['id']})")
        elif self.cfg.owner_email:
            parts.append(f"tasks unassigned: no HubSpot user has the e-mail {self.cfg.owner_email}")
        else:
            parts.append("tasks unassigned: set owner_email to your HubSpot login e-mail")
        logged_as = "Meetings" if self.cfg.log_meeting else "Notes"
        if not self.cfg.notes:
            pass
        elif self.cfg.match != "off":
            parts.append(f"meetings logged as {logged_as} on matched contacts")
        elif self.cfg.log_unmatched:
            parts.append(f"meetings logged as {logged_as} on no contact (match = off)")
        else:
            parts.append("no contact matching, so meetings aren't logged")
        return " · ".join(parts)


def _error(resp: httpx.Response, method: str, path: str, token_env: str) -> HubSpotError:
    """HubSpot's error body: ``{"category", "message", "correlationId", "errors": [{"context": …}]}``."""
    try:
        body = resp.json()
    except ValueError:
        body = None
    body = body if isinstance(body, dict) else {}
    details = [c for e in body.get("errors") or [] if isinstance(e, dict) and isinstance(c := e.get("context"), dict)]
    hint = None
    if resp.status_code == 401:
        hint = f"{token_env} is wrong, revoked or rotated: copy the service key again (see voicestream/sinks/hubspot.py)"
    elif resp.status_code == 403:
        missing = sorted({s for c in details for s in (c.get("requiredGranularScopes") or c.get("requiredScopes") or [])})
        hint = f"give the key the scope(s) {', '.join(missing or SCOPES)} (open it in HubSpot → Add new scope → Update)"
    elif resp.status_code == 404:
        hint = "it no longer exists in HubSpot"
    category = f" {body['category']}" if body.get("category") else ""
    message = body.get("message") or resp.text[:300]
    return HubSpotError(
        f"hubspot: HTTP {resp.status_code}{category} on {method} {path}: {message}" + (f" — {hint}" if hint else ""),
        status=resp.status_code,
    )


def object_id(ref: str, kind: str) -> str:
    """The numeric id in a ``hubspot:<kind>/<id>`` ref (anything else never reaches a URL)."""
    oid = ref.removeprefix(f"{REF_PREFIX}{kind}/")
    if oid == ref or not (oid.isascii() and oid.isdigit()):
        raise SinkError(f"hubspot: not a HubSpot {kind} reference: {ref!r}", retryable=False)
    return oid


def rich_text(markdown: str) -> str:
    """Markdown → escaped HTML within :data:`BODY_MAX` (cut at the end when too long)."""
    html = markdown_to_html(markdown)
    while len(html) > BODY_MAX:
        markdown = markdown[: len(markdown) * 3 // 4]
        html = markdown_to_html(markdown + "\n\n…")
    return html


def plain(text: str) -> str:
    """``text`` without accents ("Léa" → "Lea")."""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def fold(text: str) -> str:
    """Comparison key: no accents, no case, single spaces."""
    return " ".join(plain(text).casefold().split())


def _unique_contact(hits: list[dict[str, Any]] | None) -> tuple[str | None, str | None]:
    if not hits or len(hits) != 1:
        return None, None
    return str(hits[0]["id"]), _prop(hits[0], "associatedcompanyid") or None


def _prop(record: dict[str, Any], name: str) -> str:
    return str((record.get("properties") or {}).get(name) or "")


def _eq(prop: str, value: str) -> dict[str, str]:
    return {"propertyName": prop, "operator": "EQ", "value": value}


def _association(record_id: str, type_id: int) -> dict[str, Any]:
    return {"to": {"id": record_id}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": type_id}]}


def _utc(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _iso_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None
