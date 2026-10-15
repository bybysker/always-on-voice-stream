from __future__ import annotations

import json
from pathlib import Path

import pytest

from voicestream import cli
from voicestream.models import Meeting
from voicestream.storage import list_sessions, load_meeting
from voicestream.transcript_import import parse_transcript

VTT = """WEBVTT

1
00:00:01.000 --> 00:00:04.500
<v Sam Lee>Alex, can you send the revised pricing</v>

2
00:00:04.600 --> 00:00:06.000
<v Sam Lee>to Acme by Friday?</v>

3
00:00:07.000 --> 00:00:09.000
<v Alex>Yes, I'll do that.</v>
"""

SRT = """1
00:00:01,000 --> 00:00:03,000
Sam: We decided to go with the annual plan.

2
00:01:02,500 --> 00:01:05,000
Alex: I'll book the venue by tomorrow.
"""


def test_vtt_merges_split_cues_and_maps_speakers(tmp_path: Path) -> None:
    p = tmp_path / "zoom.vtt"
    p.write_text(VTT)
    segs = parse_transcript(p, user_name="Alex")
    assert [s.speaker for s in segs] == ["Sam Lee", "Me"]
    assert segs[0].text == "Alex, can you send the revised pricing to Acme by Friday?"
    assert segs[0].source == "system" and segs[1].source == "mic"
    assert segs[0].t_start == pytest.approx(1.0) and segs[0].t_end == pytest.approx(6.0)


def test_srt_with_inline_speakers(tmp_path: Path) -> None:
    p = tmp_path / "teams.srt"
    p.write_text(SRT)
    segs = parse_transcript(p, user_name="Alex Doe")
    assert [(s.speaker, s.source) for s in segs] == [("Sam", "system"), ("Me", "mic")]
    assert segs[1].t_start == pytest.approx(62.5)


def test_plain_text_with_and_without_timestamps(tmp_path: Path) -> None:
    p = tmp_path / "notes.txt"
    p.write_text("# exported\n[00:10] Sam: Can you update the doc?\nMe: Sure, by Monday.\ncontinued thought\n")
    segs = parse_transcript(p, user_name="Alex")
    assert [s.speaker for s in segs] == ["Sam", "Me", "Me"]
    assert segs[0].t_start == pytest.approx(10.0)
    assert segs[1].t_start >= segs[0].t_end
    assert segs[2].text == "continued thought"


def test_jsonl_roundtrip(tmp_path: Path) -> None:
    from voicestream.transcript_import import dump_jsonl

    src = tmp_path / "a.txt"
    src.write_text("Sam: hello there\nMe: hi\n")
    segs = parse_transcript(src)
    p = tmp_path / "t.jsonl"
    p.write_text(dump_jsonl(segs))
    again = parse_transcript(p)
    assert [s.text for s in again] == [s.text for s in segs]


def _write_cfg(tmp_path: Path) -> Path:
    cfgp = tmp_path / "voicestream.toml"
    cfgp.write_text(
        f'user_name = "Alex"\ndata_dir = "{(tmp_path / "data").as_posix()}"\n'
        "[brain]\nprovider = \"heuristic\"\n[agents]\nmode = \"off\"\n[dashboard]\nenabled = false\n"
    )
    return cfgp


def test_cli_import_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    cfgp = _write_cfg(tmp_path)
    p = tmp_path / "zoom.vtt"
    p.write_text(VTT)
    assert cli.main(["-c", str(cfgp), "import", str(p), "--title", "Pricing sync"]) == 0
    sessions = list_sessions(tmp_path / "data")
    assert len(sessions) == 1
    meeting, folder = load_meeting(sessions[0])
    assert isinstance(meeting, Meeting) and meeting.title == "Pricing sync"
    titles = [i.title.lower() for i in meeting.items.values()]
    assert any("revised pricing" in t for t in titles)
    assert (folder / "notes.md").read_text().startswith("# Pricing sync")
    assert "Send the revised pricing" in (tmp_path / "data" / "TODO.md").read_text()
    out = capsys.readouterr().out
    assert "meeting ended" in out

    # follow-up commands on the saved session
    assert cli.main(["-c", str(cfgp), "sessions"]) == 0
    assert "Pricing sync" in capsys.readouterr().out
    assert cli.main(["-c", str(cfgp), "summarize", "last"]) == 0
    assert "## Action items" in capsys.readouterr().out
    assert cli.main(["-c", str(cfgp), "push", "last"]) == 0
    assert "pushed/updated" in capsys.readouterr().out
    assert cli.main(["-c", str(cfgp), "cost", "--hours", "8"]) == 0
    assert "estimate for 8 h" in capsys.readouterr().out


def test_cli_replay_wav(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from helpers import bursts, write_wav

    monkeypatch.chdir(tmp_path)
    cfgp = _write_cfg(tmp_path)
    cfgp.write_text(cfgp.read_text() + '[transcription]\nbackend = "fake"\n')
    wav = write_wav(tmp_path / "rec.wav", bursts(2))
    assert cli.main(["-c", str(cfgp), "replay", str(wav)]) == 0
    meeting, _ = load_meeting(list_sessions(tmp_path / "data")[0])
    assert len(meeting.segments) == 2 and meeting.segments[0].text.startswith("(speech")


def test_cli_init_writes_templates(tmp_path: Path) -> None:
    target = tmp_path / "cfg" / "voicestream.toml"
    assert cli.main(["init", str(target)]) == 0
    assert target.is_file() and (target.parent / ".env").is_file()
    from voicestream.config import load_config

    load_config(target)  # the shipped template must validate
    assert cli.main(["init", str(target)]) == 1  # refuses to overwrite


def test_a_relative_data_dir_is_made_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # `--data-dir ./x`: the notes and agent-file paths sent to the dashboard must not depend on the working dir
    from voicestream.config import Config

    monkeypatch.chdir(tmp_path)
    assert Config(data_dir="./x").data_path == tmp_path / "x"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert Config(data_dir="~/.voicestream").data_path == tmp_path / "home" / ".voicestream"


def test_cli_unknown_session(tmp_path: Path) -> None:
    cfgp = _write_cfg(tmp_path)
    with pytest.raises(SystemExit):
        cli.main(["-c", str(cfgp), "summarize", "nope"])


def test_parser_has_all_commands() -> None:
    parser = cli.build_parser()
    actions = next(a for a in parser._actions if a.dest == "command")  # noqa: SLF001
    assert set(actions.choices) >= {"run", "replay", "import", "devices", "doctor", "init", "sessions", "summarize", "push", "cost"}
    json.dumps(sorted(actions.choices))
