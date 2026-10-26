"""The people you have met, remembered from one meeting to the next (``<data_dir>/people.json``).

A name gets here once it is confirmed — you added it to a meeting, or Claude listed it among the
people who took part — never from a guess alone. From then on it is a *known* name: addressed even in
a weak pattern ("Sam, the deck is ready.") it counts (:mod:`voicestream.brain.people`), and speech
recognition is told to expect it (:func:`voicestream.transcribe.keywords_for`).

It lives in the data folder with the meetings it was learned from, not in ``voicestream.toml``: the
settings file is yours to write (``brain.attendees`` is the list you keep by hand), and this one is
written by the app as it goes. Each name keeps the meetings it was confirmed in, so taking it out of
the only meeting that taught it forgets it again.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .brain.people import add_person, fold, is_user, remove_person, same_person
from .models import Meeting, utcnow
from .storage import atomic_write

log = logging.getLogger(__name__)

FILE = "people.json"
MAX_PEOPLE = 500
MAX_MEETINGS = 20
"""Meetings kept per name: enough to tell whether one correction should forget it."""
VOCABULARY = 30
"""How many of them (the most recently met) speech recognition is told about: a long list of names
in its prompt makes some engines hear names everywhere."""


def _path(data_path: Path) -> Path:
    return data_path / FILE


def load(data_path: Path) -> list[dict[str, Any]]:
    """The people, most recently met first: ``{name, meetings, last_seen}``. Empty when there is no
    file yet, or it can't be read (a damaged file is never a reason to stop a meeting)."""
    try:
        data = json.loads(_path(data_path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        log.warning("could not read %s: %s", FILE, exc)
        return []
    people = data.get("people") if isinstance(data, dict) else None
    out = []
    for p in people if isinstance(people, list) else []:
        if isinstance(p, dict) and isinstance(p.get("name"), str) and p["name"].strip():
            meetings = [m for m in p.get("meetings") or [] if isinstance(m, str)]
            out.append({"name": p["name"].strip()[:80], "meetings": meetings, "last_seen": str(p.get("last_seen") or "")})
    out.sort(key=lambda p: p["last_seen"], reverse=True)
    return out


def names(data_path: Path, limit: int | None = None) -> list[str]:
    return [p["name"] for p in load(data_path)[:limit]]


def _save(data_path: Path, people: list[dict[str, Any]]) -> None:
    people.sort(key=lambda p: p["last_seen"], reverse=True)
    try:
        atomic_write(_path(data_path), json.dumps({"people": people[:MAX_PEOPLE]}, ensure_ascii=False, indent=1))
    except OSError as exc:
        log.warning("could not save %s: %s", FILE, exc)


def remember(data_path: Path, met: Iterable[str], meeting_id: str) -> None:
    """Remember ``met`` as people met in ``meeting_id``. A first name of someone known by their full
    name ("Sam" for "Sam Lefèvre") counts for them; a fuller name replaces the shorter one."""
    people = load(data_path)
    now = utcnow().isoformat()
    changed = False
    for name in met:
        name = " ".join(name.split())[:80]
        if not name or "@" in name:
            continue
        hit = next((p for p in people if fold(p["name"]) == fold(name)), None)
        if hit is None:
            matches = [p for p in people if same_person(p["name"], name)]
            hit = matches[0] if len(matches) == 1 else None
        if hit is None:
            hit = {"name": name, "meetings": [], "last_seen": now}
            people.append(hit)
        elif len(name.split()) > len(hit["name"].split()):
            hit["name"] = name
        if meeting_id not in hit["meetings"]:
            hit["meetings"] = [*hit["meetings"], meeting_id][-MAX_MEETINGS:]
        hit["last_seen"] = now
        changed = True
    if changed:
        _save(data_path, people)


def forget(data_path: Path, name: str, meeting_id: str) -> None:
    """``name`` was taken out of ``meeting_id``: that meeting no longer vouches for them, and a name
    no other meeting vouches for is forgotten."""
    people = load(data_path)
    keep = []
    changed = False
    for p in people:
        if same_person(p["name"], name) and meeting_id in p["meetings"]:
            p["meetings"] = [m for m in p["meetings"] if m != meeting_id]
            changed = True
            if not p["meetings"]:
                continue
        keep.append(p)
    if changed:
        _save(data_path, keep)


def remember_meeting(data_path: Path, meeting: Meeting, user_name: str = "") -> None:
    """Remember the meeting's confirmed participants (added by you, or listed by Claude)."""
    confirmed = [p for p in meeting.participants
                 if (info := meeting.people.get(p)) is None or info.confirmed()]
    confirmed = [p for p in confirmed if not is_user(p, user_name)]
    if confirmed:
        remember(data_path, confirmed, meeting.id)


def edit_meeting(data_path: Path, meeting: Meeting, add: Iterable[str], remove: Iterable[str], user_name: str = "") -> bool:
    """Your corrections to who ``meeting`` was with. A name you add is remembered as someone you know;
    a name you remove is never added back to this meeting (by hearing it or by Claude), and is
    forgotten when this meeting was all that taught it. Returns whether the meeting changed."""
    changed = False
    for name in remove:
        changed |= remove_person(meeting, name)
        forget(data_path, name, meeting.id)
    for name in add:
        if add_person(meeting, name, user_name, you=True):
            changed = True
            remember(data_path, [name], meeting.id)
    return changed
