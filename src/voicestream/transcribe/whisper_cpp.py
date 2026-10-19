"""Free, private transcription on this computer with whisper.cpp (ggml-org, MIT).

voicestream runs whisper.cpp's ``whisper-server`` as a child process (one per model,
shared by the mic and system-audio pipelines, so the model is loaded once) and sends
it each VAD utterance as a small WAV. On Apple Silicon the model runs on the GPU
(Metal); ``large-v3-turbo-q5_0`` transcribes French and English with punctuation.

Behaviours of whisper-server this module works around (checked against its source and
by running it, v1.9.5):

* the port opens only once the model is loaded, and ``/health`` is the only reliable
  readiness signal (the "listening" log line is block-buffered);
* it sets ``SO_REUSEPORT`` and ``Access-Control-Allow-Origin: *``, so a stale server on the
  same port or any web page could answer or call it: every route gets a random secret
  prefix (``--request-path``) and anything that answers 404 there isn't ours;
* bad flags or a bad language exit with status 0, so any exit is a failure;
* inference is serialized by one lock inside the server; we queue on our side (FIFO);
* ``/load`` can crash it, so a model change restarts the process instead.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import io
import logging
import math
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import wave
from collections import deque
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

from .. import stt_models
from ..config import Config, VADConfig, WhisperCppConfig
from ..models import SAMPLE_RATE, AudioFrame, TranscriptSegment, Utterance
from .base import ChunkTranscriber, TranscribeResult, TranscriptionError, UsageCallback

log = logging.getLogger(__name__)

# whisper.cpp reports the language by name (verbose_json "language": "french")
_LANG = (
    "en english zh chinese de german es spanish ru russian ko korean fr french ja japanese pt portuguese "
    "tr turkish pl polish ca catalan nl dutch ar arabic sv swedish it italian id indonesian hi hindi "
    "fi finnish vi vietnamese he hebrew uk ukrainian el greek ms malay cs czech ro romanian da danish "
    "hu hungarian ta tamil no norwegian th thai ur urdu hr croatian bg bulgarian lt lithuanian la latin "
    "mi maori ml malayalam cy welsh sk slovak te telugu fa persian lv latvian bn bengali sr serbian "
    "az azerbaijani sl slovenian kn kannada et estonian mk macedonian br breton eu basque is icelandic "
    "hy armenian ne nepali mn mongolian bs bosnian kk kazakh sq albanian sw swahili gl galician "
    "mr marathi pa punjabi si sinhala km khmer sn shona yo yoruba so somali af afrikaans oc occitan "
    "ka georgian be belarusian tg tajik sd sindhi gu gujarati am amharic yi yiddish lo lao uz uzbek "
    "fo faroese ht haitian_creole ps pashto tk turkmen nn nynorsk mt maltese sa sanskrit lb luxembourgish "
    "my myanmar bo tibetan tl tagalog mg malagasy as assamese tt tatar haw hawaiian ln lingala ha hausa "
    "ba bashkir jw javanese su sundanese yue cantonese"
).split()
CODES: dict[str, str] = {_LANG[i]: _LANG[i + 1].replace("_", " ") for i in range(0, len(_LANG), 2)}
"""ISO-639-1 code -> whisper.cpp language name (all 100 it knows)."""
NAMES: dict[str, str] = {name: code for code, name in CODES.items()}

HOMEBREW_PATHS = ("/opt/homebrew/bin/whisper-server", "/usr/local/bin/whisper-server")
BINARY_ENV = "VOICESTREAM_WHISPER_SERVER"
LOG_MAX_BYTES = 5_000_000

_children: set[int] = set()


@atexit.register
def _stop_children() -> None:
    """Don't leave a server holding a gigabyte of model in memory after we exit."""
    for pid in list(_children):
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)


def find_binary(cfg: WhisperCppConfig) -> Path | None:
    """``whisper-server``: the configured path, the copy bundled in the app, PATH, then Homebrew."""
    candidates: list[str | None] = [cfg.binary, os.environ.get(BINARY_ENV)]
    if getattr(sys, "frozen", False):  # inside the packaged app (PyInstaller)
        exe = Path(sys.executable).resolve()
        bundle = getattr(sys, "_MEIPASS", None)
        candidates += [
            str(Path(bundle) / "bin" / "whisper-server") if bundle else None,
            str(exe.parent.parent / "Resources" / "bin" / "whisper-server"),  # Voicestream.app/Contents/Resources
            str(exe.parent / "bin" / "whisper-server"),
        ]
    candidates += [shutil.which("whisper-server"), *HOMEBREW_PATHS]
    for c in candidates:
        if not c:
            continue
        p = Path(c).expanduser()
        if p.is_file() and (os.access(p, os.X_OK) or p.suffix == ".py"):
            return p
    return None


def default_threads() -> int:
    if sys.platform == "darwin":
        with contextlib.suppress(OSError, ValueError, subprocess.SubprocessError):
            out = subprocess.run(
                ["/usr/sbin/sysctl", "-n", "hw.perflevel0.physicalcpu"], capture_output=True, text=True, timeout=5
            ).stdout
            return max(1, min(8, int(out.strip())))
    return max(1, min(4, (os.cpu_count() or 2) // 2 or 1))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wav_bytes(samples: Any) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(samples.astype("<i2").tobytes())
    return buf.getvalue()


class WhisperServer:
    """One ``whisper-server`` child process, started on demand and restarted if it dies.

    Shared through :meth:`acquire`/:meth:`release`: every transcriber using the same
    binary, model and options talks to the same process.
    """

    _shared: dict[tuple[Any, ...], WhisperServer] = {}

    def __init__(self, binary: Path, model: Path, cfg: WhisperCppConfig, *, vad_model: Path | None,
                 language: str | None, run_dir: Path) -> None:
        self.binary = binary
        self.model = model
        self.cfg = cfg
        self.vad_model = vad_model
        self.language = language or "auto"
        self.run_dir = run_dir
        self.token = secrets.token_hex(8)
        self.port = 0
        self.proc: asyncio.subprocess.Process | None = None
        self.users = 0
        self._start_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()  # FIFO: the server serializes anyway, in no particular order
        self._client: httpx.AsyncClient | None = None
        self._crashes: deque[float] = deque(maxlen=3)
        self._log_path = run_dir / "whisper-server.log"
        self._log_file: Any = None
        self.ready = asyncio.Event()

    # -------------------------------------------------------------- sharing

    @classmethod
    def acquire(cls, cfg: Config) -> WhisperServer:
        wc = cfg.transcription.whisper_cpp
        binary = find_binary(wc)
        if binary is None:
            raise TranscriptionError(
                "whisper.cpp isn't installed: use the Voicestream app, or `brew install whisper-cpp` "
                "(or set transcription.whisper_cpp.binary to a whisper-server)",
                fatal=True,
            )
        model = stt_models.model_path(cfg, wc.model)
        if not stt_models.is_installed(cfg, wc.model) and not model.is_file():
            raise TranscriptionError(
                f"the speech model {wc.model!r} isn't downloaded yet: run `voicestream models download {wc.model}` "
                "or download it in the app (Settings → Speech-to-text)",
                fatal=True,
            )
        vad = stt_models.model_path(cfg, stt_models.VAD_MODEL) if wc.vad else None
        if vad is not None and not stt_models.is_installed(cfg, stt_models.VAD_MODEL):
            log.warning("whisper.cpp voice detection is off: run `voicestream models download %s`", stt_models.VAD_MODEL)
            vad = None
        language = cfg.transcription.language
        if language and language not in CODES:
            raise TranscriptionError(f"whisper.cpp doesn't know the language {language!r}", fatal=True)
        key = (str(binary), str(model), str(vad), language, wc.threads, wc.gpu, wc.no_speech_threshold)
        server = cls._shared.get(key)
        if server is None or server.users == 0 and server.proc is None:
            server = cls(binary, model, wc, vad_model=vad, language=language, run_dir=cfg.data_path / "run")
            cls._shared[key] = server
        server.users += 1
        return server

    async def release(self) -> None:
        self.users = max(0, self.users - 1)
        if self.users == 0:
            await self.stop()
            for k, v in list(self._shared.items()):
                if v is self:
                    del self._shared[k]

    # ------------------------------------------------------------- process

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/{self.token}"

    def argv(self) -> list[str]:
        threads = self.cfg.threads or default_threads()
        exe = [sys.executable, str(self.binary)] if self.binary.suffix == ".py" else [str(self.binary)]  # a Python shim
        args = [
            *exe, "-m", str(self.model), "--host", "127.0.0.1", "--port", str(self.port),
            "--request-path", f"/{self.token}", "-l", self.language, "-t", str(threads),
            "-nlp", "-sns", "-nth", f"{self.cfg.no_speech_threshold:g}",
        ]  # fmt: skip
        if self.vad_model is not None:
            args += ["--vad", "-vm", str(self.vad_model)]
        if not self.cfg.gpu:
            args.append("-ng")
        return args

    def _pid_file(self) -> Path:
        return self.run_dir / f"whisper-server-{self.model.stem}.pid"

    def _kill_orphan(self) -> None:
        """A whisper-server left behind by a crashed run would hold the model in memory (and could
        share our port, thanks to SO_REUSEPORT): stop it."""
        pid_file = self._pid_file()
        try:
            pid = int(pid_file.read_text().strip())
        except (OSError, ValueError):
            return
        with contextlib.suppress(OSError):
            pid_file.unlink()
        if os.name != "posix" or pid <= 1:
            return
        try:
            cmd = subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return
        if "whisper-server" in cmd:
            log.info("stopping a whisper-server left over from an earlier run (pid %d)", pid)
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGTERM)

    async def ensure_started(self) -> None:
        if self.proc is not None and self.proc.returncode is None and self.ready.is_set():
            return
        async with self._start_lock:
            if self.proc is not None and self.proc.returncode is None and self.ready.is_set():
                return
            await self._start()

    async def _start(self) -> None:
        if self.proc is not None:
            await self._terminate()
        now = time.monotonic()
        if len(self._crashes) == self._crashes.maxlen and now - self._crashes[0] < 120:
            raise TranscriptionError(f"whisper.cpp keeps stopping: {self.log_tail()}", fatal=True)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._kill_orphan()
        self.ready.clear()
        self.token = secrets.token_hex(8)
        self.port = _free_port()
        if self._log_file is None:
            with contextlib.suppress(OSError):  # it logs every request: keep a day or two, not months
                if self._log_path.stat().st_size > LOG_MAX_BYTES:
                    os.replace(self._log_path, self._log_path.with_name(self._log_path.name + ".1"))
            self._log_file = self._log_path.open("ab")
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "TMPDIR", "LANG", "SYSTEMROOT", "TEMP", "TMP")}
        log.info("starting whisper.cpp (%s) on port %d", self.model.name, self.port)
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv(), stdin=asyncio.subprocess.DEVNULL, stdout=self._log_file, stderr=self._log_file, env=env,
            start_new_session=os.name == "posix",  # Ctrl+C in a terminal goes to us; we stop it ourselves
        )
        _children.add(self.proc.pid)
        with contextlib.suppress(OSError):
            self._pid_file().write_text(str(self.proc.pid))
        self._client = self._client or httpx.AsyncClient(timeout=httpx.Timeout(self.cfg.timeout_s, connect=5.0))
        deadline = time.monotonic() + self.cfg.startup_timeout_s
        while True:
            if self.proc.returncode is not None:
                self._crashes.append(time.monotonic())
                raise TranscriptionError(
                    f"whisper.cpp stopped while loading {self.model.name} (exit {self.proc.returncode}): {self.log_tail()}",
                    retryable=True,
                )
            try:
                r = await self._client.get(f"{self.base_url}/health", timeout=2.0)
                if r.status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if time.monotonic() > deadline:
                await self._terminate()
                raise TranscriptionError(
                    f"whisper.cpp took more than {self.cfg.startup_timeout_s:g}s to load {self.model.name}", retryable=True
                )
            await asyncio.sleep(0.25)
        self.ready.set()
        log.info("whisper.cpp ready (%s, %d threads%s)", self.model.name, self.cfg.threads or default_threads(),
                 ", voice detection" if self.vad_model else "")

    def log_tail(self, lines: int = 6) -> str:
        try:
            with self._log_path.open("rb") as fh:
                fh.seek(max(0, self._log_path.stat().st_size - 4000))
                text = fh.read().decode("utf-8", "replace")
        except OSError:
            return "(no log)"
        useful = [ln.strip() for ln in text.splitlines() if ln.strip()]
        return " | ".join(useful[-lines:]) or "(empty log)"

    async def _terminate(self) -> None:
        proc, self.proc = self.proc, None
        self.ready.clear()
        if proc is None:
            return
        _children.discard(proc.pid)
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 5.0)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
        with contextlib.suppress(OSError):
            self._pid_file().unlink()

    async def stop(self) -> None:
        await self._terminate()
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._log_file is not None:
            self._log_file.close()
            self._log_file = None

    # ------------------------------------------------------------- requests

    async def inference(self, audio: bytes, fields: dict[str, str]) -> dict[str, Any]:
        await self.ensure_started()
        assert self._client is not None and self.proc is not None
        async with self._request_lock:
            try:
                r = await self._client.post(
                    f"{self.base_url}/inference", files={"file": ("utterance.wav", audio, "audio/wav")}, data=fields
                )
            except httpx.TimeoutException as exc:
                raise TranscriptionError(f"whisper.cpp took longer than {self.cfg.timeout_s:g}s", retryable=True) from exc
            except httpx.TransportError as exc:
                await self._after_failure()
                raise TranscriptionError(f"whisper.cpp isn't answering: {exc}", retryable=True) from exc
        if r.status_code == 200:
            try:
                data = r.json()
            except ValueError as exc:
                raise TranscriptionError("whisper.cpp sent a reply that isn't JSON", retryable=True) from exc
            if not isinstance(data, dict):
                raise TranscriptionError("whisper.cpp sent an unexpected reply", retryable=True)
            return data
        if r.status_code == 404:  # not our token: another process answered on our port
            await self._after_failure(force=True)
            raise TranscriptionError("another program answered on whisper.cpp's port; restarting it", retryable=True)
        if r.status_code == 400:
            raise TranscriptionError("whisper.cpp couldn't read this audio", retryable=False)
        if "json" in r.headers.get("content-type", ""):
            raise TranscriptionError(f"whisper.cpp failed: {r.text[:200]}", retryable=True)
        raise TranscriptionError(f"whisper.cpp rejected the request ({r.status_code}): {r.text[:200]}", retryable=False)

    async def _after_failure(self, force: bool = False) -> None:
        if force or self.proc is None or self.proc.returncode is not None:
            if self.proc is not None and self.proc.returncode is not None:
                self._crashes.append(time.monotonic())
                log.warning("whisper.cpp stopped (exit %s): %s", self.proc.returncode, self.log_tail(3))
            self.ready.clear()


class WhisperCppTranscriber(ChunkTranscriber):
    backend = "whisper-cpp"
    concurrency = 1  # one model, one request at a time (the server queues across sources)

    def __init__(
        self,
        cfg: Config,
        *,
        vad: VADConfig | None = None,
        language: str | None = None,
        prompt: str | None = None,
        on_usage: UsageCallback | None = None,
        server: WhisperServer | None = None,
    ) -> None:
        wc = cfg.transcription.whisper_cpp
        super().__init__(Path(wc.model).stem.removeprefix("ggml-"), vad=vad, on_usage=on_usage, prompt=prompt,
                         language=language)
        self.cfg = cfg
        self.wc = wc
        self.allowed = [c.lower() for c in wc.allowed_languages if c.lower() in CODES]
        self._server = server
        self._owns = server is None
        self._last_allowed: str | None = None

    @property
    def server(self) -> WhisperServer:
        if self._server is None:
            self._server = WhisperServer.acquire(self.cfg)
        return self._server

    def build_prompt(self, previous_text: str) -> str | None:
        # names/vocabulary only: feeding back earlier text lets one invented sentence (or the wrong
        # language) carry over to the next utterance
        return self.base_prompt or None

    async def stream(
        self, source: str, speaker: str, frames: AsyncIterator[AudioFrame]
    ) -> AsyncIterator[TranscriptSegment]:
        server = self.server  # a missing whisper-server or model stops here, with how to fix it
        # load the model while the first utterance is still being spoken; a failure is reported
        # (and retried) by the first request
        warm = asyncio.ensure_future(server.ensure_started())
        warm.add_done_callback(lambda t: t.cancelled() or t.exception())
        try:
            async with contextlib.aclosing(super().stream(source, speaker, frames)) as segments:
                async for seg in segments:
                    yield seg
        finally:
            if not warm.done():
                warm.cancel()

    def _fields(self, language: str, prompt: str | None) -> dict[str, str]:
        fields = {
            "response_format": "verbose_json",
            "language": language,
            "temperature": "0.0",
            "temperature_inc": "0.2",
            "no_speech_thold": f"{self.wc.no_speech_threshold:g}",
            "suppress_nst": "true",
            "no_language_probabilities": "true",
            "vad": "true" if self.server.vad_model is not None else "false",
        }
        if prompt:
            fields["prompt"] = prompt[:400]
        return fields

    async def transcribe_utterance(self, utt: Utterance, prompt: str | None) -> TranscribeResult:
        audio = wav_bytes(utt.samples)
        language = (self.language or "auto").lower()
        data = await self.server.inference(audio, self._fields(language, prompt))
        text, conf, detected = parse_reply(data, self.wc.no_speech_threshold)
        if language == "auto" and text and self.allowed and detected not in self.allowed:
            # a short or noisy clip "detected" as Dutch, Welsh…: say it again in a language you speak
            retry = self._last_allowed or self.allowed[0]
            log.debug("whisper.cpp heard %s; transcribing again in %s", detected, retry)
            data = await self.server.inference(audio, self._fields(retry, prompt))
            text, conf, _ = parse_reply(data, self.wc.no_speech_threshold)
            detected = retry
        if text and detected in CODES:
            if not self.allowed or detected in self.allowed:
                self._last_allowed = detected
        return TranscribeResult(text, confidence=conf, language=detected if detected in CODES else None)

    async def aclose(self) -> None:
        if self._owns and self._server is not None:
            server, self._server = self._server, None
            await server.release()


def parse_reply(data: dict[str, Any], no_speech_threshold: float = 0.6) -> tuple[str, float | None, str | None]:
    """(text, confidence, language code) from a verbose_json reply."""
    segments = data.get("segments")
    texts: list[str] = []
    logprobs: list[float] = []
    if isinstance(segments, list):
        for s in segments:
            if not isinstance(s, dict):
                continue
            t = str(s.get("text") or "").strip()
            nsp = _num(s.get("no_speech_prob"))
            lp = _num(s.get("avg_logprob"))
            if not t:
                continue
            if nsp is not None and lp is not None and nsp > no_speech_threshold and lp < -1.0:
                continue  # whisper's own "this was silence" rule (the server applies it per window too)
            if nsp is not None and nsp > 0.9:
                continue  # almost certainly no speech: a stock sentence invented on noise
            texts.append(t)
            if lp is not None:
                logprobs.append(lp)
    else:
        t = str(data.get("text") or "").strip()
        if t:
            texts.append(t)
    lang_name = str(data.get("language") or "").strip().lower()
    code = NAMES.get(lang_name) or (lang_name if lang_name in CODES else None)
    if not texts:
        return "", None, None  # with nothing heard, the reported language is stale
    conf = math.exp(sum(logprobs) / len(logprobs)) if logprobs else None
    return " ".join(texts), conf, code


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def check(cfg: Config) -> tuple[str, str]:
    """("ok"|"warn"|"fail", detail) for doctor and the app's setup screen."""
    wc = cfg.transcription.whisper_cpp
    binary = find_binary(wc)
    if binary is None:
        return "fail", "whisper-server not found: use the Voicestream app, or `brew install whisper-cpp`"
    if not stt_models.is_installed(cfg, wc.model) and not stt_models.model_path(cfg, wc.model).is_file():
        return "fail", f"model {wc.model} not downloaded: `voicestream models download {wc.model}`"
    extra = "" if not wc.vad or stt_models.is_installed(cfg, stt_models.VAD_MODEL) else \
        f" (voice detection off: `voicestream models download {stt_models.VAD_MODEL}`)"
    return ("warn" if extra else "ok"), f"{wc.model} · {binary}{extra}"
