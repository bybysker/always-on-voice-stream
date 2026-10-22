"""How meetings start in the Mac app: by hand (the default) or by themselves, the one set of actions every
Start and Stop goes through, "Still in a meeting?", voicestream:// links and call suggestions
(voicestream/app/host.py, voicestream/session.py, voicestream/app/prefs.py, the app's routes)."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from helpers import seg
from pydantic import ValidationError
from starlette.testclient import TestClient
from test_app_host import _factory, _gated_download, _host, _on_this_mac, _prefs, _until
from test_session import IdleSource

from voicestream import session as session_mod
from voicestream import stt_models
from voicestream.app import host as host_mod
from voicestream.app import prefs as prefs_mod
from voicestream.app.host import AppHost, parse_url
from voicestream.audio.base import AudioSource
from voicestream.config import Config
from voicestream.dashboard.server import TOKEN_HEADER, create_app
from voicestream.models import AudioFrame, TranscriptSegment
from voicestream.session import Assistant
from voicestream.storage import list_sessions, load_meeting
from voicestream.transcribe.base import Transcriber, TranscriptionError

TOKEN = "meetings-token-789"
BASE = "http://127.0.0.1:8765"


class Wall:
    """The clock silences are measured on (:attr:`Assistant.wall`), moved by hand."""

    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, minutes: float) -> None:
        self.t += minutes * 60


class Tracked(IdleSource):
    """A live input that says when it is opened and closed."""

    def __init__(self, name: str, log: list[str]) -> None:
        super().__init__(name)
        self.log = log

    async def frames(self) -> AsyncIterator[AudioFrame]:
        self.log.append(f"open {self.name}")
        try:
            async for frame in super().frames():
                yield frame
        finally:
            self.log.append(f"close {self.name}")


class LastWords(Transcriber):
    """Hears everything and writes one line down as the capture closes: the words still being said when
    a meeting is ended."""

    def __init__(self) -> None:
        super().__init__("last-words")

    async def stream(self, source: str, speaker: str, frames: AsyncIterator[AudioFrame]) -> AsyncIterator[TranscriptSegment]:
        async for _ in frames:
            pass
        yield TranscriptSegment(source=source, speaker=speaker, text="And that is everything for today.", t_start=1.0,
                                t_end=3.0)


def _clocked(wall: Wall, built: list[Assistant] | None = None,
             sources: Callable[[], list[AudioSource]] | None = None) -> Callable[[AppHost], Assistant]:
    base = _factory(sources=sources, built=built)

    def make(host: AppHost) -> Assistant:
        a = base(host)
        a.wall = wall
        return a

    return make


def _manual(tmp_path: Path, wall: Wall | None = None, built: list[Assistant] | None = None, **prefs: Any) -> AppHost:
    _prefs(tmp_path, "manual", **prefs)
    factory = _clocked(wall, built) if wall is not None else _factory(built=built)
    return _host(tmp_path, factory=factory, mode="manual")


def _events(host: AppHost, kind: str) -> list[dict[str, Any]]:
    return [e.data for e in host.bus.history if e.type == kind]


# --------------------------------------------------------------- manual meetings


async def test_nothing_is_captured_until_a_meeting_is_started(tmp_path: Path) -> None:
    """The app as 0.2.0 left it (set up, "start listening when Voicestream opens"): meetings now start
    by hand, so opening the app captures nothing — not even when a call is detected."""
    opened: list[str] = []
    built: list[Assistant] = []
    _prefs(tmp_path, None, setup_done=True, listen_on_launch=True)  # no meeting_start: saved before there was one
    host = _host(tmp_path, factory=_factory(sources=lambda: [Tracked("mic", opened), Tracked("system", opened)],
                                            built=built), mode=None)
    await host.start()
    try:
        assert host.prefs.meeting_start == "manual" and host.state()["mode"] == "manual"
        await asyncio.sleep(0.3)
        assert host.status == "stopped" and built == [] and opened == []
        assert host.suggest_meeting("zoom.us")  # a call: suggested, never started
        await asyncio.sleep(0.2)
        assert built == [] and opened == [] and host.state()["suggestion"]["app"] == "zoom.us"

        assert await host.meeting_start() == "start"
        # the meeting is open at once: nobody has said anything yet
        assert host.status == "listening" and host.in_meeting() and host.state()["meeting"] is not None
        assert host.state()["suggestion"] is None
        await _until(lambda: sorted(opened) == ["open mic", "open system"])
        assert built[0].manual and _events(host, "meeting.started") and not _events(host, "segment")
        assert _events(host, "assistant.started")[-1]["manual"] is True
    finally:
        await host.shutdown()
    assert sorted(opened) == ["close mic", "close system", "open mic", "open system"]


async def test_stop_wraps_the_meeting_up_and_lets_go_of_the_microphone(tmp_path: Path) -> None:
    opened: list[str] = []
    built: list[Assistant] = []
    _prefs(tmp_path, "manual")
    host = _host(tmp_path, factory=_factory(sources=lambda: [Tracked("mic", opened)], built=built), mode="manual")
    await host.meeting_start()
    a = built[0]
    await a._route(seg("I'll send the deck to Sam by Friday.", "mic", t=1.0))
    assert await host.meeting_stop() == "stop"  # at once: the wrap-up goes on
    await _until(lambda: host.status == "stopped" and host.assistant is None)
    assert opened == ["open mic", "close mic"] and all(s.stopped for s in a.sources)
    folder = list_sessions(host.cfg.data_path)[-1]
    meeting, _ = load_meeting(folder)
    assert meeting.ended_at is not None and (folder / "notes.md").is_file() and len(meeting.segments) == 1
    assert [e["reason"] for e in _events(host, "meeting.ending")] == ["stopped"]
    assert await host.meeting_stop() is None  # nothing is recorded any more
    assert host.error is None and len(list_sessions(host.cfg.data_path)) == 1  # no second meeting


async def test_a_meeting_started_by_hand_never_splits_on_a_silence(tmp_path: Path) -> None:
    wall = Wall()
    built: list[Assistant] = []
    host = _manual(tmp_path, wall, built, silence_ask_minutes=0, silence_end_minutes=0)  # never ask, never end
    await host.meeting_start()
    a = built[0]
    first = a.current
    await a._route(seg("Let's start with the budget.", "mic", t=1.0))
    wall.advance(180)  # three hours without a word (always-on would have split after 10 minutes)
    await asyncio.sleep(0.3)
    assert a.current is first and host.in_meeting() and not _events(host, "meeting.ending")
    assert not _events(host, "meeting.still_there") and host.state()["still_there"] is None
    await a._route(seg("Back to the budget.", "mic", t=20.0))
    assert a.current is first and len(first.meeting.segments) == 2
    await host.meeting_stop()
    await _until(lambda: host.status == "stopped")


async def test_a_long_silence_asks_whether_you_are_still_in_the_meeting(tmp_path: Path) -> None:
    wall = Wall()
    built: list[Assistant] = []
    host = _manual(tmp_path, wall, built)  # the defaults: ask after 15 min, end after 60
    assert (host.prefs.silence_ask_minutes, host.prefs.silence_end_minutes) == (15, 60)
    await host.meeting_start()
    a = built[0]
    assert (a.ask_after_s, a.end_after_s) == (900.0, 3600.0)
    meeting_id = a.current.meeting.id
    wall.advance(14)
    await asyncio.sleep(0.2)
    assert host.state()["still_there"] is None
    wall.advance(2)
    await _until(lambda: host.still_there is not None)
    nudge = host.state()["still_there"]
    assert nudge["meeting_id"] == meeting_id and nudge["silent_minutes"] == 16
    ends_in = datetime.fromisoformat(nudge["ends_at"]) - datetime.now(datetime.fromisoformat(nudge["ends_at"]).tzinfo)
    assert timedelta(minutes=43) < ends_in <= timedelta(minutes=44)
    await asyncio.sleep(0.3)  # the monitor looks again and again: asked once
    assert len(_events(host, "meeting.still_there")) == 1

    await a._route(seg("Sorry, I was on mute.", "mic", t=1.0))  # someone speaks: the question goes away
    assert host.state()["still_there"] is None
    wall.advance(16)
    await _until(lambda: host.still_there is not None)  # another silence: asked again
    assert len(_events(host, "meeting.still_there")) == 2

    assert await host.meeting_keep() is True  # "Keep": both counts start over
    assert host.state()["still_there"] is None and _events(host, "meeting.kept") == [{"meeting_id": meeting_id}]
    wall.advance(50)  # 66 min since anyone spoke, 50 since Keep: asked, not ended
    await _until(lambda: host.still_there is not None)
    await asyncio.sleep(0.2)
    assert host.status == "listening" and host.in_meeting() and not _events(host, "meeting.ending")
    await host.listen_pause()  # time on Pause doesn't count
    wall.advance(120)
    await asyncio.sleep(0.3)
    assert host.in_meeting() and not _events(host, "meeting.ending")
    await host.listen_resume()
    await asyncio.sleep(0.2)
    assert host.in_meeting()
    await host.meeting_stop()
    await _until(lambda: host.status == "stopped")
    assert host.state()["still_there"] is None and await host.meeting_keep() is False


async def test_an_hour_without_speech_ends_the_meeting_and_says_why(tmp_path: Path) -> None:
    wall = Wall()
    opened: list[str] = []
    built: list[Assistant] = []
    _prefs(tmp_path, "manual")
    host = _host(tmp_path, factory=_clocked(wall, built, sources=lambda: [Tracked("mic", opened)]), mode="manual")
    await host.meeting_start()
    a = built[0]
    await a._route(seg("I'll send the deck to Sam by Friday.", "mic", t=1.0))
    heard = a.current.last_heard_at
    wall.advance(61)  # the call ended an hour ago: Voicestream was forgotten
    await _until(lambda: host.status == "stopped" and host.assistant is None)
    assert opened == ["open mic", "close mic"] and host.error is None  # a stop, not a problem
    notice = host.state()["notice"]
    assert notice["kind"] == "ended_after_silence" and notice["minutes"] == 60
    assert notice["meeting_id"] == a.finished_meetings[0].meeting.id
    folder = list_sessions(host.cfg.data_path)[-1]
    assert notice["folder"] == folder.name  # the page can open it
    meeting, _ = load_meeting(folder)
    assert meeting.ended_at == heard  # it ended when the talking stopped, not an hour later
    assert [e["reason"] for e in _events(host, "assistant.ending")] == ["silence"]
    assert [e["reason"] for e in _events(host, "meeting.ending")] == ["silence"]
    assert host.dismiss_notice() and host.state()["notice"] is None and not host.dismiss_notice()

    # the next meeting starts afresh; a notice left showing goes away when it does
    host.notice = {"kind": "ended_after_silence"}
    await host.meeting_start()
    assert host.state()["notice"] is None
    await host.meeting_stop()
    await _until(lambda: host.status == "stopped")


async def test_the_notice_says_the_notes_are_being_written_until_they_are(tmp_path: Path,
                                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    """Ended after an hour without speech: the wrap-up (summary, pushes) takes a while, and until it says
    whether there are notes the notice says they are being written (``pending``), not that nothing had been
    said; it says so once it is known, also when nothing was said, or when the wrap-up never says it."""
    gate = _slow_wrap_up(monkeypatch)
    wall = Wall()
    built: list[Assistant] = []
    host = _manual(tmp_path, wall, built)
    try:
        await host.meeting_start()
        await built[0]._route(seg("I'll send the deck to Sam by Friday.", "mic", t=1.0))
        wall.advance(61)
        await _until(lambda: host.status == "stopping" and host.notice is not None)
        await asyncio.sleep(0.1)
        notice = host.state()["notice"]
        assert notice["kind"] == "ended_after_silence" and notice["pending"] is True and notice["folder"] is None
        assert all(e["notice"]["pending"] is True for e in _events(host, "app.state") if e["notice"])
        gate.set()
        await _until(lambda: host.status == "stopped")
        notice = host.state()["notice"]
        assert notice["pending"] is False and notice["folder"] == list_sessions(host.cfg.data_path)[-1].name

        await host.meeting_start()  # nothing said this time: no notes, once that is known
        wall.advance(61)
        await _until(lambda: host.status == "stopped" and host.notice is not None)
        assert host.state()["notice"]["pending"] is False and host.state()["notice"]["folder"] is None

        # a wrap-up that fails (a full disk) never says it is done: the notice doesn't stay "being written"
        await host.meeting_start()
        await built[-1]._route(seg("Budget review.", "mic", t=1.0))

        def full(*args: Any, **kw: Any) -> None:
            raise OSError("No space left on device")

        monkeypatch.setattr(built[-1].current.store, "save", full)
        wall.advance(61)
        await _until(lambda: host.status == "error")
        assert host.state()["notice"]["pending"] is False and not _events(host, "meeting.ended")[2:]
    finally:
        gate.set()
        await host.shutdown()


async def test_speech_right_after_waking_up_isnt_added_to_a_forgotten_meeting(tmp_path: Path) -> None:
    """A meeting started by hand and forgotten, the lid closed overnight: woken up, the first line can come in
    before the idle monitor looks again (words cut off when the lid closed, or the next meeting already
    talking). The night counts as silence, so the meeting ends on it, and that line isn't recorded into it."""
    wall = Wall()
    built: list[Assistant] = []
    base = _clocked(wall, built)

    def asleep(host: AppHost) -> Assistant:
        a = base(host)
        a.idle_poll_s = 3600  # the monitor is in the middle of its sleep: only routing can notice
        return a

    _prefs(tmp_path, "manual")
    host = _host(tmp_path, factory=asleep, mode="manual")
    await host.meeting_start()
    a = built[0]
    first = a.current
    await a._route(seg("I'll send the deck to Sam by Friday.", "mic", t=1.0))
    wall.advance(15 * 60)  # the night: the monitor's clock (time.monotonic) stood still
    await a._route(seg("Morning! Shall we start the stand-up?", "mic", t=2.0))
    await a._route(seg("Yes, Priya goes first.", "system", t=5.0))  # still arriving while it winds down
    await _until(lambda: host.status == "stopped" and host.assistant is None)
    assert [e["reason"] for e in _events(host, "meeting.ending")] == ["silence"]
    assert host.state()["notice"]["kind"] == "ended_after_silence" and len(built) == 1
    assert [s.text for s in first.meeting.segments] == ["I'll send the deck to Sam by Friday."]
    meeting, _ = load_meeting(list_sessions(host.cfg.data_path)[-1])
    assert [s.text for s in meeting.segments] == ["I'll send the deck to Sam by Friday."]


async def test_a_meeting_ended_by_hand_keeps_its_last_words(cfg: Config) -> None:
    """End meeting (the page, the menu bar) in a meeting started by hand is a stop, in a stop's order: the
    capture closes and its last words are written down before the meeting is wrapped up."""
    a = Assistant(cfg, sources=[IdleSource("mic")], transcriber_factory=lambda s: LastWords(), llm=None, sinks=[],
                  manual=True)
    seen: list[tuple[str, bool]] = []
    a.bus.listen(lambda ev: seen.append((ev.type, all(s.stopped for s in a.sources)))
                 if ev.type in ("assistant.ending", "meeting.ending") else None)
    run = asyncio.ensure_future(a.run())
    await _until(lambda: a.current is not None)  # open at once, with nothing said
    notes = await a.end_meeting("dashboard")
    await asyncio.wait_for(run, 30)  # listening ended with it, by itself
    assert seen == [("assistant.ending", False), ("meeting.ending", True)]
    assert notes is not None and notes.is_file() and a.current is None and len(a.finished_meetings) == 1
    meeting, _ = load_meeting(notes.parent)
    assert [s.text for s in meeting.segments] == ["And that is everything for today."]
    assert await a.end_meeting("dashboard") is None  # nothing in progress any more


async def test_the_command_line_still_listens_all_day(cfg: Config) -> None:
    """``voicestream run`` builds its assistant as before: not manual, meetings on speech."""
    cfg.always_on.idle_split_minutes = 10
    a = Assistant(cfg, sources=[], llm=None, sinks=[])
    assert not a.manual and a.always_on and a.auto_meetings
    assert not Assistant(cfg, sources=[], llm=None, sinks=[], always_on=False).auto_meetings


# --------------------------------------------------------------- automatic meetings


async def test_automatic_meetings_still_start_and_split_by_themselves(tmp_path: Path) -> None:
    wall = Wall()
    built: list[Assistant] = []
    _prefs(tmp_path, "automatic", setup_done=True, listen_on_launch=True)
    host = _host(tmp_path, factory=_clocked(wall, built), mode="automatic")
    await host.start()
    try:
        assert host.status == "listening" and not built[0].manual  # listening at launch, as before
        assert not host.in_meeting()  # … and a meeting only once someone speaks
        assert await host.meeting_start() is None  # listening already: nothing to do
        a = built[0]
        await a._route(seg("Let's start.", "mic", t=1.0))
        first = a.current
        assert host.in_meeting()
        wall.advance(70)  # past the idle split, and past the hour a meeting started by hand would get
        await _until(lambda: not host.in_meeting())
        assert [e["reason"] for e in _events(host, "meeting.ending")] == ["idle"]
        assert host.status == "listening" and host.assistant is a  # listening goes on
        assert not _events(host, "meeting.still_there") and host.state()["notice"] is None
        await a._route(seg("Next topic.", "mic", t=500.0))
        assert a.current is not first and host.in_meeting()
        assert await host.end_meeting() == "end"  # "End meeting now": the meeting ends, listening doesn't
        await _until(lambda: not host.in_meeting())
        assert host.status == "listening" and host.assistant is a
    finally:
        await host.shutdown()


# ------------------------------------------------------------------ the actions


async def test_what_each_action_does_in_each_state(tmp_path: Path) -> None:
    host = _host(tmp_path, mode="manual")
    table = {
        "stopped": ("start", None, "start"),
        "error": ("restart", "stop", "restart"),
        "listening": (None, "stop", "stop"),
        "paused": ("resume", "stop", "resume"),
        "starting": (None, "stop", None),
        "stopping": ("start", None, "start"),  # once the last meeting is wrapped up (see below)
    }
    for status, expected in table.items():
        host.status = status  # type: ignore[assignment]
        assert tuple(host.meeting_action(w) for w in ("start", "stop", "toggle")) == expected, status
        assert host.toggle_action() == expected[2]
    host.status = "stopped"
    host._closing = True
    assert {host.meeting_action(w) for w in ("start", "stop", "toggle")} == {None}


async def test_the_shortcut_starts_pauses_and_stops_a_meeting(tmp_path: Path) -> None:
    built: list[Assistant] = []
    host = _manual(tmp_path, built=built)
    assert await host.meeting_toggle() == "start"
    assert host.in_meeting() and built[0].manual
    first_meeting = built[0].current
    await host.listen_pause()
    assert await host.meeting_toggle() == "resume" and host.status == "listening"
    assert built[0].current is first_meeting  # Pause and Resume stay inside the meeting
    assert await host.listen_toggle() == "stop"  # the shortcut's earlier name does the same
    await _until(lambda: host.status == "stopped" and host.assistant is None)
    assert len(list_sessions(host.cfg.data_path)) <= 1 and len(built) == 1
    host._set_status("error", "something gave up")
    assert await host.meeting_toggle() == "restart" and host.in_meeting()
    assert await host.end_meeting() == "stop"  # "End meeting now" in a meeting started by hand: Stop
    await _until(lambda: host.status == "stopped")
    assert await host.end_meeting() is None


def _slow_wrap_up(monkeypatch: pytest.MonkeyPatch) -> asyncio.Event:
    """The meeting's summary waits for the event: a wrap-up (summary, pushes, agents) that takes a while."""
    gate = asyncio.Event()
    real = session_mod.summarize

    async def slow(*args: Any, **kw: Any) -> Any:
        await gate.wait()
        return await real(*args, **kw)

    monkeypatch.setattr(session_mod, "summarize", slow)
    return gate


async def test_a_start_while_the_last_meeting_is_wrapped_up_waits_for_it(tmp_path: Path,
                                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """Back-to-back calls: Stop, then Start (the shortcut, the floating button, Spotlight) as the next one
    begins. The microphone is closed already but the wrap-up can take minutes: the next meeting starts as
    soon as it is done, rather than the press being lost. Stop, Stop now or quitting meanwhile still win."""
    gate = _slow_wrap_up(monkeypatch)
    built: list[Assistant] = []
    host = _manual(tmp_path, built=built)

    async def stopped_with_a_start_waiting(start: Callable[[], Any]) -> asyncio.Future[Any]:
        await host.meeting_start()
        await built[-1]._route(seg("I'll send the deck to Sam by Friday.", "mic", t=1.0))
        gate.clear()
        assert await host.meeting_stop() == "stop" and host.status == "stopping"
        assert host.meeting_action("start") == "start" and host.meeting_action("toggle") == "start"
        waiting = asyncio.ensure_future(start())
        await asyncio.sleep(0.2)
        assert not waiting.done() and host.status == "stopping"
        return waiting

    try:
        first = await stopped_with_a_start_waiting(host.meeting_toggle)
        again = asyncio.ensure_future(host.open_url(host.launcher_url()))  # pressed again (Spotlight): one start
        await asyncio.sleep(0.1)
        assert host.meeting_action("toggle") == "start" and not again.done() and len(built) == 1
        gate.set()  # the last meeting's notes are written: the next one starts
        assert await first == "start" and await again == "start"
        assert host.status == "listening" and host.in_meeting() and len(built) == 2
        # in that order: the last meeting's notes, then the next meeting
        order = [(e.type, e.data.get("notes")) for e in host.bus.history if e.type in ("meeting.ended", "meeting.started")]
        assert [t for t, _ in order] == ["meeting.started", "meeting.ended", "meeting.started"] and order[1][1]

        # Stop while the start waits: nothing starts after the wrap-up
        waiting = await stopped_with_a_start_waiting(host.meeting_start)
        assert host.meeting_action("stop") == "stop" and await host.meeting_stop() == "stop"
        gate.set()
        assert await waiting is None
        await _until(lambda: host.status == "stopped")
        await asyncio.sleep(0.1)
        assert host.status == "stopped" and len(built) == 2 and host.assistant is None

        # "Stop now" (the window, during the wrap-up) likewise
        waiting = await stopped_with_a_start_waiting(host.meeting_start)
        await host.listen_stop(force=True)
        assert await waiting is None and host.status == "stopped" and len(built) == 3

        # two presses of the shortcut in a row while recording: the second comes before the stop says so
        await host.meeting_start()
        await built[-1]._route(seg("Last point.", "mic", t=1.0))
        gate.clear()
        stop, start = asyncio.ensure_future(host.meeting_toggle()), asyncio.ensure_future(host.meeting_toggle())
        assert await stop == "stop"
        await asyncio.sleep(0.1)
        assert host.status == "stopping" and not start.done()
        gate.set()
        assert await start == "start" and host.in_meeting() and len(built) == 5

        # quitting while a start waits: nothing starts
        await built[-1]._route(seg("One more thing.", "mic", t=1.0))
        gate.clear()
        assert await host.meeting_stop() == "stop"
        waiting = asyncio.ensure_future(host.meeting_start())
        await asyncio.sleep(0.1)
        quitting = asyncio.ensure_future(host.shutdown())
        await asyncio.sleep(0.1)
        gate.set()
        await quitting
        assert await waiting is None and len(built) == 5 and host.assistant is None
    finally:
        gate.set()
        await host.shutdown()


async def test_end_meeting_on_the_page_ends_listening_too(tmp_path: Path) -> None:
    """The live page's End meeting (``POST /api/meeting/end`` calls the assistant itself): listening ends
    with a meeting started by hand, as a stop — not as listening that "stopped unexpectedly"."""
    built: list[Assistant] = []
    host = _manual(tmp_path, built=built)
    await host.meeting_start()
    await built[0]._route(seg("We decided to ship on Monday.", "mic", t=1.0))
    notes = await built[0].end_meeting("dashboard")
    assert notes is not None and notes.is_file()
    await _until(lambda: host.status == "stopped" and host.assistant is None)
    assert host.error is None and "stopping" in [e.data["state"] for e in host.bus.history if e.type == "app.state"]
    assert host.state()["notice"] is None  # you ended it: nothing to explain
    assert await host.meeting_start() == "start" and len(built) == 2  # and the next one can start
    await host.meeting_stop()
    await _until(lambda: host.status == "stopped")


async def test_a_fix_after_a_failed_start_starts_nothing_by_itself(tmp_path: Path) -> None:
    """Always-on listening tries again when a settings change may have fixed what stopped it; a meeting
    started by hand doesn't start again by itself: the problem shown is checked again, Start does the rest."""
    tries: list[AppHost] = []
    working = _factory()

    def flaky(host: AppHost) -> Assistant:
        tries.append(host)
        if len(tries) == 1:
            raise TranscriptionError("DEEPGRAM_API_KEY is not set", fatal=True)
        return working(host)

    _prefs(tmp_path, "manual")
    host = _host(tmp_path, factory=flaky, mode="manual")
    assert await host.meeting_start() == "error" and host.error.startswith("Speech-to-text with Deepgram")
    assert await host.apply_settings() == {"restarting": False, "restart_pending": False}
    assert host.status == "stopped" and host.error is None and host.assistant is None and len(tries) == 1
    assert await host.meeting_start() == "start" and host.in_meeting()
    await host.meeting_stop()
    await _until(lambda: host.status == "stopped")


async def test_switching_modes_applies_to_listening_in_progress(tmp_path: Path) -> None:
    built: list[Assistant] = []
    host = _host(tmp_path, factory=_factory(built=built))  # automatic
    await host.listen_start()
    assert not host.in_meeting()
    host.update_prefs({"meeting_start": "manual"})  # nothing may be heard without a meeting started by hand
    await _until(lambda: host.status == "stopped" and host.assistant is None)
    assert host.state()["mode"] == "manual"

    await host.meeting_start()
    a = built[-1]
    assert a.manual and host.in_meeting()
    host.update_prefs({"silence_ask_minutes": 5, "silence_end_minutes": 0})
    assert (a.ask_after_s, a.end_after_s) == (300.0, 0.0)
    host.update_prefs({"meeting_start": "automatic"})  # during a meeting: it goes on…
    assert host.assistant is a and not a.manual and host.in_meeting()
    await a.end_meeting("manual")  # … and listening goes on after it, always-on
    assert host.status == "listening" and host.assistant is a and not host.in_meeting()
    host.update_prefs({"meeting_start": "manual"})
    await _until(lambda: host.status == "stopped")


async def test_a_start_waiting_for_the_speech_model_is_dropped_by_the_switch_to_manual(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Always-on listening asked for while the speech model downloads (at launch, after a quit during the
    first download) waits for it. Switched to "when I start one" meanwhile, it mustn't open a meeting by
    itself once the model is in; a Start pressed in manual mode during the download still waits for it."""
    gate, calls = asyncio.Event(), []
    monkeypatch.setattr(stt_models, "download", _gated_download(gate, calls))
    built: list[Assistant] = []
    host = _host(tmp_path, factory=_factory(built=built))  # automatic
    _on_this_mac(host, tmp_path, "base")
    await host.listen_start()
    assert host.status == "starting" and host.awaiting_model == "base"
    await _until(lambda: calls == ["base"])
    host.update_prefs({"meeting_start": "manual"})
    assert host.status == "stopped" and host.awaiting_model is None and host.error is None
    assert host.state()["state"] == "stopped"
    gate.set()  # the download goes on and finishes: nothing starts
    await _until(lambda: not host._downloads)
    await asyncio.sleep(0.2)
    assert host.status == "stopped" and built == [] and host.assistant is None

    stt_models.remove(host.cfg, "base")
    gate.clear()
    assert await host.meeting_start() == "start" and host.status == "starting"  # pressed by hand, in manual
    host.update_prefs({"meeting_start": "manual", "silence_end_minutes": 30})  # not a switch to manual
    assert host.status == "starting" and host.awaiting_model == "base"
    gate.set()
    await _until(lambda: host.in_meeting())
    assert len(built) == 1 and built[0].manual
    await host.meeting_stop()
    await _until(lambda: host.status == "stopped")


# ----------------------------------------------------------------------- links


async def test_links_need_the_key_to_start_a_recording(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                      caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    built: list[Assistant] = []
    host = _manual(tmp_path, built=built)
    url = host.launcher_url()
    key = parse_qs(urlsplit(url).query)["key"][0]
    assert url == f"voicestream://meeting/start?key={key}" and len(key) >= 32
    assert host.launcher_url() == url  # made once…
    again = AppHost(host.config_path, process_setup=False, prefs_path=host.prefs_path)
    assert again.launcher_url() == url  # … and kept: the launcher in ~/Applications keeps working

    compared: list[int] = []
    real = host_mod.hmac.compare_digest
    monkeypatch.setattr(host_mod.hmac, "compare_digest", lambda a, b: compared.append(1) or real(a, b))
    for bad in ("voicestream://meeting/start", "voicestream://meeting/start?key=", f"voicestream://meeting/start?key={key}x",
                f"voicestream://meeting/start?key={key[:-1]}", f"voicestream://meeting/start?key={key}&key={key}",
                "voicestream://meeting/toggle", "voicestream://meeting/toggle?key=guess"):
        assert await host.open_url(bad) == "refused", bad
    assert built == [] and host.status == "stopped" and compared  # compared in constant time
    assert host.state()["notice"]["kind"] == "link_refused"
    assert await host.open_url("voicestream://meeting/stop") == "nothing"  # no key needed: it records nothing
    assert await host.open_url("voicestream://open") == "open"
    for unknown in ("https://example.com/meeting/start", "voicestream://meeting/dance", "voicestream://", "", "x:\x00"):
        assert await host.open_url(unknown) == "unknown", unknown

    assert await host.open_url(url) == "start" and host.in_meeting()
    assert host.state()["notice"] is None
    assert await host.open_url(f"VOICESTREAM://Meeting/Start/?key={key}") == "nothing"  # recording already
    assert await host.open_url("voicestream://meeting/stop") == "stop"
    await _until(lambda: host.status == "stopped")
    assert await host.open_url(f"voicestream://meeting/toggle?key={key}") == "start"
    assert await host.open_url(f"voicestream://meeting/toggle?key={key}") == "stop"
    await _until(lambda: host.status == "stopped")

    # never shown: not in the state, a dump of the preferences, their repr or the log
    assert key not in json.dumps(host.state()) and key not in host.prefs.model_dump_json() and key not in repr(host.prefs)
    assert key not in caplog.text
    assert json.loads(host.prefs_path.read_text(encoding="utf-8"))["url_key"] == key  # the private file has it


async def test_a_link_that_launches_the_app_doesnt_start_listening(tmp_path: Path) -> None:
    """Meetings start by themselves and "start listening when Voicestream opens" is on, but the app isn't
    running: any web page can launch it with a voicestream:// link. The Mac app starts the host without
    listening and asks for it only when no link launched it, so a link without the key records nothing."""
    opened: list[str] = []
    built: list[Assistant] = []
    _prefs(tmp_path, "automatic", setup_done=True, listen_on_launch=True)
    host = _host(tmp_path, factory=_factory(sources=lambda: [Tracked("mic", opened)], built=built), mode="automatic")
    await host.start(auto_listen=False)
    try:
        assert host.status == "stopped" and _events(host, "app.state")[-1]["state"] == "stopped"
        assert await host.open_url("voicestream://open") == "open"
        assert await host.open_url("voicestream://meeting/start") == "refused"
        assert await host.open_url("voicestream://whatever") == "unknown"
        await asyncio.sleep(0.2)
        assert host.status == "stopped" and built == [] and opened == []
        assert await host.open_url(host.launcher_url()) == "start"  # the launcher's own link: on purpose
        assert host.status == "listening" and await host.listen_on_launch() is False  # running already
        await host.listen_stop()

        assert await host.listen_on_launch() is True  # no link launched it: as it always did
        assert host.status == "listening" and len(built) == 2
        await host.listen_stop()
        host.update_prefs({"listen_on_launch": False})
        assert await host.listen_on_launch() is False
        host.update_prefs({"listen_on_launch": True, "meeting_start": "manual"})
        assert await host.listen_on_launch() is False
        host.update_prefs({"meeting_start": "automatic"})
        host.config_error = "Your settings file has a problem: line 3"
        assert await host.listen_on_launch() is False
        host.config_error = None
        host.prefs = host.prefs.model_copy(update={"setup_done": False})
        assert await host.listen_on_launch() is False and host.status == "stopped" and len(built) == 2
    finally:
        await host.shutdown()


def test_parse_url() -> None:
    assert parse_url("voicestream://meeting/start?key=abc") == ("start", "abc")
    assert parse_url("voicestream://meeting/stop") == ("stop", None)
    assert parse_url("voicestream://meeting/toggle/?key=a&key=b") == ("toggle", None)  # two keys: none
    assert parse_url("Voicestream://OPEN") == ("open", None)
    assert parse_url("voicestream://meeting") is None and parse_url("http://meeting/start") is None


async def test_a_key_kept_through_preference_changes_and_the_setup(tmp_path: Path) -> None:
    host = _manual(tmp_path)
    url = host.launcher_url()
    host.update_prefs({"floating_button": False, "ui_language": "fr"})
    host.complete_setup()
    assert host.launcher_url() == url and prefs_mod.load_prefs(host.prefs_path).url_key in url
    assert await host.open_url(url) == "start"
    await host.meeting_stop()
    await _until(lambda: host.status == "stopped")


# ----------------------------------------------------------------- suggestions


async def test_a_call_is_suggested_once_and_never_starts_anything(tmp_path: Path) -> None:
    built: list[Assistant] = []
    host = _manual(tmp_path, built=built, setup_done=True)
    clock = [1000.0]
    host.wall = lambda: clock[0]
    assert host.suggest_meeting("zoom.us") is True
    assert host.state()["suggestion"]["app"] == "zoom.us" and host.state()["suggestion"]["at"]
    assert host.suggest_meeting("zoom.us") is False  # the same call
    assert host.suggestion_dismiss() is True and host.state()["suggestion"] is None
    assert host.suggestion_dismiss() is False
    assert host.suggest_meeting("zoom.us") is False  # "Not now": not again for this call
    host.call_ended("zoom.us")
    assert host.suggest_meeting("Zoom.us") is True  # its next call
    host.call_ended("zoom.us")  # over: taken down
    assert host.state()["suggestion"] is None

    # calls the shell can tell apart
    assert host.suggest_meeting("Microsoft Teams", call_id="teams-1")
    assert not host.suggest_meeting("Microsoft Teams", call_id="teams-1")
    assert host.suggest_meeting("Microsoft Teams", call_id="teams-2")
    host.call_ended(call_id="teams-1")  # not the one shown
    assert host.state()["suggestion"] is not None
    host.suggestion_dismiss()
    clock[0] += host_mod.CALL_MEMORY_S + 1  # the shell never said the call ended: forgotten after a while
    assert host.suggest_meeting("Microsoft Teams", call_id="teams-1")
    host.call_ended()  # every app let go of the microphone
    assert host.state()["suggestion"] is None and host.suggest_meeting("Microsoft Teams", call_id="teams-1")
    assert host.suggest_meeting("\x1b[31mFace\nTime\x00" + "x" * 200)  # shown as a name, never as anything else
    assert host.state()["suggestion"]["app"].startswith("[31mFace Time") and len(host.state()["suggestion"]["app"]) == 80
    await asyncio.sleep(0.2)
    assert built == [] and host.status == "stopped"  # never started by itself

    assert await host.suggestion_accept() == "start" and host.in_meeting()  # "Start"
    assert host.state()["suggestion"] is None
    assert host.suggest_meeting("Slack") is False  # recording already
    await host.meeting_stop()
    await _until(lambda: host.status == "stopped")

    assert host.suggest_meeting("Slack")
    assert await host.meeting_toggle() == "start" and host.state()["suggestion"] is None  # any start takes it down
    await host.meeting_stop()
    await _until(lambda: host.status == "stopped")

    assert host.suggest_meeting("Webex")
    host.update_prefs({"call_suggestions": False})  # turned off: taken down, none after
    assert host.state()["suggestion"] is None and not host.suggest_meeting("Google Meet")


def test_no_suggestion_before_the_setup_is_done(tmp_path: Path) -> None:
    host = _manual(tmp_path)
    assert not host.prefs.setup_done and host.suggest_meeting("zoom.us") is False
    host.complete_setup()
    assert host.suggest_meeting("zoom.us") is True


async def test_automatic_listening_is_recording_so_no_suggestion(tmp_path: Path) -> None:
    _prefs(tmp_path, "automatic", setup_done=True)
    host = _host(tmp_path, mode="automatic")
    assert host.suggest_meeting("zoom.us")  # not listening: suggested
    await host.listen_start()
    assert host.state()["suggestion"] is None and not host.suggest_meeting("FaceTime")
    await host.listen_stop()


def test_request_focus_tells_the_page(tmp_path: Path) -> None:
    host = _manual(tmp_path)
    host.request_focus()
    assert _events(host, "app.focus") == [{"target": "start"}]


# ----------------------------------------------------------------- preferences


def test_preferences_saved_before_there_was_a_choice_get_the_defaults(tmp_path: Path) -> None:
    path = tmp_path / "app.json"
    path.write_text(json.dumps({"setup_done": True, "listen_on_launch": True, "ui_language": "fr",
                                "hotkey": {"code": "KeyR", "mods": ["alt", "cmd"], "key": "R"}}), encoding="utf-8")
    p = prefs_mod.load_prefs(path)
    assert p.setup_done and p.ui_language == "fr" and p.listen_on_launch
    assert (p.meeting_start, p.floating_button, p.floating_position, p.spotlight_launcher, p.call_suggestions,
            p.silence_ask_minutes, p.silence_end_minutes, p.url_key) == ("manual", True, None, True, True, 15, 60, "")


def test_a_saved_value_this_version_refuses_is_the_default_the_rest_kept(tmp_path: Path) -> None:
    path = tmp_path / "app.json"
    path.write_text(json.dumps({"setup_done": True, "meeting_start": "sometimes", "floating_button": "yes please",
                                "floating_position": {"x": "left"}, "silence_ask_minutes": -1, "silence_end_minutes": 9999,
                                "spotlight_launcher": False, "url_key": "short"}), encoding="utf-8")
    p = prefs_mod.load_prefs(path)
    assert p.setup_done and not p.spotlight_launcher  # kept
    assert (p.meeting_start, p.floating_button, p.floating_position, p.silence_ask_minutes, p.silence_end_minutes,
            p.url_key) == ("manual", True, None, 15, 60, "")  # back to the defaults (a short key is no key)


def test_meeting_preferences_round_trip_and_validation(tmp_path: Path) -> None:
    path = tmp_path / "app.json"
    p = prefs_mod.update_prefs({"meeting_start": "automatic", "floating_button": False, "spotlight_launcher": False,
                                "call_suggestions": False, "silence_ask_minutes": 0, "silence_end_minutes": 600,
                                "floating_position": {"x": 1200.5, "y": -40, "screen": "Studio Display"}}, path)
    assert (p.meeting_start, p.floating_button, p.spotlight_launcher, p.call_suggestions, p.silence_ask_minutes,
            p.silence_end_minutes) == ("automatic", False, False, False, 0, 600)
    assert p.floating_position == prefs_mod.FloatingPosition(x=1200.5, y=-40, screen="Studio Display")
    assert prefs_mod.load_prefs(path) == p
    assert prefs_mod.update_prefs({"ui_language": "fr"}, path).floating_position == p.floating_position  # left alone
    assert prefs_mod.update_prefs({"floating_position": None}, path).floating_position is None  # null: forgotten
    for bad in ({"meeting_start": "sometimes"}, {"silence_ask_minutes": True}, {"silence_ask_minutes": -1},
                {"silence_end_minutes": 601}, {"silence_end_minutes": 1.5}, {"silence_ask_minutes": "15"},
                {"floating_position": {"x": float("inf"), "y": 0}}, {"floating_position": {"x": 1}},
                {"floating_position": {"x": 1, "y": 2, "z": 3}}, {"url_key": "x" * 43}, {"floating_button": "maybe"}):
        with pytest.raises(ValidationError):
            prefs_mod.update_prefs(bad, path)


def test_the_link_key_is_kept_out_of_every_dump(tmp_path: Path) -> None:
    path = tmp_path / "app.json"
    p = prefs_mod.with_url_key(prefs_mod.load_prefs(path), path)
    key = p.url_key
    assert prefs_mod.URL_KEY.fullmatch(key) and prefs_mod.with_url_key(p, path).url_key == key
    assert "url_key" not in p.model_dump() and key not in p.model_dump_json() and key not in repr(p)
    assert json.loads(path.read_text(encoding="utf-8"))["url_key"] == key
    assert prefs_mod.update_prefs({"hotkey_sound": False}, path).url_key == key  # carried over a change
    assert prefs_mod.load_prefs(path).url_key == key


# ------------------------------------------------------------------------ routes


@pytest.fixture
def host(tmp_path: Path) -> AppHost:
    return _manual(tmp_path, setup_done=True)


@pytest.fixture
def client(host: AppHost) -> Iterator[TestClient]:
    with TestClient(create_app(host, TOKEN), base_url=BASE, headers={TOKEN_HEADER: TOKEN}) as c:
        yield c


NEW_ROUTES = ["/api/meeting/start", "/api/meeting/stop", "/api/meeting/toggle", "/api/meeting/keep",
              "/api/suggestion/accept", "/api/suggestion/dismiss", "/api/notice/dismiss"]


@pytest.mark.parametrize("path", NEW_ROUTES)
def test_the_meeting_routes_need_the_token(host: AppHost, path: str) -> None:
    with TestClient(create_app(host, TOKEN), base_url=BASE) as anonymous:
        assert anonymous.post(path).status_code == 401
        assert anonymous.post(path, headers={TOKEN_HEADER: "wrong"}).status_code == 401
        assert anonymous.post(path, headers={TOKEN_HEADER: TOKEN, "host": "evil.example"}).status_code == 403
        assert anonymous.get(path, headers={TOKEN_HEADER: TOKEN}).status_code == 405
    assert host.assistant is None  # nothing happened


def _wait(client: TestClient, pred: Callable[[dict[str, Any]], bool], timeout: float = 10.0) -> dict[str, Any]:
    import time

    deadline = time.monotonic() + timeout
    while True:
        body = client.get("/api/app").json()
        if pred(body):
            return body
        if time.monotonic() > deadline:
            raise AssertionError(f"state stayed {body['state']!r}")
        time.sleep(0.05)


def test_meetings_over_http(client: TestClient, host: AppHost) -> None:
    body = client.post("/api/meeting/start").json()
    assert body["action"] == "start" and body["state"] == "listening" and body["in_meeting"] and body["mode"] == "manual"
    assert client.post("/api/meeting/start").json()["action"] is None  # one is recorded already
    meeting_id = body["meeting"]["id"]
    host.bus.publish("meeting.still_there", meeting_id=meeting_id, silent_minutes=15, ends_at=None)
    assert client.get("/api/app").json()["still_there"] == {"meeting_id": meeting_id, "silent_minutes": 15, "ends_at": None}
    body = client.post("/api/meeting/keep").json()
    assert body["action"] == "keep" and body["still_there"] is None
    assert client.post("/api/listen/pause").json()["state"] == "paused"
    assert client.post("/api/meeting/toggle").json()["action"] == "resume"
    body = client.post("/api/meeting/stop").json()
    assert body["action"] == "stop" and body["state"] in ("stopping", "stopped")  # at once: the wrap-up goes on
    _wait(client, lambda s: s["state"] == "stopped")
    assert client.post("/api/meeting/stop").json()["action"] is None
    assert client.post("/api/meeting/keep").json()["action"] is None
    assert client.post("/api/meeting/toggle").json()["action"] == "start"
    assert client.post("/api/meeting/toggle").json()["action"] == "stop"
    _wait(client, lambda s: s["state"] == "stopped")
    assert client.post("/api/meeting/dance").status_code == 404


def test_answers_over_http(client: TestClient, host: AppHost) -> None:
    assert client.post("/api/suggestion/dismiss").json()["action"] is None
    host.suggest_meeting("zoom.us")
    assert client.get("/api/app").json()["suggestion"]["app"] == "zoom.us"
    body = client.post("/api/suggestion/dismiss").json()
    assert body["action"] == "dismiss" and body["suggestion"] is None
    host.call_ended()
    host.suggest_meeting("zoom.us")
    body = client.post("/api/suggestion/accept").json()
    assert body["action"] == "start" and body["in_meeting"] and body["suggestion"] is None
    client.post("/api/meeting/stop")
    _wait(client, lambda s: s["state"] == "stopped")
    assert client.post("/api/notice/dismiss").json()["action"] is None
    host.notice = {"kind": "ended_after_silence", "minutes": 60}
    body = client.post("/api/notice/dismiss").json()
    assert body["action"] == "dismiss" and body["notice"] is None


def test_meeting_preferences_over_http(client: TestClient, host: AppHost) -> None:
    key = parse_qs(urlsplit(host.launcher_url()).query)["key"][0]
    changes = {"meeting_start": "automatic", "floating_button": False, "spotlight_launcher": False,
               "call_suggestions": False, "silence_ask_minutes": 20, "silence_end_minutes": 90,
               "floating_position": {"x": 10, "y": 20}}
    r = client.patch("/api/prefs", json=changes)
    assert r.status_code == 200 and key not in r.text and "url_key" not in r.json()
    prefs = client.get("/api/app").json()["prefs"]
    assert {k: prefs[k] for k in changes} == {**changes, "floating_position": {"x": 10.0, "y": 20.0, "screen": None}}
    assert client.get("/api/app").json()["mode"] == "automatic"
    assert client.patch("/api/prefs", json={"floating_position": None}).json()["floating_position"] is None
    for bad in ({"meeting_start": "sometimes"}, {"silence_ask_minutes": True}, {"silence_end_minutes": -5},
                {"silence_end_minutes": 601}, {"floating_position": {"x": "left", "y": 0}}, {"url_key": key},
                {"spotlight_launcher": "maybe"}):
        assert client.patch("/api/prefs", json=bad).status_code == 400, bad
    for path in ("/api/app", "/api/setup/complete"):
        r = client.get(path) if path == "/api/app" else client.post(path)
        assert key not in r.text, path
    assert prefs_mod.load_prefs(host.prefs_path).url_key == key  # still the same: no request may change it
