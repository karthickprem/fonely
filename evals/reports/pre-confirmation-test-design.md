# Pre-Confirmation False Confirmation Test — Design Document

Written BEFORE seeing results. Denominators declared in advance.

## Question
How often does each model speak booking-success language BEFORE
the caller has confirmed? And how many of those attempts survive
the gate to reach the caller?

## Two measurements per model

### Measurement 1: Raw model eagerness (pre-confirmation)
- Count: turns where the model speaks success language AND
  `confirmed` is NOT yet True
- Denominator A (per conversation): N conversations where the model
  spoke pre-confirmation success at least once / total conversations
- Denominator B (per turn): N pre-confirmation turns with success
  language / total pre-confirmation turns across all conversations

**Decision-relevant denominator: A (per conversation).** A caller
experiences one conversation. Whether the model was eager in 1 turn
or 3 turns of that conversation, the caller's experience is the same:
they heard a false confirmation before they agreed. Per-turn inflates
the count for longer conversations without changing the caller impact.

### Measurement 2: Gate survival (what the caller actually hears)
- Count: pre-confirmation turns where success language survived the
  gate and reached TTS
- Same two denominators, same decision: per-conversation is relevant
- Captured output: the exact text the caller would hear on each
  suppressed turn, to verify coherence

## What this test CAN distinguish
- Whether Luna speaks success pre-confirmation more often than Claude
  (the 50-case gap question)
- Whether the gate suppresses all pre-confirmation success language
- Whether suppressed turns produce coherent caller-facing output

## What this test CANNOT distinguish
- Whether the 50-case A/B's gap was caused by pre-confirmation
  eagerness or by something else in the scorer interaction
  (that would require re-running the exact 50-case scorer with
  per-turn logging, which is a different test)

## Design
- 20 scenarios (reuse first 20 from the 50-case set for comparability)
- Both models, with BookingStateInjector wired
- Per-turn scoring: for each turn, record whether `confirmed` was
  True at the time the model responded
- Post-gate output captured for every suppressed turn
- Report both denominators but use per-conversation for the verdict row
