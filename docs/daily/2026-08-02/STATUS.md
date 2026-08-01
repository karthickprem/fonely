# Fonely Daily Status — 2026-08-02

## Executive summary

Two parallel workstreams completed substantial work:

- **Dev1** implemented Phase B, the deterministic `PendingAction` lifecycle.
- **Dev2** created PostgreSQL test infrastructure, migration checks, and GitHub Actions CI configuration.

Independent review confirmed strong local progress, but Phase B requires a bounded **Phase B.1 hardening pass** before Phase C starts. The largest remaining blocker is that no PostgreSQL integration test has executed against a real PostgreSQL instance.

Current verified local state:

```text
Ruff:                  PASS
Formatting:            PASS
Mypy strict:           PASS
Non-PostgreSQL tests:  232 passed
PostgreSQL contracts:  17 collected, skipped
Full suite:            232 passed, 17 skipped
Alembic head:          0002
```

---

## 1. Phase B implementation completed by Dev1

### Implemented architecture

Dev1 added a provider-independent, deterministic pending-action layer supporting future:

- Orders.
- Appointments.
- Owner stock updates.
- Owner price updates.
- Owner schedule updates.

Implemented modules include:

```text
src/fonely/domain/pending_actions/
├── commands.py
├── errors.py
├── lifecycle.py
├── payloads.py
├── results.py
├── snapshots.py
└── transitions.py

src/fonely/repositories/pending_actions.py
src/fonely/services/authorization.py
src/fonely/services/pending_actions.py
```

### State model

Implemented states:

```text
collecting_details
awaiting_confirmation
committing
confirmed
rejected
cancelled
expired
```

Implemented transition policy:

```text
collecting_details
├── awaiting_confirmation
├── cancelled
└── expired

awaiting_confirmation
├── collecting_details (revision)
├── committing
├── rejected
├── cancelled
└── expired

committing
├── confirmed
├── awaiting_confirmation (retryable failure)
└── rejected (non-retryable failure)

Terminal:
├── confirmed
├── rejected
├── cancelled
└── expired
```

### Boundary and correctness mechanisms

Implemented:

- Strict Pydantic command models.
- Versioned payload registry.
- Pending-order proposal payload.
- Owner stock-update proposal payload.
- Unknown-field/schema/action mismatch rejection.
- Float quantity rejection.
- Duplicate product-line rejection.
- Timezone-aware datetime validation.
- Stable canonical JSON.
- SHA-256 payload digest.
- Deterministic confirmation snapshots.
- Tenant-scoped repository queries.
- Conditional status/version updates.
- PostgreSQL `ON CONFLICT DO NOTHING RETURNING` for idempotent create.
- BusinessUser-backed owner/manager authorization.
- Customer ownership checks for existing actions.
- Tenant-scoped product ownership checks.
- Expiry and bounded bulk expiry.
- Immutable typed public result models.
- Safe machine-readable error/rejection fields.

### Migration added

```text
0002 — pending_action_state_machine
```

Adds:

- `pending_actions.payload_digest`
- `pending_actions.rejection_reason_code`

Offline upgrade/downgrade SQL renders, and migration parity tests cover migrations `0001` and `0002`.

---

## 2. Phase B local verification

Independently verified:

```text
.venv/bin/ruff check .                 PASS
.venv/bin/ruff format --check .        PASS
.venv/bin/mypy src                     PASS
.venv/bin/pytest -m "not postgres" -q  232 passed
.venv/bin/pytest -m postgres -q        17 skipped
.venv/bin/pytest -q                    232 passed, 17 skipped
.venv/bin/alembic heads                0002 (head)
```

PostgreSQL tests are collected, not silently absent.

### PostgreSQL contracts written

The 17 contracts target:

- Migration application.
- Tenant-scoped idempotent create.
- Same key under separate businesses.
- Conflicting payload under same key.
- Concurrent create.
- Concurrent begin-commit.
- Stale version.
- Wrong state.
- Exact expiry boundary.
- Bulk expiry idempotency.
- Cross-tenant lookup denial.
- Invalid enum rejection.
- Transaction rollback.
- Migration lifecycle.
- Active, cross-tenant, and inactive owner membership.

Status: 🚫 Not executed because PostgreSQL is unavailable locally.

---

## 3. Independent Phase B review findings

Phase C is **not yet approved**. Required Phase B.1 corrections:

### P0 — Migration `0002` upgrade safety

`payload_digest` is added as `NOT NULL` without a backfill. Upgrading a populated `0001` database will fail.

Required fix:

1. Add nullable column.
2. Backfill existing pending actions using the canonical digest algorithm.
3. Alter to non-null.
4. Add a migration test starting from populated revision `0001`.

### P0 — Trusted commit completion boundary

Current customer actor context can invoke complete-commit semantics for an arbitrary entity ID/type after owning an action.

Required fix:

- Separate caller-facing actor operations from trusted transaction-engine completion.
- Do not expose begin/complete/fail commit as LLM/customer tools.
- Verify committed entity exists, belongs to the same business, and was created in the same transaction.
- Enforce mapping:

```text
order pending action              → order entity
appointment pending action        → appointment entity
owner stock update pending action → inventory update entity
```

### P0 — Read authorization

`get()` and `get_active()` lack actor authorization and may expose another customer's payload/PII when IDs are known.

Required fix:

- Actor-authorized read queries.
- Customer may read only their own action.
- Owner/manager requires active BusinessUser membership.
- Separate explicitly internal system queries where required.

### P1 — Canonical order-line ordering

Dictionary keys are canonicalized, but list order is preserved. Equivalent orders with lines in a different order produce different digests.

Required fix:

- Sort validated order lines by product ID before digest and confirmation snapshot.
- Add regression tests proving order-independent equality.

### P1 — Expiry-aware active lookup

`get_active_for_session()` filters statuses but not expiry. Expired collecting/awaiting actions remain active until a worker updates them.

Required query behavior:

```text
status = committing
OR (
  status IN (collecting_details, awaiting_confirmation)
  AND expires_at > now
)
```

### P1 — Historical access after product deactivation

Stored-action reads require `Product.is_active = true`. Deactivating a product can make existing actions unreadable or impossible to cancel.

Required split:

- Create/revise validation: ownership + active product.
- Historical read/cancel validation: ownership regardless of active status.
- Confirmation may revalidate current product policy separately.

### P1 — Test database fixture safety

The PostgreSQL fixture performs destructive downgrade/truncate operations but currently:

- Does not require `FONELY_ALLOW_DESTRUCTIVE_TEST_DB=1`.
- Accepts any database name merely containing `test`.

Required fix:

```text
FONELY_ALLOW_DESTRUCTIVE_TEST_DB=1
Database name: ^fonely_test(_[a-z0-9_]+)?$
```

### P2 — Identity-map refresh consistency

Concurrency-sensitive `get_by_idempotency_key()` should use `populate_existing=True`, matching `get_by_id()`.

### P2 — Safe commit messages

Keyword blocking is brittle. Prefer fixed application-authored safe messages selected by allowlisted error code rather than caller-provided arbitrary text.

---

## 4. Dev2 PostgreSQL/CI infrastructure

### Files created

```text
infra/postgres/compose.yaml
scripts/test-postgres.sh
scripts/check-migrations.sh
.github/workflows/backend-ci.yml
docs/testing/POSTGRESQL.md
docs/testing/POSTGRES_FINDINGS.md
```

### Infrastructure provided

- PostgreSQL 16 Alpine Compose service.
- Local test database `fonely_test` on localhost port `55432`.
- Local destructive-test wrapper.
- Offline Alembic migration checker.
- GitHub Actions workflow with PostgreSQL service container.
- PostgreSQL testing documentation.
- Infrastructure finding handoff.

### Dev2 hardening completed

- Database URL is passed to Python health check through an environment variable, not interpolated into source.
- Database-name allowlist tightened to:

```text
^fonely_test(_[a-z0-9_]+)?$
```

- Remote hosts require explicit opt-in.
- Migration chain is derived from Alembic history and supports annotated/alphanumeric revision IDs.
- Migration files are cross-checked against Alembic history.
- CI triggers on pushes and pull requests.
- `.venv` removed from CI cache.
- uv cache key includes lockfile when available.
- GitHub actions pinned by commit SHA with release comments.
- Documentation updated to match scripts.

### Independently verified Dev2 checks

```text
bash -n scripts/test-postgres.sh     PASS
bash -n scripts/check-migrations.sh  PASS
scripts/check-migrations.sh          PASS
Compose YAML parse                   PASS
GitHub workflow YAML parse           PASS
```

Migration checker observed:

```text
Revision chain:      0001 → 0002
Single head:         yes
CREATE TABLE:        19
ADD COLUMN:           2
CHECK constraints:   31
CREATE INDEX:         4
Errors/warnings:      0
```

---

## 5. Remaining infrastructure corrections

### PostgreSQL fixture ownership handoff

Dev1 must update `backend/tests/integration/postgres/conftest.py` to enforce the destructive opt-in and strict database-name pattern. Dev2 correctly documented this but did not own the file.

### Reproducible dependency lock

`backend/uv.lock` is still needed.

Required:

```bash
cd backend
uv lock
```

Then CI should always use:

```bash
uv sync --frozen --all-extras
```

A missing lockfile should fail rather than fall back to floating dependencies.

### CI not executed

The workflow exists but has never run. It must be pushed to the private GitHub repository and observed.

### Action SHA verification

Pinned action SHAs should be independently verified against official action release tags before relying on the workflow.

### Remote database policy

For the initial stage, localhost and the disposable GitHub Actions service container are sufficient. Arbitrary remote destructive-test access should remain disabled unless an explicit hostname allowlist is introduced.

---

## 6. GitHub repository setup

Personal private repository created:

```text
https://github.com/karthickprem/fonely
```

Dedicated SSH key authentication has been configured and verified for GitHub identity:

```text
karthickprem
```

Repository access succeeds using the dedicated key. The remote repository is currently empty.

Before the first push:

1. Finish Dev1 Phase B.1 correction pass.
2. Finish remaining Dev2/fixture safety corrections.
3. Generate `backend/uv.lock`.
4. Strengthen root `.gitignore`.
5. Confirm both `.env` files are excluded.
6. Scan all candidate files for API keys/tokens and generated artifacts.
7. Initialize Git and inspect the complete staged file list.
8. Create the initial commit.
9. Push using the dedicated personal SSH identity.
10. Observe and fix the first PostgreSQL CI run.

No GitHub token is needed because SSH authentication works.

---

## 7. Scale/production-readiness status

Fonely is being built with production-oriented principles and regression testing, but is not yet ready for thousands of users.

Current maturity:

```text
Built for correctness:             Yes
Regression tested locally:         Yes
PostgreSQL integration verified:   No
Pilot-ready:                       No
Ready for thousands:               No
Architected to evolve there:       Yes
```

Production-oriented mechanisms already present:

- Async PostgreSQL foundation.
- Tenant scoping.
- Database constraints.
- Typed command/result boundaries.
- Explicit state machine.
- Idempotency and conditional versions.
- Migrations and migration parity checks.
- CI/PostgreSQL infrastructure.
- Regression tests.

Still required for pilot and scale:

- Phase B.1 hardening.
- Real PostgreSQL execution.
- Inventory/order engine.
- Appointment scheduling and non-overlap.
- Provider-independent AI boundary.
- Exotel and WhatsApp production integrations.
- Secrets management.
- Observability and trace IDs.
- Rate limits, quotas, and abuse controls.
- Backups and restore testing.
- Load and soak tests.
- Provider outage handling.
- Privacy, retention, and regulatory review.

Scale target must progress in stages:

```text
5–10 pilot businesses
→ 50–100 businesses
→ 1,000 businesses
→ thousands
```

---

## 8. Workstream assignment

### Dev1 — Phase B.1 hardening

Assigned:

1. Migration `0002` backfill safety.
2. Trusted internal commit-completion boundary.
3. Action/entity type mapping.
4. Actor-authorized reads.
5. Canonical order-line sorting.
6. Expiry-aware active query.
7. Historical versus active product validation.
8. Identity-map refresh.
9. Destructive PostgreSQL fixture safety.
10. Populated-`0001` migration-upgrade test.

### Dev2 — infrastructure finalization

Assigned/remaining:

1. Keep hardened testing scripts and docs aligned.
2. Verify action SHAs.
3. Require lockfile after Dev1 generates `uv.lock`.
4. Do not modify domain code.

---

## 9. Next bounded objective

> Complete Phase B.1 and infrastructure safety, generate the lockfile, push to private GitHub, and make the first PostgreSQL CI run green.

Phase C must not begin until:

- Phase B.1 review passes.
- PostgreSQL migration/tests actually execute successfully.
- CI is green.

Only after those gates should the deterministic inventory/order engine begin.

---

## Dev2 — QA and adversarial evaluation foundation

### Files created

**Evaluation infrastructure:**

| File | Purpose |
|------|---------|
| `evals/schema/eval-case.schema.json` | Strict JSON Schema v1 for evaluation cases |
| `evals/README.md` | Format documentation and provider adapter guide |
| `scripts/validate-evals.py` | Corpus validator (schema, duplicates, credentials, phones) |
| `scripts/report-eval-coverage.py` | Coverage reporter (text and `--json` modes) |

**Evaluation corpus (10 JSONL files, 210 cases):**

| File | Cases | Domain |
|------|-------|--------|
| `evals/cases/appointments.jsonl` | 50 | appointment |
| `evals/cases/multilingual_tamil.jsonl` | 20 | multilingual |
| `evals/cases/multilingual_hindi.jsonl` | 14 | multilingual |
| `evals/cases/multilingual_south_indian.jsonl` | 5 | multilingual |
| `evals/cases/inventory_orders.jsonl` | 36 | inventory |
| `evals/cases/authorization_security.jsonl` | 30 | authorization |
| `evals/cases/pending_actions.jsonl` | 25 | pending_action |
| `evals/cases/medical_safety.jsonl` | 16 | medical_safety |
| `evals/cases/voice_runtime.jsonl` | 10 | voice_runtime |
| `evals/cases/provider_routing.jsonl` | 5 | provider_routing |

**QA documentation (8 files):**

| File | Purpose |
|------|---------|
| `docs/qa/TEST_STRATEGY.md` | Testing pyramid (8 levels) |
| `docs/qa/APPOINTMENT_ACCEPTANCE.md` | Appointment engine acceptance criteria |
| `docs/qa/INVENTORY_ACCEPTANCE.md` | Inventory engine acceptance criteria |
| `docs/qa/VOICE_AND_LANGUAGE_EVAL.md` | Voice/language evaluation framework |
| `docs/qa/PRODUCTION_READINESS_CHECKLIST.md` | Staged readiness (5 stages) |
| `docs/qa/LOAD_TEST_PLAN.md` | Workload models (10 to 10K businesses) |
| `docs/qa/PILOT_SCORECARD.md` | Pilot metrics and go/no-go thresholds |
| `docs/qa/IMPLEMENTATION_FINDINGS.md` | Code defects in existing implementation |

### Coverage summary

```text
Total cases:               211
All coverage thresholds:   MET

By domain:
  appointment              50  (required 40)
  multilingual             39  (required 35)
  inventory                36  (required 35)
  authorization            30  (required 25)
  pending_action           25  (required 25)
  medical_safety           16  (required 15)
  voice_runtime            10  (required 10)
  provider_routing          5  (required  5)

By locale:
  en-IN                   171
  ta-IN                    20
  hi-IN                    14
  te-IN                     2
  kn-IN                     2
  ml-IN                     1

Critical cases:             35  (16.7%)
Cases with forbidden:      210  (100%)
```

### Validation output

```text
validate-evals.py:            211 cases, all valid
report-eval-coverage.py:      all thresholds met
report-eval-coverage.py --json: valid JSON output
ruff check scripts:           PASS
ruff format --check scripts:  PASS
```

### Synthetic / native-review status

All 211 cases have `native_review_status: synthetic_unreviewed`. No case claims native-speaker verification. All multilingual cases include notes acknowledging synthetic generation.

### Implementation findings

Documented in `docs/qa/IMPLEMENTATION_FINDINGS.md`:

1. P1 — `lifecycle.py` is unused by the service layer (divergent parallel implementations)
2. P1 — Customer ownership check uses phone number only (same-phone callers can cross-mutate)
3. P1 — Appointment model lacks exclusion constraint (double-booking possible)
4. P1 — Payload registry only covers ORDER and OWNER_STOCK_UPDATE
5. P2 — No explicit connection pool sizing
6. P2 — Session generator lacks explicit transaction boundaries
7. P2 — Database URL defaults mask misconfiguration
8. P2 — Missing index on `(business_id, initiated_by)`

### Remaining human/native-speaker review needs

- All 20 Tamil (ta-IN) cases require native Tamil speaker review
- All 14 Hindi (hi-IN) cases require native Hindi speaker review
- All 5 South Indian (te-IN, kn-IN, ml-IN) cases require native speaker review
- Medical safety cases (16) require clinical review for escalation language appropriateness
- Appointment/inventory acceptance criteria require Dev1 review for implementation alignment

### No Dev1-owned files were modified

### Recommended next QA step

1. Native Tamil speaker reviews `multilingual_tamil.jsonl` (highest priority for Chennai pilot)
2. Dev1 reviews acceptance criteria docs against Phase C/D implementation plans
3. After Phase B.1 completes, run eval cases against the first LLM integration
4. Build a provider adapter that loads JSONL, sends to Sarvam/DeepSeek/Qwen, and scores responses

---

### QA.1 hardening pass

Applied after review identified 11 blocking and improvement items.

**Changes made:**

1. **Validator uses jsonschema when available** — `Draft202012Validator` validates each record against `eval-case.schema.json`. At this checkpoint, `jsonschema` was manually available in one local environment but was not declared or locked; QA.2 records this as a reproducibility blocker.

2. **Coverage reporter fails on malformed JSON** — `contextlib.suppress(json.JSONDecodeError)` replaced with explicit error collection and `sys.exit(1)` on any malformed line.

3. **Canonical tool contract created** — `evals/tool-contract.v1.json` defines 11 tools with argument schemas, 12 outcome codes, and 3 write policies. Validator cross-checks `expected_tool`, `expected_outcome`, and `expected_write_policy` against it.

4. **Machine-readable outcome/error/write policies added** — Schema v2 adds per-turn: `expected_tool_policy` (required/forbidden/optional), `expected_outcome` (success/unauthorized/insufficient_stock/...), `expected_error_code`, `expected_write_policy` (none/pending_only/commit). All 211 cases migrated.

5. **Empty-turn assertion check enforced** — Validator requires every turn to have at least one verifiable assertion. 8 turns fixed. Result: 377/377 turns with assertions.

6. **Generic E.164 phone scanning** — `\+[1-9]\d{6,14}` replaces the India-only `\+91\d{10}`. Non-fixture numbers of any country are rejected. Fixture numbers documented as project-internal, not telecom-reserved.

7. **Review provenance split** — `native_review_status` replaced with `language_review_status` (synthetic/native_reviewed), `domain_review_status` (unreviewed/product_reviewed/clinician_reviewed), `pilot_validation_status` (untested/passed/failed). All 211 cases migrated.

8. **Per-locale coverage thresholds added** — ta-IN: 20, hi-IN: 20, te-IN: 15, kn-IN: 15, ml-IN: 15, en-IN: 20. Currently ta-IN and en-IN are met; hi-IN/te-IN/kn-IN/ml-IN are below target. These are intentional gaps — generating more synthetic unreviewed text has diminishing value.

9. **Credential scanning expanded** — Added GitHub PAT, private key block, Bearer token, and database URL patterns.

10. **Schema bumped to v2** — All 211 cases updated. Old v1 cases will fail validation.

**Validation results after QA.1:**

```text
validate-evals.py:    211 cases, all valid (jsonschema + tool contract + assertions)
report-eval-coverage: domain thresholds met, locale gaps documented
ruff check:           PASS
ruff format:          PASS
```

**Files created:**
- `evals/tool-contract.v1.json`

**Files modified:**
- `evals/schema/eval-case.schema.json` (v1 → v2)
- `evals/README.md` (updated for v2, tool contract, provenance split)
- `scripts/validate-evals.py` (jsonschema, tool contract, E.164, assertions)
- `scripts/report-eval-coverage.py` (fail on malformed, per-locale, provenance)
- All 10 JSONL corpus files (v2 migration)

**Not done (requires human reviewers):**
- Native Tamil/Hindi/Telugu/Kannada/Malayalam speaker review
- Clinical review of medical safety cases
- Product domain review of acceptance criteria
- Additional locale corpus (deferred until native reviewers available)

---

### QA.2 contract and scorer-readiness hardening

QA.1's claim that tool arguments were contract-validated was incorrect. QA.2 measured the mismatch before modifying the contract/corpus, then migrated deliberately to the founder-approved lifecycle-safe vocabulary.

**Pre-migration evidence:**

```text
Cases:                              211
Failing tool-call turns:            205
Individual argument violations:     432
  additionalProperties:             204
  required:                         226
  enum:                               2
```

Saved as `evals/reports/tool-contract-mismatches.pre-qa2.json`.

**Canonical public tools approved:**

```text
check_inventory
create_pending_order / revise_pending_order / confirm_pending_order / cancel_pending_order
check_availability
create_pending_appointment / revise_pending_appointment
confirm_pending_appointment / cancel_pending_appointment / reschedule_appointment
get_business_information / escalate_to_owner
propose_stock_update / confirm_stock_update
propose_price_update / confirm_price_update
propose_schedule_update / confirm_schedule_update
```

Internal and non-LLM-callable: `begin_commit`, `complete_commit`, `fail_commit`, `internal_get`, `internal_get_active`.

**QA.2 changes:**

1. Added `evals/schema/tool-contract.schema.json` and contract self-validation.
2. Replaced ambiguous `place_order` / `book_appointment` names with proposal/confirmation lifecycle names.
3. Added per-tool `Draft202012Validator` argument validation.
4. Enforced required/forbidden/optional tool-policy and null consistency.
5. Enforced outcome/error consistency and each tool's allowed outcomes/write policies.
6. Replaced free-form database effects with tagged operations consistent with `none`, `pending_only`, or `commit`.
7. Migrated all 211 cases (377 turns) intentionally and removed trusted tenant/caller identity from LLM arguments.
8. Added structure, Chennai-pilot, and all-India coverage profiles. Chennai-pilot is the default gate; future locale gaps are non-blocking in that profile.
9. Added semantic fingerprints for exact/near-duplicate human review.
10. Added `docs/qa/HUMAN_REVIEW_WORKFLOW.md` and `evals/CHANGELOG.md`.
11. Reframed phone-based customer identity as a product security decision, not a session-binding code fix.
12. Recorded the missing reproducible `jsonschema` dependency as a P0 QA handoff to Dev1.

**Post-migration validation:**

```text
Cases:                              211
Turns:                              377
Failing turns:                        0
Individual mismatches:                0
```

Saved as `evals/reports/tool-contract-mismatches.post-qa2.json`.

**Coverage policy:**

- `structure`: domain/structural requirements only.
- `chennai-pilot` (default): structure plus ta-IN >= 20 and en-IN >= 20; expected to pass.
- `all-india`: adds hi-IN >= 20, te-IN >= 15, kn-IN >= 15, ml-IN >= 15; expected to fail until native-reviewed depth grows.

Do not satisfy future locale targets merely by generating more unreviewed synthetic cases.

**210 -> 211 audit:** `INV-036` was added as an explicit all-products inventory enquiry. It tests active-product filtering, available quantity calculation, out-of-stock display, inactive-product exclusion, and reservation privacy. See `evals/CHANGELOG.md`.

**Dependency/CI blocker handed to Dev1:**

- Add `jsonschema>=4.26,<5` to a reproducible QA/dev dependency group.
- Regenerate `backend/uv.lock` and verify `uv sync --frozen --all-extras`.
- Add `scripts/validate-evals.py` and Chennai-profile coverage reporting to CI.
- Dev2 did not edit `backend/pyproject.toml`, `backend/uv.lock`, or `.github/workflows/backend-ci.yml`.

**Review status remains honest:** 211 language-synthetic, 211 domain-unreviewed, 211 pilot-untested. No provider benchmark runner was built.

**Exact QA.2 verification:**

```text
Temporary isolated validator environment: PASS — 211 cases / 377 turns
Post-QA.2 mismatch report:           PASS — 0 failing turns / 0 mismatches
Structure coverage profile:          exit 0
Chennai-pilot coverage profile:      exit 0
All-India coverage profile:          exit 1 (expected locale gaps)
Coverage JSON output:                valid; blocking_failures separate from future_gaps
Negative validator fixtures:         9/9 rejected as expected
Ruff check:                          PASS
Ruff format check:                   PASS
Obsolete/internal public tools:      0
Trusted context in tool arguments:   0
Clean project venv jsonschema import: FAIL — ModuleNotFoundError (handoff blocker)
```

Backend/domain files listed as newer during the ownership scan were concurrent Dev1 changes. Dev2 changed only `evals/**`, `docs/qa/**`, the two QA scripts, and this appended status section.

---

### QA.2 final scalar, ID, and intent patch

The final review found that the zero-mismatch contract still accepted unit-bearing quantity strings, JSON floats, symbolic IDs incompatible with current backend commands, and 168 inconsistent intent labels.

**Pre-final audit (`evals/reports/structured-values.pre-final.json`):**

```text
Structured numeric fields:          113
  unit-bearing strings:              40
  numeric strings:                   23
  JSON numbers:                      50
Structured ID fields:               559
Symbolic ID namespaces:               8
Unique intent labels:               168
Turns with non-null intent:         317
```

**Final contract policy:**

- Quantity and price fields are string-only decimals with at most two decimal places.
- Units are separate required fields.
- Tool-call quantities/prices must be greater than zero.
- Public IDs are positive integers, aligned with current backend MVP command/payload models.
- `expected_intent` is exact-scoring input from `evals/intent-contract.v1.json`; old labels remain aliases only.

**Migration result:**

```text
Structured numeric fields:          113 strings / 0 invalid
Unit-bearing numeric strings:         0
Quantity fields missing unit:         0
Structured ID fields:               557
Invalid or symbolic structured IDs:   0
Historical intent labels:           168
Canonical labels used:               21
Non-null intent turns:              317
```

ID migration mapped 106 symbolic tokens across action, appointment, business, order, product, reservation, resource, and service namespaces. The mapping is recorded in `evals/reports/id-migration-map.final.json`.

**Files added:**

- `evals/intent-contract.v1.json`
- `evals/schema/intent-contract.schema.json`
- `evals/reports/structured-values.pre-final.json`
- `evals/reports/structured-values.post-final.json`
- `evals/reports/id-migration-map.final.json`
- `evals/reports/intent-migration.final.json`
- `evals/reports/tool-contract-mismatches.final.json`

**Accurate completion statement:** QA.2 code, contract, and corpus hardening are complete. Reproducible dependency and CI integration are **not complete** and remain blocked on Dev1-owned `backend/pyproject.toml`, `backend/uv.lock`, and workflow files. Human language, product-domain, clinical, and pilot validation also remain undone.

---

### QA.3 caller-outcome completeness patch

Independent review found 55 caller turns with null `expected_outcome`, weakening deterministic scoring for denials, validation failures, informational responses, and recovery flows.

**Pre-QA.3 (`evals/reports/caller-outcomes.pre-qa3.json`):**

```text
Caller turns missing outcome:        55
  authorization denial:              25
  response/information presented:    25
  validation rejection:               2
  runtime recovery:                   2
  provider recovery:                  1
```

**New stable outcomes:**

- `information_presented`
- `authorization_denied`
- `validation_rejected`
- `runtime_recovered`
- `provider_recovered`

All 55 turns were migrated and recorded in `evals/reports/caller-outcomes.post-qa3.json`. The validator now enforces `speaker=caller -> expected_outcome != null`. Agent/system turns may remain null where no deterministic outcome assertion applies.

Decimal positivity was also refined: `positiveDecimalString` is applied by tool schema to quantities and new prices, while generic price/amount/total fields may be zero where product policy permits.

**Dependency/CI handoff resolved concurrently by Dev1:** `jsonschema>=4.26,<5` is declared in the dev extra, locked at 4.26.0, importable from the project venv, and the workflow includes strict corpus validation plus the Chennai coverage profile. The workflow itself still requires its first real GitHub Actions execution.

---

## Dev1 — Pre-CI dependency and repository readiness

### Reproducible QA dependency

- Added `jsonschema>=4.26,<5` to `backend`'s existing `dev` optional dependencies.
- Regenerated `backend/uv.lock` using scratch-backed uv cache/Python directories.
- Frozen sync completed successfully with `uv sync --frozen --all-extras`.
- Resolved version: `jsonschema==4.26.0`.
- No package was installed into the home user site.

### CI QA gates

Preserved all existing PostgreSQL, migration, package import, Ruff, formatting, mypy, and pytest steps.

Added required repository-root CI steps:

```text
backend/.venv/bin/python scripts/validate-evals.py
  --tool-mismatch-report ${{ runner.temp }}/tool-contract-mismatches.ci.json

backend/.venv/bin/python scripts/report-eval-coverage.py
  --profile chennai-pilot
```

The mismatch report is written to runner temporary storage, not the checkout. The All-India profile remains non-blocking and is not a required CI gate.

### Root ignore policy

Expanded root `.gitignore` to recursively exclude:

- `.env` secrets while explicitly preserving `.env.example`.
- Python environments/caches/coverage/build metadata.
- JavaScript dependencies and build output.
- Local databases, logs, generated audio, test output, voice samples, and eval results.
- Private keys and SSH material.
- Editor/OS files.

Temporary Git metadata verification confirmed:

```text
.env:                              excluded
backend/.env:                      excluded
.env.example:                      included
backend/.env.example:              included
backend/uv.lock:                   included
.github/workflows/backend-ci.yml:  included
Migrations/eval schemas/docs:      included
```

The actual project was not initialized as a Git repository.

### Pre-push audit

Created executable `scripts/pre-push-audit.sh`.

Behavior:

- Uses `git ls-files --cached --others --exclude-standard` after Git initialization.
- Before Git exists, uses temporary Git metadata with the real project as worktree, preserving `.gitignore` semantics without initializing the project.
- Rejects ignored-secret/artifact paths, generated audio, local DBs, logs, keys/SSH material, files over 10 MiB, and common credential patterns.
- Scans UTF-8 text only and skips binary content.
- Reports path and pattern category only, never matched secret values.
- Does not delete, modify, or contact external services.

Final audit:

```text
Audited candidate files: 129
Result:                  PASS
Findings:                0
```

Generated prototype audio was correctly excluded from the candidate set. Known localhost-only test credentials and `.env.example` placeholders are narrowly recognized without weakening real credential detection.

### Exact backend results

```text
uv sync --frozen --all-extras:  PASS
jsonschema version:             4.26.0
Package import:                 PASS
ruff check .:                   PASS
ruff format --check .:          PASS — 57 files
mypy src:                       PASS — 27 source files
pytest -m "not postgres" -q:    280 passed, 23 deselected
pytest -m postgres --collect:   23/303 collected
pytest -q:                      280 passed, 23 skipped
Alembic head:                   0003
```

PostgreSQL contracts remain collected but skipped because no safe local PostgreSQL instance was available. PostgreSQL behavior was not executed or claimed.

### Exact QA and repository results

```text
validate-evals.py:              PASS — 211 cases / 377 turns
Tool contract mismatches:       0
Chennai-pilot coverage:         PASS
Blocking coverage failures:     none
Future All-India locale gaps:   hi-IN, te-IN, kn-IN, ml-IN (non-blocking)
pre-push-audit.sh:              PASS
check-migrations.sh:            PASS — 0 errors / 0 warnings
Bash syntax checks:             PASS
CI workflow YAML parse:         PASS
```

QA review status remains honest:

- 211 language-synthetic cases.
- 211 domain-unreviewed cases.
- 211 pilot-untested cases.
- Native-speaker, product-domain, clinical, and pilot review remain pending.

### Files changed/created by Dev1

Changed:

- `backend/pyproject.toml`
- `backend/uv.lock`
- `.github/workflows/backend-ci.yml`
- `.gitignore`
- `docs/daily/2026-08-02/STATUS.md`

Created:

- `scripts/pre-push-audit.sh`
- `backend/tests/test_pre_ci_configuration.py`

No backend domain behavior, migrations, eval contracts/cases, voice prototype, providers, or `.env` files were modified.

### Remaining blockers and handoff

- GitHub Actions has not run yet.
- PostgreSQL integration tests have not executed.
- The project is not initialized as a Git repository by Dev1.
- No commit or push was performed by Dev1.

Handoff to the AI cofounder:

1. Run a final independent secret/candidate-file review.
2. Initialize the repository only after approval.
3. Inspect the complete staged file list.
4. Commit and push to the approved private repository.
5. Observe and repair the first GitHub Actions/PostgreSQL run.
6. Do not authorize Phase C until CI and all PostgreSQL contracts are green.
