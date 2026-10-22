"""The app's UI (dashboard/static/app.html, app.js, app.css) in a real browser.

Playwright drives Chromium against a real :class:`~voicestream.app.host.AppHost` — its HTTP API and
event stream, in a thread as in the Mac app — with scripted speech instead of a microphone, a fake
model download (progress events, no network) and stand-ins for the permission probes and key
checks. Skipped when Playwright or a Chromium build isn't installed (``pip install playwright``; the
browsers come from ``playwright install chromium`` or ``$PLAYWRIGHT_CHROMIUM``).

``VOICESTREAM_UI_SHOTS=<dir>`` also saves a screenshot of every screen the tests visit.
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
import re
import threading
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from voicestream import stt_models
from voicestream.app import devices as devices_mod
from voicestream.app import secrets as keys
from voicestream.app.host import AppHost
from voicestream.dashboard.server import TOKEN_HEADER, create_app

CONFIG = """\
user_name = "{user}"
data_dir = "{data}"

[transcription]
backend = "{backend}"

[brain]
provider = "{provider}"

[agents]
mode = "suggest"

[always_on]
idle_split_minutes = 10
"""

TRANSCRIPT_FR = """\
WEBVTT

00:00:01.000 --> 00:00:06.000
<v Camille>Bonjour à tous, on fait le point sur le lancement de la version 2.

00:00:07.000 --> 00:00:13.000
<v Sam>J'ai fini la maquette de la page d'accueil, il reste les textes.

00:00:14.000 --> 00:00:20.000
<v Camille>Je vais écrire les textes de la page d'accueil d'ici vendredi.

00:00:21.000 --> 00:00:27.000
<v Sam>On a décidé de garder le prix à 29 euros par mois.

00:00:28.000 --> 00:00:34.000
<v Camille>Sam, tu peux préparer la démo pour le client avant mardi ?
"""

TRANSCRIPT_EN = """\
Camille: Quick sync on the hiring plan.
Alex: I'll send the job description to the recruiter by Monday.
Camille: We decided to open two engineering roles this quarter.
Alex: Can you review the interview questions before Thursday?
Camille: Sure, I will review them tomorrow.
"""

HOSTILE_NOTES = """\
# Budget review

*Monday* · 12:00 recorded

> Budget review in short

## Action items

- [ ] **Send the budget** — Camille <img src=x onerror="window.pwned=1">
- [x] Book the room

## Notes

Plain <script>window.pwned=2</script> text with [a link](https://example.com/budget) and
[a trap](javascript:window.pwned=3) and `code <b>`.
"""

KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "DEEPGRAM_API_KEY", "HUBSPOT_TOKEN",
            "VOICESTREAM_GOOGLE_URL", "VOICESTREAM_GOOGLE_SECRET")
"""No key from the environment (or an earlier test) may leak into a UI test, nor one saved by it out of it."""

DL_STEP_S = 0.3
"""The fake download's pace: just slower than the host's progress events (at most every 0.25 s), so each step is shown."""


# ------------------------------------------------------------------------- browser


def _chromium_candidates() -> list[str]:
    roots = [os.environ.get("PLAYWRIGHT_BROWSERS_PATH"), "/opt/pw-browsers", str(Path.home() / ".cache/ms-playwright"),
             str(Path.home() / "Library/Caches/ms-playwright")]
    found = [os.environ.get("PLAYWRIGHT_CHROMIUM") or ""]
    for root in filter(None, roots):
        found += sorted(glob.glob(f"{root}/chromium-*/chrome-linux/chrome"), reverse=True)
        found += sorted(glob.glob(f"{root}/chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium"), reverse=True)
        found += sorted(glob.glob(f"{root}/chromium_headless_shell-*/chrome-*/headless_shell"), reverse=True)
    return [f for f in found if f and os.access(f, os.X_OK)]


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    sync_api = pytest.importorskip("playwright.sync_api", reason="pip install playwright to run the UI tests")
    pw = sync_api.sync_playwright().start()
    launched = None
    errors = []
    for exe in [*_chromium_candidates(), None]:  # None: Playwright's own download, if any
        try:
            launched = pw.chromium.launch(executable_path=exe) if exe else pw.chromium.launch()
            break
        except Exception as exc:  # noqa: BLE001 - try the next one
            errors.append(f"{exe}: {exc}".splitlines()[0])
    if launched is None:
        pw.stop()
        pytest.skip("no Chromium for Playwright: " + "; ".join(errors))
    yield launched
    launched.close()
    pw.stop()


# --------------------------------------------------------------------------- the app


class UIApp:
    """A real app host on a free port with scripted inputs, and the browser page showing it."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch, *, setup_done: bool, listen_on_launch: bool = False,
                 user: str = "Camille", meetings: bool = False, provider: str = "heuristic", backend: str = "fake",
                 config_extra: str = "", fresh: bool = False, models: tuple[str, ...] = (), broken_mic: int = 0,
                 keys: dict[str, str] | None = None, meeting_start: str = "automatic") -> None:
        """``fresh``: no settings file at all (a new install); ``models``: speech models already
        downloaded; ``broken_mic``: that many assistants get a microphone that gives up at once
        (meeting audio keeps going); ``keys``: keys already saved; ``meeting_start``: how meetings
        start (most of these screens were drawn for always-on listening, so ``automatic``)."""
        from test_session import FakeEngine, IdleSource

        from voicestream.models import TranscriptSegment
        from voicestream.session import Assistant
        from voicestream.sinks.markdown import MarkdownConfig, MarkdownSink
        from voicestream.transcribe.base import FakeTranscriber

        self.root = root
        self.TranscriptSegment = TranscriptSegment
        for name in KEY_VARS:
            # set first so the undo removes what the app writes into os.environ when a key is saved
            monkeypatch.setenv(name, "")
            monkeypatch.delenv(name)
        for name, value in (keys or {}).items():
            monkeypatch.setenv(name, value)
        self.config_path = root / "voicestream.toml"
        if not fresh:
            self.config_path.write_text(CONFIG.format(user=user, data=(root / "data").as_posix(), provider=provider,
                                                      backend=backend) + config_extra, encoding="utf-8")
        for name in models:
            path = root / "data" / "models" / f"ggml-{name}.bin"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(stt_models.GGML_MAGIC + b"\0" * 64)
        self.prefs_path = root / "app.json"
        self.prefs_path.write_text(json.dumps({"setup_done": setup_done, "listen_on_launch": listen_on_launch,
                                               "meeting_start": meeting_start}), encoding="utf-8")
        self.dl_gate = threading.Event()
        self.dl_gate.set()
        self.reminders_checks: list[str] = []
        self.reminders_denied = False
        self._fake_downloads(monkeypatch)
        self._fake_probes(monkeypatch)
        # the fake whisper-server passes the "on this Mac" check once a model is "downloaded"
        monkeypatch.setenv("VOICESTREAM_WHISPER_SERVER", str(Path(__file__).parent / "fake_whisper_server.py"))
        if meetings:
            self.import_meetings()
        self.built: list[Assistant] = []

        def sources() -> list[Any]:
            from voicestream.audio import CaptureSetupError
            from voicestream.audio.base import AudioSource

            class GoneMic(AudioSource):  # AirPods in their case: the named microphone isn't there
                async def frames(self) -> Any:
                    raise CaptureSetupError("no input device matching 'AirPods Pro'")
                    yield  # pragma: no cover

            broken = len(self.built) < broken_mic
            return [GoneMic("mic") if broken else IdleSource("mic"), IdleSource("system")]

        def factory(host: AppHost) -> Assistant:
            a = Assistant(
                host.cfg,
                sources=sources(),
                transcriber_factory=lambda s: FakeTranscriber([], vad=host.cfg.vad, on_usage=host.costs.add_stt),
                llm=None,
                sinks=[MarkdownSink(MarkdownConfig(), host.cfg)],
                agent_engine=FakeEngine(),
                bus=host.bus,
                costs=host.costs,
            )
            a.idle_poll_s = 0.05
            self.built.append(a)
            return a

        self.host = AppHost(self.config_path, process_setup=False, prefs_path=self.prefs_path, assistant_factory=factory)
        self.url = self.host.run_in_thread()
        self.token = self.host.token

    # ------------------------------------------------------------------ fakes

    def _fake_downloads(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gate = self.dl_gate

        async def download(cfg: Any, name: str, *, on_progress: Any = None, client: Any = None, url: Any = None) -> Path:
            info = stt_models.CATALOG[name]
            steps = 10 if info.kind == "whisper" else 1
            for i in range(1, steps + 1):
                await asyncio.sleep(DL_STEP_S)
                if on_progress:
                    on_progress(info.size * i // steps, info.size)
                while i == steps - 1 and not gate.is_set():  # held at 90% while the test looks at it
                    await asyncio.sleep(0.05)
            dest = stt_models.model_path(cfg, name)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(stt_models.GGML_MAGIC + b"\0" * 64)
            return dest

        monkeypatch.setattr(stt_models, "download", download)

    def _fake_probes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def probe(cfg: Any, kind: str, **kw: Any) -> dict[str, Any]:
            await asyncio.sleep(0.2)
            if kind == "microphone":
                return {"status": "granted", "peak_db": -24.0, "detail": "sound is coming through", "native": None}
            return {"status": "unknown", "peak_db": None, "detail": "nothing was heard", "native": None, "silent": True}

        def list_devices(cfg: Any) -> dict[str, Any]:
            return {"inputs": [{"index": 0, "name": "MacBook Pro Microphone", "default": True},
                               {"index": 2, "name": "AirPods Pro", "default": False}],
                    "error": None, "system_audio": {"method": "tap", "available": True, "detail": "Core Audio tap"}}

        async def verify(name: str, cfg: Any, **kw: Any) -> dict[str, Any]:
            return {"ok": True, "detail": "Claude accepted the key"} if os.environ.get(name) else {"ok": False, "detail": f"{name} is not set"}

        async def reminders_check(sink: Any) -> str:  # macOS would show its Automation question now
            from voicestream.sinks.base import SinkError
            from voicestream.sinks.reminders import PERMISSION_HELP

            self.reminders_checks.append(sink.cfg.list_name)
            await asyncio.sleep(0.3)
            if self.reminders_denied:
                raise SinkError(PERMISSION_HELP, retryable=False)
            return f"Reminders reachable (3 lists) · new reminders go to “{sink.cfg.list_name}”"

        from voicestream.sinks.reminders import RemindersSink

        monkeypatch.setattr(devices_mod, "probe", probe)
        monkeypatch.setattr(devices_mod, "list_devices", list_devices)
        monkeypatch.setattr(keys, "verify", verify)
        monkeypatch.setattr(RemindersSink, "check", reminders_check)

    # --------------------------------------------------------------- meetings

    def import_meetings(self) -> None:
        """Past meetings through ``voicestream import`` (the real command), at dates the list groups."""
        from voicestream.cli import main
        from voicestream.models import FollowUpEmail
        from voicestream.storage import atomic_write, list_sessions, load_meeting

        now = datetime.now()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        plan = [("planning.vtt", TRANSCRIPT_FR, midnight + (now - midnight) / 2, "Lancement de la version 2"),  # earlier today
                ("hiring.txt", TRANSCRIPT_EN, midnight - timedelta(hours=9), "Hiring plan"),
                ("budget.txt", TRANSCRIPT_EN, midnight - timedelta(days=3, hours=-10), "Budget review"),
                ("kickoff.txt", TRANSCRIPT_EN, midnight - timedelta(days=20, hours=-14), None)]
        for name, text, when, title in plan:
            src = self.root / name
            src.write_text(text, encoding="utf-8")
            args = ["-c", str(self.config_path), "import", str(src), "--me", "Camille", "--date", when.strftime("%Y-%m-%dT%H:%M")]
            if title:
                args += ["--title", title]
            # its own thread: the import runs asyncio.run(), and Playwright keeps a loop on this one
            done: list[int] = []
            worker = threading.Thread(target=lambda a=args: done.append(main(a)))
            worker.start()
            worker.join(60)
            assert done == [0]
        for folder in list_sessions(self.root / "data"):
            meeting, _ = load_meeting(folder)
            if meeting.title == "Budget review":  # Claude's output, as the UI would get it — hostile on purpose
                meeting.tldr = "Budget review in short"
                meeting.followup_email = FollowUpEmail(
                    subject="Budget review: next steps", to=["sam@example.com", "alex@example.com"],
                    body_markdown="Hi both,\n\nThanks for today. **Next steps:**\n\n- Camille sends the budget by Friday\n- Sam books the room\n\nCamille")
                atomic_write(folder / "meeting.json", meeting.model_dump_json(indent=1))
                atomic_write(folder / "notes.md", HOSTILE_NOTES)

    def folder_of(self, title: str) -> str:
        from voicestream.storage import list_sessions, load_meeting

        for folder in list_sessions(self.root / "data"):
            if load_meeting(folder)[0].title == title:
                return folder.name
        raise KeyError(title)

    # ----------------------------------------------------------------- speech

    def say(self, text: str, source: str = "mic") -> None:
        """A finished transcript line, as if the transcriber had just heard it."""
        async def route() -> None:
            a = self.host.assistant
            assert a is not None, "not listening"
            now = a.clock.now()
            seg = self.TranscriptSegment(source=source, speaker=self.host.cfg.source_label(source), text=text,
                                         t_start=max(0.0, now - 2.0), t_end=now)
            await a._route(seg)

        self.host.call(route()).result(10)

    def partial(self, text: str, source: str = "system") -> None:
        assert self.host.loop is not None
        self.host.loop.call_soon_threadsafe(lambda: self.host.bus.publish(
            "transcript.partial", source=source, speaker=self.host.cfg.source_label(source), text=text))

    def spend_history(self) -> None:
        """A few days of spending on disk (what the cost meter writes), for the Costs chart."""
        spend = self.root / "data" / "spend"
        spend.mkdir(parents=True, exist_ok=True)
        today = datetime.now().date()
        for days, (stt, llm, agents) in {1: (0.0, 0.21, 0.0), 2: (0.12, 0.34, 0.25), 5: (0.0, 0.18, 0.0),
                                         8: (0.05, 0.42, 1.1), 13: (0.0, 0.09, 0.0), 20: (0.0, 0.27, 0.4)}.items():
            day = (today - timedelta(days=days)).isoformat()
            (spend / f"{day}.json").write_text(json.dumps({"day": day, "stt_usd": stt, "llm_usd": llm, "agents_usd": agents}))

    def close(self) -> None:
        try:
            self.host.call(self.host.shutdown(force=True)).result(30)
        finally:
            if self.host.thread is not None:
                self.host.thread.join(10)


class Page:
    """A browser tab on the app that fails the test on any console error or uncaught exception."""

    def __init__(self, browser: Any, app: UIApp, *, locale: str = "en-US", scheme: str = "light",
                 size: tuple[int, int] = (1280, 800)) -> None:
        self.ctx = browser.new_context(viewport={"width": size[0], "height": size[1]}, locale=locale, color_scheme=scheme)
        self.ctx.grant_permissions(["clipboard-read", "clipboard-write"], origin=app.url.split("/app")[0])
        self.page = self.ctx.new_page()
        self.errors: list[str] = []
        self.allowed: set[int] = set()
        self.page.on("console", self._console)
        self.page.on("pageerror", lambda exc: self.errors.append(f"uncaught: {exc} {getattr(exc, 'stack', '')}"))
        self.page.goto(app.url)
        self.page.locator('#boot').wait_for(state='hidden')
        self.shots = os.environ.get("VOICESTREAM_UI_SHOTS")

    def _console(self, msg: Any) -> None:
        if msg.type != "error":
            return
        m = re.search(r"status of (\d+)", msg.text)
        if m and int(m.group(1)) in self.allowed:  # an error answer the test asked for
            return
        self.errors.append(msg.text)

    def shot(self, name: str) -> None:
        if self.shots:
            Path(self.shots).mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(Path(self.shots) / f"{name}.png"))

    def close(self) -> None:
        errors = list(self.errors)
        self.ctx.close()
        assert not errors, f"console errors: {errors}"


@pytest.fixture
def make_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., UIApp]]:
    apps: list[UIApp] = []

    def make(**kw: Any) -> UIApp:
        app = UIApp(tmp_path, monkeypatch, **kw)
        apps.append(app)
        return app

    yield make
    for app in apps:
        app.close()


@pytest.fixture
def open_page(browser: Any) -> Iterator[Callable[..., Page]]:
    pages: list[Page] = []

    def make(app: UIApp, **kw: Any) -> Page:
        p = Page(browser, app, **kw)
        pages.append(p)
        return p

    yield make
    for p in pages:
        p.close()


def _wait(pred: Callable[[], bool], timeout: float = 10.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        time.sleep(0.05)


def _wait_state(app: UIApp, *states: str, timeout: float = 10.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    while app.host.status not in states:
        if time.monotonic() > deadline:
            raise AssertionError(f"host state stayed {app.host.status!r}")
        time.sleep(0.05)


# --------------------------------------------------------------------------- tests


def test_setup_wizard_end_to_end(make_app: Callable[..., UIApp], open_page: Callable[..., Page]) -> None:
    from playwright.sync_api import expect

    app = make_app(setup_done=False, user="Me")
    app.dl_gate.clear()  # hold the model download at 90% so its progress can be seen
    p = open_page(app)
    pg = p.page
    expect(pg.locator("#setup")).to_be_visible()
    expect(pg.locator("#shell")).to_be_hidden()
    expect(pg.locator("#setup-h")).to_have_text("Welcome to Voicestream")
    p.shot("setup-1-welcome")
    pg.click("#setup-next")

    # name and languages: a name is required
    expect(pg.locator("#setup-h")).to_have_text("About you")
    pg.click("#setup-next")
    expect(pg.locator("#setup-error")).to_contain_text("enter your name")
    pg.fill("#setup-name", "Camille Martin")
    expect(pg.get_by_label("French and English")).to_be_checked()  # the default for a new install
    p.shot("setup-2-you")
    pg.click("#setup-next")

    # speech-to-text: on this Mac is chosen, and the model starts downloading at once
    expect(pg.locator("#setup-h")).to_have_text("Speech-to-text")
    progress = pg.locator("#setup-stt [data-dl='large-v3-turbo-q5_0'] .pct")
    expect(progress).to_contain_text("%")
    expect(pg.locator("#setup-foot [data-dl]")).to_be_visible()  # also in the footer, on every later step
    p.shot("setup-3-stt")
    pg.click("#setup-next")

    # Claude: a key is needed to continue (or skip); it is saved, then checked
    expect(pg.locator("#setup-h")).to_have_text("Connect Claude")
    expect(pg.locator("#where-claude")).to_have_attribute("href", "https://console.anthropic.com/settings/keys")
    pg.click("#setup-next")
    expect(pg.locator("#setup-error")).to_contain_text("Save your key first")
    pg.fill("#key-ANTHROPIC_API_KEY", "sk-ant-test-0123456789")
    pg.get_by_role("button", name="Save key").click()
    expect(pg.locator("#ks-ANTHROPIC_API_KEY")).to_have_text("Saved on this computer")  # no Keychain here: the file store
    expect(pg.locator(".keyfield .result.ok")).to_contain_text("Claude accepted the key")
    assert "sk-ant-test-0123456789" not in pg.content()  # never shown back
    p.shot("setup-4-claude")
    pg.click("#setup-next")

    # permissions
    expect(pg.locator("#setup-h")).to_have_text("Permissions")
    pg.click("#perm-microphone")
    expect(pg.locator("[data-perm=microphone] .result.ok")).to_contain_text("can hear your microphone")
    pg.click("#perm-system-audio")
    expect(pg.locator("[data-perm=system-audio] .result.unknown")).to_contain_text("Nothing was heard")
    p.shot("setup-5-permissions")
    pg.click("#setup-next")

    # tools: Reminders (a Mac-only tool, so off by default here) and the list it uses; Continue checks
    # it at once, so macOS asks for the Automation permission now rather than at a meeting's wrap-up
    expect(pg.locator("#setup-h")).to_have_text("Your tools")
    pg.check("#setup-reminders")
    pg.fill("#setup-list", "Réunions")
    p.shot("setup-6-tools")
    pg.click("#setup-next")
    expect(pg.locator("#setup-reminders-check")).to_contain_text("click OK")
    p.shot("setup-6-tools-checking")

    # how meetings start: as saved (this app listens all the time), with its own switch
    expect(pg.locator("#setup-h")).to_have_text("How you start a meeting")
    expect(pg.locator("input[name='c-How you start a meeting'][value='automatic']")).to_be_checked()
    expect(pg.locator("#setup-launch")).to_be_visible()
    p.shot("setup-7-start")
    pg.click("#setup-next")

    # done: the download is still running, so listening starts once it has finished
    expect(pg.locator("#setup-h")).to_have_text("You’re all set, Camille Martin")
    expect(pg.locator("#setup-next")).to_have_text("Start listening when the download is done")
    p.shot("setup-8-done")
    pg.click("#setup-next")
    expect(pg.locator("#shell")).to_be_visible()
    expect(pg.locator("#setup")).to_be_hidden()
    assert app.host.prefs.setup_done
    app.dl_gate.set()
    _wait_state(app, "listening")
    expect(pg.locator("#state-pill")).to_contain_text("Listening")
    expect(pg.locator("#live-heading")).to_have_text("Listening for a meeting to start")

    cfg = app.host.cfg
    assert cfg.user_name == "Camille Martin"
    assert cfg.transcription.backend == "whisper-cpp" and cfg.transcription.whisper_cpp.model == "large-v3-turbo-q5_0"
    assert cfg.transcription.whisper_cpp.allowed_languages == ["fr", "en"] and cfg.brain.provider == "anthropic"
    assert cfg.sinks["reminders"] == {"enabled": True, "list_name": "Réunions"}
    assert app.reminders_checks == ["Réunions"]
    assert stt_models.is_installed(cfg, "large-v3-turbo-q5_0")
    assert "ANTHROPIC_API_KEY" not in app.config_path.read_text(encoding="utf-8")


def test_listening_and_the_live_meeting(make_app: Callable[..., UIApp], open_page: Callable[..., Page]) -> None:
    from playwright.sync_api import expect

    app = make_app(setup_done=True)
    p = open_page(app)
    pg = p.page
    expect(pg.locator("#state-pill")).to_contain_text("Not listening")
    expect(pg.locator("#live-heading")).to_have_text("Not listening")
    p.shot("live-stopped")

    # start / pause / resume from the top bar
    pg.click("#primary")
    expect(pg.locator("#state-pill")).to_have_class(re.compile(r"st-listening"))
    expect(pg.locator("#primary")).to_have_text("Pause")
    expect(pg.locator(".meter").first).to_be_visible()  # level meters while listening
    pg.focus("#primary")  # from the keyboard: the button keeps the focus while the bar is redrawn
    pg.keyboard.press("Enter")
    expect(pg.locator("#state-pill")).to_contain_text("Paused — nothing is being recorded")
    assert app.host.assistant is not None and app.host.assistant.paused
    expect(pg.locator("#primary")).to_have_text("Resume")
    expect(pg.locator("#primary")).to_be_focused()
    pg.keyboard.press("Enter")
    expect(pg.locator("#state-pill")).to_have_class(re.compile(r"st-listening"))
    expect(pg.locator("#primary")).to_be_focused()
    assert not app.host.assistant.paused
    p.shot("live-idle")

    # a meeting starts on the first speech; lines, partial captions, then items
    app.say("Bonjour, on fait le point sur le budget.")
    expect(pg.locator("#meeting-heading")).to_be_visible()
    expect(pg.locator("#state-pill")).to_contain_text("Meeting in progress")
    # the reply to Resume, made just before the meeting started, can arrive after the pushed state
    # that says it did: it used to put "Listening" back for the whole meeting
    pg.evaluate("setApp({ ...S.app, rev: S.app.rev - 1, in_meeting: false, meeting: null })")
    expect(pg.locator("#state-pill")).to_contain_text("Meeting in progress")
    app.say("Je vais envoyer le budget à Sam d'ici vendredi.")
    app.say("We decided to keep the price at 29 euros.", source="system")
    app.say("Hey Claude, compare the pricing of Globex and Initech.")
    expect(pg.locator("#lines .seg")).to_have_count(4)
    expect(pg.locator("#lines .seg.me .spk").first).to_have_text("Me")
    expect(pg.locator("#lines .seg.others .spk")).to_have_text("Others")
    app.partial("And one more thing about the")
    expect(pg.locator("#partials .seg.partial")).to_contain_text("one more thing")
    pg.click("#extract")
    expect(pg.locator("[data-kind=action] .item")).to_have_count(2)
    expect(pg.locator("[data-kind=decision] .item")).to_have_count(1)
    p.shot("live-meeting")

    # edit an item
    card = pg.locator("[data-kind=action] .item", has_text="budget")
    card.get_by_role("button", name="Edit").click()
    pg.locator("form.edit input").first.fill("Send the budget to Sam")
    pg.locator("form.edit").get_by_role("button", name="Save").click()
    expect(pg.locator("[data-kind=action] .item", has_text="Send the budget to Sam")).to_be_visible()
    # send it to the tools (the local files here)
    pg.locator(".item", has_text="Send the budget to Sam").get_by_role("button", name="Send").click()
    expect(pg.locator(".toast.ok", has_text="Sent")).to_be_visible()
    expect(pg.locator(".item", has_text="Send the budget to Sam").locator(".badge")).to_contain_text("Files on this Mac")
    # the agent suggestion: run it with the suggested brief
    agent_card = pg.locator(".item", has_text="Globex")
    expect(agent_card.locator(".chip")).to_have_text("suggested")
    agent_card.get_by_role("button", name="Let an agent do it…").click()
    agent_card.get_by_role("button", name="Start").click()
    expect(agent_card.locator(".chip")).to_have_text("done", timeout=15000)
    expect(agent_card.locator(".summary")).to_contain_text("Globex is about 20% cheaper")
    # dismiss and restore the decision
    decision = pg.locator("[data-kind=decision] .item")
    decision.get_by_role("button", name="Dismiss").click()
    expect(decision).to_be_hidden()
    pg.check("#live-show-dismissed")
    pg.locator("[data-kind=decision] .item").get_by_role("button", name="Restore").click()
    expect(pg.locator("[data-kind=decision] .item.dismissed")).to_have_count(0)
    # add one by hand
    form = pg.locator("#screen-live form.add")
    form.locator("select").select_option("question")
    form.get_by_placeholder("Add one by hand…").fill("Who presents on Monday?")
    form.get_by_role("button", name="Add").click()
    expect(pg.locator("[data-kind=question] .item", has_text="Who presents on Monday?")).to_be_visible()

    # another agent suggestion, declined
    app.say("Hey Claude, draft the agenda for the next workshop.")
    pg.click("#extract")
    other = pg.locator(".item", has_text="agenda")
    other.get_by_role("button", name="No thanks").click()
    expect(other.locator(".chip")).to_have_text("declined")

    # end the meeting: the page goes back to waiting, with the meeting that just ended
    pg.click("#end-meeting")
    pg.click("#ask-ok")
    expect(pg.locator(".toast", has_text="The notes are ready")).to_be_visible(timeout=15000)
    expect(pg.locator("#live-heading")).to_have_text("Listening for a meeting to start")
    expect(pg.locator("#last-meeting")).to_contain_text("Just ended")

    # stop from the top bar
    pg.click("#stop")
    expect(pg.locator("#state-pill")).to_contain_text("Not listening")
    assert app.host.assistant is None


def test_new_settings_wait_for_the_meeting_to_end(make_app: Callable[..., UIApp], open_page: Callable[..., Page]) -> None:
    """A change saved during a meeting applies after it; the notice offers to apply it now (ending the meeting)."""
    from playwright.sync_api import expect

    app = make_app(setup_done=True, listen_on_launch=True)
    _wait_state(app, "listening")
    p = open_page(app)
    pg = p.page
    app.say("Let's start with the roadmap.")
    expect(pg.locator("#meeting-heading")).to_be_visible()
    pg.click("nav a[href='#/settings']")
    pg.fill("#f-user_name", "Camille Martin")
    pg.click("#save-settings")
    expect(pg.locator(".toast", has_text="They apply when the meeting in progress ends")).to_be_visible()
    notice = pg.locator("#notices .notice", has_text="Your new settings will apply when this meeting ends")
    expect(notice).to_be_visible()
    p.shot("notice-restart-pending")
    first = app.host.assistant
    pg.click("#apply-now")
    expect(pg.locator("#ask-title")).to_have_text("Apply the new settings now?")
    pg.click("#ask-ok")
    expect(notice).to_be_hidden(timeout=15000)
    _wait_state(app, "listening")
    assert app.host.assistant is not first and not app.host.in_meeting()
    expect(pg.locator("#state-pill")).to_have_class(re.compile(r"st-listening"))


def test_meetings_list_detail_and_editing(make_app: Callable[..., UIApp], open_page: Callable[..., Page]) -> None:
    from playwright.sync_api import expect

    app = make_app(setup_done=True, meetings=True)
    p = open_page(app)
    pg = p.page
    pg.click("nav a[href='#/meetings']")
    expect(pg.locator("#dg-today")).to_have_text("Today")
    expect(pg.locator("#dg-yesterday")).to_have_text("Yesterday")
    expect(pg.locator("#dg-week")).to_have_text("This week")
    expect(pg.locator("#dg-earlier")).to_have_text("Earlier")
    expect(pg.locator(".mrow")).to_have_count(4)
    expect(pg.locator(".mrow", has_text="Hiring plan")).to_contain_text("to-do")
    p.shot("meetings-list")
    pg.fill("#meeting-search", "hiring")
    expect(pg.locator(".mrow")).to_have_count(1)
    pg.fill("#meeting-search", "nothing like this")
    expect(pg.locator("#meeting-list")).to_contain_text("No meeting matches")
    pg.fill("#meeting-search", "")
    expect(pg.locator(".mrow")).to_have_count(4)

    # notes, rendered from markdown without letting any markup through
    pg.locator(".mrow", has_text="Budget review").click()
    expect(pg.locator(".detail-head h1")).to_have_text("Budget review")
    notes = pg.locator("#notes")
    expect(notes.locator("h2").first).to_have_text("Action items")
    expect(notes.locator("li.task input[type=checkbox]")).to_have_count(2)
    expect(notes.locator("li.task input[type=checkbox]").nth(1)).to_be_checked()
    expect(notes.locator("strong").first).to_have_text("Send the budget")
    expect(notes.locator("a[href='https://example.com/budget']")).to_have_text("a link")
    assert notes.locator("img, script, [onerror]").count() == 0
    assert notes.locator("a").count() == 1  # the javascript: link stayed text
    expect(notes).to_contain_text('<img src=x onerror="window.pwned=1">')
    assert pg.evaluate("window.pwned") is None
    expect(notes.locator("code")).to_have_text("code <b>")
    p.shot("meeting-notes")

    # to-dos: edit one; the change is saved in the meeting on disk
    pg.click("#tab-actions")
    expect(pg.locator("#detail-panel [data-kind=action] .item").first).to_be_visible()
    p.shot("meeting-actions")
    first = pg.locator("#detail-panel [data-kind=action] .item").first
    first.get_by_role("button", name="Edit").click()
    pg.locator("#detail-panel form.edit input").first.fill("Send the final budget")
    pg.locator("#detail-panel form.edit").get_by_role("button", name="Save").click()
    expect(pg.locator("#detail-panel .item", has_text="Send the final budget")).to_be_visible()
    pg.locator("#detail-panel .item", has_text="Send the final budget").get_by_role("button", name="Done").click()
    expect(pg.locator("#detail-panel .item.done", has_text="Send the final budget")).to_be_visible()
    from voicestream.storage import load_meeting

    meeting, _ = load_meeting(app.root / "data" / "sessions" / app.folder_of("Budget review"))
    edited = [i for i in meeting.items.values() if i.title == "Send the final budget"]
    assert edited and edited[0].status.value == "done"
    # the keyboard moves between tabs
    pg.focus("#tab-actions")
    pg.keyboard.press("ArrowRight")
    expect(pg.locator("#tab-transcript")).to_be_focused()
    expect(pg.locator("#tab-transcript")).to_have_attribute("aria-selected", "true")
    expect(pg.locator(".tline").first).to_be_visible()
    pg.get_by_placeholder("Find in the transcript…").fill("Thursday")
    expect(pg.locator(".tline")).to_have_count(1)
    p.shot("meeting-transcript")

    # the follow-up e-mail: copy, and a mailto: link for Mail
    pg.click("#tab-email")
    expect(pg.locator(".email-subject")).to_have_text("Budget review: next steps")
    href = pg.locator("#mail-link").get_attribute("href")
    assert href.startswith("mailto:sam@example.com,alex@example.com?subject=Budget%20review%3A%20next%20steps&body=Hi%20both")
    pg.click("#copy-email")
    expect(pg.locator(".toast", has_text="on the clipboard")).to_be_visible()
    assert pg.evaluate("navigator.clipboard.readText()").startswith("Budget review: next steps\n\nHi both")
    p.shot("meeting-email")

    # push to the tools, and rewrite the notes (offline here: quick)
    pg.click("#push-meeting")
    expect(pg.locator(".toast.ok", has_text="Sent to")).to_be_visible()  # the clipboard's toast may still show
    pg.click("#rewrite-notes")
    expect(pg.locator(".toast", has_text="The notes were rewritten")).to_be_visible(timeout=15000)
    pg.click("#tab-notes")
    expect(pg.locator("#notes")).to_contain_text("Send the final budget")


def test_settings_save_validation_and_model_download(make_app: Callable[..., UIApp], open_page: Callable[..., Page]) -> None:
    from playwright.sync_api import expect

    app = make_app(setup_done=True)
    p = open_page(app)
    p.allowed.add(400)  # the refused save below
    pg = p.page
    pg.click("nav a[href='#/settings']")
    expect(pg.locator("#f-user_name")).to_have_value("Camille")
    expect(pg.locator("#savebar")).to_be_hidden()
    p.shot("settings-general")
    pg.fill("#f-user_name", "Camille Martin")
    pg.get_by_label("French", exact=True).check()
    expect(pg.locator("#savebar")).to_be_visible()
    expect(pg.locator("nav a[href='#/settings'] .badge-dot")).to_be_visible()
    pg.click("#save-settings")
    expect(pg.locator(".toast", has_text="Your settings are saved").last).to_be_visible()
    expect(pg.locator("#savebar")).to_be_hidden()
    assert app.host.cfg.user_name == "Camille Martin" and app.host.cfg.transcription.language == "fr"

    # a value the settings refuse: shown under its field, nothing written
    pg.click(".subnav a[href='#/settings/tools']")
    pg.check("#f-sinks_reminders_enabled")
    pg.fill("#f-sinks_reminders_list_name", "")
    pg.click("#save-settings")
    error = pg.locator("[data-path='sinks.reminders.list_name'] .field-error")
    expect(error).to_have_text("This can't be empty.")
    expect(pg.locator("#savebar .msg.error")).to_contain_text("Nothing was saved")
    expect(pg.locator("#f-sinks_reminders_list_name")).to_be_focused()
    p.shot("settings-tools-error")
    assert "reminders" not in app.config_path.read_text(encoding="utf-8")
    pg.fill("#f-sinks_reminders_list_name", "Work")
    expect(error).to_have_text("")
    pg.click("#save-settings")
    expect(pg.locator(".toast", has_text="Your settings are saved").last).to_be_visible()
    # the first save's toast may still be up: wait for this save itself
    _wait(lambda: app.host.cfg.sinks.get("reminders") == {"enabled": True, "list_name": "Work"})
    # just turned on: checked at once (macOS asks for the Automation permission while you are here)
    expect(pg.locator("#test-reminders")).to_contain_text("Works: Reminders reachable")
    assert app.reminders_checks == ["Work"]

    # speech-to-text on this Mac: download a model and watch its progress
    pg.click(".subnav a[href='#/settings/stt']")
    pg.get_by_label("On this Mac — free & private").check()
    app.dl_gate.clear()
    pg.get_by_role("button", name="Download Lightest").click()
    pct = pg.locator("[data-model=base] .pct")
    expect(pct).to_contain_text("90%")
    expect(pg.locator("[data-model=base] [role=progressbar]")).to_have_attribute("aria-valuenow", "90")
    p.shot("settings-stt-downloading")
    app.dl_gate.set()
    expect(pg.locator("[data-model=base] .pill.ok")).to_have_text("Downloaded")
    expect(pg.locator(".toast", has_text="“Lightest” is ready")).to_be_visible()
    pg.locator("[data-model=base] label.pick").click()
    pg.click("#save-settings")
    expect(pg.locator(".toast", has_text="Your settings are saved").last).to_be_visible()
    # an earlier save's toast may still be up: wait for this save itself
    _wait(lambda: (app.host.cfg.transcription.backend, app.host.cfg.transcription.whisper_cpp.model) == ("whisper-cpp", "base"))

    # the other sections render
    for sec, heading in (("claude", "Claude"), ("audio", "Audio"), ("agents", "Agents"), ("advanced", "Files")):
        pg.click(f".subnav a[href='#/settings/{sec}']")
        expect(pg.locator("#settings-body h2").first).to_have_text(heading)
        p.shot(f"settings-{sec}")
    pg.click("#run-doctor")
    expect(pg.locator("#doctor .check").first).to_be_visible(timeout=20000)

    # the app's language is a preference: switching it redraws everything in French
    pg.click(".subnav a[href='#/settings/general']")
    pg.select_option("#p-ui_language", "fr")
    pg.click("#save-settings")
    expect(pg.locator("nav a[href='#/settings']")).to_contain_text("Réglages")
    expect(pg.locator("#state-pill")).to_contain_text("À l’arrêt")
    assert app.host.prefs.ui_language == "fr"


def test_costs_page(make_app: Callable[..., UIApp], open_page: Callable[..., Page]) -> None:
    from playwright.sync_api import expect

    app = make_app(setup_done=True, provider="anthropic")  # Claude's notes cost something: the estimate moves with the hours
    app.spend_history()
    p = open_page(app)
    pg = p.page
    pg.click("#spend")  # today's spend in the top bar opens Costs
    expect(pg.locator("#screen-costs h1")).to_have_text("Costs")
    expect(pg.locator(".chart .col")).to_have_count(30)
    tall = pg.locator(".chart .col").nth(29 - 8)
    assert "$1.57 " in tall.get_attribute("aria-label")
    tall.hover()
    expect(pg.locator("#tooltip")).to_be_visible()
    expect(pg.locator("#tooltip")).to_contain_text("$1.10")
    tall.focus()  # the same details from the keyboard
    expect(pg.locator("#tooltip")).to_be_visible()
    p.shot("costs")
    day = pg.locator("#estimate-day").inner_text()
    pg.locator("#hours").fill("8")
    expect(pg.locator("#hours-out")).to_have_text("8 h")
    expect(pg.locator("#estimate-day")).not_to_have_text(day)
    pg.locator("summary", has_text="Show as a table").click()
    expect(pg.locator("table.data tbody tr")).to_have_count(6)


def test_french_ui_follows_the_system_language(make_app: Callable[..., UIApp], open_page: Callable[..., Page]) -> None:
    from playwright.sync_api import expect

    app = make_app(setup_done=False)
    p = open_page(app, locale="fr-FR", scheme="dark", size=(900, 640))
    pg = p.page
    expect(pg.locator("#setup-h")).to_have_text("Bienvenue dans Voicestream")
    assert pg.evaluate("document.documentElement.lang") == "fr"
    pg.click("#setup-next")
    expect(pg.locator("#setup-h")).to_have_text("À propos de vous")
    # nothing scrolls sideways in the smallest window
    assert pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    # the permissions step: the longer French words wrap beside their icon, each Test button stays on its row
    pg.evaluate("sessionStorage.setItem('voicestream-setup-step', '4')")
    pg.reload()
    pg.locator('#boot').wait_for(state='hidden')
    expect(pg.locator("#setup-h")).to_have_text("Autorisations")
    assert pg.evaluate(PERM_ROWS_HOLD) == [True, True]


PERM_ROWS_HOLD = """() => [...document.querySelectorAll('.perm')].map((row) => {
  const [icon, text, button] = [':scope > .icon', ':scope > .grow', ':scope > .btn']
    .map((sel) => row.querySelector(sel).getBoundingClientRect());
  return icon.right <= text.left && text.right <= button.left  // one line: icon, words, button
    && icon.top < text.bottom && button.top < text.bottom && text.top < button.bottom;
})"""
"""For each permission row: are its icon, its words and its Test button side by side on one line?"""


def _contrast(fg: str, bg: str) -> float:
    """The WCAG contrast of two computed colours (``rgb(r, g, b)``)."""
    def luminance(css: str) -> float:
        channels = [float(v) / 255 for v in re.findall(r"[\d.]+", css)[:3]]
        r, g, b = (c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    hi, lo = sorted((luminance(fg), luminance(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_a_meeting_in_the_smallest_mac_window(make_app: Callable[..., UIApp], open_page: Callable[..., Page]) -> None:
    """The Mac window stops at MIN_SIZE (900×640; its web view as wide, and a title bar shorter). That is
    still the window's layout, not the phone's: the sidebar with its screens and the meeting's clock,
    and the meeting's two panes side by side with the to-dos in view, only the panes scrolling. And the
    voice strip grows a minute at a time: each new length lays out every one of its bars again."""
    from itertools import pairwise

    from playwright.sync_api import expect

    from voicestream.app.native import MIN_SIZE

    app = make_app(setup_done=True, listen_on_launch=True)
    _wait_state(app, "listening")
    p = open_page(app, size=MIN_SIZE)
    pg = p.page
    app.say("Bonjour, on fait le point sur le budget.")
    app.say("Je vais envoyer le budget à Sam d'ici vendredi.")
    app.say("We decided to keep the price at 29 euros.", source="system")
    expect(pg.locator("#meeting-heading")).to_be_visible()
    pg.click("#extract")
    expect(pg.locator("#screen-live [data-kind=action] .item").first).to_be_visible()
    layout = """() => {
      const panes = document.querySelector('#screen-live .panes');
      const [transcript, items] = [...panes.querySelectorAll(':scope > .pane')].map((el) => el.getBoundingClientRect());
      const page = document.documentElement;
      return { columns: getComputedStyle(panes).gridTemplateColumns.split(' ').length, transcript, items,
               fits: page.scrollHeight <= innerHeight && page.scrollWidth <= innerWidth, height: innerHeight };
    }"""
    for width, height in (MIN_SIZE, (MIN_SIZE[0], MIN_SIZE[1] - 28)):
        pg.set_viewport_size({"width": width, "height": height})
        expect(pg.locator("#nav")).to_be_visible()
        expect(pg.locator("#menu-btn")).to_be_hidden()
        expect(pg.locator("#state-extra")).to_contain_text("Meeting in progress")
        got = pg.evaluate(layout)
        assert got["columns"] == 2 and got["fits"], got
        assert got["items"]["top"] == got["transcript"]["top"] and got["items"]["bottom"] <= got["height"], got
    p.shot("live-meeting-smallest-window")
    # narrower, in a browser: the sidebar folds into a bar; the panes stay side by side down to the phone
    pg.set_viewport_size({"width": 760, "height": 600})
    expect(pg.locator("#menu-btn")).to_be_visible()
    got = pg.evaluate(layout)
    assert got["columns"] == 2 and got["fits"] and got["items"]["bottom"] <= got["height"], got
    pg.set_viewport_size({"width": 390, "height": 844})
    assert pg.evaluate(layout)["columns"] == 1

    # three and a half minutes in: the strip is 4 minutes long until the clock passes them
    pg.set_viewport_size({"width": MIN_SIZE[0], "height": MIN_SIZE[1]})
    pg.evaluate("() => { const start = Date.now() - 210000; meetingElapsed = () => (Date.now() - start) / 1000; }")
    totals = []
    pg.wait_for_timeout(1100)  # the clock ticks every second
    for _ in range(4):
        pg.wait_for_timeout(700)
        plot = pg.locator("#screen-live .voices-plot")
        totals.append(float(plot.evaluate("(el) => getComputedStyle(el).getPropertyValue('--total')")))
    assert totals == [240.0] * 4, totals
    # a whole number of minutes puts a label at the strip's end: on a phone, 3 or 4 labels, apart
    pg.set_viewport_size({"width": 390, "height": 844})
    labels = pg.evaluate("""() => [...document.querySelectorAll('#screen-live .axis > span')].filter((el) => el.offsetParent)
      .map((el) => [el.getBoundingClientRect().left, el.getBoundingClientRect().right])""")
    assert 3 <= len(labels) <= 4 and all(a[1] + 8 <= b[0] for a, b in pairwise(labels)), labels


def test_a_meeting_page_in_the_mac_window(make_app: Callable[..., UIApp], open_page: Callable[..., Page]) -> None:
    """At the window's default size the facts go under the tabs: the title's line then says how long,
    in what language and with whom, and the facts start on the cards' edge. One filled button on the
    screen, the recorder's; the words a to-do came from have a straight edge; redrawing the page draws
    the voice strip's legend once and makes no new number formatter."""
    from playwright.sync_api import expect

    from voicestream.app.native import WINDOW_SIZE
    from voicestream.storage import atomic_write, load_meeting

    app = make_app(setup_done=True, meetings=True)
    folder = app.folder_of("Budget review")
    meeting, path = load_meeting(app.root / "data" / "sessions" / folder)
    meeting.participants, meeting.language = ["Camille", "Sam Lefèvre"], "en"  # as the wrap-up finds them
    atomic_write(path / "meeting.json", meeting.model_dump_json(indent=1))
    p = open_page(app, size=WINDOW_SIZE)
    pg = p.page
    pg.goto(app.url + f"#/meetings/{folder}/notes")
    pg.locator('#boot').wait_for(state='hidden')
    meta = pg.locator(".detail-head .meta-line")
    extra = meta.locator(".facts-here")  # what the facts say, there too
    expect(extra).to_have_text(["·under a minute", "·English", "·with Camille, Sam Lefèvre"])
    for i in range(3):
        expect(extra.nth(i)).to_be_visible()
    box = meta.bounding_box()
    assert box and box["y"] + box["height"] <= WINDOW_SIZE[1]  # on the first screen
    facts, tabs = pg.locator(".facts h2").bounding_box(), pg.locator(".tabwrap").bounding_box()
    assert facts and tabs and facts["y"] > tabs["y"] + tabs["height"] and abs(facts["x"] - tabs["x"]) < 1
    for tab in ("notes", "actions", "transcript", "email"):
        pg.click(f"#tab-{tab}")
        expect(pg.locator("#tab-" + tab)).to_have_attribute("aria-selected", "true")
        assert pg.locator(".btn.primary:visible").count() == 1, tab
        expect(pg.locator("#primary")).to_have_class(re.compile(r"\bprimary\b"))
    pg.click("#tab-transcript")
    said = pg.locator(".tline.said").first
    expect(said).to_be_visible()
    corners = said.evaluate("(el) => [getComputedStyle(el).borderTopLeftRadius, getComputedStyle(el).borderBottomLeftRadius]")
    assert corners == ["0px", "0px"]

    counted = pg.evaluate("""() => {
      const made = { formats: 0, plurals: 0, legends: 0 };
      const count = (Kind, key) => new Proxy(Kind, { construct(target, args) { made[key]++; return new target(...args); } });
      Intl.NumberFormat = count(Intl.NumberFormat, 'formats');
      Intl.PluralRules = count(Intl.PluralRules, 'plurals');
      const legend = VoiceStrip.prototype.renderLegend;
      VoiceStrip.prototype.renderLegend = function () { made.legends++; return legend.call(this); };
      selectTab('transcript');  // once to make what this tab needs, then again: nothing new
      Object.assign(made, { formats: 0, plurals: 0, legends: 0 });
      selectTab('transcript');
      return made;
    }""")
    assert counted == {"formats": 0, "plurals": 0, "legends": 1}, counted

    # wide enough: the facts beside the tabs, and the title's line says only when
    pg.set_viewport_size({"width": 1440, "height": 900})
    for i in range(3):
        expect(extra.nth(i)).to_be_hidden()
    facts, tabs = pg.locator(".facts h2").bounding_box(), pg.locator(".tabwrap").bounding_box()
    assert facts and tabs and facts["x"] > tabs["x"] + tabs["width"] and facts["y"] < tabs["y"] + tabs["height"]


def test_settings_in_french_in_the_dark(make_app: Callable[..., UIApp], open_page: Callable[..., Page]) -> None:
    """Every settings section says what it is about beside its panel, Speech-to-text also what is in
    use; the permission rows keep their Test button on their line in French; and the button that
    confirms what cannot be undone reads at 4.5:1 or more, in the dark as in the light."""
    from playwright.sync_api import expect

    from voicestream.app.native import WINDOW_SIZE

    app = make_app(setup_done=True, backend="whisper-cpp", models=("large-v3-turbo-q5_0", stt_models.VAD_MODEL))
    p = open_page(app, locale="fr-FR", scheme="dark", size=WINDOW_SIZE)
    pg = p.page
    pg.click("nav a[href='#/settings']")
    for sec in ("general", "stt", "claude", "audio", "tools", "agents", "advanced"):
        pg.click(f".subnav a[href='#/settings/{sec}']")
        expect(pg.locator(f".subnav a[href='#/settings/{sec}']")).to_have_attribute("aria-current", "page")
        leads = pg.locator("#settings-body .set-label").evaluate_all(
            "(labels) => labels.map((el) => el.querySelector('.lead')?.textContent.trim() || '')")
        assert leads and all(leads), (sec, leads)
    pg.click(".subnav a[href='#/settings/general']")
    # the keyboard shortcut is one of the ways of starting a meeting: it has moved to that section
    expect(pg.locator(".set-label", has_text="Démarrer une réunion")).to_contain_text("comment démarrer une réunion")
    expect(pg.locator("#set-starting #shortcut-setting")).to_have_count(1)
    pg.click(".subnav a[href='#/settings/stt']")
    summary = pg.locator("#stt-summary")
    for words in ("Utilisé :", "Sur ce Mac — gratuit et privé", "Modèle : ", " Mo"):
        expect(summary).to_contain_text(words)

    pg.click(".subnav a[href='#/settings/audio']")
    expect(pg.locator(".perm")).to_have_count(2)
    assert pg.evaluate(PERM_ROWS_HOLD) == [True, True]
    p.shot("settings-audio-fr-dark")

    pg.click(".subnav a[href='#/settings/stt']")
    for scheme in ("dark", "light"):
        pg.emulate_media(color_scheme=scheme)
        pg.locator("[data-model='large-v3-turbo-q5_0'] .status .btn").click()  # Supprimer
        ok = pg.locator("#ask-ok")
        expect(ok).to_have_text("Supprimer")
        expect(ok).to_have_class(re.compile(r"\bdanger solid\b"))
        colours = ok.evaluate("(el) => [getComputedStyle(el).color, getComputedStyle(el).backgroundColor]")
        assert _contrast(*colours) >= 4.5, (scheme, colours)
        pg.click("#ask-cancel")
        expect(pg.locator("#ask")).to_be_hidden()


def test_costs_chart_in_french(make_app: Callable[..., UIApp], open_page: Callable[..., Page]) -> None:
    """The y ticks are figures only ("1,60", the subtitle says "in US dollars"): "1,60 $US" ran past
    its lane onto the gridlines, at every width."""
    from playwright.sync_api import expect

    app = make_app(setup_done=True)
    app.spend_history()
    p = open_page(app, locale="fr-FR")
    pg = p.page
    pg.click("#spend")
    expect(pg.locator(".chart .col")).to_have_count(30)
    expect(pg.locator(".history")).to_contain_text("Par jour, en dollars US")
    assert pg.locator(".chart .ytick").all_inner_texts() == ["0", "0,40", "0,80", "1,20", "1,60"]
    past_the_lane = """() => {
      const plot = document.querySelector('.chart .plot').getBoundingClientRect().left;
      return [...document.querySelectorAll('.chart .ytick')].map((tick) => {
        const words = document.createRange();
        words.selectNodeContents(tick);
        return words.getBoundingClientRect().right - plot;
      });
    }"""
    for width, height in ((1440, 900), (390, 844)):
        pg.set_viewport_size({"width": width, "height": height})
        over = pg.evaluate(past_the_lane)
        assert len(over) == 5 and max(over) <= -2, (width, over)  # clear of the gridlines' start


def test_the_week_counts_every_meeting_of_the_week(make_app: Callable[..., UIApp], open_page: Callable[..., Page]) -> None:
    """Always-on makes a meeting of each talk after ten quiet minutes, so a busy week holds more than a
    page of the meetings list: Live's "Last 7 days" counts every one of them, not the newest 30."""
    import shutil

    from playwright.sync_api import expect

    from voicestream.storage import atomic_write, load_meeting

    app = make_app(setup_done=True, meetings=True)
    sessions = app.root / "data" / "sessions"
    src = sessions / app.folder_of("Hiring plan")
    meeting, _ = load_meeting(src)
    for i in range(130):  # every quarter of an hour, from two days ago back
        shift = timedelta(days=2, minutes=15 * i)
        clone = meeting.model_copy(update={"id": f"{meeting.id}-{i:03d}", "title": f"Quick sync {i}",
                                           "started_at": meeting.started_at - shift,
                                           "ended_at": meeting.ended_at and meeting.ended_at - shift})
        dest = sessions / f"{clone.started_at.astimezone():%Y-%m-%d_%H%M%S}-quick-sync-{i:03d}"
        shutil.copytree(src, dest)
        atomic_write(dest / "meeting.json", clone.model_dump_json(indent=1))
    since = datetime.now().astimezone() - timedelta(days=7)
    rows = [r for r in app.host.meetings.list(limit=500)["meetings"] if datetime.fromisoformat(r["started_at"]) >= since]
    assert len(rows) == 133  # today's, yesterday's, three days ago and the 130
    want = {"meetings": len(rows), "actions": sum(r["counts"]["action"] for r in rows),
            "mine": sum(r["my_actions"] for r in rows), "decisions": sum(r["counts"]["decision"] for r in rows)}
    p = open_page(app)
    pg = p.page
    for key, n in want.items():
        expect(pg.locator(f"#live-week .figures .{key} dd")).to_have_text(f"{n:,}")


def test_markdown_renderer_never_creates_markup(make_app: Callable[..., UIApp], open_page: Callable[..., Page]) -> None:
    app = make_app(setup_done=True)
    p = open_page(app)
    result = p.page.evaluate("""() => {
      const md = '# T <img src=x onerror=alert(1)>\\n\\n- [ ] **b** [x](javascript:alert(1)) [y](data:text/html,1)\\n'
        + '  continued <svg onload=alert(1)>\\n\\n> q <iframe>\\n\\n```\\n<script>alert(1)</script>\\n```\\n'
        + 'see https://example.com/a?b=1. and <a href="https://evil">x</a> ~~gone~~ _it_ __b__';
      const frag = window.voicestream.renderMarkdown(md);
      const box = document.createElement('div');
      box.append(frag);
      const tags = [...box.querySelectorAll('*')].map((e) => e.tagName.toLowerCase());
      const attrs = [...box.querySelectorAll('*')].flatMap((e) => [...e.attributes].map((a) => a.name + '=' + a.value));
      return { tags: [...new Set(tags)].sort(), attrs, text: box.textContent };
    }""")
    assert set(result["tags"]) <= {"h1", "ul", "li", "input", "span", "strong", "br", "blockquote", "p", "pre", "code", "a", "del", "em"}
    links = [a for a in result["attrs"] if a.startswith("href=")]
    assert "href=https://example.com/a?b=1" in links  # a bare link, without the full stop after it
    assert all(a.startswith(("href=https://", "href=mailto:")) for a in links)
    assert not any("javascript" in a or "data:" in a for a in result["attrs"])
    assert all(a.split("=")[0] in ("href", "target", "rel", "class", "type", "disabled", "aria-label") for a in result["attrs"])
    assert "<img src=x onerror=alert(1)>" in result["text"] and "<script>alert(1)</script>" in result["text"]


def test_a_microphone_that_gives_up_is_a_warning_while_meeting_audio_records(make_app: Callable[..., UIApp],
                                                                             open_page: Callable[..., Page]) -> None:
    """The microphone gives up for good (here: the named one isn't there) while meeting audio is still
    recorded: listening goes on and the banner says what is missing — not an error state with half an
    assistant running, where Start did nothing and Dismiss ended the meeting."""
    from playwright.sync_api import expect

    app = make_app(setup_done=True, listen_on_launch=True, broken_mic=1)
    _wait(lambda: any("AirPods Pro" in w for w in app.host.state()["warnings"]))
    assert app.host.status == "listening" and app.host.assistant is not None and len(app.built) == 1
    p = open_page(app)
    pg = p.page
    expect(pg.locator("#state-pill")).to_have_class(re.compile(r"st-listening"))
    expect(pg.locator("#live-notices .notice.warn")).to_contain_text("AirPods Pro")
    p.shot("live-mic-gone")


def test_setup_wizard_with_a_tool_left_half_set_up_in_the_settings_file(make_app: Callable[..., UIApp],
                                                                        open_page: Callable[..., Page]) -> None:
    """A settings file from the terminal days turns Shortcuts on without naming a shortcut. The app
    runs anyway (that tool is skipped), so the wizard's name must save too, not fail forever with
    "Enter the name of at least one shortcut" under the name field."""
    from playwright.sync_api import expect

    app = make_app(setup_done=False, user="Me", config_extra="\n[sinks.shortcuts]\nenabled = true\n")
    p = open_page(app)
    pg = p.page
    pg.click("#setup-next")
    pg.fill("#setup-name", "Camille")
    pg.click("#setup-next")
    expect(pg.locator("#setup-h")).to_have_text("Speech-to-text")
    assert app.host.cfg.user_name == "Camille"
    assert app.host.cfg.sinks["shortcuts"] == {"enabled": True}  # left as it is: Settings → Your tools says what's missing


def test_setup_wizard_says_when_the_settings_file_itself_is_the_problem(make_app: Callable[..., UIApp],
                                                                       open_page: Callable[..., Page]) -> None:
    from playwright.sync_api import expect

    app = make_app(setup_done=False, user="Me", backend="nonsense")  # a typo in the file
    p = open_page(app)
    p.allowed.add(400)
    pg = p.page
    pg.click("#setup-next")
    pg.fill("#setup-name", "Camille")
    pg.click("#setup-next")
    expect(pg.locator("#setup-error")).to_contain_text("Your settings file has a problem with transcription.backend")
    expect(pg.locator("#setup-show-config")).to_have_text("Show the settings file")
    p.shot("setup-file-problem")


@pytest.mark.parametrize(("case", "expected"), [("new install", "whisper-cpp"), ("OpenAI set up before", "openai"),
                                                ("no on-device speech-to-text here", "openai")])
def test_setup_wizard_suggests_speech_to_text_on_this_mac(make_app: Callable[..., UIApp], open_page: Callable[..., Page],
                                                          case: str, expected: str) -> None:
    """A new install's settings say OpenAI without anyone choosing it: the wizard starts on this Mac
    (and its model download) unless OpenAI was really set up, or this computer can't run it."""
    from playwright.sync_api import expect

    if case == "OpenAI set up before":
        app = make_app(setup_done=False, user="Me", backend="openai", keys={"OPENAI_API_KEY": "sk-test-0123"})
    else:
        app = make_app(setup_done=False, fresh=True)
        assert app.host.cfg.transcription.backend == "openai"  # the built-in default
        if case == "no on-device speech-to-text here":
            app.host.capabilities["local_stt"] = False  # what the launcher says on an Intel Mac, or without whisper-server
    p = open_page(app)
    pg = p.page
    # reopened on the speech-to-text step (as after a reload): the choice and its download are there too
    pg.evaluate("sessionStorage.setItem('voicestream-setup-step', '2')")
    pg.reload()
    pg.locator('#boot').wait_for(state='hidden')
    expect(pg.locator("#setup-h")).to_have_text("Speech-to-text")
    expect(pg.locator(f"input[name='c-Speech-to-text'][value='{expected}']")).to_be_checked()
    if expected == "whisper-cpp":
        expect(pg.locator("#setup-stt [data-dl='large-v3-turbo-q5_0']")).to_be_visible()  # downloading already
        p.shot("setup-3-stt-new-install")
    else:
        assert pg.evaluate("Object.keys(S.downloads).length") == 0


def test_a_new_speech_model_is_used_once_it_has_downloaded(make_app: Callable[..., UIApp], open_page: Callable[..., Page]) -> None:
    """Picking a model that isn't downloaded: it downloads, listening goes on with the current one, and
    the switch happens when the download is done — not a restart on a model that can't run yet."""
    from playwright.sync_api import expect

    app = make_app(setup_done=True, listen_on_launch=True, backend="whisper-cpp",
                   models=("large-v3-turbo-q5_0", stt_models.VAD_MODEL))
    _wait_state(app, "listening")
    first = app.host.assistant
    p = open_page(app)
    pg = p.page
    pg.click("nav a[href='#/settings']")
    pg.click(".subnav a[href='#/settings/stt']")
    app.dl_gate.clear()
    pg.locator("[data-model=small] label.pick").click()  # not downloaded: picking it starts the download
    expect(pg.locator("[data-model=small] .pct")).to_contain_text("90%")
    pg.click("#save-settings")
    expect(pg.locator(".toast", has_text="keeps using the current speech-to-text")).to_be_visible()
    expect(pg.locator("#stt-held")).to_contain_text("“Faster” is used as soon as it has downloaded")
    expect(pg.locator("[data-model=small] input[type=radio]")).to_be_checked()
    p.shot("settings-stt-switch-after-download")
    assert app.host.status == "listening" and app.host.assistant is first
    assert app.host.cfg.transcription.whisper_cpp.model == "large-v3-turbo-q5_0"
    app.dl_gate.set()
    expect(pg.locator(".toast", has_text="Speech-to-text now uses “Faster”")).to_be_visible(timeout=15000)
    assert app.host.cfg.transcription.whisper_cpp.model == "small"
    _wait_state(app, "listening")
    expect(pg.locator("#stt-held")).to_have_count(0)


def test_a_key_saved_in_settings_shows_its_check(make_app: Callable[..., UIApp], open_page: Callable[..., Page]) -> None:
    """Saving a key publishes settings.changed, which redraws the section: the check started on save
    must still show its result (a wrongly pasted key would otherwise look fine)."""
    from playwright.sync_api import expect

    app = make_app(setup_done=True)
    p = open_page(app)
    pg = p.page
    pg.click("nav a[href='#/settings']")
    pg.click(".subnav a[href='#/settings/claude']")
    pg.fill("#key-ANTHROPIC_API_KEY", "sk-ant-test-0123456789")
    pg.get_by_role("button", name="Save key").click()
    expect(pg.locator("#ks-ANTHROPIC_API_KEY")).to_have_text("Saved on this computer")
    result = pg.locator("[data-secret=ANTHROPIC_API_KEY] .result.ok")
    expect(result).to_contain_text("Claude accepted the key")
    pg.wait_for_timeout(800)  # well after the settings.changed redraw
    expect(result).to_contain_text("Claude accepted the key")
    p.shot("settings-claude-key-checked")


def test_a_meeting_page_follows_the_meeting_to_its_end(make_app: Callable[..., UIApp], open_page: Callable[..., Page]) -> None:
    """The meeting whose page is open ends (here after 10 minutes of quiet) and its folder is renamed
    after its title: the page reloads under the new name instead of staying "in progress"."""
    from playwright.sync_api import expect

    app = make_app(setup_done=True, listen_on_launch=True)
    _wait_state(app, "listening")
    p = open_page(app)
    pg = p.page
    app.say("Bonjour, on fait le point sur le budget marketing.")
    app.say("Je vais envoyer le budget à Sam d'ici vendredi.")
    expect(pg.locator("#meeting-heading")).to_be_visible()
    pg.click("nav a[href='#/meetings']")
    pg.locator(".mrow").first.click()
    expect(pg.locator(".readonly-note")).to_contain_text("This meeting is in progress")
    before = pg.evaluate("location.hash")

    async def end() -> None:
        assistant = app.host.assistant
        assert assistant is not None and assistant.current is not None
        assistant.current.meeting.title = "Budget marketing"  # what Claude names it during the meeting
        await assistant.end_meeting("idle")

    app.host.call(end()).result(30)
    expect(pg.locator(".readonly-note", has_text="This meeting is in progress")).to_have_count(0)
    expect(pg.locator("#rewrite-notes")).to_be_enabled()
    after = pg.evaluate("location.hash")
    assert after != before and after.endswith("-budget-marketing")
    pg.click("#push-meeting")
    expect(pg.locator(".toast", has_text=re.compile("Sent to|already sent"))).to_be_visible()
    assert not pg.locator(".toast.error").count()
    pg.reload()
    pg.locator('#boot').wait_for(state='hidden')
    expect(pg.locator(".detail-head h1")).to_have_text("Budget marketing")


def test_reminders_permission_is_asked_during_setup(make_app: Callable[..., UIApp], open_page: Callable[..., Page]) -> None:
    """Continue on "Your tools" checks Reminders at once (macOS asks for the Automation permission
    then); a refusal is shown in the app's words, with a way on."""
    from playwright.sync_api import expect

    app = make_app(setup_done=False)
    app.reminders_denied = True
    p = open_page(app)
    pg = p.page
    pg.evaluate("sessionStorage.setItem('voicestream-setup-step', '5')")
    pg.reload()
    pg.locator('#boot').wait_for(state='hidden')
    expect(pg.locator("#setup-h")).to_have_text("Your tools")
    pg.check("#setup-reminders")
    pg.click("#setup-next")
    check = pg.locator("#setup-reminders-check")
    expect(check).to_contain_text("Voicestream isn’t allowed to add reminders")
    expect(check).to_contain_text("Privacy & Security → Automation → Voicestream")
    assert "tccutil" not in check.inner_text()  # the terminal's instructions
    expect(pg.locator("#setup-h")).to_have_text("Your tools")  # stayed, to show it
    p.shot("setup-6-tools-reminders-denied")
    app.reminders_denied = False  # allowed in System Settings
    pg.click("#setup-reminders-again")
    expect(pg.locator("#setup-h")).to_have_text("How you start a meeting")  # the next step
    assert len(app.reminders_checks) == 2


def _fake_mac_shell(app: UIApp, taken: tuple[str, ...] = ("KeyT",), layout: dict[str, str] | None = None) -> None:
    """What the native shell does with the keyboard shortcut, without a Mac: it registers the one the
    preferences name and tells the host what macOS answered (``taken``: keys another app holds), and
    what the keys type on the keyboard layout (``layout``; none: the shell couldn't read it)."""
    host = app.host
    host.capabilities["hotkey"] = True
    assert host.loop is not None
    if layout:
        host.loop.call_soon_threadsafe(host.set_keyboard_layout, layout)
        _wait(lambda: host.keyboard_layout == layout)

    def on_state(state: dict[str, Any]) -> None:  # on the host's loop, like the shell's listener
        hk, st = state["prefs"]["hotkey"], state["hotkey"]
        want = "paused" if st["recording"] else "off" if hk is None else "taken" if hk["code"] in taken else "ok"
        err = -9878 if want == "taken" else None
        if (st["status"], st["error"]) != (want, err):
            host.loop.call_soon(host.set_hotkey_status, want, err)

    host.add_state_listener(on_state)
    host.loop.call_soon_threadsafe(host._publish_state)
    _wait(lambda: host.hotkey["status"] == "ok")


def test_keyboard_shortcut_settings(make_app: Callable[..., UIApp], open_page: Callable[..., Page]) -> None:
    """Settings → General: record a new shortcut from the keyboard, refused ones say why, turn it off,
    reset it; the Live screen mentions it while not listening."""
    from playwright.sync_api import expect

    app = make_app(setup_done=True)
    _fake_mac_shell(app)
    p = open_page(app)
    pg = p.page
    expect(pg.locator("#shortcut-hint")).to_have_text(" You can also press ⌥⌘R in any app.")
    pg.goto(app.url + "#/settings/general")
    pg.locator('#boot').wait_for(state='hidden')
    keys, status, error = pg.locator("#shortcut-keys"), pg.locator("#shortcut-status"), pg.locator("#shortcut-error")
    expect(pg.locator("#shortcut-label")).to_have_text("Keyboard shortcut")
    expect(keys).to_have_text("⌥⌘R")
    expect(status).to_have_text("Works from any app, even with this window closed: it starts and stops listening.")
    expect(pg.locator("#shortcut-reset")).to_have_count(0)  # it is the default already
    p.shot("settings-shortcut")

    # Change: the current one is off while the next combination is recorded (so pressing it reaches the page)
    pg.click("#shortcut-change")
    expect(keys).to_have_text("Press the new shortcut…")
    expect(pg.locator("#shortcut-change")).to_have_text("Cancel")
    _wait(lambda: app.host.state()["hotkey"]["recording"])
    for combo, why in (("Meta+KeyQ", "⌘Q is a command every Mac app has"), ("Meta+KeyC", "⌘C is taken in many apps"),
                       ("Alt+KeyE", "⌥E is taken in many apps"), ("Shift+KeyK", "Hold ⌘, ⌥ or ⌃ as well: ⇧K alone"),
                       ("Meta+Space", "macOS uses ⌘Space itself"), ("Control+Meta+KeyQ", "macOS uses ⌃⌘Q itself"),
                       ("Alt+F5", "macOS 15 can refuse a shortcut held with ⌥ alone, or ⌥⇧ (⌥F5)")):
        pg.keyboard.press(combo)
        expect(error).to_contain_text(why)
        expect(keys).to_have_text("Press the new shortcut…")  # still recording: try another
    p.shot("settings-shortcut-refused")
    pg.keyboard.press("Escape")  # cancels
    expect(keys).to_have_text("⌥⌘R")
    expect(error).to_have_text("")
    expect(pg.locator("#shortcut-change")).to_be_focused()
    _wait(lambda: not app.host.state()["hotkey"]["recording"])

    pg.click("#shortcut-change")
    pg.keyboard.press("Control+Alt+KeyK")
    expect(keys).to_have_text("⌃⌥K")
    expect(status).to_contain_text("Works from any app")
    hk = app.host.prefs.hotkey
    assert hk is not None and (hk.code, hk.mods, hk.key) == ("KeyK", ["ctrl", "alt"], "K")
    assert not app.host.state()["hotkey"]["recording"]
    expect(pg.locator("#shortcut-change")).to_be_focused()

    # the physical key is what counts: on an AZERTY keyboard KeyQ is printed A — and ⌃⌘ with the key
    # printed Q there (KeyA) is the Lock Screen command, refused, while ⌃⌘A is fine
    pg.click("#shortcut-change")
    press = "document.dispatchEvent(new KeyboardEvent('keydown', {code: '%s', key: '%s', ctrlKey: true, metaKey: true}))"
    pg.evaluate(press % ("KeyA", "q"))
    expect(error).to_contain_text("macOS uses ⌃⌘Q itself")
    pg.evaluate(press % ("KeyQ", "a"))
    expect(keys).to_have_text("⌃⌘A")
    hk = app.host.prefs.hotkey
    assert hk is not None and (hk.code, hk.mods, hk.key) == ("KeyQ", ["ctrl", "cmd"], "A")
    # ⌃⌥ there: with ⌃ held WebKit gives what the key types without the modifiers, not ⌥'s character —
    # what it says is kept (it used to be dropped for the US letter, Q)
    pg.click("#shortcut-change")
    pg.evaluate("document.dispatchEvent(new KeyboardEvent('keydown', {code: 'KeyQ', key: 'a', ctrlKey: true, altKey: true}))")
    expect(keys).to_have_text("⌃⌥A")
    hk = app.host.prefs.hotkey
    assert hk is not None and (hk.code, hk.mods, hk.key) == ("KeyQ", ["ctrl", "alt"], "A")

    # a combination another app holds
    pg.click("#shortcut-change")
    pg.keyboard.press("Control+Alt+KeyT")
    expect(status).to_have_text("Already used by another app — choose another.")
    expect(keys).to_have_text("⌃⌥T")

    pg.click("#shortcut-off")
    expect(keys).to_have_text("Off")
    expect(status).to_contain_text("Turned off.")
    expect(pg.locator("#shortcut-change")).to_be_focused()  # the button pressed is gone: not lost
    assert app.host.prefs.hotkey is None
    pg.goto(app.url + "#/live")
    pg.locator('#boot').wait_for(state='hidden')
    expect(pg.locator("#live-heading")).to_have_text("Not listening")
    expect(pg.locator("#shortcut-hint")).to_have_count(0)  # no shortcut: nothing to mention
    pg.goto(app.url + "#/settings/general")
    pg.locator('#boot').wait_for(state='hidden')
    pg.click("#shortcut-reset")
    expect(keys).to_have_text("⌥⌘R")
    expect(status).to_contain_text("Works from any app")
    hk = app.host.prefs.hotkey
    assert hk is not None and (hk.code, hk.mods) == ("KeyR", ["alt", "cmd"])

    # the sound is an ordinary preference, saved with the others
    pg.click("label[for=p-hotkey_sound]")
    pg.click("#save-settings")
    _wait(lambda: app.host.prefs.hotkey_sound is False)
    assert app.host.prefs.hotkey is not None  # saving the others left the shortcut alone

    # the same rules on both sides: the page's, for the words at once, and the server's
    from voicestream.app import hotkey

    assert pg.evaluate("SHORTCUT_RESERVED") == hotkey.RESERVED
    combos = [(code, mods, key) for code in ("KeyR", "KeyQ", "KeyA", "KeyM", "Digit3", "F5", "F12", "Space", "Tab",
                                             "Escape", "Comma", "Semicolon", "Backquote", "ArrowLeft", "Fn", "Numpad1")
              for mods in ([], ["shift"], ["cmd"], ["alt"], ["ctrl"], ["shift", "cmd"], ["alt", "cmd"], ["ctrl", "cmd"],
                           ["ctrl", "alt"], ["alt", "shift", "cmd"], ["ctrl", "shift", "cmd"])
              for key in ("", "q", ",", "m", "ß")]  # fmt: skip
    page_says = pg.evaluate("(combos) => combos.map(([c, m, k]) => [shortcutProblem(c, m, k), shortcutLabel(c, k)])", combos)
    for (code, mods, key), (why, label) in zip(combos, page_says, strict=True):
        assert why == hotkey.problem(code, mods, key), (code, mods, key)
        if code != "Space":  # a word, in the page's language
            assert label == hotkey.label(code, key), (code, key)
    keys_en = pg.evaluate("Object.keys(STRINGS.en).filter((k) => k.startsWith('shortcut.'))")
    assert keys_en and sorted(keys_en) == sorted(pg.evaluate("Object.keys(STRINGS.fr).filter((k) => k.startsWith('shortcut.'))"))


def test_keyboard_shortcut_in_french_and_outside_the_mac_app(make_app: Callable[..., UIApp],
                                                            open_page: Callable[..., Page]) -> None:
    from playwright.sync_api import expect

    app = make_app(setup_done=True)
    p = open_page(app, locale="fr-FR")
    pg = p.page
    # the browser, or Linux and Windows: no global shortcut, said so where it would be
    expect(pg.locator("#live-heading")).to_have_text("À l’arrêt")
    expect(pg.locator("#shortcut-hint")).to_have_count(0)
    pg.goto(app.url + "#/settings/general")
    pg.locator('#boot').wait_for(state='hidden')
    expect(pg.locator("#shortcut-label")).to_have_text("Raccourci clavier")
    expect(pg.locator("#shortcut-status")).to_have_text(
        "Dans l’application Voicestream pour Mac, un raccourci clavier démarre et arrête l’écoute depuis n’importe quelle app.")
    expect(pg.locator("#shortcut-change")).to_have_count(0)
    expect(pg.locator("#p-hotkey_sound")).to_have_count(0)

    _fake_mac_shell(app)  # now as in the Mac app
    pg.reload()
    pg.locator('#boot').wait_for(state='hidden')
    expect(pg.locator("#shortcut-status")).to_have_text(
        "Fonctionne depuis n’importe quelle app, même fenêtre fermée : il démarre et arrête l’écoute.")
    pg.click("#shortcut-change")
    expect(pg.locator("#shortcut-keys")).to_have_text("Tapez le nouveau raccourci…")
    pg.keyboard.press("Meta+KeyW")
    expect(pg.locator("#shortcut-error")).to_have_text(
        "⌘W est une commande de toutes les apps Mac (Quitter, Fermer, Masquer, Placer dans le Dock, Réglages) : choisissez-en un autre.")
    pg.keyboard.press("Control+Alt+Space")
    expect(pg.locator("#shortcut-error")).to_contain_text("macOS utilise lui-même ⌃⌥Espace")
    pg.keyboard.press("Control+Alt+KeyT")
    expect(pg.locator("#shortcut-status")).to_have_text("Déjà utilisé par une autre app — choisissez-en un autre.")
    pg.click("#shortcut-off")
    expect(pg.locator("#shortcut-keys")).to_have_text("Désactivé")
    expect(pg.locator("#shortcut-reset")).to_have_text("Revenir à ⌥⌘R")
    pg.click("#shortcut-reset")
    expect(pg.locator("#shortcut-keys")).to_have_text("⌥⌘R")
    pg.goto(app.url + "#/live")
    pg.locator('#boot').wait_for(state='hidden')
    expect(pg.locator("#shortcut-hint")).to_have_text(" Vous pouvez aussi appuyer sur ⌥⌘R dans n’importe quelle app.")
    p.shot("live-stopped-shortcut-fr")


AZERTY = {  # what the keys of a French Mac keyboard type with nothing held, as the shell reads it from macOS
    **{f"Key{c}": c.lower() for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"},
    "KeyQ": "a", "KeyA": "q", "KeyW": "z", "KeyZ": "w", "KeyM": ",", "Semicolon": "m", "Comma": ";", "Period": ":",
    "Slash": "=", "Minus": ")", "Equal": "-", "BracketLeft": "^", "BracketRight": "$", "Quote": "ù", "Backslash": "`",
    "Backquote": "<", "IntlBackslash": "@",
}  # fmt: skip


def test_keyboard_shortcut_held_with_option_on_an_azerty_keyboard(make_app: Callable[..., UIApp],
                                                                  open_page: Callable[..., Page]) -> None:
    """With ⌥ held a Mac gives the page the character ⌥ types (⌥A is æ on a French keyboard), not what is
    printed on the key: the label and the check come from what the keys type on the keyboard layout, as
    the Mac app read it from macOS — not from the US letter at that place, which showed ⌥⌘Q for the key
    printed A and refused ⌥⌘Z as ⌥⌘W."""
    from playwright.sync_api import expect
    from pydantic import ValidationError

    from voicestream.app import hotkey
    from voicestream.app.prefs import Hotkey

    app = make_app(setup_done=True)
    _fake_mac_shell(app, layout=AZERTY)
    p = open_page(app, locale="fr-FR")
    pg = p.page
    pg.goto(app.url + "#/settings/general")
    pg.locator('#boot').wait_for(state='hidden')
    keys, error = pg.locator("#shortcut-keys"), pg.locator("#shortcut-error")
    option = "document.dispatchEvent(new KeyboardEvent('keydown', {code: '%s', key: '%s', altKey: true, metaKey: true}))"
    pg.click("#shortcut-change")
    pg.evaluate(option % ("KeyZ", "Â"))  # ⌥⌘ and the key printed W: Close All there
    expect(error).to_have_text(
        "⌥⌘W est une commande de toutes les apps Mac (Quitter, Fermer, Masquer, Placer dans le Dock, Réglages) : choisissez-en un autre.")
    p.shot("settings-shortcut-azerty-option-refused-fr")
    pg.evaluate(option % ("KeyQ", "æ"))  # ⌥⌘ and the key printed A
    expect(keys).to_have_text("⌥⌘A")
    hk = app.host.prefs.hotkey
    assert hk is not None and (hk.code, hk.mods, hk.key) == ("KeyQ", ["alt", "cmd"], "A")
    pg.goto(app.url + "#/live")
    pg.locator('#boot').wait_for(state='hidden')
    expect(pg.locator("#shortcut-hint")).to_have_text(" Vous pouvez aussi appuyer sur ⌥⌘A dans n’importe quelle app.")

    # the page and the server agree: what the page makes of a keydown with ⌥ held is what the server makes
    # of the same key sent with no label, by the same layout
    events = [{"code": code, "key": AZERTY.get(code, "") if "ctrlKey" in mods else "◊", **mods}
              for code in ("KeyQ", "KeyA", "KeyW", "KeyZ", "KeyM", "KeyH", "KeyR", "Semicolon", "Comma", "Quote",
                           "Digit3", "F5", "Escape")
              for mods in ({"altKey": True, "metaKey": True}, {"altKey": True}, {"altKey": True, "shiftKey": True,
                           "metaKey": True}, {"ctrlKey": True, "altKey": True}, {"ctrlKey": True, "altKey": True,
                           "metaKey": True})]  # fmt: skip
    page_says = pg.evaluate("(evs) => evs.map((ev) => { const hk = shortcutFromEvent(new KeyboardEvent('keydown', ev));"
                            " return [hk.mods, hk.key, shortcutProblem(hk.code, hk.mods, hk.key), shortcutText(hk)]; })",
                            events)
    for ev, (mods, key, why, text) in zip(events, page_says, strict=True):
        if ev["code"] in AZERTY:
            assert key == AZERTY[ev["code"]], ev  # what is printed on the key, whichever way it came
        try:
            saved = Hotkey.model_validate({"code": ev["code"], "mods": mods, "key": ""}, context={"layout": AZERTY})
        except ValidationError as exc:
            assert why is not None and hotkey.explain(why, ev["code"], mods, key) in str(exc), (ev, why, str(exc))
        else:
            assert why is None and hotkey.describe(ev["code"], saved.mods, saved.key) == text, (ev, why, text)


# ----------------------------------------------------------- meetings started by hand


def _mac_features(app: UIApp) -> None:
    """The Mac app's ways of starting a meeting, as the native shell says it has them (its menu bar, the
    floating button, Spotlight, call detection), and its keyboard shortcut."""
    app.host.capabilities.update(native_window=True, floating_button=True, spotlight=True, call_detection=True)
    _fake_mac_shell(app)


NEW_STRINGS = ("state.m.", "action.start_meeting", "where.", "sug.", "still.", "notice.silence", "notice.link", "live.m.",
               "live.mode.", "live.shortcut.", "live.clock", "live.extract_short", "start.", "setup.start.", "setup.done.",
               "setup.nav.", "shortcut.", "err.minutes")


def test_a_meeting_started_by_hand_from_live(make_app: Callable[..., UIApp], open_page: Callable[..., Page]) -> None:
    """Meetings start by hand (the default): Live is built around Start meeting, in focus so Return
    starts one; in the meeting its clock, Pause and Stop lead; Stop ends it without a question (as the
    shortcut and the floating button do) and Live is ready for the next one, Start in focus again. The
    sidebar's recorder says what is going on but leaves these controls to the screen."""
    from playwright.sync_api import expect

    from voicestream.app.native import WINDOW_SIZE

    app = make_app(setup_done=True, meeting_start="manual")
    _mac_features(app)
    p = open_page(app, size=WINDOW_SIZE)
    pg = p.page
    expect(pg.locator("#live-heading")).to_have_text("No meeting in progress")
    expect(pg.locator("#state-pill")).to_contain_text("Not recording")
    expect(pg.locator("#topbar .recorder-actions")).to_be_hidden()
    start = pg.locator("#live-start")
    expect(start).to_have_text("Start meeting")
    expect(start).to_be_focused()
    box = start.bounding_box()
    assert box and box["height"] >= 44 and box["y"] + box["height"] <= WINDOW_SIZE[1] / 3, box  # large, at the top
    expect(pg.locator("#live-shortcut-text")).to_have_text("Or press ⌥⌘R in any app.")
    expect(pg.locator("#live-shortcut-change")).to_have_text("Choose a shortcut")
    expect(pg.locator("#live-mode")).to_contain_text(
        "Meetings start only when you start one, from this window, the menu bar, the floating button, or Spotlight.")
    expect(pg.locator("#live-mode-change")).to_have_attribute("href", "#/settings/general/starting")
    p.shot("live-manual-idle")

    # Return starts the meeting, open at once with nothing said yet
    pg.keyboard.press("Enter")
    _wait_state(app, "listening")
    assert app.host.in_meeting()
    expect(pg.locator("#meeting-heading")).to_be_focused()  # not Stop: Return must not end what it began
    expect(pg.locator("#rec-clock")).to_be_visible()
    expect(pg.locator("#live-pause")).to_have_text("Pause")
    expect(pg.locator("#live-stop")).to_have_class(re.compile(r"\bprimary\b"))
    expect(pg.locator("#end-meeting")).to_be_hidden()
    expect(pg.locator("#state-pill")).to_contain_text("Recording")
    app.say("Je vais envoyer le budget à Sam d'ici vendredi.")
    expect(pg.locator("#lines .seg")).to_have_count(1)
    p.shot("live-manual-meeting")

    # Pause and Resume keep the focus on their button
    pg.focus("#live-pause")
    pg.keyboard.press("Enter")
    expect(pg.locator("#live-pause")).to_have_text("Resume")
    expect(pg.locator("#live-pause")).to_be_focused()
    expect(pg.locator("#rec-clock")).to_have_class(re.compile(r"\bpaused\b"))
    assert app.host.assistant is not None and app.host.assistant.paused
    pg.keyboard.press("Enter")
    expect(pg.locator("#live-pause")).to_have_text("Pause")
    assert not app.host.assistant.paused

    # Stop: no question; the meeting is wrapped up and Live waits for the next one
    pg.click("#live-stop")
    expect(pg.locator("#ask")).to_be_hidden()
    expect(pg.locator(".toast", has_text="The notes are ready")).to_be_visible(timeout=15000)
    expect(pg.locator("#live-heading")).to_have_text("No meeting in progress")
    expect(pg.locator("#last-meeting")).to_contain_text("Just ended")
    _wait_state(app, "stopped")
    assert app.host.assistant is None
    expect(pg.locator("#live-start")).to_be_focused()

    # elsewhere the recorder has the controls, in the same words, and they start a meeting too
    pg.click("nav a[href='#/meetings']")
    expect(pg.locator("#topbar .recorder-actions")).to_be_visible()
    expect(pg.locator("#primary")).to_have_text("Start meeting")
    pg.click("#primary")
    _wait_state(app, "listening")
    assert app.host.in_meeting()
    pg.click("#stop")  # by hand: no question here either
    expect(pg.locator("#ask")).to_be_hidden()
    _wait_state(app, "stopped")


def test_choose_a_shortcut_from_live(make_app: Callable[..., UIApp], open_page: Callable[..., Page]) -> None:
    """Live's "Choose a shortcut" records the next combination pressed, as Settings does: refused ones
    say why, Escape cancels, one another app holds says so beside the button."""
    from playwright.sync_api import expect

    app = make_app(setup_done=True, meeting_start="manual")
    _mac_features(app)
    p = open_page(app)
    pg = p.page
    text, change, error = pg.locator("#live-shortcut-text"), pg.locator("#live-shortcut-change"), pg.locator("#live-shortcut-error")
    change.click()
    expect(pg.locator("#live-shortcut-keys")).to_have_text("Press the new shortcut…")
    expect(change).to_have_text("Cancel")
    _wait(lambda: app.host.state()["hotkey"]["recording"])
    pg.keyboard.press("Meta+KeyQ")
    expect(error).to_contain_text("⌘Q is a command every Mac app has")
    pg.keyboard.press("Enter")  # still recording: Return is a key to record, not Start meeting
    assert app.host.status == "stopped"
    pg.keyboard.press("Escape")
    expect(text).to_have_text("Or press ⌥⌘R in any app.")
    expect(change).to_be_focused()
    _wait(lambda: not app.host.state()["hotkey"]["recording"])

    change.click()
    pg.keyboard.press("Control+Alt+KeyK")
    expect(text).to_have_text("Or press ⌃⌥K in any app.")
    hk = app.host.prefs.hotkey
    assert hk is not None and (hk.code, hk.mods) == ("KeyK", ["ctrl", "alt"])
    change.click()
    pg.keyboard.press("Control+Alt+KeyT")  # another app holds this one
    expect(text).to_have_text("⌃⌥T is used by another app: choose another.")
    p.shot("live-manual-shortcut-taken")


def test_calls_silences_and_endings_are_banners(make_app: Callable[..., UIApp], open_page: Callable[..., Page]) -> None:
    """What the app asks or tells from its state shows inline, on every screen, with its answers — never a
    dialog, never taking the focus: a call that may want notes (Not now / Start meeting), "Still in a
    meeting?" after a long silence (End meeting / Keep recording), a meeting that ended by itself, a link
    that asked to record without the key."""
    from playwright.sync_api import expect

    app = make_app(setup_done=True, meeting_start="manual", meetings=True)
    _mac_features(app)
    p = open_page(app)
    pg = p.page
    host, loop = app.host, app.host.loop
    assert loop is not None
    expect(pg.locator("#live-start")).to_be_focused()

    loop.call_soon_threadsafe(host.suggest_meeting, "zoom.us")
    call = pg.locator("#notices .notice", has_text="Call detected in zoom.us.")
    expect(call).to_contain_text("Start notes for this call?")
    expect(pg.locator("#live-start")).to_be_focused()  # the banner never takes the focus
    expect(pg.locator("#ask")).to_be_hidden()
    p.shot("banner-call")
    pg.click("#suggestion-dismiss")
    expect(call).to_have_count(0)
    assert host.suggestion is None and host.status == "stopped"

    # another call, on another screen: Start meeting starts one
    loop.call_soon_threadsafe(lambda: host.suggest_meeting("Microsoft Teams", call_id="teams-1"))
    pg.click("nav a[href='#/settings']")
    expect(pg.locator("#notices .notice", has_text="Call detected in Microsoft Teams.")).to_be_visible()
    pg.click("#suggestion-start")
    _wait_state(app, "listening")
    assert host.in_meeting()
    expect(pg.locator("#notices .notice", has_text="Call detected")).to_have_count(0)

    def still(minutes: int) -> None:
        def ask() -> None:
            a = host.assistant
            assert a is not None and a.current is not None
            host.bus.publish("meeting.still_there", meeting_id=a.current.meeting.id, silent_minutes=minutes,
                             ends_at=(datetime.now().astimezone() + timedelta(minutes=44)).isoformat())
        loop.call_soon_threadsafe(ask)

    # a long silence: keep the meeting…
    pg.click("nav a[href='#/live']")
    still(16)
    nudge = pg.locator("#notices .notice", has_text="Still in a meeting?")
    expect(nudge).to_contain_text("Nothing has been said for 16 min. Without an answer, the meeting ends by itself at")
    p.shot("banner-still-there")
    pg.click("#still-keep")
    expect(nudge).to_have_count(0)
    assert host.in_meeting() and host.still_there is None
    # … or end it, without a question: that was the question
    still(17)
    expect(nudge).to_contain_text("17 min")
    pg.click("#still-end")
    expect(pg.locator("#ask")).to_be_hidden()
    _wait_state(app, "stopped")
    expect(nudge).to_have_count(0)

    # a meeting that ended by itself: what happened, and its notes one click away
    folder = app.folder_of("Budget review")

    def ended() -> None:
        host.notice = {"kind": "ended_after_silence", "meeting_id": "m-1", "minutes": 60, "at": "2026-10-21T10:00:00+00:00",
                       "title": "Budget review", "folder": folder}
        host._publish_state()

    loop.call_soon_threadsafe(ended)
    told = pg.locator("#notices .notice", has_text="The meeting ended by itself.")
    expect(told).to_contain_text("“Budget review” ended after 60 min without speech: its notes are ready.")
    p.shot("banner-ended-after-silence")
    pg.click("#notice-open")
    expect(pg.locator(".detail-head h1")).to_have_text("Budget review")
    expect(told).to_have_count(0)
    _wait(lambda: host.notice is None)

    # a link that asked to record without Voicestream's key: said, and put away with OK
    assert host.call(host.open_url("voicestream://meeting/start")).result(10) == "refused"
    refused = pg.locator("#notices .notice", has_text="A link was ignored.")
    expect(refused).to_contain_text("nothing was recorded")
    pg.click("#notice-ok")
    expect(refused).to_have_count(0)
    assert host.notice is None and host.status == "stopped"


def test_starting_meetings_settings(make_app: Callable[..., UIApp], open_page: Callable[..., Page]) -> None:
    """Settings → General → Starting meetings, which Live's "Change" opens: how meetings start, the
    silence thresholds (checked as you type), the shortcut, the floating button, Spotlight and call
    suggestions, saved with Save; automatically, the always-on settings instead of the silences."""
    from playwright.sync_api import expect

    from voicestream.app.native import WINDOW_SIZE

    app = make_app(setup_done=True, meeting_start="manual")
    _mac_features(app)
    p = open_page(app, size=WINDOW_SIZE)
    pg = p.page
    pg.click("#live-mode-change")
    heading = pg.locator("#set-starting h2")
    expect(heading).to_have_text("Starting meetings")
    expect(heading).to_be_focused()
    box = heading.bounding_box()
    assert box and 0 <= box["y"] < WINDOW_SIZE[1] / 2
    expect(pg.locator("input[name='c-How meetings start'][value='manual']")).to_be_checked()
    expect(pg.locator("#p-silence_ask_minutes")).to_have_value("15")
    expect(pg.locator("#p-silence_end_minutes")).to_have_value("60")
    expect(pg.locator("#f-always_on_idle_split_minutes")).to_have_count(0)  # an always-on setting
    expect(pg.locator("#set-starting #shortcut-keys")).to_have_text("⌥⌘R")
    expect(pg.locator("#shortcut-status")).to_have_text("Works from any app, even with this window closed: it starts and stops a meeting.")
    expect(pg.locator("#shortcut-change")).to_have_text("Choose a shortcut")
    for key in ("floating_button", "spotlight_launcher", "call_suggestions"):
        expect(pg.locator(f"#p-{key}")).to_be_checked()
        expect(pg.locator(f"#p-{key}")).to_be_enabled()
    expect(pg.locator("#start-summary")).to_contain_text("Meetings start when you start them")
    p.shot("settings-starting-meetings")

    # minutes the app would refuse: said under the field at once, and Save asks to fix it first
    pg.fill("#p-silence_ask_minutes", "7.5")
    error = pg.locator("[data-path='pref:silence_ask_minutes'] .field-error")
    expect(error).to_have_text("Enter a whole number of minutes, from 0 to 600.")
    expect(pg.locator("#p-silence_ask_minutes")).to_have_attribute("aria-invalid", "true")
    pg.click("#save-settings")
    expect(pg.locator("#savebar .msg.error")).to_contain_text("Nothing was saved")
    expect(pg.locator("#p-silence_ask_minutes")).to_be_focused()
    assert app.host.prefs.silence_ask_minutes == 15
    pg.fill("#p-silence_ask_minutes", "20")
    expect(error).to_have_text("")
    pg.click("label[for=p-floating_button]")
    pg.click("#save-settings")
    expect(pg.locator(".toast", has_text="Your settings are saved").last).to_be_visible()
    _wait(lambda: (app.host.prefs.silence_ask_minutes, app.host.prefs.floating_button) == (20, False))
    expect(pg.locator("#start-summary")).not_to_contain_text("Floating button")

    # automatically: listening all the time, so its own settings instead of the silences
    pg.get_by_label("Automatically", exact=True).check()
    expect(pg.locator("#p-silence_ask_minutes")).to_have_count(0)
    expect(pg.locator("#f-always_on_idle_split_minutes")).to_be_visible()
    expect(pg.locator("#p-listen_on_launch")).to_be_visible()
    pg.click("#save-settings")
    _wait(lambda: app.host.prefs.meeting_start == "automatic")
    pg.click("nav a[href='#/live']")
    expect(pg.locator("#live-mode")).to_contain_text("Automatic mode: meetings start by themselves when someone speaks.")
    expect(pg.locator("#live-heading")).to_have_text("Not listening")
    expect(pg.locator("#primary")).to_have_text("Start listening")
    expect(pg.locator("#shortcut-hint")).to_have_text(" You can also press ⌥⌘R in any app.")


def test_starting_meetings_outside_the_mac_app(make_app: Callable[..., UIApp], open_page: Callable[..., Page]) -> None:
    """In a browser (or on Linux), what only the Mac app does is said, not offered: the switches are off
    and say so, the shortcut says it is the Mac app's, and Live names only this window."""
    from playwright.sync_api import expect

    app = make_app(setup_done=True, meeting_start="manual")
    p = open_page(app, locale="fr-FR")
    pg = p.page
    expect(pg.locator("#live-heading")).to_have_text("Aucune réunion en cours")
    expect(pg.locator("#live-start")).to_have_text("Démarrer la réunion")
    expect(pg.locator("#live-shortcut-text")).to_have_count(0)
    expect(pg.locator("#live-mode")).to_contain_text("Une réunion ne démarre que quand vous la lancez, depuis cette fenêtre.")
    pg.click("#live-mode-change")
    expect(pg.locator("#shortcut-status")).to_have_text(
        "Dans l’application Voicestream pour Mac, un raccourci clavier démarre et arrête une réunion depuis n’importe quelle app.")
    for key in ("floating_button", "spotlight_launcher", "call_suggestions"):
        expect(pg.locator(f"#p-{key}")).to_be_disabled()
        expect(pg.locator(f"[data-path='pref:{key}']")).to_contain_text("Disponible uniquement dans l’application Voicestream pour Mac.")
    # every new word, in both languages
    keys = pg.evaluate("(prefixes) => [Object.keys(STRINGS.en), Object.keys(STRINGS.fr)].map((ks) => ks.filter("
                       "(k) => prefixes.some((p) => k.startsWith(p))).sort())", list(NEW_STRINGS))
    assert keys[0] and keys[0] == keys[1], set(keys[0]) ^ set(keys[1])


def test_setup_step_how_you_start_a_meeting(make_app: Callable[..., UIApp], open_page: Callable[..., Page]) -> None:
    """The setup's "How you start a meeting": by hand is chosen, every way of starting one is on, so
    Continue is enough; the shortcut can be chosen there with a large button. In a browser, what only the
    Mac app does is said instead. The last step then says Done, and nothing starts by itself."""
    from playwright.sync_api import expect

    app = make_app(setup_done=False, meeting_start="manual")
    p = open_page(app)
    pg = p.page
    pg.goto(app.url + "#/setup/start")  # a link to the step (as a reload of it)
    pg.locator('#boot').wait_for(state='hidden')
    expect(pg.locator("#setup-h")).to_have_text("How you start a meeting")
    expect(pg.locator("input[name='c-How you start a meeting'][value='manual']")).to_be_checked()
    expect(pg.locator("#setup-start-mac-only")).to_contain_text(
        "In the Voicestream app for Mac you can also start a meeting with a keyboard shortcut")
    expect(pg.locator("#setup-shortcut-change")).to_have_count(0)

    _mac_features(app)  # now as in the Mac app
    pg.reload()
    pg.locator('#boot').wait_for(state='hidden')
    expect(pg.locator("#setup-shortcut-keys")).to_have_text("⌥⌘R")
    choose = pg.locator("#setup-shortcut-change")
    expect(choose).to_have_text("Choose a shortcut")
    box = choose.bounding_box()
    assert box and box["height"] >= 36
    for switch in ("#setup-floating", "#setup-spotlight", "#setup-suggestions"):
        expect(pg.locator(switch)).to_be_checked()
    expect(pg.locator("#setup-start-mac-only")).to_have_count(0)
    p.shot("setup-start-manual")
    choose.click()
    pg.keyboard.press("Control+Alt+KeyK")
    expect(pg.locator("#setup-shortcut-keys")).to_have_text("⌃⌥K")
    pg.click("label[for=setup-spotlight]")
    # automatically: its own switch instead, and back: the choices made are kept
    pg.get_by_label("Automatically", exact=True).check()
    expect(pg.locator("#setup-launch")).to_be_visible()
    expect(pg.locator("#setup-floating")).to_have_count(0)
    pg.get_by_label("When you start them (recommended)", exact=True).check()
    expect(pg.locator("#setup-spotlight")).not_to_be_checked()
    pg.click("#setup-next")

    expect(pg.locator("#setup-h")).to_have_text(re.compile("You’re all set"))
    prefs = app.host.prefs
    assert (prefs.meeting_start, prefs.floating_button, prefs.spotlight_launcher, prefs.call_suggestions) == ("manual", True, False, True)
    expect(pg.locator(".summary-list")).to_contain_text("Meetings start when you start them, or with ⌃⌥K in any app")
    expect(pg.locator("#setup-next")).to_have_text("Done")
    expect(pg.locator("#setup-secondary")).to_have_count(0)
    p.shot("setup-done-manual")
    pg.click("#setup-next")
    expect(pg.locator("#shell")).to_be_visible()
    expect(pg.locator("#live-start")).to_be_focused()
    assert app.host.prefs.setup_done and app.host.status == "stopped" and app.host.assistant is None


def test_the_keyboard_reaches_and_answers_everything(make_app: Callable[..., UIApp], open_page: Callable[..., Page]) -> None:
    """Tab reaches Start meeting from the top of the window, each stop showing where it is, and Return
    starts it; opening the app again (Spotlight, the Dock) brings Live back with Start in focus; a
    dialog has its default button on the right, Escape cancels it and Return answers it."""
    from playwright.sync_api import expect

    app = make_app(setup_done=True, meeting_start="manual", keys={"ANTHROPIC_API_KEY": "sk-ant-test-0123"})
    _mac_features(app)
    p = open_page(app)
    pg = p.page
    host, loop = app.host, app.host.loop
    assert loop is not None

    # opened again while another screen is on show
    pg.click("nav a[href='#/costs']")
    expect(pg.locator("#screen-costs h1")).to_be_visible()
    loop.call_soon_threadsafe(host.request_focus)
    expect(pg.locator("#live-start")).to_be_focused()
    assert pg.evaluate("location.hash") == "#/live"

    pg.focus("#skip-link")  # the top of the window
    stops = []
    for _ in range(12):
        pg.keyboard.press("Tab")
        stops.append(pg.evaluate("""() => { const el = document.activeElement;
          return [el.id || el.textContent.trim().slice(0, 24), getComputedStyle(el).outlineStyle]; }"""))
        if stops[-1][0] == "live-start":
            break
    assert stops[-1][0] == "live-start" and len(stops) <= 6, stops  # the screens, today's spending, Start
    assert all(style == "solid" for _, style in stops), stops  # every stop shows the focus
    pg.keyboard.press("Enter")
    _wait_state(app, "listening")
    expect(pg.locator("#meeting-heading")).to_be_focused()
    expect(pg.locator("#live-pause")).to_be_enabled()
    pg.keyboard.press("Tab")  # from the meeting's title, on to its controls
    expect(pg.locator("#live-pause")).to_be_focused()
    pg.click("#live-stop")
    _wait_state(app, "stopped")

    # a dialog: Cancel on the left, the answer on the right; Escape cancels, Return answers
    pg.goto(app.url + "#/settings/claude")
    pg.locator('#boot').wait_for(state='hidden')
    pg.locator("[data-secret=ANTHROPIC_API_KEY] .btn", has_text="Remove").click()
    dialog, ok, cancel = pg.locator("#ask"), pg.locator("#ask-ok"), pg.locator("#ask-cancel")
    expect(dialog).to_be_visible()
    expect(cancel).to_be_focused()  # it can't be undone: Return alone doesn't do it
    assert ok.bounding_box()["x"] > cancel.bounding_box()["x"]
    pg.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    assert os.environ.get("ANTHROPIC_API_KEY")
    pg.locator("[data-secret=ANTHROPIC_API_KEY] .btn", has_text="Remove").click()
    pg.keyboard.press("Tab")
    expect(ok).to_be_focused()
    pg.keyboard.press("Enter")
    expect(dialog).to_be_hidden()
    expect(pg.locator("#ks-ANTHROPIC_API_KEY")).to_have_text("Not set")


def test_a_meeting_by_hand_in_the_smallest_window_and_on_a_phone(make_app: Callable[..., UIApp],
                                                                    open_page: Callable[..., Page]) -> None:
    """At the Mac window's smallest, a meeting started by hand keeps its title, clock and controls on one
    line and its two panes side by side in view, "Still in a meeting?" above them; on a phone, Start
    meeting is the width of the screen and a thumb high, and nothing scrolls sideways."""
    from playwright.sync_api import expect

    from voicestream.app.native import MIN_SIZE

    app = make_app(setup_done=True, meeting_start="manual")
    p = open_page(app, size=(390, 844))
    pg = p.page
    start = pg.locator("#live-start")
    expect(start).to_be_visible()
    box, row = start.bounding_box(), pg.locator("#live-controls").bounding_box()
    assert box and row and box["height"] >= 44 and abs(box["width"] - row["width"]) < 1, (box, row)
    assert pg.evaluate("document.documentElement.scrollWidth <= innerWidth")
    menu = pg.locator("#menu-btn").bounding_box()
    assert menu and menu["height"] >= 44 and menu["width"] >= 44, menu

    pg.set_viewport_size({"width": MIN_SIZE[0], "height": MIN_SIZE[1] - 28})
    start.click()
    _wait_state(app, "listening")
    app.say("Bonjour, on fait le point sur le budget.")
    host = app.host
    assert host.loop is not None

    def ask() -> None:
        a = host.assistant
        assert a is not None and a.current is not None
        host.bus.publish("meeting.still_there", meeting_id=a.current.meeting.id, silent_minutes=15, ends_at=None)

    host.loop.call_soon_threadsafe(ask)
    expect(pg.locator("#notices .notice", has_text="Still in a meeting?")).to_be_visible()
    got = pg.evaluate("""() => {
      const rect = (sel) => document.querySelector(sel).getBoundingClientRect();
      const panes = [...document.querySelectorAll('#screen-live .panes > .pane')].map((el) => el.getBoundingClientRect());
      const page = document.documentElement;
      return { title: rect('#meeting-heading'), stop: rect('#live-stop'), panes,
               fits: page.scrollHeight <= innerHeight && page.scrollWidth <= innerWidth, height: innerHeight };
    }""")
    assert got["fits"], got
    assert got["stop"]["top"] < got["title"]["bottom"], got  # the controls beside the title
    assert len(got["panes"]) == 2 and got["panes"][0]["top"] == got["panes"][1]["top"], got
    assert got["panes"][1]["bottom"] <= got["height"], got
    p.shot("live-manual-meeting-smallest-window")


def test_a_start_waiting_for_the_speech_model_can_be_stopped_on_live(make_app: Callable[..., UIApp],
                                                                     open_page: Callable[..., Page]) -> None:
    """Start meeting pressed while the speech model is still downloading: the meeting waits for it, and
    Live offers Stop right there. Stopped, nothing starts when the download is done — a start nobody can
    cancel where they pressed it would open the microphone by itself minutes later."""
    from playwright.sync_api import expect

    app = make_app(setup_done=True, meeting_start="manual", backend="whisper-cpp", models=(stt_models.VAD_MODEL,))
    app.dl_gate.clear()  # the model download holds at 90%
    p = open_page(app)
    pg = p.page
    pg.click("#live-start")
    _wait(lambda: app.host.awaiting_model is not None)
    assert app.host.status == "starting" and app.host.assistant is None
    expect(pg.locator("#live-heading")).to_have_text("Starting the meeting…")
    stop = pg.locator("#live-stop")
    expect(stop).to_have_text("Stop")
    p.shot("live-manual-starting-waits-for-model")
    stop.click()
    _wait_state(app, "stopped")
    expect(pg.locator("#live-start")).to_have_text("Start meeting")
    app.dl_gate.set()
    expect(pg.locator(".toast", has_text="is ready")).to_be_visible(timeout=15000)
    pg.wait_for_timeout(500)  # well after the download's end, when a start still waiting would run
    assert app.host.status == "stopped" and app.host.assistant is None and not app.built


def test_a_start_left_for_after_the_download_is_dropped_by_a_switch_to_by_hand(make_app: Callable[..., UIApp],
                                                                               open_page: Callable[..., Page]) -> None:
    """Listening was to start once the speech model is in (the setup's "Start listening when the download
    is done", meetings automatic), then meetings were set to start by hand: the download ending starts
    nothing — no meeting opens, the microphone stays closed."""
    from playwright.sync_api import expect

    app = make_app(setup_done=True, backend="whisper-cpp", models=(stt_models.VAD_MODEL,))
    p = open_page(app)
    pg = p.page
    host, loop = app.host, app.host.loop
    assert loop is not None

    def wait_to_start_after(name: str) -> None:
        app.dl_gate.clear()
        loop.call_soon_threadsafe(host.model_download, name)
        _wait(lambda: pg.evaluate("(n) => Boolean(S.downloads[n])", name))
        pg.evaluate("(n) => { S.startAfterDownload = n; }", name)  # as finishSetup does

    def download_ends(name: str) -> None:
        app.dl_gate.set()
        _wait(lambda: stt_models.model_path(host.cfg, name).is_file() and name not in host._downloads, timeout=15)
        pg.wait_for_timeout(500)  # the page has had the download's end, when it would start listening
        assert host.status == "stopped" and host.assistant is None and not app.built

    # by hand from now on, wherever it was changed (the page hears it from the app's state)…
    wait_to_start_after("large-v3-turbo-q5_0")
    loop.call_soon_threadsafe(host.update_prefs, {"meeting_start": "manual"})
    _wait(lambda: pg.evaluate("isManual()"))
    download_ends("large-v3-turbo-q5_0")

    # … and when it is saved in Settings, the start waiting for the download is forgotten there and then
    loop.call_soon_threadsafe(host.update_prefs, {"meeting_start": "automatic"})
    _wait(lambda: not pg.evaluate("isManual()"))
    wait_to_start_after("small")
    pg.click("nav a[href='#/settings']")
    pg.get_by_label("When you start them (recommended)", exact=True).check()
    pg.click("#save-settings")
    expect(pg.locator(".toast", has_text="Your settings are saved").last).to_be_visible()
    assert host.prefs.meeting_start == "manual" and pg.evaluate("S.startAfterDownload") is None
    download_ends("small")


def test_space_scrolls_live_and_never_starts_a_meeting(make_app: Callable[..., UIApp], open_page: Callable[..., Page]) -> None:
    """Start meeting is in focus whenever Live opens (Return starts one), and Live is a long page: Space
    scrolls it, as on any page, wherever the app put the focus on Start — the window opening, the
    meeting just stopped, the app opened again. Reached with Tab, Start is a button like any other and
    Space presses it."""
    from playwright.sync_api import expect

    from voicestream.app.native import WINDOW_SIZE

    app = make_app(setup_done=True, meeting_start="manual", meetings=True)
    _mac_features(app)
    p = open_page(app, size=WINDOW_SIZE)
    pg = p.page
    host, loop = app.host, app.host.loop
    assert loop is not None
    start = pg.locator("#live-start")
    expect(start).to_be_focused()
    _wait(lambda: pg.evaluate("document.documentElement.scrollHeight > innerHeight + 200"))  # the meetings are in

    def space_scrolls() -> None:
        pg.evaluate("scrollTo(0, 0)")
        pg.keyboard.press("Space")
        _wait(lambda: pg.evaluate("scrollY") > 200)
        pg.keyboard.press("Shift+Space")
        _wait(lambda: pg.evaluate("scrollY") == 0)
        pg.wait_for_timeout(300)
        assert host.status == "stopped" and host.assistant is None, host.status
        expect(start).to_be_focused()

    space_scrolls()  # the window opened
    pg.keyboard.press("Enter")  # Return still starts one
    _wait_state(app, "listening")
    pg.click("#live-stop")
    _wait_state(app, "stopped")
    expect(start).to_be_focused()
    space_scrolls()  # the meeting just stopped
    pg.click("nav a[href='#/costs']")
    loop.call_soon_threadsafe(host.request_focus)
    expect(start).to_be_focused()
    space_scrolls()  # opened again

    pg.keyboard.press("Shift+Tab")
    pg.keyboard.press("Tab")
    expect(start).to_be_focused()
    pg.keyboard.press("Space")
    _wait_state(app, "listening")
    assert host.in_meeting()


def test_banners_in_a_meeting_never_move_its_controls(make_app: Callable[..., UIApp], open_page: Callable[..., Page]) -> None:
    """In a meeting, what comes and goes by itself ("Still in a meeting?", which speech takes away; a
    warning; new settings waiting) shows under the meeting's title and controls: Pause and Stop stay
    where the pointer is going, and End meeting never slides under it."""
    from playwright.sync_api import expect

    from voicestream.app.native import WINDOW_SIZE

    app = make_app(setup_done=True, meeting_start="manual")
    _mac_features(app)
    p = open_page(app, size=WINDOW_SIZE)
    pg = p.page
    host, loop = app.host, app.host.loop
    assert loop is not None
    pg.click("#live-start")
    _wait_state(app, "listening")
    app.say("Bonjour, on fait le point sur le budget.")
    expect(pg.locator("#lines .seg")).to_have_count(1)
    where = "() => ['#live-pause', '#live-stop', '#rec-clock', '#meeting-heading'].map((s) => { " \
            "const r = document.querySelector(s).getBoundingClientRect(); return [s, Math.round(r.x), Math.round(r.y)]; })"
    before = pg.evaluate(where)

    def still() -> None:
        a = host.assistant
        assert a is not None and a.current is not None
        host.bus.publish("meeting.still_there", meeting_id=a.current.meeting.id, silent_minutes=16,
                         ends_at=(datetime.now().astimezone() + timedelta(minutes=44)).isoformat())

    loop.call_soon_threadsafe(still)
    nudge = pg.locator("#notices .notice", has_text="Still in a meeting?")
    expect(nudge).to_be_visible()
    assert pg.evaluate(where) == before
    assert nudge.bounding_box()["y"] >= pg.locator("#live-stop").bounding_box()["y"] + 36  # under the controls
    p.shot("live-manual-still-there")
    loop.call_soon_threadsafe(lambda: (host.add_warning("Meeting audio isn't coming through: check Settings → Audio"),
                                       host._publish_state()))
    expect(pg.locator("#live-notices .notice")).to_be_visible()
    assert pg.evaluate(where) == before
    app.say("Oui, toujours là.")  # someone speaks: the question goes by itself
    expect(nudge).to_have_count(0)
    assert pg.evaluate(where) == before

    # elsewhere the shell's banners are back above the screen, and in the next meeting under its controls
    pg.click("nav a[href='#/settings']")
    loop.call_soon_threadsafe(still)
    expect(pg.locator(".frame > #notices .notice", has_text="Still in a meeting?")).to_be_visible()
    pg.click("nav a[href='#/live']")
    expect(pg.locator(".live-meeting > #notices .notice", has_text="Still in a meeting?")).to_be_visible()
    pg.click("#still-end")
    _wait_state(app, "stopped")
    expect(pg.locator(".frame > #notices")).to_have_count(1)


def test_opening_the_app_again_keeps_what_you_were_typing(make_app: Callable[..., UIApp], open_page: Callable[..., Page]) -> None:
    """Opening the app again (the Dock, Spotlight, its menus) brings Live back with Start in focus —
    unless something is being typed: a to-do being corrected, or Settings changed and not saved yet.
    That stays as it is, in focus; nothing is lost."""
    from playwright.sync_api import expect

    app = make_app(setup_done=True, meeting_start="manual", meetings=True)
    _mac_features(app)
    p = open_page(app)
    pg = p.page
    host, loop = app.host, app.host.loop
    assert loop is not None
    hash_ = "#/meetings/" + app.folder_of("Budget review") + "/actions"
    pg.evaluate("(h) => { location.hash = h; }", hash_)
    pg.get_by_role("button", name="Edit").first.click()
    title = pg.locator("form.edit input").first
    title.fill("Half typed correc")
    loop.call_soon_threadsafe(host.request_focus)
    pg.wait_for_timeout(600)
    assert pg.evaluate("location.hash") == hash_
    expect(title).to_have_value("Half typed correc")
    expect(title).to_be_focused()

    pg.click("nav a[href='#/settings']")
    pg.fill("#f-user_name", "Camille Z")
    loop.call_soon_threadsafe(host.request_focus)
    pg.wait_for_timeout(600)
    assert pg.evaluate("location.hash").startswith("#/settings")
    expect(pg.locator("#f-user_name")).to_have_value("Camille Z")
    expect(pg.locator("#savebar")).to_be_visible()

    pg.click("#discard-settings")  # nothing left unsaved: Live, Start in focus
    loop.call_soon_threadsafe(host.request_focus)
    expect(pg.locator("#live-start")).to_be_focused()


def test_a_meeting_that_ended_by_itself_says_while_its_notes_are_written(make_app: Callable[..., UIApp],
                                                                         open_page: Callable[..., Page]) -> None:
    """A meeting that ended after a long silence is wrapped up before anyone knows whether it had notes:
    meanwhile its banner says they are being written (no link yet), never that nothing was said."""
    from playwright.sync_api import expect

    app = make_app(setup_done=True, meeting_start="manual", meetings=True)
    _mac_features(app)
    p = open_page(app)
    pg = p.page
    host, loop = app.host, app.host.loop
    assert loop is not None
    folder = app.folder_of("Budget review")

    def notice(**kw: Any) -> None:
        def go() -> None:
            host.notice = {"kind": "ended_after_silence", "meeting_id": "m-1", "minutes": 60, "at": "2026-10-21T10:00:00+00:00",
                           "title": None, "folder": None, **kw}
            host._publish_state()
        loop.call_soon_threadsafe(go)

    told = pg.locator("#notices .notice", has_text="The meeting ended by itself.")
    notice(pending=True)
    expect(told).to_contain_text("It ended after 60 min without speech: its notes are being written…")
    expect(pg.locator("#notice-open")).to_have_count(0)
    p.shot("banner-ended-after-silence-writing")
    notice(pending=False, title="Budget review", folder=folder)
    expect(told).to_contain_text("“Budget review” ended after 60 min without speech: its notes are ready.")
    expect(pg.locator("#notice-open")).to_be_visible()
    notice(pending=True)  # the next one; nothing was said in it after all
    expect(told).to_contain_text("its notes are being written…")
    notice(pending=False)
    expect(told).to_contain_text("Nothing had been said, so there are no notes.")
    notice()  # a notice from before the field: as it says
    expect(told).to_contain_text("Nothing had been said, so there are no notes.")

    pg.evaluate("applyLanguage('fr')")
    notice(pending=True)
    expect(pg.locator("#notices .notice", has_text="La réunion s’est terminée d’elle-même.")).to_contain_text(
        "Elle s’est terminée après 60 min sans parole : ses notes sont en cours de rédaction…")


def test_saving_settings_with_a_switch_to_by_hand_sends_the_switch_first(make_app: Callable[..., UIApp],
                                                                         open_page: Callable[..., Page]) -> None:
    """One Save with a setting and "When you start them": the switch goes first, so listening all the
    time stops before the new settings arrive — sent the other way round, those settings restart
    listening (or retry a failed start) in automatic mode, the microphone opens after "by hand" was
    asked for, and the restart can open a meeting nobody started. A refused preference then saves
    nothing else: the other changes stay on screen, unsaved."""
    from playwright.sync_api import expect

    app = make_app(setup_done=True, listen_on_launch=True)  # automatic, listening all the time
    _wait_state(app, "listening")
    p = open_page(app)
    pg = p.page
    host = app.host
    sent: list[str] = []
    pg.on("request", lambda r: sent.append(r.url.split("/api/")[1].split("?")[0]) if r.method == "PATCH" else None)
    pg.click("nav a[href='#/settings']")
    pg.fill("#f-user_name", "Camille Z")
    pg.get_by_label("When you start them (recommended)", exact=True).check()
    pg.click("#save-settings")
    expect(pg.locator(".toast.ok", has_text="Saved").last).to_be_visible()
    assert sent == ["prefs", "settings"], sent
    _wait_state(app, "stopped")
    pg.wait_for_timeout(500)
    assert host.assistant is None and not host.in_meeting() and len(app.built) == 1
    assert host.cfg.user_name == "Camille Z" and host.prefs.meeting_start == "manual"

    sent.clear()
    pg.fill("#f-user_name", "Camille Y")
    pg.evaluate("setPref('silence_ask_minutes', 601)")  # one the app refuses (the page's own check is by-passed)
    p.allowed.add(400)
    pg.click("#save-settings")
    expect(pg.locator("[data-path='pref:silence_ask_minutes'] .field-error")).not_to_be_empty()
    assert sent == ["prefs"], sent
    assert host.cfg.user_name == "Camille Z" and host.prefs.silence_ask_minutes == 15
    expect(pg.locator("#f-user_name")).to_have_value("Camille Y")


def test_still_in_a_meeting_is_asked_before_the_meeting_ends(make_app: Callable[..., UIApp],
                                                              open_page: Callable[..., Page]) -> None:
    """The meeting can end by itself only after "Still in a meeting?" has been asked: Save refuses an end
    at or before the question (it would never be asked), says so under the end's field and saves
    nothing; 0 for the question (never ask) takes any end."""
    from playwright.sync_api import expect

    app = make_app(setup_done=True, meeting_start="manual")
    _mac_features(app)
    p = open_page(app)
    pg = p.page
    host = app.host
    pg.goto(app.url + "#/settings/general/starting")
    pg.locator('#boot').wait_for(state='hidden')
    ask, end = pg.locator("#p-silence_ask_minutes"), pg.locator("#p-silence_end_minutes")
    error = pg.locator("[data-path='pref:silence_end_minutes'] .field-error")

    ask.fill("60")  # the end is 60 too
    pg.click("#save-settings")
    expect(error).to_have_text("Choose more minutes than for “Still in a meeting?” (60), or the question is never asked. "
                               "To never ask, set that to 0.")
    expect(end).to_be_focused()
    expect(pg.locator("#savebar .msg.error")).to_contain_text("Nothing was saved")
    assert (host.prefs.silence_ask_minutes, host.prefs.silence_end_minutes) == (15, 60)
    p.shot("settings-ask-after-the-end")
    ask.fill("0")  # never asked: any end will do, and the message goes
    expect(error).to_have_text("")
    pg.click("#save-settings")
    _wait(lambda: host.prefs.silence_ask_minutes == 0)

    ask.fill("20")
    end.fill("10")
    pg.click("#save-settings")
    expect(error).to_contain_text("(20)")
    end.fill("30")
    pg.click("#save-settings")
    _wait(lambda: (host.prefs.silence_ask_minutes, host.prefs.silence_end_minutes) == (20, 30))
    expect(error).to_have_text("")

    pg.evaluate("applyLanguage('fr')")
    pg.locator("#p-silence_end_minutes").fill("20")
    pg.click("#save-settings")
    expect(error).to_have_text("Choisissez plus de minutes que pour « Toujours en réunion ? » (20), sinon la question "
                               "n’est jamais posée. Pour ne jamais la poser, mettez-y 0.")


def test_a_wrong_silence_left_behind_by_a_switch_to_automatic_is_dropped(make_app: Callable[..., UIApp],
                                                                         open_page: Callable[..., Page]) -> None:
    """Minutes typed wrong, then Automatically picked: the silence fields are gone from the screen, so
    what was typed in them goes too — Save saves the switch instead of failing on a field nobody sees."""
    from playwright.sync_api import expect

    app = make_app(setup_done=True, meeting_start="manual")
    _mac_features(app)
    p = open_page(app)
    pg = p.page
    host = app.host
    pg.goto(app.url + "#/settings/general/starting")
    pg.locator('#boot').wait_for(state='hidden')
    pg.fill("#p-silence_ask_minutes", "-3")
    expect(pg.locator("[data-path='pref:silence_ask_minutes'] .field-error")).to_have_text(
        "Enter a whole number of minutes, from 0 to 600.")
    pg.get_by_label("Automatically", exact=True).check()
    expect(pg.locator("#p-silence_ask_minutes")).to_have_count(0)
    pg.click("#save-settings")
    _wait(lambda: host.prefs.meeting_start == "automatic")
    expect(pg.locator(".toast.ok", has_text="Saved").last).to_be_visible()
    assert host.prefs.silence_ask_minutes == 15
    assert pg.evaluate("[S.prefsDraft, S.fieldErrors]") == [{}, {}]
    expect(pg.locator("#savebar .msg.error")).to_have_count(0)


RECORDING_LOOK = """() => {
  const style = (html) => {
    const box = document.createElement('div');
    box.innerHTML = html;
    document.body.append(box);
    const el = box.querySelector('[data-probe]'), cs = getComputedStyle(el);
    const out = [cs.backgroundColor, cs.borderTopColor + ' ' + cs.borderTopWidth];
    box.remove();
    return out;
  };
  return {
    clock: style('<div class="rec-clock listening"><span class="rec-dot" data-probe></span></div>'),
    pill: style('<div class="state st-listening"><span class="dot-state" data-probe></span></div>'),
    nav: style('<nav class="nav"><a><span class="live-dot" data-probe></span></a></nav>'),
    hero: style('<div class="hero-icon listening" data-probe></div>'),
    list: style('<span class="pill"><span class="dot-state" data-probe></span></span>'),
    connected: style('<span class="conn live"><span class="dot" data-probe></span></span>'),
    error: style('<div class="rec-clock error"><span class="rec-dot" data-probe></span></div>'),
    errorPill: style('<div class="state st-error"><span class="dot-state" data-probe></span></div>'),
    token: getComputedStyle(document.documentElement).getPropertyValue('--recording').trim(),
  };
}"""


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_recording_has_one_colour_and_its_clock_says_what_it_counts(make_app: Callable[..., UIApp],
                                                                    open_page: Callable[..., Page], scheme: str) -> None:
    """Recording is one colour everywhere in the window, red as on the floating button and as macOS
    has it, and never the green of "Connected"; a problem is not that same dot. The meeting's clock
    counts the meeting, pauses included, and is named so; a meeting stopped with nothing said is
    "ended", not "in progress"."""
    from playwright.sync_api import expect

    app = make_app(setup_done=True, meeting_start="manual")
    _mac_features(app)
    p = open_page(app, scheme=scheme)
    pg = p.page
    look = pg.evaluate(RECORDING_LOOK)
    assert look["token"], look
    red = look["clock"][0]
    assert red != "rgba(0, 0, 0, 0)" and all(look[k][0] == red for k in ("pill", "nav", "hero", "list")), look
    assert look["connected"][0] != red, look
    assert look["error"] != look["clock"] and look["errorPill"] != look["pill"], look

    pg.click("#live-start")
    _wait_state(app, "listening")
    expect(pg.locator("#rec-clock .rec-time")).to_have_attribute("aria-label", "Meeting time")
    assert pg.evaluate("getComputedStyle(document.querySelector('#rec-clock .rec-dot')).backgroundColor") == red
    pg.click("#live-stop")  # nothing was said
    toast = pg.locator(".toast", has_text="Nothing was said, so nothing was kept.")
    expect(toast.locator("strong")).to_have_text("Meeting ended")
    _wait_state(app, "stopped")

    pg.evaluate("applyLanguage('fr')")
    pg.click("#live-start")
    _wait_state(app, "listening")
    expect(pg.locator("#rec-clock .rec-time")).to_have_attribute("aria-label", "Durée de la réunion")
    pg.click("#live-stop")
    expect(pg.locator(".toast", has_text="Rien n’a été dit").locator("strong")).to_have_text("Réunion terminée")
    _wait_state(app, "stopped")


def test_google_bridge_script_route(tmp_path: Path) -> None:
    """Settings → Your tools → Google copies the bridge script from the app (needs the token)."""
    from test_app_host import _factory, _write_config

    host = AppHost(_write_config(tmp_path), process_setup=False, prefs_path=tmp_path / "app.json", assistant_factory=_factory())
    with TestClient(create_app(host, "tok-123"), base_url="http://127.0.0.1:8765") as c:
        assert c.get("/api/google-bridge").status_code == 401
        r = c.get("/api/google-bridge", headers={TOKEN_HEADER: "tok-123"})
    assert r.status_code == 200 and "function doPost(e)" in r.json()["script"]
