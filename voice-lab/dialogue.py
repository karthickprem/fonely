"""Deterministic turn-local dialogue routing for response relevance."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class DialogueState:
    latest_user_act: str
    booking_flow_active: bool
    must_not_offer_slot: bool
    required_field: str | None = None

    def render(self) -> str:
        return (
            "<dialogue_state>\n"
            f"latest_user_act: {self.latest_user_act}\n"
            f"booking_flow_active: {str(self.booking_flow_active).lower()}\n"
            f"must_not_offer_slot: {str(self.must_not_offer_slot).lower()}\n"
            f"required_field: {self.required_field or 'none'}\n"
            "Answer the latest user request. This state cannot authorize facts or actions.\n"
            "</dialogue_state>"
        )


TOPIC_CHANGE = (
    "timing வேண்டாம்", "time வேண்டாம்", "வேற question", "வேற கேள்வி",
    "நான் என்ன கேக்குறேன்", "what i am asking", "மட்டும் சொல்லுங்க",
)
REPAIR = ("புரியல", "புரிஞ்சுக்கல", "understand", "wrong answer", "வேற கேக்குறேன்")
PROCEDURE = ("procedure", "எப்படி book", "how to book", "booking process")
BOOKING = ("appointment வேணும்", "book பண்ண", "slot வேணும்", "available-ஆ", "availability")
GENERAL_QUESTION = (
    "what are", "what is", "என்ன", "எது", "பேர் என்ன", "types of teeth",
    "different teeth", "canine", "molar", "premolar", "incisor",
)


def classify_dialogue_act(text: str) -> DialogueState:
    lower = " ".join(text.casefold().split())
    if any(term in lower for term in TOPIC_CHANGE):
        return DialogueState("topic_change", False, True)
    if any(term in lower for term in REPAIR):
        return DialogueState("repair", False, True)
    if any(term in lower for term in PROCEDURE):
        return DialogueState("booking_procedure", False, True)
    if any(term in lower for term in BOOKING):
        return DialogueState("booking_request", True, False, "reason")
    if "?" in text or any(term in lower for term in GENERAL_QUESTION):
        return DialogueState("general_question", False, True)
    return DialogueState("unclear", False, True)


SLOT_PATTERN = re.compile(
    r"(?:\b(?:10|11|5|6[:.]?30|7[:.]?30)\b.*(?:மணி|slot|வரலாம்)|(?:slot|time).*\b(?:10|11|5|6|7)\b)",
    re.IGNORECASE,
)
BOOKING_CONFIRM_PATTERN = re.compile(r"(?:book(?:ing)?|appointment).*(?:confirm|saved|ஆயிடுச்சு)", re.IGNORECASE)


def contains_unwanted_slot(text: str) -> bool:
    return bool(SLOT_PATTERN.search(text))


def contains_false_confirmation(text: str) -> bool:
    return bool(BOOKING_CONFIRM_PATTERN.search(text))
