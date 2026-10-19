"""Transcriber interface.

Every backend exposes the same streaming contract::

    async for seg in transcriber.stream(source="mic", speaker="Me", frames=frames):
        ...

*Chunked* backends (OpenAI REST, local Whisper) subclass :class:`ChunkTranscriber`
and only implement :meth:`ChunkTranscriber.transcribe_utterance`; the base class
does VAD segmentation, bounded concurrency, in-order delivery, retries and
hallucination filtering. *Streaming* backends (OpenAI Realtime, Deepgram)
implement :meth:`Transcriber.stream` directly.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

from ..audio.vad import VADSegmenter
from ..config import VADConfig
from ..models import AudioFrame, TranscriptSegment, Utterance

log = logging.getLogger(__name__)

UsageCallback = Callable[[str, float], None]
"""(model, billed_audio_seconds) -> None"""


@dataclass(slots=True)
class TranscribeResult:
    text: str
    confidence: float | None = None
    language: str | None = None


class TranscriptionError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False, fatal: bool = False):
        super().__init__(message)
        self.retryable = retryable
        self.fatal = fatal
        """Retrying can't help (bad credentials, missing package): stop the pipeline."""


class Transcriber(ABC):
    backend: str = "base"

    def __init__(self, model: str, on_usage: UsageCallback | None = None) -> None:
        self.model = model
        self.on_usage = on_usage
        self.billed_seconds = 0.0

    def _bill(self, seconds: float) -> None:
        self.billed_seconds += seconds
        if self.on_usage and seconds > 0:
            try:
                self.on_usage(self.model, seconds)
            except Exception:  # pragma: no cover
                log.exception("usage callback failed")

    @abstractmethod
    def stream(
        self, source: str, speaker: str, frames: AsyncIterator[AudioFrame]
    ) -> AsyncIterator[TranscriptSegment]:
        """Consume frames until exhausted; yield final transcript segments."""

    async def aclose(self) -> None:
        """Release network clients / models."""

    def segment(self, source: str, speaker: str, text: str, t0: float, t1: float, conf: float | None = None) -> TranscriptSegment:
        return TranscriptSegment(
            source=source,
            speaker=speaker,
            text=text,
            t_start=t0,
            t_end=t1,
            backend=f"{self.backend}:{self.model}",
            confidence=conf,
        )


class ChunkTranscriber(Transcriber):
    """VAD-chunked transcription with up to ``concurrency`` requests in flight."""

    concurrency = 3
    max_retries = 3

    def __init__(
        self,
        model: str,
        vad: VADConfig | None = None,
        on_usage: UsageCallback | None = None,
        prompt: str | None = None,
        language: str | None = None,
    ) -> None:
        super().__init__(model, on_usage)
        self.vad_cfg = vad or VADConfig()
        self.base_prompt = prompt
        self.language = language
        self.on_level: Callable[[str, float, bool], None] | None = None

    @abstractmethod
    async def transcribe_utterance(self, utt: Utterance, prompt: str | None) -> TranscribeResult:
        """Transcribe one utterance (int16 mono 16 kHz)."""

    def build_prompt(self, previous_text: str) -> str | None:
        parts = [p for p in (self.base_prompt, previous_text[-300:].strip()) if p]
        return " ".join(parts) if parts else None

    async def _transcribe_with_retry(self, utt: Utterance, prompt: str | None) -> TranscribeResult | None:
        delay = 1.0
        for attempt in range(self.max_retries + 1):
            try:
                return await self.transcribe_utterance(utt, prompt)
            except TranscriptionError as exc:
                if exc.fatal:
                    raise
                if not exc.retryable or attempt == self.max_retries:
                    log.error("%s transcription failed (%.1fs audio): %s", self.backend, utt.duration, exc)
                    return None
            except (asyncio.TimeoutError, OSError) as exc:
                if attempt == self.max_retries:
                    log.error("%s transcription failed after retries: %r", self.backend, exc)
                    return None
            except ImportError:
                raise  # a missing package won't install itself: stop the pipeline
            except Exception:
                # a bug or an odd reply must cost this utterance, not the whole stream
                log.exception("%s transcription failed (%.1fs audio)", self.backend, utt.duration)
                return None
            await asyncio.sleep(delay)
            delay = min(delay * 2, 8.0)
        return None

    async def stream(
        self, source: str, speaker: str, frames: AsyncIterator[AudioFrame]
    ) -> AsyncIterator[TranscriptSegment]:
        level_cb = None
        if self.on_level is not None:
            cb = self.on_level
            level_cb = lambda db, sp: cb(source, db, sp)  # noqa: E731
        seg = VADSegmenter(source, self.vad_cfg, on_level=level_cb)
        pending: asyncio.Queue[asyncio.Task[TranscribeResult | None] | None] = asyncio.Queue()
        sem = asyncio.Semaphore(self.concurrency)
        prev_text = ""
        utterances: dict[asyncio.Task[TranscribeResult | None], Utterance] = {}

        async def run(utt: Utterance, prompt: str | None) -> TranscribeResult | None:
            async with sem:
                return await self._transcribe_with_retry(utt, prompt)

        def submit(utt: Utterance) -> None:
            task = asyncio.ensure_future(run(utt, self.build_prompt(prev_text)))
            utterances[task] = utt
            pending.put_nowait(task)

        async def feed() -> None:
            try:
                async for frame in frames:
                    for utt in seg.push(frame):
                        submit(utt)
                for utt in seg.flush():
                    submit(utt)
            finally:
                pending.put_nowait(None)

        feeder = asyncio.ensure_future(feed())
        try:
            while True:
                task = await pending.get()
                if task is None:
                    break
                result = await task
                utt = utterances.pop(task)
                if result is None:
                    continue  # the request failed, and failed requests aren't billed
                self._bill(utt.duration)  # transcribed (even if filtered below): billed
                text = clean_transcript(result.text)
                if not text or is_hallucination(text, utt.speech_seconds):
                    continue
                prev_text = (prev_text + " " + text)[-600:]
                yield self.segment(source, speaker, text, utt.t_start, utt.t_end, result.confidence)
            await feeder  # surface capture errors
        finally:
            if not feeder.done():
                feeder.cancel()
                # let the cancellation close the frames before anyone else tries to (aclose() on a
                # generator still running in the feeder would fail)
                with contextlib.suppress(BaseException):
                    await feeder
            for t in list(utterances):
                t.cancel()


# ------------------------------------------------------------------- text cleanup

_HALLUCINATIONS = {
    "thank you.",
    "thank you",
    "thanks.",
    "thanks for watching!",
    "thanks for watching.",
    "thank you for watching.",
    "thank you for watching!",
    "please subscribe.",
    "subscribe to my channel.",
    "bye.",
    "you",
    "you.",
    ".",
    "...",
    "so",
    "okay.",
    "hmm.",
    "uh",
    "um",
    "merci.",
    "merci",
    "merci beaucoup.",
    "au revoir.",
    "euh",
}

_HALLUCINATION_PATTERNS = [
    re.compile(r"^(subtitles|captions|transcription|transcribed) (by|from)\b", re.I),
    re.compile(r"amara\.org", re.I),
    re.compile(r"^\W*(music|applause|silence|noise|inaudible|blank_audio|musique|applaudissements|rires)\W*$", re.I),
    # Whisper's stock French inventions on silence/noise (also "— Sous-titrage ST'501 —")
    re.compile(r"^\W*sous-titr(age|es|é)", re.I),
    re.compile(r"\b(soci[ée]t[ée] radio-canada|merci d'avoir regard[ée]|abonnez-vous|soustitreur\.com)\b", re.I),
]


def clean_transcript(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text


def is_hallucination(text: str, speech_seconds: float) -> bool:
    """Whisper-family models invent stock phrases for noise/silence."""
    low = text.lower().strip()
    if any(p.search(low) for p in _HALLUCINATION_PATTERNS):
        return True
    if low in _HALLUCINATIONS and speech_seconds < 1.5:
        return True
    # pathological repetition ("the the the the ...")
    words = low.split()
    if len(words) >= 12 and len(set(words)) <= max(2, len(words) // 8):
        return True
    return False


class FakeTranscriber(ChunkTranscriber):
    """Deterministic backend for tests and dry runs.

    Returns texts from ``script`` in order (cycling ``None`` -> placeholder), so a
    synthetic WAV with N speech bursts produces N known segments.
    """

    backend = "fake"
    concurrency = 1  # deterministic ordering of scripted replies

    def __init__(self, script: list[str] | None = None, vad: VADConfig | None = None, **kw) -> None:
        super().__init__("fake", vad=vad, **kw)
        self.script = list(script or [])
        self.calls: list[Utterance] = []

    async def transcribe_utterance(self, utt: Utterance, prompt: str | None) -> TranscribeResult:
        self.calls.append(utt)
        if self.script:
            return TranscribeResult(self.script.pop(0))
        return TranscribeResult(f"(speech {utt.duration:.1f}s)")
