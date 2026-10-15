"""Free, offline transcription with faster-whisper (CTranslate2).

``small`` with int8 on a laptop CPU transcribes speech several times faster
than real time; ``base``/``tiny`` are lighter, ``medium``/``large-v3`` (ideally
on a GPU: ``device = "cuda"``, ``compute_type = "float16"``) more accurate.
Nothing leaves the machine, so the speech-to-text cost is $0.
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
from typing import Any

from ..audio.base import int16_to_float
from ..config import LocalWhisperConfig, VADConfig
from ..models import Utterance
from .base import ChunkTranscriber, TranscribeResult, TranscriptionError, UsageCallback

log = logging.getLogger(__name__)

_MODELS: dict[tuple[str, str, str], Any] = {}
_MODELS_LOCK = threading.Lock()


def _load(cfg: LocalWhisperConfig) -> Any:
    key = (cfg.model, cfg.device, cfg.compute_type)
    with _MODELS_LOCK:
        if key not in _MODELS:
            try:
                from faster_whisper import WhisperModel  # type: ignore[import-not-found]
            except ImportError as exc:
                raise TranscriptionError("faster-whisper is not installed: pip install 'voicestream[local]'", fatal=True) from exc
            log.info("loading whisper model %s (%s/%s)…", *key)
            try:
                _MODELS[key] = WhisperModel(cfg.model, device=cfg.device, compute_type=cfg.compute_type)
            except Exception as exc:  # unknown model, unsupported device/compute type, failed download
                # not fatal: this utterance is skipped and the next one tries loading again
                raise TranscriptionError(f"could not load whisper model {cfg.model!r} ({cfg.device}/{cfg.compute_type}): {exc}") from exc
        return _MODELS[key]


class LocalWhisperTranscriber(ChunkTranscriber):
    backend = "local"
    concurrency = 1  # one model instance; CTranslate2 parallelizes internally

    def __init__(
        self,
        cfg: LocalWhisperConfig | None = None,
        *,
        vad: VADConfig | None = None,
        language: str | None = None,
        prompt: str | None = None,
        on_usage: UsageCallback | None = None,
        model: Any = None,
    ) -> None:
        self.cfg = cfg or LocalWhisperConfig()
        super().__init__("local", vad=vad, on_usage=on_usage, prompt=prompt, language=language)
        self._model = model

    def _transcribe_sync(self, utt: Utterance, prompt: str | None) -> TranscribeResult:
        model = self._model or _load(self.cfg)
        self._model = model
        segments, info = model.transcribe(
            int16_to_float(utt.samples),
            language=self.language,
            beam_size=self.cfg.beam_size,
            initial_prompt=prompt,
            vad_filter=False,  # we already cut on speech
            condition_on_previous_text=False,
            without_timestamps=True,
        )
        texts, logprobs = [], []
        for s in segments:
            if getattr(s, "no_speech_prob", 0.0) > 0.6 and getattr(s, "avg_logprob", 0.0) < -1.0:
                continue  # likely noise hallucination
            texts.append(s.text)
            logprobs.append(getattr(s, "avg_logprob", 0.0))
        conf = math.exp(sum(logprobs) / len(logprobs)) if logprobs else None
        return TranscribeResult(" ".join(t.strip() for t in texts), confidence=conf, language=getattr(info, "language", None))

    async def transcribe_utterance(self, utt: Utterance, prompt: str | None) -> TranscribeResult:
        return await asyncio.to_thread(self._transcribe_sync, utt, prompt)
