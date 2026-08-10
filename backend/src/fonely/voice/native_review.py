"""Native Tamil/Tanglish review worksheet and naturalness heuristics.

Provides deterministic naturalness checks and a structured worksheet
for human native-speaker review of voice conversation quality.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class NaturalnessCheck:
    name: str
    passed: bool
    detail: str = ""


def check_naturalness(text: str, language: str = "ta-Latn") -> list[NaturalnessCheck]:
    """Run deterministic naturalness heuristics on response text."""
    checks: list[NaturalnessCheck] = []

    checks.append(NaturalnessCheck(
        "no_formal_tamil",
        not bool(re.search(r"(செய்கிறேன்|செய்கின்றேன்|உள்ளது|இருக்கின்றது)", text)),
        "Formal literary Tamil detected; use conversational forms",
    ))

    checks.append(NaturalnessCheck(
        "no_isolated_suffix",
        "ஆ " not in text and not text.endswith("ஆ"),
        "Isolated ஆ suffix may cause TTS pronunciation issue",
    ))

    checks.append(NaturalnessCheck(
        "no_telugu_kannada",
        not bool(re.search(r"[ఀ-౿ಀ-೿ऀ-ॿ]", text)),
        "Foreign script (Telugu/Kannada/Devanagari) detected",
    ))

    checks.append(NaturalnessCheck(
        "no_emoji",
        not bool(re.search(r"[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF]", text)),
        "Emoji in spoken text",
    ))

    checks.append(NaturalnessCheck(
        "reasonable_length",
        5 <= len(text.split()) <= 30,
        f"Response length {len(text.split())} words outside 5-30 range",
    ))

    checks.append(NaturalnessCheck(
        "no_markdown",
        not bool(re.search(r"[*_#`\[\]]", text)),
        "Markdown formatting in spoken text",
    ))

    dental_terms = ["scaling", "root canal", "extraction", "filling",
                    "crown", "braces", "consultation", "appointment",
                    "doctor", "clinic", "fee"]
    has_familiar_english = any(t in text.lower() for t in dental_terms)
    has_tamil = bool(re.search(r"[஀-௿]", text))
    checks.append(NaturalnessCheck(
        "natural_code_switch",
        not has_tamil or has_familiar_english or len(text.split()) < 5,
        "Tamil text without natural English dental/appointment terms",
    ))

    return checks


@dataclass
class ReviewWorksheetEntry:
    scenario_id: str
    turn: int
    response_text: str
    naturalness_checks: list[NaturalnessCheck] = field(default_factory=list)
    native_rating: int | None = None
    native_notes: str = ""
    pronunciation_issues: list[str] = field(default_factory=list)


@dataclass
class ReviewWorksheet:
    reviewer_name: str = ""
    reviewer_language: str = "Tamil"
    entries: list[ReviewWorksheetEntry] = field(default_factory=list)

    def add_entry(
        self,
        scenario_id: str,
        turn: int,
        response_text: str,
    ) -> ReviewWorksheetEntry:
        checks = check_naturalness(response_text)
        entry = ReviewWorksheetEntry(
            scenario_id=scenario_id,
            turn=turn,
            response_text=response_text,
            naturalness_checks=checks,
        )
        self.entries.append(entry)
        return entry

    def summary(self) -> dict[str, int]:
        total = len(self.entries)
        auto_pass = sum(
            1 for e in self.entries
            if all(c.passed for c in e.naturalness_checks)
        )
        rated = sum(1 for e in self.entries if e.native_rating is not None)
        return {
            "total_entries": total,
            "auto_naturalness_pass": auto_pass,
            "native_rated": rated,
            "pending_native_review": total - rated,
        }
