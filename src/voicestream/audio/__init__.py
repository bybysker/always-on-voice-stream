"""Audio capture: microphone, system audio (what others say), files, ffmpeg."""

from __future__ import annotations

import logging
import re
import sys

from ..config import Config
from .base import AudioSource, Clock

log = logging.getLogger(__name__)


class CaptureSetupError(RuntimeError):
    """Raised with a human-readable fix when a source can't be opened."""


def build_source(spec: str, cfg: Config, clock: Clock, *, speed: float = 1.0) -> AudioSource:
    """Create a source from a spec: ``mic``, ``system``, ``file:<path>[@name]`` or ``ffmpeg:<args>[@name]``."""
    if spec.startswith("file:"):
        from .file import file_source

        path, name = _split_name(spec[5:])
        return file_source(path, name=name or "mic", clock=clock, speed=speed)
    if spec.startswith("ffmpeg:"):
        from .ffmpeg import FFmpegSource

        args, name = _split_name(spec[7:])
        return FFmpegSource(name=name or "system", input_args=args, clock=clock)
    if spec == "mic":
        from .mic import MicSource

        return MicSource("mic", device=cfg.capture.mic.device, clock=clock)
    if spec == "system":
        return _system_source(cfg, clock)
    raise CaptureSetupError(f"unknown source '{spec}' (use mic, system, file:<path> or ffmpeg:<args>)")


def _split_name(target: str) -> tuple[str, str]:
    """``<path-or-args>[@name]`` -> (path-or-args, name or "").

    Paths and device names may contain '@' themselves (Google Drive's macOS mount is
    ~/Library/CloudStorage/GoogleDrive-<email>/, DirectShow has '@device_cm_{...}' names), so only a
    plain identifier after the last '@' counts as the name.
    """
    head, at, name = target.rpartition("@")
    if at and head and re.fullmatch(r"[\w-]+", name):
        return head, name
    return target, ""


def _system_source(cfg: Config, clock: Clock) -> AudioSource:
    sc = cfg.capture.system
    method = sc.method
    if method == "auto":
        if sc.ffmpeg_input:
            method = "ffmpeg"
        elif sc.device is not None:
            method = "device"
        elif sys.platform == "darwin":
            from .mactap import available

            method = "tap" if available() else "device"
        else:
            method = "soundcard"
    if method == "tap":
        from .mactap import TapSource, available

        if not available():
            raise CaptureSetupError(
                "capture.system.method = 'tap' needs macOS 14.2+ and the catap package (pip install catap)"
            )
        return TapSource("system", clock=clock)
    if method == "ffmpeg":
        from .ffmpeg import FFmpegSource
        from .mic import default_system_ffmpeg_input

        spec = sc.ffmpeg_input or default_system_ffmpeg_input()
        if not spec:
            raise CaptureSetupError(
                "capture.system.method = 'ffmpeg' needs capture.system.ffmpeg_input "
                "(e.g. '-f pulse -i <sink>.monitor' on Linux, '-f avfoundation -i :BlackHole 2ch' on macOS)"
            )
        return FFmpegSource(name="system", input_args=spec, clock=clock)
    if method == "device":
        from .mic import MicSource, find_loopback_device

        device = sc.device if sc.device is not None else find_loopback_device()
        if device is None:
            raise CaptureSetupError(
                "No loopback input device found. On macOS install BlackHole "
                "(brew install blackhole-2ch), create a Multi-Output Device (your speakers + BlackHole) "
                "in Audio MIDI Setup and select it as output, then set capture.system.device = 'BlackHole 2ch'. "
                "See README → 'Capturing system audio'."
            )
        return MicSource("system", device=device, clock=clock)
    if method == "soundcard":
        try:
            from .loopback import LoopbackSource

            return LoopbackSource("system", device=sc.device, clock=clock)
        except ImportError as exc:
            from .ffmpeg import FFmpegSource
            from .mic import default_system_ffmpeg_input

            spec = default_system_ffmpeg_input()
            if spec:
                log.info("soundcard not installed (%s); capturing system audio with ffmpeg: %s", exc, spec)
                return FFmpegSource(name="system", input_args=spec, clock=clock)
            raise CaptureSetupError(
                "System-audio capture needs the 'soundcard' package: pip install 'voicestream[audio]'"
            ) from exc
    raise CaptureSetupError(f"unknown capture.system.method '{method}'")


__all__ = ["AudioSource", "CaptureSetupError", "Clock", "build_source"]
