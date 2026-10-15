"""OpenAI Realtime API, transcription-only session (GA shape, checked 2026-10).

* ``wss://api.openai.com/v1/realtime?intent=transcription`` — model goes in the
  session config, not the URL; no ``OpenAI-Beta`` header (the beta shape is shut down).
* ``session.update`` with ``session.type = "transcription"`` and
  ``audio.input.format = {"type": "audio/pcm", "rate": 24000}``.
* ``turn_detection: null`` — our own VAD decides where utterances end and we
  ``input_audio_buffer.commit`` them. This works for both ``gpt-transcribe`` and
  ``gpt-live-transcribe`` (which rejects server VAD) and means silence is never
  uploaded (or billed).
* Results: ``conversation.item.input_audio_transcription.delta`` (live captions)
  and ``.completed`` (final), matched to our utterances through
  ``input_audio_buffer.committed`` events, which arrive in commit order.
* Sessions are capped at 60 minutes, so we rotate the socket between utterances.
  Utterances whose transcript hasn't arrived are re-sent after a reconnect.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import itertools
import json
import logging
import os
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..audio.base import StreamResampler, float_to_int16, int16_to_float
from ..audio.vad import SpeechGate
from ..config import OpenAIRealtimeConfig, VADConfig
from ..models import AudioFrame, TranscriptSegment
from .base import Transcriber, TranscriptionError, UsageCallback, clean_transcript, is_hallucination

log = logging.getLogger(__name__)

RATE = 24_000
PartialCallback = Callable[[str, str, str], None]
"""(source, speaker, partial_text) -> None"""


@dataclass
class _Utt:
    seq: int
    t_start: float
    t_end: float = 0.0
    speech_seconds: float = 0.0
    chunks: list[bytes] = field(default_factory=list)
    committed: bool = False
    item_id: str | None = None
    text: str | None = None
    done: bool = False
    partial: str = ""
    committed_at: float | None = None

    @property
    def audio_seconds(self) -> float:
        return sum(len(c) for c in self.chunks) / 2 / RATE


class OpenAIRealtimeTranscriber(Transcriber):
    backend = "openai-realtime"

    def __init__(
        self,
        cfg: OpenAIRealtimeConfig | None = None,
        *,
        vad: VADConfig | None = None,
        language: str | None = None,
        keywords: list[str] | None = None,
        api_key: str | None = None,
        on_usage: UsageCallback | None = None,
        on_partial: PartialCallback | None = None,
        connect: Callable[..., Any] | None = None,
        result_timeout_s: float = 20.0,
        completion_timeout_s: float = 12.0,
    ) -> None:
        self.cfg = cfg or OpenAIRealtimeConfig()
        super().__init__(self.cfg.model, on_usage)
        self.vad_cfg = vad or VADConfig()
        self.language = language
        self.keywords = [k for k in (keywords or []) if k.strip()]
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        if not self.api_key:
            raise TranscriptionError("OPENAI_API_KEY is not set")
        self.on_partial = on_partial
        self.on_level: Callable[[str, float, bool], None] | None = None
        self._connect = connect
        self.result_timeout_s = result_timeout_s
        self.completion_timeout_s = completion_timeout_s
        self.reconnects = 0

    # ----------------------------------------------------------------- protocol

    def session_update(self) -> dict[str, Any]:
        transcription: dict[str, Any] = {"model": self.model}
        new_style = self.model.startswith(("gpt-transcribe", "gpt-live-transcribe"))
        if self.language:
            if new_style:
                transcription["languages"] = [self.language]
            else:
                transcription["language"] = self.language
        if new_style and self.keywords:
            transcription["keywords"] = self.keywords[:100]
        if self.cfg.prompt:
            transcription["prompt"] = self.cfg.prompt
        if self.cfg.delay:
            transcription["delay"] = self.cfg.delay
        audio_input: dict[str, Any] = {
            "format": {"type": "audio/pcm", "rate": RATE},
            "transcription": transcription,
            "turn_detection": None,
        }
        audio_input["noise_reduction"] = (
            None if self.cfg.noise_reduction == "none" else {"type": self.cfg.noise_reduction}
        )
        return {"type": "session.update", "session": {"type": "transcription", "audio": {"input": audio_input}}}

    async def _open(self) -> Any:
        connect = self._connect
        if connect is None:
            from websockets.asyncio.client import connect  # the top-level alias is legacy on websockets 13
        ws = await connect(
            self.cfg.url,
            additional_headers={"Authorization": f"Bearer {self.api_key}"},
            max_size=None,
            open_timeout=15,
        )
        await ws.send(json.dumps(self.session_update()))
        return ws

    # ------------------------------------------------------------------- stream

    async def stream(
        self, source: str, speaker: str, frames: AsyncIterator[AudioFrame]
    ) -> AsyncIterator[TranscriptSegment]:
        level_cb = None
        if self.on_level is not None:
            cb = self.on_level
            level_cb = lambda db, sp: cb(source, db, sp)  # noqa: E731
        gate = SpeechGate(source, self.vad_cfg, on_level=level_cb)
        seq = itertools.count(1)
        inflight: deque[_Utt] = deque()  # committed/streaming utterances awaiting results, in order
        awaiting_ack: deque[_Utt] = deque()  # committed on the current socket, waiting for item_id
        by_item: dict[str, _Utt] = {}
        out: asyncio.Queue[TranscriptSegment | None] = asyncio.Queue()
        state: dict[str, Any] = {"ws": None, "opened": 0.0, "rx": None, "current": None, "failed": None, "reconnect": None}
        resampler = StreamResampler(16_000, RATE)

        def to24k(samples: np.ndarray) -> bytes:
            return float_to_int16(resampler.process(int16_to_float(samples))).astype("<i2").tobytes()

        def release_ready() -> None:
            # some streaming models occasionally never send ``.completed``: fall back to the
            # accumulated deltas so one lost result can't hold back every later transcript
            now = time.monotonic()
            for u in inflight:
                if not u.done and u.committed_at is not None and now - u.committed_at > self.completion_timeout_s:
                    log.warning("%s: no final transcript after %.0fs; using live deltas", source, self.completion_timeout_s)
                    u.text, u.done = u.partial, True
            while inflight and inflight[0].done:
                u = inflight.popleft()
                if u.item_id:
                    by_item.pop(u.item_id, None)
                text = clean_transcript(u.text or "")
                if text and not is_hallucination(text, u.speech_seconds):
                    out.put_nowait(self.segment(source, speaker, text, u.t_start, u.t_end))

        conn_lock = asyncio.Lock()

        async def receiver(ws: Any) -> None:
            try:
                async for raw in ws:
                    try:
                        ev = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    try:
                        self._handle(ev, awaiting_ack, by_item, source, speaker)
                    except Exception as exc:  # one odd event mustn't take the socket and its results down
                        log.warning("%s: ignoring unexpected realtime event (%r): %.200s", source, exc, raw)
                    release_ready()
            except Exception as exc:  # connection closed / network error
                log.info("%s realtime socket closed: %r", source, exc)
            finally:
                if state["ws"] is ws:
                    state["ws"] = None
                    if any(u.committed and not u.done for u in inflight):
                        # results were lost with the socket: reconnect now and re-send
                        # (keep a reference: the loop only holds tasks weakly)
                        state["reconnect"] = asyncio.ensure_future(_reconnect_quietly())

        async def _reconnect_quietly() -> None:
            try:
                await ensure_connected()
            except Exception as exc:
                state["failed"] = exc

        async def ensure_connected() -> Any:
            async with conn_lock:
                ws = state["ws"]
                # idle: between utterances with every result in, so closing the socket loses nothing
                idle = state["current"] is None and all(u.done for u in inflight)
                if ws is not None and (time.monotonic() - state["opened"] < self.cfg.rotate_after_s or not idle):
                    return ws
                if ws is not None:  # rotate the 60-minute session between utterances
                    await close_ws()
                delay = 1.0
                while True:
                    try:
                        ws = await self._open()
                        break
                    except Exception as exc:
                        status = getattr(getattr(exc, "response", None), "status_code", None)
                        if status in (401, 403):
                            raise TranscriptionError(f"OpenAI realtime auth failed (HTTP {status})", fatal=True) from exc
                        log.warning("%s: realtime connect failed (%r); retrying in %.0fs", source, exc, delay)
                        await asyncio.sleep(delay)
                        delay = min(delay * 2, 30.0)
                state["ws"], state["opened"] = ws, time.monotonic()
                state["rx"] = asyncio.ensure_future(receiver(ws))
                awaiting_ack.clear()
                # re-send whatever the previous socket lost: committed utterances still
                # waiting for a transcript, then the utterance currently being spoken
                for u in inflight:
                    if u.committed and not u.done:
                        self.reconnects += 1
                        if u.item_id:  # it gets a new item (and new live deltas) on this socket
                            by_item.pop(u.item_id, None)
                            u.item_id = None
                        u.partial = ""
                        await self._send_audio(ws, u.chunks)
                        await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                        u.committed_at = time.monotonic()
                        awaiting_ack.append(u)
                        self._bill(u.audio_seconds)  # committed (and billed) once more
                cur = state["current"]
                if cur is not None and cur.chunks:
                    await self._send_audio(ws, cur.chunks)
                return ws

        async def close_ws() -> None:
            ws, rx = state["ws"], state["rx"]
            state["ws"] = None
            if ws is not None:
                with contextlib.suppress(Exception):
                    await ws.close()
            if rx is not None and rx is not asyncio.current_task():
                rx.cancel()
                with contextlib.suppress(BaseException):
                    await rx

        async def send(payload: dict[str, Any]) -> None:
            for attempt in range(3):
                ws = await ensure_connected()
                try:
                    await ws.send(json.dumps(payload))
                    return
                except Exception as exc:
                    log.info("%s: send failed (%r), reconnecting", source, exc)
                    if state["ws"] is ws:
                        state["ws"] = None
                    if attempt == 2:
                        raise

        async def on_event(ev: Any) -> None:
            if ev.kind == "start":
                # the one safe point to rotate a due session: before this utterance is registered,
                # so it isn't split across sockets and (when idle) no pending result is lost
                await ensure_connected()
                u = _Utt(seq=next(seq), t_start=ev.t)
                state["current"] = u
                inflight.append(u)
                chunk = to24k(ev.samples)
                await self._send_audio_payload(send, chunk)  # send before recording, so a
                u.chunks.append(chunk)  # reconnect inside send() doesn't duplicate it
            elif ev.kind == "audio" and state["current"] is not None:
                chunk = to24k(ev.samples)
                await self._send_audio_payload(send, chunk)
                state["current"].chunks.append(chunk)
            elif ev.kind == "end" and state["current"] is not None:
                u = state["current"]
                u.t_end = ev.t
                u.speech_seconds = ev.speech_seconds
                if ev.speech_seconds < self.vad_cfg.min_speech_s or u.audio_seconds < 0.15:
                    await send({"type": "input_audio_buffer.clear"})
                    state["current"] = None
                    u.done = True
                    u.text = ""
                    release_ready()
                    return
                await send({"type": "input_audio_buffer.commit"})
                state["current"] = None
                u.committed = True
                u.committed_at = time.monotonic()
                awaiting_ack.append(u)
                self._bill(u.audio_seconds)

        async def feed() -> None:
            try:
                async for frame in frames:
                    for ev in gate.push(frame):
                        await on_event(ev)
                for ev in gate.flush():
                    await on_event(ev)
                # wait for outstanding transcripts
                deadline = time.monotonic() + self.result_timeout_s
                while inflight and time.monotonic() < deadline:
                    if state["ws"] is None and any(u.committed and not u.done for u in inflight):
                        await ensure_connected()  # socket died: re-send pending utterances
                    await asyncio.sleep(0.05)
                    release_ready()
                if inflight:
                    log.warning("%s: %d utterance(s) got no transcript in time", source, len(inflight))
            except Exception as exc:
                state["failed"] = exc
            finally:
                if state["reconnect"] is not None:
                    state["reconnect"].cancel()  # don't let a late reconnect reopen what we close
                await close_ws()
                out.put_nowait(None)

        async def watchdog() -> None:
            while True:
                await asyncio.sleep(1.0)
                release_ready()

        dog = asyncio.ensure_future(watchdog())
        feeder = asyncio.ensure_future(feed())
        try:
            while True:
                seg = await out.get()
                if seg is None:
                    break
                yield seg
            if state["failed"] is not None:
                raise state["failed"]
        finally:
            dog.cancel()
            if not feeder.done():
                feeder.cancel()
                with contextlib.suppress(BaseException):
                    await feeder

    async def _send_audio_payload(self, send: Callable[[dict[str, Any]], Any], chunk: bytes) -> None:
        await send({"type": "input_audio_buffer.append", "audio": base64.b64encode(chunk).decode("ascii")})

    async def _send_audio(self, ws: Any, chunks: list[bytes]) -> None:
        for chunk in chunks:
            await ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": base64.b64encode(chunk).decode("ascii")}))

    def _handle(
        self,
        ev: dict[str, Any],
        awaiting_ack: deque[_Utt],
        by_item: dict[str, _Utt],
        source: str,
        speaker: str,
    ) -> None:
        t = ev.get("type", "")
        if t == "input_audio_buffer.committed":
            item_id = ev.get("item_id")
            if awaiting_ack and item_id:
                u = awaiting_ack.popleft()
                u.item_id = item_id
                by_item[item_id] = u
        elif t == "conversation.item.input_audio_transcription.delta":
            u = by_item.get(ev.get("item_id", ""))
            if u is not None:
                u.partial += ev.get("delta") or ""
                if self.on_partial:
                    with contextlib.suppress(Exception):
                        self.on_partial(source, speaker, u.partial)
        elif t == "conversation.item.input_audio_transcription.completed":
            u = by_item.get(ev.get("item_id", ""))
            if u is not None:
                u.text = ev.get("transcript") or ""
                u.done = True
        elif t == "conversation.item.input_audio_transcription.failed":
            u = by_item.get(ev.get("item_id", ""))
            if u is not None:
                log.warning("%s: transcription failed: %s", source, ev.get("error"))
                u.text = ""
                u.done = True
        elif t == "error":
            err = ev.get("error") or {}
            code = err.get("code")
            if code == "input_audio_buffer_commit_empty":
                # the commit we were waiting on won't be acknowledged
                if awaiting_ack:
                    u = awaiting_ack.popleft()
                    u.text, u.done = "", True
                return
            log.warning("%s: realtime error %s/%s: %s", source, err.get("type"), code, err.get("message"))
