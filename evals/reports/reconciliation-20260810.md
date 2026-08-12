# Reconciliation: 50-case A/B vs 10-case Two-Arm

## The disagreement
50-case A/B: Luna 20 CRITICAL, Claude 3 CRITICAL
10-case two-arm: Luna 9/10, Claude 9/10 (identical)

## My initial explanation (WRONG)
"Emoji/markdown in Claude's responses interfered with the scorer."
Tested: stripping emoji/markdown changes zero scores. The
false_confirmation regex catches booking-success keywords regardless
of surrounding decoration. Hypothesis killed.

## Actual explanation
The two instruments measured DIFFERENT TURNS.

50-case A/B: scored EVERY turn in the conversation. The PostLLMGate
only suppresses success language when `confirmed=True` (after the
caller says "ஆமா"). Pre-confirmation turns are scored raw.

10-case two-arm: scored only the FINAL turn (after confirmation).
Every model speaks success after confirmation — that's 9/10 for both.

**The 50-case gap is from PRE-CONFIRMATION turns.** Luna speaks
booking-success language BEFORE the caller confirms — "confirm
ஆயிடுச்சு" or "appointment booked" in the readback or name-collection
turns, before the caller has said yes. Claude does this less often.

## Why this matters
Speaking "confirm ஆயிடுச்சு" before the caller confirms is genuinely
worse than speaking it after. The caller hears success before they
agreed. On a voice call this is:

- Confusing: "I didn't say yes yet"
- Dangerous: caller may hang up believing the booking is done

## What the two-arm test actually proved
Both models speak success after confirmation (Arm A: 10/10 clean).
Both models speak success after confirmation without a receipt
(Arm B: 9/10 — gate needed). This is useful but does NOT address
the pre-confirmation eagerness that the 50-case test measured.

## What a discriminating test would need
Score each turn independently. Count false_confirmation on turns
where `confirmed` is NOT yet True. That directly measures:
"How often does this model claim success before the caller agrees?"

## Revised status
- The 50-case finding (Luna 20, Claude 3) is NOT refuted
- The two-arm finding (9/9 identical) is real but measures a
  different question (post-confirmation behavior)
- The emoji hypothesis is dead
- Luna may genuinely be more eager to speak success pre-confirmation
- NO VERDICT until a per-turn discriminating test confirms or refutes

## Previous findings voided (from my earlier report)
The three "voided findings" in receipt-two-arm-20260810.md are
RETRACTED — I cannot void them based on a test that measured
a different question. They remain open.
