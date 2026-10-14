"""Speech-to-text backends behind one streaming interface (see :mod:`.base`)."""

from __future__ import annotations

from collections.abc import Callable

from ..config import Config
from .base import ChunkTranscriber, FakeTranscriber, Transcriber, TranscriptionError, UsageCallback

PartialCallback = Callable[[str, str, str], None]


def keywords_for(cfg: Config) -> list[str]:
    seen: dict[str, None] = {}
    for word in [*cfg.brain.vocabulary, *cfg.brain.attendees, cfg.user_name]:
        w = (word or "").strip()
        if w and w.lower() not in {"me", "others"}:
            seen.setdefault(w, None)
    return list(seen)


def build_transcriber(
    cfg: Config,
    source: str,
    *,
    on_usage: UsageCallback | None = None,
    on_partial: PartialCallback | None = None,
    backend: str | None = None,
) -> Transcriber:
    tc = cfg.transcription
    backend = backend or tc.backend
    keywords = keywords_for(cfg)
    if backend == "openai":
        from .openai_rest import OpenAITranscriber

        return OpenAITranscriber(tc.openai, vad=cfg.vad, language=tc.language, keywords=keywords, on_usage=on_usage)
    if backend == "openai-realtime":
        from .openai_realtime import OpenAIRealtimeTranscriber

        rt = tc.openai_realtime
        if source != "mic" and rt.noise_reduction != "none":
            rt = rt.model_copy(update={"noise_reduction": "none"})  # system audio is already clean
        return OpenAIRealtimeTranscriber(
            rt, vad=cfg.vad, language=tc.language, keywords=keywords, on_usage=on_usage, on_partial=on_partial
        )
    if backend == "deepgram":
        from .deepgram import DeepgramTranscriber

        return DeepgramTranscriber(
            tc.deepgram, vad=cfg.vad, language=tc.language, keywords=keywords, on_usage=on_usage, on_partial=on_partial
        )
    if backend == "local":
        from .local_whisper import LocalWhisperTranscriber

        return LocalWhisperTranscriber(
            tc.local, vad=cfg.vad, language=tc.language, prompt=", ".join(keywords) or None, on_usage=on_usage
        )
    if backend == "fake":
        return FakeTranscriber(vad=cfg.vad, on_usage=on_usage)
    raise TranscriptionError(f"unknown transcription backend '{backend}'")


__all__ = [
    "ChunkTranscriber",
    "FakeTranscriber",
    "Transcriber",
    "TranscriptionError",
    "build_transcriber",
    "keywords_for",
]
