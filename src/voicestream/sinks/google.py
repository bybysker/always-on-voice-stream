"""Google sink: your action items become Google Calendar reminders, and each meeting's
notes become a Google Doc, an e-mail recap to yourself and a Gmail draft of the
follow-up for the other participants.

It talks to a small Apps Script web app that runs as you (``google_bridge.gs``, shipped
inside the package), not to the Google APIs. That avoids a Google Cloud project, an
OAuth client and refresh tokens that expire: the script uses your own Google
permissions, and voicestream only knows its URL and a shared secret.

Setup (once, about five minutes):

1. Go to https://script.google.com → **New project**. Name it "voicestream bridge".
2. Copy the script to the clipboard: ``voicestream google-bridge | pbcopy``.
   Select everything in the editor, paste over it, and save (⌘S).
3. Make a secret: ``voicestream google-bridge --secret``. In
   the editor's left bar click **Project Settings** (⚙), scroll to **Script Properties**
   → **Add script property**, Property ``SECRET``, Value the secret →
   **Save script properties**.
4. **Deploy** → **New deployment** → **Select type** (⚙) → **Web app**. Execute as:
   **Me**; Who has access: **Anyone** → **Deploy**.
5. **Authorize access** → pick your account. Google warns "Google hasn't verified this
   app" because it's your own unreviewed script: **Advanced** → **Go to voicestream
   bridge (unsafe)** → **Allow**. It asks for Calendar, Gmail, Docs and Drive.
6. Copy the **Web app URL** (it ends in ``/exec``) and add to ``~/.voicestream/.env``::

       VOICESTREAM_GOOGLE_URL=https://script.google.com/macros/s/…/exec
       VOICESTREAM_GOOGLE_SECRET=<the secret from step 3>

7. In ``voicestream.toml`` add ``[sinks.google]`` with ``enabled = true``, then run
   ``voicestream doctor``. It should show ``google: bridge v1 runs as you@gmail.com …``.

When an update ships a new ``google_bridge.gs`` (``doctor`` tells you), paste it over
the old code, then **Deploy** → **Manage deployments** → ✎ **Edit** → Version: **New
version** → **Deploy**. That keeps the URL. A *new* deployment would get a new URL.

"Who has access: Anyone" means anyone holding the URL can reach the script, so it
refuses every request without the secret, and refuses to run at all while ``SECRET``
is unset or shorter than 16 characters. Web apps can't read request headers, so the
secret travels in the JSON body. Even with the secret, the script only edits events
and Docs it created itself, e-mails nobody but you, and never sends a draft. It
answers each POST with a redirect to a one-time URL holding its output. voicestream
fetches that with a GET and never replays the POST, which would run the script a
second time.

What it does:

* **Reminders** (``kinds = ["action"]``, ``owners = "mine"``): one timed event per action
  item of yours on its due date at ``event_time`` (your Mac's time zone, or ``timezone``
  from the config), with a pop-up 10 minutes before. Events show you as free. An item
  due earlier than now is put at the next quarter hour. Undated items are skipped,
  unless ``undated = "next_workday"`` (a manual push from the dashboard always uses the
  next workday). Edits rename the event. A new due date moves it, but a time you picked
  in Calendar is kept otherwise. Done items get a "✓ " prefix. Cancelled, dismissed
  and reassigned items have their event deleted: it's your reminder, not a record. An
  event you deleted yourself is left deleted. ``calendar = false`` turns reminders off
  (same as ``kinds = []``).
* **Notes** at the end of the meeting: a Google Doc "<YYYY-MM-DD> <title>" in the Drive
  folder ``docs_folder`` (created if missing), an HTML recap e-mailed only to your own
  address (``recap_to_self``), and the model's follow-up e-mail saved as a Gmail draft,
  never sent (``followup_draft``). Pushing the notes again (``voicestream push``)
  rewrites the same Doc, replacing any edits you made in it, and doesn't e-mail again.

Each create carries a key, and the script de-duplicates on it for six hours. A retry
after a lost answer (Apps Script can take a while) returns the event or Doc the first
attempt made instead of adding a second one. That's what makes resending POSTs safe.

Consumer (@gmail.com) quotas, which reset 24 hours after first use: 100 e-mail
recipients/day (a recap uses one), 5,000 calendar events/day, 250 new Docs/day, and
6 minutes per script run.

Ref formats: ``gcal:<calendar id>/<event id>`` for items, the Doc URL for notes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, ClassVar, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator

from ..models import Item, ItemStatus, Meeting
from ..render import markdown_to_html
from .base import Sink, SinkConfig, SinkError, env_secret
from .http import HttpSinkMixin
from .webhook import redacted

log = logging.getLogger(__name__)

BRIDGE_VERSION = 1
"""Must equal ``VERSION`` in google_bridge.gs."""
BRIDGE_SOURCE = Path(__file__).resolve().parent.parent / "google_bridge.gs"
URL_PREFIX = "https://script.google.com/"
REF_PREFIX = "gcal:"
POPUP_MINUTES = 10
"""Calendar accepts pop-ups 5 minutes to 4 weeks before an event."""
TITLE_MAX = 250
DESCRIPTION_MAX = 8000
UNDATED = "undated"
"""The due-date tag of an event made for an undated item."""
_REDIRECTS = (301, 302, 303)
_DOC_ID = re.compile(r"/document/d/([\w-]+)")
_EMAIL = re.compile(r"[^\s@,;:<>()\[\]\"']+@[^\s@,;:<>()\[\]\"']+\.[^\s@,;:<>()\[\]\"'.]+")
_TEXT = {
    "en": {
        "meeting": "Meeting", "notes": "Notes: ", "open_doc": "Open in Google Docs", "owner": "Owner: ",
        "due": "Due: ", "priority": "Priority: ", "agent": "Agent result:", "from": "From: ",
        "high": "high", "medium": "medium", "low": "low",
    },
    "fr": {
        "meeting": "Réunion", "notes": "Notes : ", "open_doc": "Ouvrir dans Google Docs", "owner": "Responsable : ",
        "due": "Échéance : ", "priority": "Priorité : ", "agent": "Résultat de l'agent :", "from": "Réunion : ",
        "high": "haute", "medium": "moyenne", "low": "basse",
    },
}  # fmt: skip


class GoogleConfig(SinkConfig):
    owners: Literal["all", "mine"] = "mine"
    url_env: str = "VOICESTREAM_GOOGLE_URL"
    """Env var holding the web app's ``/exec`` URL."""
    secret_env: str = "VOICESTREAM_GOOGLE_SECRET"
    """Env var holding the secret, equal to the script's ``SECRET`` property."""
    calendar: bool = True
    """Calendar reminders for action items. ``false`` = notes only (sets ``kinds = []``)."""
    calendar_id: str = "primary"
    """``primary`` or a calendar id from Google Calendar → Settings → Integrate calendar."""
    event_time: time = time(9, 0)
    event_minutes: int = Field(default=30, ge=5, le=480)
    undated: Literal["skip", "next_workday"] = "skip"
    docs: bool = True
    """Write the notes to a Google Doc."""
    docs_folder: str = "Meeting notes"
    recap_to_self: bool = True
    """E-mail the notes to your own address."""
    followup_draft: bool = True
    """Save the follow-up e-mail for the participants as a Gmail draft (never sent)."""

    @model_validator(mode="after")
    def _calendar_off(self) -> GoogleConfig:
        if not self.calendar:
            self.kinds = []
        return self


class GoogleSink(HttpSinkMixin, Sink):
    name = "google"
    Config: ClassVar[type[SinkConfig]] = GoogleConfig
    supports_update = True
    supports_notes = True
    timeout_s = 120.0  # the script runs while the POST waits; creating a Doc can take a while
    cfg: GoogleConfig

    async def _call(self, action: str, **fields: Any) -> dict[str, Any]:
        url = env_secret(self.cfg, "url_env", "VOICESTREAM_GOOGLE_URL")
        if not url.startswith(URL_PREFIX):
            raise SinkError(
                f"{self.cfg.url_env} must be the Apps Script web app URL (https://script.google.com/macros/s/…/exec)",
                retryable=False,
            )
        # ASCII JSON (accents as \u escapes), so no charset can garble French text on the way
        body = json.dumps({"secret": env_secret(self.cfg, "secret_env", "VOICESTREAM_GOOGLE_SECRET"), "action": action, **fields})
        location = ""
        try:
            # resending is safe: creates carry a key the bridge de-duplicates on, the rest set absolute values
            resp = await self.request(
                "POST", url, content=body, headers={"Content-Type": "application/json"}, write_safe=True, ok=(200, *_REDIRECTS)
            )
            if resp.status_code in _REDIRECTS:
                # the script has run and its output waits at a one-time URL: GET it (a POST would run it again)
                location = resp.headers.get("location", "")
                if urlsplit(location).hostname == "accounts.google.com":
                    raise SinkError("google: the web app asks for a Google sign-in; deploy it with Who has access: Anyone", retryable=False)
                if not location.startswith("https://"):
                    raise SinkError(f"google: HTTP {resp.status_code} without a usable Location", retryable=False)
                resp = await self.request("GET", location)
        except SinkError as exc:
            for hidden in filter(None, (url, location)):
                exc = redacted(exc, hidden)
            raise exc from None
        try:
            data = resp.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            text = " ".join(re.sub(r"<[^>]*>", " ", resp.text).split())[:160]
            raise SinkError(
                f"google: the bridge answered {resp.headers.get('content-type', '?')} instead of JSON ({text}). Is "
                f"{self.cfg.url_env} the /exec URL of a deployment with Execute as: Me and Who has access: Anyone? "
                "See the setup steps in voicestream/sinks/google.py",
                retryable=False,
            )
        if not data.get("ok"):
            raise SinkError(f"google: {data.get('error') or 'bridge error'}", retryable=bool(data.get("retryable")))
        return data

    # ------------------------------------------------------------- reminders

    def _local(self, day: date, at: time) -> datetime:
        naive = datetime.combine(day, at)
        return naive.replace(tzinfo=ZoneInfo(self.app.timezone)) if self.app.timezone else naive.astimezone()

    def _event(self, item: Item, meeting: Meeting) -> dict[str, Any]:
        now = _now().astimezone(ZoneInfo(self.app.timezone) if self.app.timezone else None)
        due = _iso_date(item.due_date)
        start = self._local(due or next_workday(now.date()), self.cfg.event_time)
        if start < now:
            start = now.replace(second=0, microsecond=0) + timedelta(minutes=15 - now.minute % 15)
        title = _oneline(item.title)
        return {
            "title": _clip(f"✓ {title}" if item.status == ItemStatus.DONE else title, TITLE_MAX),
            "description": self._description(item, meeting),
            "start": start.isoformat(timespec="seconds"),
            "end": (start + timedelta(minutes=self.cfg.event_minutes)).isoformat(timespec="seconds"),
            "day": item.due_date if due else UNDATED,
        }

    def _description(self, item: Item, meeting: Meeting) -> str:
        """Plain text. Calendar renders HTML in descriptions, so no ``<``/``>`` from the transcript gets through."""
        t = _TEXT[_lang(meeting)]
        lines = [item.detail, ""] if item.detail else []
        meta = []
        if item.owner:
            meta.append(t["owner"] + item.owner)
        if item.due_date or item.due_text:
            due = item.due_date or ""
            if item.due_text and item.due_text != item.due_date:
                due = f"{due} ({item.due_text})" if due else item.due_text
            meta.append(t["due"] + due)
        if item.priority:
            meta.append(t["priority"] + t[item.priority])
        if meta:
            lines.append(" · ".join(meta))
        if item.evidence:
            lines.append(f"“{item.evidence}”")
        if item.agent and item.agent.result_summary:
            lines += ["", t["agent"], item.agent.result_summary]
        started = meeting.started_at.astimezone(ZoneInfo(self.app.timezone) if self.app.timezone else None)
        lines += ["", f"{t['from']}{meeting.title or t['meeting']}, {started:%Y-%m-%d %H:%M}"]
        text = "\n".join(lines).strip().replace("<", "‹").replace(">", "›")
        return _clip(text, DESCRIPTION_MAX)

    def _removed(self, item: Item) -> bool:
        return (
            item.dismissed
            or item.status == ItemStatus.CANCELLED
            or (self.cfg.owners == "mine" and not item.is_mine(self.app.user_name))
        )

    # -------------------------------------------------------------- sink API

    def accepts(self, item: Item, default_min_conf: float) -> bool:
        return (
            super().accepts(item, default_min_conf)
            and item.status == ItemStatus.OPEN
            and (_iso_date(item.due_date) is not None or self.cfg.undated == "next_workday")
        )

    async def push_item(self, item: Item, meeting: Meeting) -> str:
        data = await self._call(
            "task.create",
            key=_key("task", meeting.id, meeting.started_at.isoformat(), item.id),
            calendar_id=self.cfg.calendar_id,
            popup_minutes=POPUP_MINUTES,
            **self._event(item, meeting),
        )
        return f"{REF_PREFIX}{data['calendar_id']}/{data['event_id']}"

    async def update_item(self, item: Item, meeting: Meeting, ref: str) -> None:
        calendar_id, _, event_id = ref.removeprefix(REF_PREFIX).partition("/")
        fields: dict[str, Any] = {"calendar_id": calendar_id, "event_id": event_id, "remove": self._removed(item)}
        if not fields["remove"]:
            fields |= self._event(item, meeting)
        if (await self._call("task.update", **fields)).get("gone"):
            log.info("google: the reminder for %s is no longer in Calendar; leaving it deleted", item.id)

    async def push_notes(self, meeting: Meeting, notes_markdown: str) -> str | None:
        t = _TEXT[_lang(meeting)]
        previous = meeting.notes_ref.get(self.name)
        title = _oneline(meeting.title or t["meeting"])
        started = meeting.started_at.astimezone(ZoneInfo(self.app.timezone) if self.app.timezone else None)
        doc = None
        if self.cfg.docs:
            doc_id = m.group(1) if previous and (m := _DOC_ID.search(previous)) else None
            doc = {
                "id": doc_id,
                "title": _clip(f"{started:%Y-%m-%d} {title}", TITLE_MAX),
                "folder": self.cfg.docs_folder,
                "markdown": notes_markdown,
            }
        recap = draft = None
        if previous is None and self.cfg.recap_to_self:  # e-mails go out once, on the first push
            recap = {
                "subject": _clip(t["notes"] + title, TITLE_MAX),
                "html": markdown_to_html(notes_markdown),
                "text": notes_markdown,
                "link_label": t["open_doc"],
            }
        if previous is None and self.cfg.followup_draft and (followup := meeting.followup_email):
            draft = {
                "to": _addresses(followup.to),
                "subject": _clip(_oneline(followup.subject), TITLE_MAX),
                "html": markdown_to_html(followup.body_markdown),
                "text": followup.body_markdown,
            }
        if not (doc or recap or draft):
            return previous
        key = _key("notes", meeting.id, meeting.started_at.isoformat())
        data = await self._call("meeting.notes", key=key, doc=doc, recap=recap, draft=draft)
        for warning in data.get("warnings") or []:
            log.warning("google: %s", warning)
        return data.get("doc_url") or previous or f"google:{meeting.id}"

    async def check(self) -> str:
        data = await self._call("ping")
        if data.get("version") != BRIDGE_VERSION:
            raise SinkError(
                f"google: the deployed bridge is v{data.get('version')} but this voicestream needs v{BRIDGE_VERSION}: paste "
                "the output of `voicestream google-bridge` into the script, then Deploy → Manage deployments → Edit → "
                "Version: New version → Deploy",
                retryable=False,
            )
        parts = [f"bridge v{BRIDGE_VERSION} runs as {data.get('email')}"]
        if self.cfg.kinds:
            undated = "next workday" if self.cfg.undated == "next_workday" else "skipped"
            parts.append(f"reminders at {self.cfg.event_time:%H:%M} in calendar {self.cfg.calendar_id} (undated: {undated})")
        if self.cfg.notes:
            if self.cfg.docs:
                parts.append(f"notes in Drive folder “{self.cfg.docs_folder}”")
            if self.cfg.recap_to_self:
                parts.append("recap e-mailed to you")
            if self.cfg.followup_draft:
                parts.append("follow-up saved as a Gmail draft")
        return " · ".join(parts)


def next_workday(day: date) -> date:
    day += timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def _now() -> datetime:
    return datetime.now(UTC)


def _lang(meeting: Meeting) -> str:
    return "fr" if (meeting.language or "").lower().startswith("fr") else "en"


def _iso_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _key(*parts: str) -> str:
    return "vs:" + hashlib.sha256("/".join(parts).encode()).hexdigest()[:40]


def _addresses(values: list[str]) -> list[str]:
    """The recipients that are plain e-mail addresses (the model may write names or worse)."""
    good = [v.strip() for v in values if _EMAIL.fullmatch(v.strip())]
    if len(good) < len(values):
        log.info("google: left %d malformed recipient(s) out of the follow-up draft", len(values) - len(good))
    return good


def _oneline(text: str) -> str:
    return " ".join(text.split())


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
