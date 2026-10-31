"""Telling the people on the meeting audio apart (voicestream/voices.py) and naming their voices."""

from __future__ import annotations

import os
import wave
from datetime import timedelta
from pathlib import Path

import numpy as np
import pytest
from helpers import WEDNESDAY, seg
from test_app_meetings import WED

from voicestream import voices
from voicestream.brain.llm import ScriptedLLM
from voicestream.brain.people import name_voice, speaker_label, voice_labels
from voicestream.brain.schema import WireSpeaker, WireSummary
from voicestream.brain.summarizer import full_transcript, summarize
from voicestream.brain.tracker import ItemTracker
from voicestream.config import Config
from voicestream.models import Meeting, TranscriptSegment
from voicestream.session import Assistant
from voicestream.storage import atomic_write, load_meeting


def _unit(*v: float) -> np.ndarray:
    a = np.array(v, dtype=np.float32)
    return a / np.linalg.norm(a)


def _voiced(text: str, voice: int | None, t: float = 0.0) -> TranscriptSegment:
    return seg(text, "system", t=t).model_copy(update={"voice": voice})


# ------------------------------------------------------------------ features


def test_fbank_has_kaldis_frames_and_puts_a_tone_in_its_band() -> None:
    t = np.arange(16000) / 16000
    tone = (8000 * np.sin(2 * np.pi * 1000 * t)).astype(np.int16)
    feats = voices.fbank(tone)
    assert feats.shape == (98, 80)  # 1 + (16000 - 400) // 160 frames of 25 ms every 10 ms
    peak = int(feats.mean(axis=0).argmax())
    mel = lambda f: 1127 * np.log(1 + f / 700)  # noqa: E731
    centre = (mel(1000) - mel(20)) / (mel(8000) - mel(20)) * 81 - 1  # the filter centred on 1 kHz
    assert abs(peak - centre) <= 1
    assert voices.fbank(np.zeros(100, dtype=np.int16)).shape == (0, 80)


def test_fbank_matches_kaldi() -> None:
    knf = pytest.importorskip("kaldi_native_fbank", reason="pip install kaldi-native-fbank to compare with Kaldi")
    x = (np.random.default_rng(0).normal(0, 3000, 24000)).astype(np.float32)
    opts = knf.FbankOptions()
    opts.frame_opts.dither, opts.frame_opts.window_type = 0, "hamming"
    opts.mel_opts.num_bins, opts.mel_opts.low_freq, opts.mel_opts.high_freq = 80, 20, 0
    f = knf.OnlineFbank(opts)
    f.accept_waveform(16000, x.tolist())
    f.input_finished()
    ref = np.stack([np.array(f.get_frame(i)) for i in range(f.num_frames_ready)])
    assert np.abs(ref - voices.fbank(x)).max() < 1e-3


# ---------------------------------------------------------------- clustering


def test_lines_are_grouped_by_voice() -> None:
    c = voices.VoiceClusters()
    a, b = _unit(1, 0.1, 0), _unit(0, 1, 0.1)
    assert c.assign(a, 0, 3) == 1
    assert c.assign(b, 4, 6) == 2
    assert c.assign(_unit(0.9, 0.2, 0.1), 7, 9) == 1  # close to the first voice
    assert c.assign(_unit(0.1, 0.1, 1), 10, 12) == 3  # far from both: someone new
    # too short to tell: the voice of the line just before, if it was close in time; else none
    assert c.assign(None, 12.5, 13) == 3
    assert c.assign(None, 30, 31) is None


def test_a_meeting_never_has_more_voices_than_the_cap() -> None:
    c = voices.VoiceClusters()
    eye = np.eye(voices.MAX_VOICES + 3, dtype=np.float32)
    got = [c.assign(eye[i], i, i + 1) for i in range(len(eye))]
    assert max(n for n in got if n is not None) == voices.MAX_VOICES


def test_the_ring_returns_what_was_heard_between_two_times() -> None:
    ring = voices.AudioRing(seconds=2.0)
    for k in range(5):  # 0.5 s frames, each filled with its number
        ring.add(k * 0.5, np.full(8000, k, dtype=np.int16))
    clip = ring.clip(0.75, 1.25)
    assert clip.size == 8000 and set(clip[:4000]) == {1} and set(clip[4000:]) == {2}
    ring.add(5.0, np.full(8000, 9, dtype=np.int16))  # older than 2 s is let go
    assert ring.clip(0, 1).size == 0


# ------------------------------------------------------------------- labels


def test_one_voice_stays_the_meeting_audio_two_are_told_apart() -> None:
    m = Meeting(participants=["Sam"])
    m.segments = [seg("Hello", "mic"), _voiced("Hi", 1, t=3)]
    assert voice_labels(m) is None  # one voice: the meeting audio, and the one other person's name as before
    assert speaker_label(m.segments[1], "Sam", "Others", voice_labels(m)) == "Sam"
    m.segments.append(_voiced("Me too", 2, t=6))
    labels = voice_labels(m)
    assert labels == {1: "Speaker 1", 2: "Speaker 2"}
    assert [speaker_label(s, "Sam", "Others", labels) for s in m.segments] == ["Me", "Speaker 1", "Speaker 2"]
    assert speaker_label(_voiced("Mm", None), "Sam", "Others", labels) == "Others"  # not told: not anyone's


def test_names_you_give_win_over_claudes() -> None:
    m = Meeting(segments=[_voiced("Hi", 1)])
    assert name_voice(m, 1, "Julie", by_you=True)
    assert voice_labels(m) == {1: "Julie"}  # one voice, but named: it carries the name
    assert not name_voice(m, 1, "Julia", by_you=False)
    assert m.voices == {"1": "Julie"}
    assert name_voice(m, 1, "", by_you=True)  # taken back: Speaker 1 again, Claude may name it
    assert m.voices == {} and m.voices_named_by_you == []
    assert name_voice(m, 1, "Julia", by_you=False) and m.voices == {"1": "Julia"}


async def test_claude_names_the_voices_it_can_tell(cfg: Config) -> None:
    m = Meeting(segments=[seg("Julie, can you share?", "mic"), _voiced("Sure, sharing now", 1, t=3),
                          _voiced("I'm Paul from finance", 2, t=8), _voiced("Thanks", 3, t=12)])
    name_voice(m, 3, "Sam", by_you=True)
    assert "[#1 00:03 Speaker 1] Sure, sharing now" in full_transcript(m)
    cfg.brain.provider = "anthropic"
    reply = WireSummary(title="Sync", tldr="", notes_markdown="", updates=[], missing_items=[],
                        participants=["Julie", "Paul"], language="en", followup_subject="", followup_body="",
                        speakers=[WireSpeaker(speaker="Speaker 1", name="Julie"), WireSpeaker(speaker="speaker 2", name="Paul"),
                                  WireSpeaker(speaker="Speaker 3", name="Samuel"), WireSpeaker(speaker="Bob", name="Bob")])
    await summarize(cfg, m, ItemTracker(m, None, "Alex", today=WEDNESDAY), ScriptedLLM([reply]))
    assert m.voices == {"1": "Julie", "2": "Paul", "3": "Sam"}  # yours stays; a label that isn't a voice is ignored


# ------------------------------------------------------------------ session


class ByLoudness:
    """A stand-in for the model: a line's "voice" is how loud it is (two people, one quiet and one loud)."""

    def embed(self, samples: np.ndarray) -> np.ndarray | None:
        if samples.size < voices.SAMPLE_RATE:
            return None
        return _unit(1, 0) if int(np.abs(samples).max()) < 5000 else _unit(0, 1)


async def test_the_meeting_audio_lines_get_their_voices(cfg: Config) -> None:
    cfg.capture.system.voices = True
    a = Assistant(cfg, sources=[], llm=None, sinks=[], always_on=True)
    ring = a._voice_rings["system"] = voices.AudioRing()
    a._embedder = ByLoudness()
    for k, level in enumerate([2000, 2000, 9000, 9000, 2000, 0, 0, 0]):  # 2 s each: quiet, loud, quiet, silence
        ring.add(k * 2.0, np.full(32000, level, dtype=np.int16))
    for text, t in (("Hello", 0.0), ("Bonjour", 4.0), ("Merci", 8.0), ("…", 13.5)):  # "…": silence, no voice
        await a._route(seg(text, "system", t=t, dur=2.0))
    await a._route(seg("And me", "mic", t=15.0, dur=1.0))
    m = a.current.meeting
    assert [(s.text, s.voice) for s in m.segments] == [("Hello", 1), ("Bonjour", 2), ("Merci", 1), ("…", None), ("And me", None)]
    labels = [ev.data["labels"] for ev in a.bus.history if ev.type == "meeting.voices"]
    assert labels == [{"1": "Speaker 1", "2": "Speaker 2"}]  # said once, when a second voice made them worth telling
    a.current.name_voice(2, "Julie")
    assert "Julie" in m.participants and m.voices == {"2": "Julie"}
    assert a.current.speaker(m.segments[1]) == "Julie"
    with pytest.raises(KeyError):
        a.current.name_voice(7, "Nobody")
    await a.end_meeting("manual")


async def test_without_the_model_nothing_changes(cfg: Config) -> None:
    cfg.capture.system.voices = False
    a = Assistant(cfg, sources=[], llm=None, sinks=[], always_on=True)
    await a._route(seg("Hello", "system"))
    assert a._voice_rings == {} and a.current.meeting.segments[0].voice is None
    await a.end_meeting("manual")


# ------------------------------------------------------------------ the API


def test_naming_a_voice_of_a_past_meeting(tmp_path: Path) -> None:
    from starlette.testclient import TestClient
    from test_app_host import _factory, _write_config

    from voicestream.app.host import AppHost
    from voicestream.dashboard.server import TOKEN_HEADER, create_app

    host = AppHost(_write_config(tmp_path), process_setup=False, prefs_path=tmp_path / "app.json", assistant_factory=_factory())
    m = Meeting(title="Sync", started_at=WED, ended_at=WED + timedelta(minutes=30))
    m.segments = [seg("Julie, ça va ?", "mic"), _voiced("Oui !", 1, t=3), _voiced("Moi aussi", 2, t=6)]
    folder = host.cfg.data_path / "sessions" / "2026-10-14_090000-sync"
    atomic_write(folder / "meeting.json", m.model_dump_json())
    with TestClient(create_app(host, "t"), base_url="http://127.0.0.1:8765", headers={TOKEN_HEADER: "t"}) as c:
        r = c.post(f"/api/meetings/{folder.name}/voices", json={"voice": 1, "name": "Julie"})
        assert r.status_code == 200 and r.json()["labels"] == {"1": "Julie", "2": "Speaker 2"}
        assert c.post(f"/api/meetings/{folder.name}/voices", json={"voice": 9, "name": "X"}).status_code == 404
        assert c.post(f"/api/meetings/{folder.name}/voices", json={"voice": 1, "name": "a\nb"}).status_code == 400
    saved, _ = load_meeting(folder)
    assert saved.voices == {"1": "Julie"} and "Julie" in saved.participants
    transcript = (folder / "transcript.md").read_text(encoding="utf-8")
    assert "Julie:** Oui !" in transcript and "Speaker 2:** Moi aussi" in transcript


# ------------------------------------------------------------- the real model


def _wav(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def test_the_real_model_tells_real_people_apart() -> None:
    """With ``VOICESTREAM_VOICE_TEST_DIR``: a folder holding the model and sherpa-onnx's speaker test recordings
    (github.com/csukuangfj/sr-data, under ``sr-data/``): seven people, cut into 1.5–4 s lines, mixed up."""
    root = os.environ.get("VOICESTREAM_VOICE_TEST_DIR")
    if not root:
        pytest.skip("set VOICESTREAM_VOICE_TEST_DIR to run the voice model on real recordings")
    emb = voices.OnnxEmbedder(Path(root) / voices.MODEL_NAME)

    def who(p: Path) -> str:  # 3D-Speaker's speaker1_a_cn and speaker1_a_en are two different people
        parts = p.stem.split("_")
        return p.stem.split("-")[0] if len(parts) == 1 else parts[0] + parts[2]

    rng = np.random.default_rng(0)
    lines = []
    for p in sorted(Path(root, "sr-data").rglob("*.wav")):
        audio = _wav(p)
        for i in range(0, audio.size - 32000, 48000):
            lines.append((who(p), audio[i : i + int(rng.uniform(1.5, 4) * 16000)]))
    rng.shuffle(lines)
    c = voices.VoiceClusters()
    found: dict[int | None, list[str]] = {}
    for k, (person, audio) in enumerate(lines):
        found.setdefault(c.assign(emb.embed(audio), k * 10.0, k * 10.0 + 2), []).append(person)
    wrong = sum(len(ps) - max(ps.count(x) for x in ps) for ps in found.values())
    assert len({p for p, _ in lines}) == 7 and 7 <= len(c.voices) <= 8 and wrong == 0, found
