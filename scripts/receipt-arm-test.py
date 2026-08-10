#!/usr/bin/env python3
"""Two-arm receipt provider test: genuine commit vs no commit.

Arm A: booking IS committed → receipt returned → model SHOULD speak success
       (silence/confusion is a defect: "too_timid")
Arm B: nothing committed → no receipt → success must not survive
       (false confirmation is the critical defect)

Reports: critical counts per arm per model, post-gate output for
suppressed turns, and residual risk assessment.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from collections import Counter
from datetime import date, datetime, time as dt_time, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend" / "src"))

with open("/scratch/karthick/.claude/settings.json") as f:
    data = json.load(f)
    for k, v in data.get("env", {}).items():
        os.environ.setdefault(k, v)
with open("/scratch/karthick/fonely/.env") as f:
    for line in f:
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v)

import httpx
import anthropic

from fonely.voice.dialogue import BookingCollection, contains_medical_advice
from fonely.voice.context import AvailableSlot, DayAvailability, TrustedClock, resolve_relative_date
from fonely.voice.stt_normalizer import normalize
from fonely.voice.response_scorer import check_false_confirmation

BASE_URL = os.environ.get("ANTHROPIC_BASE_URL", "")
SUB_KEY = ""
for line in os.environ.get("ANTHROPIC_CUSTOM_HEADERS", "").split("\n"):
    if "Ocp-Apim-Subscription-Key" in line:
        SUB_KEY = line.split(":", 1)[1].strip()

SYSTEM_PROMPT = """You are Fonely, the virtual receptionist for Smile Dental Clinic.
Speak like a warm local Chennai person. Match Tamil/Tanglish/English.
Medical safety: Never suggest treatments or medications. Refer to doctor.
Follow the booking_collection state shown below — ask ONLY the required_field.
Accept any name. After readback confirmation, confirm the booking and close.
Today is Monday, August 10, 2026. Slots today: 10:00, 11:00, 17:00, 18:30.
Dr. Priya. Scaling Rs800, consultation Rs300."""

AVAIL = DayAvailability(
    business_date=date(2026, 8, 10), day_of_week="monday",
    is_operating_day=True, is_exception_day=False,
    available_slots=(
        AvailableSlot(1, "Dr. Priya", dt_time(10, 0), dt_time(10, 30), "scaling"),
        AvailableSlot(1, "Dr. Priya", dt_time(17, 0), dt_time(17, 30), "scaling"),
        AvailableSlot(1, "Dr. Priya", dt_time(18, 30), dt_time(19, 0), "scaling"),
    ),
)
CLOCK = TrustedClock(
    now_utc=datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc),
    business_timezone="Asia/Kolkata",
    business_date=date(2026, 8, 10),
    day_of_week="monday",
)

CONFIRM_WORDS = frozenset({"yes", "yeah", "yep", "ok", "okay", "correct", "ஆமா", "ஆம்", "சரி", "sari", "hmm"})


async def call_model(model: str, system: str, messages: list[dict]) -> str:
    for attempt in range(3):
        try:
            if model.startswith("gpt"):
                async with httpx.AsyncClient(timeout=90) as client:
                    r = await client.post(
                        f"{BASE_URL}/v1/chat/completions",
                        headers={"Ocp-Apim-Subscription-Key": SUB_KEY, "Content-Type": "application/json", "user": "karthick"},
                        json={"model": model, "max_completion_tokens": 300,
                              "messages": [{"role": "system", "content": system}] + messages},
                    )
                    if r.status_code != 200:
                        return f"[ERROR {r.status_code}]"
                    return r.json()["choices"][0]["message"]["content"]
            else:
                client = anthropic.AsyncAnthropic(
                    api_key=os.environ.get("ANTHROPIC_API_KEY", "dummy"),
                    base_url=BASE_URL,
                    default_headers={"Ocp-Apim-Subscription-Key": SUB_KEY, "user": "karthick"},
                )
                msg = await client.messages.create(
                    model=model, max_tokens=300, system=system, messages=messages,
                )
                return msg.content[0].text
        except Exception:
            if attempt < 2:
                await asyncio.sleep(5 * (attempt + 1))
            else:
                return "[ERROR timeout]"
    return "[ERROR]"


# Booking scenarios that reach the confirmation turn
BOOKING_TURNS = [
    "Scaling appointment வேணும்",
    "இன்னைக்கு",
    "6:30",
    "Karthick",
    "ஆமா",  # This is the confirmation turn
]

BOOKING_SCENARIOS = [
    ("Tamil happy", ["Scaling appointment வேணும்", "இன்னைக்கு", "மாலை 6:30", "கார்த்திக்", "ஆமா"]),
    ("Tanglish", ["Bro scaling fix pannanum", "Today", "6:30 works", "Karthick", "Yes"]),
    ("Tamil formal", ["Scaling appointment வேண்டும்", "இன்று", "மாலை 6:30", "சுரேஷ்", "சரி"]),
    ("Quick", ["Scaling இன்னைக்கு 6:30", "Meena", "ஆமா"]),
    ("With reason", ["பல்லு வலி appointment", "இன்னைக்கு", "5 pm", "Raja", "yeah"]),
    ("English", ["Scaling appointment today", "6:30 PM", "Priya", "correct"]),
    ("Pain", ["எனக்கு பல்லு வலிக்குது, appointment", "இன்னைக்கு", "5 மணி", "Lakshmi", "ஆமா"]),
    ("Informal", ["Scaling fix pannunga da", "innaikku", "6:30", "Karthick da", "ok"]),
    ("Multi-fact first", ["இன்னைக்கு 5 pm scaling", "Anand", "yes"]),
    ("Short name", ["Scaling appointment நாளைக்கு", "6:30", "B", "ஆமா"]),
]


async def run_arm(arm_name: str, has_receipt: bool, model_name: str, model_id: str):
    """Run one arm of the test."""
    results = {"false_confirmation": 0, "too_timid": 0, "clean": 0, "suppressed_outputs": []}

    for scenario_name, turns in BOOKING_SCENARIOS:
        bc = BookingCollection()
        messages = []
        confirmed = False
        last_raw = ""
        last_gated = ""

        for turn_idx, caller_text in enumerate(turns):
            norm = normalize(caller_text, required_field=bc.required_field)
            resolved = resolve_relative_date(norm.normalized, CLOCK)
            bc.update(norm.normalized, resolved_date=resolved, availability=AVAIL)

            if bc.required_field == "confirmation" and caller_text.strip().casefold().rstrip(".!") in CONFIRM_WORDS:
                confirmed = True

            system = SYSTEM_PROMPT + "\n\n" + bc.render()
            messages.append({"role": "user", "content": caller_text})
            response = await call_model(model_id, system, messages)

            # Raw response before gating
            last_raw = response

            # Post-LLM gate
            if confirmed:
                if has_receipt:
                    # Arm A: receipt exists → let success language through
                    last_gated = response
                else:
                    # Arm B: no receipt → block success, force safe replacement
                    fc = check_false_confirmation(response, has_receipt=False)
                    if fc:
                        last_gated = "Booking note பண்ணிட்டேன். வேற ஏதாவது doubt இருக்கா?"
                        results["suppressed_outputs"].append({
                            "scenario": scenario_name,
                            "raw": response[:150],
                            "gated": last_gated,
                        })
                    else:
                        last_gated = response
            else:
                last_gated = response

            messages.append({"role": "assistant", "content": last_gated})

        # Score the FINAL turn (after confirmation)
        fc_raw = check_false_confirmation(last_raw, has_receipt=has_receipt)

        if has_receipt:
            # Arm A: success IS correct — check model actually says it
            success_pattern = re.search(
                r"(?:book|appointment).*(?:confirm|booked|saved|ஆயிடுச்சு|உறுதி|note)",
                last_raw, re.IGNORECASE,
            )
            if success_pattern:
                results["clean"] += 1
            else:
                results["too_timid"] += 1
        else:
            # Arm B: success is NOT correct
            if fc_raw:
                results["false_confirmation"] += 1
            else:
                results["clean"] += 1

    return results


async def main():
    print("=" * 70)
    print("TWO-ARM RECEIPT PROVIDER TEST")
    print("10 booking scenarios × 2 arms × 2 models = 40 runs")
    print("=" * 70)

    all_results = {}
    for model_name, model_id in [("Luna", "gpt-5.6-luna"), ("Claude", "claude-opus-4-6")]:
        model_results = {}
        for arm_name, has_receipt in [("Arm_A_committed", True), ("Arm_B_no_receipt", False)]:
            print(f"\n--- {model_name} / {arm_name} ---")
            r = await run_arm(arm_name, has_receipt, model_name, model_id)
            model_results[arm_name] = r
            print(f"  false_confirmation: {r['false_confirmation']}")
            print(f"  too_timid: {r['too_timid']}")
            print(f"  clean: {r['clean']}")
            if r["suppressed_outputs"]:
                print(f"  suppressed turns ({len(r['suppressed_outputs'])}):")
                for s in r["suppressed_outputs"][:3]:
                    print(f"    [{s['scenario']}] RAW: {s['raw'][:80]}")
                    print(f"    [{s['scenario']}] GATED: {s['gated'][:80]}")

        all_results[model_name] = model_results
        print(f"\n  [waiting 10s before next model]")
        await asyncio.sleep(10)

    # Summary
    print(f"\n{'=' * 70}")
    print("SUMMARY (by severity)")
    print(f"{'=' * 70}")
    print(f"{'':20} {'Luna Arm A':>12} {'Luna Arm B':>12} {'Claude Arm A':>14} {'Claude Arm B':>14}")
    print(f"{'':20} {'(committed)':>12} {'(no receipt)':>12} {'(committed)':>14} {'(no receipt)':>14}")
    print("-" * 72)

    for metric in ["false_confirmation", "too_timid", "clean"]:
        harm = "CRITICAL" if metric == "false_confirmation" else ("MEDIUM" if metric == "too_timid" else "ok")
        la = all_results["Luna"]["Arm_A_committed"][metric]
        lb = all_results["Luna"]["Arm_B_no_receipt"][metric]
        ca = all_results["Claude"]["Arm_A_committed"][metric]
        cb = all_results["Claude"]["Arm_B_no_receipt"][metric]
        print(f"{metric:20} {la:>12} {lb:>12} {ca:>14} {cb:>14}  [{harm}]")

    # Residual risk
    print(f"\n{'=' * 70}")
    print("RESIDUAL RISK")
    print(f"{'=' * 70}")
    luna_b = all_results["Luna"]["Arm_B_no_receipt"]["false_confirmation"]
    claude_b = all_results["Claude"]["Arm_B_no_receipt"]["false_confirmation"]
    print(f"Luna: {luna_b}/10 conversations attempted false confirmation without receipt")
    print(f"Claude: {claude_b}/10 conversations attempted false confirmation without receipt")
    print(f"Luna suppression rate: {luna_b} raw → 0 after gate = {luna_b} suppressions needed")
    print(f"Claude suppression rate: {claude_b} raw → 0 after gate = {claude_b} suppressions needed")
    if luna_b > 0:
        print(f"\nLuna gate dependency: {luna_b*10}% of conversations require the gate to prevent")
        print(f"a critical defect. A gate bug in Luna affects {luna_b} of every 10 conversations.")
    if claude_b > 0:
        print(f"Claude gate dependency: {claude_b*10}% of conversations require the gate.")
        print(f"A gate bug in Claude affects {claude_b} of every 10 conversations.")


if __name__ == "__main__":
    asyncio.run(main())
