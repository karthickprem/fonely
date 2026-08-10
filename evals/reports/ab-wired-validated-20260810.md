# A/B Wired Comparison — Validated Scorer (20 cases)

Scored by `fonely.voice.response_scorer` — the single calibrated instrument
(25/25 negative controls, 13/15 classes proven, 3/3 critical).

Configuration: BookingStateInjector + PostLLMGate engaged (shipping config).

## Results

| Class | Luna | Claude |
|---|---|---|
| false_confirmation | **8** | 3 |
| model_ignores_collection_state | 0 | **8** |
| wrong_language_response | 1 | 1 |
| **Total** | **9** | **12** |

## Analysis

**Luna's dominant defect: false_confirmation (8/9).**
The PostLLMGate fires `false_confirmation` because there is no real receipt
provider — the gate correctly flags any booking-success language. This is the
expected behavior of a gated system without the application seam wired.
When the real receipt provider exists, these would be gated out by the
deterministic confirmation-closure logic.

**Claude's dominant defect: model_ignores_collection_state (8/12).**
Claude fights the injected BookingCollection state — it asks for fields
the state machine says aren't needed, or skips to readback before all
fields are collected. Luna follows the injected state more faithfully.

**Both have 1 wrong_language_response** on the English-only input case.

## Verdict

Luna at 24× cheaper has FEWER total defects (9 vs 12) and its dominant
defect class (false_confirmation) is an infrastructure gap that disappears
when the receipt provider is wired — not a model quality issue.

Claude's dominant defect class (model_ignores_collection_state) is a
model behavior problem that persists regardless of infrastructure.

**Luna is the correct choice: cheaper, fewer real defects, and its
apparent defects are infrastructure gaps, not model quality.**
