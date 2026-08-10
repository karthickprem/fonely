"""Turn budget, repetition detection, and terminal closure enforcement.

Deterministic dialogue-state constraints that do not depend on the LLM
or provider calls.  These enforce the acceptance matrix turn limits,
prevent repeated questions, and produce deterministic terminal responses.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, time

from .context import DayAvailability


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

    def record_turn(self, assistant_text: str, asked_field: str | None = None) -> bool:
        """Record a turn. Returns False if terminal — caller must not deliver."""
        if self.terminal:
            return False
        self.turn_count += 1
        if asked_field:
            if asked_field in self.asked_fields:
                self.repeated_question_count += 1
            self.asked_fields.append(asked_field)
        self.last_assistant_text = assistant_text
        return True

    def _last_asked_field(self) -> str:
        return self.asked_fields[-1] if self.asked_fields else ""

    def is_over_budget(self) -> bool:
        return self.turn_count >= self.max_turns

    def has_repeated_question(self) -> bool:
        return self.repeated_question_count > 0

    def set_terminal(self, reason: str) -> None:
        self.terminal = True
        self.terminal_reason = reason


_BOOKING_REQUEST = re.compile(
    r"(?:appointment|அப்பாயிண்ட்மெண்ட்).*(?:book|புக்|வேணும்|வேண்டும்|பண்ணனும்|venum|pannanum)"
    r"|(?:book|புக்|pannanum).*(?:appointment|அப்பாயிண்ட்மெண்ட்)"
    r"|(?:doctor|டாக்டர்).*(?:பாக்கணும்|paakkanum)"
    r"|(?:scaling|cleaning|checkup|root canal|extraction|consultation).*(?:வேணும்|venum)",
    re.IGNORECASE,
)
_TIME = re.compile(
    r"(?<!\d)(?P<hour>\d{1,2})(?:[:.](?P<minute>[0-5]\d))?\s*"
    r"(?P<meridiem>am|pm)?\s*(?:மணி(?:க்கு)?)?(?=\s|[.,!?]|$)",
    re.IGNORECASE,
)
_VISIT_REASON = re.compile(
    r"வலி|வலிக்க|சொத்தை|pain|scaling|cleaning|checkup|root canal|consultation|treatment|filling",
    re.IGNORECASE,
)
_NAME = re.compile(r"(?:[A-Za-z][A-Za-z .'-]{0,79}|[஀-௿][஀-௿ .'-]{0,79})$")


@dataclass
class BookingCollection:
    """Non-authoritative caller candidates for one booking conversation.

    Date/time are retained only after matching caller input against the most
    recent typed availability result. They are candidates for the application
    seam, never authorization to propose or commit.
    """

    active: bool = False
    reason: str | None = None
    target_date: date | None = None
    selected_time: time | None = None
    patient_name: str | None = None

    @property
    def required_field(self) -> str | None:
        if not self.active:
            return None
        if self.target_date is None:
            return "date"
        if self.selected_time is None:
            return "time"
        if self.reason is None:
            return "reason"
        if self.patient_name is None:
            return "name"
        return "confirmation"

    def update(
        self,
        caller_text: str,
        *,
        resolved_date: date | None,
        availability: DayAvailability | None,
        previous_assistant_text: str = "",
    ) -> None:
        normalized = " ".join(caller_text.casefold().split())
        if _BOOKING_REQUEST.search(normalized):
            self.active = True

        if resolved_date is not None and resolved_date != self.target_date:
            self.target_date = resolved_date
            self.selected_time = None

        candidate_time = extract_booking_time(caller_text)
        if candidate_time is not None and availability is not None:
            offered = {
                slot.start_time
                for slot in availability.available_slots
                if slot.status.value == "available"
            }
            selected = _match_offered_time(candidate_time, offered)
            if selected is not None:
                self.selected_time = selected

        if self.active and self.reason is None and _VISIT_REASON.search(normalized):
            self.reason = caller_text.strip()

        if (
            self.active
            and self.patient_name is None
            and _assistant_asks_name(previous_assistant_text)
            and _NAME.fullmatch(caller_text.strip())
            and not _VISIT_REASON.search(normalized)
            and not _is_date_or_time_word(normalized)
        ):
            self.patient_name = caller_text.strip()

    @property
    def should_include_availability(self) -> bool:
        return self.target_date is not None

    def format_readback(self) -> str | None:
        if self.required_field != "confirmation":
            return None
        time_str = self.selected_time.strftime("%H:%M") if self.selected_time else "?"
        date_str = self.target_date.isoformat() if self.target_date else "?"
        return (
            f"{self.reason}, {date_str} {time_str}, {self.patient_name}. "
            f"இது correct-ஆ?"
        )

    def render(self) -> str:
        selected = self.selected_time.strftime("%H:%M") if self.selected_time else "missing"
        return (
            "<booking_collection>\n"
            f"active: {str(self.active).lower()}\n"
            f"reason: {self.reason or 'missing'}\n"
            f"target_date: {self.target_date.isoformat() if self.target_date else 'missing'}\n"
            f"selected_time: {selected}\n"
            f"patient_name: {self.patient_name or 'missing'}\n"
            f"required_field: {self.required_field or 'none'}\n"
            "Caller candidates only; this state cannot authorize availability, proposal, or commit.\n"
            "</booking_collection>"
        )


_TAMIL_NUMERALS = {
    "ஒன்று": 1, "ரெண்டு": 2, "மூன்று": 3, "நான்கு": 4,
    "ஐந்து": 5, "ஆறு": 6, "ஏழு": 7, "எட்டு": 8,
    "ஒன்பது": 9, "பத்து": 10, "பதினொன்று": 11, "பன்னிரெண்டு": 12,
}
_TAMIL_NUMERAL_RE = re.compile(
    r"(?:" + "|".join(re.escape(k) for k in sorted(_TAMIL_NUMERALS, key=len, reverse=True)) + r")"
    r"\s*(?:[:.](?P<tmin>[0-5]\d))?\s*(?:மணி(?:க்கு)?)?",
)


def extract_booking_time(text: str) -> time | None:
    match = _TIME.search(text)
    tamil_match = _TAMIL_NUMERAL_RE.search(text)

    if tamil_match is not None:
        matched_text = tamil_match.group(0)
        for k, v in sorted(_TAMIL_NUMERALS.items(), key=lambda x: -len(x[0])):
            if k in matched_text:
                hour = v
                minute = int(tamil_match.group("tmin") or 0)
                return time(hour, minute)

    if match is None:
        return None
    hour = int(match.group("hour"))
    minute = int(match.group("minute") or 0)
    meridiem = (match.group("meridiem") or "").casefold()
    if hour > 23 or (meridiem and hour > 12):
        return None
    if meridiem == "pm" and hour < 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    return time(hour, minute)


def _match_offered_time(candidate: time, offered: set[time]) -> time | None:
    if candidate in offered:
        complement = time((candidate.hour + 12) % 24, candidate.minute)
        if complement in offered:
            return None
        return candidate
    matches = {
        value
        for value in offered
        if value.minute == candidate.minute and value.hour % 12 == candidate.hour % 12
    }
    return next(iter(matches)) if len(matches) == 1 else None


_DATE_TIME_WORDS = frozenset({
    "today", "tomorrow", "innaikku", "innaiku", "naalaikku", "naalai",
    "இன்று", "இன்னைக்கு", "இன்னைக்கே", "நாளை", "நாளைக்கு",
    "morning", "evening", "காலை", "மாலை", "சாயங்காலம்",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "திங்கள்", "செவ்வாய்", "புதன்", "வியாழன்", "வெள்ளி", "சனி", "ஞாயிறு",
})


def _is_date_or_time_word(normalized: str) -> bool:
    return normalized.strip() in _DATE_TIME_WORDS or extract_booking_time(normalized) is not None


def _assistant_asks_name(text: str) -> bool:
    lower = text.casefold()
    return any(term in lower for term in ("name", "பேரு", "பெயர்", "நேம்"))


_MEDICAL_ADVICE = re.compile(
    r"\b(?:take|use|apply|need)\s+(?:paracetamol|ibuprofen|amoxicillin|crocin|combiflam|antibiotic)"
    r"|\b\d+\s*(?:mg|ml)\b.*(?:daily|twice|once|thrice)"
    r"|(?:root canal|extraction|filling|surgery|implant)\s+(?:தேவை|need|required|வேணும்)"
    r"|(?:you |நீங்க )?\s*need\s+(?:a |an )?(?:root canal|extraction|filling|surgery|implant)"
    r"|(?:could be|might be|probably)\s+(?:an? )?(?:infection|cavity|abscess|fracture)"
    r"|(?:இருக்கலாம்|தேவைப்படலாம்)\s*$",
    re.IGNORECASE,
)

SAFE_MEDICAL_REFERRAL = re.compile(
    r"doctor\s+(?:பார்|பாக்க|கிட்ட)"
    r"|clinic.*(?:call|contact|பண்ணு)"
    r"|staff\s+(?:கிட்ட|with)",
    re.IGNORECASE,
)


def contains_medical_advice(text: str) -> bool:
    return bool(_MEDICAL_ADVICE.search(text))


_BOOKING_SUCCESS = re.compile(
    r"(?:book(?:ing)?|appointment).*(?:confirm|confirmed|booked|saved|fixed|scheduled|ஆயிடுச்சு|உறுதி|பதிவு|aayiduchu|aagiduchu|panniten|pannitten)",
    re.IGNORECASE,
)

SAFE_NO_RECEIPT = "Details collect பண்ணிட்டேன், verify பண்றேன். சிறிது நேரம் காத்திருங்க."


def contains_booking_success(text: str) -> bool:
    return bool(_BOOKING_SUCCESS.search(text))


def gate_response(
    response: str,
    *,
    has_receipt: bool,
) -> tuple[str, bool]:
    """Receipt-keyed gate. Any turn, any phase, no receipt → no success language.

    Returns (gated_text, was_suppressed). The predicate is receipt
    existence, not conversation state. A receipt exists only because
    something committed — there is no other way to set it.
    """
    if contains_medical_advice(response):
        return "Doctor பார்த்துதான் சொல்ல முடியும். Appointment book பண்ணலாமா?", True
    if not has_receipt and contains_booking_success(response):
        return SAFE_NO_RECEIPT, True
    return response, False


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
