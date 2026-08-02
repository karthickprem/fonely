"""Deterministic safety classifier — runs BEFORE LLM, cannot be overridden."""

import re

URGENT_PATTERNS = [
    re.compile(r"\b(emergency|unconscious|choking|heavy bleeding|severe pain|can'?t breathe|major swelling|accident|trauma)\b", re.I),
    re.compile(r"(ரத்தம் நிற்கல|மூச்சு விட முடிய|கடுமை வீக்கம்|விபத்து)"),
]

MEDICAL_PATTERNS = [
    re.compile(r"\b(symptom|medicine|medication|dosage|x-?ray|diagnosis|prescription|side effect|after surgery|is this normal|infection)\b", re.I),
    re.compile(r"\bwhat (medicine|tablet|pill|drug)\b", re.I),
    re.compile(r"\bshould i take\b", re.I),
    re.compile(r"\bis it (cancer|serious|dangerous)\b", re.I),
    re.compile(r"(மருந்து|normal-ஆ இருக்கா|என்ன நோய்)"),
]

URGENT_RESPONSE_TA = (
    "இது urgent-ஆ இருக்கலாம். உடனே hospital போங்க அல்லது emergency services-ஐ call பண்ணுங்க."
)
URGENT_RESPONSE_EN = (
    "This may be urgent. Please seek immediate medical care or call emergency services."
)

MEDICAL_RESPONSE_TA = (
    "நான் medical advice தர முடியாது. Clinic-ஐ நேரடியாக call பண்ணுங்க."
)
MEDICAL_RESPONSE_EN = (
    "I cannot give medical advice. Please call the clinic directly for help."
)


def classify(text: str) -> dict | None:
    """Check user text for safety issues. Returns None if safe."""
    for p in URGENT_PATTERNS:
        if p.search(text):
            return {
                "type": "urgent",
                "response_ta": URGENT_RESPONSE_TA,
                "response_en": URGENT_RESPONSE_EN,
            }
    for p in MEDICAL_PATTERNS:
        if p.search(text):
            return {
                "type": "medical",
                "response_ta": MEDICAL_RESPONSE_TA,
                "response_en": MEDICAL_RESPONSE_EN,
            }
    return None
