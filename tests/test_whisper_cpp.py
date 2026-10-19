"""whisper.cpp backend: the whisper-server supervisor, the request/reply mapping, model files.

Most tests drive ``fake_whisper_server.py`` (same flags and HTTP API as the real server,
scripted replies). ``test_real_whisper_server_*`` run the real binary when one is found
(``VOICESTREAM_WHISPER_SERVER``, PATH or Homebrew) with whisper.cpp's empty test model,
or a real model given by ``VOICESTREAM_WHISPER_TEST_MODEL`` (the macOS CI job uses
large-v3-turbo-q5_0 and speech made with ``say``).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
import wave
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest

from helpers import bursts, noise, speechlike, write_wav
from voicestream import cli, stt_models
from voicestream.audio.base import float_to_int16
from voicestream.audio.file import WavFileSource
from voicestream.config import Config
from voicestream.models import SAMPLE_RATE, Utterance
from voicestream.session import Assistant
from voicestream.transcribe import build_transcriber
from voicestream.transcribe import whisper_cpp as wc
from voicestream.transcribe.base import TranscriptionError

FAKE = Path(__file__).with_name("fake_whisper_server.py")


def reply(text: str, language: str = "french", no_speech: float = 0.01, logprob: float = -0.2) -> dict[str, Any]:
    return {
        "task": "transcribe", "language": language, "duration": 2.0, "text": f" {text}\n",
        "segments": [{"id": 0, "text": f" {text}", "start": 0.0, "end": 2.0, "tokens": [1, 2],
                      "temperature": 0.0, "avg_logprob": logprob, "no_speech_prob": no_speech}],
    }  # fmt: skip


@pytest.fixture
def whisper_cfg(cfg: Config) -> Config:
    cfg.transcription.backend = "whisper-cpp"
    cfg.transcription.whisper_cpp.binary = str(FAKE)
    cfg.transcription.whisper_cpp.startup_timeout_s = 20
    cfg.transcription.whisper_cpp.timeout_s = 20
    return cfg


def install_model(cfg: Config, name: str = stt_models.DEFAULT_MODEL, script: dict[str, Any] | None = None) -> Path:
    p = stt_models.model_path(cfg, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(stt_models.GGML_MAGIC + b"\0" * 64)
    if script is not None:
        Path(str(p) + ".json").write_text(json.dumps(script))
    return p


def requests_seen(model: Path) -> list[dict[str, Any]]:
    log = Path(str(model) + ".requests.jsonl")
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def utt(seconds: float = 2.0, seed: int = 1) -> Utterance:
    pcm = float_to_int16(speechlike(seconds, seed=seed))
    return Utterance(source="mic", samples=pcm, t_start=0.0, t_end=seconds, speech_seconds=seconds)


@pytest.fixture(autouse=True)
def _no_shared_servers() -> Any:
    yield
    leftover = list(wc.WhisperServer._shared.values())
    wc.WhisperServer._shared.clear()
    for s in leftover:  # a test that failed half-way must not leave processes behind
        if s.proc is not None and s.proc.returncode is None:
            s.proc.kill()


# ------------------------------------------------------------------ reply parsing


def test_parse_reply_joins_segments_and_maps_the_language() -> None:
    data = reply("Bonjour à tous.")
    data["segments"].append({"text": " On commence.", "avg_logprob": -0.4, "no_speech_prob": 0.02})
    text, conf, lang = wc.parse_reply(data)
    assert text == "Bonjour à tous. On commence." and lang == "fr"
    assert conf is not None and 0.6 < conf < 0.8


def test_parse_reply_drops_what_whisper_heard_in_silence() -> None:
    # whisper's own rule (no-speech probability high AND decoding unsure) and near-certain silence
    assert wc.parse_reply(reply("Sous-titres réalisés par la communauté d'Amara.org", no_speech=0.7, logprob=-1.3))[0] == ""
    assert wc.parse_reply(reply("Merci.", no_speech=0.95, logprob=-0.3))[0] == ""
    assert wc.parse_reply(reply("Oui, d'accord.", no_speech=0.7, logprob=-0.4))[0] == "Oui, d'accord."


def test_parse_reply_ignores_the_stale_language_of_an_empty_reply() -> None:
    assert wc.parse_reply({"language": "english", "segments": [], "text": ""}) == ("", None, None)
    assert wc.parse_reply({"text": " Hello there\n"}) == ("Hello there", None, None)  # json format: no language
    assert wc.parse_reply({"language": "klingon", **reply("x")}  # type: ignore[arg-type]
                          | {"language": "klingon"})[2] is None


def test_language_table_matches_whisper_cpp() -> None:
    assert len(wc.CODES) == 100 and wc.CODES["fr"] == "french" and wc.NAMES["haitian creole"] == "ht"


# ------------------------------------------------------------------ the server process


async def test_transcribes_through_whisper_server(whisper_cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wc, "usable_cpus", lambda: 8)  # voice detection would fit, but its model isn't downloaded
    model = install_model(whisper_cfg, script={"load_s": 0.3, "replies": [reply("On valide le devis.")]})
    t = build_transcriber(whisper_cfg, "mic")
    try:
        res = await t.transcribe_utterance(utt(), None)  # type: ignore[attr-defined]
    finally:
        await t.aclose()
    assert (res.text, res.language) == ("On valide le devis.", "fr")
    (req,) = requests_seen(model)
    assert req["fields"] == {
        "response_format": "verbose_json", "language": "auto", "temperature": "0.0", "temperature_inc": "0.2",
        "no_speech_thold": "0.6", "suppress_nst": "true", "no_language_probabilities": "true",
        "token_timestamps": "false", "vad": "false",
    }  # fmt: skip
    assert req["file_bytes"] == 44 + 2 * 2 * SAMPLE_RATE  # a 16 kHz mono 16-bit WAV
    assert req["path"].count("/") == 2 and len(req["path"].split("/")[1]) == 16  # behind the random token
    argv = json.loads(Path(str(model) + ".argv.json").read_text())
    assert argv[:2] == ["-m", str(model)] and ["--host", "127.0.0.1"] == argv[2:4]
    assert {"-nlp", "-sns"} <= set(argv) and argv[argv.index("-l") + 1] == "auto"
    assert "--vad" not in argv  # no Silero model downloaded: whisper-server would fail every request


async def test_long_sentences_come_back_whole(whisper_cfg: Config) -> None:
    sentence = ("Pour le trimestre prochain, on garde la facturation annuelle pour les clients entreprise "
                "et on revoit la grille tarifaire avec l'équipe commerciale avant vendredi.")
    model = install_model(whisper_cfg, script={"replies": [reply(sentence)]})
    t = build_transcriber(whisper_cfg, "mic")
    try:
        res = await t.transcribe_utterance(utt(), None)  # type: ignore[attr-defined]
    finally:
        await t.aclose()
    assert res.text == sentence  # no word cut in two
    assert requests_seen(model)[0]["fields"]["token_timestamps"] == "false"
    # and pieces that were cut anyway (another client's settings, a long window) join back whole
    pieces = {"language": "french", "segments": [{"text": " Pour le trim", "no_speech_prob": 0.0}, {"text": "estre prochain.", "no_speech_prob": 0.0}]}
    assert wc.parse_reply(pieces)[0] == "Pour le trimestre prochain."


async def test_vocabulary_is_the_only_prompt(whisper_cfg: Config) -> None:
    whisper_cfg.brain.vocabulary = ["Promethee", "HubSpot"]
    model = install_model(whisper_cfg, script={"replies": [reply("Premier."), reply("Deuxième.")]})
    t = build_transcriber(whisper_cfg, "mic")
    frames = WavFileSource(write_wav(whisper_cfg.data_path / "two.wav", bursts(2)), speed=0).frames()
    try:
        texts = [s.text async for s in t.stream("mic", "Me", frames)]
    finally:
        await t.aclose()
    assert texts == ["Premier.", "Deuxième."]
    # earlier text is never fed back (an invented sentence or a wrong language would carry over)
    assert [r["fields"].get("prompt") for r in requests_seen(model)] == ["Promethee, HubSpot, Alex"] * 2


async def test_voice_detection_is_used_when_its_model_is_there(whisper_cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wc, "usable_cpus", lambda: 8)  # an M1
    model = install_model(whisper_cfg, script={"replies": [reply("Oui.")]})
    vad = install_model(whisper_cfg, stt_models.VAD_MODEL)
    t = build_transcriber(whisper_cfg, "mic")
    try:
        await t.transcribe_utterance(utt(), None)  # type: ignore[attr-defined]
    finally:
        await t.aclose()
    argv = json.loads(Path(str(model) + ".argv.json").read_text())
    assert argv[argv.index("--vad"):argv.index("--vad") + 3] == ["--vad", "-vm", str(vad)]
    assert requests_seen(model)[0]["fields"]["vad"] == "true"


async def test_voice_detection_is_skipped_with_4_cores_or_fewer(
    whisper_cfg: Config, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """whisper.cpp runs it on 4 threads whatever the machine: on a 3-core Mac each 8-second clip took
    52 s with it and 11 s without, so the meeting's transcript fell further and further behind."""
    monkeypatch.setattr(wc, "usable_cpus", lambda: 3)
    monkeypatch.setattr(wc, "_vad_off_said", False)
    caplog.set_level(logging.INFO, logger="voicestream")
    model = install_model(whisper_cfg, script={"replies": [reply("Oui.")]})
    install_model(whisper_cfg, stt_models.VAD_MODEL)
    for source in ("mic", "system"):  # each source and restart acquires the server again
        t = build_transcriber(whisper_cfg, source)
        try:
            assert (await t.transcribe_utterance(utt(), None)).text == "Oui."  # type: ignore[attr-defined]
        finally:
            await t.aclose()
    assert "--vad" not in json.loads(Path(str(model) + ".argv.json").read_text())
    assert [r["fields"]["vad"] for r in requests_seen(model)] == ["false", "false"]
    said = [(r.levelno, r.getMessage()) for r in caplog.records if "voice detection is off" in r.getMessage()]
    # once, and below WARNING: the app shows every voicestream warning as a problem to fix
    assert said == [(logging.INFO, "whisper.cpp voice detection is off: it needs more than 4 CPU cores and this computer gives us 3")]
    status, detail = wc.check(whisper_cfg)  # nothing for you to fix, so not a warning
    assert status == "ok" and detail.endswith("(voice detection off: needs more than 4 CPU cores)")
    monkeypatch.setattr(wc, "usable_cpus", lambda: 4)
    assert not wc.vad_fits()
    monkeypatch.setattr(wc, "usable_cpus", lambda: 5)
    assert wc.vad_fits()


async def test_a_language_you_dont_speak_is_retried_in_one_you_do(whisper_cfg: Config) -> None:
    whisper_cfg.transcription.whisper_cpp.allowed_languages = ["fr", "en"]
    model = install_model(whisper_cfg, script={
        "replies": [reply("Ja, dat is goed.", language="dutch")],
        "by_language": {"fr": reply("Oui, c'est bon.")},
    })  # fmt: skip
    t = build_transcriber(whisper_cfg, "mic")
    try:
        res = await t.transcribe_utterance(utt(), None)  # type: ignore[attr-defined]
    finally:
        await t.aclose()
    assert (res.text, res.language) == ("Oui, c'est bon.", "fr")
    assert [r["fields"]["language"] for r in requests_seen(model)] == ["auto", "fr"]


async def test_a_dead_server_is_restarted(whisper_cfg: Config) -> None:
    model = install_model(whisper_cfg, script={"exit_after": 1, "replies": [reply("Un.")]})
    t = build_transcriber(whisper_cfg, "mic")
    try:
        first = await t.transcribe_utterance(utt(), None)  # type: ignore[attr-defined]
        pid = t.server.proc.pid  # type: ignore[attr-defined]
        await asyncio.sleep(0.3)  # it died after answering
        second = await t._transcribe_with_retry(utt(), None)  # type: ignore[attr-defined]
        assert t.server.proc.pid != pid  # type: ignore[attr-defined]
    finally:
        await t.aclose()
    assert first.text == "Un." and second is not None and second.text == "Un."
    assert len(requests_seen(model)) == 2


async def test_a_server_that_keeps_dying_stops_the_pipeline_with_its_log(whisper_cfg: Config) -> None:
    install_model(whisper_cfg, script={"exit_at_start": 0})  # what bad flags do: print an error, exit 0
    t = build_transcriber(whisper_cfg, "mic")
    try:
        for _ in range(3):
            with pytest.raises(TranscriptionError) as exc:
                await t.transcribe_utterance(utt(), None)  # type: ignore[attr-defined]
            assert exc.value.retryable and "invalid argument" in str(exc.value)
        with pytest.raises(TranscriptionError) as exc:
            await t.transcribe_utterance(utt(), None)  # type: ignore[attr-defined]
        assert exc.value.fatal and "keeps stopping" in str(exc.value)
    finally:
        await t.aclose()


async def test_bad_audio_is_dropped_and_server_errors_retried(whisper_cfg: Config) -> None:
    install_model(whisper_cfg, script={"status": 400})
    t = build_transcriber(whisper_cfg, "mic")
    try:
        with pytest.raises(TranscriptionError) as exc:
            await t.transcribe_utterance(utt(), None)  # type: ignore[attr-defined]
        assert not exc.value.retryable and not exc.value.fatal
    finally:
        await t.aclose()
    install_model(whisper_cfg, script={"status": 500})
    t = build_transcriber(whisper_cfg, "mic")
    try:
        with pytest.raises(TranscriptionError) as exc:
            await t.transcribe_utterance(utt(), None)  # type: ignore[attr-defined]
        assert exc.value.retryable
    finally:
        await t.aclose()


async def test_mic_and_system_share_one_server(whisper_cfg: Config) -> None:
    model = install_model(whisper_cfg, script={"replies": [reply("Oui.")]})
    mic, system = build_transcriber(whisper_cfg, "mic"), build_transcriber(whisper_cfg, "system")
    try:
        await asyncio.gather(mic.transcribe_utterance(utt(), None), system.transcribe_utterance(utt(), None))  # type: ignore[attr-defined]
        assert mic.server is system.server and mic.server.users == 2  # type: ignore[attr-defined]
        proc = mic.server.proc  # type: ignore[attr-defined]
        await mic.aclose()
        assert proc.returncode is None  # still used by the system pipeline
    finally:
        await system.aclose()
    await asyncio.wait_for(proc.wait(), 10)
    assert len(requests_seen(model)) == 2 and wc.WhisperServer._shared == {}


@pytest.mark.skipif(os.name != "posix", reason="leftover processes are found with ps")
async def test_a_server_left_over_by_a_crash_is_stopped(whisper_cfg: Config) -> None:
    model = install_model(whisper_cfg, script={"replies": [reply("Oui.")]})
    stray_model = install_model(whisper_cfg, "stray")
    stray = subprocess.Popen([sys.executable, str(FAKE), "-m", str(stray_model), "--port", "0", "-x", "whisper-server"])
    try:
        run = whisper_cfg.data_path / "run"
        run.mkdir(parents=True)
        (run / f"whisper-server-{model.stem}.pid").write_text(str(stray.pid))
        t = build_transcriber(whisper_cfg, "mic")
        try:
            await t.transcribe_utterance(utt(), None)  # type: ignore[attr-defined]
        finally:
            await t.aclose()
        assert stray.wait(10) is not None
    finally:
        stray.kill()


async def test_missing_server_or_model_says_how_to_fix_it(whisper_cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wc, "usable_cpus", lambda: 8)
    t = build_transcriber(whisper_cfg, "mic")
    with pytest.raises(TranscriptionError, match="voicestream models download large-v3-turbo-q5_0") as exc:
        t.server  # type: ignore[attr-defined]  # noqa: B018
    assert exc.value.fatal
    assert wc.check(whisper_cfg) == ("fail", "model large-v3-turbo-q5_0 not downloaded: `voicestream models download large-v3-turbo-q5_0`")
    install_model(whisper_cfg)
    status, detail = wc.check(whisper_cfg)
    assert status == "warn" and "voice detection off" in detail
    install_model(whisper_cfg, stt_models.VAD_MODEL)
    assert wc.check(whisper_cfg)[0] == "ok"

    whisper_cfg.transcription.whisper_cpp.binary = None
    monkeypatch.setattr(wc.shutil, "which", lambda name: None)
    monkeypatch.setattr(wc, "HOMEBREW_PATHS", ())
    monkeypatch.delenv(wc.BINARY_ENV, raising=False)
    with pytest.raises(TranscriptionError, match="brew install whisper-cpp") as exc:
        build_transcriber(whisper_cfg, "mic").server  # type: ignore[attr-defined]  # noqa: B018
    assert exc.value.fatal and wc.check(whisper_cfg)[0] == "fail"


async def test_full_pipeline_with_whisper_cpp(whisper_cfg: Config) -> None:
    install_model(whisper_cfg, script={"load_s": 0.5, "replies": [
        reply("Je vais envoyer le devis à Acme d'ici vendredi."),
        reply("Sous-titres réalisés par la communauté d'Amara.org", no_speech=0.3, logprob=-0.5),
        reply("Sam réserve la salle.", language="french"),
    ]})  # fmt: skip
    wav = write_wav(whisper_cfg.data_path / "meeting.wav", bursts(3))
    whisper_cfg.capture.sources = ["mic"]
    a = Assistant(whisper_cfg, source_specs=[f"file:{wav}"], llm=None, sinks=[], always_on=False, replay_speed=0)
    await a.run()
    lines = [s.text for s in a.finished_meetings[0].meeting.segments]
    assert lines == ["Je vais envoyer le devis à Acme d'ici vendredi.", "Sam réserve la salle."]  # the stock line is filtered
    assert wc.WhisperServer._shared == {}  # stopped with the pipeline


def test_doctor_reports_whisper_cpp(whisper_cfg: Config, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    install_model(whisper_cfg)
    install_model(whisper_cfg, stt_models.VAD_MODEL)
    cfgp = tmp_path / "voicestream.toml"
    cfgp.write_text(
        f'data_dir = "{whisper_cfg.data_path.as_posix()}"\n[transcription]\nbackend = "whisper-cpp"\n'
        f'[transcription.whisper_cpp]\nbinary = "{FAKE.as_posix()}"\n'
    )
    cli.main(["-c", str(cfgp), "doctor"])
    assert "✔ transcription: whisper.cpp on this computer (free) — large-v3-turbo-q5_0" in capsys.readouterr().out


# ------------------------------------------------------------------ model files


def _model_bytes(n: int = 300_000) -> bytes:
    return stt_models.GGML_MAGIC + bytes(range(256)) * (n // 256)


def _catalog(monkeypatch: pytest.MonkeyPatch, data: bytes, name: str = "base") -> None:
    info = stt_models.CATALOG[name]
    monkeypatch.setitem(stt_models.CATALOG, name, stt_models.ModelInfo(
        info.name, info.label, len(data), info.ram_gb, hashlib.sha1(data).hexdigest(), info.note))  # noqa: S324


def _server(data: bytes, *, ignore_range: bool = False, cut_at: int | None = None, fail_first: int = 0) -> tuple[httpx.AsyncClient, list[str]]:
    seen: list[str] = []
    state = {"fails": fail_first}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("range", ""))
        if state["fails"]:
            state["fails"] -= 1
            return httpx.Response(503)
        start = int(request.headers["range"].removeprefix("bytes=").rstrip("-")) if "range" in request.headers and not ignore_range else 0
        body = data[start:cut_at] if cut_at and start < cut_at else data[start:]
        headers = {"content-length": str(len(data) - start)}
        return httpx.Response(206 if start else 200, content=body, headers=headers)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen


async def test_download_checks_and_moves_the_model_into_place(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    data = _model_bytes()
    _catalog(monkeypatch, data)
    client, _ = _server(data)
    progress: list[tuple[int, int | None]] = []
    path = await stt_models.download(cfg, "base", client=client, on_progress=lambda d, t: progress.append((d, t)))
    assert path.read_bytes() == data and stt_models.installed(cfg) == ["base"]
    assert progress[-1] == (len(data), len(data))
    assert not path.with_name(path.name + ".part").exists()


async def test_download_resumes_after_a_cut(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    data = _model_bytes()
    _catalog(monkeypatch, data)
    monkeypatch.setattr(stt_models, "RETRY_DELAY_S", 0.0)
    client, seen = _server(data, cut_at=100_000)  # the first response stops a third of the way
    path = await stt_models.download(cfg, "base", client=client)
    assert path.read_bytes() == data and seen == ["", "bytes=100000-"]


async def test_download_starts_over_when_the_server_ignores_the_range(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    data = _model_bytes()
    _catalog(monkeypatch, data)
    part = stt_models.model_path(cfg, "base").with_name("ggml-base.bin.part")
    part.parent.mkdir(parents=True)
    part.write_bytes(b"lmgg-stale-bytes")
    client, seen = _server(data, ignore_range=True)
    path = await stt_models.download(cfg, "base", client=client)
    assert path.read_bytes() == data and seen == ["bytes=16-"]


async def test_download_rejects_a_damaged_or_wrong_file(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    data = _model_bytes()
    _catalog(monkeypatch, data)
    client, _ = _server(data[:-1] + b"X")
    with pytest.raises(stt_models.DownloadError, match="damaged"):
        await stt_models.download(cfg, "base", client=client)
    assert not stt_models.model_path(cfg, "base").exists()
    assert not stt_models.model_path(cfg, "base").with_name("ggml-base.bin.part").exists()  # no bad resume later
    client, _ = _server(b"<html>Access denied</html>" + b"x" * 1000)
    with pytest.raises(stt_models.DownloadError, match="not a whisper.cpp model"):
        await stt_models.download(cfg, "base", client=client)


async def test_download_retries_server_errors_but_not_missing_files(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    data = _model_bytes()
    _catalog(monkeypatch, data)
    monkeypatch.setattr(stt_models, "RETRY_DELAY_S", 0.0)
    client, seen = _server(data, fail_first=2)
    await stt_models.download(cfg, "base", client=client)
    assert len(seen) == 3
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    with pytest.raises(stt_models.DownloadError, match="HTTP 404"):
        await stt_models.download(cfg, "small", client=client)
    with pytest.raises(stt_models.DownloadError, match="unknown model"):
        await stt_models.download(cfg, "gigantic")


def test_models_command(cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    cfgp = tmp_path / "voicestream.toml"
    cfgp.write_text(f'data_dir = "{cfg.data_path.as_posix()}"\n')
    fetched: list[str] = []

    async def fake_download(c: Config, name: str, on_progress: Any = None, **kw: Any) -> Path:
        fetched.append(name)
        on_progress(50, 100)
        on_progress(100, 100)
        return install_model(c, name)

    monkeypatch.setattr(stt_models, "download", fake_download)
    assert cli.main(["-c", str(cfgp), "models", "download", "small"]) == 0
    assert fetched == ["small", stt_models.VAD_MODEL]  # voice detection comes along
    assert "(100%)" in capsys.readouterr().out
    assert cli.main(["-c", str(cfgp), "models"]) == 0
    out = capsys.readouterr().out
    assert " ✔ small " in out and "   large-v3-turbo-q5_0 " in out
    assert cli.main(["-c", str(cfgp), "models", "remove", "small"]) == 0
    assert not stt_models.is_installed(cfg, "small")
    assert cli.main(["-c", str(cfgp), "models", "download"]) == 2


# ------------------------------------------------------------------ the real whisper-server


def _real_binary() -> Path | None:
    found = wc.find_binary(Config().transcription.whisper_cpp)
    return found if found is not None and found.suffix != ".py" else None


REAL = _real_binary()
TEST_MODEL = os.environ.get("VOICESTREAM_WHISPER_TEST_MODEL")
GPU = os.environ.get("VOICESTREAM_WHISPER_TEST_GPU", "1") != "0"
"""``VOICESTREAM_WHISPER_TEST_GPU=0``: run the real server on the CPU (a CI runner's Metal is a paravirtual GPU)."""
TEST_VAD = os.environ.get("VOICESTREAM_WHISPER_TEST_VAD")  # ggml-silero-v6.2.0.bin: transcribe with voice detection on
REQUIRED = os.environ.get("VOICESTREAM_WHISPER_TEST_REQUIRED") == "1"
"""Set by the macOS CI job: fail, instead of skipping, when the real-server tests can't run."""
needs_real = pytest.mark.skipif(REAL is None and not REQUIRED, reason="no whisper-server binary on this machine")


def _skip_unless(ok: object, reason: str) -> None:
    if not ok:
        (pytest.fail if REQUIRED else pytest.skip)(reason)


def _empty_test_model() -> Path | None:
    for base in (os.environ.get("VOICESTREAM_WHISPER_TEST_DIR"), "/opt/homebrew/share/whisper-cpp", "/usr/local/share/whisper-cpp"):
        if base and (Path(base) / "for-tests-ggml-tiny.bin").is_file():
            return Path(base) / "for-tests-ggml-tiny.bin"
    return None


@needs_real
async def test_real_whisper_server_protocol(cfg: Config) -> None:
    """Our flags, routes and fields against the real binary (an empty test model returns no text)."""
    _skip_unless(REAL, "no whisper-server binary (set VOICESTREAM_WHISPER_SERVER)")
    model = Path(TEST_MODEL) if TEST_MODEL else _empty_test_model()
    _skip_unless(model, "no whisper.cpp model to load (set VOICESTREAM_WHISPER_TEST_MODEL or VOICESTREAM_WHISPER_TEST_DIR)")
    assert model is not None
    cfg.transcription.backend = "whisper-cpp"
    cfg.transcription.whisper_cpp.binary = str(REAL)
    cfg.transcription.whisper_cpp.model = str(model)
    cfg.transcription.whisper_cpp.gpu = GPU
    silero = model.with_name("for-tests-silero-v6.2.0-ggml.bin")  # real Silero weights, shipped with the tests
    if silero.is_file():
        vad = stt_models.model_path(cfg, stt_models.VAD_MODEL)
        vad.parent.mkdir(parents=True, exist_ok=True)
        vad.write_bytes(silero.read_bytes())
    t = build_transcriber(cfg, "mic")
    try:
        res = await t.transcribe_utterance(utt(3.0), None)  # type: ignore[attr-defined]
        server = t.server  # type: ignore[attr-defined]
        assert (server.vad_model is not None) == (silero.is_file() and wc.vad_fits())
        async with httpx.AsyncClient() as client:  # nothing answers outside the secret prefix
            assert (await client.get(f"http://127.0.0.1:{server.port}/health")).status_code == 404
            assert (await client.get(f"{server.base_url}/health")).status_code == 200
    finally:
        await t.aclose()
    assert isinstance(res.text, str)


SPEECH = os.environ.get("VOICESTREAM_WHISPER_TEST_SPEECH")  # dir with en.wav/en.txt, fr.wav/fr.txt


@pytest.mark.skipif(not (REAL and TEST_MODEL and SPEECH) and not REQUIRED, reason="needs a real model and recorded speech")
@pytest.mark.parametrize("lang", ["en", "fr"])
async def test_real_whisper_server_transcribes_speech(cfg: Config, lang: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _skip_unless(REAL and TEST_MODEL and SPEECH, "needs VOICESTREAM_WHISPER_SERVER, _TEST_MODEL and _TEST_SPEECH")
    cfg.transcription.backend = "whisper-cpp"
    cfg.transcription.whisper_cpp.binary = str(REAL)
    cfg.transcription.whisper_cpp.model = str(TEST_MODEL)
    cfg.transcription.whisper_cpp.allowed_languages = ["fr", "en"]
    cfg.transcription.whisper_cpp.gpu = GPU
    if TEST_VAD:  # as the app runs it on a Mac (more than 4 cores): Silero voice detection before each utterance
        monkeypatch.setattr(wc, "usable_cpus", lambda: 8)  # also on a 3-core runner
        vad = stt_models.model_path(cfg, stt_models.VAD_MODEL)
        vad.parent.mkdir(parents=True, exist_ok=True)
        vad.write_bytes(Path(TEST_VAD).read_bytes())
    wav, expected = Path(SPEECH) / f"{lang}.wav", (Path(SPEECH) / f"{lang}.txt").read_text(encoding="utf-8")
    with wave.open(str(wav)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (SAMPLE_RATE, 1, 2)
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    audio_s = len(pcm) / SAMPLE_RATE
    speech = Utterance(source="mic", samples=pcm, t_start=0, t_end=audio_s, speech_seconds=audio_s)
    t = build_transcriber(cfg, "mic")
    server = t.server  # type: ignore[attr-defined]
    timings: list[tuple[str, float, str]] = []

    async def timed(phase: str, step: Any) -> Any:
        started = time.monotonic()
        result = await step
        timings.append((phase, time.monotonic() - started, f" · {result.text!r}" if result is not None else ""))
        return result

    try:
        # where the time goes on a slow machine: loading the model, the first request, a warm one, and what
        # auto-detecting the language costs (an extra pass over the audio) next to a fixed language
        await timed("server start until ready", server.ensure_started())
        res = await timed("first utterance (auto language)", t.transcribe_utterance(speech, None))  # type: ignore[attr-defined]
        await timed("same utterance again (warm)", t.transcribe_utterance(speech, None))  # type: ignore[attr-defined]
        t.language = lang  # type: ignore[attr-defined]
        await timed(f"same utterance, language fixed to {lang}", t.transcribe_utterance(speech, None))  # type: ignore[attr-defined]
        used_vad = server.vad_model is not None
        if used_vad:  # what whisper.cpp's own voice detection costs (Silero, always on 4 CPU threads)
            await timed(f"same, language fixed to {lang}, voice detection off", _without_vad(t, speech, lang))
    finally:
        await t.aclose()  # whisper-server prints its timings when it shuts down
    words = lambda s: {w.strip(".,!?;:'’").lower() for w in s.split() if len(w) > 3}  # noqa: E731
    overlap = len(words(res.text) & words(expected)) / max(1, len(words(expected)))
    threads = cfg.transcription.whisper_cpp.threads or wc.default_threads()
    print(f"\n[{lang}] {'GPU' if GPU else 'CPU'}{' + VAD' if used_vad else ''} · {threads} threads · "
          f"{audio_s:.1f}s of audio · {overlap:.0%} words")
    for phase, took, text in timings:
        print(f"[{lang}] {phase}: {took:.1f}s{text}")
    for line in _server_timings(server):
        print(f"[{lang}] whisper-server: {line}")
    assert used_vad == bool(TEST_VAD)
    assert res.language == lang and overlap >= 0.6, res.text


async def _without_vad(t: Any, speech: Utterance, lang: str) -> Any:
    fields = {**t._fields(lang, None), "vad": "false"}  # the server's flags are copied for each request
    data = await t.server.inference(wc.wav_bytes(speech.samples), fields)
    return wc.TranscribeResult(wc.parse_reply(data, t.wc.no_speech_threshold)[0])


def _server_timings(server: wc.WhisperServer) -> list[str]:
    """The last ``whisper_print_timings`` block of the server's log (load, mel, encode, decode, total…),
    all requests together, and the voice detection's own running total."""
    try:
        text = server._log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ["(no log)"]
    lines = [ln.split("whisper_print_timings:", 1)[1].strip() for ln in text.splitlines() if "whisper_print_timings:" in ln]
    vad = [ln.split(":", 1)[1].strip() for ln in text.splitlines() if ": vad time =" in ln]  # …_detect_speech_no_reset: vad time
    return lines[-12:] + [f"voice detection, {len(vad)} requests: {v}" for v in vad[-1:]] or \
        ["(no whisper_print_timings lines in its log)"]


def test_silence_reaches_no_one(whisper_cfg: Config) -> None:
    """The energy VAD keeps pure noise from ever being sent to whisper."""
    install_model(whisper_cfg, script={"replies": [reply("Merci d'avoir regardé cette vidéo.")]})

    async def go() -> list[str]:
        t = build_transcriber(whisper_cfg, "mic")

        async def frames() -> AsyncIterator[Any]:
            async for f in WavFileSource(write_wav(whisper_cfg.data_path / "quiet.wav", noise(8.0)), speed=0).frames():
                yield f

        try:
            return [s.text async for s in t.stream("mic", "Me", frames())]
        finally:
            await t.aclose()

    assert asyncio.run(go()) == []
