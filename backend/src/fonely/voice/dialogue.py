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


_REQUESTING_VERBS = r"(?:book|புக்|வேணும்|வேண்டும்|பண்ணனும்|venum|pannanum|போடணும்|podanum|எடுக்கணும்|fix|need|want|schedule)"
# Tamil-script transliterations real Sarvam STT emits when a Tamil caller says
# an English service word ("cleaning" → "கிளீனிங்"). Defined ONCE and shared by
# every service-recognizing regex below — booking activation (_SERVICES),
# reason capture (_VISIT_REASON) — so the three can never drift apart. The
# STT-on-audio proof showed that a form present in one but missing from another
# silently broke booking activation while the readback still looked right.
_TAMIL_SERVICE_FORMS = (
    r"கிளீனிங்|கிளீனிங|க்ளீனிங்|ஸ்கேலிங்|ஸ்கேலிங|செக்கப்|கன்சல்ட|"
    r"ரூட்\s*கேனால்|ரூட்கேனால்|ஃபில்லிங்|பில்லிங்|பிரேஸ்|எக்ஸ்ட்ராக்ஷன்"
)
_SERVICES = (
    r"(?:scaling|cleaning|checkup|root\s*canal|extraction|consultation|filling|treatment|"
    + _TAMIL_SERVICE_FORMS + r")"
)
_APPOINTMENT = r"(?:appointment|அப்பாயிண்ட்மெண்ட்)"

_BOOKING_ACTIVATORS: list[re.Pattern[str]] = [
    # appointment + requesting verb (either order)
    re.compile(_APPOINTMENT + r".*" + _REQUESTING_VERBS, re.IGNORECASE),
    re.compile(_REQUESTING_VERBS + r".*" + _APPOINTMENT, re.IGNORECASE),
    # English intent: "I need/want/schedule a/an (dental) appointment"
    re.compile(r"(?:need|want|schedule|get|make)\s+(?:a\s+|an\s+)?(?:dental\s+)?" + _APPOINTMENT, re.IGNORECASE),
    # Doctor visit intent
    re.compile(r"(?:doctor|டாக்டர்|dentist).*(?:பாக்கணும்|paakkanum|போகணும்|poganum|visit|appointment)", re.IGNORECASE),
    # Service + requesting verb
    re.compile(_SERVICES + r".*" + _REQUESTING_VERBS, re.IGNORECASE),
    re.compile(_REQUESTING_VERBS + r".*" + _SERVICES, re.IGNORECASE),
]
_TIME = re.compile(
    r"(?<!\d)(?P<hour>\d{1,2})(?:[:.](?P<minute>[0-5]\d))?\s*"
    r"(?P<meridiem>am|pm)?\s*(?:மணி(?:க்கு)?)?(?=\s|[.,!?]|$)",
    re.IGNORECASE,
)
# Reason capture shares the same Tamil-script service forms as booking
# activation (_TAMIL_SERVICE_FORMS) plus pain/symptom words. The captured
# phrase is later resolved to a real service_id through
# clinic_resolver._SERVICE_ALIASES (which carries the same forms). Without the
# Tamil-script forms here, `reason` stayed None on real audio, required_field
# never reached "confirmation", and the booking could never commit — even
# though the readback looked right. Found by the STT-on-audio proof.
_VISIT_REASON = re.compile(
    r"வலி|வலிக்க|சொத்தை|pain|scaling|cleaning|checkup|root canal|consultation|treatment|filling"
    r"|" + _TAMIL_SERVICE_FORMS,
    re.IGNORECASE,
)
_NAME = re.compile(r"(?:[A-Za-z][A-Za-z .'-]{0,79}|[஀-௿][஀-௿ .'-]{0,79})$")


_SERVICE_NAMES = {
    "scaling": "Scaling", "cleaning": "Cleaning", "checkup": "Checkup",
    "root canal": "Root canal", "extraction": "Extraction",
    "filling": "Filling", "consultation": "Consultation", "treatment": "Treatment",
}


def _normalize_service(raw_reason: str) -> str:
    lower = raw_reason.lower()
    for key, display in _SERVICE_NAMES.items():
        if key in lower:
            return display
    return raw_reason


def _format_spoken_date(d: date) -> str:
    months = ["January", "February", "March", "April", "May", "June",
              "July", "August", "September", "October", "November", "December"]
    return f"{months[d.month - 1]} {d.day}"


def _format_spoken_time(t: time) -> str:
    hour_12 = t.hour % 12 or 12
    minute_str = f":{t.minute:02d}" if t.minute else ""
    if 5 <= t.hour < 12:
        period = "காலை"
    elif 12 <= t.hour < 17:
        period = "மதியம்"
    elif 17 <= t.hour < 21:
        period = "மாலை"
    else:
        period = ""
    return f"{period} {hour_12}{minute_str}".strip()


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
        if any(p.search(normalized) for p in _BOOKING_ACTIVATORS):
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

    def format_readback(self, lang: str = "ta-Latn") -> str | None:
        if self.required_field != "confirmation":
            return None
        # Facts are language-neutral: service (English display name), date
        # (English month), and the time VALUE are identical across languages.
        # Only the time-period word and the trailing question mirror the caller.
        # This keeps the readback's date/time byte-identical to what commit
        # uses — a readback whose facts drifted per language would book wrong.
        from .language import format_time_spoken, get_response
        service = _normalize_service(self.reason) if self.reason else "?"
        date_str = _format_spoken_date(self.target_date) if self.target_date else "?"
        time_str = format_time_spoken(self.selected_time, lang) if self.selected_time else "?"
        tail = get_response("readback_tail", lang)
        return f"{service}, {date_str} {time_str}, {self.patient_name}. {tail}"

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


# Tamil / Tanglish / English period-of-day words that carry a PM meaning. The
# _TIME regex only understands the Latin "am/pm"; a Tamil caller never says
# "pm" — they say "மாலை 7:30" (evening 7:30 = 19:30) or "இரவு எட்டு" (night 8).
# Real Sarvam STT also emits word-order variants ("7:30 அந்தி மாலை"), so this
# is a whole-text substring scan, not a positional match. WITHOUT this, evening
# times silently book at the morning hour — the caller asks for 7:30 evening
# and the row lands at 07:30. Each form here was seen from real STT or is a
# common spoken variant.
_PM_PERIOD_MARKERS = (
    "மாலை", "அந்தி", "சாயங்கால", "இரவு", "ராத்திரி", "ராத்ரி", "மதியம்", "மத்தியான",
    "maalai", "malai", "andhi", "saayangaalam", "iravu", "raathiri", "raatri",
    "madhiyam", "mathiyam", "evening", "night", "afternoon",
)
# Morning words: force a spoken "12" back to 00 only when explicitly morning.
_AM_PERIOD_MARKERS = ("காலை", "kaalai", "kalai", "morning")


def _apply_period_marker(hour: int, text: str) -> int:
    """Shift a 1–12 hour to PM when the surrounding text carries an evening /
    night / afternoon marker. Noon '12' with such a marker stays 12 (midday),
    never 24. Morning '12' becomes 0. This is the single place the Tamil period
    word turns into 24-hour time, applied to both extraction branches."""
    low = text.casefold()
    if any(m in low for m in _PM_PERIOD_MARKERS) and 1 <= hour <= 11:
        return hour + 12
    if any(m in low for m in _AM_PERIOD_MARKERS) and hour == 12:
        return 0
    return hour


def extract_booking_time(text: str) -> time | None:
    match = _TIME.search(text)
    tamil_match = _TAMIL_NUMERAL_RE.search(text)

    if tamil_match is not None:
        matched_text = tamil_match.group(0)
        for k, v in sorted(_TAMIL_NUMERALS.items(), key=lambda x: -len(x[0])):
            if k in matched_text:
                hour = _apply_period_marker(v, text)
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
    elif not meridiem:
        # No Latin am/pm — let a Tamil/Tanglish period word decide.
        hour = _apply_period_marker(hour, text)
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
    return any(term in lower for term in (
        "name", "பேரு", "பெயர்", "பெயர", "நேம்", "நேம",
    ))


_DRUG_NAMES = r"(?:paracetamol|ibuprofen|amoxicillin|crocin|combiflam|antibiotic|dolo(?:\s*\d+)?|meftal|brufen)"
_MEDICAL_ADVICE = re.compile(
    r"\b(?:take|use|apply|need)\s+" + _DRUG_NAMES
    + r"|\b" + _DRUG_NAMES + r"\s+(?:எடு|எடுக்க|எடுத்து|போட|போடு|சாப்பிடு|குடி)"
    + r"|(?:எடு|எடுக்க|எடுத்து|போட|போடு|சாப்பிடு)\w*\s+" + _DRUG_NAMES
    + r"|\b\d+\s*(?:mg|ml)\b.*(?:daily|twice|once|thrice)"
    + r"|(?:root canal|extraction|filling|surgery|implant)\s+(?:தேவை|need|required|வேணும்)"
    + r"|(?:you |நீங்க )?\s*need\s+(?:a |an )?(?:root canal|extraction|filling|surgery|implant)"
    + r"|(?:could be|might be|probably)\s+(?:an? )?(?:infection|cavity|abscess|fracture)"
    + r"|(?:இருக்கலாம்|தேவைப்படலாம்)\s*$",
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
    r"(?:book(?:ing)?|appointment).*(?:confirm|confirmed|booked|saved|fixed|scheduled|ஆயிடுச்சு|உறுதி|பதிவு|aayiduchu|aagiduchu|panniten|pannitten)"
    r"|(?:confirm|confirmed|saved|fixed|scheduled|booked).*(?:book(?:ing)?|appointment)"
    r"|(?:confirm|confirmed|booked|saved) ஆயிடுச்சு",
    re.IGNORECASE,
)

# BEHAVIOR-AFFECTING: this string changes the conversation history the
# model sees on subsequent turns, measurably altering downstream eagerness
# (raw false-confirmation rate narrowed from 30% to 15% when engaged).
# Do NOT edit for tone without re-measuring downstream effects.
# "சிறிது நேரம் காத்திருங்க" tells the caller to wait — verify what
# actually follows a suppressed turn before shipping. Needs native review.
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
