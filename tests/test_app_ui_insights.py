"""The app's Ask screen, a meeting's notes template, fields and conversation, and Settings → Claude's notes
and MCP cards, in a real browser (the harness of tests/test_app_ui.py: a real app host, scripted inputs).

``VOICESTREAM_UI_SHOTS=<dir>`` also saves a screenshot of every screen the tests visit.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from test_app_ui import Page, UIApp, browser, make_app, open_page  # noqa: F401 - the fixtures

from voicestream.brain.llm import ScriptedLLM
from voicestream.storage import atomic_write, load_meeting


def _scripted_ask(app: UIApp, answer: Callable[[dict[str, Any]], dict[str, Any]]) -> ScriptedLLM:
    """Claude for Ask: the search words, then ``answer(call)``; the app's other Claude calls stay offline."""
    llm = ScriptedLLM([{"terms": ["job description", "fiche de poste", "recruiter", "recruteur"], "days": 0}, answer])
    app.host.meetings._llm = lambda: (llm, False)  # type: ignore[method-assign]
    return llm


def _ref_and_line(call: dict[str, Any], title: str, words: str) -> tuple[str, int]:
    """In the records Claude was shown: the reference of the meeting called ``title``, and its line with ``words``."""
    text = call["content"][0]["text"]
    ref = re.search(rf'<meeting ref="(M\d+)"[^>]*title="{re.escape(title)}"', text).group(1)
    line = int(re.search(rf"\[{ref}#(\d+) [^\]]*\][^\n]*{re.escape(words)}", text).group(1))
    return ref, line


def test_ask_answers_with_its_sources_and_a_source_opens_its_line(make_app: Callable[..., UIApp],
                                                                     open_page: Callable[..., Page]) -> None:
    from playwright.sync_api import expect

    app = make_app(setup_done=True, meetings=True)

    def answer(call: dict[str, Any]) -> dict[str, Any]:
        ref, line = _ref_and_line(call, "Hiring plan", "job description")
        return {"answer_markdown": "**Alex** sends the job description to the recruiter by Monday [1]. "
                                   "Two engineering roles open this quarter [2].",
                "citations": [{"n": 1, "meeting": ref, "line": line}, {"n": 2, "meeting": ref, "line": -1},
                              {"n": 3, "meeting": "M99", "line": 4}]}  # a meeting Claude wasn't shown: dropped

    llm = _scripted_ask(app, answer)
    p = open_page(app)
    pg = p.page
    pg.click("nav a[href='#/ask']")
    expect(pg.locator("#screen-ask h1")).to_have_text("Ask your meetings")
    expect(pg.locator(".qa-chips .btn")).to_have_count(4)
    p.shot("ask-empty")
    pg.fill("#qa-input", "Who sends the job description, and when?")
    pg.press("#qa-input", "Enter")
    expect(pg.locator("#qa-text")).to_contain_text("sends the job description to the recruiter by Monday")
    expect(pg.locator("#qa-text strong")).to_have_text("Alex")  # Markdown, rendered safely
    expect(pg.locator("#qa-text a.cite")).to_have_count(2)
    sources = pg.locator(".qa-sources li")
    expect(sources).to_have_count(2)
    expect(sources.nth(0)).to_contain_text("Hiring plan")
    expect(sources.nth(0).locator(".qa-quote")).to_contain_text("I'll send the job description to the recruiter by Monday")
    expect(sources.nth(1)).to_contain_text("From the meeting’s notes")
    expect(pg.locator(".qa-foot")).to_contain_text("meetings searched")
    p.shot("ask-answer")
    # the search words came from the question, and the answer from the best meetings' records
    assert [c["schema"].__name__ for c in llm.calls] == ["WireQuery", "WireAnswer"]
    assert "Who sends the job description" in llm.calls[0]["content"][0]["text"]

    # [1] goes to its source; the source opens the meeting's transcript on that line
    pg.locator("#qa-text a.cite").first.click()
    expect(pg.locator("#qa-src-1")).to_be_focused()
    sources.nth(0).locator("a").click()
    expect(pg.locator(".detail-head h1")).to_have_text("Hiring plan")
    expect(pg.locator("#tab-transcript")).to_have_attribute("aria-selected", "true")
    expect(pg.locator(".tline.hit")).to_have_count(1)
    expect(pg.locator(".tline.hit")).to_contain_text("job description")
    p.shot("ask-source-line")

    # back on Ask: the answer is still there
    pg.click("nav a[href='#/ask']")
    expect(pg.locator("#qa-text")).to_contain_text("by Monday")
    expect(pg.locator("#qa-input")).to_have_value("Who sends the job description, and when?")


def test_ask_without_claude_is_a_search_of_every_transcript(make_app: Callable[..., UIApp],
                                                            open_page: Callable[..., Page]) -> None:
    from playwright.sync_api import expect

    app = make_app(setup_done=True, meetings=True)  # brain.provider = "heuristic": no Claude
    p = open_page(app)
    pg = p.page
    pg.goto(app.url.split("#")[0] + "#/ask")
    pg.fill("#qa-input", "recruiter")
    pg.click("#qa-go")
    expect(pg.locator("#qa-out .notice")).to_contain_text("Claude is turned off")
    expect(pg.locator(".qa-matches h2")).to_have_text("Found in 3 meetings")  # three were held from the same transcript
    expect(pg.locator(".qa-match .qa-src-head strong", has_text=re.compile("^Hiring plan$"))).to_have_count(1)
    expect(pg.locator(".qa-match a.tline", has_text="recruiter").first).to_be_visible()  # with a line of context around it
    p.shot("ask-search")
    pg.locator("#qa-out .notice").get_by_role("button", name="Claude settings").click()
    expect(pg.locator("#screen-settings")).to_be_visible()

    # a word no meeting has
    pg.click("nav a[href='#/ask']")
    pg.select_option("#qa-days", index=1)  # the last 7 days
    pg.fill("#qa-input", "zeppelin")
    pg.press("#qa-input", "Enter")
    expect(pg.locator("#qa-out .notice")).to_contain_text("No meeting has your question’s words, and without Claude")


def test_ask_in_french(make_app: Callable[..., UIApp], open_page: Callable[..., Page]) -> None:
    from playwright.sync_api import expect

    app = make_app(setup_done=True, meetings=True)
    p = open_page(app, locale="fr-FR")
    pg = p.page
    pg.click("nav a[href='#/ask']")
    expect(pg.locator("nav a[href='#/ask']")).to_contain_text("Demander")
    expect(pg.locator("#screen-ask h1")).to_have_text("Interrogez vos réunions")
    pg.locator(".qa-chips .btn").first.click()  # an example question, asked at once
    expect(pg.locator("#qa-input")).to_have_value("Qu’est-ce que j’ai promis de faire cette semaine ?")
    expect(pg.locator("#qa-out .notice")).to_contain_text("sans Claude, la recherche ne trouve que des mots")
    pg.fill("#qa-input", "version 2")  # words the French meeting has
    pg.press("#qa-input", "Enter")
    expect(pg.locator("#qa-out .notice")).to_contain_text("Claude est désactivé")
    expect(pg.locator(".qa-matches h2")).to_have_text("Trouvé dans 1 réunion")
    p.shot("ask-fr")


def _with_fields(app: UIApp, title: str) -> str:
    folder = app.folder_of(title)
    d = app.root / "data" / "sessions" / folder
    meeting, _ = load_meeting(d)
    meeting.template = "sales"
    meeting.fields = {"Budget": "29 € per month", "Decision maker": "", "Next step": "Demo on Tuesday"}
    meeting.notes_body = "## Summary\n\n- Two roles open this quarter."
    from voicestream.brain.summarizer import rerender

    atomic_write(d / "notes.md", rerender(meeting, "Camille"))
    atomic_write(d / "meeting.json", meeting.model_dump_json(indent=1))
    return folder


def test_a_meetings_template_fields_and_conversation(make_app: Callable[..., UIApp], open_page: Callable[..., Page]) -> None:
    from playwright.sync_api import expect

    app = make_app(setup_done=True, meetings=True)
    folder = _with_fields(app, "Hiring plan")
    p = open_page(app)
    pg = p.page
    pg.click("nav a[href='#/meetings']")
    row = pg.locator(".mrow", has_text="Hiring plan")
    expect(row.locator(".pill", has_text="Sales call")).to_be_visible()
    expect(row.locator(".mfields")).to_contain_text("Budget 29 € per month")
    row.click()

    # the fields beside the notes, and not again inside them
    fields = pg.locator(".facts .fields")
    expect(fields.locator("h2")).to_have_text("At a glance")
    expect(fields.locator("dd").nth(0)).to_have_text("29 € per month")
    expect(fields.locator("dd").nth(1)).to_have_text("—")
    expect(pg.locator("#notes")).not_to_contain_text("At a glance")
    expect(pg.locator("#notes")).to_contain_text("Two roles open this quarter")
    expect(pg.locator("#notes-template")).to_have_value("auto")
    expect(pg.locator(".template-row")).to_contain_text("Claude picked Sales call")
    p.shot("meeting-fields")

    # corrected by hand: saved, and the notes list it
    pg.click("#fields-edit")
    expect(pg.locator("#field-0")).to_be_focused()
    pg.fill("#field-1", "Their CTO, Léa")
    p.shot("meeting-fields-edit")
    pg.click("#fields-save")
    expect(pg.locator(".toast", has_text="Saved")).to_be_visible()
    expect(fields.locator("dd").nth(1)).to_have_text("Their CTO, Léa")
    d = app.root / "data" / "sessions" / folder
    assert load_meeting(d)[0].fields["Decision maker"] == "Their CTO, Léa"
    assert "- **Decision maker:** Their CTO, Léa" in (d / "notes.md").read_text(encoding="utf-8")

    # another template: the notes are written again with it, and the meeting remembers the choice
    pg.select_option("#notes-template", "recruiting")
    expect(pg.locator(".toast", has_text="The notes were rewritten")).to_be_visible(timeout=15000)
    assert load_meeting(d)[0].template_choice == "recruiting"

    # who talked how much: your figures, then everyone's
    pg.click("#tab-conversation")
    expect(pg.locator(".talk-figures dt").first).to_have_text("You spoke")
    expect(pg.locator(".talk-table tbody tr")).to_have_count(2)
    expect(pg.locator(".talk-table tr.you th")).to_have_text("Me")
    expect(pg.locator(".talk")).to_contain_text("never in the notes")
    p.shot("meeting-conversation")


def test_settings_notes_template_fields_and_mcp(make_app: Callable[..., UIApp], open_page: Callable[..., Page]) -> None:
    from playwright.sync_api import expect

    app = make_app(setup_done=True)
    p = open_page(app)
    pg = p.page
    pg.goto(app.url.split("#")[0] + "#/settings/claude")
    select = pg.locator("#f-brain_notes_template")
    expect(select.locator("option")).to_have_count(7)  # auto and the six built-in templates
    expect(select.locator("option:checked")).to_have_text("Let Claude choose")
    select.select_option(label="Sales call")
    expect(pg.locator("[data-path='brain.notes_template'] .help")).to_contain_text("Sections: Summary, Customer context")
    expect(pg.locator("[data-path='brain.notes_template'] .help")).to_contain_text("Fields: Budget, Decision maker")
    pg.fill("#f-brain_fields", "Budget: what budget did they mention?\n\n  Next step  ")
    expect(pg.locator("#mcp-desktop")).to_contain_text('"mcpServers"')
    expect(pg.locator("#mcp-code")).to_contain_text("claude mcp add voicestream --")
    p.shot("settings-notes-mcp")
    pg.click("#copy-code")
    expect(pg.locator(".toast", has_text="Copied")).to_be_visible()
    assert pg.evaluate("navigator.clipboard.readText()").startswith("claude mcp add voicestream --")
    pg.get_by_role("button", name="Save changes").click()
    expect(pg.locator(".toast", has_text="Saved")).to_be_visible()
    text = app.config_path.read_text(encoding="utf-8")
    assert 'notes_template = "sales"' in text
    assert app.host.cfg.brain.fields == ["Budget: what budget did they mention?", "Next step"]
