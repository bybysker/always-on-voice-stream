"""Apple Reminders sink: your own action items become reminders on your Mac.

It drives Reminders.app with a JavaScript for Automation (JXA) script run by
``osascript``, so the reminders sync through iCloud to your iPhone like any other.
The script is a constant. The item's text goes to it as JSON on stdin, ASCII-escaped
and decoded explicitly as UTF-8, so a title is never parsed as code and accents
survive whatever the locale is. Due dates are built from numbers
(``new Date(y, m - 1, d, hh, mm)`` on the Mac's clock), never by parsing a date
string, which AppleScript does by locale.

Reminders is a personal list, so the default is ``owners = "mine"``. New reminders
go to ``list_name`` (created when missing), due at ``due_time`` on the item's due
date. Edits update the title, notes, priority and due date. A due date is never
cleared, and a completed reminder is never reopened, since you may have changed it
in Reminders yourself. Done items complete the reminder; cancelled or dismissed
ones delete it. Meeting notes aren't supported.

Ref format: the reminder's scripting id, ``x-apple-reminder://<UUID>``.

The first time, macOS asks whether the app that runs voicestream (Terminal, say)
may control Reminders. Run ``voicestream doctor`` from that app and click Allow.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from typing import Annotated, Any, ClassVar, Literal

from pydantic import Field

from ..models import Item, ItemStatus, Meeting
from . import _proc
from .base import Sink, SinkConfig, SinkError

log = logging.getLogger(__name__)

OSASCRIPT = "/usr/bin/osascript"
TRANSIENT = {-600, -609, -1712}
"""Apple event errors worth retrying: app not running, connection invalid, timed out."""
NOT_PERMITTED = -1743
PERMISSION_HELP = (
    "macOS didn't let voicestream control Reminders (error -1743). Open System Settings → "
    "Privacy & Security → Automation and turn on Reminders under the app that runs voicestream "
    "(Terminal, iTerm…). If that app isn't listed, run `tccutil reset AppleEvents <its bundle id>` "
    "(Terminal: com.apple.Terminal), then `voicestream doctor` from it and click Allow."
)

SCRIPT = r"""
ObjC.import("Foundation");

function run() {
  const input = $.NSFileHandle.fileHandleWithStandardInput.readDataToEndOfFile;
  const req = JSON.parse($.NSString.alloc.initWithDataEncoding(input, $.NSUTF8StringEncoding).js);
  const out = handle(Application("Reminders"), req);
  return JSON.stringify(out).replace(/[^\x20-\x7e]/g, (c) => "\\u" + ("000" + c.charCodeAt(0).toString(16)).slice(-4));
}

function handle(app, req) {
  const list = app.lists.byName(req.list);
  if (req.op === "check") return { list: list.exists(), lists: app.lists.length };
  if (req.op === "create") {
    if (!list.exists()) app.lists.push(app.List({ name: req.list }));
    const r = app.Reminder({ name: req.name, body: req.body });
    list.reminders.push(r);
    apply(r, req);
    return { id: r.id() };
  }
  const r = app.reminders.byId(req.id);
  if (!r.exists()) return { missing: true };
  if (req.op === "delete") {
    app.delete(r);
    return { deleted: true };
  }
  r.name = req.name;
  r.body = req.body;
  apply(r, req);
  return { id: r.id() };
}

function apply(r, req) {
  if (req.priority !== null) r.priority = req.priority;
  if (req.due) {
    const [y, m, d, hh, mm] = req.due;
    if (hh === undefined) {
      r.alldayDueDate = new Date(y, m - 1, d);
    } else {
      const due = new Date(y, m - 1, d, hh, mm);
      r.dueDate = due;
      r.remindMeDate = due;
    }
  }
  if (req.completed) r.completed = true;
}
"""


class RemindersConfig(SinkConfig):
    owners: Literal["all", "mine"] = "mine"
    list_name: str = Field(default="Meetings", min_length=1)
    """Reminders list for new reminders; created in your default account if missing."""
    due_time: str | None = Field(default="09:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    """Time of day (HH:MM, Mac's clock) for items with a due date; also when you're
    alerted. ``None`` = all-day reminders, without an alert."""
    priority: dict[Literal["low", "medium", "high"], Annotated[int, Field(ge=0, le=9)]] = Field(
        default_factory=lambda: {"high": 1, "medium": 5, "low": 9}
    )
    """Reminders priorities: 1 = high, 5 = medium, 9 = low, 0 = none."""
    timeout_s: float = 60.0


class RemindersSink(Sink):
    name = "reminders"
    Config: ClassVar[type[SinkConfig]] = RemindersConfig
    supports_update = True
    cfg: RemindersConfig

    def __init__(self, cfg: RemindersConfig, app) -> None:  # type: ignore[override]
        super().__init__(cfg, app)
        self.run: _proc.Runner = _proc.run

    def accepts(self, item: Item, default_min_conf: float) -> bool:
        # cancelled before it was ever pushed: nothing to remind you of
        return item.status != ItemStatus.CANCELLED and super().accepts(item, default_min_conf)

    async def _osa(self, request: dict[str, Any]) -> dict[str, Any]:
        _proc.require_macos("Apple Reminders")
        res = await self.run(
            [OSASCRIPT, "-l", "JavaScript", "-e", SCRIPT],
            stdin=json.dumps(request).encode("ascii"),
            timeout=self.cfg.timeout_s,
        )
        if res.returncode != 0:
            raise osa_error(res.stderr)
        try:
            out = json.loads(res.stdout)
        except ValueError:
            out = None
        if not isinstance(out, dict):
            raise SinkError(f"reminders: unexpected reply from osascript: {_proc.tail(res.stdout, 200)!r}", retryable=False)
        return out

    def _request(self, op: str, item: Item, meeting: Meeting, ref: str | None = None) -> dict[str, Any]:
        return {
            "op": op,
            "list": self.cfg.list_name,
            "id": ref,
            "name": re.sub(r"[\x00-\x1f\x7f\s]+", " ", item.title).strip(),
            "body": self._notes(item, meeting),
            "priority": self.cfg.priority.get(item.priority) if item.priority else None,
            "due": self._due(item),
            "completed": item.status == ItemStatus.DONE,
        }

    def _due(self, item: Item) -> list[int] | None:
        if not item.due_date:
            return None
        try:
            d = date.fromisoformat(item.due_date)
        except ValueError:
            log.warning("reminders: ignoring unparseable due date %r on %s", item.due_date, item.id)
            return None
        if self.cfg.due_time is None:
            return [d.year, d.month, d.day]
        hh, mm = self.cfg.due_time.split(":")
        return [d.year, d.month, d.day, int(hh), int(mm)]

    def _notes(self, item: Item, meeting: Meeting) -> str:
        """Plain text: Reminders shows Markdown as typed."""
        parts = [item.detail] if item.detail else []
        if item.owner and not item.is_mine(self.app.user_name):
            parts.append(f"@{item.owner}")
        if item.due_text and not item.due_date:
            parts.append(f"⏰ {item.due_text}")
        if item.evidence:
            parts.append(f"“{item.evidence}”")
        if item.agent and item.agent.result_summary:
            parts.append(f"🤖 {item.agent.result_summary}")
        started = f"{meeting.started_at.astimezone():%Y-%m-%d %H:%M}"
        parts.append(" · ".join(["voicestream", meeting.title, started] if meeting.title else ["voicestream", started]))
        return "\n\n".join(parts)

    # -------------------------------------------------------------- sink API

    async def push_item(self, item: Item, meeting: Meeting) -> str:
        out = await self._osa(self._request("create", item, meeting))
        rid = out.get("id")
        if not isinstance(rid, str) or not rid:
            raise SinkError(f"reminders: created a reminder but got no id back: {out}", retryable=False)
        return rid

    async def update_item(self, item: Item, meeting: Meeting, ref: str) -> None:
        op = "delete" if item.dismissed or item.status == ItemStatus.CANCELLED else "update"
        if (await self._osa(self._request(op, item, meeting, ref))).get("missing"):
            log.info("reminders: %s is gone (deleted in Reminders?); not recreating it", ref)

    async def check(self) -> str:
        out = await self._osa({"op": "check", "list": self.cfg.list_name})
        where = f"“{self.cfg.list_name}”" + ("" if out.get("list") else " (created on first push)")
        mine = " · only items you own" if self.cfg.owners == "mine" else ""
        return f"Reminders reachable ({out.get('lists')} lists) · new reminders go to {where}{mine}"


def osa_error(stderr: str) -> SinkError:
    """Map osascript's ``execution error: … (-NNNN)`` to a :class:`SinkError`."""
    m = re.search(r"\((-\d+)\)\s*$", stderr.strip())
    code = int(m.group(1)) if m else None
    if code == NOT_PERMITTED:
        return SinkError(PERMISSION_HELP, retryable=False)
    return SinkError(f"reminders: osascript failed: {_proc.tail(stderr)}", retryable=code in TRANSIENT)
