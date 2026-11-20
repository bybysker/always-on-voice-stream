"""How a meeting went, as a conversation: who talked how much, for how long at a stretch, and who
asked the questions. Worked out from the transcript's times on this computer: no model call and no cost.

These figures are for you (the meeting page, ``voicestream mcp``), never for the others: they stay out
of the notes and the follow-up e-mail, so nothing about how someone talked reaches a shared Doc or a CRM.

* **talk share**: each speaker's seconds of speech over everyone's;
* **longest monologue**: the longest stretch one speaker held the floor, their lines following each
  other with no one else in between and pauses of at most :data:`MONOLOGUE_GAP_S`;
* **questions**: the question marks in their lines (speech-to-text punctuates questions it hears);
* **pace**: words per minute of speech, once there are :data:`PACE_MIN_S` seconds of it;
* **switches per minute**: how often the speaker changed, a sign of a dialogue rather than a talk.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .models import Meeting, TranscriptSegment

MONOLOGUE_GAP_S = 5.0
"""A pause longer than this ends a monologue, even when the same person speaks next."""
PACE_MIN_S = 30.0
"""Words per minute are only given for someone with at least this much speech."""


def _words(text: str) -> int:
    return sum(1 for w in text.split() if any(c.isalnum() for c in w))


def conversation_stats(meeting: Meeting, speaker: Callable[[TranscriptSegment], str]) -> dict[str, Any]:
    """``speaker`` names each line's speaker (the transcript's labels, voices named). The microphone's
    lines are always yours (``you: true``). Speakers are listed by talk time, the longest first."""
    segs = sorted((s for s in meeting.segments if s.t_end > s.t_start), key=lambda s: s.t_start)
    people: dict[str, dict[str, Any]] = {}
    run_who: str | None = None
    run_start = run_end = 0.0
    switches = 0

    def close_run() -> None:
        if run_who is not None:
            p = people[run_who]
            p["longest_s"] = max(p["longest_s"], run_end - run_start)
            p["turns"] += 1

    for s in segs:
        who = speaker(s)
        p = people.setdefault(who, {"name": who, "you": False, "talk_s": 0.0, "words": 0, "questions": 0,
                                    "longest_s": 0.0, "turns": 0})
        p["you"] = p["you"] or s.source == "mic"
        p["talk_s"] += s.t_end - s.t_start
        p["words"] += _words(s.text)
        p["questions"] += min(s.text.count("?"), 5)  # a line of "???" is one confused moment, not five questions
        if who == run_who and s.t_start - run_end <= MONOLOGUE_GAP_S:
            run_end = max(run_end, s.t_end)
            continue
        if run_who is not None and who != run_who:
            switches += 1
        close_run()
        run_who, run_start, run_end = who, s.t_start, s.t_end
    close_run()

    total = sum(p["talk_s"] for p in people.values())
    duration = max((s.t_end for s in segs), default=0.0) - min((s.t_start for s in segs), default=0.0)
    out = []
    for p in sorted(people.values(), key=lambda p: -p["talk_s"]):
        out.append({
            **p,
            "talk_s": round(p["talk_s"], 1),
            "longest_s": round(p["longest_s"], 1),
            "share": round(p["talk_s"] / total, 3) if total else 0.0,
            "wpm": round(p["words"] / (p["talk_s"] / 60)) if p["talk_s"] >= PACE_MIN_S else None,
        })
    you = [p for p in out if p["you"]]
    return {
        "speakers": out,
        "speech_s": round(total, 1),
        "duration_s": round(duration, 1),
        "switches_per_min": round(switches / (duration / 60), 1) if duration >= 60 else None,
        "you": {
            "share": round(sum(p["share"] for p in you), 3),
            "longest_s": max((p["longest_s"] for p in you), default=0.0),
            "questions": sum(p["questions"] for p in you),
        } if you else None,
    }
