"""Resolve spoken due dates ("by Friday", "end of next week", "tomorrow EOD") to ISO dates."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_WD_ABBR = {w[:3]: i for i, w in enumerate(WEEKDAYS)} | {"tues": 1, "thur": 3, "thurs": 3}
MONTHS = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]  # fmt: skip
_MONTH_ABBR = {m[:3]: i + 1 for i, m in enumerate(MONTHS)} | {"sept": 9}
_NUM_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "couple of": 2, "few": 3,
}  # fmt: skip

DUE_PHRASE = re.compile(
    r"\b(?:by|before|until|due|on|this|next|end of|eod|eow|tomorrow|today|tonight|in \w+ (?:days?|weeks?))\b"
    r"[^.?!,;]{0,40}",
    re.I,
)


def today(tz: str | None = None) -> date:
    if tz:
        return datetime.now(ZoneInfo(tz)).date()
    return datetime.now().astimezone().date()


def _weekday_index(token: str) -> int | None:
    t = token.lower().rstrip(".")
    if t in WEEKDAYS:
        return WEEKDAYS.index(t)
    return _WD_ABBR.get(t)


def _next_weekday(base: date, wd: int, *, strictly_next_week: bool = False) -> date:
    delta = (wd - base.weekday()) % 7
    if strictly_next_week:
        # "next Friday": the Friday of next week
        start_next_week = base + timedelta(days=7 - base.weekday())
        return start_next_week + timedelta(days=wd)
    if delta == 0:
        delta = 7  # "by Monday" said on a Monday means next Monday
    return base + timedelta(days=delta)


def resolve_due(text: str | None, base: date | None = None) -> str | None:
    """Best-effort conversion of a spoken deadline to ``YYYY-MM-DD`` (or ``None``)."""
    if not text:
        return None
    base = base or today()
    s = text.lower().strip()
    s = re.sub(r"[^\w\s/-]", " ", s)
    s = re.sub(r"\s+", " ", s)

    if m := re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", s):
        try:
            return date(int(m[1]), int(m[2]), int(m[3])).isoformat()
        except ValueError:
            return None
    if re.search(r"\b(today|tonight|eod|end of (the )?day|this afternoon|this evening)\b", s) and "tomorrow" not in s:
        return base.isoformat()
    if re.search(r"\b(day after tomorrow)\b", s):
        return (base + timedelta(days=2)).isoformat()
    if "tomorrow" in s:
        return (base + timedelta(days=1)).isoformat()
    if m := re.search(r"\bin (\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|a couple of|a few|couple of|few) (day|week|month)s?\b", s):
        qty_raw = m[1].replace("a couple of", "couple of").replace("a few", "few")
        qty = int(qty_raw) if qty_raw.isdigit() else _NUM_WORDS.get(qty_raw, 1)
        unit = m[2]
        days = qty * {"day": 1, "week": 7, "month": 30}[unit]
        return (base + timedelta(days=days)).isoformat()
    if re.search(r"\b(end of next week|next week end)\b", s):
        start_next = base + timedelta(days=7 - base.weekday())
        return (start_next + timedelta(days=4)).isoformat()
    if re.search(r"\b(eow|end of (the )?week|this week)\b", s):
        friday = base + timedelta(days=(4 - base.weekday()) % 7)
        return friday.isoformat()
    if re.search(r"\b(end of (the )?month|eom)\b", s):
        nxt = (base.replace(day=28) + timedelta(days=4)).replace(day=1)
        return (nxt - timedelta(days=1)).isoformat()
    if re.search(r"\bnext month\b", s):
        nxt = (base.replace(day=28) + timedelta(days=4)).replace(day=1)
        return nxt.isoformat()

    tokens = s.split()
    for i, tok in enumerate(tokens):
        wd = _weekday_index(tok)
        if wd is None:
            continue
        prev = tokens[i - 1] if i > 0 else ""
        return _next_weekday(base, wd, strictly_next_week=(prev == "next")).isoformat()

    if re.search(r"\bnext week\b", s):
        return (base + timedelta(days=7 - base.weekday())).isoformat()  # next Monday

    # "October 20th", "20 Oct", "the 20th"
    month_re = "|".join(sorted(set(MONTHS) | set(_MONTH_ABBR), key=len, reverse=True))
    m = re.search(rf"\b({month_re}) (\d{{1,2}})(?:st|nd|rd|th)?\b", s) or None
    day_first = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)? (?:of )?({month_re})\b", s)
    month = day = None
    if m:
        month, day = _month_num(m[1]), int(m[2])
    elif day_first:
        month, day = _month_num(day_first[2]), int(day_first[1])
    elif m2 := re.search(r"\bthe (\d{1,2})(?:st|nd|rd|th)\b", s):
        month, day = base.month, int(m2[1])
        if day < base.day:
            month = month % 12 + 1
    if month and day:
        year = base.year
        try:
            d = date(year, month, day)
            if d < base - timedelta(days=7):
                d = date(year + 1, month, day)
            return d.isoformat()
        except ValueError:
            return None
    if m := re.search(r"\b(\d{1,2})/(\d{1,2})\b", s):  # ambiguous; assume month/day
        try:
            d = date(base.year, int(m[1]), int(m[2]))
            return d.isoformat()
        except ValueError:
            return None
    return None


def _month_num(token: str) -> int:
    t = token.lower()
    if t in MONTHS:
        return MONTHS.index(t) + 1
    return _MONTH_ABBR[t[:4] if t.startswith("sept") else t[:3]]


def is_iso_date(value: str | None) -> bool:
    if not value:
        return False
    try:
        date.fromisoformat(value)
        return True
    except ValueError:
        return False
