# Evaluation Corpus Changelog

## QA.3 — Caller outcome completeness

- Added explicit `information_presented`, `authorization_denied`, `validation_rejected`, `runtime_recovered`, and `provider_recovered` outcomes.
- Migrated all 55 caller turns that previously had null outcomes.
- Validator now requires every caller turn to declare a non-null outcome.
- Moved positivity into the tool schema: quantity and new price use `positiveDecimalString`; generic decimal fields may be zero.
- Preserved null outcomes only for appropriate agent/system turns.

Audit artifacts:

- `reports/caller-outcomes.pre-qa3.json`
- `reports/caller-outcomes.post-qa3.json`
- regenerated `reports/tool-contract-mismatches.final.json`

## QA.2 final — Structured scalar, ID, and intent alignment

- Added reusable `decimalString`, explicit unit, and positive-integer ID definitions.
- Migrated 113 structured quantity/price fields to decimal strings; removed 40 unit-bearing strings and 50 JSON-number values.
- Ensured every one of the 95 quantity fields has an explicit normalized unit.
- Deterministically migrated 106 symbolic ID tokens across 8 namespaces; all 557 structured ID occurrences are now positive integers or allowed null selectors.
- Added `intent-contract.v1.json` and its self-schema.
- Migrated 168 historical intent labels across 317 turns to 21 canonical exact-scoring labels.
- Added recursive decimal/unit/ID validation and selector consistency checks.
- Recorded the `jsonschema` dependency/CI integration handoff; Dev1 subsequently declared and locked the dependency and added QA workflow steps. First real CI execution remains unobserved.

Audit artifacts:

- `reports/structured-values.pre-final.json`
- `reports/structured-values.post-final.json`
- `reports/id-migration-map.final.json`
- `reports/intent-migration.final.json`
- `reports/tool-contract-mismatches.final.json`

## QA.2 — Lifecycle-safe contract migration

- Added `tool-contract.schema.json` and strict tool-contract self-validation.
- Replaced ambiguous public tools with lifecycle-safe proposal/confirmation names.
- Marked commit-engine operations as internal and non-LLM-callable.
- Validated expected arguments by selected tool.
- Normalized database effects into tagged operations.
- Added outcome/error/write-policy consistency checks.
- Added coverage profiles: structure, Chennai pilot, and future all-India.
- Added human review workflow and separated language/domain/pilot provenance.

Pre-migration QA.1 baseline:

- 211 cases.
- 205 tool-call turns failed their selected argument schema.
- 432 individual argument-schema violations.

Post-QA.2 target/result:

- 211 cases.
- Zero tool contract, argument, policy, or write/effect mismatches.

## 210 → 211 case count

`INV-036` ("Stock enquiry for all products") was added during the supplemental inventory corpus pass. It is intentionally distinct from single-product stock inquiries: it tests listing all active products, calculating available quantity as on-hand minus reserved quantity, displaying out-of-stock products, excluding inactive products, and not exposing reserved quantities. The earlier 210 count was recorded before this supplemental case completed.

## Initial synthetic foundation

- Created ten JSONL corpora covering pending actions, inventory, appointments, authorization, multilingual behavior, medical safety, voice runtime, and provider routing.
- All cases started as synthetic, domain-unreviewed, and pilot-untested.
