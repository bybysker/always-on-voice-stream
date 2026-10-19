"""Audio devices and permission checks for the Settings and setup screens.

macOS has an API for the microphone permission (AVFoundation) but none for system-audio
recording: a source without the permission simply delivers digital silence. So the check here
is a real one-second capture through the same sources the assistant uses, reporting how loud
it was. On a Mac the native shell can add the microphone's authorization status through the
optional ``microphone_status()`` hook (see :mod:`voicestream.app.host`).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import sys
from typing import Any

import numpy as np

from ..audio import CaptureSetupError, Clock, build_source
from ..config import Config

log = logging.getLogger(__name__)

PROBE_S = 1.0
PROBE_TIMEOUT_S = 20.0
"""The first capture can sit behind macOS's permission prompt."""


def list_devices(cfg: Config) -> dict[str, Any]:
    """``{inputs: [{index, name, default}], system_audio: {method, available, detail}}``."""
    out: dict[str, Any] = {"inputs": [], "error": None}
    try:
        from ..audio.mic import _sd, input_devices

        try:
            default = _sd().default.device[0]
        except Exception:  # noqa: BLE001 - no default input
            default = None
        out["inputs"] = [{"index": d["index"], "name": str(d["name"]), "default": d["index"] == default} for d in input_devices()]
    except ImportError as exc:
        out["error"] = str(exc)
    out["system_audio"] = _system_audio(cfg)
    return out


def _system_audio(cfg: Config) -> dict[str, Any]:
    probe = cfg.model_copy(deep=True)
    probe.capture.sources = ["system"]
    try:
        from ..audio.mic import capture_report

        rows = capture_report(probe)
    except ImportError as exc:
        return {"method": cfg.capture.system.method, "available": False, "detail": str(exc)}
    if not rows:
        return {"method": cfg.capture.system.method, "available": False, "detail": "not configured"}
    status, what, detail = rows[0]
    return {"method": cfg.capture.system.method, "available": status != "fail", "detail": f"{what}: {detail}" if detail else what}


def peak_db(peak: int) -> float | None:
    """dBFS of an int16 peak; ``None`` for digital silence (-inf isn't JSON)."""
    return round(20 * math.log10(peak / 32768), 1) if peak > 0 else None


async def probe(cfg: Config, kind: str, *, seconds: float = PROBE_S, hooks: Any = None) -> dict[str, Any]:
    """Capture ``seconds`` from the microphone (``kind="microphone"``) or system audio
    (``"system-audio"``): ``{status: granted|denied|unknown, peak_db, detail}``.

    The microphone always hears some room noise, so exact zeros mean macOS withholds it. System
    audio is silent whenever nothing plays, so silence there stays "unknown": play something
    (a video) during the test.
    """
    spec = {"microphone": "mic", "system-audio": "system"}.get(kind)
    if spec is None:
        raise ValueError(f"unknown permission {kind!r}")
    native = _native_status(hooks) if spec == "mic" else None
    if native in ("denied", "restricted"):
        return {"status": "denied", "peak_db": None, "detail": "microphone access is off for Voicestream", "native": native}
    try:
        peak, heard = await _capture(cfg, spec, seconds)
    except TimeoutError:
        return {"status": "unknown", "peak_db": None, "detail": "no audio arrived (waiting for the permission prompt?)",
                "native": native}
    except (CaptureSetupError, ImportError) as exc:
        return {"status": "unknown", "peak_db": None, "detail": str(exc), "native": native}
    except Exception as exc:  # noqa: BLE001 - PortAudio and Core Audio errors: reported, not raised
        return {"status": "unknown", "peak_db": None, "detail": f"{type(exc).__name__}: {exc}", "native": native}
    if spec == "mic":
        native = _native_status(hooks) or native  # the prompt may have just been answered
    if heard == 0:
        return {"status": "unknown", "peak_db": None, "detail": "no audio arrived", "native": native}
    if peak > 0:
        return {"status": "granted", "peak_db": peak_db(peak), "detail": "sound is coming through", "native": native}
    if spec == "mic":
        if native == "granted":  # allowed, yet silent: a hardware mute switch, a muted input level
            return {"status": "granted", "peak_db": None, "native": native,
                    "detail": "access is allowed but the microphone delivered pure silence: is it muted?"}
        # macOS hands a denied app exact zeros; elsewhere zeros say nothing about permissions
        status = "denied" if native is None and sys.platform == "darwin" else "unknown"
        return {"status": status, "peak_db": None, "native": native,
                "detail": "the microphone delivered pure silence: allow Voicestream under Privacy & Security → Microphone"}
    return {"status": "unknown", "peak_db": None, "silent": True,
            "detail": "nothing was heard: play some sound during the test; if it stays silent, allow Voicestream under "
                      "Privacy & Security → Screen & System Audio Recording",
            "native": None}


def _native_status(hooks: Any) -> str | None:
    fn = getattr(hooks, "microphone_status", None)
    if fn is None:
        return None
    try:
        return str(fn())
    except Exception as exc:  # noqa: BLE001 - an optional nicety
        log.debug("microphone_status hook failed: %s", exc)
        return None


async def _capture(cfg: Config, spec: str, seconds: float) -> tuple[int, int]:
    """(peak sample, samples heard) over ``seconds`` of capture."""
    src = build_source(spec, cfg, Clock())
    peak, heard = 0, 0

    async def collect() -> None:
        nonlocal peak, heard
        frames = src.frames()
        try:
            async for frame in frames:
                if frame.samples.size:
                    peak = max(peak, int(np.abs(frame.samples.astype(np.int32)).max()))
                    heard += int(frame.samples.size)
        finally:
            with contextlib.suppress(Exception):
                await frames.aclose()  # type: ignore[attr-defined]

    task = asyncio.ensure_future(collect())
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + PROBE_TIMEOUT_S
        # the clock starts with the first sound card buffer, not while the device opens
        while heard == 0 and not task.done() and loop.time() < deadline:
            await asyncio.sleep(0.05)
        if heard == 0 and not task.done():
            raise TimeoutError
        await asyncio.wait({task}, timeout=seconds)
    finally:
        src.stop()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), 3)
        if not task.done():
            task.cancel()
    if task.done() and not task.cancelled() and (exc := task.exception()) is not None:
        raise exc
    return peak, heard
