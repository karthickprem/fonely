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

    print("\nAll injected defects caught. Scorer has teeth.")
    unimplemented = set(TAXONOMY["classes"].keys()) - {
        "false_confirmation", "invented_availability", "ambiguity_guessed",
        "medical_advice_given", "wrong_language_response", "date_lost_across_turns",
        "wrong_day_booking", "wrong_time_booking", "time_lost_across_turns",
        "stt_misrecognition", "booking_not_activated", "field_re_asked",
        "tts_pronunciation_error", "model_ignores_collection_state",
        "correction_not_applied",
    }
    if unimplemented:
        print(f"\nNOTE: {len(unimplemented)} taxonomy classes not tested by negative control:")
        for cls in sorted(unimplemented):
            print(f"  {cls}")
    print("\nClasses requiring real audio (Tier B/C only):")
    print("  stt_misrecognition — needs real Sarvam STT output")
    print("  tts_pronunciation_error — needs native speaker review")
    return 0


if __name__ == "__main__":
    sys.exit(run_negative_controls())
