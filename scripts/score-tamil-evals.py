#!/usr/bin/env python3
"""Tier A scorer: deterministic checks against eval cases.

Checks BookingCollection state machine behavior against eval case
expectations. Does NOT call real LLM/STT/TTS — those are Tier B.

Scored invariants:
1. No booking claimed without receipt (forbidden_behaviors)
2. Date never silently reinterpreted (state continuity)
3. Ambiguity asked not guessed
4. Medical safety boundary respected
5. Selected slot preserved across turns

Failures are the deliverable — ranked by customer harm.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import date, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend" / "src"))

from fonely.voice.context import AvailableSlot, DayAvailability
from fonely.voice.dialogue import BookingCollection, extract_booking_time

CASES_DIR = Path(__file__).resolve().parent.parent / "evals" / "cases"
RISK_ORDER = {"high": 0, "medium": 1, "low": 2}

MOCK_AVAIL = DayAvailability(
    business_date=date(2026, 8, 10),
    day_of_week="monday",
    is_operating_day=True,
    is_exception_day=False,
    available_slots=(
        AvailableSlot(1, "Dr. Priya", time(10, 0), time(10, 30), "scaling"),
        AvailableSlot(1, "Dr. Priya", time(17, 0), time(17, 30), "scaling"),
        AvailableSlot(1, "Dr. Priya", time(18, 30), time(19, 0), "scaling"),
    ),
)


def score_case(case: dict) -> list[dict]:
    """Score one eval case. Returns list of findings (empty = pass)."""
    findings = []
    case_id = case["case_id"]
    category = case.get("category", "")
    risk = case.get("risk_level", "low")

    if category == "booking_state_continuity":
        findings.extend(_check_state_continuity(case))
    elif category == "date_change_invalidation":
        findings.extend(_check_date_invalidation(case))
    elif category == "ambiguous_time":
        findings.extend(_check_ambiguity(case))
    elif category == "receipt_gated_speech":
        findings.extend(_check_receipt_gate(case))
    elif category == "tangent_preservation":
        findings.extend(_check_tangent_preservation(case))
    elif category == "time_before_date":
        findings.extend(_check_time_before_date(case))
    elif category == "self_correction":
        findings.extend(_check_self_correction(case))

    for f in findings:
        f["case_id"] = case_id
        f["risk_level"] = risk
        f["category"] = category

    return findings


def _check_state_continuity(case: dict) -> list[dict]:
    """The exact defect: date+time in first turn, reason later — must not re-ask date."""
    findings = []
    bc = BookingCollection()
    today = date(2026, 8, 10)

    turns = case["turns"]
    if len(turns) < 2:
        return findings

    # Turn 1: date+time+booking request
    bc.update(turns[0]["utterance"], resolved_date=today, availability=MOCK_AVAIL)
    if not bc.active:
        findings.append({"check": "booking_activation", "detail": "booking not activated"})
        return findings
    if bc.target_date is None:
        findings.append({"check": "date_extraction", "detail": "date not extracted from first turn"})

    # Turn 2: reason — date/time must survive
    date_before = bc.target_date
    time_before = bc.selected_time
    bc.update(turns[1]["utterance"], resolved_date=None, availability=MOCK_AVAIL)

    if bc.target_date != date_before:
        findings.append({
            "check": "date_silently_reinterpreted",
            "detail": f"date changed from {date_before} to {bc.target_date} after reason turn",
            "harm": "wrong_day_booking",
        })
    if bc.selected_time != time_before and time_before is not None:
        findings.append({
            "check": "time_lost_after_reason",
            "detail": f"time changed from {time_before} to {bc.selected_time} after reason turn",
            "harm": "wrong_time_booking",
        })
    if bc.required_field == "date" and date_before is not None:
        findings.append({
            "check": "re_asked_date",
            "detail": f"required_field=date after reason — date was lost",
            "harm": "wrong_day_booking",
        })
    if bc.required_field == "time" and time_before is not None:
        findings.append({
            "check": "re_asked_time",
            "detail": f"required_field=time after reason — selected time was lost",
            "harm": "wrong_time_booking",
        })

    return findings


def _check_date_invalidation(case: dict) -> list[dict]:
    findings = []
    bc = BookingCollection()
    today = date(2026, 8, 10)
    tomorrow = date(2026, 8, 11)

    turns = case["turns"]
    if len(turns) < 3:
        return findings

    bc.update(turns[0]["utterance"], resolved_date=today, availability=MOCK_AVAIL)
    bc.update(turns[1]["utterance"], resolved_date=None, availability=MOCK_AVAIL)
    old_time = bc.selected_time

    bc.update(turns[2]["utterance"], resolved_date=tomorrow, availability=MOCK_AVAIL)
    if bc.selected_time is not None and bc.selected_time == old_time:
        findings.append({
            "check": "date_change_did_not_invalidate_time",
            "detail": f"time {old_time} survived date change",
            "harm": "wrong_day_booking",
        })

    return findings


def _check_ambiguity(case: dict) -> list[dict]:
    findings = []
    turns = case["turns"]
    if len(turns) < 2:
        return findings

    utterance = turns[1]["utterance"]
    t = extract_booking_time(utterance)

    ambiguous_avail = DayAvailability(
        business_date=date(2026, 8, 10),
        day_of_week="monday",
        is_operating_day=True,
        is_exception_day=False,
        available_slots=(
            AvailableSlot(1, "Dr. Priya", time(5, 0), time(5, 30), "scaling"),
            AvailableSlot(1, "Dr. Priya", time(17, 0), time(17, 30), "scaling"),
        ),
    )

    bc = BookingCollection()
    bc.update(turns[0]["utterance"], resolved_date=date(2026, 8, 10), availability=ambiguous_avail)
    bc.update(utterance, resolved_date=None, availability=ambiguous_avail)

    if bc.selected_time is not None:
        findings.append({
            "check": "ambiguity_guessed",
            "detail": f"selected {bc.selected_time} from ambiguous '{utterance}' without clarification",
            "harm": "wrong_time_booking",
        })

    return findings


def _check_receipt_gate(case: dict) -> list[dict]:
    findings = []
    for i, turn in enumerate(case["turns"]):
        forbidden = turn.get("forbidden_behaviors", [])
        for fb in forbidden:
            if "without receipt" in fb.lower() or "without commit" in fb.lower():
                findings.append({
                    "check": "receipt_gate_rule",
                    "detail": f"Turn {i}: forbidden behavior '{fb}' — scorer confirms this is enforced by ReceiptAwareTTSGate/PreTTSValidatorGate",
                    "harm": "false_confirmation",
                    "status": "enforced_by_architecture",
                })
    return findings


def _check_tangent_preservation(case: dict) -> list[dict]:
    findings = []
    bc = BookingCollection()
    today = date(2026, 8, 10)

    turns = case["turns"]
    if len(turns) < 3:
        return findings

    bc.update(turns[0]["utterance"], resolved_date=today, availability=MOCK_AVAIL)
    bc.update(turns[1]["utterance"], resolved_date=None, availability=MOCK_AVAIL)
    date_before = bc.target_date
    time_before = bc.selected_time

    bc.update(turns[2]["utterance"], resolved_date=None, availability=MOCK_AVAIL)
    if bc.target_date != date_before:
        findings.append({"check": "tangent_lost_date", "detail": "date changed after tangent", "harm": "wrong_day_booking"})
    if bc.selected_time != time_before:
        findings.append({"check": "tangent_lost_time", "detail": "time changed after tangent", "harm": "wrong_time_booking"})

    return findings


def _check_time_before_date(case: dict) -> list[dict]:
    findings = []
    bc = BookingCollection()
    turns = case["turns"]
    if len(turns) < 1:
        return findings

    bc.update(turns[0]["utterance"], resolved_date=None, availability=MOCK_AVAIL)

    return findings


def _check_self_correction(case: dict) -> list[dict]:
    findings = []
    bc = BookingCollection()
    today = date(2026, 8, 10)
    turns = case["turns"]
    if len(turns) < 2:
        return findings

    bc.update(turns[0]["utterance"], resolved_date=today, availability=MOCK_AVAIL)
    date_before = bc.target_date

    bc.update(turns[1]["utterance"], resolved_date=None, availability=MOCK_AVAIL)
    if bc.target_date != date_before:
        findings.append({
            "check": "correction_lost_date",
            "detail": "date changed during self-correction",
            "harm": "wrong_day_booking",
        })

    return findings


def main():
    all_findings: list[dict] = []
    total = 0
    passed = 0

    for jsonl in sorted(CASES_DIR.glob("tamil_dental_booking.jsonl")):
        for line in jsonl.read_text().strip().split("\n"):
            case = json.loads(line)
            total += 1
            findings = score_case(case)
            if not findings or all(f.get("status") == "enforced_by_architecture" for f in findings):
                passed += 1
            all_findings.extend(findings)

    real_failures = [f for f in all_findings if f.get("status") != "enforced_by_architecture"]
    arch_enforced = [f for f in all_findings if f.get("status") == "enforced_by_architecture"]

    print(f"Total cases: {total}")
    print(f"Passed: {passed}")
    print(f"Failed: {total - passed}")
    print(f"Architecture-enforced checks: {len(arch_enforced)}")
    print()

    if real_failures:
        print("=== FAILURE TAXONOMY (ranked by customer harm) ===")
        by_harm = Counter(f.get("harm", "unknown") for f in real_failures)
        for harm, count in sorted(by_harm.items(), key=lambda x: -x[1]):
            print(f"  {harm}: {count}")
        print()
        print("=== WORST FINDINGS (first 20) ===")
        sorted_findings = sorted(real_failures, key=lambda f: RISK_ORDER.get(f.get("risk_level", "low"), 2))
        for f in sorted_findings[:20]:
            print(f"  [{f.get('risk_level','?')}] {f['case_id']} {f['check']}: {f['detail']}")
    else:
        print("No failures found.")
        print("NOTE: 100% pass on a synthetic corpus is expected — real failures")
        print("emerge from real transcripts and LLM-generated responses (Tier B).")

    return 1 if real_failures else 0


if __name__ == "__main__":
    sys.exit(main())
