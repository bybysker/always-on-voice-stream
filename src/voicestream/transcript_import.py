"""Import existing transcripts (Zoom/Meet/Teams .vtt, .srt, plain "Speaker: text"
files, or our own transcript.jsonl) so meetings recorded elsewhere get the
same extraction, notes, pushes and agents."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .models import TranscriptSegment

_TS = r"(?:(\d+):)?(\d{1,2}):(\d{2})(?:[.,](\d{1,3}))?"
_CUE = re.compile(rf"^\s*{_TS}\s*-->\s*{_TS}")
_BRACKET_TS = re.compile(r"^\s*[\[(]?\s*((?:\d+:)?\d{1,2}:\d{2}(?:[.,]\d+)?)\s*[\])]?\s*[-–]?\s*")
_SPEAKER = re.compile(r"^\s*(?P<name>[A-Z][\w .'-]{0,40}?)\s*:\s+(?P<text>.+)$")
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


def _make(speaker: str, text: str, t0: float, t1: float, me_names: set[str]) -> TranscriptSegment:
    is_me = speaker.strip().lower() in me_names
    return TranscriptSegment(
        source="mic" if is_me else "system",
        speaker="Me" if is_me else (speaker.strip() or "Others"),
        text=text.strip(),
        t_start=t0,
        t_end=max(t1, t0 + 0.5),
        backend="import",
    )


def parse_transcript(path: str | Path, user_name: str = "Me") -> list[TranscriptSegment]:
    p = Path(path)
    raw = p.read_text(encoding="utf-8-sig", errors="replace")
    me = {"me", "i", user_name.strip().lower(), user_name.strip().split()[0].lower() if user_name.strip() else "me"}
    suffix = p.suffix.lower()
    if suffix == ".jsonl":
        return [TranscriptSegment.model_validate_json(line) for line in raw.splitlines() if line.strip()]
    if suffix in (".vtt", ".srt") or "-->" in raw[:2000]:
        return _parse_cues(raw, me)
    return _parse_plain(raw, me)


def _parse_cues(raw: str, me: set[str]) -> list[TranscriptSegment]:
    out: list[TranscriptSegment] = []
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
            if sm := _SPEAKER.match(text):
                speaker, text = sm["name"], sm["text"]
            if text:
                # merge consecutive cues from the same speaker (captions are split mid-sentence)
                if out and out[-1].speaker == ("Me" if speaker.lower() in me else speaker) and t0 - out[-1].t_end < 1.5 and len(out[-1].text) < 400:
                    out[-1] = out[-1].model_copy(update={"text": f"{out[-1].text} {text}", "t_end": t1})
                else:
                    out.append(_make(speaker, text, t0, t1, me))
            break
    return out


def _parse_plain(raw: str, me: set[str]) -> list[TranscriptSegment]:
    out: list[TranscriptSegment] = []
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
        if sm := _SPEAKER.match(line):
            speaker, text = sm["name"], sm["text"]
        else:
            text = line
        if stamp is not None:
            t = stamp
        dur = max(1.0, len(text.split()) / 2.5)  # ~150 words per minute
        out.append(_make(speaker, text, t, t + dur, me))
        t += dur
    return out


def dump_jsonl(segments: list[TranscriptSegment]) -> str:
    return "\n".join(json.dumps(s.model_dump(mode="json")) for s in segments) + "\n"
