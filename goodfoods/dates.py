"""Relative date phrases -> calendar dates, resolved in code (Asia/Kolkata), never by the model.

Rules (documented in the README, "Relative dates"):
- today / tonight / aaj                         -> today
- tomorrow / kal                                -> today + 1   ("kal" means tomorrow in a booking context)
- day after tomorrow / parso                    -> today + 2
- <weekday>, this / coming / is <weekday>       -> the next occurrence of that weekday, today included
- next / agle <weekday>, <weekday> next week    -> that weekday in NEXT calendar week (weeks run Mon-Sun).
  e.g. on Tue 27 Oct 2026: "Friday" = Fri 30 Oct, "next Friday" = Fri 6 Nov. One rule, no "if today is..." cases.
- weekend / this weekend / is weekend           -> this week's Saturday (today if it is already Sat or Sun)
- next weekend / agle weekend                   -> Saturday of next calendar week
Hindi weekday names are accepted: somvar, mangalvar, budhvar, guruvar, shukravar, shanivar, ravivar/itvaar.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from goodfoods import config

WEEKDAY_WORDS: dict[str, int] = {}
for _i, _names in enumerate([
    ("monday", "mon", "somvar", "somvaar"),
    ("tuesday", "tue", "tues", "mangalvar", "mangalvaar"),
    ("wednesday", "wed", "budhvar", "budhvaar", "budhwar"),
    ("thursday", "thu", "thur", "thurs", "guruvar", "guruvaar", "brihaspativar"),
    ("friday", "fri", "shukravar", "shukravaar", "shukrawar"),
    ("saturday", "sat", "shanivar", "shanivaar", "shaniwar"),
    ("sunday", "sun", "ravivar", "ravivaar", "itvaar", "itwar", "itvar"),
]):
    for _n in _names:
        WEEKDAY_WORDS[_n] = _i

THIS_WORDS = r"this|coming|upcoming|is|iss|aane wale|aane wala"
NEXT_WORDS = r"next|agle|agla|agli"
_WD = "|".join(sorted(WEEKDAY_WORDS, key=len, reverse=True))
_WEEK_SUFFIX = r"next week|agle hafte|agle week"

# one pattern, longest alternatives first, so it can also find a phrase inside a whole guest message
PHRASE_RE = re.compile(
    rf"\b(?:(?P<mod>{NEXT_WORDS}|{THIS_WORDS})\s+)?(?:(?P<wd>{_WD})|(?P<weekend>weekend))"
    rf"(?:\s+(?P<suffix>{_WEEK_SUFFIX}))?\b"
    r"|\b(?P<simple>day after tomorrow|day after|today|tonight|tomorrow|tmrw|aaj|kal|parso|parson)\b",
    re.I,
)
SIMPLE_OFFSETS = {"today": 0, "tonight": 0, "aaj": 0, "tomorrow": 1, "tmrw": 1, "kal": 1,
                  "day after tomorrow": 2, "day after": 2, "parso": 2, "parson": 2}


def today_ist() -> date:
    return datetime.now(config.TIMEZONE).date()


def _resolve_match(m: re.Match, today: date) -> date:
    if m.group("simple"):
        return today + timedelta(days=SIMPLE_OFFSETS[m.group("simple").lower()])
    mod = (m.group("mod") or "").lower()
    is_next = bool(re.fullmatch(NEXT_WORDS, mod)) or bool(m.group("suffix"))
    target = 5 if m.group("weekend") else WEEKDAY_WORDS[m.group("wd").lower()]
    if is_next:
        next_monday = today + timedelta(days=7 - today.weekday())
        return next_monday + timedelta(days=target)
    if m.group("weekend") and today.weekday() >= 5:
        return today  # already the weekend
    return today + timedelta(days=(target - today.weekday()) % 7)


def resolve_relative(phrase: str, today: date | None = None) -> date | None:
    """Resolve a phrase that is ONLY a relative date ("next friday", "kal", "agle shanivar"); else None."""
    text = " ".join((phrase or "").lower().replace(",", " ").split())
    m = PHRASE_RE.fullmatch(text)
    return _resolve_match(m, today or today_ist()) if m else None


def find_relative(text: str, today: date | None = None) -> tuple[str, date] | None:
    """The last relative date phrase in free text (a guest message), with its resolved date."""
    matches = [m for m in PHRASE_RE.finditer(" ".join((text or "").lower().split()))
               # in free text a bare "sat"/"sun"/"wed" is too likely to be an ordinary word
               if not (m.group("wd") and len(m.group("wd")) <= 4 and not m.group("mod"))]
    if not matches:
        return None
    m = matches[-1]
    return m.group(0), _resolve_match(m, today or today_ist())


_MON = r"jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
EXPLICIT_DATE_RE = re.compile(
    rf"\b\d{{4}}-\d{{2}}-\d{{2}}\b|\b\d{{1,2}}[/.]\d{{1,2}}\b|\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{_MON})[a-z]*\b"
    rf"|\b(?:{_MON})[a-z]*\s+\d{{1,2}}(?:st|nd|rd|th)?\b", re.I)


def has_explicit_date(text: str) -> bool:
    """True if the text names a calendar date ("2 Nov", "2026-11-06", "6/11")."""
    return bool(EXPLICIT_DATE_RE.search(text or ""))


def describe(raw: str, day: date) -> str | None:
    """How a relative phrase was read, for the tool result: "'next friday' = Friday, 06 Nov 2026"."""
    raw = (raw or "").strip()
    try:
        date.fromisoformat(raw)
        return None  # already a calendar date: nothing to explain
    except ValueError:
        return f"'{raw}' = {day:%A, %d %b %Y} ({day.isoformat()})"
