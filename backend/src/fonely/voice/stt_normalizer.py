"""Post-STT text normalizer — deterministic, field-aware, non-destructive.

Sits between STT output and BookingCollection. Normalizes known Sarvam
output variants to canonical forms. NEVER destroys the raw transcript.

Five binding constraints (CEO ruling):
1. Carry raw AND normalized together — raw is evidence, normalized is convenience
2. Never resolve ambiguity — "5" stays "5", never becomes "5 PM"
3. Field-aware — skip normalization when required_field is "name"
4. Provenance on every entry — "guessed" or "observed"
5. Score on RAW transcript, never normalized
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class NormalizationResult:
    """Carries both raw and normalized text. Raw is the evidence."""
    raw: str
    normalized: str
    changes: tuple[str, ...]


# Each entry: (pattern, replacement, provenance)
# provenance: "guessed" = hypothesized from known Tamil variants
#              "observed" = seen in real Sarvam STT output
_ROMANIZED_DATE_VARIANTS: list[tuple[str, str, str]] = [
    (r"\bnaalaikku\b", "நாளைக்கு", "guessed"),
    (r"\bnaalaku\b", "நாளைக்கு", "guessed"),
    (r"\bnaalaiku\b", "நாளைக்கு", "guessed"),
    (r"\bnaalai\b", "நாளை", "guessed"),
    (r"\binnaikku\b", "இன்னைக்கு", "guessed"),
    (r"\binnaiku\b", "இன்னைக்கு", "guessed"),
    (r"\binnaku\b", "இன்னைக்கு", "guessed"),
]

_ROMANIZED_TIME_VARIANTS: list[tuple[str, str, str]] = [
    (r"\bainthu\b", "5", "guessed"),
    (r"\baindu\b", "5", "guessed"),
    (r"\bpathu\b", "10", "guessed"),
    (r"\bpathhu\b", "10", "guessed"),
    (r"\brendu\b", "2", "guessed"),
    (r"\bmoonu\b", "3", "guessed"),
    (r"\bnaalu\b", "4", "guessed"),
    (r"\baaru\b", "6", "guessed"),
    (r"\bezhu\b", "7", "guessed"),
    (r"\bettu\b", "8", "guessed"),
    (r"\bonbadhu\b", "9", "guessed"),
    (r"\bpannirandu\b", "12", "guessed"),
]

_SPELLING_CORRECTIONS: list[tuple[str, str, str]] = [
    (r"\bapointment\b", "appointment", "guessed"),
    (r"\bappoitment\b", "appointment", "guessed"),
    (r"\bappoinment\b", "appointment", "guessed"),
    (r"\bdocter\b", "doctor", "guessed"),
    (r"\bscalling\b", "scaling", "guessed"),
]

_NUMBER_WORDS: list[tuple[str, str, str]] = [
    (r"\bsix thirty\b", "6:30", "guessed"),
    (r"\bsix-thirty\b", "6:30", "guessed"),
    (r"\bten o'?clock\b", "10", "guessed"),
    (r"\bfive o'?clock\b", "5", "guessed"),
    (r"\beleven\b", "11", "guessed"),
    (r"\btwelve\b", "12", "guessed"),
]

_FILLER_PATTERNS: list[tuple[str, str, str]] = [
    (r"^(?:um|uh|hmm|ah)\s*,?\s*", "", "guessed"),
    (r"^(?:actually|basically)\s*,?\s*", "", "guessed"),
    (r"^(?:ஒரு நிமிஷம்|wait wait)\s*,?\s*", "", "guessed"),
]

_ALL_TABLES = [
    ("date", _ROMANIZED_DATE_VARIANTS),
    ("time", _ROMANIZED_TIME_VARIANTS),
    ("spelling", _SPELLING_CORRECTIONS),
    ("number", _NUMBER_WORDS),
    ("filler", _FILLER_PATTERNS),
]

# Tables that must NOT run when required_field is "name"
_SKIP_FOR_NAME = {"date", "time", "number"}


def normalize(
    raw_text: str,
    *,
    required_field: str | None = None,
) -> NormalizationResult:
    """Normalize STT output. Returns both raw and normalized.

    When required_field is "name", date/time/number normalization is
    skipped to prevent rewriting a caller's name (e.g. "Aindu" → "5").
    """
    text = raw_text
    changes: list[str] = []

    for table_name, table in _ALL_TABLES:
        if required_field == "name" and table_name in _SKIP_FOR_NAME:
            continue
        for pattern, replacement, provenance in table:
            new_text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
            if new_text != text:
                changes.append(f"{table_name}:{pattern}→{replacement}({provenance})")
                text = new_text

    return NormalizationResult(
        raw=raw_text,
        normalized=text.strip(),
        changes=tuple(changes),
    )


def get_table_provenance() -> dict[str, list[dict[str, str]]]:
    """Report provenance of every normalizer entry for audit."""
    report: dict[str, list[dict[str, str]]] = {}
    for table_name, table in _ALL_TABLES:
        entries = []
        for pattern, replacement, provenance in table:
            entries.append({
                "pattern": pattern,
                "replacement": replacement,
                "provenance": provenance,
            })
        report[table_name] = entries
    return report
