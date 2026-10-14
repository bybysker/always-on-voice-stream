"""Voice activity detection: cuts a continuous frame stream into utterances.

Only speech is sent to (paid) transcription, which is most of why an
always-on stream costs cents rather than dollars. The default engine is an
energy detector with an adaptive noise floor (numpy only); ``webrtc`` uses
``webrtcvad`` when installed for better robustness to steady noise.
"""

from __future__ import annotations

import collections
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Literal

import numpy as np

from ..config import VADConfig
from ..models import SAMPLE_RATE, AudioFrame, Utterance
from .base import rms_dbfs

log = logging.getLogger(__name__)


class VADSegmenter:
    """Synchronous, push-based segmenter (easy to test, cheap to run in the loop)."""

    def __init__(
        self,
        source: str,
        cfg: VADConfig | None = None,
        on_level: Callable[[float, bool], None] | None = None,
    ) -> None:
        self.source = source
        self.cfg = cfg or VADConfig()
        self.frame_len = int(SAMPLE_RATE * self.cfg.frame_ms / 1000)
        self.frame_s = self.frame_len / SAMPLE_RATE
        self.on_level = on_level
        self._buf = np.zeros(0, dtype=np.int16)
        self._buf_t: float | None = None
        # Noise floor = 10th percentile of the last ~12 s of frame energies
        # ("minimum statistics"). Natural speech has enough pauses that the low
        # percentile tracks the background, while steady noise (fans, hum) is
        # absorbed into the floor within a few seconds.
        self._history: collections.deque[float] = collections.deque(
            maxlen=max(10, int(12.0 / self.frame_s))
        )
        self.noise_db = -60.0
        self._pre_roll: collections.deque[tuple[float, np.ndarray, float]] = collections.deque(
            maxlen=max(1, int(self.cfg.pre_roll_s / self.frame_s))
        )
        self._candidate: list[tuple[float, np.ndarray, float]] = []
        self._active: list[tuple[float, np.ndarray, float]] = []  # (t, samples, db)
        self._speech_frames = 0
        self._silence_run = 0
        self._held: Utterance | None = None
        self._webrtc = None
        if self.cfg.engine == "webrtc":
            try:
                import webrtcvad  # type: ignore[import-not-found]

                self._webrtc = webrtcvad.Vad(self.cfg.webrtc_aggressiveness)
            except ImportError:
                log.warning("webrtcvad not installed; falling back to the energy VAD")

    # ------------------------------------------------------------------ public

    @property
    def in_speech(self) -> bool:
        return bool(self._active)

    def push(self, frame: AudioFrame) -> list[Utterance]:
        out: list[Utterance] = []
        if self._buf_t is None or self._buf.size == 0:
            self._buf_t = frame.t_start
        elif frame.t_start - (self._buf_t + self._buf.size / SAMPLE_RATE) > 0.5:
            # discontinuity (device hiccup / dropped frames): restart timing
            self._buf = np.zeros(0, dtype=np.int16)
            self._buf_t = frame.t_start
        self._buf = np.concatenate([self._buf, frame.samples.astype(np.int16, copy=False)])
        while self._buf.size >= self.frame_len:
            chunk = self._buf[: self.frame_len]
            self._buf = self._buf[self.frame_len :]
            t = self._buf_t
            self._buf_t = t + self.frame_s
            out.extend(self._process_frame(t, chunk))
        out.extend(self._release_held(self._buf_t))
        return out

    def flush(self) -> list[Utterance]:
        """End of stream: close any open utterance and release held ones."""
        out: list[Utterance] = []
        if self._active:
            utt = self._close_active(trim=True)
            if utt:
                out.extend(self._hold_or_emit(utt))
        if self._held is not None:
            out.append(self._held)
            self._held = None
        self._candidate.clear()
        return out

    # ----------------------------------------------------------------- internals

    def _is_speech(self, chunk: np.ndarray, db: float) -> bool:
        threshold = max(self.cfg.min_threshold_db, self.noise_db + self.cfg.margin_db)
        if db < self.cfg.min_threshold_db:
            return False
        if self._webrtc is not None:
            try:
                return bool(self._webrtc.is_speech(chunk.tobytes(), SAMPLE_RATE)) and db > (
                    self.noise_db + self.cfg.margin_db / 2
                )
            except Exception:  # bad frame size etc.
                pass
        return db > threshold

    def _update_noise(self, db: float) -> None:
        self._history.append(db)
        floor = float(np.percentile(np.fromiter(self._history, dtype=np.float64), 10))
        self.noise_db = float(np.clip(floor + 1.0, -90.0, -20.0))

    def classify(self, chunk: np.ndarray) -> tuple[float, bool]:
        """Update the noise floor with one frame and classify it: (dBFS, is_speech)."""
        db = rms_dbfs(chunk)
        self._update_noise(db)
        speech = self._is_speech(chunk, db)
        if self.on_level:
            self.on_level(db, speech)
        return db, speech

    def _process_frame(self, t: float, chunk: np.ndarray) -> list[Utterance]:
        db, speech = self.classify(chunk)
        out: list[Utterance] = []

        if not self._active:
            if speech:
                self._candidate.append((t, chunk, db))
                if len(self._candidate) >= self.cfg.start_frames:
                    self._active = list(self._pre_roll) + self._candidate
                    self._pre_roll.clear()
                    self._speech_frames = len(self._candidate)
                    self._silence_run = 0
                    self._candidate = []
            else:
                # failed start: candidate frames become pre-roll
                for item in self._candidate:
                    self._pre_roll.append(item)
                self._candidate = []
                self._pre_roll.append((t, chunk, db))
            return out

        self._active.append((t, chunk, db))
        if speech:
            self._speech_frames += 1
            self._silence_run = 0
        else:
            self._silence_run += 1

        if self._silence_run * self.frame_s >= self.cfg.hangover_s:
            utt = self._close_active(trim=True)
            if utt:
                out.extend(self._hold_or_emit(utt))
        elif len(self._active) * self.frame_s >= self.cfg.max_utterance_s:
            out.extend(self._split_long())
        return out

    def _split_long(self) -> list[Utterance]:
        """Utterance hit max length: cut at the quietest frame in the last ~3 s."""
        look = min(len(self._active) - 1, int(3.0 / self.frame_s))
        tail = self._active[-look:]
        idx = len(self._active) - look + int(np.argmin([db for _, _, db in tail]))
        head, rest = self._active[:idx], self._active[idx:]
        self._active = head
        utt = self._close_active(trim=False)
        self._active = rest
        self._speech_frames = len(rest)
        self._silence_run = 0
        return self._hold_or_emit(utt) if utt else []

    def _close_active(self, trim: bool) -> Utterance | None:
        frames = self._active
        speech_frames = self._speech_frames
        self._active = []
        self._speech_frames = 0
        self._silence_run = 0
        if trim:
            keep_tail = max(1, int(0.2 / self.frame_s))
            # drop trailing silence beyond a short tail
            silent = 0
            for _, _, db in reversed(frames):
                if db > max(self.cfg.min_threshold_db, self.noise_db + self.cfg.margin_db):
                    break
                silent += 1
            cut = max(0, silent - keep_tail)
            if cut:
                frames = frames[:-cut]
        if not frames or speech_frames * self.frame_s < self.cfg.min_speech_s:
            return None
        samples = np.concatenate([c for _, c, _ in frames])
        t0 = frames[0][0]
        return Utterance(
            source=self.source,
            samples=samples,
            t_start=t0,
            t_end=t0 + samples.size / SAMPLE_RATE,
            speech_seconds=speech_frames * self.frame_s,
        )

    def _hold_or_emit(self, utt: Utterance) -> list[Utterance]:
        out: list[Utterance] = []
        if self._held is not None:
            gap = utt.t_start - self._held.t_end
            if gap <= self.cfg.merge_gap_s:
                utt = _merge(self._held, utt)
            else:
                out.append(self._held)
            self._held = None
        if utt.duration < self.cfg.min_chunk_s:
            self._held = utt
        else:
            out.append(utt)
        return out

    def _release_held(self, now: float | None) -> list[Utterance]:
        if self._held is None or now is None or self._active or self._candidate:
            return []
        if now - self._held.t_end > self.cfg.merge_gap_s:
            held, self._held = self._held, None
            return [held]
        return []


@dataclass(slots=True)
class GateEvent:
    """``start`` (with pre-roll audio), ``audio`` (one frame), or ``end`` of a speech run."""

    kind: Literal["start", "audio", "end"]
    t: float
    samples: np.ndarray | None = None
    t_start: float = 0.0
    speech_seconds: float = 0.0


class SpeechGate:
    """Frame-level speech gate for *streaming* backends (OpenAI Realtime, Deepgram).

    Instead of whole utterances it reports when speech starts (with pre-roll),
    every frame while it continues, and when it ends — so audio can be streamed
    as it is spoken while silence never leaves the machine.
    """

    def __init__(
        self,
        source: str,
        cfg: VADConfig | None = None,
        on_level: Callable[[float, bool], None] | None = None,
    ) -> None:
        self.cfg = cfg or VADConfig()
        self._clf = VADSegmenter(source, self.cfg, on_level=on_level)
        self.frame_len = self._clf.frame_len
        self.frame_s = self._clf.frame_s
        self._buf = np.zeros(0, dtype=np.int16)
        self._buf_t: float | None = None
        self._pre: collections.deque[tuple[float, np.ndarray]] = collections.deque(
            maxlen=max(1, int(self.cfg.pre_roll_s / self.frame_s))
        )
        self._cand: list[tuple[float, np.ndarray]] = []
        self.active = False
        self._t_start = 0.0
        self._dur = 0.0
        self._speech = 0
        self._silence = 0

    @property
    def noise_db(self) -> float:
        return self._clf.noise_db

    def push(self, frame: AudioFrame) -> list[GateEvent]:
        if self._buf_t is None or self._buf.size == 0:
            self._buf_t = frame.t_start
        self._buf = np.concatenate([self._buf, frame.samples.astype(np.int16, copy=False)])
        out: list[GateEvent] = []
        while self._buf.size >= self.frame_len:
            chunk = self._buf[: self.frame_len]
            self._buf = self._buf[self.frame_len :]
            t = self._buf_t
            self._buf_t = t + self.frame_s
            out.extend(self._frame(t, chunk))
        return out

    def flush(self) -> list[GateEvent]:
        return [self._end(self._buf_t or 0.0)] if self.active else []

    def _frame(self, t: float, chunk: np.ndarray) -> list[GateEvent]:
        _, speech = self._clf.classify(chunk)
        if not self.active:
            if speech:
                self._cand.append((t, chunk))
                if len(self._cand) >= self.cfg.start_frames:
                    frames = list(self._pre) + self._cand
                    self._pre.clear()
                    self._cand = []
                    self.active = True
                    self._t_start = frames[0][0]
                    self._dur = len(frames) * self.frame_s
                    self._speech = self.cfg.start_frames
                    self._silence = 0
                    return [GateEvent("start", self._t_start, np.concatenate([c for _, c in frames]))]
            else:
                self._pre.extend(self._cand)
                self._cand = []
                self._pre.append((t, chunk))
            return []
        self._dur += self.frame_s
        if speech:
            self._speech += 1
            self._silence = 0
        else:
            self._silence += 1
        events = [GateEvent("audio", t, chunk)]
        if self._silence * self.frame_s >= self.cfg.hangover_s or self._dur >= self.cfg.max_utterance_s:
            events.append(self._end(t + self.frame_s))
        return events

    def _end(self, t: float) -> GateEvent:
        ev = GateEvent("end", t, t_start=self._t_start, speech_seconds=self._speech * self.frame_s)
        self.active = False
        self._dur = 0.0
        self._speech = 0
        self._silence = 0
        return ev


def _merge(a: Utterance, b: Utterance) -> Utterance:
    gap = np.zeros(int(0.15 * SAMPLE_RATE), dtype=np.int16)
    samples = np.concatenate([a.samples, gap, b.samples])
    return Utterance(
        source=a.source,
        samples=samples,
        t_start=a.t_start,
        t_end=b.t_end,
        speech_seconds=a.speech_seconds + b.speech_seconds,
    )


async def segment(
    frames: AsyncIterator[AudioFrame],
    source: str,
    cfg: VADConfig | None = None,
    on_level: Callable[[float, bool], None] | None = None,
) -> AsyncIterator[Utterance]:
    """Async adapter: frames in, utterances out (flushes at end of stream)."""
    seg = VADSegmenter(source, cfg, on_level)
    async for frame in frames:
        for utt in seg.push(frame):
            yield utt
    for utt in seg.flush():
        yield utt
