"""Google sink against a fake Apps Script web app (httpx.MockTransport), the e-mail
Markdown renderer, and (when node is installed) the bridge's own JS tests."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from voicestream.config import Config
from voicestream.dispatch import Dispatcher
from voicestream.models import (
    AgentRun,
    AgentStatus,
    FollowUpEmail,
    Item,
    ItemKind,
    ItemStatus,
    Meeting,
)
from voicestream.render import markdown_to_html
from voicestream.sinks import google
from voicestream.sinks.base import SinkError, build_sinks, sink_class
from voicestream.sinks.google import (
    BRIDGE_SOURCE,
    BRIDGE_VERSION,
    GoogleConfig,
    GoogleSink,
    next_workday,
)

EXEC = "https://script.google.com/macros/s/AKfycbDEPLOYMENT/exec"
ECHO = "https://script.googleusercontent.com/macros/echo?user_content_key=ONE-TIME&lib=LIB"
SECRET = "correct-horse-battery-staple"
ROOT = Path(__file__).resolve().parents[1]

Reply = dict[str, Any] | httpx.Response


class FakeBridge:
    """The web app: a POST runs ``handler(body)`` and redirects (302) to its output; the GET returns it."""

    def __init__(self, sink: GoogleSink, handler: Callable[[dict[str, Any]], Reply] | None = None) -> None:
        self.handler = handler or (lambda body: {"ok": True})
        self.requests: list[httpx.Request] = []
        self.bodies: list[dict[str, Any]] = []
        self._pending: Reply | None = None
        sink._client = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "POST" and str(request.url) == EXEC:
            body = json.loads(request.content)
            self.bodies.append(body)
            reply = self.handler(body)
            if isinstance(reply, httpx.Response):
                return reply
            self._pending = reply
            return httpx.Response(302, headers={"Location": ECHO}, text="<HTML>Moved Temporarily</HTML>")
        if request.method == "GET" and str(request.url) == ECHO and self._pending is not None:
            reply, self._pending = self._pending, None
            return httpx.Response(200, json=reply)
        return httpx.Response(405, text="Method Not Allowed")

    @property
    def last(self) -> dict[str, Any]:
        return self.bodies[-1]


@pytest.fixture
def app(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("VOICESTREAM_GOOGLE_URL", EXEC)
    monkeypatch.setenv("VOICESTREAM_GOOGLE_SECRET", SECRET)
    cfg.timezone = "Europe/Amsterdam"
    set_now(monkeypatch, "2026-10-15T08:00:00+02:00")  # Thursday morning
    return cfg


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("voicestream.sinks.http.asyncio.sleep", fake_sleep)
    return slept


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """A sink without a FakeBridge must fail loudly, never reach the network."""

    def refuse(self: GoogleSink) -> httpx.AsyncClient:
        raise AssertionError("GoogleSink used a real HTTP client; attach a FakeBridge")

    monkeypatch.setattr(GoogleSink, "client", lambda self: self._client or refuse(self))


def set_now(monkeypatch: pytest.MonkeyPatch, iso: str) -> None:
    monkeypatch.setattr(google, "_now", lambda: datetime.fromisoformat(iso).astimezone(UTC))


def make_sink(app: Config, **cfg: Any) -> GoogleSink:
    return GoogleSink(GoogleConfig(enabled=True, **cfg), app)


def created(body: dict[str, Any]) -> Reply:
    return {"ok": True, "calendar_id": "alex@gmail.com", "event_id": "ev1@google.com"}


def action(title: str = "Envoyer le devis à Léa", **kw: Any) -> Item:
    return Item(**{"id": "A1", "kind": ItemKind.ACTION, "title": title, "owner": "Alex", "due_date": "2026-10-16", **kw})


@pytest.fixture
def fr_meeting() -> Meeting:
    return Meeting(
        id="20261014-080000",
        title="Point budget",
        language="fr",
        started_at=datetime(2026, 10, 14, 8, 0, tzinfo=UTC),
    )


# ------------------------------------------------------------------ wiring


def test_registered_and_builds_without_credentials(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VOICESTREAM_GOOGLE_URL", raising=False)
    assert sink_class("google") is GoogleSink
    cfg.sinks = {"google": {"enabled": True}}
    (sink,) = build_sinks(cfg)
    assert isinstance(sink, GoogleSink)
    assert sink.cfg.owners == "mine" and sink.cfg.kinds == ["action"]


def test_bridge_ships_with_the_package_and_versions_match() -> None:
    assert BRIDGE_SOURCE.is_file()
    assert re.search(r"^var VERSION = (\d+);", BRIDGE_SOURCE.read_text(encoding="utf-8"), re.MULTILINE).group(1) == str(BRIDGE_VERSION)


def test_config_validation() -> None:
    assert GoogleConfig(event_time="08:30").event_time.strftime("%H:%M") == "08:30"
    for bad in ({"event_time": "25:00"}, {"event_minutes": 0}, {"undated": "tomorrow"}, {"calender": True}):
        with pytest.raises(ValidationError):
            GoogleConfig(**bad)
    assert GoogleConfig(calendar=False, kinds=["action"]).kinds == []


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_bridge_javascript_suite() -> None:
    proc = subprocess.run(
        ["node", "--test", str(ROOT / "tests" / "js" / "google_bridge.test.mjs")],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]


# --------------------------------------------------------------- transport


async def test_post_once_then_read_the_answer_with_a_get(app: Config, meeting: Meeting) -> None:
    sink = make_sink(app)
    api = FakeBridge(sink, created)
    ref = await sink.push_item(action(), meeting)
    assert ref == "gcal:alex@gmail.com/ev1@google.com"
    post, get = api.requests
    assert (post.method, get.method) == ("POST", "GET")
    assert str(get.url) == ECHO and get.content == b"" and "content-length" not in get.headers
    assert post.headers["content-type"] == "application/json"
    assert post.content.isascii()  # accents travel as \u escapes
    assert api.last["title"] == "Envoyer le devis à Léa"
    assert api.last["secret"] == SECRET and SECRET.encode() not in get.content + str(get.url).encode()


async def test_bridge_errors_map_to_sink_errors(app: Config, meeting: Meeting) -> None:
    sink = make_sink(app)
    FakeBridge(sink, lambda b: {"ok": False, "error": "Service invoked too many times in a short time: calendar", "retryable": True})
    with pytest.raises(SinkError, match="too many times") as exc:
        await sink.push_item(action(), meeting)
    assert exc.value.retryable

    FakeBridge(sink, lambda b: {"ok": False, "error": "wrong secret: …", "retryable": False})
    with pytest.raises(SinkError, match="wrong secret") as exc:
        await sink.check()
    assert not exc.value.retryable


async def test_html_instead_of_json_explains_the_deployment(app: Config) -> None:
    sink = make_sink(app)
    FakeBridge(sink, lambda b: httpx.Response(200, headers={"content-type": "text/html"}, text="<html><b>Authorization is required</b></html>"))
    with pytest.raises(SinkError, match=r"text/html instead of JSON \(Authorization is required\).*Who has access: Anyone") as exc:
        await sink.check()
    assert not exc.value.retryable


async def test_a_sign_in_redirect_means_the_wrong_access_setting(app: Config) -> None:
    sink = make_sink(app)
    api = FakeBridge(
        sink, lambda b: httpx.Response(302, headers={"Location": "https://accounts.google.com/ServiceLogin?continue=x"})
    )
    with pytest.raises(SinkError, match="Who has access: Anyone"):
        await sink.check()
    assert len(api.requests) == 1


async def test_only_script_google_com_gets_the_secret(app: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    sink = make_sink(app)
    api = FakeBridge(sink)
    for url in ("http://script.google.com/macros/s/x/exec", "https://evil.example/exec", "https://script.google.com.evil.example/x"):
        monkeypatch.setenv("VOICESTREAM_GOOGLE_URL", url)
        with pytest.raises(SinkError, match="must be the Apps Script web app URL"):
            await sink.check()
    monkeypatch.delenv("VOICESTREAM_GOOGLE_URL")
    with pytest.raises(SinkError, match="VOICESTREAM_GOOGLE_URL is not set"):
        await sink.check()
    assert api.requests == []


async def test_server_errors_are_retried_and_the_url_is_redacted(app: Config, meeting: Meeting, no_sleep: list[float]) -> None:
    sink = make_sink(app)
    calls = iter([httpx.Response(503, text="busy"), {"ok": True, "calendar_id": "c", "event_id": "e"}])
    api = FakeBridge(sink, lambda b: next(calls))
    assert await sink.push_item(action(), meeting) == "gcal:c/e"
    assert len(api.bodies) == 2 and api.bodies[0]["key"] == api.bodies[1]["key"]  # the bridge de-duplicates on it

    FakeBridge(sink, lambda b: httpx.Response(500, text="boom"))
    with pytest.raises(SinkError) as exc:
        await sink.push_item(action(), meeting)
    assert "AKfycbDEPLOYMENT" not in str(exc.value) and "script.google.com/…" in str(exc.value)


# --------------------------------------------------------------- reminders


async def test_create_payload(app: Config, fr_meeting: Meeting) -> None:
    sink = make_sink(app)
    api = FakeBridge(sink, created)
    item = action(
        detail="Avec la remise de 5 %",
        due_text="demain",
        priority="high",
        evidence="Je t'envoie le devis demain",
        agent=AgentRun(prompt="p", status=AgentStatus.DONE, result_summary="Brouillon prêt"),
    )
    await sink.push_item(item, fr_meeting)
    body = api.last
    assert {k: body[k] for k in ("action", "calendar_id", "popup_minutes", "start", "end", "day", "title")} == {
        "action": "task.create",
        "calendar_id": "primary",
        "popup_minutes": 10,
        "start": "2026-10-16T09:00:00+02:00",
        "end": "2026-10-16T09:30:00+02:00",
        "day": "2026-10-16",
        "title": "Envoyer le devis à Léa",
    }
    assert body["description"] == (
        "Avec la remise de 5 %\n\n"
        "Responsable : Alex · Échéance : 2026-10-16 (demain) · Priorité : haute\n"
        "“Je t'envoie le devis demain”\n\n"
        "Résultat de l'agent :\nBrouillon prêt\n\n"
        "Réunion : Point budget, 2026-10-14 10:00"
    )
    assert body["key"].startswith("vs:") and len(body["key"]) <= 250
    await sink.push_item(item, fr_meeting)
    assert api.bodies[0]["key"] == api.bodies[1]["key"]


async def test_times_follow_the_local_zone_and_dst(app: Config, meeting: Meeting) -> None:
    sink = make_sink(app, event_time="08:15", event_minutes=45)
    api = FakeBridge(sink, created)
    await sink.push_item(action(due_date="2026-10-26"), meeting)  # the clocks went back on the 25th
    assert (api.last["start"], api.last["end"]) == ("2026-10-26T08:15:00+01:00", "2026-10-26T09:00:00+01:00")


async def test_a_reminder_that_would_be_in_the_past_goes_to_the_next_quarter_hour(
    app: Config, meeting: Meeting, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_now(monkeypatch, "2026-10-15T15:07:30+02:00")
    sink = make_sink(app)
    api = FakeBridge(sink, created)
    for due in ("2026-10-15", "2026-10-01"):
        await sink.push_item(action(due_date=due), meeting)
        assert (api.last["start"], api.last["day"]) == ("2026-10-15T15:15:00+02:00", due)


async def test_undated_items(app: Config, meeting: Meeting, monkeypatch: pytest.MonkeyPatch) -> None:
    undated = action(due_date=None, due_text="bientôt")
    assert not make_sink(app).accepts(undated, 0.5)
    assert make_sink(app, undated="next_workday").accepts(undated, 0.5)

    set_now(monkeypatch, "2026-10-16T08:00:00+02:00")  # Friday → Monday
    sink = make_sink(app)  # a manual push of an undated item lands on the next workday
    api = FakeBridge(sink, created)
    await sink.push_item(undated, meeting)
    assert (api.last["start"], api.last["day"]) == ("2026-10-19T09:00:00+02:00", "undated")
    assert [next_workday(datetime(2026, 10, d).date()).day for d in (15, 16, 17, 18)] == [16, 19, 19, 19]


def test_accepts_only_your_open_actions(app: Config) -> None:
    sink = make_sink(app)
    assert sink.accepts(action(), 0.5)
    assert sink.accepts(action(owner="me"), 0.5)
    assert not sink.accepts(action(owner="Sam"), 0.5)
    assert not sink.accepts(action(status=ItemStatus.DONE), 0.5)
    assert not sink.accepts(action(status=ItemStatus.CANCELLED), 0.5)
    assert not sink.accepts(action(dismissed=True), 0.5)
    assert not sink.accepts(action(kind=ItemKind.DECISION), 0.5)
    assert make_sink(app, owners="all").accepts(action(owner="Sam"), 0.5)


async def test_calendar_off_never_pushes_items(app: Config, meeting: Meeting) -> None:
    sink = make_sink(app, calendar=False)
    api = FakeBridge(sink)
    item = action()
    meeting.items[item.id] = item
    d = Dispatcher(app, meeting, [sink])
    assert await d.sync(require_stable=False) == 0
    assert await d.push_now(item.id) == {}
    assert api.requests == []


async def test_description_cannot_carry_markup(app: Config, meeting: Meeting) -> None:
    sink = make_sink(app)
    api = FakeBridge(sink, created)
    evil = '<a href="javascript:alert(1)">Open</a><img src=x onerror=alert(1)>'
    await sink.push_item(action(title=f"Pay {evil}\nnow", detail=evil, evidence=evil, owner="Alex <b>"), meeting)
    assert "<" not in api.last["description"] and ">" not in api.last["description"]
    assert "‹img src=x onerror=alert(1)›" in api.last["description"]
    assert "\n" not in api.last["title"] and api.last["title"].startswith("Pay <a href")  # a title is plain text in Calendar


async def test_update(app: Config, meeting: Meeting) -> None:
    sink = make_sink(app)
    api = FakeBridge(sink, lambda b: {"ok": True})
    ref = "gcal:alex@gmail.com/ev1@google.com"
    await sink.update_item(action(status=ItemStatus.DONE, due_date="2026-10-20"), meeting, ref)
    body = api.last
    assert (body["action"], body["calendar_id"], body["event_id"], body["remove"]) == ("task.update", "alex@gmail.com", "ev1@google.com", False)
    assert (body["title"], body["day"], body["start"]) == ("✓ Envoyer le devis à Léa", "2026-10-20", "2026-10-20T09:00:00+02:00")
    assert "key" not in body

    for gone in (action(status=ItemStatus.CANCELLED), action(dismissed=True), action(owner="Sam")):
        await sink.update_item(gone, meeting, ref)
        assert api.last["remove"] is True and "title" not in api.last
    everyone = make_sink(app, owners="all")
    api = FakeBridge(everyone)
    await everyone.update_item(action(owner="Sam"), meeting, ref)
    assert api.last["remove"] is False


async def test_update_of_an_event_deleted_in_calendar_is_fine(app: Config, meeting: Meeting) -> None:
    sink = make_sink(app)
    FakeBridge(sink, lambda b: {"ok": True, "gone": True})
    await sink.update_item(action(title="Renamed"), meeting, "gcal:alex@gmail.com/ev1@google.com")


async def test_dispatcher_round_trip(app: Config, meeting: Meeting) -> None:
    sink = make_sink(app)
    api = FakeBridge(sink, lambda b: created(b) if b["action"] == "task.create" else {"ok": True})
    item = action(confidence=0.9)
    meeting.items[item.id] = item
    d = Dispatcher(app, meeting, [sink])
    assert await d.sync(require_stable=False) == 1
    assert item.pushed["google"] == "gcal:alex@gmail.com/ev1@google.com"
    item.status, item.revision = ItemStatus.CANCELLED, 2
    assert await d.sync(require_stable=False) == 1
    assert api.last == {"secret": SECRET, "action": "task.update", "calendar_id": "alex@gmail.com", "event_id": "ev1@google.com", "remove": True}


# ------------------------------------------------------------------- notes

NOTES = "# Point budget\n\n- [ ] **Envoyer le devis** — Alex\n\n<script>alert(1)</script> [x](javascript:alert(1))"


def notes_reply(body: dict[str, Any]) -> Reply:
    return {"ok": True, "doc_id": "D0c", "doc_url": "https://docs.google.com/document/d/D0c/edit", "recap_sent": True, "draft_id": "r-1", "warnings": []}


async def test_notes_payload(app: Config, fr_meeting: Meeting) -> None:
    sink = make_sink(app)
    api = FakeBridge(sink, notes_reply)
    fr_meeting.followup_email = FollowUpEmail(
        subject="Suivi :\nbudget", body_markdown="Bonjour à tous,\n\n**Merci** !\nAlex", to=["sam@example.com", "Léa", "x@y.fr\nBcc: z@w.io"]
    )
    assert await sink.push_notes(fr_meeting, NOTES) == "https://docs.google.com/document/d/D0c/edit"
    body = api.last
    assert body["action"] == "meeting.notes" and body["key"].startswith("vs:")
    assert body["doc"] == {"id": None, "title": "2026-10-14 Point budget", "folder": "Meeting notes", "markdown": NOTES}
    recap = body["recap"]
    assert (recap["subject"], recap["link_label"], recap["text"]) == ("Notes : Point budget", "Ouvrir dans Google Docs", NOTES)
    assert "<script" not in recap["html"] and "javascript:" not in recap["html"].split("[x]")[0]
    assert "<li>☐ <strong>Envoyer le devis</strong> — Alex" in recap["html"]
    assert body["draft"] == {
        "to": ["sam@example.com"],
        "subject": "Suivi : budget",
        "html": "<p>Bonjour à tous,</p>\n<p><strong>Merci</strong> !<br>\nAlex</p>",
        "text": "Bonjour à tous,\n\n**Merci** !\nAlex",
    }


async def test_english_notes_and_warnings(app: Config, meeting: Meeting, caplog: pytest.LogCaptureFixture) -> None:
    sink = make_sink(app, docs_folder="Réunions")
    api = FakeBridge(sink, lambda b: {**notes_reply(b), "recap_sent": False, "warnings": ["recap e-mail: quota"]})
    await sink.push_notes(meeting, "# Weekly sync")
    assert api.last["recap"]["subject"] == "Notes: Weekly sync" and api.last["doc"]["folder"] == "Réunions"
    assert api.last["draft"] is None  # no follow-up e-mail in this meeting
    assert "recap e-mail: quota" in caplog.text


async def test_pushing_notes_again_rewrites_the_doc_and_sends_nothing(app: Config, fr_meeting: Meeting) -> None:
    sink = make_sink(app)
    api = FakeBridge(sink, notes_reply)
    fr_meeting.followup_email = FollowUpEmail(subject="Suivi", body_markdown="Bonjour")
    fr_meeting.notes_ref["google"] = "https://docs.google.com/document/d/D0c/edit"
    await sink.push_notes(fr_meeting, "v2")
    assert api.last["doc"]["id"] == "D0c"
    assert api.last["recap"] is None and api.last["draft"] is None


async def test_notes_without_a_doc(app: Config, meeting: Meeting) -> None:
    sink = make_sink(app, docs=False, followup_draft=False)
    api = FakeBridge(sink, lambda b: {"ok": True, "recap_sent": True, "draft_id": None, "warnings": []})
    assert await sink.push_notes(meeting, "# Notes") == f"google:{meeting.id}"
    assert api.last["doc"] is None and api.last["recap"]["html"] == "<h1>Notes</h1>"

    sink = make_sink(app, docs=False, recap_to_self=False, followup_draft=False)
    api = FakeBridge(sink)
    assert await sink.push_notes(meeting, "# Notes") is None
    assert api.requests == []


async def test_check(app: Config) -> None:
    sink = make_sink(app, undated="next_workday")
    FakeBridge(sink, lambda b: {"ok": True, "email": "alex@gmail.com", "version": BRIDGE_VERSION})
    assert await sink.check() == (
        "bridge v1 runs as alex@gmail.com · reminders at 09:00 in calendar primary (undated: next workday) · "
        "notes in Drive folder “Meeting notes” · recap e-mailed to you · follow-up saved as a Gmail draft"
    )
    FakeBridge(sink, lambda b: {"ok": True, "email": "alex@gmail.com", "version": 0})
    with pytest.raises(SinkError, match="bridge is v0 but this voicestream needs v1.*New version"):
        await sink.check()


# ------------------------------------------------------------------ render

XSS = [
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "<svg/onload=alert(1)>",
    '"><iframe src=javascript:alert(1)>',
    "[click](javascript:alert(1))",
    "[click](JaVaScRiPt:alert`1`)",
    "[click](jav&#x61;script:alert(1))",
    "[click](data:text/html;base64,PHNjcmlwdD4=)",
    "[click](vbscript:msgbox)",
    '[click](https://ok.example/"onmouseover="alert(1))',
    "[click](https://ok.example/'onmouseover='alert(1))",
    "**<b onclick=alert(1)>x</b>**",
    "`<script>`",
    "```\n</code></pre><script>alert(1)</script>\n```",
    "# <h1 onclick=x>",
    "> <style>*{display:none}</style>",
    "- [x] <a href=javascript:x>",
]


@pytest.mark.parametrize("payload", XSS)
def test_render_never_emits_markup_from_the_input(payload: str) -> None:
    html = markdown_to_html(payload)
    tags = re.findall(r"<\s*/?\s*([a-zA-Z0-9]+)([^>]*)>", html)
    allowed = {"p", "br", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "blockquote", "pre", "code", "strong", "em", "del", "a", "hr"}
    for name, attrs in tags:
        assert name in allowed, html
        assert attrs == "" or (name == "a" and re.fullmatch(r' href="(https?://|mailto:)[^"<>]*"', attrs)), html
    assert "javascript:" not in "".join(a for _, a in tags).lower()


def test_render_structure() -> None:
    md = (
        "# Réunion **budget**\n"
        "*mercredi* · _2027_ · ~~vieux~~ · `a**b**` · [doc](https://x.fr/?a=1&b=2) · [moi](mailto:a@b.fr)\n\n"
        "> Résumé\n> suite\n\n"
        "- [ ] **Envoyer**\n  détail\n- [x] Fait\n  - imbriqué\n1. un\n2. deux\n\n"
        "Bonjour,\nAlex\n\n---\n```\nx < y\n```"
    )
    assert markdown_to_html(md).split("\n") == [
        "<h1>Réunion <strong>budget</strong></h1>",
        (
            '<p><em>mercredi</em> · <em>2027</em> · <del>vieux</del> · <code>a**b**</code> · '
            '<a href="https://x.fr/?a=1&amp;b=2">doc</a> · <a href="mailto:a@b.fr">moi</a></p>'
        ),
        "<blockquote><p>Résumé<br>",
        "suite</p></blockquote>",
        "<ul><li>☐ <strong>Envoyer</strong>",
        "<br>détail",
        "</li><li>☑ Fait",
        "<ul><li>imbriqué",
        "</li></ul>",
        "</li></ul>",
        "<ol><li>un",
        "</li><li>deux",
        "</li></ol>",
        "<p>Bonjour,<br>",
        "Alex</p>",
        "<hr>",
        "<pre><code>x &lt; y</code></pre>",
    ]


def test_render_leaves_words_alone() -> None:
    assert markdown_to_html("snake_case_name 2 * 3 * 4 # not a heading C#") == "<p>snake_case_name 2 * 3 * 4 # not a heading C#</p>"
    assert markdown_to_html("## Learn C# ##") == "<h2>Learn C#</h2>"
    assert markdown_to_html("") == ""
