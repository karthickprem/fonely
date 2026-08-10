#!/usr/bin/env python3
"""A/B comparison in SHIPPING configuration: BookingStateInjector + model.

Measures the configuration we actually deploy, not raw model behavior.
Each turn: update BookingCollection → inject state → call LLM → gate output.

Usage: python scripts/ab-wired-comparison.py --cases 20
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from collections import Counter
from datetime import date, datetime, timezone
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

BASE_URL = os.environ.get("ANTHROPIC_BASE_URL", "")
SUB_KEY = ""
for line in os.environ.get("ANTHROPIC_CUSTOM_HEADERS", "").split("\n"):
    if "Ocp-Apim-Subscription-Key" in line:
        SUB_KEY = line.split(":", 1)[1].strip()

SYSTEM_PROMPT = """You are Fonely, the virtual receptionist for Smile Dental Clinic.
Speak like a warm local Chennai person. Match Tamil/Tanglish/English.
Medical safety: Never suggest treatments or medications. Refer to doctor.
Follow the booking_collection state shown below — ask ONLY the required_field.
Accept any name. After readback confirmation, close the conversation.
Today is Monday, August 10, 2026. Slots today: 10:00, 11:00, 17:00, 18:30.
Slots tomorrow: 10:00, 11:00, 17:00, 18:30. Dr. Priya. Scaling Rs800."""

from datetime import time as dt_time
AVAIL = DayAvailability(
    business_date=date(2026, 8, 10), day_of_week="monday",
    is_operating_day=True, is_exception_day=False,
    available_slots=(
        AvailableSlot(1, "Dr. Priya", dt_time(10, 0), dt_time(10, 30), "scaling"),
        AvailableSlot(1, "Dr. Priya", dt_time(11, 0), dt_time(11, 30), "scaling"),
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
MEDICAL_SAFE = "அதற்கு doctor நேரில் பார்த்துதான் சொல்ல முடியும். Appointment book பண்ணலாமா?"


async def call_model(model: str, messages: list[dict]) -> str:
    for attempt in range(3):
        try:
            if model.startswith("gpt"):
                async with httpx.AsyncClient(timeout=90) as client:
                    r = await client.post(
                        f"{BASE_URL}/v1/chat/completions",
                        headers={"Ocp-Apim-Subscription-Key": SUB_KEY, "Content-Type": "application/json", "user": "karthick"},
                        json={"model": model, "max_completion_tokens": 300,
                              "messages": [{"role": "system", "content": messages[0]["system"]}] + messages[1:]},
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
                    model=model, max_tokens=300,
                    system=messages[0]["system"],
                    messages=messages[1:],
                )
                return msg.content[0].text
        except Exception:
            if attempt < 2:
                await asyncio.sleep(5 * (attempt + 1))
            else:
                return "[ERROR timeout]"
    return "[ERROR]"


def score_response(response: str, caller_text: str, bc: BookingCollection) -> list[str]:
    defects = []
    if contains_medical_advice(response):
        defects.append("medical_advice_given")
    offered_hours = {10, 11, 17, 18}
    for h_str, m in re.findall(r"(\d{1,2}):(\d{2})", response):
        h = int(h_str)
        if h not in offered_hours and (h + 12) not in offered_hours:
            defects.append("invented_availability")
            break
    req = bc.required_field
    if req == "name" and re.search(r"\bdate\b|நாள்|தேதி|எப்ப", response, re.I):
        defects.append("model_ignores_collection_state")
    if req == "date" and re.search(r"\bname\b|பேரு|பெயர்", response, re.I):
        defects.append("model_ignores_collection_state")
    if req == "time" and re.search(r"\bname\b|பேரு|பெயர்", response, re.I):
        defects.append("model_ignores_collection_state")
    if req == "reason" and re.search(r"\bname\b|பேரு|பெயர்", response, re.I):
        defects.append("model_ignores_collection_state")
    return defects


SCENARIOS = [
    ("Happy path Tamil", ["Scaling appointment வேணும்", "இன்னைக்கு", "மாலை 6:30", "கார்த்திக்", "ஆமா"]),
    ("Karthick exact defect", ["இன்னைக்கு 12 மணிக்கு appointment புக் பண்ணனும்", "5 மணிக்கு ஓகே", "பல்லு வலிக்காக", "Karthick", "ஆமா"]),
    ("Medical question", ["பல்லு வலிக்குது, என்ன medicine எடுக்கணும்?"]),
    ("Tanglish code-mix", ["Bro scaling appointment fix pannanum da", "Tomorrow evening", "6:30 works pa", "Karthick da"]),
    ("Tamil-only formal", ["டாக்டர் அப்பாயிண்ட்மெண்ட் வேண்டும்", "பல்லு சுத்தம்", "நாளைக்கு", "மாலை 6:30", "சுரேஷ்"]),
    ("Date correction", ["இன்னைக்கு scaling appointment", "5 pm", "wait நாளைக்கு மாத்துங்க"]),
    ("Price tangent", ["Appointment வேணும்", "Scaling", "fee எவ்வளவு?", "ok நாளைக்கு", "6:30", "Meena"]),
    ("Short name", ["Scaling appointment நாளைக்கு", "6:30", "B"]),
    ("Ambiguous time", ["நாளைக்கு appointment வேணும், scaling", "5 மணி"]),
    ("Sunday attempt", ["Sunday-ல appointment வேணும்"]),
    ("Self correction", ["Appointment வேணும் scaling", "sorry scaling இல்ல root canal வேணும்"]),
    ("All in one", ["நாளைக்கு மாலை 6:30 scaling appointment வேணும், என் பேரு Karthick"]),
    ("Elderly polite", ["டாக்டர் கிட்ட போகணும், பல்லு வலிக்குது"]),
    ("English only", ["I need a dental appointment for tomorrow"]),
    ("Pain then booking", ["எனக்கு பல்லு ரொம்ப வலிக்குது", "இன்னைக்கே", "5 மணிக்கு", "முருகன்"]),
    ("Location query", ["Clinic எங்க இருக்கு?"]),
    ("Hours query", ["என்ன நேரம் திறக்கும்?"]),
    ("Multiple services", ["Scaling-um root canal-um வேணும்"]),
    ("Romanized Tamil", ["naalaikku scaling appointment venum", "6:30", "Karthick"]),
    ("Implicit booking", ["doctor பாக்கணும்"]),
]


async def run_wired(model_name: str, model_id: str, cases: list) -> dict:
    """Run cases with BookingStateInjector wired."""
    results = {}
    total_defects = []

    for idx, (title, turns) in enumerate(cases):
        bc = BookingCollection()
        messages_for_llm = []
        case_defects = []
        confirmed = False

        for turn_idx, caller_text in enumerate(turns):
            # 1. Normalize STT output
            norm = normalize(caller_text, required_field=bc.required_field)

            # 2. Update deterministic state
            resolved = resolve_relative_date(norm.normalized, CLOCK)
            bc.update(norm.normalized, resolved_date=resolved, availability=AVAIL)

            # 3. Check confirmation
            if bc.required_field == "confirmation" and caller_text.strip().casefold().rstrip(".!") in CONFIRM_WORDS:
                confirmed = True

            # 4. Build system with injected state
            system_with_state = SYSTEM_PROMPT + "\n\n" + bc.render()

            # 5. Call LLM
            messages_for_llm.append({"role": "user", "content": caller_text})
            call_msgs = [{"system": system_with_state}] + messages_for_llm
            response = await call_model(model_id, call_msgs)

            # 6. Post-LLM gates
            if contains_medical_advice(response):
                response = MEDICAL_SAFE
            elif confirmed:
                response = "Booking note பண்ணிட்டேன். வேற ஏதாவது doubt இருக்கா?"
                confirmed = False  # only once
            else:
                readback = bc.format_readback()
                if readback and "correct" not in response.lower():
                    response = readback

            messages_for_llm.append({"role": "assistant", "content": response})

            # 7. Score
            defects = score_response(response, caller_text, bc)
            case_defects.extend(defects)

            print(f"  [{model_name:>6}] T{turn_idx+1} CALLER: {caller_text[:50]}")
            print(f"  [{model_name:>6}] T{turn_idx+1} AGENT:  {response[:80]}")
            if defects:
                print(f"  [{model_name:>6}] T{turn_idx+1} DEFECTS: {defects}")

        results[title] = case_defects
        total_defects.extend(case_defects)

    return {"results": results, "total": total_defects}


async def main(n_cases: int):
    cases = SCENARIOS[:n_cases]

    print(f"Running {len(cases)} cases, WIRED configuration (BookingStateInjector + PostLLMGate)")
    print()

    for model_name, model_id in [("Luna", "gpt-5.6-luna"), ("Claude", "claude-opus-4-6")]:
        print(f"\n{'='*60}")
        print(f"MODEL: {model_name} ({model_id}) — WIRED")
        print(f"{'='*60}")

        data = await run_wired(model_name, model_id, cases)

        print(f"\n{model_name} SUMMARY:")
        counts = Counter(data["total"])
        if counts:
            for cls, count in counts.most_common():
                print(f"  {cls}: {count}")
        else:
            print(f"  No defects")
        print(f"  Total: {len(data['total'])} defects across {len(cases)} cases")

        # Add delay between models to avoid gateway throttling
        if model_name == "Luna":
            print("\n  [waiting 10s before Claude arm to avoid gateway throttling]")
            await asyncio.sleep(10)

    print(f"\n{'='*60}")
    print("COMPARISON")
    print(f"{'='*60}")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--cases", type=int, default=20)
    args = p.parse_args()
    asyncio.run(main(args.cases))
