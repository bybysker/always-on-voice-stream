"""Session folders on disk: naming, append-only transcripts, atomic state saves, audio and reload."""

from __future__ import annotations

import json
import os
import wave
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from helpers import seg
from voicestream.models import AgentRun, AgentStatus, AudioFrame, Item, ItemKind, Meeting, TranscriptSegment
from voicestream.storage import SessionStore, atomic_write, list_sessions, load_meeting, slugify

STARTED = datetime(2026, 10, 14, 13, 30, 12, tzinfo=timezone.utc)


def _stamp(dt: datetime = STARTED) -> str:
    return dt.astimezone().strftime("%Y-%m-%d_%H%M%S")


def _meeting(**kw: object) -> Meeting:
    return Meeting(started_at=STARTED, **kw)  # type: ignore[arg-type]


def test_slugify() -> None:
    assert slugify("Q4 Planning — Acme & Globex!") == "q4-planning-acme-globex"
    assert slugify("   ") == slugify("!!!") == "meeting"
    long = slugify("a very long meeting title that keeps going and going past the limit")
    assert len(long) <= 40 and not long.endswith("-")


def test_folder_is_named_after_the_local_start_time(tmp_path: Path) -> None:
    store = SessionStore(tmp_path, _meeting())
    assert store.dir == tmp_path / "sessions" / _stamp()
    assert store.agents_dir == store.dir / "agents" and store.screens_dir == store.dir / "screens"
    header = (store.dir / "transcript.md").read_text(encoding="utf-8")
    assert header == f"# Transcript — {STARTED.astimezone():%Y-%m-%d %H:%M}\n\n"


def test_meetings_started_in_the_same_second_get_separate_folders(tmp_path: Path) -> None:
    first = SessionStore(tmp_path, _meeting())
    first.save()
    second = SessionStore(tmp_path, _meeting())
    second.save()
    third = SessionStore(tmp_path, _meeting())
    assert [first.dir.name, second.dir.name, third.dir.name] == [_stamp(), f"{_stamp()}-2", f"{_stamp()}-3"]


def test_transcript_is_appended_as_markdown_and_jsonl(tmp_path: Path) -> None:
    store = SessionStore(tmp_path, _meeting())
    a = seg("Can you send the deck?", "system", t=65.2)
    b = seg("Sure, by Friday.", "mic", t=70.0)
    store.append_segment(a)
    store.append_segment(b)
    lines = (store.dir / "transcript.jsonl").read_text(encoding="utf-8").splitlines()
    assert [TranscriptSegment.model_validate_json(x) for x in lines] == [a, b]
    md = (store.dir / "transcript.md").read_text(encoding="utf-8")
    assert md.endswith("**[01:05] Others:** Can you send the deck?\n\n**[01:10] Me:** Sure, by Friday.\n\n")


def test_save_writes_the_full_meeting_state_atomically(tmp_path: Path) -> None:
    m = _meeting(title="Sync")
    store = SessionStore(tmp_path, m)
    store.save()
    m.items["A1"] = Item(id="A1", kind=ItemKind.ACTION, title="Send deck")
    store.save()
    data = json.loads((store.dir / "meeting.json").read_text(encoding="utf-8"))
    assert data["title"] == "Sync" and data["items"]["A1"]["title"] == "Send deck"
    assert not list(store.dir.glob("*.tmp"))


def test_atomic_write_never_leaves_a_half_written_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "deep" / "dir" / "state.json"
    atomic_write(target, "old")
    assert target.read_text(encoding="utf-8") == "old"

    def crash(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", crash)
    with pytest.raises(OSError):
        atomic_write(target, "new")
    assert target.read_text(encoding="utf-8") == "old"


def test_save_notes(tmp_path: Path) -> None:
    store = SessionStore(tmp_path, _meeting())
    path = store.save_notes("# Notes\n")
    assert path == store.dir / "notes.md" and path.read_text(encoding="utf-8") == "# Notes\n"


def test_rename_with_title_appends_a_slug_once(tmp_path: Path) -> None:
    m = _meeting()
    store = SessionStore(tmp_path, m)
    store.rename_with_title()  # no title yet: nothing to do
    assert store.dir.name == _stamp()
    m.title = "Weekly Sync: Q4 plans!"
    store.rename_with_title()
    assert store.dir == tmp_path / "sessions" / f"{_stamp()}-weekly-sync-q4-plans"
    assert store.dir.is_dir() and not (tmp_path / "sessions" / _stamp()).exists()
    assert (store.dir / "meeting.json").is_file()  # state saved before the move
    store.append_segment(seg("after the rename"))
    assert "after the rename" in (store.dir / "transcript.md").read_text(encoding="utf-8")
    m.title = "Another title"
    store.rename_with_title()
    assert store.dir.name == f"{_stamp()}-weekly-sync-q4-plans"


def test_rename_with_title_keeps_the_folder_when_the_target_exists(tmp_path: Path) -> None:
    m = _meeting(title="Sync")
    store = SessionStore(tmp_path, m)
    (tmp_path / "sessions" / f"{_stamp()}-sync").mkdir()
    store.rename_with_title()
    assert store.dir.name == _stamp() and store.dir.is_dir()


@pytest.mark.xfail(strict=True, reason="BUG: rename_with_title treats the '-2' collision suffix as an existing title slug")
def test_rename_with_title_works_for_a_collision_folder(tmp_path: Path) -> None:
    SessionStore(tmp_path, _meeting()).save()
    second = SessionStore(tmp_path, _meeting(title="Design review"))
    second.rename_with_title()
    assert second.dir.name == f"{_stamp()}-2-design-review"


def _frame(source: str, values: list[int]) -> AudioFrame:
    return AudioFrame(source, np.array(values, dtype=np.int16), 0.0)


def test_write_audio_is_a_no_op_unless_enabled(tmp_path: Path) -> None:
    store = SessionStore(tmp_path, _meeting())
    store.write_audio(_frame("mic", [1, 2, 3]))
    store.close()
    assert not list(store.dir.glob("audio-*.wav"))


def test_write_audio_keeps_one_wav_per_source(tmp_path: Path) -> None:
    store = SessionStore(tmp_path, _meeting(), store_audio=True)
    store.write_audio(_frame("mic", [1, -2, 3]))
    store.write_audio(_frame("system", [7, 8]))
    store.write_audio(_frame("mic", [32767, -32768]))
    store.close()
    for source, expected in (("mic", [1, -2, 3, 32767, -32768]), ("system", [7, 8])):
        with wave.open(str(store.dir / f"audio-{source}.wav"), "rb") as w:
            assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 16_000)
            assert np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").tolist() == expected
    assert (store.dir / "meeting.json").is_file()  # close() also saves


def test_load_meeting_round_trips_from_folder_or_file(tmp_path: Path) -> None:
    m = _meeting(title="Pricing", participants=["Sam"])
    m.segments.append(seg("Can you research Globex pricing?", "system", t=3.0))
    m.items["A1"] = Item(
        id="A1", kind=ItemKind.ACTION, title="Research Globex pricing", owner="Assistant",
        segment_ids=[m.segments[0].id], pushed={"markdown": "file:///TODO.md#x"}, pushed_revision={"markdown": 1},
        agent=AgentRun(prompt="Research Globex pricing", status=AgentStatus.DONE, result_files=["/w/RESULT.md"]),
    )
    store = SessionStore(tmp_path, m)
    store.save()
    loaded, folder = load_meeting(store.dir)
    assert loaded == m and folder == store.dir
    loaded2, folder2 = load_meeting(store.dir / "meeting.json")
    assert loaded2 == m and folder2 == store.dir


def test_load_meeting_without_state_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    with pytest.raises(FileNotFoundError):
        load_meeting(tmp_path / "empty")


def test_list_sessions_newest_first_and_only_saved_ones(tmp_path: Path) -> None:
    assert list_sessions(tmp_path) == []
    stores = []
    for hour in (9, 15, 11):
        s = SessionStore(tmp_path, Meeting(started_at=STARTED.replace(hour=hour)))
        s.save()
        stores.append(s)
    (tmp_path / "sessions" / "2099-01-01_000000").mkdir()  # no meeting.json: not a session
    assert list_sessions(tmp_path) == [stores[1].dir, stores[2].dir, stores[0].dir]
