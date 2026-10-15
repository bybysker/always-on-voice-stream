"""Deepgram live streaming (``wss://api.deepgram.com/v1/listen``).

Deepgram bills streamed audio by duration, silence included, so the socket is
fed only while our :class:`~voicestream.audio.vad.SpeechGate` says someone is
talking; ``KeepAlive`` messages hold the connection open in between and a
``Finalize`` at the end of each utterance flushes its final transcript. Audio
goes up as 16 kHz linear16 (no resampling needed).

Deepgram timestamps (``start``) are positions in the audio *sent on this
connection*; we map them back to the meeting clock through the offsets at
which each utterance was sent.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from ..audio.base import pcm16_bytes
from ..audio.vad import SpeechGate
from ..config import DeepgramConfig, VADConfig
from ..models import SAMPLE_RATE, AudioFrame, TranscriptSegment
from .base import Transcriber, TranscriptionError, UsageCallback, clean_transcript, is_hallucination

log = logging.getLogger(__name__)

PartialCallback = Callable[[str, str, str], None]
KEEPALIVE_S = 4.0
FINALIZE_WAIT_S = 6.0


@dataclass
class _Utt:
    t_start: float
    t_end: float = 0.0
    speech_seconds: float = 0.0
    chunks: list[bytes] = field(default_factory=list)
    stream_start: float = 0.0  # position on the current connection's audio timeline
    stream_end: float | None = None
    ended_at: float | None = None
    finals: list[str] = field(default_factory=list)
    confidences: list[float] = field(default_factory=list)
    done: bool = False

    @property
    def seconds(self) -> float:
        return sum(len(c) for c in self.chunks) / 2 / SAMPLE_RATE


class DeepgramTranscriber(Transcriber):
    backend = "deepgram"

    def __init__(
        self,
        cfg: DeepgramConfig | None = None,
        *,
        vad: VADConfig | None = None,
        language: str | None = None,
        keywords: list[str] | None = None,
        api_key: str | None = None,
        on_usage: UsageCallback | None = None,
        on_partial: PartialCallback | None = None,
        connect: Callable[..., Any] | None = None,
        result_timeout_s: float = 15.0,
    ) -> None:
        self.cfg = cfg or DeepgramConfig()
        super().__init__(self.cfg.model, on_usage)
        self.vad_cfg = vad or VADConfig()
        self.language = language
        self.keywords = [k for k in [*(keywords or []), *self.cfg.keyterms] if k.strip()]
        self.api_key = api_key or os.environ.get("DEEPGRAM_API_KEY")
        if not self.api_key:
            raise TranscriptionError("DEEPGRAM_API_KEY is not set")
        self.on_partial = on_partial
        self.on_level: Callable[[str, float, bool], None] | None = None
        self._connect = connect
        self.result_timeout_s = result_timeout_s

    def url(self) -> str:
        params: list[tuple[str, str]] = [
            ("model", self.model),
            ("encoding", "linear16"),
            ("sample_rate", str(SAMPLE_RATE)),
            ("channels", "1"),
            ("interim_results", "true"),
            ("punctuate", "true"),
            ("smart_format", str(self.cfg.smart_format).lower()),
            ("endpointing", str(self.cfg.endpointing_ms)),
        ]
        if self.cfg.diarize:
            params.append(("diarize_model", "latest"))  # replaces the deprecated diarize=true
        if self.language:
            params.append(("language", self.language))
        # nova-3 takes ``keyterm`` (prompting); older models take ``keywords``
        key = "keyterm" if self.model.startswith("nova-3") else "keywords"
        params += [(key, k) for k in self.keywords[:50]]
        return f"{self.cfg.url}?{urlencode(params)}"

    async def _open(self) -> Any:
        connect = self._connect
        if connect is None:
            from websockets.asyncio.client import connect  # the top-level alias is legacy on websockets 13
        return await connect(
            self.url(), additional_headers={"Authorization": f"Token {self.api_key}"}, max_size=None, open_timeout=15
        )

    async def stream(
        self, source: str, speaker: str, frames: AsyncIterator[AudioFrame]
    ) -> AsyncIterator[TranscriptSegment]:
        level_cb = None
        if self.on_level is not None:
            cb = self.on_level
            level_cb = lambda db, sp: cb(source, db, sp)  # noqa: E731
        gate = SpeechGate(source, self.vad_cfg, on_level=level_cb)
        inflight: deque[_Utt] = deque()
        out: asyncio.Queue[TranscriptSegment | None] = asyncio.Queue()
        st: dict[str, Any] = {"ws": None, "rx": None, "ka": None, "sent": 0.0, "last_send": 0.0, "current": None, "failed": None}
        lock = asyncio.Lock()

        def release() -> None:
            now = time.monotonic()
            for u in inflight:  # stragglers: Finalize answered but nothing marked it done
                if not u.done and u.ended_at is not None and now - u.ended_at > FINALIZE_WAIT_S:
                    u.done = True
            while inflight and inflight[0].done:
                u = inflight.popleft()
                text = clean_transcript(" ".join(u.finals))
                if text and not is_hallucination(text, u.speech_seconds):
                    conf = sum(u.confidences) / len(u.confidences) if u.confidences else None
                    out.put_nowait(self.segment(source, speaker, text, u.t_start, u.t_end, conf))

        def owner(stream_t: float) -> _Utt | None:
            for u in inflight:
                end = u.stream_end if u.stream_end is not None else float("inf")
                if u.stream_start - 0.05 <= stream_t < end + 0.05:
                    return u
            return None

        def on_message(ev: dict[str, Any]) -> None:
            if ev.get("type") == "Error":
                log.warning("%s: deepgram error %s: %s", source, ev.get("code"), ev.get("description") or ev.get("message"))
                return
            if ev.get("type") != "Results":
                return
            alts = (ev.get("channel") or {}).get("alternatives") or [{}]
            text = (alts[0].get("transcript") or "").strip()
            u = owner(float(ev.get("start", 0.0)))
            if u is None:
                return
            if not ev.get("is_final"):
                if text and self.on_partial:
                    with contextlib.suppress(Exception):
                        self.on_partial(source, speaker, " ".join([*u.finals, text]))
                return
            if text:
                u.finals.append(text)
                if alts[0].get("confidence") is not None:
                    u.confidences.append(float(alts[0]["confidence"]))
            if ev.get("from_finalize") and u.stream_end is not None:
                u.done = True
                for other in inflight:  # everything sent before it is final too
                    if other is u:
                        break
                    if other.stream_end is not None:
                        other.done = True
            release()

        async def receiver(ws: Any) -> None:
            try:
                async for raw in ws:
                    if isinstance(raw, (bytes, bytearray)):
                        continue
                    try:
                        ev = json.loads(raw)
                    except ValueError:
                        continue
                    try:
                        on_message(ev)
                    except Exception as exc:  # one odd message mustn't take the socket and its results down
                        log.warning("%s: ignoring unexpected deepgram message (%r): %.200s", source, exc, raw)
            except Exception as exc:
                log.info("%s: deepgram socket closed: %r", source, exc)
            finally:
                if st["ws"] is ws:
                    st["ws"] = None

        async def keepalive() -> None:
            while True:
                await asyncio.sleep(1.0)
                ws = st["ws"]
                if ws is not None and time.monotonic() - st["last_send"] >= KEEPALIVE_S:
                    try:
                        await ws.send(json.dumps({"type": "KeepAlive"}))
                        st["last_send"] = time.monotonic()
                    except Exception:
                        st["ws"] = None
                release()

        async def connected() -> Any:
            async with lock:
                if st["ws"] is not None:
                    return st["ws"]
                delay = 1.0
                while True:
                    try:
                        ws = await self._open()
                        break
                    except Exception as exc:
                        status = getattr(getattr(exc, "response", None), "status_code", None)
                        if status in (401, 403):
                            raise TranscriptionError(f"Deepgram rejected the API key (HTTP {status})", fatal=True) from exc
                        log.warning("%s: deepgram connect failed (%r); retrying in %.0fs", source, exc, delay)
                        await asyncio.sleep(delay)
                        delay = min(delay * 2, 30.0)
                st["ws"], st["sent"], st["last_send"] = ws, 0.0, time.monotonic()
                if st["rx"] is not None:
                    st["rx"].cancel()
                st["rx"] = asyncio.ensure_future(receiver(ws))
                # a new connection has a new timeline: re-base every unresolved utterance on it
                # (the current one too, even before its first chunk got through) and re-send its audio
                for u in inflight:
                    if u.done:
                        continue
                    u.stream_start = st["sent"]
                    u.finals.clear()  # the new connection transcribes all of it again
                    u.confidences.clear()
                    for c in u.chunks:
                        await ws.send(c)
                        st["sent"] += len(c) / 2 / SAMPLE_RATE
                    self._bill(u.seconds)  # Deepgram bills the audio each connection receives
                    if u.stream_end is not None:
                        u.stream_end = st["sent"]
                        await ws.send(json.dumps({"type": "Finalize"}))
                        u.ended_at = time.monotonic()
                st["last_send"] = time.monotonic()
                return ws

        async def send(payload: bytes | str) -> None:
            for attempt in range(3):
                ws = await connected()
                try:
                    await ws.send(payload)
                    st["last_send"] = time.monotonic()
                    if isinstance(payload, bytes):
                        st["sent"] += len(payload) / 2 / SAMPLE_RATE
                    return
                except Exception as exc:
                    log.info("%s: deepgram send failed (%r), reconnecting", source, exc)
                    if st["ws"] is ws:
                        st["ws"] = None
                    if attempt == 2:
                        raise

        async def on_gate(ev: Any) -> None:
            if ev.kind == "start":
                await connected()
                u = _Utt(t_start=ev.t, stream_start=st["sent"])
                st["current"] = u
                inflight.append(u)
                chunk = pcm16_bytes(ev.samples)
                await send(chunk)
                u.chunks.append(chunk)
            elif ev.kind == "audio" and st["current"] is not None:
                chunk = pcm16_bytes(ev.samples)
                await send(chunk)
                st["current"].chunks.append(chunk)
            elif ev.kind == "end" and st["current"] is not None:
                u = st["current"]
                st["current"] = None
                u.t_end, u.speech_seconds = ev.t, ev.speech_seconds
                u.stream_end = st["sent"]
                u.ended_at = time.monotonic()
                self._bill(u.seconds)
                await send(json.dumps({"type": "Finalize"}))

        async def feed() -> None:
            st["ka"] = asyncio.ensure_future(keepalive())
            try:
                async for frame in frames:
                    for ev in gate.push(frame):
                        await on_gate(ev)
                for ev in gate.flush():
                    await on_gate(ev)
                deadline = time.monotonic() + self.result_timeout_s
                while inflight and time.monotonic() < deadline:
                    if st["ws"] is None and any(not u.done for u in inflight):
                        await connected()
                    await asyncio.sleep(0.05)
                    release()
                for u in inflight:
                    u.done = True
                release()
            except Exception as exc:
                st["failed"] = exc
            finally:
                st["ka"].cancel()
                ws = st["ws"]
                st["ws"] = None
                if ws is not None:
                    with contextlib.suppress(Exception):
                        await ws.send(json.dumps({"type": "CloseStream"}))
                        await ws.close()
                if st["rx"] is not None:
                    st["rx"].cancel()
                out.put_nowait(None)

        feeder = asyncio.ensure_future(feed())
        try:
            while True:
                seg = await out.get()
                if seg is None:
                    break
                yield seg
            if st["failed"] is not None:
                raise st["failed"]
        finally:
            if not feeder.done():
                feeder.cancel()
                with contextlib.suppress(BaseException):
                    await feeder
