"""Turn budget, repetition detection, and terminal closure enforcement.

Deterministic dialogue-state constraints that do not depend on the LLM
or provider calls.  These enforce the acceptance matrix turn limits,
prevent repeated questions, and produce deterministic terminal responses.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class DialogueState:
    """Mutable per-session dialogue tracking."""
    turn_count: int = 0
    max_turns: int = 12
    asked_fields: list[str] = field(default_factory=list)
    terminal: bool = False
    terminal_reason: str = ""
    last_assistant_text: str = ""
    repeated_question_count: int = 0

    def record_turn(self, assistant_text: str, asked_field: str | None = None) -> None:
        self.turn_count += 1
        if asked_field:
            if asked_field in self.asked_fields and asked_field == self._last_asked_field():
                self.repeated_question_count += 1
            self.asked_fields.append(asked_field)
        self.last_assistant_text = assistant_text

    def _last_asked_field(self) -> str:
        return self.asked_fields[-1] if self.asked_fields else ""

    def is_over_budget(self) -> bool:
        return self.turn_count >= self.max_turns

    def has_repeated_question(self) -> bool:
        return self.repeated_question_count > 0

    def set_terminal(self, reason: str) -> None:
        self.terminal = True
        self.terminal_reason = reason


TERMINAL_RESPONSES = {
    "abandoned": {
        "ta-Latn": "Sari, booking vendum-na call pannunga.",
        "ta": "சரி, booking வேணும்னா call பண்ணுங்க.",
        "en": "Okay, call back if you'd like to book.",
    },
    "max_turns": {
        "ta-Latn": "Clinic staff-kitta connect pannunga, avanga help pannuvanga.",
        "ta": "Clinic staff கிட்ட connect பண்ணுங்க, அவங்க help பண்ணுவாங்க.",
        "en": "Please contact clinic staff directly for further help.",
    },
    "demo_complete": {
        "ta-Latn": "Details collect pannitten, aanaa save aagala. Clinic staff-kitta confirm pannunga.",
        "ta": "Details collect பண்ணிட்டேன், ஆனா save ஆகல. Clinic staff கிட்ட confirm பண்ணுங்க.",
        "en": "Details collected but not saved. Please confirm with clinic staff.",
    },
    "safety": {
        "ta-Latn": "Idhu urgent-aa irukkalam. Hospital ponga or emergency services call pannunga.",
        "ta": "இது urgent-ஆ இருக்கலாம். Hospital போங்க அல்லது emergency services call பண்ணுங்க.",
        "en": "This may be urgent. Please seek immediate medical care.",
    },
    "handoff": {
        "ta-Latn": "Indha request-ku staff help vennum. Automated help mudiyudhu.",
        "ta": "இந்த request-க்கு staff help வேணும். Automated help முடியுது.",
        "en": "A staff member is needed for this request.",
    },
}


def get_terminal_response(reason: str, language: str = "ta-Latn") -> str:
    """Return a deterministic terminal response, never LLM-generated."""
    responses = TERMINAL_RESPONSES.get(reason, TERMINAL_RESPONSES["handoff"])
    return responses.get(language, responses.get("en", ""))


def detect_filler(text: str) -> bool:
    """Detect narration/filler that should not appear in responses."""
    filler_patterns = [
        r"\bI'll note that\b",
        r"\bLet me check\b",
        r"\bSure,? I can help\b",
        r"\bசோ அதனால\b",
        r"\bone moment\b",
        r"\bplease wait\b",
        r"\bjust a second\b",
    ]
    for pattern in filler_patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


def count_questions(text: str) -> int:
    """Count questions in response text."""
    count = text.count("?")
    tamil_q = len(re.findall(r"-ஆ\b|என்ன\b|எப்போ\b|எது\b|எவ்வளவு\b|எங்க\b|எந்த\b", text))
    return max(count, tamil_q)
