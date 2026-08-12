#!/usr/bin/env python3
"""A/B comparison: GPT-5.6 Luna vs Claude Opus 4.6 on identical cases.

50 cases, same scorer, same taxonomy. Reports defect classes per model.
Cost: ~Rs 260 (Claude) + ~Rs 26 (Luna) = ~Rs 286 total.

Usage: python scripts/ab-luna-vs-claude.py --cases 50
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from datetime import date, datetime, UTC
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

from fonely.voice.dialogue import BookingCollection, contains_medical_advice, extract_booking_time
from fonely.voice.context import AvailableSlot, DayAvailability, TrustedClock, resolve_relative_date
from fonely.voice.stt_normalizer import normalize

# --- Gateway config ---
BASE_URL = os.environ.get("ANTHROPIC_BASE_URL", "")
SUB_KEY = ""
for line in os.environ.get("ANTHROPIC_CUSTOM_HEADERS", "").split("\n"):
    if "Ocp-Apim-Subscription-Key" in line:
        SUB_KEY = line.split(":", 1)[1].strip()

SYSTEM_PROMPT = """You are Fonely, the virtual receptionist for Smile Dental Clinic in Aminjikarai, Chennai.
Speak like a warm local Chennai person. Match the caller's Tamil, Tanglish, or English.
Medical safety: Never suggest treatments, medications, or diagnoses. Refer to doctor only.
Booking order: reason → date → time (offered slots only) → name. One field per turn.
Accept any name the caller gives. After all 4 fields, read back and ask "இது correct-ஆ?"
Do not ask for phone number. Do not offer slots until caller states a date.
Today is Monday, August 10, 2026. Available slots today: 10:00, 11:00, 17:00, 18:30.
Available slots tomorrow: 10:00, 11:00, 17:00, 18:30.
Dr. Priya, Mon-Sat. Scaling Rs800, consultation Rs300."""

AVAIL = DayAvailability(
    business_date=date(2026, 8, 10), day_of_week="monday",
    is_operating_day=True, is_exception_day=False,
    available_slots=(
        AvailableSlot(1, "Dr. Priya", datetime.strptime("10:00", "%H:%M").time(),
                      datetime.strptime("10:30", "%H:%M").time(), "scaling"),
        AvailableSlot(1, "Dr. Priya", datetime.strptime("17:00", "%H:%M").time(),
                      datetime.strptime("17:30", "%H:%M").time(), "scaling"),
        AvailableSlot(1, "Dr. Priya", datetime.strptime("18:30", "%H:%M").time(),
                      datetime.strptime("19:00", "%H:%M").time(), "scaling"),
    ),
)

CLOCK = TrustedClock(
    now_utc=datetime(2026, 8, 10, 9, 0, tzinfo=__import__("datetime").timezone.utc),
    business_timezone="Asia/Kolkata",
    business_date=date(2026, 8, 10),
    day_of_week="monday",
)


# --- LLM clients ---
async def call_luna(messages: list[dict]) -> str:
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=90) as client:
                r = await client.post(
                    f"{BASE_URL}/v1/chat/completions",
                    headers={"Ocp-Apim-Subscription-Key": SUB_KEY, "Content-Type": "application/json", "user": "karthick"},
                    json={"model": "gpt-5.6-luna", "max_completion_tokens": 300,
                          "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + messages},
                )
                if r.status_code != 200:
                    return f"[ERROR {r.status_code}]"
                return r.json()["choices"][0]["message"]["content"]
        except (httpx.ConnectTimeout, httpx.ReadTimeout):
            if attempt < 2:
                await asyncio.sleep(5 * (attempt + 1))
                continue
            return "[ERROR timeout after 3 retries]"


async def call_claude(messages: list[dict]) -> str:
    for attempt in range(3):
        try:
            client = anthropic.AsyncAnthropic(
                api_key=os.environ.get("ANTHROPIC_API_KEY", "dummy"),
                base_url=BASE_URL,
                default_headers={"Ocp-Apim-Subscription-Key": SUB_KEY, "user": "karthick"},
            )
            msg = await client.messages.create(
                model="claude-opus-4-6", max_tokens=300,
                system=SYSTEM_PROMPT, messages=messages,
            )
            return msg.content[0].text
        except Exception as e:
            if attempt < 2:
                await asyncio.sleep(5 * (attempt + 1))
                continue
            return f"[ERROR {type(e).__name__} after 3 retries]"


# --- Scorer ---
def score_response(response: str, caller_text: str, bc: BookingCollection) -> list[str]:
    """Score one LLM response for defect classes."""
    defects = []

    # D1: medical advice
    if contains_medical_advice(response):
        defects.append("medical_advice_given")

    # invented availability — mentions a time not in offered slots
    # Normalize 12h/24h: "5:00" = "17:00", "6:30" = "18:30"
    offered_24 = {10, 11, 17, 18}  # hours that have offered slots
    offered_exact = {"10:00", "10:30", "11:00", "11:30", "17:00", "17:30", "18:30", "19:00"}
    time_mentions = re.findall(r"(\d{1,2}):(\d{2})", response)
    for h_str, m in time_mentions:
        h = int(h_str)
        canonical = f"{h:02d}:{m}"
        canonical_pm = f"{h+12:02d}:{m}" if h < 12 else canonical
        if canonical not in offered_exact and canonical_pm not in offered_exact:
            if h not in offered_24 and (h + 12) not in offered_24:
                defects.append("invented_availability")
                break

    # wrong language — Tamil input should get Tamil/Tanglish response
    tamil_in = any("஀" <= c <= "௿" for c in caller_text)
    if tamil_in:
        tamil_out = sum(1 for c in response if "஀" <= c <= "௿")
        if tamil_out == 0 and len(response) > 20:
            defects.append("wrong_language_response")

    # model ignores collection state — asks for wrong field
    req = bc.required_field
    if req == "name" and re.search(r"date|நாள்|தேதி|எப்ப", response, re.I):
        defects.append("model_ignores_collection_state")
    if req == "date" and re.search(r"name|பேரு|பெயர்", response, re.I):
        defects.append("model_ignores_collection_state")

    return defects


# --- Test scenarios ---
SCENARIOS = [
    # (title, turns_list)
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
    ("Self correction reason", ["Appointment வேணும் scaling", "sorry scaling இல்ல root canal வேணும்"]),
    ("All in one", ["நாளைக்கு மாலை 6:30 scaling appointment வேணும், என் பேரு Karthick"]),
    ("Elderly polite", ["டாக்டர் கிட்ட போகணும், பல்லு வலிக்குது"]),
    ("English only", ["I need a dental appointment for tomorrow"]),
    ("Pain then booking", ["எனக்கு பல்லு ரொம்ப வலிக்குது", "இன்னைக்கே போகணும்", "5 மணிக்கு", "முருகன்"]),
    ("Location query", ["Clinic எங்க இருக்கு?"]),
    ("Hours query", ["என்ன நேரம் திறக்கும்?"]),
    ("Multiple services", ["Scaling-um root canal-um வேணும்"]),
    ("Romanized Tamil", ["naalaikku scaling appointment venum", "6:30", "Karthick"]),
    ("Implicit booking", ["doctor பாக்கணும்"]),
]


async def run_ab(n_cases: int):
    results = {"luna": {}, "claude": {}}
    total_defects = {"luna": [], "claude": []}

    cases = SCENARIOS[:n_cases] if n_cases <= len(SCENARIOS) else SCENARIOS

    for idx, (title, turns) in enumerate(cases):
        print(f"\n{'='*60}")
        print(f"Case {idx+1}/{len(cases)}: {title}")
        print(f"{'='*60}")

        for model_name, call_fn in [("luna", call_luna), ("claude", call_claude)]:
            messages = []
            bc = BookingCollection()
            case_defects = []

            for turn_idx, caller_text in enumerate(turns):
                resolved = resolve_relative_date(caller_text, CLOCK)
                bc.update(caller_text, resolved_date=resolved, availability=AVAIL)

                messages.append({"role": "user", "content": caller_text})
                response = await call_fn(messages)
                messages.append({"role": "assistant", "content": response})

                defects = score_response(response, caller_text, bc)
                case_defects.extend(defects)

                print(f"  [{model_name:>6}] T{turn_idx+1} CALLER: {caller_text[:50]}")
                print(f"  [{model_name:>6}] T{turn_idx+1} AGENT:  {response[:80]}")
                if defects:
                    print(f"  [{model_name:>6}] T{turn_idx+1} DEFECTS: {defects}")

            results[model_name][title] = case_defects
            total_defects[model_name].extend(case_defects)

    # --- Summary ---
    print(f"\n{'='*60}")
    print("A/B COMPARISON SUMMARY")
    print(f"{'='*60}")

    from collections import Counter
    for model in ["luna", "claude"]:
        counts = Counter(total_defects[model])
        total = len(total_defects[model])
        print(f"\n{model.upper()} ({len(cases)} cases, {total} total defects):")
        if counts:
            for cls, count in counts.most_common():
                print(f"  {cls}: {count}")
        else:
            print("  No defects found")

    # Per-case comparison
    print(f"\n{'='*60}")
    print("PER-CASE COMPARISON")
    print(f"{'='*60}")
    print(f"{'Case':<35} {'Luna':<25} {'Claude':<25}")
    print("-" * 85)
    for title, _ in cases:
        luna_d = results["luna"].get(title, [])
        claude_d = results["claude"].get(title, [])
        luna_str = ", ".join(luna_d) if luna_d else "clean"
        claude_str = ", ".join(claude_d) if claude_d else "clean"
        print(f"{title:<35} {luna_str:<25} {claude_str:<25}")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--cases", type=int, default=20)
    args = p.parse_args()
    asyncio.run(run_ab(args.cases))
