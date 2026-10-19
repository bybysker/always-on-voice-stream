"""On-disk session folders.

``<data_dir>/sessions/2026-10-14_1530-weekly-sync/``
  * ``transcript.md``    – human-readable live transcript (appended as you talk)
  * ``transcript.jsonl`` – one TranscriptSegment per line
  * ``meeting.json``     – full meeting state (items, screen notes, summary)
  * ``notes.md``         – final notes
  * ``audio-<source>.wav`` – raw audio, only when ``capture.store_audio`` is on
  * ``agents/<item-id>/`` – agent workspaces and deliverables
  * ``screens/``         – screenshots, only when ``screen.save_images`` is on
  * ``.recording``       – locked by the process recording the meeting, removed when it ends

A meeting whose ``meeting.json`` has no ``ended_at`` is either being recorded right now (by this
app or ``voicestream run`` in a terminal: hands off, that process owns the file) or was cut off — a
crash, a force quit, a power cut — and nothing will ever end it. ``.recording`` tells them apart:
the recording process holds an OS lock on it (``flock``; ``msvcrt.locking`` on Windows), which the
OS drops when the process dies, however it dies. See :func:`is_recording`.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import sys
import time
import unicodedata
import wave
from datetime import datetime
from pathlib import Path

from .models import SAMPLE_RATE, AudioFrame, Meeting, TranscriptSegment, fmt_ts

log = logging.getLogger(__name__)

RECORDING_LOCK = ".recording"
UNLOCKED_STALE_S = 3600.0
"""A folder without :data:`RECORDING_LOCK` (written by a voicestream from before it existed) counts as
still being recorded while its transcript or meeting file changed within this long: a live recorder
appends every line it hears, and always-on listening ends a meeting after minutes of silence."""

if sys.platform == "win32":  # pragma: no cover - exercised on the Windows CI
    import msvcrt

    def _try_lock(fd: int) -> bool:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # one byte, past the end is fine; released when the handle closes
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        # flock, not lockf: its lock belongs to this open file, so a second open in the same process
        # (the app checking a folder its own assistant records into) sees it as taken too
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


def is_recording(folder: Path) -> bool:
    """Whether a voicestream process is recording into ``folder`` right now (its meeting may then not
    be changed from here). ``False`` for a meeting that was cut off: whoever recorded it is gone."""
    lock = folder / RECORDING_LOCK
    try:
        fd = os.open(lock, os.O_RDWR)
    except FileNotFoundError:
        # no lock file: an ended meeting, or one recorded by an older voicestream — still going if it
        # was written to recently
        newest = 0.0
        for name in ("transcript.jsonl", "meeting.json"):
            with contextlib.suppress(OSError):
                newest = max(newest, (folder / name).stat().st_mtime)
        return time.time() - newest < UNLOCKED_STALE_S
    except OSError:
        return True  # can't tell: leave it alone
    try:
        if not _try_lock(fd):
            return True
        _unlock(fd)
        return False
    finally:
        os.close(fd)


def read_transcript(folder: Path) -> list[TranscriptSegment]:
    """The lines in ``transcript.jsonl``: written as they are heard, while ``meeting.json`` is only saved
    when items or notes change — so after a crash it is the complete transcript. A damaged last line
    (cut off mid-write) is skipped."""
    out: list[TranscriptSegment] = []
    try:
        lines = (folder / "transcript.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        if line.strip():
            try:
                out.append(TranscriptSegment.model_validate_json(line))
            except ValueError:
                log.debug("skipping a damaged transcript line in %s", folder.name)
    return out


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
        self._lock_fd: int | None = self._take_lock()
        self._transcript_md = self.dir / "transcript.md"
        if not self._transcript_md.exists():
            try:
                self._transcript_md.write_text(
                    f"# Transcript — {meeting.started_at.astimezone():%Y-%m-%d %H:%M}\n\n", encoding="utf-8"
                )
            except OSError as exc:  # the meeting still runs in memory (meeting.json saves log their own errors)
                log.error("could not write transcript: %s", exc)

    def _take_lock(self) -> int | None:
        """Hold :data:`RECORDING_LOCK` while the meeting is recorded (see :func:`is_recording`)."""
        try:
            fd = os.open(self.dir / RECORDING_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:  # the meeting still records; other processes fall back to the file times
            log.warning("could not mark %s as being recorded: %s", self.dir.name, exc)
            return None
        if not _try_lock(fd):
            log.warning("%s is locked by another process", self.dir.name)
        with contextlib.suppress(OSError):
            os.write(fd, f"{os.getpid()}\n".encode())  # for a person looking: which process records it
        return fd

    def release(self, *, ended: bool = True) -> None:
        """Drop the lock. Idempotent. ``ended``: the meeting is over, so the file goes too. Otherwise this
        process gave up on it before its wrap-up (a forced stop cut it short): the file stays, unlocked —
        that is what marks it as cut off, to be ended by the app at once. Without the file it would look
        like an older voicestream's recording, left alone for :data:`UNLOCKED_STALE_S`."""
        fd, self._lock_fd = self._lock_fd, None
        if fd is None:
            return
        with contextlib.suppress(OSError):
            os.close(fd)  # releases the lock (both flock and msvcrt.locking)
        if ended:
            with contextlib.suppress(OSError):
                (self.dir / RECORDING_LOCK).unlink()

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
        # closed when the meeting is over (wrapped up, its ended_at saved just above — or with nothing
        # in it, before its folder is removed); also, Windows can't rename a folder with a file open in it
        self.release()

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
