"""Date and time understanding for dental booking.

Two hard rules, both from the M1 review:

1. Date and time are INDEPENDENT. A bare time can never touch the date.
   parse_time_of_day never returns a date; parse_relative_date never
   returns a time. A caller composes a datetime only when it holds BOTH.

2. NEVER guess. Every function returns None on anything short of a
   confident parse, and it never leans a bare hour toward the clinic's
   opening hours. A bare "5:30" is reported as 05:30 with
   meridiem_explicit=False; resolving it to 17:30 is the caller's job, done
   against an authoritative, finite offer set (safe disambiguation) — not a
   heuristic here (an unsafe guess). A misparse that books the wrong slot is
   worse than a question.

Handles the forms real people send in English, Tamil, and Tanglish.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, time, timedelta

# --- Time of day ------------------------------------------------------------


@dataclass(frozen=True)
class TimeSpec:
    """A parsed time plus whether its am/pm was stated explicitly.

    `meridiem_explicit` is False for a bare "5:30" (which could be 05:30 or
    17:30) and True for "5:30 pm" / "5:30 in the evening". The parser does NOT
    guess the meridiem for a bare hour — the caller resolves it against an
    authoritative, finite offer set (safe disambiguation), never by a clinic-
    hours heuristic (an unsafe guess).
    """

    time: time
    meridiem_explicit: bool


_WORD_NUMBERS: dict[str, int] = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}

# Tamil / Tanglish hour words (transliterated + script).
_TAMIL_HOUR: dict[str, int] = {
    "onnu": 1,
    "rendu": 2,
    "moonu": 3,
    "naalu": 4,
    "anju": 5,
    "aaru": 6,
    "ezhu": 7,
    "ettu": 8,
    "onbadhu": 9,
    "pathu": 10,
    "பதினொன்று": 11,
    "பன்னிரண்டு": 12,
}

_HALF_MARKERS = ("half past", "half", "arai", "அரை")
_QUARTER_PAST = ("quarter past", "kaal", "கால்")

# Words that indicate part of day, used to disambiguate a bare hour.
_MORNING = ("morning", "am", "kaalai", "காலை", "fore noon", "forenoon")
_AFTERNOON = ("afternoon", "noon", "pagal", "பகல்", "mathiyaanam", "மதியம்")
_EVENING = ("evening", "pm", "maalai", "மாலை", "saayangaalam")
_NIGHT = ("night", "iravu", "இரவு")


def _spec(h: int, m: int, *, explicit: bool) -> TimeSpec | None:
    if 0 <= h <= 23 and 0 <= m <= 59:
        return TimeSpec(time(h, m), meridiem_explicit=explicit)
    return None


def parse_time_spec(text: str) -> TimeSpec | None:
    """Parse a time of day into a TimeSpec, or None if not confidently present.

    Accepts "10:30", "10.30", "10 30", "1030", "10:30 am", "10 am", "ten
    thirty", "half past ten", "quarter past ten", "pathu mani", "aaru mani",
    "3 in the afternoon", plus am/pm and Tamil/Tanglish part-of-day markers.

    When am/pm is NOT stated for an hour <= 12, the returned time keeps the
    literal hour (e.g. "5:30" -> 05:30) and `meridiem_explicit=False`. The
    parser never leans the hour toward the clinic's opening hours — that would
    be an unsafe guess. The caller disambiguates a bare time against the
    authoritative offer set. Hours >= 13 are unambiguous (24h), so they are
    reported explicit.
    """
    if not text:
        return None
    t = text.lower().strip()

    explicit_pm = bool(
        re.search(r"\bpm\b|\bp\.m\b", t) or any(w in t for w in _EVENING if w != "pm")
    )
    explicit_am = bool(
        re.search(r"\bam\b|\ba\.m\b", t) or any(w in t for w in _MORNING if w != "am")
    )
    # "afternoon" / "noon" imply PM; "night" implies PM (evening/night hours).
    if not explicit_pm and (any(w in t for w in _AFTERNOON) or any(w in t for w in _NIGHT)):
        explicit_pm = True

    ampm = "pm" if explicit_pm else ("am" if explicit_am else None)

    # A genuine clock token — an am/pm suffix, o'clock, sharp — as opposed to a
    # mere part-of-day word ("evening", "morning"). A part-of-day word is a
    # meridiem HINT, not evidence that a time was named; it must not license a
    # bare word-number ("one") to be read as an hour. "the evening one" names
    # no time; it is a slot-picking phrase, so parse must return None there.
    has_clock_token = bool(re.search(r"\bam\b|\ba\.m\b|\bpm\b|\bp\.m\b|o'?clock|\bsharp\b", t))

    def _resolve(h: int) -> tuple[int, bool]:
        # Returns (hour_24, meridiem_explicit).
        if h >= 13:
            return h, True  # 24h form is unambiguous
        if ampm == "pm":
            return (h + 12 if h < 12 else 12), True
        if ampm == "am":
            return (0 if h == 12 else h), True
        return h, False  # bare hour, meridiem unknown

    # 1. Explicit H:MM / H.MM / "H MM" with a separator.
    sep = re.search(r"\b(\d{1,2})[:.\s](\d{2})\b", t)
    if sep:
        h, m = int(sep.group(1)), int(sep.group(2))
        if 0 <= m <= 59:
            hh, exp = _resolve(h)
            return _spec(hh, m, explicit=exp)

    # 2. Compact "1030" -> 10:30. Reject a bare 4-digit number that is really a
    # year (2026) or otherwise date-like — require the leading pair be a valid
    # 24h hour AND the value not look like a plausible year (1900-2099).
    compact = re.search(r"(?<!\d)(\d{2})(\d{2})(?!\d)", t)
    if compact:
        whole = int(compact.group(0))
        h, m = int(compact.group(1)), int(compact.group(2))
        looks_like_year = 1900 <= whole <= 2099
        if 0 <= h <= 23 and 0 <= m <= 59 and not looks_like_year:
            hh, exp = _resolve(h)
            return _spec(hh, m, explicit=exp)

    # Half/quarter markers must match as words, so "kaal" (quarter) does not
    # fire inside "kaalai" (morning).
    def _has_marker(markers: tuple[str, ...]) -> bool:
        return any(re.search(rf"(?<!\w){re.escape(m)}(?!\w)", t) for m in markers)

    half = _has_marker(_HALF_MARKERS)
    quarter = _has_marker(_QUARTER_PAST)

    # 3. "half past ten" / "quarter past ten" / "ten thirty" / "ten am".
    for word, val in _WORD_NUMBERS.items():
        if re.search(rf"\b{word}\b", t):
            minute: int | None = None
            if half:
                minute = 30
            elif quarter:
                minute = 15
            else:
                tail = re.search(rf"\b{word}\b\s+(\d{{1,2}})\b", t)
                if tail and 0 <= int(tail.group(1)) <= 59:
                    minute = int(tail.group(1))
                elif "thirty" in t:
                    minute = 30
                elif "fifteen" in t:
                    minute = 15
                elif ampm is not None or "o'clock" in t or "oclock" in t or "sharp" in t:
                    minute = 0
            # "one" also serves as the ordinal in slot-picking phrases ("the
            # evening one", "the first one"). It may be read as the HOUR 1 only
            # when a real clock token is present ("one o'clock", "one pm") — a
            # bare part-of-day word ("evening") does not license it, so
            # "the evening one" names no time and returns None.
            if word == "one" and minute is not None and not has_clock_token:
                minute = None
            if minute is not None:
                hh, exp = _resolve(val)
                return _spec(hh, minute, explicit=exp)

    # 4. Tamil/Tanglish hour + "mani" (o'clock). No clinic-hours lean — a bare
    # Tamil hour is reported with the literal hour and meridiem_explicit=False,
    # exactly like a bare numeric hour.
    for word, val in _TAMIL_HOUR.items():
        if word in t:
            minute = 30 if half else (15 if quarter else 0)
            hh, exp = _resolve(val)
            return _spec(hh, minute, explicit=exp)

    # 5. Bare hour with an explicit am/pm or "mani"/"o'clock".
    bare = re.search(r"\b(\d{1,2})\s*(am|pm|mani|o'?clock)\b", t)
    if bare:
        h = int(bare.group(1))
        minute = 30 if half else 0
        hh, exp = _resolve(h)
        return _spec(hh, minute, explicit=exp)

    # 6. A lone hour digit with a part-of-day marker ("3 in the afternoon",
    # "afternoon 3", "evening 6"). The marker already set `ampm` above, so a
    # bare digit is now anchored to a meridiem. Requires a marker so a stray
    # digit in unrelated text is not read as a time.
    if ampm is not None:
        lone = re.search(r"\b(\d{1,2})\b", t)
        if lone:
            h = int(lone.group(1))
            minute = 30 if half else (15 if quarter else 0)
            hh, exp = _resolve(h)
            return _spec(hh, minute, explicit=exp)

    return None


def parse_time_of_day(text: str) -> time | None:
    """Backward-compatible wrapper returning just the parsed time (or None)."""
    spec = parse_time_spec(text)
    return spec.time if spec is not None else None


# --- Relative date ----------------------------------------------------------

_WEEKDAYS: dict[str, int] = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
    # Tanglish / Tamil
    "thingal": 0,
    "செவ்வாய்": 1,
    "sevvai": 1,
    "budhan": 2,
    "vyaazhan": 3,
    "velli": 4,
    "வெள்ளி": 4,
    "sani": 5,
    "சனி": 5,
    "nyayiru": 6,
    "gnayiru": 6,
}

_TODAY = ("today", "innaiku", "innaikku", "இன்று", "இன்னைக்கு")
_TOMORROW = ("tomorrow", "naalai", "நாளை", "naalaikku")
_DAY_AFTER = ("day after tomorrow", "naalaiku aprom", "methangu", "நாளன்று")


def parse_relative_date(text: str, today: date) -> date | None:
    """Parse a date relative to `today`, or None if not confidently present.

    Accepts: "today", "tomorrow", "day after tomorrow", weekday names
    (next occurrence), and Tamil/Tanglish equivalents. `today` is injected
    by the caller — this function never reads the clock, so it cannot drift
    and is deterministic under test. Never returns a time.
    """
    if not text:
        return None
    t = text.lower().strip()

    if any(w in t for w in _DAY_AFTER):
        return today + timedelta(days=2)
    if any(w in t for w in _TOMORROW):
        return today + timedelta(days=1)
    if any(w in t for w in _TODAY):
        return today

    for word, dow in _WEEKDAYS.items():
        if re.search(rf"\b{re.escape(word)}\b", t):
            days_ahead = (dow - today.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7  # "monday" said on a Monday means next Monday
            return today + timedelta(days=days_ahead)

    # Explicit day-of-month like "on the 15th" — the ordinal suffix must be
    # attached to the digits (st/nd/rd/th), so "at 10:30" or "with" never
    # reads as a date, and a bare time is never mistaken for a day.
    dom = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)\b", t)
    if dom:
        day = int(dom.group(1))
        if 1 <= day <= 31:
            month, year = today.month, today.year
            try:
                candidate = date(year, month, day)
            except ValueError:
                return None
            if candidate < today:
                month += 1
                if month > 12:
                    month = 1
                    year += 1
                try:
                    candidate = date(year, month, day)
                except ValueError:
                    return None
            return candidate

    return None
