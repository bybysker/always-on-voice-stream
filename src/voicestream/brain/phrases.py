"""Matching spoken phrases ("hey claude", "d'ici", "follow up") in transcripts, whatever the punctuation:
speech-to-text writes "Hey, Claude, …", "Dis, Claude :" or "d’ici" as often as the plain form."""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache

from ..transcribe.echo import words


@lru_cache(maxsize=64)
def _pattern(phrases: tuple[str, ...]) -> re.Pattern[str] | None:
    bodies = []
    for phrase in phrases:
        ws = words(phrase)
        if ws:
            # any run of spaces/punctuation between the words; any apostrophe inside one
            bodies.append(r"[\W_]+".join(re.escape(w).replace("'", "['’ʼ]") for w in ws))
    if not bodies:
        return None
    return re.compile(rf"(?<!\w)(?:{'|'.join(sorted(bodies, key=len, reverse=True))})(?!\w)", re.IGNORECASE)


def find_phrase(text: str, phrases: Iterable[str]) -> re.Match[str] | None:
    """The first of ``phrases`` said in ``text`` (case, spacing and punctuation ignored), or ``None``."""
    rx = _pattern(tuple(phrases))
    return rx.search(text) if rx is not None else None
