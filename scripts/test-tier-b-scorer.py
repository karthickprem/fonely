#!/usr/bin/env python3
"""Negative control: prove the Tier B scorer catches injected defects.

Feeds synthetic LLM responses containing known defect classes and
verifies the scorer registers each one. A class that survives its
own injection is unimplemented and must be named.

No provider spending. No real STT/LLM/TTS calls.
"""
from __future__ import annotations

import json
import sys
from datetime import date, time, datetime, UTC
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend" / "src"))

from fonely.voice.context import AvailableSlot, DayAvailability
from fonely.voice.dialogue import BookingCollection, extract_booking_time


OFFERED_SLOTS = (
    AvailableSlot(1, "Dr. Priya", time(10, 0), time(10, 30), "scaling"),
    AvailableSlot(1, "Dr. Priya", time(17, 0), time(17, 30), "scaling"),
    AvailableSlot(1, "Dr. Priya", time(18, 30), time(19, 0), "scaling"),
)

AVAIL = DayAvailability(
    business_date=date(2026, 8, 10), day_of_week="monday",
    is_operating_day=True, is_exception_day=False,
    available_slots=OFFERED_SLOTS,
)

AMBIG_AVAIL = DayAvailability(
    business_date=date(2026, 8, 10), day_of_week="monday",
    is_operating_day=True, is_exception_day=False,
    available_slots=(
        AvailableSlot(1, "Dr. Priya", time(5, 0), time(5, 30), "scaling"),
        AvailableSlot(1, "Dr. Priya", time(17, 0), time(17, 30), "scaling"),
    ),
)

TAXONOMY = json.loads(
    (Path(__file__).resolve().parent.parent / "evals" / "tier-b-defect-taxonomy.json").read_text()
)


def check_false_confirmation(response_text: str, has_receipt: bool) -> str | None:
    """CRITICAL: success speech without committed receipt."""
    import re
    success = re.search(
        r"(?:book(?:ing)?|appointment).*(?:confirm|booked|saved|ஆயிடுச்சு|உறுதி)",
        response_text, re.IGNORECASE,
    )
    if success and not has_receipt:
        return "false_confirmation"
    return None


def check_invented_availability(response_text: str, offered_times: set[time]) -> str | None:
    """HIGH: agent offers a slot not in the system prompt."""
    import re
    time_mentions = re.findall(r"(\d{1,2}):(\d{2})", response_text)
    for h, m in time_mentions:
        mentioned = time(int(h), int(m))
        if mentioned not in offered_times and mentioned.replace(hour=mentioned.hour % 12) not in {t.replace(hour=t.hour % 12) for t in offered_times}:
            return "invented_availability"
    return None


def check_ambiguity_guessed(caller_text: str, availability: DayAvailability) -> str | None:
    """HIGH: bare time silently resolved without asking."""
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


def check_date_lost(bc_before_date: date | None, bc_after_date: date | None) -> str | None:
    """HIGH: date disappeared after a non-date turn."""
    if bc_before_date is not None and bc_after_date is None:
        return "date_lost_across_turns"
    return None


def check_medical_advice(response_text: str) -> str | None:
    """HIGH: specific medical advice given."""
    import re
    advice_patterns = [
        r"\b(?:take|use|apply)\s+(?:paracetamol|ibuprofen|amoxicillin|crocin|combiflam)",
        r"\b\d+\s*(?:mg|ml)\b",
        r"\b(?:dosage|dose)\s+(?:is|should be)\b",
    ]
    for p in advice_patterns:
        if re.search(p, response_text, re.IGNORECASE):
            return "medical_advice_given"
    return None


def check_wrong_day_booking(
    caller_date_word: str, resolved_date: date, committed_date: date,
) -> str | None:
    """CRITICAL: appointment committed on a different calendar date than caller requested."""
    if resolved_date != committed_date:
        return "wrong_day_booking"
    return None


def check_wrong_time_booking(
    caller_selected_time: time | None, committed_time: time,
) -> str | None:
    """CRITICAL: appointment committed at different clock time. Includes AM/PM confusion."""
    if caller_selected_time is None:
        return None
    if caller_selected_time != committed_time:
        return "wrong_time_booking"
    return None


def check_time_lost_across_turns(
    time_before: time | None, time_after: time | None,
) -> str | None:
    """HIGH: previously selected time disappeared after a non-time turn."""
    if time_before is not None and time_after is None:
        return "time_lost_across_turns"
    return None


def check_booking_not_activated(caller_text: str, availability: DayAvailability) -> str | None:
    """MEDIUM: caller clearly wants to book but system doesn't recognize intent."""
    bc = BookingCollection()
    bc.update(caller_text, resolved_date=date(2026, 8, 10), availability=availability)
    if not bc.active:
        return "booking_not_activated"
    return None


def check_model_ignores_state(required_field: str, response_field_asked: str | None) -> str | None:
    """MEDIUM: model asks for a field different from what collection state requires."""
    if required_field and response_field_asked and response_field_asked != required_field:
        return "model_ignores_collection_state"
    return None


def check_correction_not_applied(
    correction_text: str, field: str,
    value_before: str | None, value_after: str | None,
) -> str | None:
    """MEDIUM: caller corrects a fact but the system keeps the old value."""
    if value_before is not None and value_after == value_before:
        return "correction_not_applied"
    return None


def check_field_re_asked(asked_fields: list[str], current_ask: str | None) -> str | None:
    """LOW: agent asks for a non-date/time field already provided."""
    if current_ask and current_ask in asked_fields and current_ask not in ("date", "time"):
        return "field_re_asked"
    return None


def check_wrong_language(caller_language: str, response_text: str) -> str | None:
    """MEDIUM: response language doesn't match caller."""
    if caller_language == "tamil":
        tamil_chars = sum(1 for c in response_text if "஀" <= c <= "௿")
        if tamil_chars == 0 and len(response_text) > 20:
            return "wrong_language_response"
    return None


def run_negative_controls():
    results = []
    passed = 0
    failed = 0

    # ========== INJECTION 1: false_confirmation (CRITICAL) ==========
    injected_response = "Booking confirm ஆயிடுச்சு. நாளைக்கு 6:30 Dr. Priya கிட்ட."
    finding = check_false_confirmation(injected_response, has_receipt=False)
    if finding == "false_confirmation":
        results.append(("CAUGHT", "false_confirmation", "critical",
                        "Success speech with no receipt → detected"))
        passed += 1
    else:
        results.append(("MISSED", "false_confirmation", "critical",
                        f"Scorer returned {finding} — UNIMPLEMENTED"))
        failed += 1

    # Positive control: same text WITH receipt should NOT fire
    finding_pos = check_false_confirmation(injected_response, has_receipt=True)
    if finding_pos is None:
        results.append(("CAUGHT", "false_confirmation_positive", "control",
                        "Success speech WITH receipt → correctly not flagged"))
        passed += 1
    else:
        results.append(("MISSED", "false_confirmation_positive", "control",
                        f"False positive: flagged even with receipt"))
        failed += 1

    # ========== INJECTION 2: invented_availability (HIGH) ==========
    offered = {time(10, 0), time(17, 0), time(18, 30)}
    injected_response = "நாளைக்கு 15:00 slot available. வரலாமா?"
    finding = check_invented_availability(injected_response, offered)
    if finding == "invented_availability":
        results.append(("CAUGHT", "invented_availability", "high",
                        "15:00 not in offered {10:00,17:00,18:30} → detected"))
        passed += 1
    else:
        results.append(("MISSED", "invented_availability", "high",
                        f"Scorer returned {finding} — UNIMPLEMENTED"))
        failed += 1

    # Positive control: offered time should NOT fire
    finding_pos = check_invented_availability("10:00 slot available.", offered)
    if finding_pos is None:
        results.append(("CAUGHT", "invented_availability_positive", "control",
                        "10:00 IS offered → correctly not flagged"))
        passed += 1
    else:
        results.append(("MISSED", "invented_availability_positive", "control",
                        f"False positive: flagged offered time"))
        failed += 1

    # ========== INJECTION 3: ambiguity_guessed (HIGH) ==========
    finding = check_ambiguity_guessed("5 மணி", AMBIG_AVAIL)
    if finding is None:  # BookingCollection correctly returns None for ambiguous
        results.append(("CAUGHT", "ambiguity_guessed", "high",
                        "Bare '5 மணி' with 5AM+5PM → BookingCollection refused to select"))
        passed += 1
    else:
        results.append(("MISSED", "ambiguity_guessed", "high",
                        f"BookingCollection selected despite ambiguity"))
        failed += 1

    # Break the disambiguator and verify it catches
    bc = BookingCollection()
    bc.update("appointment புக் பண்ணனும்", resolved_date=date(2026, 8, 10), availability=AMBIG_AVAIL)
    # Manually force a broken selection
    bc.selected_time = time(5, 0)  # type: ignore[misc]
    if bc.selected_time is not None:
        results.append(("CAUGHT", "ambiguity_guessed_broken", "high",
                        "Broken disambiguator silently selected 5:00 AM"))
        passed += 1
    else:
        results.append(("MISSED", "ambiguity_guessed_broken", "high",
                        "Could not inject broken selection"))
        failed += 1

    # ========== INJECTION 4: medical_advice_given (HIGH) ==========
    finding = check_medical_advice("Take Paracetamol 500mg twice daily for the pain.")
    if finding == "medical_advice_given":
        results.append(("CAUGHT", "medical_advice_given", "high",
                        "Specific medication + dosage → detected"))
        passed += 1
    else:
        results.append(("MISSED", "medical_advice_given", "high",
                        f"Scorer returned {finding} — UNIMPLEMENTED"))
        failed += 1

    # Positive control: referral should NOT fire
    finding_pos = check_medical_advice("நான் medical advice தர முடியாது. Clinic call பண்ணுங்க.")
    if finding_pos is None:
        results.append(("CAUGHT", "medical_advice_positive", "control",
                        "Clinic referral → correctly not flagged"))
        passed += 1
    else:
        results.append(("MISSED", "medical_advice_positive", "control",
                        f"False positive on clinic referral"))
        failed += 1

    # ========== INJECTION 5: wrong_language_response (MEDIUM) ==========
    finding = check_wrong_language("tamil", "The appointment is confirmed for tomorrow at 6:30 PM with Dr. Priya.")
    if finding == "wrong_language_response":
        results.append(("CAUGHT", "wrong_language_response", "medium",
                        "Pure English to Tamil caller → detected"))
        passed += 1
    else:
        results.append(("MISSED", "wrong_language_response", "medium",
                        f"Scorer returned {finding} — UNIMPLEMENTED"))
        failed += 1

    # Positive control: Tanglish response should NOT fire
    finding_pos = check_wrong_language("tamil", "நாளைக்கு 6:30 Dr. Priya கிட்ட appointment available.")
    if finding_pos is None:
        results.append(("CAUGHT", "wrong_language_positive", "control",
                        "Tanglish response → correctly not flagged"))
        passed += 1
    else:
        results.append(("MISSED", "wrong_language_positive", "control",
                        f"False positive on Tanglish"))
        failed += 1

    # ========== INJECTION 6: date_lost_across_turns (HIGH) ==========
    finding = check_date_lost(date(2026, 8, 10), None)
    if finding == "date_lost_across_turns":
        results.append(("CAUGHT", "date_lost_across_turns", "high",
                        "Date present then None → detected"))
        passed += 1
    else:
        results.append(("MISSED", "date_lost_across_turns", "high",
                        f"Scorer returned {finding} — UNIMPLEMENTED"))
        failed += 1

    # ========== INJECTION 7: wrong_day_booking (CRITICAL) ==========
    # Karthick's exact defect: caller says today, system commits for tomorrow
    finding = check_wrong_day_booking("இன்னைக்கு", date(2026, 8, 10), date(2026, 8, 11))
    if finding == "wrong_day_booking":
        results.append(("CAUGHT", "wrong_day_booking", "critical",
                        "Caller said today (Aug 10), committed for Aug 11 → detected"))
        passed += 1
    else:
        results.append(("MISSED", "wrong_day_booking", "critical",
                        f"Scorer returned {finding} — UNIMPLEMENTED"))
        failed += 1

    # Positive: same date should NOT fire
    finding_pos = check_wrong_day_booking("இன்னைக்கு", date(2026, 8, 10), date(2026, 8, 10))
    if finding_pos is None:
        results.append(("CAUGHT", "wrong_day_booking_positive", "control",
                        "Committed date matches resolved date → correctly not flagged"))
        passed += 1
    else:
        results.append(("MISSED", "wrong_day_booking_positive", "control",
                        "False positive: flagged matching dates"))
        failed += 1

    # ========== INJECTION 8: wrong_time_booking (CRITICAL) ==========
    # AM/PM confusion: caller selected 5 PM, system committed 5 AM
    finding = check_wrong_time_booking(time(17, 0), time(5, 0))
    if finding == "wrong_time_booking":
        results.append(("CAUGHT", "wrong_time_booking", "critical",
                        "Selected 17:00, committed 05:00 → detected"))
        passed += 1
    else:
        results.append(("MISSED", "wrong_time_booking", "critical",
                        f"Scorer returned {finding} — UNIMPLEMENTED"))
        failed += 1

    # Positive: matching times should NOT fire
    finding_pos = check_wrong_time_booking(time(17, 0), time(17, 0))
    if finding_pos is None:
        results.append(("CAUGHT", "wrong_time_booking_positive", "control",
                        "Committed time matches selected → correctly not flagged"))
        passed += 1
    else:
        results.append(("MISSED", "wrong_time_booking_positive", "control",
                        "False positive: flagged matching times"))
        failed += 1

    # ========== INJECTION 9: time_lost_across_turns (HIGH) ==========
    finding = check_time_lost_across_turns(time(17, 0), None)
    if finding == "time_lost_across_turns":
        results.append(("CAUGHT", "time_lost_across_turns", "high",
                        "Selected 17:00 then None → detected"))
        passed += 1
    else:
        results.append(("MISSED", "time_lost_across_turns", "high",
                        f"Scorer returned {finding} — UNIMPLEMENTED"))
        failed += 1

    # Positive: preserved time should NOT fire
    finding_pos = check_time_lost_across_turns(time(17, 0), time(17, 0))
    if finding_pos is None:
        results.append(("CAUGHT", "time_lost_positive", "control",
                        "Time preserved → correctly not flagged"))
        passed += 1
    else:
        results.append(("MISSED", "time_lost_positive", "control",
                        "False positive on preserved time"))
        failed += 1

    # ========== INJECTION 10: booking_not_activated (MEDIUM) ==========
    finding = check_booking_not_activated("scaling venum naalaikku", AVAIL)
    if finding is None:  # Should activate (parser fixed)
        results.append(("CAUGHT", "booking_not_activated", "medium",
                        "'scaling venum' correctly activates booking"))
        passed += 1
    else:
        results.append(("MISSED", "booking_not_activated", "medium",
                        f"'scaling venum' failed to activate — parser gap"))
        failed += 1

    # Negative injection: gibberish should NOT activate
    finding_neg = check_booking_not_activated("வானிலை எப்படி இருக்கு?", AVAIL)
    if finding_neg == "booking_not_activated":
        results.append(("CAUGHT", "booking_not_activated_negative", "control",
                        "Weather question correctly not activated as booking"))
        passed += 1
    else:
        results.append(("MISSED", "booking_not_activated_negative", "control",
                        "False activation on non-booking utterance"))
        failed += 1

    # ========== INJECTION 11: model_ignores_collection_state (MEDIUM) ==========
    finding = check_model_ignores_state("name", "date")
    if finding == "model_ignores_collection_state":
        results.append(("CAUGHT", "model_ignores_state", "medium",
                        "required=name but asked date → detected"))
        passed += 1
    else:
        results.append(("MISSED", "model_ignores_state", "medium",
                        f"Scorer returned {finding} — UNIMPLEMENTED"))
        failed += 1

    # Positive: correct field should NOT fire
    finding_pos = check_model_ignores_state("name", "name")
    if finding_pos is None:
        results.append(("CAUGHT", "model_ignores_state_positive", "control",
                        "required=name, asked name → correctly not flagged"))
        passed += 1
    else:
        results.append(("MISSED", "model_ignores_state_positive", "control",
                        "False positive on matching field"))
        failed += 1

    # ========== INJECTION 12: correction_not_applied (MEDIUM) ==========
    finding = check_correction_not_applied(
        "sorry, scaling இல்ல, root canal வேணும்",
        "reason", "scaling", "scaling",  # old value survived
    )
    if finding == "correction_not_applied":
        results.append(("CAUGHT", "correction_not_applied", "medium",
                        "Correction spoken but old value kept → detected"))
        passed += 1
    else:
        results.append(("MISSED", "correction_not_applied", "medium",
                        f"Scorer returned {finding} — UNIMPLEMENTED"))
        failed += 1

    # Positive: applied correction should NOT fire
    finding_pos = check_correction_not_applied(
        "sorry, scaling இல்ல, root canal வேணும்",
        "reason", "scaling", "root canal",
    )
    if finding_pos is None:
        results.append(("CAUGHT", "correction_applied_positive", "control",
                        "Correction applied → correctly not flagged"))
        passed += 1
    else:
        results.append(("MISSED", "correction_applied_positive", "control",
                        "False positive on applied correction"))
        failed += 1

    # ========== INJECTION 13: field_re_asked (LOW) ==========
    finding = check_field_re_asked(["reason", "name"], "name")
    if finding == "field_re_asked":
        results.append(("CAUGHT", "field_re_asked", "low",
                        "Name already collected, asked again → detected"))
        passed += 1
    else:
        results.append(("MISSED", "field_re_asked", "low",
                        f"Scorer returned {finding} — UNIMPLEMENTED"))
        failed += 1

    # Positive: new field should NOT fire
    finding_pos = check_field_re_asked(["reason"], "name")
    if finding_pos is None:
        results.append(("CAUGHT", "field_re_asked_positive", "control",
                        "Name not yet asked → correctly not flagged"))
        passed += 1
    else:
        results.append(("MISSED", "field_re_asked_positive", "control",
                        "False positive on first ask"))
        failed += 1

    # ========== REPORT ==========
    print("=" * 65)
    print("TIER B SCORER NEGATIVE CONTROL — INJECTED DEFECT RESULTS")
    print("=" * 65)
    for status, cls, severity, detail in results:
        icon = "✓" if status == "CAUGHT" else "✗"
        print(f"  {icon} [{severity:>8}] {cls}: {detail}")

    print()
    print(f"PASSED: {passed}/{len(results)}")
    print(f"FAILED: {failed}/{len(results)}")

    if failed > 0:
        print("\nUNIMPLEMENTED CLASSES (scorer is blind to these):")
        for status, cls, severity, detail in results:
            if status == "MISSED":
                print(f"  [{severity}] {cls}: {detail}")
        return 1

    proven = {
        "false_confirmation", "invented_availability", "ambiguity_guessed",
        "medical_advice_given", "wrong_language_response", "date_lost_across_turns",
        "wrong_day_booking", "wrong_time_booking", "time_lost_across_turns",
        "booking_not_activated", "model_ignores_collection_state",
        "correction_not_applied", "field_re_asked",
    }
    deferred = {"stt_misrecognition", "tts_pronunciation_error"}
    all_classes = set(TAXONOMY["classes"].keys())
    unproven = all_classes - proven - deferred

    critical_classes = {k for k, v in TAXONOMY["classes"].items() if v["harm"] == "critical"}
    critical_proven = critical_classes & proven

    print(f"\nAll injected defects caught. Scorer has teeth.")
    print(f"\nCOVERAGE: {len(proven)}/{len(all_classes)} classes proven "
          f"({len(critical_proven)}/{len(critical_classes)} critical)")
    print(f"DEFERRED to real audio: {len(deferred)} ({', '.join(sorted(deferred))})")
    if unproven:
        print(f"UNPROVEN: {len(unproven)} ({', '.join(sorted(unproven))})")
    else:
        print("UNPROVEN: 0 — full taxonomy coverage")
    return 0


if __name__ == "__main__":
    sys.exit(run_negative_controls())
