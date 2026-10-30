"""Telling the people on the meeting audio apart, on this computer: "Speaker 1", "Speaker 2"… then their names.

Everyone heard through the computer's audio arrives on one channel, ``Others``. Each of those lines is
turned into a *voice embedding* — 256 numbers that are close for two lines of the same voice and far for
two different voices — by a small speaker-recognition model (WeSpeaker's ResNet34, trained on VoxCeleb,
26 MB, run with ONNX Runtime). The lines are then grouped as they come (:class:`VoiceClusters`): a line
close enough to a voice already heard is that voice, else it is a new one. Nothing leaves the computer,
whichever speech-to-text backend runs.

::

    meeting audio ─ utterance ─▶ fbank (80 mel) ─▶ ResNet34 ─▶ embedding ─▶ nearest voice ≥ threshold?
                                                                              yes: that voice (its centroid moves)
                                                                              no:  a new voice, "Speaker N"

Only a meeting's own voices are kept, and only while it lasts: the embeddings are dropped with the
meeting, never written to disk (a stored voiceprint that identifies someone is biometric data). What a
meeting keeps is each line's voice number and the names given to the voices (:attr:`Meeting.voices`).

Limits, by design: a line shorter than :data:`MIN_SPEECH_S` has too little voice to tell (it gets the
voice of the line before it when that one was close in time, else none); two people talking over each
other are one line, given to whichever voice dominates; the same person through a different microphone
mid-meeting may become a second voice — you can give both the same name.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

import httpx
import numpy as np

from .config import Config

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
MODEL_NAME = "wespeaker_en_voxceleb_resnet34_LM.onnx"
MODEL_URL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/{MODEL_NAME}"
MODEL_SHA256 = "e9848563da86f263117134dfd7ad63c92355b37de492b55e325400c9d9c39012"
MODEL_SIZE = 26_530_550

MIN_SPEECH_S = 1.0
"""Shorter lines carry too little voice to place on their own."""
MAX_SPEECH_S = 20.0
"""Longer lines are embedded from their first this-many seconds: enough for the voice, and bounded work."""
SAME_VOICE = 0.4
"""Cosine similarity from which a line is a voice already heard. Measured on real recordings cut into 1.5–4 s lines:
the same person scores 0.5–0.75, two people below 0.25; at telephone quality 0.4 still puts 97 % of lines on the
right voice (0.35: 90 %). Above it, one person may now and then become two voices — fixed by giving both one name;
below it, two people would share a voice, which no name can undo."""
MAX_VOICES = 12
"""A meeting never has more voices than this: past it, a line goes to the nearest one."""
CARRY_OVER_S = 3.0
"""A line too short to place, starting within this long after the previous meeting-audio line, is that voice."""


# ------------------------------------------------------------------ features


@lru_cache(maxsize=4)
def _mel_banks(num_bins: int, fft: int, rate: int, low: float, high: float) -> np.ndarray:
    """Kaldi's triangular mel filters (``mel = 1127 ln(1 + f/700)``), over the ``fft // 2`` first bins."""

    def mel(f: np.ndarray | float) -> np.ndarray:
        return 1127.0 * np.log(1.0 + np.asarray(f, dtype=np.float64) / 700.0)

    half = fft // 2
    bin_mel = mel(np.arange(half) * rate / fft)
    lo, hi = float(mel(low)), float(mel(high))
    delta = (hi - lo) / (num_bins + 1)
    banks = np.zeros((num_bins, half), dtype=np.float64)
    for b in range(num_bins):
        left, center, right = lo + b * delta, lo + (b + 1) * delta, lo + (b + 2) * delta
        up = (bin_mel - left) / (center - left)
        down = (right - bin_mel) / (right - center)
        banks[b] = np.where((bin_mel > left) & (bin_mel < right), np.minimum(up, down), 0.0)
    return banks.astype(np.float32)


def fbank(samples: np.ndarray, num_bins: int = 80) -> np.ndarray:
    """Kaldi-compatible log mel filterbank (25 ms frames every 10 ms, Hamming window, pre-emphasis 0.97, no
    dither, no energy), as WeSpeaker computes it for its models; samples on the int16 scale. ``(frames, bins)``."""
    x = np.asarray(samples, dtype=np.float32).reshape(-1)
    length, shift, fft = 400, 160, 512
    if x.size < length:
        return np.zeros((0, num_bins), dtype=np.float32)
    n = 1 + (x.size - length) // shift
    idx = np.arange(length)[None, :] + shift * np.arange(n)[:, None]
    frames = x[idx]
    frames = frames - frames.mean(axis=1, keepdims=True)
    frames = np.concatenate([frames[:, :1] * (1 - 0.97), frames[:, 1:] - 0.97 * frames[:, :-1]], axis=1)
    frames = frames * np.hamming(length).astype(np.float32)
    power = np.abs(np.fft.rfft(frames, n=fft, axis=1)[:, : fft // 2]) ** 2
    energies = power.astype(np.float32) @ _mel_banks(num_bins, fft, SAMPLE_RATE, 20.0, SAMPLE_RATE / 2).T
    return np.log(np.maximum(energies, np.finfo(np.float32).eps)).astype(np.float32)


# --------------------------------------------------------------------- model


class Embedder(Protocol):
    def embed(self, samples: np.ndarray) -> np.ndarray | None:
        """A unit-length voice embedding of ``samples`` (int16, 16 kHz), ``None`` when there is too little."""


class OnnxEmbedder:
    """The WeSpeaker model, run by ONNX Runtime on the CPU (a fraction of a second per line, off the loop)."""

    def __init__(self, path: Path) -> None:
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2  # beside speech-to-text, which wants the cores more
        opts.log_severity_level = 3
        self._session = ort.InferenceSession(str(path), sess_options=opts, providers=["CPUExecutionProvider"])
        self._input = self._session.get_inputs()[0].name

    def embed(self, samples: np.ndarray) -> np.ndarray | None:
        feats = fbank(samples[: int(MAX_SPEECH_S * SAMPLE_RATE)])
        if feats.shape[0] < int(MIN_SPEECH_S * 100):
            return None
        feats = feats - feats.mean(axis=0, keepdims=True)  # per-line mean normalisation, as in training
        emb = self._session.run(None, {self._input: feats[None, :, :]})[0][0].astype(np.float32)
        norm = float(np.linalg.norm(emb))
        return emb / norm if norm > 0 else None


def model_path(cfg: Config) -> Path:
    from .stt_models import models_dir

    return models_dir(cfg) / MODEL_NAME


def is_installed(cfg: Config) -> bool:
    p = model_path(cfg)
    with contextlib.suppress(OSError):
        return p.stat().st_size == MODEL_SIZE
    return False


def available() -> bool:
    """ONNX Runtime is installed (``pip install 'voicestream[voices]'``; the Mac app has it)."""
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return False
    return True


async def download(cfg: Config, client: httpx.AsyncClient | None = None) -> Path:
    """Fetch the model once (resuming a partial file), check its SHA-256 and move it into place."""
    from .stt_models import DownloadError, _fetch, _why

    dest = model_path(cfg)
    if is_installed(cfg):
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    own = client is None
    client = client or httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(30.0, read=60.0))
    try:
        await _fetch(client, MODEL_URL, part, None)
    except (httpx.TransportError, httpx.HTTPStatusError) as exc:
        raise DownloadError(f"could not download the voice model: {_why(exc)}") from exc
    finally:
        if own:
            await client.aclose()
    digest = await asyncio.to_thread(lambda: hashlib.sha256(part.read_bytes()).hexdigest())
    if digest != MODEL_SHA256:
        with contextlib.suppress(OSError):
            part.unlink()
        raise DownloadError("the voice model is damaged (checksum mismatch); it is downloaded again next time")
    os.replace(part, dest)
    log.info("voice model ready at %s", dest)
    return dest


# ---------------------------------------------------------------- clustering


@dataclass
class _Voice:
    number: int
    centroid: np.ndarray
    count: int = 1


@dataclass
class VoiceClusters:
    """One meeting's voices, grouped as lines come in. Lives in memory only."""

    threshold: float = SAME_VOICE
    voices: list[_Voice] = field(default_factory=list)
    _last: tuple[int, float] | None = None
    """(voice, end time) of the last line placed."""

    def assign(self, emb: np.ndarray | None, t_start: float, t_end: float) -> int | None:
        """The voice number (1, 2…) of a line with this embedding, ``None`` when it can't be told."""
        if emb is None:
            last = self._last
            if last is not None and 0 <= t_start - last[1] <= CARRY_OVER_S:
                self._last = (last[0], t_end)
                return last[0]
            return None
        best, score = None, -1.0
        for v in self.voices:
            s = float(np.dot(emb, v.centroid / max(float(np.linalg.norm(v.centroid)), 1e-9)))
            if s > score:
                best, score = v, s
        if best is None or (score < self.threshold and len(self.voices) < MAX_VOICES):
            best = _Voice(len(self.voices) + 1, emb.copy())
            self.voices.append(best)
        else:
            best.centroid = best.centroid + (emb - best.centroid) / (best.count + 1)  # running mean
            best.count += 1
        self._last = (best.number, t_end)
        return best.number


@dataclass
class AudioRing:
    """The last ``seconds`` of one source's audio, on the capture clock: what a line just transcribed said."""

    seconds: float = 60.0
    _chunks: list[tuple[float, np.ndarray]] = field(default_factory=list)

    def add(self, t_start: float, samples: np.ndarray) -> None:
        self._chunks.append((t_start, samples))
        horizon = t_start - self.seconds
        while self._chunks and self._chunks[0][0] + self._chunks[0][1].size / SAMPLE_RATE < horizon:
            self._chunks.pop(0)

    def clip(self, t_start: float, t_end: float) -> np.ndarray:
        parts: list[np.ndarray] = []
        for t0, s in self._chunks:
            t1 = t0 + s.size / SAMPLE_RATE
            if t1 <= t_start or t0 >= t_end:
                continue
            a = max(0, int(round((t_start - t0) * SAMPLE_RATE)))
            b = min(s.size, int(round((t_end - t0) * SAMPLE_RATE)))
            parts.append(s[a:b])
        return np.concatenate(parts) if parts else np.zeros(0, dtype=np.int16)


def make_embedder(cfg: Config) -> Any | None:
    """The embedder when the model and ONNX Runtime are there, else ``None`` (and why, logged once)."""
    if not available():
        log.info("voices: ONNX Runtime is not installed; meeting audio stays one voice (pip install 'voicestream[voices]')")
        return None
    if not is_installed(cfg):
        return None
    try:
        return OnnxEmbedder(model_path(cfg))
    except Exception as exc:  # noqa: BLE001 - a damaged file, an unsupported CPU: one voice, said in the log
        log.info("voices: could not load the voice model (%s); meeting audio stays one voice", exc)
        return None
