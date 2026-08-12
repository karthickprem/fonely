# A/B 50-Case Validated Results — Severity-Tiered

Scored by `fonely.voice.response_scorer` (25/25 negative controls).
Configuration: BookingStateInjector + PostLLMGate engaged.
50 cases per model, 50 distinct Tanglish scenarios.

## Results (by severity, never totaled)

| Severity | Luna | Claude |
|---|---|---|
| **CRITICAL** | **20** | **3** |
| HIGH | 0 | 0 |
| MEDIUM | 5 | 26 |
| LOW | 0 | 0 |

## Analysis

**Luna has 20 CRITICAL defects. Claude has 3.**

Luna speaks booking-success language approximately 7× more eagerly
than Claude when no receipt exists. This is the exact behavior that
becomes a patient showing up at a clinic that isn't expecting them.

The CEO's prediction was correct: "The eager model is not made safe
by giving it a receipt to wait for. It is the model most likely not
to wait."

**Claude has 26 MEDIUM defects (model_ignores_collection_state).**
It fights the BookingStateInjector — asks for fields the state machine
says aren't needed. This is irritating but recoverable and invisible
to the caller's health.

## Verdict (corrected)

The 24× cost saving comes with a 7× CRITICAL defect increase.
The model choice cannot be made on cost alone. The question is
whether the deterministic PostLLMGate can suppress Luna's eagerness
to speak success — a stub receipt provider test is needed before
deciding.

Previous verdict ("Luna wins") was wrong because it summed across
severity tiers. This correction uses the taxonomy as designed.
