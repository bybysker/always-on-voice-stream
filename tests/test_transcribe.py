"""Transcription backends: the shared chunked pipeline (ordering, retries, hallucination
filter, billing), the OpenAI REST request shape and the OpenAI Realtime protocol (against a
local websocket server), all offline."""

from __future__ import annotations

import asyncio
import base64
import email.policy
import http
import io
import json
import wave
from collections.abc import AsyncIterator, Awaitable, Callable
from email.parser import BytesParser
from typing import Any

import httpx
import numpy as np
import pytest
import websockets

from helpers import bursts, noise, speechlike
from voicestream.audio.base import float_to_int16
from voicestream.config import Config, DeepgramConfig, OpenAIRealtimeConfig, OpenAITranscribeConfig
from voicestream.models import SAMPLE_RATE, AudioFrame, Utterance
from voicestream.transcribe import build_transcriber, keywords_for
from voicestream.transcribe.base import (
    ChunkTranscriber,
    FakeTranscriber,
    TranscribeResult,
    TranscriptionError,
    clean_transcript,
    is_hallucination,
)
from voicestream.transcribe.deepgram import DeepgramTranscriber
from voicestream.transcribe.openai_realtime import RATE, OpenAIRealtimeTranscriber
from voicestream.transcribe.openai_rest import OpenAITranscriber

PRE_ROLL_S = 0.3  # VADConfig default


async def _frames(sig: np.ndarray, name: str = "mic") -> AsyncIterator[AudioFrame]:
    pcm = float_to_int16(sig) if sig.dtype != np.int16 else sig
    for k in range(0, len(pcm), 1600):
        yield AudioFrame(name, pcm[k : k + 1600], k / SAMPLE_RATE)
        await asyncio.sleep(0)


def _onset(k: int) -> float:
    """Start of speech burst ``k`` in ``helpers.bursts()`` (1 s lead-in, 2.5 s speech + 1.5 s gap)."""
    return 1.0 + 4.0 * k


def _utt(seconds: float = 1.0, speech_seconds: float | None = None) -> Utterance:
    samples = float_to_int16(speechlike(seconds))
    return Utterance("mic", samples, 0.0, seconds, speech_seconds=seconds if speech_seconds is None else speech_seconds)


@pytest.fixture
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Retry back-off without the wait: records non-zero sleeps, still yields to the loop."""
    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def fast_sleep(delay: float, result: Any = None) -> Any:
        if delay:
            delays.append(delay)
        return await real_sleep(0, result)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    return delays


class _Scripted(ChunkTranscriber):
    """Replies (or raises) from ``outcomes`` in call order; records the prompts it was given."""

    backend = "scripted"

    def __init__(self, outcomes: list[str | Exception], concurrency: int = 1, **kw: Any) -> None:
        super().__init__("scripted-model", **kw)
        self.concurrency = concurrency
        self.outcomes = list(outcomes)
        self.prompts: list[str | None] = []

    async def transcribe_utterance(self, utt: Utterance, prompt: str | None) -> TranscribeResult:
        self.prompts.append(prompt)
        nxt = self.outcomes.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return TranscribeResult(nxt, confidence=0.5)


# ======================================================================= base pipeline


async def test_stream_yields_one_segment_per_utterance_in_order() -> None:
    usage: list[tuple[str, float]] = []
    t = FakeTranscriber(["one", "two", "three"], on_usage=lambda model, s: usage.append((model, s)))
    segs = [s async for s in t.stream("system", "Others", _frames(bursts(3), "system"))]
    assert [s.text for s in segs] == ["one", "two", "three"]
    assert all(s.source == "system" and s.speaker == "Others" and s.backend == "fake:fake" for s in segs)
    for k, s in enumerate(segs):
        assert s.t_start == pytest.approx(_onset(k) - PRE_ROLL_S, abs=0.05)
        assert s.t_end == pytest.approx(_onset(k) + 2.5 + 0.2, abs=0.1)  # trailing silence trimmed to 0.2 s
        assert t.calls[k].t_start == s.t_start and t.calls[k].t_end == s.t_end
    # every utterance is billed by its audio length, and only speech is sent
    assert [m for m, _ in usage] == ["fake"] * 3
    assert [sec for _, sec in usage] == pytest.approx([u.duration for u in t.calls])
    assert t.billed_seconds == pytest.approx(sum(u.duration for u in t.calls))
    assert t.billed_seconds < len(bursts(3)) / SAMPLE_RATE * 0.8


async def test_stream_of_silence_yields_nothing_and_bills_nothing() -> None:
    t = FakeTranscriber(["never"])
    assert [s async for s in t.stream("mic", "Me", _frames(noise(5.0)))] == []
    assert t.calls == [] and t.billed_seconds == 0.0


async def test_concurrent_requests_still_yield_in_utterance_order() -> None:
    started: list[int] = []
    third_started = asyncio.Event()
    in_flight = peak = 0

    class Slow(ChunkTranscriber):
        backend = "slow"
        concurrency = 3

        async def transcribe_utterance(self, utt: Utterance, prompt: str | None) -> TranscribeResult:
            nonlocal in_flight, peak
            n = len(started) + 1
            started.append(n)
            in_flight += 1
            peak = max(peak, in_flight)
            if n == 3:
                third_started.set()
            if n < 3:
                await third_started.wait()  # the third reply arrives first
            in_flight -= 1
            return TranscribeResult(f"utterance {n}")

    t = Slow("slow-model")
    segs = [s async for s in t.stream("mic", "Me", _frames(bursts(3)))]
    assert peak == 3
    assert [s.text for s in segs] == ["utterance 1", "utterance 2", "utterance 3"]
    assert [s.t_start for s in segs] == sorted(s.t_start for s in segs)


async def test_retryable_error_is_retried_with_backoff(slept: list[float]) -> None:
    t = _Scripted([TranscriptionError("429", retryable=True), TranscriptionError("503", retryable=True), "hello there"])
    segs = [s async for s in t.stream("mic", "Me", _frames(bursts(1)))]
    assert [s.text for s in segs] == ["hello there"]
    assert segs[0].confidence == 0.5
    assert slept == [1.0, 2.0]


async def test_retries_are_capped_then_the_utterance_is_skipped(slept: list[float]) -> None:
    t = _Scripted([TranscriptionError("busy", retryable=True)] * 4 + ["second"])
    segs = [s async for s in t.stream("mic", "Me", _frames(bursts(2)))]
    assert [s.text for s in segs] == ["second"]
    assert len(t.prompts) == ChunkTranscriber.max_retries + 2
    assert slept == [1.0, 2.0, 4.0]


async def test_network_errors_and_timeouts_are_retried(slept: list[float]) -> None:
    t = _Scripted([OSError("reset"), TimeoutError(), "made it"])
    segs = [s async for s in t.stream("mic", "Me", _frames(bursts(1)))]
    assert [s.text for s in segs] == ["made it"]


async def test_non_retryable_error_skips_only_that_utterance(slept: list[float]) -> None:
    t = _Scripted([TranscriptionError("HTTP 400: bad audio"), "two", "three"])
    segs = [s async for s in t.stream("mic", "Me", _frames(bursts(3)))]
    assert [s.text for s in segs] == ["two", "three"]
    assert segs[0].t_start == pytest.approx(_onset(1) - PRE_ROLL_S, abs=0.05)
    assert slept == []


async def test_failed_utterances_are_not_billed(slept: list[float]) -> None:
    usage: list[float] = []
    t = _Scripted([TranscriptionError("HTTP 400"), "two"], on_usage=lambda m, s: usage.append(s))
    segs = [s async for s in t.stream("mic", "Me", _frames(bursts(2)))]
    assert len(segs) == 1
    assert usage == [pytest.approx(segs[0].t_end - segs[0].t_start)]


async def test_unexpected_error_skips_only_that_utterance(slept: list[float]) -> None:
    usage: list[float] = []
    t = _Scripted([KeyError("text"), "two"], on_usage=lambda m, s: usage.append(s))
    segs = [s async for s in t.stream("mic", "Me", _frames(bursts(2)))]
    assert [s.text for s in segs] == ["two"]
    assert len(t.prompts) == 2 and slept == []  # not retried: a bug won't fix itself
    assert usage == [pytest.approx(segs[0].t_end - segs[0].t_start)]


async def test_missing_package_stops_the_stream() -> None:
    t = _Scripted([ModuleNotFoundError("No module named 'faster_whisper'"), "never"])
    with pytest.raises(ImportError):
        _ = [s async for s in t.stream("mic", "Me", _frames(bursts(2)))]
    assert len(t.prompts) == 1


async def test_fatal_error_stops_the_stream() -> None:
    t = _Scripted([TranscriptionError("bad key", fatal=True), "never"])
    with pytest.raises(TranscriptionError) as err:
        _ = [s async for s in t.stream("mic", "Me", _frames(bursts(2)))]
    assert err.value.fatal
    assert len(t.prompts) == 1  # not retried


async def test_hallucinations_are_dropped_but_real_speech_is_kept() -> None:
    long_burst = np.concatenate([noise(1.0), speechlike(2.5, seed=3), noise(1.5)])
    short_burst = np.concatenate([noise(1.0), speechlike(0.9, seed=4), noise(3.0)])

    async def run(texts: list[str], sig: np.ndarray) -> list[str]:
        t = FakeTranscriber(texts)
        return [s.text async for s in t.stream("mic", "Me", _frames(sig))]

    assert await run(["Thank you."], short_burst) == []  # Whisper's stock phrase for a blip of noise
    assert await run(["Thank you."], long_burst) == ["Thank you."]  # two seconds of speech: really said
    assert await run(["Subtitles by the Amara.org community"], long_burst) == []
    assert await run(["the " * 14], long_burst) == []
    assert await run(["  so we \n ship   it on Monday  "], long_burst) == ["so we ship it on Monday"]
    assert await run(["   "], long_burst) == []


@pytest.mark.parametrize(
    ("text", "speech_s", "expected"),
    [
        ("Thank you.", 0.8, True),
        ("Thank you.", 2.0, False),
        ("you", 1.0, True),
        ("Subtitles by the Amara.org community", 5.0, True),
        ("Captions from Rev", 5.0, True),
        ("[Music]", 5.0, True),
        ("(applause)", 5.0, True),
        ("no no no no no no no no no no no no", 5.0, True),
        ("we we need need to to ship ship the the beta beta", 5.0, False),  # 6 distinct words
        ("Let's ship the beta on Monday and send the notes to Sarah after", 5.0, False),
        ("I subscribe to the subtitles idea", 5.0, False),
        ("❤️ par SousTitreur.com", 5.0, True),  # Whisper's stock French inventions (openai/whisper#928)
        ("— Sous-titrage ST'501 —", 5.0, True),
        ("Sous-titres réalisés par la communauté d'Amara.org", 5.0, True),
        ("On revoit les sous-titres de la vidéo demain", 5.0, False),
    ],
)
def test_is_hallucination(text: str, speech_s: float, expected: bool) -> None:
    assert is_hallucination(clean_transcript(text), speech_s) is expected


async def test_prompt_carries_the_base_prompt_and_previous_text() -> None:
    t = _Scripted(["First line.", "Second line.", "Third."], prompt="Weekly sync with Sarah.")
    _ = [s async for s in t.stream("mic", "Me", _frames(bursts(3)))]
    assert t.prompts[0] == "Weekly sync with Sarah."
    assert t.prompts[1] == "Weekly sync with Sarah. First line."
    assert t.prompts[2] == "Weekly sync with Sarah. First line. Second line."


def test_build_prompt_keeps_only_recent_context() -> None:
    t = _Scripted([])
    assert t.build_prompt("") is None
    prompt = t.build_prompt("x" * 500 + " the most recent words")
    assert prompt is not None and len(prompt) == 300 and prompt.endswith("the most recent words")


async def test_levels_are_reported_with_the_source_name() -> None:
    t = FakeTranscriber(["hi"])
    levels: list[tuple[str, float, bool]] = []
    t.on_level = lambda src, db, speech: levels.append((src, db, speech))
    _ = [s async for s in t.stream("system", "Others", _frames(bursts(1), "system"))]
    assert levels and {src for src, _, _ in levels} == {"system"}
    assert any(speech for *_, speech in levels) and not all(speech for *_, speech in levels)


class _SlowFake(FakeTranscriber):
    """Each utterance takes 0.15 s to transcribe, much longer than the stream takes to deliver them."""

    backlog_after_s, backlog_max_queued, backlog_poll_s = 0.3, 3, 0.02

    async def transcribe_utterance(self, utt: Utterance, prompt: str | None) -> TranscribeResult:
        await asyncio.sleep(0.15)
        return await super().transcribe_utterance(utt, prompt)


async def test_falling_behind_is_said_once_and_so_is_catching_up() -> None:
    """A model too slow for the computer (large-v3 on a CI runner's CPU: ~70 s for an 8 s sentence) falls
    further behind with every sentence, silently: the transcript and the to-dos come minutes late."""
    t = _SlowFake([f"line {i}" for i in range(6)])
    seen: list[tuple[str, float, int, str]] = []
    t.on_backlog = lambda *report: seen.append(report)
    texts = [s.text async for s in t.stream("mic", "Me", _frames(bursts(6)))]
    assert texts == [f"line {i}" for i in range(6)]  # nothing lost, nothing out of order
    (source, behind_s, queued, backend), cleared = seen  # one warning per episode, not one per poll
    assert (source, backend) == ("mic", "fake") and (queued > 3 or behind_s >= 0.3) and behind_s > 0
    assert cleared == ("mic", 0.0, 0, "fake")


async def test_keeping_up_reports_nothing() -> None:
    t = FakeTranscriber(["one", "two", "three"])
    t.backlog_poll_s = 0.01
    seen: list[Any] = []
    t.on_backlog = lambda *report: seen.append(report)
    assert len([s async for s in t.stream("mic", "Me", _frames(bursts(3)))]) == 3
    assert seen == []


async def test_capture_errors_surface_after_pending_segments() -> None:
    async def broken() -> AsyncIterator[AudioFrame]:
        async for f in _frames(bursts(1)):
            yield f
        raise OSError("device unplugged")

    t = FakeTranscriber(["kept"])
    got: list[str] = []
    with pytest.raises(OSError, match="unplugged"):
        async for s in t.stream("mic", "Me", broken()):
            got.append(s.text)
    assert got == ["kept"]


# ------------------------------------------------------------------- backend factory


def test_keywords_merge_vocabulary_attendees_and_user(cfg: Config) -> None:
    cfg.brain.vocabulary = ["Notion", " Acme ", ""]
    cfg.brain.attendees = ["Sarah", "Notion", "me"]
    assert keywords_for(cfg) == ["Notion", "Acme", "Sarah", "Alex"]


def test_realtime_backend_turns_noise_reduction_off_for_system_audio(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    cfg.transcription.backend = "openai-realtime"
    mic = build_transcriber(cfg, "mic")
    system = build_transcriber(cfg, "system")
    assert isinstance(mic, OpenAIRealtimeTranscriber) and isinstance(system, OpenAIRealtimeTranscriber)
    assert mic.cfg.noise_reduction == "near_field"
    assert system.cfg.noise_reduction == "none"
    assert cfg.transcription.openai_realtime.noise_reduction == "near_field"  # config not mutated


def test_unknown_backend_is_an_error(cfg: Config) -> None:
    with pytest.raises(TranscriptionError):
        build_transcriber(cfg, "mic", backend="nope")


# ========================================================================= OpenAI REST


Reply = dict[str, Any] | httpx.Response | Exception


class _Api:
    def __init__(self, *replies: Reply) -> None:
        self.replies = list(replies)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, httpx.Response) else httpx.Response(200, json=reply)

    def form(self, i: int = -1) -> list[tuple[str, bytes, str | None]]:
        """(field name, raw value, filename) for every multipart part, in order."""
        req = self.requests[i]
        head = f"Content-Type: {req.headers['content-type']}\r\n\r\n".encode()
        msg = BytesParser(policy=email.policy.HTTP).parsebytes(head + req.content)
        return [
            (p.get_param("name", header="content-disposition"), p.get_payload(decode=True), p.get_filename())
            for p in msg.iter_parts()
        ]

    def fields(self, i: int = -1) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for name, value, filename in self.form(i):
            if filename is None:
                out.setdefault(name, []).append(value.decode())
        return out


def _rest(api: _Api, model: str = "gpt-transcribe", **kw: Any) -> OpenAITranscriber:
    return OpenAITranscriber(
        OpenAITranscribeConfig(model=model, base_url="https://stt.test/v1/"),
        api_key="sk-test",
        client=httpx.AsyncClient(transport=httpx.MockTransport(api)),
        **kw,
    )


async def test_rest_request_shape_for_gpt_transcribe() -> None:
    api = _Api({"text": "Hello Sarah", "languages": [{"code": "en", "probability": 0.98}]})
    t = _rest(api, language="en", keywords=["Notion", "Sarah", " "])
    utt = _utt(1.0)
    res = await t.transcribe_utterance(utt, "Weekly sync.")
    assert (res.text, res.language) == ("Hello Sarah", "en")
    req = api.requests[0]
    assert req.method == "POST" and str(req.url) == "https://stt.test/v1/audio/transcriptions"
    assert req.headers["authorization"] == "Bearer sk-test"
    assert api.fields() == {
        "model": ["gpt-transcribe"],
        "response_format": ["json"],
        "languages[]": ["en"],
        "keywords[]": ["Notion", "Sarah"],
        "prompt": ["Weekly sync."],
    }
    ((name, wav, filename),) = [p for p in api.form() if p[2] is not None]
    assert (name, filename) == ("file", "utterance.wav")
    with wave.open(io.BytesIO(wav)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()) == (SAMPLE_RATE, 1, 2, len(utt.samples))


async def test_rest_older_model_uses_language_and_folds_vocabulary_into_prompt() -> None:
    api = _Api({"text": "ok", "language": "english"})
    t = _rest(api, model="gpt-4o-mini-transcribe", language="en", keywords=["Notion", "Acme"])
    prompt = t.build_prompt("we talked about pricing")
    res = await t.transcribe_utterance(_utt(), prompt)
    fields = api.fields()
    assert fields["language"] == ["en"]
    assert "languages[]" not in fields and "keywords[]" not in fields
    assert fields["prompt"] == ["Vocabulary: Notion, Acme. we talked about pricing"]
    assert res.language == "english"


async def test_rest_prompt_is_capped_and_omitted_when_empty() -> None:
    api = _Api({"text": "a"}, {"text": "b"})
    t = _rest(api)
    await t.transcribe_utterance(_utt(), "x" * 1500 + "END")
    assert len(api.fields(0)["prompt"][0]) == 1000 and api.fields(0)["prompt"][0].endswith("END")
    await t.transcribe_utterance(_utt(), None)
    assert "prompt" not in api.fields(1) and "languages[]" not in api.fields(1) and "keywords[]" not in api.fields(1)


async def test_rest_keyword_lists_are_capped() -> None:
    api = _Api({"text": "a"}, {"text": "b"})
    words = [f"term{i}" for i in range(150)]
    await _rest(api, keywords=words).transcribe_utterance(_utt(), None)
    assert api.fields()["keywords[]"] == words[:100]
    old_style = _rest(api, model="gpt-4o-mini-transcribe", keywords=words)
    await old_style.transcribe_utterance(_utt(), old_style.build_prompt(""))
    assert api.fields()["prompt"] == ["Vocabulary: " + ", ".join(words[:50]) + "."]


async def test_rest_missing_text_is_empty() -> None:
    res = await _rest(_Api({"usage": {"seconds": 1}})).transcribe_utterance(_utt(), None)
    assert res.text == "" and res.language is None


@pytest.mark.parametrize("status", [408, 409, 429, 500, 502, 503])
async def test_rest_transient_http_errors_are_retryable(status: int) -> None:
    t = _rest(_Api(httpx.Response(status, text="slow down")))
    with pytest.raises(TranscriptionError) as err:
        await t.transcribe_utterance(_utt(), None)
    assert err.value.retryable and not err.value.fatal
    assert str(status) in str(err.value) and "slow down" in str(err.value)


@pytest.mark.parametrize("status", [401, 403])
async def test_rest_rejected_key_is_fatal_not_retryable(status: int) -> None:
    t = _rest(_Api(httpx.Response(status, json={"error": {"message": "Incorrect API key"}})))
    with pytest.raises(TranscriptionError) as err:
        await t.transcribe_utterance(_utt(), None)
    assert err.value.fatal and not err.value.retryable


async def test_rest_bad_request_is_neither_retryable_nor_fatal() -> None:
    t = _rest(_Api(httpx.Response(400, json={"error": {"message": "audio too short"}})))
    with pytest.raises(TranscriptionError) as err:
        await t.transcribe_utterance(_utt(), None)
    assert not err.value.retryable and not err.value.fatal


@pytest.mark.parametrize("exc", [httpx.ReadTimeout("slow"), httpx.ConnectError("refused")])
async def test_rest_network_failures_are_retryable(exc: Exception) -> None:
    with pytest.raises(TranscriptionError) as err:
        await _rest(_Api(exc)).transcribe_utterance(_utt(), None)
    assert err.value.retryable


async def test_rest_stream_retries_a_429_then_delivers(slept: list[float]) -> None:
    api = _Api(httpx.Response(429, text="rate limited"), {"text": "Ship it Friday."})
    segs = [s async for s in _rest(api).stream("mic", "Me", _frames(bursts(1)))]
    assert [s.text for s in segs] == ["Ship it Friday."]
    assert segs[0].backend == "openai:gpt-transcribe"
    assert len(api.requests) == 2 and slept == [1.0]


async def test_rest_stream_stops_on_a_rejected_key() -> None:
    api = _Api(httpx.Response(401, text="invalid key"))
    with pytest.raises(TranscriptionError) as err:
        _ = [s async for s in _rest(api).stream("mic", "Me", _frames(bursts(2)))]
    assert err.value.fatal and len(api.requests) == 1


async def test_rest_non_json_reply_does_not_end_the_stream(slept: list[float]) -> None:
    api = _Api(httpx.Response(200, text="<html>Sign in to the Wi-Fi</html>"), {"text": "Recovered."}, {"text": "Recovered."})
    segs = [s async for s in _rest(api).stream("mic", "Me", _frames(bursts(2)))]
    assert segs and segs[-1].text == "Recovered."
    assert segs[-1].t_start == pytest.approx(_onset(1) - PRE_ROLL_S, abs=0.05)


@pytest.mark.parametrize("body", [{"text": "<html>Sign in to the Wi-Fi</html>"}, {"json": ["not", "an", "object"]}])
async def test_rest_200_without_a_json_transcript_is_retryable(body: dict[str, Any]) -> None:
    with pytest.raises(TranscriptionError) as err:
        await _rest(_Api(httpx.Response(200, **body))).transcribe_utterance(_utt(), None)
    assert err.value.retryable and not err.value.fatal


async def test_rest_bills_only_requests_that_returned_a_transcript(slept: list[float]) -> None:
    usage: list[tuple[str, float]] = []
    api = _Api(httpx.Response(400, json={"error": {"message": "audio too short"}}), httpx.Response(503, text="busy"), {"text": "Second."})
    segs = [s async for s in _rest(api, on_usage=lambda m, s: usage.append((m, s))).stream("mic", "Me", _frames(bursts(2)))]
    assert [s.text for s in segs] == ["Second."] and len(api.requests) == 3
    # OpenAI doesn't bill failed requests: not the rejected utterance, and the retried one only once
    assert usage == [("gpt-transcribe", pytest.approx(segs[0].t_end - segs[0].t_start))]


def test_rest_requires_an_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(TranscriptionError, match="OPENAI_API_KEY"):
        OpenAITranscriber()


async def test_rest_closes_only_its_own_client() -> None:
    shared = httpx.AsyncClient(transport=httpx.MockTransport(_Api()))
    await OpenAITranscriber(api_key="k", client=shared).aclose()
    assert not shared.is_closed
    own = OpenAITranscriber(api_key="k")
    await own.aclose()
    assert own.client.is_closed


# ===================================================================== OpenAI Realtime


Handler = Callable[[Any, "_Conn"], Awaitable[None]]


class _Conn:
    """What one websocket connection received."""

    def __init__(self, n: int, path: str, headers: Any) -> None:
        self.n = n
        self.path = path
        self.headers = headers
        self.events: list[dict[str, Any]] = []
        self.commits = 0
        self.committed_audio: list[int] = []  # bytes appended before each commit
        self._audio = 0

    @property
    def types(self) -> list[str]:
        return [e["type"] for e in self.events]

    def item(self, k: int | None = None) -> str:
        return f"item_c{self.n}_{self.commits if k is None else k}"


class _RealtimeMock:
    """Minimal transcription-session server; ``on_commit`` decides how each commit is answered."""

    def __init__(self, on_commit: Handler | None = None) -> None:
        self.conns: list[_Conn] = []
        self.on_commit = on_commit or self.answer

    @staticmethod
    async def answer(ws: Any, conn: _Conn) -> None:
        iid = conn.item()
        await ws.send(json.dumps({"type": "input_audio_buffer.committed", "item_id": iid, "previous_item_id": None}))
        await ws.send(json.dumps({"type": "conversation.item.input_audio_transcription.delta", "item_id": iid, "delta": "utterance "}))
        await ws.send(json.dumps({"type": "conversation.item.input_audio_transcription.delta", "item_id": iid, "delta": str(conn.commits)}))
        await ws.send(json.dumps({"type": "conversation.item.input_audio_transcription.completed", "item_id": iid,
                                  "transcript": f"utterance {conn.commits} (conn {conn.n})"}))

    async def handler(self, ws: Any) -> None:
        conn = _Conn(len(self.conns) + 1, ws.request.path, ws.request.headers)
        self.conns.append(conn)
        async for raw in ws:
            ev = json.loads(raw)
            conn.events.append(ev)
            if ev["type"] == "input_audio_buffer.append":
                conn._audio += len(base64.b64decode(ev["audio"]))
            elif ev["type"] == "input_audio_buffer.clear":
                conn._audio = 0
            elif ev["type"] == "input_audio_buffer.commit":
                conn.commits += 1
                conn.committed_audio.append(conn._audio)
                conn._audio = 0
                await self.on_commit(ws, conn)

    @property
    def commits(self) -> int:
        return sum(c.commits for c in self.conns)


async def _realtime(
    mock: _RealtimeMock,
    sig: np.ndarray | None = None,
    *,
    source: str = "mic",
    speaker: str = "Me",
    cfg: dict[str, Any] | None = None,
    process_request: Any = None,
    hold: dict[int, int] | None = None,
    **kw: Any,
) -> tuple[OpenAIRealtimeTranscriber, list[Any]]:
    """``hold`` maps burst ``k`` to how many transcripts must be out before it starts, which pins
    where results land relative to utterances without relying on timing."""
    segs: list[Any] = []

    async def frames() -> AsyncIterator[AudioFrame]:
        async for f in _frames(bursts(3) if sig is None else sig, source):
            for k, n in (hold or {}).items():
                if abs(f.t_start - (_onset(k) - 0.5)) < 0.01:  # burst k-1 is committed, burst k not started
                    async with asyncio.timeout(5):
                        while len(segs) < n:
                            await asyncio.sleep(0.01)
            yield f

    async with websockets.serve(mock.handler, "127.0.0.1", 0, process_request=process_request) as srv:
        port = srv.sockets[0].getsockname()[1]
        t = OpenAIRealtimeTranscriber(
            OpenAIRealtimeConfig(url=f"ws://127.0.0.1:{port}/v1/realtime?intent=transcription", **(cfg or {})),
            api_key="sk-test",
            **kw,
        )
        async for s in t.stream(source, speaker, frames()):
            segs.append(s)
    return t, segs


async def test_realtime_session_audio_and_results() -> None:
    mock = _RealtimeMock()
    partials: list[tuple[str, str, str]] = []
    usage: list[tuple[str, float]] = []
    t, segs = await _realtime(
        mock, language="en", keywords=["Notion", "Sarah"], cfg={"prompt": "Product sync"},
        on_partial=lambda *p: partials.append(p), on_usage=lambda m, s: usage.append((m, s)),
    )
    assert [s.text for s in segs] == [f"utterance {k} (conn 1)" for k in (1, 2, 3)]
    assert all(s.source == "mic" and s.speaker == "Me" and s.backend == "openai-realtime:gpt-transcribe" for s in segs)
    for k, s in enumerate(segs):
        assert s.t_start == pytest.approx(_onset(k) - PRE_ROLL_S, abs=0.05)
        assert s.t_end == pytest.approx(_onset(k) + 2.5 + 0.7, abs=0.1)  # speech end + hangover

    (conn,) = mock.conns  # one socket for the whole stream
    assert conn.path == "/v1/realtime?intent=transcription"
    assert conn.headers["Authorization"] == "Bearer sk-test"
    assert "OpenAI-Beta" not in conn.headers
    assert conn.events[0] == {
        "type": "session.update",
        "session": {
            "type": "transcription",
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "transcription": {"model": "gpt-transcribe", "languages": ["en"], "keywords": ["Notion", "Sarah"], "prompt": "Product sync"},
                    "turn_detection": None,
                    "noise_reduction": {"type": "near_field"},
                }
            },
        },
    }
    # append… commit, three times; nothing else
    body = conn.types[1:]
    assert body.count("input_audio_buffer.commit") == 3 and body[-1] == "input_audio_buffer.commit"
    assert set(body) == {"input_audio_buffer.append", "input_audio_buffer.commit"}
    # 24 kHz PCM16 covering exactly each utterance; silence between utterances is never sent
    for s, nbytes in zip(segs, conn.committed_audio, strict=True):
        assert nbytes / 2 / RATE == pytest.approx(s.t_end - s.t_start, abs=0.02)
    assert [m for m, _ in usage] == ["gpt-transcribe"] * 3
    assert t.billed_seconds == pytest.approx(sum(n / 2 / RATE for n in conn.committed_audio))
    assert t.billed_seconds < len(bursts(3)) / SAMPLE_RATE * 0.9
    # live captions accumulate per utterance
    assert partials[:2] == [("mic", "Me", "utterance "), ("mic", "Me", "utterance 1")]


def test_realtime_session_update_for_older_models_and_system_audio() -> None:
    t = OpenAIRealtimeTranscriber(
        OpenAIRealtimeConfig(model="gpt-4o-mini-transcribe", noise_reduction="none"),
        api_key="k", language="de", keywords=["Notion"],
    )
    audio_in = t.session_update()["session"]["audio"]["input"]
    assert audio_in["transcription"] == {"model": "gpt-4o-mini-transcribe", "language": "de"}  # no keywords field
    assert audio_in["noise_reduction"] is None
    live = OpenAIRealtimeTranscriber(
        OpenAIRealtimeConfig(model="gpt-live-transcribe", noise_reduction="far_field", delay="low"),
        api_key="k", keywords=[f"term{i}" for i in range(150)],
    )
    live_in = live.session_update()["session"]["audio"]["input"]
    assert live_in["transcription"] == {"model": "gpt-live-transcribe", "keywords": [f"term{i}" for i in range(100)], "delay": "low"}
    assert live_in["noise_reduction"] == {"type": "far_field"}


async def test_realtime_results_follow_commit_order_not_arrival_order() -> None:
    async def late_first(ws: Any, conn: _Conn) -> None:
        iid = conn.item()
        await ws.send(json.dumps({"type": "input_audio_buffer.committed", "item_id": iid}))
        if conn.commits == 1:
            return  # its transcript only comes after the second one's
        done = [(iid, f"second {conn.commits}")]
        if conn.commits == 2:
            done.append((conn.item(1), "first"))
        for item_id, text in done:
            await ws.send(json.dumps({"type": "conversation.item.input_audio_transcription.completed", "item_id": item_id, "transcript": text}))

    _, segs = await _realtime(_RealtimeMock(late_first))
    assert [s.text for s in segs] == ["first", "second 2", "second 3"]
    assert [s.t_start for s in segs] == sorted(s.t_start for s in segs)


async def test_realtime_too_short_utterance_is_cleared_not_committed() -> None:
    mock = _RealtimeMock()
    sig = np.concatenate([noise(1.0), speechlike(0.2, seed=5), noise(1.5, seed=6), speechlike(2.5, seed=7), noise(1.5, seed=8)])
    t, segs = await _realtime(mock, sig)
    (conn,) = mock.conns
    marks = [k for k in conn.types if k != "input_audio_buffer.append"]
    assert marks == ["session.update", "input_audio_buffer.clear", "input_audio_buffer.commit"]
    assert [s.text for s in segs] == ["utterance 1 (conn 1)"]
    assert segs[0].t_start > 2.0  # the real utterance, not the blip
    assert t.billed_seconds == pytest.approx(conn.committed_audio[0] / 2 / RATE)  # the blip isn't billed


async def test_realtime_reconnects_and_resends_when_the_socket_drops() -> None:
    async def drop_first(ws: Any, conn: _Conn) -> None:
        if conn.n == 1:
            await ws.close()  # results for the committed utterance are lost with the socket
            return
        await _RealtimeMock.answer(ws, conn)

    mock = _RealtimeMock(drop_first)
    t, segs = await _realtime(mock)
    assert len(mock.conns) == 2
    second = mock.conns[1]
    assert second.types[0] == "session.update"
    assert [s.text for s in segs] == [f"utterance {k} (conn 2)" for k in (1, 2, 3)]  # nothing lost or duplicated
    assert second.commits == 3
    # the first utterance was re-sent in full
    assert second.committed_audio[0] == mock.conns[0].committed_audio[0]
    assert [s.t_start for s in segs] == pytest.approx([_onset(k) - PRE_ROLL_S for k in range(3)], abs=0.05)
    assert t.reconnects >= 1


async def test_realtime_does_not_rotate_before_rotate_after_s() -> None:
    mock = _RealtimeMock()
    await _realtime(mock, cfg={"rotate_after_s": 3600.0})
    assert len(mock.conns) == 1


async def test_realtime_rotates_the_session_between_utterances() -> None:
    mock = _RealtimeMock()
    # every transcript is in before the next utterance starts, so each start is a safe point to rotate
    _, segs = await _realtime(mock, cfg={"rotate_after_s": 0.0}, hold={1: 1, 2: 2})
    assert [s.text for s in segs] == [f"utterance 1 (conn {n})" for n in (1, 2, 3)]
    assert len(segs) == 3 and mock.commits == 3  # nothing re-sent
    assert len(mock.conns) == 3  # and no socket is opened after the last utterance
    for conn in mock.conns:  # no utterance is split across sockets
        assert conn.types[0] == "session.update"
        assert conn.types[-1] == "input_audio_buffer.commit"


async def test_realtime_rotation_waits_until_trailing_results_are_in() -> None:
    def completed(item_id: str, text: str) -> str:
        return json.dumps({"type": "conversation.item.input_audio_transcription.completed", "item_id": item_id, "transcript": text})

    async def first_answered_late(ws: Any, conn: _Conn) -> None:
        iid = conn.item()
        await ws.send(json.dumps({"type": "input_audio_buffer.committed", "item_id": iid}))
        if (conn.n, conn.commits) == (1, 1):
            return  # still outstanding when the second utterance starts: no rotation then
        if (conn.n, conn.commits) == (1, 2):
            await ws.send(completed(conn.item(1), "first"))
        await ws.send(completed(iid, f"utterance {conn.commits} (conn {conn.n})"))

    mock = _RealtimeMock(first_answered_late)
    _, segs = await _realtime(mock, cfg={"rotate_after_s": 0.0}, hold={2: 2})
    assert [s.text for s in segs] == ["first", "utterance 2 (conn 1)", "utterance 1 (conn 2)"]
    assert [c.commits for c in mock.conns] == [2, 1]  # rotated at the first idle start, nothing re-sent
    for conn in mock.conns:
        assert conn.types[0] == "session.update"
        assert conn.types[-1] == "input_audio_buffer.commit"
    assert [s.t_start for s in segs] == pytest.approx([_onset(k) - PRE_ROLL_S for k in range(3)], abs=0.05)


async def test_realtime_commit_empty_error_skips_only_that_utterance() -> None:
    async def reject_first(ws: Any, conn: _Conn) -> None:
        if conn.commits == 1:
            await ws.send(json.dumps({"type": "error", "error": {
                "type": "invalid_request_error", "code": "input_audio_buffer_commit_empty",
                "message": "buffer too small", "param": None, "event_id": None}}))
            return
        await _RealtimeMock.answer(ws, conn)

    _, segs = await _realtime(_RealtimeMock(reject_first), result_timeout_s=5.0)
    assert [s.text for s in segs] == ["utterance 2 (conn 1)", "utterance 3 (conn 1)"]
    assert [s.t_start for s in segs] == pytest.approx([_onset(k) - PRE_ROLL_S for k in (1, 2)], abs=0.05)


async def test_realtime_failed_transcription_and_hallucinations_are_skipped() -> None:
    texts = {2: "Subtitles by the Amara.org community", 3: "Thank you."}

    async def mixed(ws: Any, conn: _Conn) -> None:
        iid = conn.item()
        await ws.send(json.dumps({"type": "input_audio_buffer.committed", "item_id": iid}))
        if conn.commits == 1:
            await ws.send(json.dumps({"type": "conversation.item.input_audio_transcription.failed", "item_id": iid,
                                      "error": {"type": "server_error", "message": "boom"}}))
            return
        await ws.send(json.dumps({"type": "conversation.item.input_audio_transcription.completed", "item_id": iid,
                                  "transcript": texts[conn.commits]}))

    _, segs = await _realtime(_RealtimeMock(mixed))
    assert [s.text for s in segs] == ["Thank you."]  # 2 s of speech: a real "thank you"


async def test_realtime_bills_committed_audio_even_without_a_transcript() -> None:
    async def fail_first(ws: Any, conn: _Conn) -> None:
        if conn.commits == 1:
            iid = conn.item()
            await ws.send(json.dumps({"type": "input_audio_buffer.committed", "item_id": iid}))
            await ws.send(json.dumps({"type": "conversation.item.input_audio_transcription.failed", "item_id": iid,
                                      "error": {"type": "server_error", "message": "boom"}}))
            return
        await _RealtimeMock.answer(ws, conn)

    mock = _RealtimeMock(fail_first)
    t, segs = await _realtime(mock)
    assert len(segs) == 2
    (conn,) = mock.conns
    assert len(conn.committed_audio) == 3  # the failed utterance's audio was committed, so it is billed too
    assert t.billed_seconds == pytest.approx(sum(conn.committed_audio) / 2 / RATE)


async def test_realtime_bills_audio_re_sent_after_a_reconnect() -> None:
    async def drop_first(ws: Any, conn: _Conn) -> None:
        if conn.n == 1:
            await ws.close()
            return
        await _RealtimeMock.answer(ws, conn)

    mock = _RealtimeMock(drop_first)
    t, segs = await _realtime(mock)
    assert len(segs) == 3 and [c.commits for c in mock.conns] == [1, 3]
    # the first utterance was committed on both sockets: each commit is transcribed, so each is billed
    assert t.billed_seconds == pytest.approx(sum(n for c in mock.conns for n in c.committed_audio) / 2 / RATE)


async def test_realtime_re_sent_utterance_starts_a_fresh_live_caption() -> None:
    async def drop_after_delta(ws: Any, conn: _Conn) -> None:
        iid = conn.item()
        await ws.send(json.dumps({"type": "input_audio_buffer.committed", "item_id": iid}))
        await ws.send(json.dumps({"type": "conversation.item.input_audio_transcription.delta", "item_id": iid,
                                  "delta": f"words {conn.commits}"}))
        if conn.n == 1:
            await ws.close()  # the final transcript is lost with the socket
        elif conn.commits > 1:
            await ws.send(json.dumps({"type": "conversation.item.input_audio_transcription.completed", "item_id": iid,
                                      "transcript": f"final {conn.commits}"}))
        # the re-sent first utterance never completes either, so its live caption stands in

    partials: list[str] = []
    _, segs = await _realtime(_RealtimeMock(drop_after_delta), completion_timeout_s=0.5,
                              on_partial=lambda src, sp, text: partials.append(text))
    assert [s.text for s in segs] == ["words 1", "final 2", "final 3"]
    assert partials[:2] == ["words 1", "words 1"]  # re-sent: the caption starts over instead of doubling


async def test_realtime_rejected_key_raises_from_stream() -> None:
    def unauthorized(connection: Any, request: Any) -> Any:
        return connection.respond(http.HTTPStatus.UNAUTHORIZED, "invalid api key\n")

    mock = _RealtimeMock()
    with pytest.raises(TranscriptionError) as err:
        await asyncio.wait_for(_realtime(mock, process_request=unauthorized), timeout=10)
    assert err.value.fatal and "401" in str(err.value)
    assert mock.conns == []


def test_realtime_requires_an_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(TranscriptionError, match="OPENAI_API_KEY"):
        OpenAIRealtimeTranscriber()


# ============================================================================ Deepgram


class _DeepgramSocket:
    """In-memory Deepgram connection: answers each Finalize with one final result covering the audio
    since the previous one; ``dies_after_finalize`` makes the first audio sent after a Finalize fail."""

    def __init__(self, n: int, dies_after_finalize: bool) -> None:
        self.n = n
        self.dies_after_finalize = dies_after_finalize
        self.finalized = False
        self.received = self.answered = 0.0
        self.inbox: asyncio.Queue[str | None] = asyncio.Queue()

    async def send(self, payload: bytes | str) -> None:
        if isinstance(payload, bytes):
            if self.dies_after_finalize and self.finalized:
                raise ConnectionResetError("socket is dead")  # the receiver hasn't noticed yet
            self.received += len(payload) / 2 / SAMPLE_RATE
        elif json.loads(payload)["type"] == "Finalize":
            self.finalized = True
            self.inbox.put_nowait(json.dumps({
                "type": "Results", "start": self.answered, "duration": self.received - self.answered, "is_final": True,
                "from_finalize": True, "channel": {"alternatives": [{"transcript": f"utterance on socket {self.n}"}]},
            }))
            self.answered = self.received

    async def close(self) -> None:
        self.inbox.put_nowait(None)

    def __aiter__(self) -> _DeepgramSocket:
        return self

    async def __anext__(self) -> str:
        raw = await self.inbox.get()
        if raw is None:
            raise StopAsyncIteration
        return raw


async def test_deepgram_utterance_whose_first_chunk_hits_a_dead_socket_is_kept() -> None:
    sockets: list[_DeepgramSocket] = []

    async def connect(url: str, **kw: Any) -> _DeepgramSocket:
        sockets.append(_DeepgramSocket(len(sockets) + 1, dies_after_finalize=not sockets))
        return sockets[-1]

    t = DeepgramTranscriber(DeepgramConfig(), api_key="k", connect=connect, result_timeout_s=0.5)
    segs = [s async for s in t.stream("mic", "Me", _frames(bursts(2)))]
    assert len(sockets) == 2
    assert [s.text for s in segs] == ["utterance on socket 1", "utterance on socket 2"]


class _DyingMidUtterance(_DeepgramSocket):
    """The first connection sends a final for the first half second it hears, then dies at 1 s."""

    said = False

    async def send(self, payload: bytes | str) -> None:
        if isinstance(payload, bytes) and self.n == 1:
            if self.received >= 1.0:
                raise ConnectionResetError("socket is dead")
            if self.received >= 0.5 and not self.said:
                self.said = True
                self.inbox.put_nowait(json.dumps({
                    "type": "Results", "start": 0.0, "duration": self.received, "is_final": True,
                    "channel": {"alternatives": [{"transcript": "heard on socket 1"}]},
                }))
        await super().send(payload)


async def test_deepgram_utterance_cut_off_mid_way_is_re_sent_once_and_billed_per_connection() -> None:
    sockets: list[_DeepgramSocket] = []

    async def connect(url: str, **kw: Any) -> _DeepgramSocket:
        sockets.append(_DyingMidUtterance(len(sockets) + 1, dies_after_finalize=False))
        return sockets[-1]

    t = DeepgramTranscriber(DeepgramConfig(), api_key="k", connect=connect, result_timeout_s=0.5)
    segs = [s async for s in t.stream("mic", "Me", _frames(bursts(1)))]
    assert len(sockets) == 2
    # the new connection transcribes the whole utterance again: the dead one's final isn't repeated
    assert [s.text for s in segs] == ["utterance on socket 2"]
    assert sockets[1].received == pytest.approx(segs[0].t_end - segs[0].t_start, abs=0.05)
    # Deepgram bills the audio each connection received, the re-sent part included
    assert sockets[0].received >= 1.0
    assert t.billed_seconds == pytest.approx(sockets[0].received + sockets[1].received)
