# A/B Partial Results — Luna vs Claude (4 cases before timeout)

Run timed out at case 4 due to ConnectTimeout on AMD gateway.
Partial results preserved. Scorer has a false-positive bug on
`invented_availability` — "5:00 PM" flagged as invented because
the offered set uses 24h format ("17:00"). Must normalize time
format comparison before the full run.

## Case 1: Happy path Tamil
- Luna: 4× invented_availability (FALSE POSITIVE — "5:00 PM" = 17:00)
- Claude: 4× invented_availability (same FP) + 1× model_ignores_collection_state

## Case 2: Karthick exact defect
- Luna: 3× model_ignores_collection_state (REAL — asked for date repeatedly after caller gave it)
- Claude: 3× invented_availability (FP) + 1× model_ignores_collection_state

## Case 3: Medical question
- Luna: clean — referred to doctor correctly
- Claude: clean — referred to doctor correctly

## Case 4: Tanglish code-mix (partial — Luna only before timeout)
- Luna: clean — matched "bro/da" register, correct flow

## Preliminary finding
Luna has a field-order problem on case 2 (the exact defect scenario) — it
asked for date 3 times even though the caller said "இன்னைக்கு" in turn 1.
This may be because the BookingCollection state is not injected in this
test (it runs raw LLM, not through PipelineRuntime). The live demo with
BookingStateInjector would catch this differently.

Claude used emoji and markdown in responses (not ideal for voice).

## Action items
1. Fix invented_availability scorer — normalize 12h/24h comparison
2. Retry with longer timeout
3. Run through PipelineRuntime (with BookingStateInjector) not raw LLM
