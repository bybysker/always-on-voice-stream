"""Notes templates and AI fields, conversation stats, Ask (voicestream/library.py) and the MCP server
(voicestream/mcp_server.py), with their API routes and commands."""

from __future__ import annotations

import io
import json
import time
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient
from test_app_host import _factory, _write_config
from test_app_meetings import WED, _save

from voicestream import mcp_server
from voicestream.app.host import AppHost
from voicestream.brain.llm import LLMError, ScriptedLLM
from voicestream.brain.summarizer import render_notes, summarize
from voicestream.brain.templates import BUILTIN, all_templates, choose, fields_for, parse_field, prompt_block
from voicestream.brain.tracker import ItemTracker
from voicestream.config import Config, ConfigError, load_config
from voicestream.dashboard.server import TOKEN_HEADER, create_app
from voicestream.library import Library, ask, search, stem, terms
from voicestream.models import Item, ItemKind, ItemStatus, Meeting, TranscriptSegment
from voicestream.stats import conversation_stats
from voicestream.storage import atomic_write, load_meeting

TODAY = date(2026, 10, 14)


def seg(text: str, source: str = "system", t: float = 0.0, dur: float = 4.0, speaker: str | None = None,
        voice: int | None = None) -> TranscriptSegment:
    return TranscriptSegment(source=source, speaker=speaker or ("Me" if source == "mic" else "Others"), text=text,
                             t_start=t, t_end=t + dur, voice=voice)


def _summary(**kw: Any) -> dict[str, Any]:
    return {"title": "Acme discovery", "tldr": "Acme wants fewer no-shows.", "notes_markdown": "## Summary\n\n- Acme call.",
            "updates": [], "missing_items": [], "participants": ["Léa"], "speakers": [], "language": "en",
            "followup_subject": "", "followup_body": "", "template": "general", "fields": [],
            "recap_overview": "", "recap_discussed": [], "recap_next_steps": [], **kw}


# ------------------------------------------------------------------ templates


def test_parse_field_and_custom_templates() -> None:
    assert parse_field("  Budget :  what   they can spend ") == parse_field("Budget: what they can spend")
    assert parse_field("Budget").question == "" and parse_field(" : x") is None
    cfg = Config.model_validate({"brain": {
        "fields": ["Budget: the amount", "Mood"],
        "templates": {"board": {"name": "Board meeting", "when": "a board meeting", "sections": ["Summary", "Asks"],
                                "fields": ["Runway", "Budget"]},
                      "sales": {"name": "My sales call", "sections": ["Summary", "Deal"]}}}})
    templates = all_templates(cfg.brain)
    assert list(templates)[:6] == [t.id for t in BUILTIN]  # the built-in ones first, in order
    assert templates["sales"].name == "My sales call" and not templates["sales"].builtin  # yours replaces it
    assert templates["board"].when == "a board meeting"
    # the template's fields, then yours, each name once (case aside)
    assert [f.name for f in fields_for(templates["board"], cfg.brain)] == ["Runway", "Budget", "Mood"]
    assert [f.name for f in fields_for(None, cfg.brain)] == ["Budget", "Mood"]


@pytest.mark.parametrize(("brain", "message"), [
    ({"notes_template": "nope"}, "notes_template 'nope'"),
    ({"templates": {"Bad Id": {"name": "x", "sections": ["a"]}}}, "template id 'Bad Id'"),
    ({"templates": {"auto": {"name": "x", "sections": ["a"]}}}, "not 'auto'"),
    ({"templates": {"x": {"name": "x", "sections": []}}}, "sections"),
])
def test_template_settings_are_checked(tmp_path: Path, brain: dict[str, Any], message: str) -> None:
    import tomlkit

    path = tmp_path / "voicestream.toml"
    path.write_text(tomlkit.dumps({"brain": brain}), encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        load_config(path, load_env=False)


def test_a_custom_template_can_be_the_setting() -> None:
    cfg = Config.model_validate({"brain": {"notes_template": "board",
                                           "templates": {"board": {"name": "Board", "sections": ["Summary"]}}}})
    template, _ = choose(cfg.brain, None)
    assert template is not None and template.id == "board"
    # a choice made on the meeting's page wins over the setting; "auto" lets Claude pick
    assert choose(cfg.brain, "sales")[0].id == "sales" and choose(cfg.brain, "auto")[0] is None


def test_prompt_block_lists_every_template_only_for_auto() -> None:
    cfg = Config.model_validate({"brain": {"fields": ["Next step: the next meeting"]}})
    template, templates = choose(cfg.brain, None)
    auto = prompt_block(template, templates, cfg.brain)
    assert all(f"- {t.id} (" in auto for t in BUILTIN)
    assert "fields: Budget: the budget or price range" in auto  # the sales template's own
    assert "for every meeting, whatever the template:\n- Next step: the next meeting" in auto
    one = prompt_block(*choose(cfg.brain, "one_on_one"), cfg.brain)
    assert one.startswith("Notes template: one_on_one (One-on-one) — sections: Summary; Wins;")
    assert "- Next step: the next meeting" in one and "sales" not in one


async def test_claude_picks_the_template_and_fills_its_fields() -> None:
    cfg = Config(user_name="Alex", brain={"fields": ["Mood: how the client felt"]})  # type: ignore[arg-type]
    m = Meeting(segments=[seg("Our budget is 40 000 euros a year, and we also look at Globex.")])
    llm = ScriptedLLM([_summary(template="sales", fields=[
        {"name": "budget", "value": "  40 000 € a year "},  # names matched case aside, values tidied
        {"name": "Competitors", "value": "Globex"},
        {"name": "Invented", "value": "dropped"},
    ])])
    notes = await summarize(cfg, m, ItemTracker(m, None, "Alex", today=TODAY), llm, today=TODAY)
    content = llm.calls[0]["content"][1]["text"]
    assert "Notes templates — pick the one that fits" in content and "- recruiting (Job interview)" in content
    assert m.template == "sales"
    # every field of the template, then yours, in order; '' when not said
    assert m.fields == {"Budget": "40 000 € a year", "Decision maker": "", "Timeline": "", "Competitors": "Globex",
                        "Next step": "", "Mood": ""}
    assert "## At a glance\n\n- **Budget:** 40 000 € a year\n- **Competitors:** Globex\n\n" in notes
    assert "Decision maker" not in notes  # an empty answer stays out of the notes


async def test_a_chosen_template_wins_and_an_unknown_pick_is_general() -> None:
    cfg = Config(user_name="Alex", brain={"notes_template": "project"})  # type: ignore[arg-type]
    m = Meeting(segments=[seg("We are late on the API.")])
    llm = ScriptedLLM([_summary(template="sales", fields=[{"name": "Status", "value": "late"}])])
    await summarize(cfg, m, ItemTracker(m, None, "Alex", today=TODAY), llm, today=TODAY)
    assert "Notes template: project (Team or project meeting)" in llm.calls[0]["content"][1]["text"]
    assert m.template == "project" and m.fields == {"Status": "late"}  # Claude's "sales" ignored: you chose
    # picked on the meeting's page: auto, and Claude's pick doesn't exist
    m.template_choice = "auto"
    await summarize(cfg, m, ItemTracker(m, None, "Alex", today=TODAY), ScriptedLLM([_summary(template="board")]), today=TODAY)
    assert m.template == "general" and m.fields == {}


async def test_without_claude_the_notes_keep_their_template_and_fields() -> None:
    cfg = Config(user_name="Alex", brain={"provider": "heuristic"})  # type: ignore[arg-type]
    m = Meeting(segments=[seg("Le budget est de 40 000 euros.")], template="sales", fields={"Budget": "40 000 €"})
    notes = await summarize(cfg, m, ItemTracker(m, None, "Alex", today=TODAY), None, today=TODAY)
    assert m.template == "sales" and "## En bref\n\n- **Budget:** 40 000 €" in notes  # a French meeting


def test_fields_lead_the_notes_after_the_summary() -> None:
    m = Meeting(title="Acme", fields={"Budget": "40 k€", "Next step": ""}, language="en")
    m.items["A1"] = Item(id="A1", kind=ItemKind.ACTION, title="Send the quote")
    notes = render_notes(m, "Acme wants a quote.", "## Summary\n\n- x", "Alex")
    assert notes.index("> Acme wants a quote.") < notes.index("## At a glance") < notes.index("## Action items")


# ---------------------------------------------------------------------- stats


def test_conversation_stats() -> None:
    m = Meeting(segments=[
        seg("Hello, how are you? What do you need?", "mic", t=0, dur=10),
        seg("Fine.", "system", t=11, dur=2, voice=1),
        seg("So here is our product and its many features " * 3, "mic", t=14, dur=40),
        seg("and more about the price", "mic", t=57, dur=10),  # 3 s later: the same monologue
        seg("and the roadmap", "mic", t=80, dur=5),  # 13 s later: a new stretch
        seg("Who decides???", "system", t=90, dur=3, voice=2),
    ])
    st = conversation_stats(m, lambda s: "Me" if s.source == "mic" else f"Speaker {s.voice}")
    me, *others = st["speakers"]
    assert (me["name"], me["you"], me["talk_s"], me["longest_s"], me["questions"], me["turns"]) == ("Me", True, 65.0, 53.0, 2, 3)
    assert me["wpm"] == round(me["words"] / (65 / 60))
    assert [p["name"] for p in others] == ["Speaker 2", "Speaker 1"]
    assert others[0]["questions"] == 3 and others[0]["wpm"] is None  # under 30 s of speech: no pace
    assert st["you"] == {"share": me["share"], "longest_s": 53.0, "questions": 2}
    assert abs(sum(p["share"] for p in st["speakers"]) - 1) < 0.01
    assert st["duration_s"] == 93.0 and st["switches_per_min"] == round(3 / (93 / 60), 1)  # Me→1, 1→Me, Me→2
    assert conversation_stats(Meeting(), lambda s: s.speaker) == {
        "speakers": [], "speech_s": 0, "duration_s": 0.0, "switches_per_min": None, "you": None}


# ------------------------------------------------------------------ the library


def _meeting(cfg: Config, name: str, title: str, lines: list[tuple[str, str]], *, days_ago: float, **kw: Any) -> Path:
    m = Meeting(title=title, started_at=datetime.now(UTC) - timedelta(days=days_ago), **kw)
    for i, (source, text) in enumerate(lines):
        m.segments.append(seg(text, source, t=i * 10.0, dur=8.0))
    m.ended_at = m.started_at + timedelta(minutes=30)
    folder = cfg.data_path / "sessions" / name
    atomic_write(folder / "meeting.json", m.model_dump_json(indent=1))
    return folder


@pytest.fixture
def lib_cfg(tmp_path: Path) -> Config:
    cfg = Config(data_dir=str(tmp_path / "data"), user_name="Alex")
    _meeting(cfg, "2026-11-03_100000-acme", "Acme renewal", [
        ("system", "Bonjour, on regarde le renouvellement."),
        ("system", "Vos tarifs sont trop chers pour notre budget."),
        ("mic", "On peut proposer une remise de dix pour cent."),
        ("system", "Globex nous fait une meilleure offre."),
    ], days_ago=2, participants=["Léa"], fields={"Budget": "40 000 €"})
    _meeting(cfg, "2026-11-01_090000-hiring", "Hiring sync", [
        ("mic", "We need two engineers this quarter."),
        ("system", "I'll post the job ad tomorrow."),
    ], days_ago=4)
    _meeting(cfg, "2026-09-01_090000-old", "Old pricing review", [("mic", "Our pricing page needs work.")], days_ago=60)
    return cfg


def test_terms_fold_accents_drop_stop_words_and_stem() -> None:
    assert terms("Qu'est-ce que les clients ont dit sur les TARIFS et la concurrence ?") == ["client", "tarif", "concur"]
    assert terms("What did Acme say about pricing?") == ["acme", "pricin"]
    assert stem("budgets") == "budget" and stem("class") == "class" and terms("Réunion de l'équipe") == ["equipe"]


def test_search_ranks_by_rare_words_and_the_period(lib_cfg: Config) -> None:
    docs = Library().docs(lib_cfg)
    hits = search(docs, terms("tarifs budget Globex"))
    assert [h.doc.meeting.title for h in hits] == ["Acme renewal"]
    assert [ln.index for ln in hits[0].lines] == [0, 1, 2, 3]  # the matches with a line of context around them
    # no word matches anywhere: the latest meetings, without lines
    recent = search(docs, terms("zeppelin"), limit=2)
    assert [h.doc.meeting.title for h in recent] == ["Acme renewal", "Hiring sync"] and not recent[0].lines
    since = datetime.now(UTC) - timedelta(days=30)
    # none of the period's meetings has the word: its latest ones, unscored (Ask's "summarize my week")
    assert [(h.doc.meeting.title, h.score) for h in search(docs, terms("pricing"), since=since)] == [
        ("Acme renewal", 0.0), ("Hiring sync", 0.0)]
    assert [h.doc.meeting.title for h in search(docs, terms("pricing"))] == ["Old pricing review"]


def test_the_library_reads_a_meeting_again_only_when_it_changed(lib_cfg: Config) -> None:
    lib = Library()
    first = {d.folder: d for d in lib.docs(lib_cfg)}
    again = {d.folder: d for d in lib.docs(lib_cfg)}
    assert all(again[k] is first[k] for k in first)
    folder = lib_cfg.data_path / "sessions" / "2026-11-01_090000-hiring"
    m, _ = load_meeting(folder)
    m.title = "Hiring plan"
    time.sleep(0.01)
    atomic_write(folder / "meeting.json", m.model_dump_json())
    assert {d.folder: d.meeting.title for d in lib.docs(lib_cfg)}["2026-11-01_090000-hiring"] == "Hiring plan"


async def test_ask_answers_with_checked_citations(lib_cfg: Config) -> None:
    def answer(call: dict[str, Any]) -> dict[str, Any]:
        text = call["content"][0]["text"]
        assert '<meeting ref="M1"' in text and 'title="Acme renewal"' in text and "Fields: Budget: 40 000 €" in text
        assert "[M1#1 00:10 Léa] Vos tarifs sont trop chers pour notre budget." in text  # the one other person
        assert "Hiring sync" not in text  # no word of the question in it
        return {"answer_markdown": "Acme finds it too expensive [1], and Globex is cheaper [2] [3] [4].",
                "citations": [{"n": 1, "meeting": "m1", "line": 1}, {"n": 2, "meeting": "M1", "line": -1},
                              {"n": 3, "meeting": "M7", "line": 0}, {"n": 4, "meeting": "M1", "line": 99}]}

    llm = ScriptedLLM([{"terms": ["prix", "tarif", "pricing", "cher", "Globex"], "days": 0}, answer])
    r = await ask(lib_cfg, Library(), "What did Acme say about our prices?", llm)
    assert r["answer"].startswith("Acme finds it too expensive [1]") and r["mode"] == "claude"
    assert [c["n"] for c in r["citations"]] == [1, 2, 4]  # M7 was never shown
    assert r["citations"][0]["line"]["text"] == "Vos tarifs sont trop chers pour notre budget."
    assert r["citations"][1]["line"] is None and r["citations"][2]["line"] is None  # the notes; a line it wasn't shown
    assert r["searched"] == 3 and "tarif" in r["words"]
    assert [c["model"] for c in llm.calls] == [lib_cfg.brain.model, lib_cfg.brain.summary_model]
    assert "<question>What did Acme say about our prices?</question>" in llm.calls[0]["content"][0]["text"]


async def test_ask_takes_its_period_from_the_question_unless_you_chose_one(lib_cfg: Config) -> None:
    llm = ScriptedLLM([{"terms": ["pricing"], "days": 7}, {"answer_markdown": "x", "citations": []}])
    r = await ask(lib_cfg, Library(), "pricing this week", llm)
    assert r["days"] == 7 and r["searched"] == 2 and r["meetings"] == [
        m for m in r["meetings"] if m["title"] != "Old pricing review"]
    llm = ScriptedLLM([{"terms": ["pricing"], "days": 7}, {"answer_markdown": "x", "citations": []}])
    r = await ask(lib_cfg, Library(), "pricing this week", llm, days=0)  # "all meetings" chosen
    assert r["days"] == 0 and r["searched"] == 3 and r["meetings"][0]["title"] == "Old pricing review"


async def test_ask_without_claude_is_a_search(lib_cfg: Config) -> None:
    r = await ask(lib_cfg, Library(), "Globex", None, offline_reason="no_key")
    assert (r["answer"], r["mode"], r["offline_reason"]) == (None, "search", "no_key")
    assert [m["title"] for m in r["meetings"]] == ["Acme renewal"]
    assert [x["text"] for x in r["meetings"][0]["excerpts"]][-1] == "Globex nous fait une meilleure offre."
    assert (await ask(lib_cfg, Library(), "zeppelin", None))["meetings"] == []  # not the latest meetings: no match


async def test_ask_keeps_the_matches_when_claude_fails(lib_cfg: Config) -> None:
    llm = ScriptedLLM([LLMError("overloaded"), LLMError("overloaded")])  # no search words, then no answer
    r = await ask(lib_cfg, Library(), "Globex offer", llm)
    assert r["answer"] is None and r["mode"] == "search" and "overloaded" in r["error"]
    assert r["meetings"][0]["title"] == "Acme renewal"


# ----------------------------------------------------------------------- MCP


@pytest.fixture
def server(lib_cfg: Config) -> mcp_server.Server:
    folder = lib_cfg.data_path / "sessions" / "2026-11-03_100000-acme"
    m, _ = load_meeting(folder)
    m.items["A1"] = Item(id="A1", kind=ItemKind.ACTION, title="Send Acme a new quote", owner="Alex", due_date="2026-11-07")
    m.items["A2"] = Item(id="A2", kind=ItemKind.ACTION, title="Check with legal", owner="Léa")
    m.items["A3"] = Item(id="A3", kind=ItemKind.ACTION, title="Old idea", owner="Alex", status=ItemStatus.CANCELLED)
    m.notes_body = "## Summary\n\n- Acme pushes back on price."
    atomic_write(folder / "meeting.json", m.model_dump_json())
    return mcp_server.Server(lambda: lib_cfg)


def _rpc(server: mcp_server.Server, method: str, params: dict[str, Any] | None = None, msg_id: int = 1) -> dict[str, Any]:
    reply = mcp_server.handle(server, {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}})
    assert reply is not None
    return reply


def _tool(server: mcp_server.Server, name: str, **arguments: Any) -> tuple[str, bool]:
    result = _rpc(server, "tools/call", {"name": name, "arguments": arguments})["result"]
    return result["content"][0]["text"], result["isError"]


def test_mcp_handshake_and_tools(server: mcp_server.Server) -> None:
    init = _rpc(server, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}})
    assert init["result"]["protocolVersion"] == "2025-06-18" and init["result"]["serverInfo"]["name"] == "voicestream"
    assert _rpc(server, "initialize", {"protocolVersion": "1999-01-01"})["result"]["protocolVersion"] == mcp_server.PROTOCOLS[0]
    assert mcp_server.handle(server, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert _rpc(server, "ping")["result"] == {}
    tools = _rpc(server, "tools/list")["result"]["tools"]
    assert [t["name"] for t in tools] == ["search_meetings", "list_meetings", "get_meeting", "get_transcript", "list_action_items"]
    assert all(t["annotations"]["readOnlyHint"] and t["inputSchema"]["type"] == "object" for t in tools)
    assert _rpc(server, "resources/list")["error"]["code"] == -32601
    assert mcp_server.handle(server, ["not", "an", "object"])["error"]["code"] == -32600


def test_mcp_tools_read_the_meetings(server: mcp_server.Server) -> None:
    text, failed = _tool(server, "search_meetings", query="Globex remise")
    assert not failed and "## Acme renewal\nref: 2026-11-03_100000-acme" in text
    assert "#3 [00:30] Léa: Globex nous fait une meilleure offre." in text and "Fields: Budget: 40 000 €" in text
    text, _ = _tool(server, "list_meetings", days=0)
    assert text.startswith("3 of 3 meeting(s), newest first:") and text.index("Acme renewal") < text.index("Hiring sync")
    text, _ = _tool(server, "get_meeting", meeting="2026-11-03")  # the start of a ref
    assert "Fields:\n- Budget: 40 000 €" in text and "- [ ] Send Acme a new quote · owner: Alex · due: 2026-11-07" in text
    assert "Notes:\n## Summary" in text and "Talk time: Léa 75%, Me (the user) 25%" in text
    text, _ = _tool(server, "get_transcript", meeting="last", start_line=1, max_lines=2)
    assert "#1 [00:10] Léa: Vos tarifs" in text and "#3" not in text and "1 more lines: call again with start_line 3" in text
    text, _ = _tool(server, "list_action_items")  # yours, open, the soonest due first
    assert "Send Acme a new quote" in text and "Check with legal" not in text and "Old idea" not in text
    text, _ = _tool(server, "list_action_items", owner="all", status="all")
    assert "Check with legal" in text and "cancelled" in text


@pytest.mark.parametrize(("name", "arguments", "error"), [
    ("nope", {}, "no tool 'nope'"),
    ("get_meeting", {}, "missing argument(s): meeting"),
    ("get_meeting", {"meeting": "2026-12"}, "no meeting '2026-12'"),
    ("get_meeting", {"meeting": "2026-11-0"}, "is the start of 2 meetings"),
    ("list_meetings", {"days": "7"}, "days must be a whole number"),
    ("list_action_items", {"owner": "boss"}, "owner must be mine or all"),
    ("search_meetings", {"query": "the of"}, "give words to look for"),
    ("list_meetings", {"bogus": 1}, "unknown argument(s): bogus"),
])
def test_mcp_tool_errors_are_results(server: mcp_server.Server, name: str, arguments: dict[str, Any], error: str) -> None:
    text, failed = _tool(server, name, **arguments)
    assert failed and error in text


def test_mcp_serves_lines_of_json_and_reports_broken_settings(lib_cfg: Config) -> None:
    def broken() -> Config:
        raise ConfigError("voicestream.toml: brain.langage: Extra inputs are not permitted")

    stdin = io.BytesIO(b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n\nnot json\n'
                       b'{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"list_meetings","arguments":{}}}\n')
    stdout = io.BytesIO()
    mcp_server.serve(mcp_server.Server(broken), stdin, stdout)
    replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [r.get("id") for r in replies] == [1, None, 2]
    assert replies[1]["error"]["code"] == -32700
    assert replies[2]["result"]["isError"] and "settings can't be read" in replies[2]["result"]["content"][0]["text"]


def test_mcp_client_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(mcp_server.shutil, "which", lambda name: "/usr/local/bin/voicestream")
    monkeypatch.setenv("VOICESTREAM_CONFIG", str(tmp_path / "default.toml"))
    c = mcp_server.client_config(tmp_path / "default.toml")
    assert (c["command"], c["args"]) == ("/usr/local/bin/voicestream", ["mcp"])
    assert json.loads(c["desktop"]) == {"mcpServers": {"voicestream": {"command": "/usr/local/bin/voicestream", "args": ["mcp"]}}}
    assert c["claude_code"] == "claude mcp add voicestream -- /usr/local/bin/voicestream mcp"
    other = tmp_path / "my settings.toml"
    assert mcp_server.client_config(other)["args"] == ["--config", str(other), "mcp"]
    monkeypatch.setattr(mcp_server.sys, "frozen", True, raising=False)
    monkeypatch.setattr(mcp_server.sys, "executable", "/Applications/Voicestream.app/Contents/MacOS/Voicestream")
    frozen = mcp_server.client_config(other)
    assert frozen["args"] == ["mcp", "--config", str(other)]  # the app reads it after "mcp"
    assert f"'{other}'" in frozen["claude_code"]  # quoted for the shell


def test_the_packaged_app_runs_the_mcp_server(monkeypatch: pytest.MonkeyPatch) -> None:
    from voicestream.app import launcher

    seen: list[str | None] = []
    monkeypatch.setattr(mcp_server, "main", lambda config=None: seen.append(config) or 0)
    assert launcher.main(["mcp"]) == 0 and launcher.main(["mcp", "--config", "/x.toml"]) == 0
    assert seen == [None, "/x.toml"]


# ------------------------------------------------------------- routes, command


@pytest.fixture
def host(tmp_path: Path) -> AppHost:
    h = AppHost(_write_config(tmp_path), process_setup=False, prefs_path=tmp_path / "app.json", assistant_factory=_factory())
    folder = _save(h.cfg, "2026-10-14_110000-planning", started=WED, title="Q4 planning")
    m, _ = load_meeting(folder)
    m.template, m.fields = "sales", {"Budget": "40 k€", "Next step": ""}
    atomic_write(folder / "meeting.json", m.model_dump_json(indent=1))
    return h


@pytest.fixture
def client(host: AppHost) -> Iterator[TestClient]:
    with TestClient(create_app(host, "tok"), base_url="http://127.0.0.1:8765", headers={TOKEN_HEADER: "tok"}) as c:
        yield c


def test_routes_for_fields_templates_stats_and_ask(host: AppHost, client: TestClient) -> None:
    path = "/api/meetings/2026-10-14_110000-planning"
    d = client.get(path).json()
    assert d["stats"]["speakers"][0]["you"] and d["template_setting"] == "auto"
    assert [t["id"] for t in d["templates"]][:2] == ["general", "sales"]
    assert client.get("/api/meetings").json()["meetings"][0]["fields"] == {"Budget": "40 k€"}  # answered ones
    assert client.get("/api/meetings?q=40 k").json()["total"] == 1  # the list's search reads them too

    r = client.patch(f"{path}/fields", json={"values": {"Next step": "  Demo  on Tuesday "}})
    assert r.status_code == 200 and r.json()["fields"]["Next step"] == "Demo on Tuesday"
    folder = host.cfg.data_path / "sessions" / "2026-10-14_110000-planning"
    assert "- **Next step:** Demo on Tuesday" in (folder / "notes.md").read_text(encoding="utf-8")
    assert client.patch(f"{path}/fields", json={"values": {"Nope": "x"}}).status_code == 404
    assert client.patch(f"{path}/fields", json={"values": {}}).status_code == 400

    assert client.post(f"{path}/summarize", json={"template": "nope"}).status_code == 400
    assert client.post(f"{path}/summarize", json={"template": "recruiting"}).status_code == 202
    deadline = time.monotonic() + 5
    while load_meeting(folder)[0].template_choice != "recruiting" and time.monotonic() < deadline:
        time.sleep(0.05)
    assert load_meeting(folder)[0].template_choice == "recruiting"

    r = client.post("/api/ask", json={"question": "deck"})
    assert r.status_code == 200 and r.json()["meetings"][0]["title"] == "Q4 planning"
    assert client.post("/api/ask", json={"question": ""}).status_code == 400
    assert client.post("/api/ask", json={"question": "x", "days": -1}).status_code == 400
    settings = client.get("/api/settings").json()
    assert settings["mcp"]["args"][-1] == "mcp" and len(settings["templates"]) == 6


def test_ask_command(lib_cfg: Config, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from voicestream.cli import main

    config = tmp_path / "voicestream.toml"
    config.write_text(f'data_dir = "{lib_cfg.data_path.as_posix()}"\nuser_name = "Alex"\n', encoding="utf-8")
    assert main(["-c", str(config), "ask", "Globex", "--offline"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Matches in 1 of 3 meetings:") and "00:30 Léa: Globex nous fait une meilleure offre." in out


def test_ask_command_prints_the_answer_and_its_sources() -> None:
    from voicestream.cli import format_answer

    r = {"answer": "Too expensive [1].", "citations": [
        {"n": 1, "title": "Acme renewal", "started_at": "2026-11-03T10:00:00+00:00", "folder": "f",
         "line": {"t": 10.0, "speaker": "Léa", "text": "Vos tarifs sont trop chers."}}]}
    out = format_answer(r)
    assert out.startswith("Too expensive [1].\n\nSources:\n  [1] Acme renewal")
    assert "00:10 Léa: “Vos tarifs sont trop chers.”  (f)" in out
