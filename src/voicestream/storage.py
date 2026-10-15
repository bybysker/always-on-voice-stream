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
import unicodedata
import wave
from datetime import datetime
from pathlib import Path

from .models import SAMPLE_RATE, AudioFrame, Meeting, TranscriptSegment, fmt_ts

log = logging.getLogger(__name__)


def fold(text: str) -> str:
    """Accents folded to plain letters ("Réunion stratégie" → "Reunion strategie") for file names."""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def slugify(text: str, max_len: int = 40) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", fold(text).lower()).strip("-")
    return s[:max_len].strip("-") or "meeting"


def private_dir(path: Path) -> Path:
    """Create ``path`` (or tighten it) so only you can read it: transcripts, notes and agent results are
    the most sensitive data voicestream keeps, and a Mac's home folders are readable by other accounts."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        if path.stat().st_mode & 0o077:
            path.chmod(0o700)
    except OSError as exc:  # e.g. a volume without Unix permissions
        log.debug("could not restrict %s: %s", path, exc)
    return path


def atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, path)


class SessionStore:
    def __init__(self, root: Path, meeting: Meeting, store_audio: bool = False, filed_at: datetime | None = None) -> None:
        """``filed_at`` names the folder when it isn't the meeting's start (an import is filed when imported)."""
        stamp = (filed_at or meeting.started_at).astimezone().strftime("%Y-%m-%d_%H%M%S")
        self.dir = root / "sessions" / stamp
        n = 1
        while self.dir.exists() and (self.dir / "meeting.json").exists():
            n += 1
            self.dir = root / "sessions" / f"{stamp}-{n}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.meeting = meeting
        self.store_audio = store_audio
        self._titled = False  # the title slug is appended once (the name may already end in "-2")
        self._written: int | None = None  # mtime of the meeting.json we last wrote
        self._wavs: dict[str, wave.Wave_write] = {}
        self._transcript_md = self.dir / "transcript.md"
        if not self._transcript_md.exists():
            try:
                self._transcript_md.write_text(
                    f"# Transcript — {meeting.started_at.astimezone():%Y-%m-%d %H:%M}\n\n", encoding="utf-8"
                )
            except OSError as exc:  # the meeting still runs in memory (meeting.json saves log their own errors)
                log.error("could not write transcript: %s", exc)

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
        path = self.dir / "meeting.json"
        try:
            self._adopt_foreign_refs(path)
            atomic_write(path, self.meeting.model_dump_json(indent=1))
            self._written = path.stat().st_mtime_ns
        except OSError as exc:
            log.error("could not save meeting state: %s", exc)

    def _adopt_foreign_refs(self, path: Path) -> None:
        """``voicestream push`` (another process) records what it created in this file. Keep those refs, or
        the next save drops them and the next push creates everything again."""
        try:
            if path.stat().st_mtime_ns == self._written:
                return  # still the file we wrote
            disk = Meeting.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for item_id, theirs in disk.items.items():
            mine = self.meeting.items.get(item_id)
            if mine is None:
                continue
            for sink, ref in theirs.pushed.items():
                if sink not in mine.pushed:
                    mine.pushed[sink] = ref
                    mine.pushed_revision[sink] = theirs.pushed_revision.get(sink, 0)
        for sink, ref in disk.notes_ref.items():
            self.meeting.notes_ref.setdefault(sink, ref)

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
