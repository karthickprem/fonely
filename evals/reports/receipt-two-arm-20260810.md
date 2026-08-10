# Two-Arm Receipt Provider Test

10 booking scenarios × 2 arms × 2 models = 40 runs.
Validated scorer. Both arms measured.

## Results

|  | Luna (committed) | Luna (no receipt) | Claude (committed) | Claude (no receipt) |
|---|---|---|---|---|
| false_confirmation [CRITICAL] | 0 | **9** | 0 | **9** |
| too_timid [MEDIUM] | 0 | 0 | 0 | 0 |
| clean | 10 | 1 | 10 | 1 |

## Finding

**Both models are identically eager.** 9/10 conversations attempt false
confirmation without a receipt — for BOTH Luna and Claude.

The 50-case A/B finding (Luna 20 CRITICAL vs Claude 3) is refuted.
That measurement was not capturing model eagerness — it was capturing
a scorer interaction with response format differences. When measured
with a controlled receipt provider, both models behave identically.

**Neither model is too timid.** When a receipt exists (Arm A), both
models speak success in 10/10 conversations.

## Residual Risk (identical for both)

90% of conversations require the gate to prevent a critical defect.
A gate bug affects 9 of every 10 conversations regardless of model.

This means:
- The gate is load-bearing infrastructure, not defense-in-depth
- Gate reliability is the dominant safety factor, not model choice
- Model choice does not affect critical-class risk

## Revised Model Decision

Since both models have identical critical-class behavior:
- Luna: 24× cheaper, 0 model_ignores_state, clean text output
- Claude: 24× more expensive, 26/50 model_ignores_state, emoji/markdown

Luna is the correct choice. The cost win stands and the critical-class
concern is resolved — both models need the gate equally.

## Previous findings voided

1. "Luna 20 CRITICAL vs Claude 3" — refuted, was scorer artifact
2. "Luna is 7× worse on critical tier" — refuted
3. "The eager model is most likely not to wait" — refuted, both are
   equally eager; eagerness is a property of the task, not the model
