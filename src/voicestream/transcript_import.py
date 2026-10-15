"""Import existing transcripts (Zoom/Meet/Teams .vtt, .srt, plain "Speaker: text"
files, or our own transcript.jsonl) so meetings recorded elsewhere get the
same extraction, notes, pushes and agents."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path

from .models import TranscriptSegment, _name_key

_TS = r"(?:(\d+):)?(\d{1,2}):(\d{2})(?:[.,](\d{1,3}))?"
_CUE = re.compile(rf"^\s*{_TS}\s*-->\s*{_TS}")
_BRACKET_TS = re.compile(r"^\s*[\[(]?\s*((?:\d+:)?\d{1,2}:\d{2}(?:[.,]\d+)?)\s*[\])]?\s*[-–]?\s*")
# any letter opens a name ("Élodie Durand:", "Çağla:"); _speaker() checks it is a capital
_SPEAKER = re.compile(r"^\s*(?P<name>[^\W\d_][\w .'’-]{0,40}?)\s*:\s+(?P<text>.+)$")
_VTT_VOICE = re.compile(r"<v(?:\.[^ >]+)?\s+([^>]+)>")
_TAGS = re.compile(r"</?[^>]+>")


def _secs(h: str | None, m: str, s: str, frac: str | None) -> float:
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + (int(frac.ljust(3, "0")[:3]) / 1000 if frac else 0.0)


def _parse_clock(text: str) -> float:
    parts = re.split(r"[:]", text.replace(",", "."))
    nums = [float(p) for p in parts]
    total = 0.0
    for n in nums:
        total = total * 60 + n
    return total


def _speaker(line: str) -> re.Match[str] | None:
    m = _SPEAKER.match(line)
    return m if m is not None and m["name"][0].isupper() else None


def _me_keys(user_name: str, speakers: Iterable[str], me: str | None = None) -> set[str]:
    """Name keys (see ``_name_key``) of the speaker labels that are you."""
    keys = {"me", "i", "moi"}
    if me and _name_key(me):
        return keys | {_name_key(me)}  # given exactly (``import --me``)
    user = _name_key(user_name)
    if not user:
        return keys
    keys |= {user, user.split()[0]}
    if " " not in user:
        # user_name = "Alex" while the meeting tool labels you "Alex Martin": that is you, unless
        # two speakers share the first name (then nobody is, rather than the wrong one)
        same = {k for k in map(_name_key, speakers) if k.split()[:1] == [user]}
        if len(same) == 1:
            keys |= same
    return keys


def _make(speaker: str, text: str, t0: float, t1: float, me_keys: set[str]) -> TranscriptSegment:
    is_me = _name_key(speaker) in me_keys
    return TranscriptSegment(
        source="mic" if is_me else "system",
        speaker="Me" if is_me else (speaker.strip() or "Others"),
        text=text.strip(),
        t_start=t0,
        t_end=max(t1, t0 + 0.5),
        backend="import",
    )


Line = tuple[str, str, float, float]
"""(speaker label as written, text, start, end)"""


def parse_transcript(path: str | Path, user_name: str = "Me", me: str | None = None) -> list[TranscriptSegment]:
    """``me``: your name exactly as the transcript labels you (default: matched from ``user_name``)."""
    p = Path(path)
    raw = p.read_text(encoding="utf-8-sig", errors="replace")
    suffix = p.suffix.lower()
    if suffix == ".jsonl":
        return [TranscriptSegment.model_validate_json(line) for line in raw.splitlines() if line.strip()]
    lines = _parse_cues(raw) if suffix in (".vtt", ".srt") or "-->" in raw[:2000] else _parse_plain(raw)
    keys = _me_keys(user_name, {spk for spk, *_ in lines}, me)
    return [_make(spk, text, t0, t1, keys) for spk, text, t0, t1 in lines]


def _parse_cues(raw: str) -> list[Line]:
    out: list[Line] = []
    blocks = re.split(r"\n\s*\n", raw.replace("\r\n", "\n"))
    for block in blocks:
        lines = [ln for ln in block.split("\n") if ln.strip()]
        for i, line in enumerate(lines):
            m = _CUE.match(line)
            if not m:
                continue
            t0 = _secs(m[1], m[2], m[3], m[4])
            t1 = _secs(m[5], m[6], m[7], m[8])
            text = " ".join(lines[i + 1 :])
            speaker = "Others"
            if v := _VTT_VOICE.search(text):
                speaker = v.group(1)
            text = _TAGS.sub("", text).strip()
            if sm := _speaker(text):
                speaker, text = sm["name"], sm["text"]
            if text:
                # merge consecutive cues from the same speaker (captions are split mid-sentence)
                if out and _name_key(out[-1][0]) == _name_key(speaker) and t0 - out[-1][3] < 1.5 and len(out[-1][1]) < 400:
                    out[-1] = (out[-1][0], f"{out[-1][1]} {text}", out[-1][2], t1)
                else:
                    out.append((speaker, text, t0, t1))
            break
    return out


def _parse_plain(raw: str) -> list[Line]:
    out: list[Line] = []
    t = 0.0
    speaker = "Others"
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        stamp = None
        if m := _BRACKET_TS.match(line):
            stamp = _parse_clock(m.group(1))
            line = line[m.end() :]
        if sm := _speaker(line):
            speaker, text = sm["name"], sm["text"]
        else:
            text = line
        if stamp is not None:
            t = stamp
        dur = max(1.0, len(text.split()) / 2.5)  # ~150 words per minute
        out.append((speaker, text.strip(), t, t + dur))
        t += dur
    return out


def dump_jsonl(segments: list[TranscriptSegment]) -> str:
    return "\n".join(json.dumps(s.model_dump(mode="json")) for s in segments) + "\n"
