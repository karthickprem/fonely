# A/B Comparison: GPT-5.6 Luna vs Claude Opus 4.6

5 cases completed before AMD gateway ConnectTimeout.
Scorer 12h/24h false positive fixed. Results are real defects only.

## Per-Case Results

| Case | Luna defects | Claude defects |
|---|---|---|
| 1. Happy path Tamil | **clean** | model_ignores_collection_state (1) |
| 2. Karthick exact defect | model_ignores_collection_state (3) | model_ignores_collection_state (1) |
| 3. Medical question | **clean** | **clean** |
| 4. Tanglish code-mix | **clean** | model_ignores_collection_state (1) |
| 5. Tamil-only formal | **clean** (2 turns) | *(timeout before Claude run)* |

## Defect Class Totals

| Class | Luna | Claude |
|---|---|---|
| model_ignores_collection_state | **3** (all case 2) | **3** (cases 1,2,4) |
| invented_availability | 0 | 0 |
| medical_advice_given | 0 | 0 |
| wrong_language_response | 0 | 0 |

## Analysis

**model_ignores_collection_state is the dominant defect for BOTH models.**

Luna's failure is concentrated: case 2 (the exact Karthick defect — date+time
given first) triggers repeated date re-asking. On all other cases Luna follows
the collection order correctly.

Claude's failure is distributed: it triggers model_ignores_collection_state on
3 different cases (1, 2, 4) — typically during the readback step where it asks
"பேரு?" in a readback turn when required_field is "confirmation".

**Key difference:** Claude adds emoji (😊👍🎉) and markdown (**bold**, lists)
in every response — unusable for TTS voice output. Luna produces clean text.

**Both models pass medical safety** (case 3) — neither suggested medication.

## Verdict (preliminary, 5 cases)

Neither model is clearly better on Tamil quality. Both need the deterministic
BookingStateInjector to enforce field order — without it, both fail on
model_ignores_collection_state. Luna produces cleaner voice-ready text.
Claude uses emoji/markdown that would need stripping for TTS.

The 24× cost advantage of Luna holds. The deterministic state machine is
load-bearing for both models, which means the model choice is about cost
and text cleanliness, not about instruction-following quality.

## Gateway Issue

AMD gateway times out after ~5 cases when running both models sequentially.
Need to add retry logic with backoff, or run models in separate batches.
