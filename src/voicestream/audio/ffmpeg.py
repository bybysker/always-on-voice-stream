"""Universal capture backend: let ffmpeg read any device/file and hand us raw PCM.

Examples of ``input_args`` (see README for how to find device names):

* Linux (PulseAudio/PipeWire), system audio: ``-f pulse -i <default-sink>.monitor``
* Linux, microphone:                        ``-f pulse -i default``
* macOS, BlackHole loopback device:         ``-f avfoundation -i :BlackHole 2ch``
* Windows, microphone (DirectShow):         ``-f dshow -i audio=Microphone (Realtek Audio)``
* Any media file:                           ``-i meeting.mp4``
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shlex
import shutil
from collections.abc import AsyncIterator, Sequence

import numpy as np

from ..models import SAMPLE_RATE, AudioFrame
from .base import AudioSource, Clock

log = logging.getLogger(__name__)


class FFmpegError(RuntimeError):
    pass


class FFmpegSource(AudioSource):
    def __init__(
        self,
        name: str,
        input_args: str | Sequence[str],
        clock: Clock | None = None,
        block_s: float = 0.1,
        ffmpeg: str | None = None,
        describe_as: str | None = None,
        live: bool = True,
    ) -> None:
        super().__init__(name, clock)
        self.live = live
        """A device (the default): ffmpeg ending by itself is then a failure to restart from, not the end."""
        self.input_args = shlex.split(input_args) if isinstance(input_args, str) else list(input_args)
        self.block_bytes = int(SAMPLE_RATE * block_s) * 2
        self.ffmpeg = ffmpeg or shutil.which("ffmpeg")
        self._describe = describe_as
        self._proc: asyncio.subprocess.Process | None = None

    def describe(self) -> str:
        return f"{self.name}: {self._describe or 'ffmpeg ' + shlex.join(self.input_args)}"

    def command(self) -> list[str]:
        if not self.ffmpeg:
            raise FFmpegError("ffmpeg not found on PATH (install it or choose another capture method)")
        return [
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            *self.input_args,
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-f",
            "s16le",
            "-acodec",
            "pcm_s16le",
            "pipe:1",
        ]

    async def frames(self) -> AsyncIterator[AudioFrame]:
        cmd = self.command()
        log.debug("starting %s", shlex.join(cmd))
        self._proc = proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert proc.stdout is not None and proc.stderr is not None
        stderr_task = asyncio.ensure_future(proc.stderr.read())
        stopper = asyncio.ensure_future(self._stop.wait())
        t0 = self.clock.now()
        samples_out = 0
        leftover = b""
        eof = False
        err = b""
        try:
            while True:
                reader = asyncio.ensure_future(proc.stdout.read(self.block_bytes))
                done, _ = await asyncio.wait({reader, stopper}, return_when=asyncio.FIRST_COMPLETED)
                if reader not in done:
                    reader.cancel()
                    break
                chunk = reader.result()
                if not chunk:
                    eof = True
                    break
                data = leftover + chunk
                usable = len(data) - (len(data) % 2)
                leftover = data[usable:]
                if not usable:
                    continue
                samples = np.frombuffer(data[:usable], dtype="<i2").astype(np.int16)
                yield AudioFrame(
                    source=self.name, samples=samples, t_start=t0 + samples_out / SAMPLE_RATE
                )
                samples_out += samples.size
        finally:
            stopper.cancel()
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), 3)
                except asyncio.TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                    await proc.wait()
            with contextlib.suppress(Exception):
                err = await asyncio.wait_for(stderr_task, 1)
            if proc.returncode not in (0, None, -15, 255) and not self.stopped:
                msg = err.decode(errors="replace").strip()[-800:]
                raise FFmpegError(f"ffmpeg exited with {proc.returncode}: {msg}")
            if samples_out == 0 and err and not self.stopped:
                log.warning("%s: ffmpeg produced no audio: %s", self.name, err.decode(errors="replace")[-400:])
        if eof and self.live and not self.stopped:
            # ffmpeg also exits 0 when a device's input goes away (PipeWire restarted, device unplugged):
            # a normal end would leave the source off for the rest of the day, an error gets it restarted
            msg = err.decode(errors="replace").strip()[-400:]
            raise FFmpegError(f"ffmpeg capture ended unexpectedly (exit {proc.returncode})" + (f": {msg}" if msg else ""))
