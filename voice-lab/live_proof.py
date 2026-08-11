"""Live-proof harness: drive the PRODUCTION pipeline text-in, commit a real
booking, then assert the appointment row exists in PostgreSQL ON THE RIGHT DAY.

This runs the exact FrameProcessors the browser demo runs (from
backend/src/fonely/voice via production_wiring), against the seeded fonely_dev4
clinic. It replaces the microphone with text so the whole path can be exercised
and verified deterministically — STT is the only thing not exercised here (it's
the browser's job; measured separately).

The assertion that counts: row-on-the-right-day, queried from PG, not the
agent's spoken claim.
"""
import asyncio
import sys
import time as _time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, "/scratch/karthick/fonely-worktrees/dev4-cartesia/voice-lab")

import production_wiring as pw
from fonely.voice.language import detect_language

# Minimal fake Pipecat frames + context so we can drive the real processors.
from pipecat.frames.frames import (
    LLMContextFrame, LLMFullResponseStartFrame, LLMFullResponseEndFrame, LLMTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.processors.aggregators.llm_context import LLMContext

import os, httpx

BASE_URL = os.environ.get("ANTHROPIC_BASE_URL", "")
SUB_KEY = ""
for line in os.environ.get("ANTHROPIC_CUSTOM_HEADERS", "").splitlines():
    if "Ocp-Apim-Subscription-Key" in line:
        SUB_KEY = line.split(":", 1)[1].strip()

SYSTEM = open("/dev/stdin").read() if False else None


def _load_system_prompt():
    import booking_pipeline
    from datetime import datetime as _dt
    now = _dt.now(ZoneInfo("Asia/Kolkata"))
    return booking_pipeline.BOOKING_SYSTEM_PROMPT.format(
        today_display=now.strftime("%A, %B %d, %Y"),
        day_of_week=now.strftime("%A"),
    )


def call_llm(system, messages):
    t0 = _time.monotonic()
    r = httpx.post(
        f"{BASE_URL}/v1/chat/completions",
        headers={"Ocp-Apim-Subscription-Key": SUB_KEY, "Content-Type": "application/json", "user": "karthick"},
        json={"model": "gpt-5.6-luna", "max_completion_tokens": 300,
              "messages": [{"role": "system", "content": system}] + messages},
        timeout=60,
    )
    ms = (_time.monotonic() - t0) * 1000
    if r.status_code != 200:
        return f"[ERROR {r.status_code}]", ms
    return r.json()["choices"][0]["message"]["content"], ms


class _Capture:
    def __init__(self):
        self.text_parts = []

    async def cb(self, frame, direction):
        if isinstance(frame, LLMTextFrame):
            self.text_parts.append(frame.text)

    def text(self):
        return "".join(self.text_parts)


async def run_turn(injector, gate, system, history, caller_text):
    """One full turn: inject state → LLM → gate → return spoken text + latency."""
    # 1) Inject: feed an LLMContextFrame through the injector to update state +
    #    build the augmented context (this is the pre-LLM production processor).
    history.append({"role": "user", "content": caller_text})
    ctx = LLMContext(messages=list(history))

    cap_inj = _Capture()
    captured_ctx = {}

    async def inj_push(frame, direction):
        if isinstance(frame, LLMContextFrame):
            captured_ctx["ctx"] = frame.context
    injector.push_frame = inj_push
    await injector.process_frame(LLMContextFrame(context=ctx), FrameDirection.DOWNSTREAM)

    aug = captured_ctx.get("ctx")
    aug_messages = list(aug.messages) if aug else list(history)

    # 2) LLM on the augmented messages.
    raw, ms = call_llm(system, aug_messages)

    # 3) Gate: feed the LLM response frames through the post-LLM production gate.
    cap = _Capture()
    gate.push_frame = cap.cb
    await gate.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
    await gate.process_frame(LLMTextFrame(text=raw), FrameDirection.DOWNSTREAM)
    await gate.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)

    spoken = cap.text() or raw
    history.append({"role": "assistant", "content": spoken})
    return spoken, ms, raw, injector.caller_language


async def query_appointments(business_id, since_id=0):
    from sqlalchemy import text as sql_text
    async with pw._SessionLocal() as s:
        rows = await s.execute(sql_text(
            "SELECT id, service_name_snapshot, resource_name_snapshot, start_at, status, customer_name "
            "FROM appointments WHERE business_id=:b AND id > :sid ORDER BY id"
        ), {"b": business_id, "sid": since_id})
        return [dict(r._mapping) for r in rows.fetchall()]


async def main():
    print("=" * 70)
    print("LIVE PROOF — production pipeline, text-in, DB row assertion")
    print("=" * 70)

    conv_id = f"liveproof-{int(_time.time())}"
    injector, gate = pw.build_processors(conv_id)
    system = _load_system_prompt()

    tz = ZoneInfo("Asia/Kolkata")
    tomorrow = (datetime.now(tz) + timedelta(days=1)).date()
    print(f"Target booking day: {tomorrow} (tomorrow)")

    # Snapshot appointment ids before.
    before = await query_appointments(pw.DEMO_BUSINESS_ID)
    max_id_before = max([a["id"] for a in before], default=0)
    print(f"Appointments before: {len(before)} (max id {max_id_before})")
    print()

    # A complete Tamil booking for tomorrow.
    turns = [
        "நாளைக்கு cleaning appointment வேணும்",
        "காலைல 11:30",
        "கார்த்திக்",
        "ஆமா சரி",
    ]
    history = []
    latencies = []
    for t in turns:
        spoken, ms, raw, lang = await run_turn(injector, gate, system, history, t)
        latencies.append(ms)
        print(f"  Me: {t}")
        print(f"  Agent [{ms:.0f}ms, lang={lang}]: {spoken[:140]}")
        print()

    # Assert the row.
    after = await query_appointments(pw.DEMO_BUSINESS_ID, since_id=max_id_before)
    print("=" * 70)
    print("DB ASSERTION")
    print("=" * 70)
    if not after:
        print("✗ NO new appointment row — booking did NOT commit.")
    for a in after:
        local = a["start_at"].astimezone(tz)
        right_day = local.date() == tomorrow
        print(f"  Appointment #{a['id']}: {a['service_name_snapshot']} with {a['resource_name_snapshot']}")
        print(f"    start_at: {local} (day={'✓ RIGHT' if right_day else '✗ WRONG'})")
        print(f"    status: {a['status']}, customer: {a['customer_name']}")
        print(f"    RIGHT-DAY ASSERTION: {'PASS' if right_day else 'FAIL'}")

    if latencies:
        s = sorted(latencies)
        print()
        print(f"Latency (LLM only): avg={sum(latencies)/len(latencies):.0f}ms "
              f"p50={s[len(s)//2]:.0f}ms max={max(latencies):.0f}ms")


if __name__ == "__main__":
    asyncio.run(main())
