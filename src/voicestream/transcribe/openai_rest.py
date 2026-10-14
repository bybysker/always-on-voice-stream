"""OpenAI REST transcription, one request per VAD utterance.

Simple, stateless and cheap: only speech is uploaded, failed chunks are retried
independently, and nothing is lost if the network blips. Latency is roughly
"end of sentence + 1-2 s", which is plenty for extracting action items.
"""

from __future__ import annotations

import logging
import os

import httpx

from ..audio.base import wav_bytes
from ..config import OpenAITranscribeConfig, VADConfig
from ..models import Utterance
from .base import ChunkTranscriber, TranscribeResult, TranscriptionError, UsageCallback

log = logging.getLogger(__name__)

# Models that take ``languages[]``/``keywords[]`` (2026+) instead of ``language``.
_NEW_STYLE = ("gpt-transcribe", "gpt-live-transcribe")


class OpenAITranscriber(ChunkTranscriber):
    backend = "openai"

    def __init__(
        self,
        cfg: OpenAITranscribeConfig | None = None,
        *,
        vad: VADConfig | None = None,
        language: str | None = None,
        keywords: list[str] | None = None,
        api_key: str | None = None,
        on_usage: UsageCallback | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.cfg = cfg or OpenAITranscribeConfig()
        super().__init__(self.cfg.model, vad=vad, on_usage=on_usage, prompt=self.cfg.prompt, language=language)
        self.keywords = [k for k in (keywords or []) if k.strip()]
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        if not self.api_key:
            raise TranscriptionError("OPENAI_API_KEY is not set")
        self._own_client = client is None
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(self.cfg.timeout_s, connect=10.0))

    def form_fields(self, prompt: str | None) -> dict[str, str | list[str]]:
        """Multipart form fields; list values become repeated ``name[]`` parts."""
        fields: dict[str, str | list[str]] = {"model": self.model, "response_format": "json"}
        new_style = self.model.startswith(_NEW_STYLE)
        if self.language:
            if new_style:
                fields["languages[]"] = [self.language]
            else:
                fields["language"] = self.language
        if new_style and self.keywords:
            fields["keywords[]"] = self.keywords[:100]
        if prompt:
            fields["prompt"] = prompt[-1000:]
        return fields

    def build_prompt(self, previous_text: str) -> str | None:
        prompt = super().build_prompt(previous_text)
        if not self.model.startswith(_NEW_STYLE) and self.keywords:
            # older models have no keywords field; vocabulary in the prompt helps spelling
            vocab = "Vocabulary: " + ", ".join(self.keywords[:50]) + "."
            prompt = f"{vocab} {prompt}" if prompt else vocab
        return prompt

    async def transcribe_utterance(self, utt: Utterance, prompt: str | None) -> TranscribeResult:
        url = self.cfg.base_url.rstrip("/") + "/audio/transcriptions"
        files = {"file": ("utterance.wav", wav_bytes(utt.samples), "audio/wav")}
        try:
            resp = await self.client.post(
                url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                data=self.form_fields(prompt),
                files=files,
            )
        except httpx.TimeoutException as exc:
            raise TranscriptionError(f"timeout: {exc}", retryable=True) from exc
        except httpx.TransportError as exc:
            raise TranscriptionError(f"network error: {exc}", retryable=True) from exc
        if resp.status_code == 200:
            data = resp.json()
            langs = data.get("languages") or []
            lang = langs[0].get("code") if langs and isinstance(langs[0], dict) else data.get("language")
            return TranscribeResult(text=data.get("text", ""), language=lang)
        retryable = resp.status_code in (408, 409, 429) or resp.status_code >= 500
        detail = resp.text[:300]
        if resp.status_code in (401, 403):
            raise TranscriptionError(f"OpenAI rejected the API key (HTTP {resp.status_code}): {detail}", fatal=True)
        raise TranscriptionError(f"HTTP {resp.status_code}: {detail}", retryable=retryable)

    async def aclose(self) -> None:
        if self._own_client:
            await self.client.aclose()
