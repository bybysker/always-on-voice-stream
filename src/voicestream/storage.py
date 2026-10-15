"""On-disk session folders.

``<data_dir>/sessions/2026-10-14_1530-weekly-sync/``
  * ``transcript.md``    – human-readable live transcript (appended as you talk)
  * ``transcript.jsonl`` – one TranscriptSegment per line
  * ``meeting.json``     – full meeting state (items, screen notes, summary)
  * ``notes.md``         – final notes
  * ``audio-<source>.wav`` – raw audio, only when ``capture.store_audio`` is on
  * ``agents/<item-id>/`` – agent workspaces and deliverables
  * ``screens/``         – screenshots, only when ``screen.save_images`` is on
"""

from __future__ import annotations

import logging
import os
import re
import wave
from pathlib import Path

from .models import SAMPLE_RATE, AudioFrame, Meeting, TranscriptSegment, fmt_ts

log = logging.getLogger(__name__)


def slugify(text: str, max_len: int = 40) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return s[:max_len].strip("-") or "meeting"


def atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, path)


class SessionStore:
    def __init__(self, root: Path, meeting: Meeting, store_audio: bool = False) -> None:
        stamp = meeting.started_at.astimezone().strftime("%Y-%m-%d_%H%M%S")
        self.dir = root / "sessions" / stamp
        n = 1
        while self.dir.exists() and (self.dir / "meeting.json").exists():
            n += 1
            self.dir = root / "sessions" / f"{stamp}-{n}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.meeting = meeting
        self.store_audio = store_audio
        self._titled = False  # the title slug is appended once (the name may already end in "-2")
        self._wavs: dict[str, wave.Wave_write] = {}
        self._transcript_md = self.dir / "transcript.md"
        if not self._transcript_md.exists():
            self._transcript_md.write_text(
                f"# Transcript — {meeting.started_at.astimezone():%Y-%m-%d %H:%M}\n\n", encoding="utf-8"
            )

    @property
    def agents_dir(self) -> Path:
        return self.dir / "agents"

    @property
    def screens_dir(self) -> Path:
        return self.dir / "screens"

    def append_segment(self, seg: TranscriptSegment) -> None:
        try:
            with (self.dir / "transcript.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(seg.model_dump_json() + "\n")
            with self._transcript_md.open("a", encoding="utf-8") as fh:
                fh.write(f"**[{fmt_ts(seg.t_start)}] {seg.speaker}:** {seg.text}\n\n")
        except OSError as exc:
            log.error("could not write transcript: %s", exc)

    def save(self) -> None:
        try:
            atomic_write(self.dir / "meeting.json", self.meeting.model_dump_json(indent=1))
        except OSError as exc:
            log.error("could not save meeting state: %s", exc)

    def save_notes(self, markdown: str) -> Path:
        path = self.dir / "notes.md"
        atomic_write(path, markdown)
        return path

    def write_audio(self, frame: AudioFrame) -> None:
        if not self.store_audio:
            return
        w = self._wavs.get(frame.source)
        if w is None:
            w = wave.open(str(self.dir / f"audio-{frame.source}.wav"), "wb")
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            self._wavs[frame.source] = w
        w.writeframes(frame.samples.astype("<i2").tobytes())

    def close(self) -> None:
        for w in self._wavs.values():
            try:
                w.close()
            except Exception:  # pragma: no cover
                pass
        self._wavs.clear()
        self.save()

    def rename_with_title(self) -> None:
        """Append a slug of the meeting title to the folder name once it is known."""
        if not self.meeting.title or self._titled:
            return
        target = self.dir.with_name(f"{self.dir.name}-{slugify(self.meeting.title)}")
        if target.exists():
            return
        try:
            self.close()
            self.dir.rename(target)
            self.dir = target
            self._transcript_md = target / "transcript.md"
            self._titled = True
        except OSError as exc:
            log.debug("could not rename session dir: %s", exc)


def load_meeting(path: str | Path) -> tuple[Meeting, Path]:
    """Load a saved session (folder or meeting.json)."""
    p = Path(path).expanduser()
    if p.is_dir():
        p = p / "meeting.json"
    if not p.is_file():
        raise FileNotFoundError(f"no meeting.json at {p}")
    return Meeting.model_validate_json(p.read_text(encoding="utf-8")), p.parent


def list_sessions(root: Path) -> list[Path]:
    base = root / "sessions"
    if not base.is_dir():
        return []
    return sorted((d for d in base.iterdir() if (d / "meeting.json").is_file()), reverse=True)
