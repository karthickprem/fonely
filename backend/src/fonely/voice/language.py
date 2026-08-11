"""Caller-language detection + language-mirrored deterministic responses.

The open-ended LLM turns already mirror the caller's language (the system
prompt tells them to). This module closes the gap for the DETERMINISTIC gate
strings — readback, confirmation, goodbye, medical-safe, commit results — which
were hardcoded single-language Tanglish. A pure-English caller used to get an
English conversation that suddenly turned Tamil at the readback and
confirmation, the exact moments that close the booking.

Three buckets, matching the existing TERMINAL_RESPONSES convention in
dialogue.py: "en" (English), "ta" (Tamil script), "ta-Latn" (Tanglish).

Detection is STICKY script-based: a turn with real language signal sets the
bucket; a short/ambiguous turn ("ok", "6:30", "B") keeps the previous bucket.
This runs every turn, so a mid-conversation switch flips the NEXT deterministic
response — English caller who switches to Tamil gets Tamil from then on.

Safety invariant: the medical-safe and no-receipt strings are equally locked
in all three buckets. Only wording changes across languages, never whether a
gate fires. A missing variant would silently fall back to English at a safety
moment, so every key carries all three (asserted in tests).
"""
from __future__ import annotations

import re
from datetime import time

# Tamil script Unicode block.
_TAMIL_BLOCK = re.compile(r"[஀-௿]")

# Romanized-Tamil markers. Drawn from the vocabulary already used elsewhere in
# the package (stt_normalizer romanized tables, context.TAMIL_RELATIVE_DATES,
# dialogue._CONFIRM_WORDS) so this is not a new invented word list. A Latin-only
# turn containing any of these reads as Tanglish, not English.
_ROMANIZED_TAMIL = frozenset({
    # verbs / requests
    "venum", "vendum", "pannunga", "pannanum", "pannitten", "panniten",
    "sollunga", "podunga", "podanum", "paakkanum", "poganum",
    # dates (romanized)
    "innaikku", "innaiku", "naalaikku", "naalai",
    # confirmations / particles
    "aama", "aamaa", "sari", "seri", "seringa", "illa",
    "da", "pa", "nga", "bro",  # Chennai colloquial particles
})

# The default bucket before any caller language is known (first turn). Warm
# Chennai Tanglish, matching the greeting.
DEFAULT_LANGUAGE = "ta-Latn"
_VALID = ("en", "ta", "ta-Latn")


def detect_language(text: str, previous: str = DEFAULT_LANGUAGE) -> str:
    """Sticky script-based language detection for one caller turn.

    Returns "ta" | "ta-Latn" | "en". A turn with no decisive signal (bare
    number, single confirm word, single-letter name) returns `previous`.
    """
    if previous not in _VALID:
        previous = DEFAULT_LANGUAGE

    stripped = text.strip()
    if not stripped:
        return previous

    # Tamil script present → Tamil, unambiguously.
    if _TAMIL_BLOCK.search(stripped):
        return "ta"

    lower = stripped.lower()
    tokens = re.findall(r"[a-z]+", lower)

    # Romanized-Tamil marker in a Latin-only turn → Tanglish.
    if any(tok in _ROMANIZED_TAMIL for tok in tokens):
        return "ta-Latn"

    # No decisive signal: bare number/punctuation, or a single short token that
    # carries no language of its own ("ok", "yes", "B", "6:30"). Stay sticky.
    real_words = [t for t in tokens if len(t) >= 2]
    if not real_words:
        return previous
    if len(tokens) == 1 and len(tokens[0]) <= 3:
        # single short word like "ok", "yes", "hmm" — no language signal
        return previous

    # Latin-only, real English words, no Tamil markers → English.
    return "en"


# ---------------------------------------------------------------------------
# Deterministic response table — all three buckets for every key.
# Shape matches dialogue.TERMINAL_RESPONSES: dict[key][lang] -> str.
# commit_success carries {id}; format at call site.
# ---------------------------------------------------------------------------
RESPONSES: dict[str, dict[str, str]] = {
    "medical_safe": {
        "en": "That's something the doctor needs to see in person. Shall we book an appointment?",
        "ta": "அது doctor நேரில் பார்த்துதான் சொல்ல முடியும். Appointment book பண்ணலாமா?",
        "ta-Latn": "Adhu doctor nேரில் paathuthaan solla mudiyum. Appointment book pannalaamaa?",
    },
    "goodbye": {
        "en": "Thank you, take care! See you at the clinic.",
        "ta": "நன்றி, பத்திரம்! Clinic-ல சந்திப்போம்.",
        "ta-Latn": "நன்றி, take care! Clinic-ல சந்திப்போம்.",
    },
    "booking_noted": {
        "en": "I've noted the booking. Anything else I can help with?",
        "ta": "Booking note பண்ணிட்டேன். வேற ஏதாவது doubt இருக்கா?",
        "ta-Latn": "Booking note பண்ணிட்டேன். வேற ஏதாவது doubt இருக்கா?",
    },
    "commit_incomplete": {
        "en": "The details are incomplete. Please confirm with the clinic staff.",
        "ta": "Details முழுசா இல்ல. Clinic staff கிட்ட confirm பண்ணுங்க.",
        "ta-Latn": "Details incomplete. Clinic staff கிட்ட confirm பண்ணுங்க.",
    },
    "commit_error": {
        "en": "I've noted the details. The clinic staff will confirm.",
        "ta": "Details note பண்ணிட்டேன். Clinic staff confirm பண்ணுவாங்க.",
        "ta-Latn": "Details note பண்ணிட்டேன். Clinic staff confirm பண்ணுவாங்க.",
    },
    "commit_refused": {
        "en": "That time couldn't be booked. Please call the clinic to confirm.",
        "ta": "அந்த நேரம் book பண்ண முடியல. Clinic-ல call பண்ணி confirm பண்ணுங்க.",
        "ta-Latn": "Andha நேரம் book pannna mudiyala. Clinic-ல call பண்ணி confirm பண்ணுங்க.",
    },
    # commit_success interpolates the appointment id; format at call site.
    "commit_success": {
        "en": "Appointment #{id} is confirmed. Anything else I can help with?",
        "ta": "Appointment #{id} confirm ஆயிடுச்சு. வேற ஏதாவது doubt இருக்கா?",
        "ta-Latn": "Appointment #{id} confirm ஆயிடுச்சு. வேற ஏதாவது doubt இருக்கா?",
    },
    # The readback tail — the "is this correct?" question appended to the facts.
    "readback_tail": {
        "en": "Is this correct?",
        "ta": "இது சரியா?",
        "ta-Latn": "இது correct-ஆ?",
    },
}


def get_response(key: str, lang: str) -> str:
    """Look up a deterministic response in the caller's language.

    Falls back to English if the language variant is somehow missing — but
    every key carries all three buckets (asserted in tests), so the fallback
    is a belt-and-suspenders guard, not an expected path.
    """
    variants = RESPONSES[key]
    return variants.get(lang, variants["en"])


# English period labels for spoken time. Tamil/Tanglish reuse the existing
# dialogue._format_spoken_time (காலை/மதியம்/மாலை).
def format_time_spoken(t: time, lang: str) -> str:
    """Spoken time in the caller's language.

    Only the period label is language-specific; the hour/minute math is shared
    and identical, so the time VALUE embedded in a readback is byte-identical
    across languages — only the connective word changes. This matters: a
    readback whose time drifts between languages would be a wrong-booking bug.
    """
    hour_12 = t.hour % 12 or 12
    minute_str = f":{t.minute:02d}" if t.minute else ""

    if lang == "en":
        if 5 <= t.hour < 12:
            period = "morning"
        elif 12 <= t.hour < 17:
            period = "afternoon"
        elif 17 <= t.hour < 21:
            period = "evening"
        else:
            period = ""
        # English says "6:30 evening" awkwardly; use "evening 6:30" to match
        # the Tamil word order the rest of the readback uses.
        return f"{period} {hour_12}{minute_str}".strip()

    # ta / ta-Latn: reuse the established Tamil period words.
    from .dialogue import _format_spoken_time
    return _format_spoken_time(t)
