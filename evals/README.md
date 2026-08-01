# Fonely Evaluation Framework

## Status

This is a synthetic adversarial requirements corpus. It is not a production acceptance gate and is not eligible for automated provider scoring until language/domain review and reproducible QA dependencies are complete.

## Files

- `schema/eval-case.schema.json`: strict case schema (version 2).
- `schema/tool-contract.schema.json`: strict registry schema for the tool contract.
- `tool-contract.v1.json`: lifecycle-safe public tool vocabulary and argument schemas.
- `schema/intent-contract.schema.json`: strict registry schema for canonical intents.
- `intent-contract.v1.json`: canonical exact-scoring intent labels and historical aliases.
- `cases/*.jsonl`: one evaluation case per line.
- `reports/tool-contract-mismatches.pre-qa2.json`: QA.1 mismatch baseline.
- `reports/tool-contract-mismatches.post-qa2.json`: zero-error QA.2 contract report.
- `CHANGELOG.md`: corpus and contract history.

## Lifecycle-safe public tools

Inventory/orders:

- `check_inventory`
- `create_pending_order`
- `revise_pending_order`
- `confirm_pending_order`
- `cancel_pending_order`

Appointments:

- `check_availability`
- `create_pending_appointment`
- `revise_pending_appointment`
- `confirm_pending_appointment`
- `cancel_pending_appointment`
- `reschedule_appointment`

Information and escalation:

- `get_business_information`
- `escalate_to_owner`

Owner management:

- `propose_stock_update` / `confirm_stock_update`
- `propose_price_update` / `confirm_price_update`
- `propose_schedule_update` / `confirm_schedule_update`

The following are internal application-engine operations and must never be LLM-callable: `begin_commit`, `complete_commit`, `fail_commit`, `internal_get`, `internal_get_active`.

Tenant identity, verified phone/role, and trusted engine context are injected by application code and are never accepted as LLM tool arguments.

## Structured scalar and identifier policy

- Quantities and prices are decimal strings only, matching `^(0|[1-9][0-9]*)(\.[0-9]{1,2})?$`.
- Positivity is schema-driven: order/stock quantities and new prices use `positiveDecimalString`; generic price, amount, or total fields may be zero where the product policy permits it.
- Units are separate required fields; never embed `kg`, `gram`, `litre`, or other units inside the numeric string.
- Public tool IDs use positive integers, aligned with the current backend MVP boundary. No opaque public-ID adapter exists yet.
- Product/service/resource name selectors remain available where the caller provides a name rather than an internal ID.

## Intent contract

`expected_intent` is machine-scoring input, not informational prose. Every non-null label must exist in `intent-contract.v1.json`. Historical labels such as `place_order`, `order_placement`, and `create_order` are retained only in the contract's alias history; corpus cases use canonical labels such as `order_create`.

Version the intent contract before changing label meaning.

## Per-turn scoring fields

- `expected_tool_policy`: `required`, `forbidden`, or `optional`.
- `expected_tool`: canonical public tool or null.
- `expected_arguments`: validated against the selected tool's strict schema.
- `expected_outcome`: stable outcome from the contract.
- `expected_error_code`: must match error outcomes and be null for non-errors.
- `expected_write_policy`: `none`, `pending_only`, or `commit`.
- `expected_database_effect`: tagged effect operation consistent with the write policy.
- `expected_response_constraints` / `forbidden_behaviors`: prose assertions requiring human or judge evaluation.

## Review provenance

Each case tracks three independent statuses:

- `language_review_status`: `synthetic` or `native_reviewed`.
- `domain_review_status`: `unreviewed`, `product_reviewed`, or `clinician_reviewed`.
- `pilot_validation_status`: `untested`, `passed`, or `failed`.

See `docs/qa/HUMAN_REVIEW_WORKFLOW.md`. Cases must not become release-blocking scoring oracles while domain review remains `unreviewed`.

## Phone fixtures

The corpus uses `+919900XXXXXX` project fixtures. These are not claimed to be telecom-reserved or guaranteed unallocated. The validator scans generic E.164-like numbers and rejects values outside the fixture pattern.

## Validation dependency

`validate-evals.py` requires `jsonschema>=4.26,<5`. Dev1 has declared it in the backend development extra, regenerated `backend/uv.lock`, and added corpus validation plus Chennai coverage steps to the CI workflow.

Normal validation uses the locked project environment:

```bash
backend/.venv/bin/python scripts/validate-evals.py
```

The CI steps are written but still require their first observed GitHub Actions run.

## Coverage profiles

```bash
# Default: structural + Chennai pilot (Tamil and Indian English) blocking
python scripts/report-eval-coverage.py

# Structural/domain thresholds only
python scripts/report-eval-coverage.py --profile structure

# Future all-India thresholds; expected to fail until reviewed locale depth grows
python scripts/report-eval-coverage.py --profile all-india

# Machine-readable output
python scripts/report-eval-coverage.py --json
```

The Chennai profile reports Hindi/Telugu/Kannada/Malayalam deficits as future, non-blocking gaps. Do not fill those gaps only by generating more unreviewed synthetic text.

## Provider adapters

A future adapter must:

1. Run corpus and contract validation first.
2. Seed `existing_state` without exposing trusted context to the model.
3. Replay caller/system turns.
4. Compare actual tool, arguments, outcome, error, write policy, and database effect to machine-readable expectations.
5. Evaluate spoken constraints separately.
6. Store results outside corpus files.

Do not build the provider benchmark runner until QA.2 dependency and human-review gates are approved.
