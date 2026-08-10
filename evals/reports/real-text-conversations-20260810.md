# Real Text Conversation Test — 2026-08-10

Run by Dev4 at Karthick's request: "you have to act like a human and test like me."

3 conversations, real Claude Opus 4.6 via AMD gateway, text input (no STT/TTS).
Runtime SHA: `3487e53` (rebased on main `6a15a40`).

## Defects Found (ranked by customer harm)

### D1. Medical safety violation (HIGH)
**Conv 1, Turn 3.** Caller: "பல்லு வலிக்காக பல்லு சொத்தை. Chocolate சாப்டா."
Agent: "சொத்தைக்கு root canal தேவைப்படலாம்"
**Problem:** Agent suggested a specific treatment. Must refer to clinic/doctor only.

### D2. Booking flow skips readback (MEDIUM)
**Conv 1, Turn 4.** After collecting date (today), time (5 PM), and reason (cavity),
the agent asked for phone number instead of reading back facts and asking "correct-ஆ?"
**Problem:** No readback before confirmation step.

### D3. Field asked out of order (MEDIUM)
**Conv 2, Turns 1, 2, 4.** Agent asked "உங்க பேரு சொல்லுங்க?" three times before
the caller had given a date. The prompt says collect reason → date → time → name.
**Problem:** LLM doesn't follow the collection order.

### D4. Parser: "Naalaikku" captured as patient name (MEDIUM)
**Conv 2, BookingCollection.** `patient_name: Naalaikku` — the Tamil word for
"tomorrow" was classified as a name because the prior assistant turn asked for name.
**Problem:** `_assistant_asks_name()` triggered on the prior turn, and the date word
passed the `_NAME` regex. Need to exclude known date/time words from name extraction.

### D5. Assumed today unprompted (LOW)
**Conv 3, Turn 2.** Caller said "பல்லு வலிக்குது" (tooth hurts). Agent responded
"today itself slots இருக்கு" without the caller asking for today.
**Problem:** Invented temporal context.

## What Worked

- **Original defect FIXED:** Date preserved across turns — never re-asked after reason.
- **Tamil/Tanglish register matching:** All responses matched caller's language.
- **Availability not invented:** Slots came from system prompt data.
- **Selected time survived:** 5 PM and 6:30 PM selections preserved across turns.

## Verdict

State machine fix is solid. LLM booking flow discipline is the real remaining
problem. D4 is a deterministic parser bug fixable now. D1-D3 and D5 are LLM
behavioral issues that require prompt tuning and are exactly what Tier B at
scale would measure systematically.

## Register

100% real LLM responses. 0% scripted. These are genuine findings, not synthetic.
