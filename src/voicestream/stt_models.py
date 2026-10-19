"""Speech-to-text model files for the local whisper.cpp backend: the catalogue, where
they live on disk, and a resumable, checksummed download with progress.

Models come from the whisper.cpp project's Hugging Face repository (MIT licence,
OpenAI's Whisper weights converted to ggml). They are big (0.15-1.5 GB), so they are
downloaded once, on first use, into ``<data_dir>/models``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import Config

log = logging.getLogger(__name__)

WHISPER_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-{name}.bin"
VAD_URL = "https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-{name}.bin"
GGML_MAGIC = b"lmgg"  # 0x67676d6c, little-endian, at the start of every ggml model file

MiB = 1024 * 1024
RETRY_DELAY_S = 2.0


@dataclass(frozen=True, slots=True)
class ModelInfo:
    name: str
    """whisper.cpp's name, e.g. ``large-v3-turbo-q5_0`` (the file is ``ggml-<name>.bin``)."""
    label: str
    size: int
    """Approximate download size in bytes (for the UI; the real length comes from the server)."""
    ram_gb: float
    """Approximate memory while loaded."""
    sha1: str | None
    """From whisper.cpp's models/README.md; ``None`` when it publishes none."""
    note: str
    kind: str = "whisper"  # or "vad"
    recommended: bool = False

    @property
    def filename(self) -> str:
        return f"ggml-{self.name}.bin"

    @property
    def url(self) -> str:
        return (VAD_URL if self.kind == "vad" else WHISPER_URL).format(name=self.name)


# Multilingual models only: they all do French and English (and detect which one is spoken).
CATALOG: dict[str, ModelInfo] = {
    m.name: m
    for m in (
        ModelInfo(
            "large-v3-turbo-q5_0", "Recommended", 547 * MiB, 1.2, "e050f7970618a659205450ad97eb95a18d69c9ee",
            "Large-v3 Turbo, compressed. Near-best accuracy in French and English; fine on an 8 GB Mac.",
            recommended=True,
        ),
        ModelInfo(
            "large-v3-turbo", "Best quality", 1536 * MiB, 2.4, "4af2b29d7ec73d781377bfd1758ca957a807e941",
            "Large-v3 Turbo, full precision. Slightly more accurate; for Macs with 16 GB or more.",
        ),
        ModelInfo(
            "small", "Faster", 466 * MiB, 0.9, "55356645c2b361a969dfd0ef2c5a50d530afd8d5",
            "Small. Quicker on older Macs, but makes more mistakes, especially in French.",
        ),
        ModelInfo(
            "base", "Lightest", 142 * MiB, 0.4, "465707469ff3a37a2b9b8d8f89f2f99de7299dac",
            "Base. Very fast and small; noticeably less accurate. For testing or very old Macs.",
        ),
        ModelInfo(
            "silero-v6.2.0", "Voice detection", 885_098, 0.05, None,
            "Silero voice detector used by whisper.cpp to skip non-speech (fewer invented sentences).",
            kind="vad",
        ),
    )
}
DEFAULT_MODEL = "large-v3-turbo-q5_0"
VAD_MODEL = "silero-v6.2.0"


def models_dir(cfg: Config) -> Path:
    custom = cfg.transcription.whisper_cpp.models_dir
    return Path(custom).expanduser() if custom else cfg.data_path / "models"


def model_path(cfg: Config, name: str) -> Path:
    """Where model ``name`` lives (a catalogue name, a file name, or an absolute path)."""
    p = Path(name).expanduser()
    if p.is_absolute() or name.endswith(".bin"):
        return p if p.is_absolute() else models_dir(cfg) / p
    return models_dir(cfg) / f"ggml-{name}.bin"


def is_installed(cfg: Config, name: str) -> bool:
    p = model_path(cfg, name)
    try:
        with p.open("rb") as fh:
            return fh.read(4) == GGML_MAGIC
    except OSError:
        return False


def installed(cfg: Config) -> list[str]:
    return [name for name in CATALOG if is_installed(cfg, name)]


class DownloadError(RuntimeError):
    pass


ProgressCallback = Callable[[int, int | None], None]
"""(bytes downloaded so far, total bytes or None) -> None"""


async def download(
    cfg: Config,
    name: str,
    *,
    on_progress: ProgressCallback | None = None,
    client: httpx.AsyncClient | None = None,
    url: str | None = None,
) -> Path:
    """Download model ``name`` (resuming a partial ``.part`` file), check it and move it into place.

    Raises :class:`DownloadError`; cancelling the task leaves the ``.part`` file for a later resume.
    """
    info = CATALOG.get(name)
    if info is None:
        raise DownloadError(f"unknown model {name!r} (known: {', '.join(CATALOG)})")
    dest = model_path(cfg, name)
    if is_installed(cfg, name):
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    own_client = client is None
    client = client or httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(30.0, read=60.0))
    try:
        for attempt in range(4):
            try:
                await _fetch(client, url or info.url, part, on_progress)
                break
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                if status is not None and status < 500 and status != 429 or attempt == 3:
                    raise DownloadError(f"could not download {info.filename}: {_why(exc)}") from exc
                log.warning("download of %s interrupted (%s); resuming", info.filename, _why(exc))
                await asyncio.sleep(RETRY_DELAY_S * (attempt + 1))
    finally:
        if own_client:
            await client.aclose()
    _verify(info, part)
    os.replace(part, dest)
    log.info("model %s ready at %s", name, dest)
    return dest


async def _fetch(client: httpx.AsyncClient, url: str, part: Path, on_progress: ProgressCallback | None) -> None:
    have = part.stat().st_size if part.exists() else 0
    headers = {"Range": f"bytes={have}-"} if have else {}
    async with client.stream("GET", url, headers=headers) as resp:
        if resp.status_code == 416:  # the .part is already complete
            return
        resp.raise_for_status()
        if have and resp.status_code != 206:
            have = 0  # the server ignored the range: start over
        length = resp.headers.get("content-length")
        total = have + int(length) if length and length.isdigit() else None
        mode = "ab" if have else "wb"
        done = have
        last = -1
        with part.open(mode) as fh:
            async for chunk in resp.aiter_bytes(1 << 20):
                fh.write(chunk)
                done += len(chunk)
                if on_progress is not None and done - last >= (1 << 20):
                    last = done
                    on_progress(done, total)
        if on_progress is not None:
            on_progress(done, total)
        if total is not None and done < total:
            raise httpx.ReadError(f"connection closed after {done} of {total} bytes")


def _verify(info: ModelInfo, part: Path) -> None:
    with part.open("rb") as fh:
        head = fh.read(4)
        if head != GGML_MAGIC:
            with contextlib.suppress(OSError):
                part.unlink()
            raise DownloadError(f"{info.filename} is not a whisper.cpp model (got {head!r}); download it again")
        if info.sha1 is None:
            return
        h = hashlib.sha1(head)  # noqa: S324 - the checksum whisper.cpp publishes
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    if h.hexdigest() != info.sha1:
        with contextlib.suppress(OSError):
            part.unlink()
        raise DownloadError(f"{info.filename} is damaged (checksum mismatch); download it again")


def remove(cfg: Config, name: str) -> bool:
    removed = False
    for p in (model_path(cfg, name), model_path(cfg, name).with_name(model_path(cfg, name).name + ".part")):
        with contextlib.suppress(FileNotFoundError):
            p.unlink()
            removed = True
    return removed


def _why(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return str(exc) or type(exc).__name__
