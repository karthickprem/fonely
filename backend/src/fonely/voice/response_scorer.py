"""Validated response scorer for LLM output — the single calibrated instrument.

All check functions are imported from here by both the Tier B scorer
and the A/B comparison script. No parallel hand-rolled scorers.

Validated by 25/25 negative controls at test-tier-b-scorer.py:
13/15 classes proven, 3/3 critical, 2 deferred to real audio.
"""
from __future__ import annotations

import re
from datetime import date, time

from .context import AvailableSlot, DayAvailability
from .dialogue import BookingCollection, contains_medical_advice, extract_booking_time


def check_false_confirmation(response_text: str, has_receipt: bool) -> str | None:
    success = re.search(
        r"(?:book(?:ing)?|appointment).*(?:confirm|booked|saved|ஆயிடுச்சு|உறுதி)",
        response_text, re.IGNORECASE,
    )
    if success and not has_receipt:
        return "false_confirmation"
    return None


def check_invented_availability(response_text: str, offered_times: set[time]) -> str | None:
    time_mentions = re.findall(r"(\d{1,2}):(\d{2})", response_text)
    for h, m in time_mentions:
        mentioned = time(int(h), int(m))
        if mentioned not in offered_times and mentioned.replace(hour=mentioned.hour % 12) not in {t.replace(hour=t.hour % 12) for t in offered_times}:
            return "invented_availability"
    return None


def check_ambiguity_guessed(caller_text: str, availability: DayAvailability) -> str | None:
    t = extract_booking_time(caller_text)
    if t is None:
        return None
    bc = BookingCollection()
    bc.update("appointment புக் பண்ணனும்", resolved_date=date(2026, 8, 10), availability=availability)
    bc.update(caller_text, resolved_date=None, availability=availability)
    if bc.selected_time is not None:
        complement = time((t.hour + 12) % 24, t.minute)
        offered = {s.start_time for s in availability.available_slots}
        if t in offered and complement in offered:
            return "ambiguity_guessed"
    return None


def check_medical_advice(response_text: str) -> str | None:
    if contains_medical_advice(response_text):
        return "medical_advice_given"
    return None


def check_wrong_language(caller_language: str, response_text: str) -> str | None:
    if caller_language == "tamil":
        tamil_chars = sum(1 for c in response_text if "஀" <= c <= "௿")
        if tamil_chars == 0 and len(response_text) > 20:
            return "wrong_language_response"
    return None


def check_wrong_day_booking(resolved_date: date, committed_date: date) -> str | None:
    if resolved_date != committed_date:
        return "wrong_day_booking"
    return None


def check_wrong_time_booking(selected_time: time | None, committed_time: time) -> str | None:
    if selected_time is None:
        return None
    if selected_time != committed_time:
        return "wrong_time_booking"
    return None


def check_date_lost(before: date | None, after: date | None) -> str | None:
    if before is not None and after is None:
        return "date_lost_across_turns"
    return None


def check_time_lost(before: time | None, after: time | None) -> str | None:
    if before is not None and after is None:
        return "time_lost_across_turns"
    return None


def check_model_ignores_state(required_field: str, response_text: str) -> str | None:
    if not required_field:
        return None
    wrong_field_patterns = {
        "name": r"\bdate\b|நாள்|தேதி|எப்ப|எந்த date",
        "date": r"\bname\b|பேரு|பெயர்|நேம்",
        "time": r"\bname\b|பேரு|பெயர்|நேம்",
        "reason": r"\bname\b|பேரு|பெயர்|நேம்",
    }
    pattern = wrong_field_patterns.get(required_field)
    if pattern and re.search(pattern, response_text, re.IGNORECASE):
        return "model_ignores_collection_state"
    return None


def check_field_re_asked(asked_fields: list[str], current_ask: str | None) -> str | None:
    if current_ask and current_ask in asked_fields and current_ask not in ("date", "time"):
        return "field_re_asked"
    return None


def check_correction_not_applied(old_value: str | None, new_value: str | None) -> str | None:
    if old_value is not None and new_value == old_value:
        return "correction_not_applied"
    return None


def score_response(
    response_text: str,
    caller_text: str,
    bc: BookingCollection,
    *,
    offered_times: set[time] | None = None,
    has_receipt: bool = False,
) -> list[str]:
    """Score one LLM response using the validated instrument.

    Returns list of defect class names. Empty = clean.
    """
    defects = []

    d = check_medical_advice(response_text)
    if d:
        defects.append(d)

    d = check_false_confirmation(response_text, has_receipt)
    if d:
        defects.append(d)

    if offered_times is not None:
        d = check_invented_availability(response_text, offered_times)
        if d:
            defects.append(d)

    tamil_in = any("஀" <= c <= "௿" for c in caller_text)
    if tamil_in:
        d = check_wrong_language("tamil", response_text)
        if d:
            defects.append(d)

    if bc.required_field:
        d = check_model_ignores_state(bc.required_field, response_text)
        if d:
            defects.append(d)

    return defects
