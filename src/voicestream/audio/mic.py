"""Microphone (and any named input device, e.g. BlackHole) via python-sounddevice,
plus device discovery and system-audio setup hints per OS."""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from typing import Any

from .base import Clock, QueueAudioSource

log = logging.getLogger(__name__)

# Virtual/loopback input devices that carry system audio, best first.
LOOPBACK_HINTS = (
    "blackhole",
    "loopback audio",
    "soundflower",
    "cable output",  # VB-Audio Virtual Cable (Windows)
    "voicemeeter out",
    "stereo mix",  # Realtek "what you hear" (Windows)
    "monitor of",  # PulseAudio/PipeWire monitor sources (Linux)
)


def _sd() -> Any:
    try:
        import sounddevice as sd
    except (ImportError, OSError) as exc:  # OSError: PortAudio library missing
        raise ImportError(f"sounddevice unavailable ({exc}); pip install 'voicestream[audio]'") from exc
    return sd


def input_devices() -> list[dict[str, Any]]:
    sd = _sd()
    out = []
    for idx, dev in enumerate(sd.query_devices()):
        if int(dev.get("max_input_channels", 0)) > 0:
            out.append({"index": idx, **dict(dev)})
    return out


def resolve_device(spec: str | int | None) -> int | None:
    """``None`` → OS default input; int / digit string → index; other strings → name substring."""
    if spec is None or spec == "":
        return None
    if isinstance(spec, int):
        return spec
    if str(spec).isdigit():
        return int(spec)
    needle = str(spec).lower()
    devices = input_devices()
    for dev in devices:
        if needle == str(dev["name"]).lower():
            return int(dev["index"])
    for dev in devices:
        if needle in str(dev["name"]).lower():
            return int(dev["index"])
    from . import CaptureSetupError

    names = ", ".join(repr(d["name"]) for d in devices) or "none"
    raise CaptureSetupError(f"no input device matching {spec!r}; available: {names}")


def find_loopback_device() -> str | None:
    try:
        devices = input_devices()
    except ImportError:
        return None
    for hint in LOOPBACK_HINTS:
        for dev in devices:
            if hint in str(dev["name"]).lower():
                return str(dev["name"])
    return None


class MicSource(QueueAudioSource):
    """Captures an input device with a PortAudio callback (float32, device rate)."""

    def __init__(
        self,
        name: str = "mic",
        device: str | int | None = None,
        clock: Clock | None = None,
        block_s: float = 0.05,
        samplerate: int | None = None,
    ) -> None:
        super().__init__(name, clock)
        self.device = device
        self.block_s = block_s
        self.samplerate = samplerate
        self._stream: Any = None
        self._overflows = 0
        self._device_name = "default"

    def describe(self) -> str:
        return f"{self.name}: input device {self._device_name if self._stream else (self.device or 'default')}"

    def start_capture(self) -> None:
        sd = _sd()
        index = resolve_device(self.device)
        info = sd.query_devices(index, "input")
        self._device_name = str(info["name"])
        rate = int(self.samplerate or info["default_samplerate"] or 48_000)
        channels = max(1, min(2, int(info["max_input_channels"])))

        def callback(indata: Any, frames: int, time_info: Any, status: Any) -> None:
            if status and status.input_overflow:
                self._overflows += 1
                if self._overflows in (1, 10, 100):
                    log.warning("%s: input overflow (%d) — audio dropped by the driver", self.name, self._overflows)
            self.push_from_thread(indata.copy(), rate)

        self._stream = sd.InputStream(
            device=index,
            samplerate=rate,
            channels=channels,
            dtype="float32",
            blocksize=max(64, int(rate * self.block_s)),
            callback=callback,
        )
        self._stream.start()
        log.info("%s: capturing %r at %d Hz, %d ch", self.name, self._device_name, rate, channels)

    def stop_capture(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # pragma: no cover - driver quirks on shutdown
                log.debug("error closing input stream", exc_info=True)


# ------------------------------------------------------------- system audio


def default_system_ffmpeg_input() -> str | None:
    """Best-effort ffmpeg input spec for system audio on this OS (Linux only today)."""
    if not sys.platform.startswith("linux"):
        return None
    # the Pulse server resamples/downmixes for us; small fragments keep latency low
    pulse = "-f pulse -sample_rate 16000 -channels 1 -fragment_size 640 -i"
    if shutil.which("pactl"):
        try:
            sink = subprocess.run(
                ["pactl", "get-default-sink"], capture_output=True, text=True, timeout=3, check=True
            ).stdout.strip()
            if sink:
                return f"{pulse} {sink}.monitor"
        except (subprocess.SubprocessError, OSError):
            pass
    return f"{pulse} @DEFAULT_MONITOR@"  # resolved by PulseAudio / pipewire-pulse


SYSTEM_AUDIO_HELP = {
    "darwin": (
        "macOS 14.2+: system audio is recorded with a Core Audio tap (pip install 'voicestream[audio]' installs "
        "catap); grant your terminal 'System Audio Recording' permission and restart it. Older macOS: install "
        "BlackHole (brew install blackhole-2ch), create a Multi-Output Device (your speakers/headphones first, "
        "then BlackHole) in Audio MIDI Setup, select it as the output, and set capture.system.device = "
        "\"BlackHole 2ch\". Grant your terminal Microphone access too (System Settings → Privacy & Security)."
    ),
    "win32": (
        "Windows: system audio is captured with WASAPI loopback from your default output device "
        "(pip install 'voicestream[audio]'). Alternatively enable 'Stereo Mix' or install VB-Cable and set "
        "capture.system.method = \"device\"."
    ),
    "linux": (
        "Linux: system audio is the PulseAudio/PipeWire monitor of your output (soundcard loopback, or "
        "capture.system.method = \"ffmpeg\" with ffmpeg_input = \"-f pulse -i <sink>.monitor\"; find the sink "
        "with `pactl get-default-sink`)."
    ),
}


def system_audio_help() -> str:
    key = "darwin" if sys.platform == "darwin" else "win32" if sys.platform.startswith("win") else "linux"
    return SYSTEM_AUDIO_HELP[key]


def describe_devices() -> str:
    sd = _sd()
    lines = ["Input devices (use the name or index for capture.mic.device / capture.system.device):"]
    try:
        default_in = sd.default.device[0]
    except Exception:  # noqa: BLE001
        default_in = None
    for dev in input_devices():
        mark = "*" if dev["index"] == default_in else " "
        tag = "  ← looks like a loopback device" if any(h in str(dev["name"]).lower() for h in LOOPBACK_HINTS) else ""
        lines.append(
            f" {mark}{dev['index']:>3}  {dev['name']}  ({int(dev['max_input_channels'])} ch, "
            f"{int(dev['default_samplerate'])} Hz){tag}"
        )
    try:
        import soundcard as sc  # type: ignore[import-not-found]

        speaker = sc.default_speaker()
        lines.append(f"\nSystem audio (WASAPI/PulseAudio loopback) of default output: {speaker.name}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"\nsoundcard loopback unavailable: {exc}")
    lines.append("\n" + system_audio_help())
    return "\n".join(lines)


def capture_report(cfg: Any) -> list[tuple[str, str, str]]:
    """(status, what, detail) rows for ``voicestream doctor``."""
    rows: list[tuple[str, str, str]] = []
    sources = cfg.capture.sources
    if "mic" in sources:
        try:
            idx = resolve_device(cfg.capture.mic.device)
            info = _sd().query_devices(idx, "input")
            rows.append(("ok", "microphone", str(info["name"])))
        except Exception as exc:  # noqa: BLE001
            rows.append(("fail", "microphone", str(exc)))
    if "system" in sources:
        sc_cfg = cfg.capture.system
        method = sc_cfg.method
        if method == "ffmpeg" or sc_cfg.ffmpeg_input:
            spec = sc_cfg.ffmpeg_input or default_system_ffmpeg_input()
            status = "ok" if spec and shutil.which("ffmpeg") else "fail"
            rows.append((status, "system audio via ffmpeg", spec or "set capture.system.ffmpeg_input"))
        elif sys.platform == "darwin" and method in ("auto", "tap") and sc_cfg.device is None:
            from .mactap import available

            rows.append(("ok", "system audio tap (catap)", "macOS 14.2+") if available()
                        else ("fail", "system audio", system_audio_help()))
        elif method == "device" or sys.platform == "darwin" or sc_cfg.device is not None:
            dev = sc_cfg.device or find_loopback_device()
            rows.append(("ok" if dev else "fail", "system audio device", str(dev) if dev else system_audio_help()))
        else:
            try:
                import soundcard as sc  # type: ignore[import-not-found]

                rows.append(("ok", "system audio loopback", f"default output: {sc.default_speaker().name}"))
            except Exception as exc:  # noqa: BLE001
                spec = default_system_ffmpeg_input()
                if spec and shutil.which("ffmpeg"):
                    rows.append(("warn", "system audio loopback", f"soundcard unavailable ({exc}); will use ffmpeg {spec}"))
                else:
                    rows.append(("fail", "system audio loopback", f"{exc} — {system_audio_help()}"))
    return rows
