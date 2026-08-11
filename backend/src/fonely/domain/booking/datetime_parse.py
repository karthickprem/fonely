"""Date and time understanding for dental booking.

Two hard rules, both from the M1 review:

1. Date and time are INDEPENDENT. A bare time can never touch the date.
   parse_time_of_day never returns a date; parse_relative_date never
   returns a time. A caller composes a datetime only when it holds BOTH.

2. NEVER guess. Every function returns None on anything short of a
   confident parse. A misparse that books the wrong slot is worse than a
   question — the caller asks the patient to repeat rather than defaulting.

Handles the forms real people send in English, Tamil, and Tanglish.
"""

from __future__ import annotations

import re
from datetime import date, time, timedelta

# --- Time of day ------------------------------------------------------------

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


def _clamp(h: int, m: int) -> time | None:
    if 0 <= h <= 23 and 0 <= m <= 59:
        return time(h, m)
    return None


def parse_time_of_day(text: str) -> time | None:
    """Parse a time of day, or None if not confidently present.

    Accepts: "10:30", "10.30", "10 30", "1030", "10:30 am", "10 am",
    "ten thirty", "half past ten", "quarter past ten", "pathu mani",
    "aaru mani", plus am/pm and Tamil/Tanglish part-of-day markers.
    Never returns a date. Never guesses a missing minute as anything but :00
    only when an explicit hour is given.
    """
    if not text:
        return None
    t = text.lower().strip()

    ampm = None
    if re.search(r"\bpm\b|\bp\.m\b", t) or any(w in t for w in _EVENING if w != "pm"):
        ampm = "pm"
    elif re.search(r"\bam\b|\ba\.m\b", t) or any(w in t for w in _MORNING if w != "am"):
        ampm = "am"

    def _apply_ampm(h: int) -> int:
        if ampm == "pm" and h < 12:
            return h + 12
        if ampm == "am" and h == 12:
            return 0
        return h

    # 1. Explicit H:MM / H.MM / "H MM" with a separator.
    sep = re.search(r"\b(\d{1,2})[:.\s](\d{2})\b", t)
    if sep:
        h, m = int(sep.group(1)), int(sep.group(2))
        # A bare "10 30" is only a time if the second group is a valid minute.
        if 0 <= m <= 59:
            return _clamp(_apply_ampm(h), m)

    # 2. Compact "1030" -> 10:30 (4 digits, valid minute tail).
    compact = re.search(r"\b(\d{2})(\d{2})\b", t)
    if compact:
        h, m = int(compact.group(1)), int(compact.group(2))
        if 0 <= h <= 23 and 0 <= m <= 59:
            return _clamp(_apply_ampm(h), m)

    # Half/quarter markers must match as words, so "kaal" (quarter) does not
    # fire inside "kaalai" (morning).
    def _has_marker(markers: tuple[str, ...]) -> bool:
        return any(re.search(rf"(?<!\w){re.escape(m)}(?!\w)", t) for m in markers)

    half = _has_marker(_HALF_MARKERS)
    quarter = _has_marker(_QUARTER_PAST)

    # 3. "half past ten" / "quarter past ten" / "ten thirty" / "ten am".
    for word, val in _WORD_NUMBERS.items():
        if re.search(rf"\b{word}\b", t):
            if half:
                return _clamp(_apply_ampm(val), 30)
            if quarter:
                return _clamp(_apply_ampm(val), 15)
            tail = re.search(rf"\b{word}\b\s+(\d{{1,2}})\b", t)
            if tail and 0 <= int(tail.group(1)) <= 59:
                return _clamp(_apply_ampm(val), int(tail.group(1)))
            if "thirty" in t:
                return _clamp(_apply_ampm(val), 30)
            if "fifteen" in t:
                return _clamp(_apply_ampm(val), 15)
            # Bare word hour with any disambiguator (am/pm, o'clock, sharp).
            if ampm is not None or "o'clock" in t or "oclock" in t or "sharp" in t:
                return _clamp(_apply_ampm(val), 0)

    # 4. Tamil/Tanglish hour + "mani" (o'clock).
    for word, val in _TAMIL_HOUR.items():
        if word in t:
            h = val
            # Clinic-hours heuristic: a bare Tamil hour <= 8 with an evening or
            # no explicit morning marker leans afternoon/evening.
            leans_pm = ampm == "pm" and h < 12
            bare_low = ampm is None and h <= 8 and not any(w in t for w in _MORNING)
            if leans_pm or bare_low:
                h += 12
            minute = 30 if half else (15 if quarter else 0)
            return _clamp(h, minute)

    # 5. Bare hour with an explicit am/pm or "mani"/"o'clock".
    bare = re.search(r"\b(\d{1,2})\s*(am|pm|mani|o'?clock)\b", t)
    if bare:
        h = int(bare.group(1))
        suffix = bare.group(2)
        if suffix == "pm" and h < 12:
            h += 12
        elif suffix == "am" and h == 12:
            h = 0
        elif suffix in ("mani", "oclock", "o'clock") and h <= 8:
            h += 12
        return _clamp(h, 30 if half else 0)

    return None


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

    # Explicit day-of-month like "on the 15th".
    dom = re.search(r"\b(?:on\s+)?(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)?\b", t)
    if dom and ("th" in t or "st" in t or "nd" in t or "rd" in t):
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
