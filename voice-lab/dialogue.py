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
            "Answer the latest user request. This state cannot authorize facts or actions. "
            "When a booking_goal is active, booking_goal controls whether and how to resume collection.\n"
            "</dialogue_state>"
        )


@dataclass(frozen=True)
class BookingGoal:
    status: str
    reason: str | None
    preferred_date: str | None
    preferred_time: str | None
    patient_name: str | None
    required_field: str | None
    tangent_count: int
    turn_count: int

    @property
    def active(self) -> bool:
        return self.status in {"collecting", "awaiting_confirmation"}

    def render(self) -> str:
        return (
            "<booking_goal>\n"
            f"status: {self.status}\n"
            f"reason: {self.reason or 'missing'}\n"
            f"preferred_date: {self.preferred_date or 'missing'}\n"
            f"preferred_time: {self.preferred_time or 'missing'}\n"
            f"patient_name: {self.patient_name or 'missing'}\n"
            f"required_field: {self.required_field or 'none'}\n"
            f"tangent_count: {self.tangent_count}\n"
            f"turn_count: {self.turn_count}\n"
            "This is the active call goal, not authority to mutate or confirm a booking.\n"
            "If active, answer one caller tangent briefly, then ask exactly one question for required_field. "
            "Do not say goodbye or ask 'anything else' while required_field is present.\n"
            "If awaiting_confirmation, read back reason/date/time/name and ask for explicit confirmation.\n"
            "If status is abandoned, acknowledge that no booking was made and close without resuming collection.\n"
            "If status is handoff_required, stop collecting, say the booking was not completed, and direct the caller to clinic staff.\n"
            "If status is demo_complete, say the details were collected but not saved, advise clinic staff confirmation, then close.\n"
            "</booking_goal>"
        )


class BookingGoalTracker:
    """Maintain one bounded booking objective for the current voice session."""

    def __init__(self, max_turns: int = 12, max_tangents: int = 2):
        self._max_turns = max_turns
        self._max_tangents = max_tangents
        self._status = "inactive"
        self._reason: str | None = None
        self._date: str | None = None
        self._time: str | None = None
        self._name: str | None = None
        self._tangent_count = 0
        self._turn_count = 0

    def update(self, text: str, previous_assistant_text: str) -> BookingGoal:
        normalized = " ".join(text.casefold().split())
        was_active = self._status in {"collecting", "awaiting_confirmation"}

        if _contains_booking_request(normalized) and not was_active:
            self._reset()
            self._status = "collecting"
        if self._status == "inactive":
            return self.snapshot()

        self._turn_count += 1
        if _abandons_booking(normalized):
            self._status = "abandoned"
            return self.snapshot()

        if self._status == "awaiting_confirmation":
            if _rejects_confirmation(normalized):
                self._apply_correction(text, normalized)
                self._status = "collecting"
            elif _is_confirmation(normalized):
                self._status = "demo_complete"
                return self.snapshot()

        if self._reason is None and _contains_visit_reason(normalized):
            self._reason = text.strip()

        extracted_date = _extract_date(text)
        if extracted_date and extracted_date != self._date:
            self._date = extracted_date
            self._time = None

        extracted_time = _extract_time(
            text,
            previous_assistant_text,
            goal_requires_time=self._date is not None,
        )
        if extracted_time and (
            self._time is None or SLOT_OFFER.search(previous_assistant_text)
        ):
            self._time = extracted_time
        if self._name is None and _assistant_asks_name(previous_assistant_text):
            self._name = _extract_name(text)

        required_after = self._required_field()
        if was_active and _has_tangent_topic(text):
            self._tangent_count += 1
        if required_after == "confirmation":
            self._status = "awaiting_confirmation"
        if (
            self._status in {"collecting", "awaiting_confirmation"}
            and (self._turn_count >= self._max_turns or self._tangent_count > self._max_tangents)
        ):
            self._status = "handoff_required"
        return self.snapshot()

    def _reset(self) -> None:
        self._reason = None
        self._date = None
        self._time = None
        self._name = None
        self._tangent_count = 0
        self._turn_count = 0

    _NAME_CORRECTION = re.compile(
        r"(?:name\s+is|பேரு|பெயர்|நேம்)\s+(\S+)", re.IGNORECASE,
    )

    def _apply_correction(self, text: str, normalized: str) -> None:
        name_match = self._NAME_CORRECTION.search(text)
        if name_match:
            candidate = name_match.group(1).strip(" .,!?")
            if candidate and not NAME_REJECT.search(candidate):
                self._name = candidate
            else:
                self._name = None
        elif any(term in normalized for term in ("name", "பேரு", "பெயர்")):
            self._name = None
        elif any(term in normalized for term in ("reason", "service", "treatment", "scaling", "root canal", "extraction")):
            self._reason = None
            self._date = None
            self._time = None
            self._name = None
        elif any(term in normalized for term in ("date", "day", "நாள்", "தேதி")):
            self._date = None
            self._time = None
        else:
            self._time = None

    def _required_field(self) -> str | None:
        if self._status not in {"collecting", "awaiting_confirmation"}:
            return None
        if self._reason is None:
            return "reason"
        if self._date is None:
            return "preferred_date"
        if self._time is None:
            return "preferred_time"
        if self._name is None:
            return "patient_name"
        return "confirmation"

    def snapshot(self) -> BookingGoal:
        return BookingGoal(
            status=self._status,
            reason=self._reason,
            preferred_date=self._date,
            preferred_time=self._time,
            patient_name=self._name,
            required_field=self._required_field(),
            tangent_count=self._tangent_count,
            turn_count=self._turn_count,
        )


TOPIC_CHANGE = (
    "timing வேண்டாம்", "time வேண்டாம்", "வேற question", "வேற கேள்வி",
    "நான் என்ன கேக்குறேன்", "what i am asking", "மட்டும் சொல்லுங்க",
)
REPAIR = ("புரியல", "புரிஞ்சுக்கல", "understand", "wrong answer", "வேற கேக்குறேன்")
PROCEDURE = ("procedure", "எப்படி book", "how to book", "booking process")
BOOKING = (
    "appointment வேணும்", "appointment பண்ணனும்", "appointment வேண்டும்",
    "appointment புக்", "அப்பாயிண்ட்மெண்ட் புக்",
    "book பண்ண", "book பண்ணனும்", "புக் பண்ண", "புக் பண்ணனும்",
    "slot வேணும்", "available-ஆ", "availability",
    "i want to book", "i'd like to book",
)
_BOOKING_NEED = re.compile(
    r"\bi need (?:a|an)\s+(?:\S+\s+){1,3}(?:appointment|booking)\b", re.IGNORECASE,
)
GENERAL_QUESTION = (
    "what are", "what is", "என்ன", "எது", "பேர் என்ன", "types of teeth",
    "different teeth", "canine", "molar", "premolar", "incisor",
)
VISIT_REASON = (
    "pain", "வலி", "வலிக்க", "clean", "cleaning", "scaling", "root canal",
    "checkup", "consultation", "braces", "extraction", "teeth whitening",
    "general", "filling", "crown", "denture", "implant", "orthodontic",
)
BOOKING_ABANDON = (
    "appointment வேண்டாம்", "booking வேண்டாம்", "book வேண்டாம்", "cancel booking",
    "don't book", "do not book", "வேண்டாம் விடுங்க",
    "never mind", "forget it", "stop", "bye", "விடுங்க",
)
TANGENT = (
    "insurance", "claim", "clinic எங்க", "location", "address", "fee", "price",
    "cost", "parking", "payment", "card accept",
)
_MONTHS_LIST = ["january", "february", "march", "april", "may", "june",
                "july", "august", "september", "october", "november", "december"]
_MONTHS = "|".join(_MONTHS_LIST)
_MONTH_DAYS = {1: 31, 2: 29, 3: 31, 4: 30, 5: 31, 6: 30, 7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}
_RELATIVE_DATE = re.compile(
    r"\b(?:today|tomorrow|day after tomorrow|monday|tuesday|wednesday|thursday|friday|saturday)\b"
    r"|இன்னைக்கு|இன்று|நாளைக்கு|நாளை மறுநாள்|திங்கள்|செவ்வாய்|புதன்|வியாழன்|வெள்ளி|சனி",
    re.IGNORECASE,
)
_EXPLICIT_DATE_DMY = re.compile(
    rf"\b(?P<day>[1-9]|[12]\d|3[01])\s*(?:st|nd|rd|th)?\s*(?P<month>{_MONTHS})(?:\s+(?P<year>\d{{4}}))?\b",
    re.IGNORECASE,
)
_EXPLICIT_DATE_MDY = re.compile(
    rf"\b(?P<month>{_MONTHS})\s*(?P<day>[1-9]|[12]\d|3[01])(?:\s+(?P<year>\d{{4}}))?\b",
    re.IGNORECASE,
)
_PHONE_PATTERN = re.compile(r"\b\d{5,}\b")
NAME_PROMPT = ("name", "patient name", "பேரு", "பெயர்", "நேம்", "பேஷன்ட் நேம்")
NAME_REJECT = re.compile(r"(?:insurance|claim|appointment|slot|time|pain|வலி|fee|price|cost|parking|location|எவ்வளவு|எங்க)", re.IGNORECASE)
CONFIRMATION = ("yes", "confirm", "correct", "ஆமா", "ஆம்", "சரி")
_CONFIRM_PHRASES = re.compile(
    r"^(?:yes,?\s*that'?s?\s*correct|ஆம்,?\s*சரி|correct-ஆ|சரிங்க)",
    re.IGNORECASE,
)


SLOT_SELECTION = re.compile(
    r"^\s*(?:\b(?:10(?::00)?|11(?::00)?|5(?::00)?|6[:.]?30|7[:.]?30)\b|morning|evening|காலை|சாயங்காலம்)"
    r"(?:\s*(?:am|pm|slot|time|மணி))?\s*(?:works?|okay|ok|convenient|please|வேணும்|போதும்|பரவாயில்லை)?\s*[.!?]?\s*$",
    re.IGNORECASE,
)
SLOT_OFFER = re.compile(
    r"(?:\bslots?\b|\bavailable\b|\bavailability\b|எந்த time convenient|எந்த நேரம்|வரலாம்)",
    re.IGNORECASE,
)


def _contains_booking_request(text: str) -> bool:
    return any(term in text for term in BOOKING) or bool(_BOOKING_NEED.search(text))


def _abandons_booking(text: str) -> bool:
    return any(term in text for term in BOOKING_ABANDON)


def _contains_visit_reason(text: str) -> bool:
    return any(term in text for term in VISIT_REASON)


def _validate_explicit_date(day: int, month_name: str, year: int | None) -> str | None:
    month_idx = _MONTHS_LIST.index(month_name.casefold()) + 1
    max_day = _MONTH_DAYS[month_idx]
    if month_idx == 2 and day == 29:
        if year is not None and (year % 4 != 0 or (year % 100 == 0 and year % 400 != 0)):
            return None
    if day > max_day:
        return None
    if year is not None:
        return f"{day} {month_name.capitalize()} {year}"
    return f"{day} {month_name.capitalize()}"


def _extract_date(text: str) -> str | None:
    if _PHONE_PATTERN.search(text):
        cleaned = _PHONE_PATTERN.sub("", text)
    else:
        cleaned = text
    rel = _RELATIVE_DATE.search(cleaned)
    if rel:
        return rel.group(0)
    for pat in (_EXPLICIT_DATE_DMY, _EXPLICIT_DATE_MDY):
        m = pat.search(cleaned)
        if m:
            day = int(m.group("day"))
            month = m.group("month")
            year_str = m.group("year")
            year = int(year_str) if year_str else None
            validated = _validate_explicit_date(day, month, year)
            if validated:
                return validated
    return None


def _extract_time(
    text: str,
    previous_assistant_text: str,
    *,
    goal_requires_time: bool = False,
) -> str | None:
    if not goal_requires_time and not SLOT_OFFER.search(previous_assistant_text):
        return None
    numeric_time = r"(?:10(?::00)?|11(?::00)?|12(?::00)?|0?5(?::00)?|6[:.]?30|7[:.]?30)"
    match = re.search(
        rf"(?<!\d){numeric_time}\s*(?:am|pm|மணி(?:க்கு)?)(?=\s|[.,!?]|$)"
        rf"|(?<!\d){numeric_time}(?=\s|[.,!?]|$)"
        r"|\b(?:morning|evening)\b|காலை|சாயங்காலம்",
        text,
        re.IGNORECASE,
    )
    if match is None:
        return None
    return re.sub(r"மணிக்கு$", "மணி", match.group(0), flags=re.IGNORECASE)


def _assistant_asks_name(text: str) -> bool:
    lower = text.casefold()
    return any(term in lower for term in NAME_PROMPT)


def _assistant_asks_date(text: str) -> bool:
    lower = text.casefold()
    return any(
        term in lower
        for term in (
            "which date",
            "what date",
            "which day",
            "date-ல",
            "date ல",
            "எந்த date",
            "எந்த நாள்",
            "எப்ப",
            "தேதி",
        )
    )


def _extract_name(text: str) -> str | None:
    candidate = " ".join(text.split()).strip(" .,!?")
    if not candidate or len(candidate) > 80 or NAME_REJECT.search(candidate):
        return None
    return candidate


def _has_tangent_topic(text: str) -> bool:
    lower = text.casefold()
    return any(term in lower for term in TANGENT)


def _normalize_for_match(text: str) -> str:
    return text.strip(" .,!?;:'\"").casefold()


def _is_confirmation(text: str) -> bool:
    cleaned = _normalize_for_match(text)
    if any(term == cleaned or cleaned.startswith(f"{term} ") for term in CONFIRMATION):
        return not _REJECT_PATTERN.search(text)
    return bool(_CONFIRM_PHRASES.match(cleaned))


_REJECT_PATTERN = re.compile(r"\b(no|wrong|change|மாத்த)\b|இல்ல", re.IGNORECASE)


def _rejects_confirmation(text: str) -> bool:
    return bool(_REJECT_PATTERN.search(text))


def classify_dialogue_act(text: str, previous_assistant_text: str = "") -> DialogueState:
    lower = " ".join(text.casefold().split())
    if any(term in lower for term in TOPIC_CHANGE):
        return DialogueState("topic_change", False, True)
    if any(term in lower for term in REPAIR):
        return DialogueState("repair", False, True)
    if any(term in lower for term in PROCEDURE):
        return DialogueState("booking_procedure", False, True)
    if SLOT_OFFER.search(previous_assistant_text) and SLOT_SELECTION.search(text):
        return DialogueState("slot_selection", True, False)
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
