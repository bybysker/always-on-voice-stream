"""Cross-source echo suppression.

Without headphones the microphone also hears the speakers, so everything the
other participants say shows up twice: once from system audio ("Others") and
once, slightly mangled, from the mic ("Me") — which would make the assistant
think *you* said it. Mic segments are held for a moment; if a system segment
overlapping in time contains (most of) the same words, the mic copy is dropped.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from ..config import EchoConfig
from ..models import TranscriptSegment

# Unicode-aware, so French words stay whole ('très', not 'tr' + 's'), with elisions kept in one piece
# ("c'est", "aujourd'hui"); fragments would let a short reply match any long line containing them.
_WORD = re.compile(r"\w+(?:'\w+)*")
_APOSTROPHES = str.maketrans({"\u2019": "'", "\u2018": "'", "\u02bc": "'"})


def words(text: str) -> list[str]:
    # NFKC composes 'e' + combining accent into 'é', so both transcripts' spellings compare equal
    return _WORD.findall(unicodedata.normalize("NFKC", text).lower().translate(_APOSTROPHES))


def containment(needle: list[str], haystack: list[str]) -> float:
    """Fraction of ``needle`` words (as a multiset) present in ``haystack``."""
    if not needle:
        return 0.0
    pool: dict[str, int] = {}
    for w in haystack:
        pool[w] = pool.get(w, 0) + 1
    hit = 0
    for w in needle:
        if pool.get(w, 0) > 0:
            pool[w] -= 1
            hit += 1
    return hit / len(needle)


@dataclass
class EchoFilter:
    cfg: EchoConfig = field(default_factory=EchoConfig)
    mic_source: str = "mic"
    system_source: str = "system"
    hold_s: float = 4.0
    """How long a mic segment waits for a matching system segment (wall seconds)."""
    system_active: bool = True
    _held: list[tuple[float, TranscriptSegment]] = field(default_factory=list)
    _recent_system: list[TranscriptSegment] = field(default_factory=list)
    dropped: int = 0

    def add(self, seg: TranscriptSegment, now: float) -> list[TranscriptSegment]:
        """Feed a segment; returns segments that are safe to publish now."""
        if not self.cfg.enabled or not self.system_active:
            return [seg]
        if seg.source == self.system_source:
            self._recent_system.append(seg)
            self._prune_system(seg.t_end)
            # drop held mic segments this system segment explains; release the ones
            # system audio has moved past; keep the rest waiting
            still: list[tuple[float, TranscriptSegment]] = []
            out: list[TranscriptSegment] = []
            for held_at, s in self._held:
                if self._is_echo(s):
                    self.dropped += 1
                elif s.t_end < seg.t_start - self.cfg.window_s:
                    out.append(s)
                else:
                    still.append((held_at, s))
            self._held = still
            return out + [seg]
        if seg.source == self.mic_source:
            if self._is_echo(seg):
                self.dropped += 1
                return []
            self._held.append((now, seg))
            return []
        return [seg]

    def tick(self, now: float) -> list[TranscriptSegment]:
        """Release mic segments that waited long enough without a match."""
        out: list[TranscriptSegment] = []
        still: list[tuple[float, TranscriptSegment]] = []
        for held_at, s in self._held:
            if now - held_at >= self.hold_s:
                out.append(s)
            else:
                still.append((held_at, s))
        self._held = still
        return out

    def flush(self) -> list[TranscriptSegment]:
        out = [s for _, s in self._held if not self._is_echo(s)]
        self._held = []
        return out

    def _prune_system(self, t_now: float) -> None:
        horizon = t_now - 4 * self.cfg.window_s - 30
        self._recent_system = [s for s in self._recent_system if s.t_end >= horizon]

    def _is_echo(self, mic: TranscriptSegment) -> bool:
        mw = words(mic.text)
        if not mw:
            return False
        lo, hi = mic.t_start - self.cfg.window_s, mic.t_end + self.cfg.window_s
        overlapping = [s for s in self._recent_system if s.t_end >= lo and s.t_start <= hi]
        if not overlapping:
            return False
        sw = [w for s in overlapping for w in words(s.text)]
        score = containment(mw, sw)
        if len(mw) <= 2:
            return score == 1.0 and len(sw) <= 6
        return score >= self.cfg.similarity
