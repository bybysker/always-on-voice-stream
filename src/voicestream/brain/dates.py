"""Resolve spoken due dates ("by Friday", "end of next week", "tomorrow EOD", and the French
"d'ici vendredi", "fin de la semaine prochaine", "le 20 octobre"…) to ISO dates."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
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


# French → English rewrites applied before parsing (accents already stripped). Order matters:
# longer phrases first. Every left-hand side is French-only, so English input is untouched.
_FR_NUM = r"(\d+|un|une|deux|trois|quatre|cinq|six|sept|huit|neuf|dix|quinze|quelques)"
_FR_NUM_VALUES = {
    "un": "1", "une": "1", "deux": "2", "trois": "3", "quatre": "4", "cinq": "5", "six": "6",
    "sept": "7", "huit": "8", "neuf": "9", "dix": "10", "quinze": "15", "quelques": "3",
}  # fmt: skip
_FR_WEEKDAYS = {"lundi": "monday", "mardi": "tuesday", "mercredi": "wednesday", "jeudi": "thursday",
                "vendredi": "friday", "samedi": "saturday", "dimanche": "sunday"}  # fmt: skip
_FR_MONTHS = {"janvier": "january", "fevrier": "february", "mars": "march", "avril": "april", "mai": "may",
              "juin": "june", "juillet": "july", "aout": "august", "septembre": "september",
              "octobre": "october", "novembre": "november", "decembre": "december"}  # fmt: skip
_WD_FR = "|".join(_FR_WEEKDAYS)
_FR_UNITS = {"jour": "day", "jours": "days", "semaine": "week", "semaines": "weeks", "mois": "months"}


def _in_n(m: re.Match[str]) -> str:  # "dans trois jours" → "in 3 days"
    n = _FR_NUM_VALUES.get(m[1], m[1])
    if m[2].startswith("jour") and n in ("8", "15"):
        return f"in {int(n) // 7} weeks"  # "dans huit/quinze jours" means in one/two weeks
    return f"in {n} {_FR_UNITS[m[2]]}"


_FR_RULES: list[tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]]] = [
    (re.compile(r"\bapres[- ]demain\b"), "day after tomorrow"),
    (re.compile(r"\baujourd hui\b"), "today"),
    (re.compile(r"\b(?:en )?fin de (?:la )?journee\b"), "end of day"),
    (re.compile(r"\bce soir\b"), "tonight"),
    (re.compile(r"\bdemain\b"), "tomorrow"),
    (re.compile(r"\bfin de la semaine prochaine\b"), "end of next week"),
    (re.compile(rf"\b({_WD_FR}) de la semaine prochaine\b"), r"next \1"),
    (re.compile(r"\b(?:la )?fin de (?:la )?semaine\b|\bcette semaine\b"), "end of week"),
    (re.compile(r"\b(?:la )?semaine prochaine\b"), "next week"),
    (re.compile(r"\bfin (?:du|de) mois\b"), "end of month"),
    (re.compile(r"\b(?:le )?mois prochain\b"), "next month"),
    (re.compile(rf"\b(?:dans|d ici) {_FR_NUM} (jours?|semaines?|mois)\b"), _in_n),
    (re.compile(rf"\b({_WD_FR}) prochaine?\b"), r"\1"),  # "vendredi prochain" = the coming Friday
    # "mardi en huit": the Tuesday a week after the coming one ("en quinze": two weeks after)
    (re.compile(rf"\b({_WD_FR}) en (huit|quinze)\b"), lambda m: f"{m[1]} plus{1 if m[2] == 'huit' else 2}week"),
    (re.compile(r"\b(?:la )?fin (?:de )?l annee\b"), "end of year"),
    (re.compile(rf"\b(?:la )?fin (?:de |d )?(?=(?:{'|'.join(_FR_MONTHS)})\b)"), "end of "),  # "fin novembre"
    (re.compile(rf"\bmi[- ](?=(?:{'|'.join(_FR_MONTHS)})\b)"), "mid "),  # "mi-novembre"
    (re.compile(r"\b1er\b"), "1st"),
    # "mon" is also French for "my" ("avant mon départ"): keep it as Mon(day) only before a time or number
    (re.compile(r"\bmon (?!\d|(?:morning|afternoon|evening|night|eod|end)\b)(?=[a-z])"), ""),
    (re.compile(r"\ble (\d{1,2})\b"), r"the \1th"),
]


def _french_to_english(s: str) -> str:
    for rx, repl in _FR_RULES:
        s = rx.sub(repl, s)
    for fr, en in (*_FR_WEEKDAYS.items(), *_FR_MONTHS.items()):
        s = re.sub(rf"\b{fr}\b", en, s)
    return s


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


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
    s = _strip_accents(text.lower().strip()).replace("’", "'")
    s = re.sub(r"[^\w\s/-]", " ", s)
    s = re.sub(r"\s+", " ", s)
    s = _french_to_english(s)

    if m := re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", s):
        try:
            return date(int(m[1]), int(m[2]), int(m[3])).isoformat()
        except ValueError:
            return None
    tokens = s.split()
    named_day = next(((i, wd) for i, tok in enumerate(tokens) if (wd := _weekday_index(tok)) is not None), None)
    eod = re.search(r"\b(today|tonight|eod|end of (the )?day|this afternoon|this evening)\b", s) is not None
    if eod and named_day is None and "tomorrow" not in s:  # a named day wins: "EOD Friday" is Friday
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

    # an explicit date wins over the weekday said with it: "lundi 2 novembre", "Friday the 23rd"
    if (explicit := _explicit_date(s, tokens, named_day, base)) is not None:
        return explicit

    if named_day is not None:
        i, wd = named_day
        if (later := re.fullmatch(r"plus(\d)week", " ".join(tokens[i + 1 : i + 2]))) is not None:
            return (_next_weekday(base, wd) + timedelta(days=7 * int(later[1]))).isoformat()  # "mardi en huit"
        # "next Friday", "next week Friday", "Friday (of) next week": the Friday of next week
        next_week = (
            tokens[i - 1 : i] == ["next"]
            or tokens[max(0, i - 2) : i] == ["next", "week"]
            or re.match(r"(?:of )?next week\b", " ".join(tokens[i + 1 : i + 4])) is not None
        )
        if eod and not next_week and wd == base.weekday():
            return base.isoformat()  # "EOD Wednesday" said on a Wednesday
        return _next_weekday(base, wd, strictly_next_week=next_week).isoformat()

    if re.search(r"\bnext week\b", s):
        return (base + timedelta(days=7 - base.weekday())).isoformat()  # next Monday

    if m := re.search(r"\b(\d{1,2})/(\d{1,2})\b", s):  # ambiguous; assume month/day
        try:
            d = date(base.year, int(m[1]), int(m[2]))
            return d.isoformat()
        except ValueError:
            return None
    return None


_MONTH_RE = "|".join(sorted(set(MONTHS) | set(_MONTH_ABBR), key=len, reverse=True))
_NOT_A_DAY = r"(?!\s*(?:am|pm|h|hours?|heures?|o clock)\b)"  # "Friday 10 am" is a time, not the 10th


def _explicit_date(s: str, tokens: list[str], named_day: tuple[int, int] | None, base: date) -> str | None:
    """A date given by month and/or day of month ("October 20th", "20 Oct", "the 20th", "Friday 23",
    "end of November", "mid-November", "end of the year")."""

    def upcoming(month: int, day: int) -> str | None:
        try:
            d = date(base.year, month, day)
            if d < base - timedelta(days=7):
                d = date(base.year + 1, month, day)
            return d.isoformat()
        except ValueError:
            return None

    if re.search(r"\bend of (?:the )?year\b", s):
        return date(base.year, 12, 31).isoformat()
    if m := re.search(rf"\b(?:end of|late) ({_MONTH_RE})\b", s):
        month = _month_num(m[1])
        last = (date(base.year, month, 28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        return upcoming(month, last.day)
    if m := re.search(rf"\bmid[- ]?({_MONTH_RE})\b", s):
        return upcoming(_month_num(m[1]), 15)
    month = day = None
    if m := re.search(rf"\b({_MONTH_RE}) (\d{{1,2}})(?:st|nd|rd|th)?\b{_NOT_A_DAY}", s):
        month, day = _month_num(m[1]), int(m[2])
    elif m := re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)? (?:of )?({_MONTH_RE})\b", s):
        month, day = _month_num(m[2]), int(m[1])
    else:
        m = re.search(r"\bthe (\d{1,2})(?:st|nd|rd|th)\b", s)
        if m is None and named_day is not None:  # "vendredi 23", "Friday 23rd"
            m = re.match(rf"(\d{{1,2}})(?:st|nd|rd|th)?\b{_NOT_A_DAY}", " ".join(tokens[named_day[0] + 1 :]))
        if m is not None:
            month, day = base.month, int(m[1])
            if day < base.day:
                month = month % 12 + 1
    if month and day:
        return upcoming(month, day)
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
