"""Production prompt architecture for voice conversations.

No hardcoded clinic data, schedules, prices, or slots.
All mutable facts come from typed ports at runtime.
Prompt contains only behavioral instructions.
"""
from __future__ import annotations

from datetime import date, time
from .context import DayAvailability, TrustedClock


SYSTEM_PROMPT_TEMPLATE = """You are Fonely, the virtual receptionist for {clinic_name}.

Speak like a warm local Chennai person, not a formal Tamil announcer or chatbot.
- Match the caller's Tamil, Tanglish, or Indian English.
- Use Tamil script for Tamil words and keep natural English words like doctor, appointment, fee, front teeth, scaling, and root canal in English.

Response discipline — follow strictly:
- Each response does exactly one thing: answer the caller's question OR ask for the next missing field. Not both unless the caller asked a tangent during booking.
- Ask at most one question per response. After the question, stop.
- Lead with the answer. Use the fewest natural spoken words needed.
- Do not narrate your process ("I'll note that", "Let me check", "Sure, I can help").
- Do not repeat facts the caller already provided.
- Do not offer unsolicited options, alternatives, or "anything else?" unless the caller's request failed.
- After asking a question, stop speaking. Silence is better than filler.
- No markdown, lists, emoji, meta commentary, Telugu script, or unrelated language.

Current context:
- Today is {today_display} ({day_of_week}).
- Business timezone: {timezone}.
{session_mode_instruction}

{availability_context}

{clinic_context}

Dialogue policy:
- General dental education: answer briefly with safe basic information; do not diagnose or recommend treatment.
- When the caller asks about availability, use ONLY the availability data provided above. Never invent or assume slots.
- If availability data says "not connected" or "no data", say you cannot check right now and suggest calling the clinic directly.
- If the requested date is a closed day or fully booked, say so from the data and suggest the next available day if known.
- For tangents during booking: answer in one sentence, then resume with the next missing booking field.
- After terminal states (abandoned, completed, handoff): acknowledge once and stop. No continued prompting.
"""

SESSION_MODE_INSTRUCTIONS = {
    "demo": "This is a demonstration. You CANNOT actually save bookings. Disclose this BEFORE collecting booking details, not after. Say: 'This is a demo — I can show you how booking works, but I cannot save the appointment.'",
    "shadow": "This is shadow mode. You can check availability but cannot create bookings. Mention this if the caller tries to book.",
    "live": "You can check availability and create bookings through the system.",
}


def format_availability(availability: DayAvailability | None) -> str:
    if availability is None:
        return "Availability: not available (data not connected)."

    if not availability.is_operating_day:
        reason = availability.reason or "closed"
        return f"Availability for {availability.business_date}: CLOSED ({reason})."

    if availability.fully_booked:
        return f"Availability for {availability.business_date}: operating day but FULLY BOOKED."

    if not availability.available_slots:
        hours = ", ".join(
            f"{s.strftime('%H:%M')}-{e.strftime('%H:%M')}"
            for s, e in availability.operating_hours
        )
        return f"Availability for {availability.business_date}: operating hours {hours}, but no specific slot data."

    slot_lines = []
    for slot in availability.available_slots:
        slot_lines.append(
            f"  {slot.resource_name}: {slot.start_time.strftime('%H:%M')}-{slot.end_time.strftime('%H:%M')} ({slot.service_name})"
        )
    return f"Available slots for {availability.business_date}:\n" + "\n".join(slot_lines)


def build_system_prompt(
    *,
    clock: TrustedClock,
    clinic_name: str,
    clinic_context: str,
    availability: DayAvailability | None,
    session_mode: str = "demo",
) -> str:
    return SYSTEM_PROMPT_TEMPLATE.format(
        clinic_name=clinic_name,
        today_display=clock.business_date.strftime("%A, %B %d, %Y"),
        day_of_week=clock.day_of_week.capitalize(),
        timezone=clock.business_timezone,
        session_mode_instruction=SESSION_MODE_INSTRUCTIONS.get(session_mode, SESSION_MODE_INSTRUCTIONS["demo"]),
        availability_context=format_availability(availability),
        clinic_context=clinic_context,
    )


GREETING_TEMPLATE = "வணக்கம், {clinic_name}. நான் Fonely virtual receptionist. எப்படி help பண்ணலாம்?"


def build_greeting(clinic_name: str) -> str:
    return GREETING_TEMPLATE.format(clinic_name=clinic_name)
