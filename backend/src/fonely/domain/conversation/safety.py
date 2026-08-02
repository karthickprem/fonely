"""Dental safety boundary for conversation intent classification."""

import re
from dataclasses import dataclass

from fonely.domain.conversation.state import ConversationIntent

_URGENT_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"\b(emergency|unconscious|choking)\b",
        r"\bheavy\s+bleed",
        r"\bsevere\s+(bleed|swell|pain|trauma)",
        r"\bbreathing\s+difficult",
        r"\bcan'?t\s+breathe",
        r"\bmajor\s+(swell|trauma|bleed)",
        r"\bfacial\s+swelling",
    ]
]

_MEDICAL_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"\bsymptom",
        r"\b(medicine|medication|dosage|dose)\b",
        r"\bx-?ray\b",
        r"\btreatment\s+(result|outcome|option)",
        r"\bpost.?op",
        r"\bafter\s+(surgery|extraction|procedure)",
        r"\bis\s+this\s+normal",
        r"\bdiagnos",
        r"\bprescri",
        r"\bside\s+effect",
        r"\binfect",
        r"\bswoll?en\b",
        r"\bbleeding\b",
        r"\b(tooth|teeth)\b.{0,20}\b(hurt|pain|ache|broken|crack|loose|hurting|aching)",
        r"\bgum\s+(bleed|swell|pain)",
        r"\bnumb",
    ]
]

_ADMINISTRATIVE_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"\b(book|schedule|appointment|slot)\b",
        r"\b(cancel|reschedule)\b",
        r"\b(available|availability|opening)\b",
        r"\b(hour|timing|open|close)\b",
        r"\b(fee|cost|price|charge)\b",
        r"\b(service|treatment\s+list|procedure\s+list)\b",
        r"\b(location|address|direction|parking)\b",
        r"\b(doctor|dentist)\s+(name|list|available)",
        r"\b(call\s+back|callback)\b",
    ]
]

ESCALATION_MEDICAL = (
    "I'm not qualified to assess medical concerns. "
    "Let me connect you with the clinic staff who can help."
)

ESCALATION_URGENT = (
    "This sounds urgent. Please seek immediate medical attention "
    "or call emergency services. I'll also alert the clinic."
)


@dataclass(frozen=True)
class SafetyClassification:
    intent: ConversationIntent
    classification: str
    confidence: float
    reasoning: str


def classify_intent(message: str) -> SafetyClassification:
    for pattern in _URGENT_PATTERNS:
        if pattern.search(message):
            return SafetyClassification(
                intent=ConversationIntent.URGENT_MEDICAL,
                classification="urgent_medical",
                confidence=0.95,
                reasoning="Urgent medical keyword detected",
            )

    medical_matches = sum(1 for p in _MEDICAL_PATTERNS if p.search(message))
    if medical_matches > 0:
        return SafetyClassification(
            intent=ConversationIntent.MEDICAL_QUESTION,
            classification="medical",
            confidence=min(0.5 + medical_matches * 0.15, 0.95),
            reasoning=f"Medical pattern matched ({medical_matches} indicators)",
        )

    admin_matches = sum(1 for p in _ADMINISTRATIVE_PATTERNS if p.search(message))
    if admin_matches > 0:
        booking_patterns = re.compile(r"\b(book|schedule|appointment)\b", re.IGNORECASE)
        if booking_patterns.search(message):
            return SafetyClassification(
                intent=ConversationIntent.BOOK_APPOINTMENT,
                classification="administrative",
                confidence=min(0.6 + admin_matches * 0.1, 0.95),
                reasoning="Booking intent detected",
            )
        return SafetyClassification(
            intent=ConversationIntent.GENERAL_ENQUIRY,
            classification="administrative",
            confidence=min(0.5 + admin_matches * 0.1, 0.90),
            reasoning="Administrative enquiry detected",
        )

    return SafetyClassification(
        intent=ConversationIntent.UNKNOWN,
        classification="administrative",
        confidence=0.3,
        reasoning="No clear pattern matched",
    )
