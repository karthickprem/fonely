# Fonely Daily Status — 2026-08-01

## Day objective

Finish the remaining Phase A production-foundation corrections, preserve new product requirements, and do not begin the pending-action implementation until Phase A is approved.

---

## Founder/product decisions captured today

### 1. Appointment correctness is a hard product guarantee

Fonely must never double-book an appointment resource.

If Patient 1 books Doctor A from 5:00 PM to 5:30 PM, Patient 2 must not be offered or allowed to confirm any overlapping interval for Doctor A:

```text
5:00–5:30  unavailable
5:15–5:45  unavailable
4:45–5:15  unavailable
5:30–6:00  available
```

Appointment intervals use half-open semantics:

```text
[start_at, end_at)
```

The logical overlap rule is:

```text
existing_start < requested_end
AND existing_end > requested_start
```

An application query alone is insufficient because two callers may concurrently observe the same slot. PostgreSQL must enforce resource overlap safety through an exclusion constraint or an equivalently strong serialized transaction mechanism. The planned PostgreSQL approach is a range exclusion constraint scoped to the same appointment resource and active held/confirmed states.

Current status: ⏳ Not implemented. The schema currently has only a resource/time lookup index, which does not prevent overlaps.

### 2. Appointment duration is configured by the business owner

During WhatsApp onboarding, Fonely must ask the owner/doctor for typical service durations rather than assuming one universal slot duration.

Example configuration:

```text
Consultation: 15 minutes
Cleaning: 30 minutes
Root canal: 60 minutes
Extraction: 30 minutes
```

Fonely must also ask:

- Which staff/resources provide each service?
- Does duration vary by doctor/staff member?
- Is preparation or cleanup buffer required?
- How much buffer follows each service?
- Are walk-ins allowed?
- What are working hours and breaks?
- How far in advance may customers book?
- Can the owner override the normal duration for one booking?
- Do urgent cases reserve additional time?

Required future data model additions:

- Service default duration.
- Optional service-specific buffer-before and buffer-after.
- Service-to-resource eligibility.
- Optional resource-specific duration override.
- Resource schedules and exceptions.
- Booking hold expiry.

Example effective reservation:

```text
Root canal treatment: 5:00–6:00
Cleanup buffer:       6:00–6:15
Next available:       6:15
```

### 3. The LLM never owns appointment availability

Expected call flow:

```text
Caller speech
  → STT
  → LLM extracts service/date/preference
  → deterministic check_availability tool
  → database returns exact valid slots
  → LLM offers only returned slots
  → caller confirms
  → deterministic confirm_appointment tool
  → PostgreSQL atomically reserves interval
  → LLM speaks committed result
```

The LLM must not invent slots, calculate conflicts from conversational memory, or say “confirmed” before the database transaction commits.

---

## AI/model architecture decision

### Provider independence

Fonely will not be permanently tied to Sarvam's LLM. Speech, language reasoning, and voice generation are separate provider boundaries:

```text
SpeechToTextProvider
├── Sarvam Saaras
└── future providers

LanguageModelProvider
├── Sarvam-105B
├── DeepSeek
├── Qwen
├── Llama/open-weight hosted models
└── future providers

TextToSpeechProvider
├── Sarvam Bulbul
├── Fish Audio experimental adapter
└── future providers
```

Fonely's deterministic domain services must not depend on any provider-specific response shape.

### DeepSeek/open-weight decision

DeepSeek or another open-weight model may replace the Sarvam LLM component, but it does not replace Sarvam STT or TTS by itself.

Possible stack:

```text
Sarvam STT
  → DeepSeek/Qwen/other LLM
  → Fonely deterministic tools
  → Sarvam Bulbul or Fish Audio TTS
```

The provider choice will be based on measured performance, not reputation alone.

### Cost conclusion

Replacing Sarvam-105B is not the main cost-saving opportunity. Current verified Sarvam pricing makes the LLM only a few paise per short conversation, while TTS, STT, and telephony dominate variable cost.

Therefore model evaluation priorities are:

1. Tool/intent accuracy.
2. P50/P95 latency.
3. Structured-output reliability.
4. Indian-language and code-switching quality.
5. Availability and rate limits.
6. Cost per turn.

### Self-hosting policy

Open weights do not mean free production inference. Self-hosting adds GPU, scaling, monitoring, redundancy, security, deployment, and idle-capacity costs.

Initial policy:

- Use hosted APIs during MVP/pilot.
- Keep provider adapters replaceable.
- Consider self-hosting only after stable traffic can keep GPUs well utilized or data-residency/fine-tuning requirements justify it.

### Live-call model routing

- Use a fast instruct/tool-calling model for ordinary live turns.
- Avoid heavy reasoning models for simple checks such as availability, price, or confirmation.
- Reserve stronger reasoning models for complex onboarding, offline analysis, quality review, and document-to-configuration tasks.

### Required model evaluation suite

Before changing providers, benchmark the same Fonely examples across models. Planned evaluation should cover at least:

- Tamil script.
- Romanized Tamil.
- Tanglish.
- Hindi/Hinglish.
- Telugu, Kannada, and Malayalam.
- Quantities and prices.
- Dates and time expressions.
- Corrections such as “not 2 kg, make it 3.”
- Appointment duration and resource selection.
- Unknown product/service.
- Ambiguous time.
- Emergency escalation.
- Unauthorized attempts to mutate owner data.

Required metrics:

- Intent accuracy.
- Correct tool selection.
- Tool argument accuracy.
- Invalid action rate.
- Structured-output compliance.
- P50/P95 latency.
- Native-language quality.
- Cost per turn.

---

## Work built before today's new requirements

The production backend currently includes:

- Python 3.12 `uv` project.
- Hatchling package configuration.
- Async SQLAlchemy/PostgreSQL foundation.
- Capability-based business schema.
- Product, service, resource, order, inventory, appointment, pending-action, call, and audit models.
- Migration revision `0001` containing 18 application tables.
- Phone, locale, timezone, aware-datetime, money, and quantity validation utilities.
- Canonical locale-to-Sarvam mapping.
- 71 passing non-PostgreSQL unit tests.
- Complete-project Ruff, formatting, and mypy gates passing at the last verified checkpoint.

---

## Known Phase A corrections still pending

Status: 🟡 Phase A not yet approved.

1. Fix ORM/migration parity for:
   - `Call.transcript` nullability.
   - `OwnerAuditLog.details` nullability.
   - `InventoryMovement.movement_type` type/length.
   - `PendingAction.status` type/length.

2. Enforce enum domains in PostgreSQL using consistent SQLAlchemy enum constraints or explicit named checks.

3. Remove premature `btree_gist` creation from migration `0001`; add it only with the future appointment overlap constraint.

4. Reject non-finite Decimal values: NaN and positive/negative infinity.

5. Enforce quantity scale compatible with database storage so a positive quantity cannot be persisted as zero.

6. Correct documentation:
   - INR supports at most two decimal places.
   - `ROUND_HALF_UP` is half-up, not banker's rounding.

7. Strengthen migration parity tests beyond source-text searches.

8. Render and inspect explicit offline downgrade SQL for `0001:base`.

9. Run migration upgrade/downgrade and `alembic check` against PostgreSQL when a test instance becomes available.

---

## Pending implementation phases

### Phase B — Pending-action state machine

Status: ⏳ Not started.

Implement deterministic create, revise, await-confirmation, begin-commit, complete/fail-commit, reject, cancel, and expire transitions with idempotency, version checks, and tests.

### Phase C — Inventory/order engine

Status: ⏳ Not started.

Implement owner stock updates, walk-in sales, pending orders, atomic multi-product reservations, cancellation, pickup completion, expiry, ledger consistency, and PostgreSQL concurrency tests.

### Phase D — Appointment engine

Status: ⏳ Not started.

In addition to previously planned work, explicitly implement today's requirements:

- Owner-defined duration per service.
- Buffer-before/buffer-after.
- Service-to-resource eligibility.
- Resource-specific duration override where needed.
- Resource calendars and breaks.
- Atomic booking holds.
- Database-enforced non-overlap.
- Concurrent confirmation test: only one booking for the same resource and interval succeeds.
- Parallel-resource test: two different doctors/staff may accept the same clock time.
- Cancellation and safe rescheduling.

### Phase E — Provider-independent AI boundary

Status: ⏳ Not started.

Implement STT, LLM, and TTS interfaces; strict structured intent schemas; tool allowlists; sanitized results; provider routing; and an evaluation harness comparing Sarvam, DeepSeek/Qwen/other hosted options, and TTS providers.

### Phase F — Voice, WhatsApp, payments, pilot

Status: ⏳ Not started.

Continue with Exotel AgentStream, WhatsApp owner onboarding, Razorpay, dedicated-number provisioning, usage/cost telemetry, and a 10-business pilot only after deterministic transaction engines are verified.

---

## External blockers

- PostgreSQL test instance unavailable locally.
- Exotel AgentStream enablement and written production pricing pending.
- WhatsApp Business API provider/onboarding pending.
- Previously exposed provider credentials require rotation.

---

## Next bounded objective

> Finish the remaining Phase A corrections and stop for review. Do not begin Phase B in the same implementation pass.

After Phase A approval, Phase B begins. Appointment duration and non-overlap requirements are now recorded as mandatory Phase D acceptance criteria, and provider independence/model evaluation is recorded as mandatory Phase E architecture.

---

## Phase B — Pending-action state machine

Status: ✅ Implemented, statically verified, and unit tested. PostgreSQL integration tests are written and collected but blocked by the missing test database.

### Implemented

- Explicit transition policy for:
  - `collecting_details`
  - `awaiting_confirmation`
  - `committing`
  - `confirmed`
  - `rejected`
  - `cancelled`
  - `expired`
- Pure immutable lifecycle mutations for revision, confirmation, commit begin/completion/failure, and expiry.
- Strict Pydantic commands for all Phase B operations and queries.
- Verified `ActorContext` boundary.
- Versioned payload registry supporting:
  - Pending order proposals.
  - Owner stock-update proposals.
- Unknown schema versions, envelope/action mismatches, unknown fields, float quantities, duplicate product lines, naive datetimes, and empty orders are rejected.
- Stable canonical JSON and SHA-256 payload digests.
- Deterministic machine-readable confirmation snapshots.
- Tenant-scoped `PendingActionRepository` with conditional expected-version/status updates.
- PostgreSQL `INSERT ... ON CONFLICT DO NOTHING RETURNING` for concurrent idempotent create.
- Active owner/manager authorization through `BusinessUser` only.
- Customers may mutate only customer actions they initiated.
- Product references in supported proposal payloads are verified by both `business_id` and product ID before persistence and on later reads.
- Retryable commit failure returns to confirmation; non-retryable failure rejects.
- Bulk expiry uses bounded, deterministic, `SKIP LOCKED` selection and conditional updates.
- Public service results are immutable Pydantic models and contain no SQLAlchemy state.
- Safe error fields reject multiline, traceback-like, and SQL-like diagnostic content.

### Migration

Revision added:

```text
0002 — pending_action_state_machine
```

Operations:

- Add `pending_actions.payload_digest VARCHAR(64) NOT NULL`.
- Add nullable `pending_actions.rejection_reason_code VARCHAR(50)`.
- Downgrade drops both columns in reverse order.
- No empty migration and no `CREATE EXTENSION`.

Offline verification:

```text
0002 upgrade ADD COLUMN operations:   2
0002 downgrade DROP COLUMN operations: 2
Alembic head:                         0002
```

Migration `0002` has not been applied to live PostgreSQL.

### Exact verification results

```text
Package import:                 PASS
ruff check .:                   PASS
ruff format --check .:          PASS — 49 files formatted
mypy src:                       PASS — 26 source files
pytest -m "not postgres" -q:    232 passed, 17 deselected
pytest -m postgres -q:          17 skipped, 232 deselected
pytest -q:                      232 passed, 17 skipped
alembic heads:                  0002 (head)
alembic history:                0001 -> 0002; base -> 0001
```

PostgreSQL tests skip only because `FONELY_TEST_DATABASE_URL` is absent.

### PostgreSQL contracts written

17 marked PostgreSQL tests cover:

- Applying migrations `0001` and `0002`.
- Tenant-scoped idempotent create.
- Same key under different businesses.
- Conflicting payload under the same key.
- Concurrent create with separate sessions.
- Concurrent begin-commit with exactly one winner.
- Stale expected versions.
- Wrong-state transitions.
- Exact expiry boundary.
- Idempotent bulk expiry.
- Cross-tenant lookup denial.
- Invalid database enum rejection.
- Transaction rollback.
- Migration downgrade ownership.
- Tenant-scoped active/inactive owner membership.

### Security and transaction invariants

- No service/repository calls `session.commit()`.
- Transaction ownership remains with the caller/unit of work.
- No global session exists.
- Every public pending-action lookup includes `business_id`.
- Every referenced product lookup includes both `business_id` and product ID.
- No Python `hash()` is used; SHA-256 is stable across processes.
- Terminal states cannot transition.
- Actions at `expires_at <= now` are expired.
- `committing` actions are not automatically expired.
- Direct cancellation while committing is forbidden.
- No order, inventory, appointment, AI, Exotel, WhatsApp, payment, or API-route execution was implemented in this phase.

### Known limitation / blocker

🚫 No PostgreSQL test instance is available. Therefore live migration execution, database constraint behavior, conditional-update concurrency, and real rollback behavior are written but not executed here.

### Exact next recommended phase

Phase C should implement only the deterministic inventory/order engine on top of this lifecycle:

1. Owner-authorized stock setup/addition.
2. Walk-in sales.
3. Pending-order revision integration.
4. Atomic multi-product inventory reservation in stable product-ID order.
5. All-or-nothing confirmation.
6. Reservation cancellation/expiry and pickup completion.
7. Ledger/balance consistency.
8. Live PostgreSQL concurrency verification before approving transaction correctness.

---

## Dev2 — PostgreSQL verification infrastructure

### Files created

| File | Purpose |
|------|---------|
| `infra/postgres/compose.yaml` | Docker Compose for local PostgreSQL 16 test database |
| `scripts/test-postgres.sh` | Safe local test script: URL validation, migration cycle, pytest |
| `scripts/check-migrations.sh` | Offline migration smoke-test (no database required) |
| `.github/workflows/backend-ci.yml` | GitHub Actions CI with PostgreSQL service container |
| `docs/testing/POSTGRESQL.md` | Testing documentation |
| `docs/testing/POSTGRES_FINDINGS.md` | Findings in existing code Dev2 cannot edit |

### What was validated

| Check | Result |
|-------|--------|
| `bash -n scripts/test-postgres.sh` | ✅ PASS |
| `bash -n scripts/check-migrations.sh` | ✅ PASS |
| `scripts/check-migrations.sh` (full run) | ✅ PASS — single head (0002), upgrade SQL 356 lines, downgrade SQL rendered for both revisions, no empty migrations, no disallowed extensions |
| YAML parse `infra/postgres/compose.yaml` | ✅ VALID |
| YAML parse `.github/workflows/backend-ci.yml` | ✅ VALID (PyYAML 1.1 `on`→`True` coercion is expected; GitHub Actions uses YAML 1.2) |
| `pytest -m postgres --collect-only` | ✅ 17 tests collected (233 deselected) |
| `pytest -m "not postgres" -q` | 🟡 227 passed, 6 failed, 17 deselected — all 6 failures are in Dev1's `tests/unit/pending_actions/test_service.py` due to in-progress command schema changes |
| `ruff check .` | 🟡 6 errors — all in Dev1's `src/fonely/services/pending_actions.py` and `migrations/versions/0002` |
| `ruff format --check .` | 🟡 2 files would be reformatted — Dev1's `src/fonely/repositories/pending_actions.py` and `tests/test_migration_parity.py` |
| `mypy src` | 🟡 30 errors — all in Dev1's `src/fonely/services/pending_actions.py` |
| Self-review scan (credentials, production hosts, deploy commands, `\|\| true`, unsafe rm, `.env` refs) | ✅ All hits are false positives |
| Ownership boundary check | ✅ No files created or modified under `backend/src/`, `backend/migrations/`, `backend/tests/`, or `backend/pyproject.toml` |

### What could not run

- `scripts/test-postgres.sh` — requires a running PostgreSQL instance (no Docker/Podman/PostgreSQL on this machine).
- PostgreSQL integration tests — `FONELY_TEST_DATABASE_URL` not set, all 17 skip.
- Live `alembic upgrade/downgrade` against real PostgreSQL.
- `alembic check` with a live database connection.

### Exact PostgreSQL test status

🚫 **Not executed.** 17 PostgreSQL tests are collected and skip because no PostgreSQL instance is available. They have not been run against a real database.

### CI workflow status

✅ `.github/workflows/backend-ci.yml` is created and YAML-valid. It has not been executed because there is no Git repository or GitHub remote. Once pushed, it will:

- Start a PostgreSQL 16 service container with test-only credentials.
- Run all static checks, migration cycle, and both unit and PostgreSQL test suites.
- Use workflow concurrency cancellation for superseded branch runs.
- Apply minimal `contents: read` permissions.

### Findings handed to Dev1

Documented in `docs/testing/POSTGRES_FINDINGS.md`:

1. **P1** — Test fixture (`conftest.py:46-74`) does not check `FONELY_ALLOW_DESTRUCTIVE_TEST_DB`.
2. **P1** — Test fixture passes `DATABASE_URL` to Alembic, not `FONELY_TEST_DATABASE_URL` — naming inconsistency risk.
3. **P2** — Test fixture database name validation uses only substring `"test"`, not the stricter blocked-name list.
4. **P2** — No `uv.lock` file for reproducible CI builds.

### Remaining infrastructure blocker

🚫 No PostgreSQL, Docker, or Podman is available on this development machine. Local PostgreSQL execution is blocked until a container runtime or PostgreSQL server becomes available. The CI workflow provides the first automated execution path.

### Local tools discovered

| Tool | Status |
|------|--------|
| Docker | ❌ Not installed |
| Podman | ❌ Not installed |
| PostgreSQL (pg_isready, psql, initdb, pg_ctl) | ❌ Not installed |
| Python 3.12 | ✅ Available via `.venv/bin/python` |
| PyYAML | ✅ Available for YAML validation |
| Alembic | ✅ Available, offline mode works |

### Resolution note — Dev1 quality gate status

The initial Dev2 status section reported Dev1 failures observed during concurrent editing:

- 6 unit test failures in `tests/unit/pending_actions/test_service.py`
- 6 ruff errors in `src/fonely/services/pending_actions.py` and `migrations/versions/0002`
- 2 ruff format issues in `src/fonely/repositories/pending_actions.py` and `tests/test_migration_parity.py`
- 30 mypy errors in `src/fonely/services/pending_actions.py`

Those were true at the moment of observation but reflected Dev1's in-progress work, not a stable state. The latest independently verified backend state is:

```text
ruff check .:                   PASS
ruff format --check .:          PASS
mypy src:                       PASS
pytest -m "not postgres" -q:    232 passed, 17 skipped
pytest -m postgres -q:          17 skipped (no database)
```

### Hardening pass applied

After review feedback, the following infrastructure issues were fixed:

1. **test-postgres.sh** — URL no longer interpolated into Python source; passed via `FONELY_HEALTHCHECK_URL` env var. Database name allowlist tightened to `^fonely_test(_[a-z0-9_]+)?$`. Remote hosts require `FONELY_ALLOW_REMOTE_TEST_DB=1`.
2. **check-migrations.sh** — Revision chain now derived from `alembic history`, not numeric filenames. Supports alphanumeric revision IDs. Cross-checks migration files against Alembic history. AST checking uses env var for file path instead of shell interpolation.
3. **backend-ci.yml** — Triggers on all pushes and pull requests (not branch-restricted). `.venv` removed from cache; only `~/.cache/uv` cached. Cache key includes `uv.lock` when present. Uses `uv sync --frozen --all-extras` when lockfile exists. Actions pinned to commit SHAs with version comments.
4. **POSTGRESQL.md** — Updated to match exact hardened script behavior (allowlist regex, remote opt-in, fixture caveat, reproducibility note).

### Final infrastructure patch applied

After second review, the following issues were fixed:

1. **test-postgres.sh** — URL parsing replaced with Python `urllib.parse` (handles IPv6, percent-encoding, query strings). Remote databases denied entirely (no opt-in override). `FONELY_ALLOW_REMOTE_TEST_DB` removed.
2. **backend-ci.yml** — `astral-sh/setup-uv` SHA corrected from `0c5e2b81...` (wrong) to `38f3f104...` (verified v4.2.0 tag). All four action SHAs verified against official GitHub tag refs. CI now requires `uv.lock` — fails immediately if missing instead of falling back to floating resolution.
3. **POSTGRESQL.md** — Updated: remote host section now says "denied, no override". URL parsing section added. CI section updated: lockfile required, action SHAs verified.

Action SHA verification results:

| Action | Tag | SHA | Verified |
|--------|-----|-----|----------|
| `actions/checkout` | v4.2.2 | `11bd71901bbe5b1630ceea73d27597364c9af683` | ✅ |
| `actions/setup-python` | v5.3.0 | `0b93645e9fea7318ecaed2b359559ac225c90a2b` | ✅ |
| `astral-sh/setup-uv` | v4.2.0 | `38f3f104447c67c051c4a08e39b64a148898af3a` | ✅ (previously wrong) |
| `actions/cache` | v4.1.2 | `6849a6489940f00c2f30c0fb92c6274307ccb58a` | ✅ |

### Completed handoff items

1. ✅ Generated `backend/uv.lock` using `uv lock`.
2. ✅ Hardened `backend/tests/integration/postgres/conftest.py`:
   - Requires `FONELY_ALLOW_DESTRUCTIVE_TEST_DB=1`.
   - Requires database name matching `^fonely_test(_[a-z0-9_]+)?$`.
   - Requires a dedicated database username containing `test`.
   - Rejects production-like hosts.
   - Never logs the URL.

---

## Phase B.1 — Hardening after independent review

Status: ✅ Implemented and locally verified. Live PostgreSQL execution remains blocked.

### Corrections implemented

1. **Migration 0002 populated-row safety**
   - Adds `payload_digest` as nullable.
   - Online migration validates and canonically hashes every existing supported payload.
   - Backfills each existing row with the same SHA-256 algorithm used at runtime.
   - Enforces `NOT NULL` only after successful backfill.
   - Invalid or unsupported legacy payloads abort migration rather than receiving a misleading digest.
   - Offline SQL emits a guard that aborts if rows exist because static SQL cannot run Python canonicalization.
   - Added a PostgreSQL contract that downgrades to `0001`, seeds a legacy pending action, upgrades to `0002`, and verifies digest, backfill, and nullability.

2. **Trusted internal commit boundary**
   - `BeginCommitCommand`, `CompleteCommitCommand`, and `FailCommitCommand` no longer accept `ActorContext`.
   - Added internal `CommitResultContext` with allowlisted engine identities.
   - Enforced mapping:
     - `order` → `order_engine` → `order`
     - `appointment` → `appointment_engine` → `appointment`
     - `owner_stock_update` → `inventory_engine` → `inventory_update`
   - Completion verifies the committed entity exists under the same `business_id`.
   - Commit completion records trusted engine identity, not caller phone.
   - Fail-commit uses allowlisted error codes and application-authored safe messages.

3. **Actor-authorized reads**
   - Public get/get-active queries now require `ActorContext`.
   - Customers may read only actions they initiated.
   - Active owners/managers require `BusinessUser` membership.
   - Added distinct internal/system read queries for workers and transaction engines.

4. **Semantic order canonicalization**
   - Validated order lines are sorted by product ID after duplicate rejection.
   - Equivalent line permutations produce identical digest and confirmation snapshot.

5. **Expiry-aware active query**
   - Collecting/awaiting actions are active only when `expires_at > now`.
   - Committing actions remain active regardless of proposal expiry.
   - Repository concurrency-sensitive reads use `populate_existing=True`.

6. **Product deactivation handling**
   - New/revised/confirmation proposals require active products owned by the business.
   - Historical reads, cancellation, expiry, and committed-action retrieval require ownership but do not require current active status.
   - Exact idempotent retries return existing actions even after product deactivation or proposal expiry.

7. **Destructive PostgreSQL test protection**
   - Requires `FONELY_ALLOW_DESTRUCTIVE_TEST_DB=1`.
   - Database must match `fonely_test` or `fonely_test_<suffix>`.
   - Database username must contain `test`.
   - Production-like hosts are rejected.
   - No fallback to production `DATABASE_URL`.

8. **Concurrency reread consistency**
   - `get_by_idempotency_key()` now uses `populate_existing=True`.
   - Concurrent integration contracts use separate `AsyncSession` instances.
   - Idempotent create uses PostgreSQL `ON CONFLICT DO NOTHING RETURNING`.

### Phase B.1 verification

```text
Package import:                 PASS
ruff check .:                   PASS
ruff format --check .:          PASS — 53 files
mypy src:                       PASS — 26 source files
pytest -m "not postgres" -q:    254 passed, 18 deselected
pytest -m postgres -q:          18 skipped, 254 deselected
pytest -q:                      254 passed, 18 skipped
Alembic head:                   0002
0002 ADD COLUMN operations:     2
0002 SET NOT NULL operations:   1
Offline populated-row guard:    1
0002 DROP COLUMN operations:    2
CREATE EXTENSION operations:    0
```

PostgreSQL contracts remain skipped because no safe disposable database URL and destructive-test opt-in are configured.

### Phase B.1 unresolved external blocker

🚫 Migration backfill, trusted-entity checks, active-query SQL, conditional transitions, and concurrency behavior are written but not executed against PostgreSQL on this machine.

### Phase B.1 scope compliance

No inventory mutation, order creation, appointment booking, provider integration, Exotel, WhatsApp, payment, route, frontend, or worker behavior was added.

### Next phase recommendation after review

Provision a disposable PostgreSQL database and execute all 18 marked integration contracts before authorizing Phase C transaction implementation.

---

## Phase B.1 final correction checkpoint

The final independent-review corrections were implemented after the earlier B.1 checkpoint:

- Boolean values are rejected by all transactional Decimal boundaries.
- Migration/runtime canonicalization parity covers orders, owner stock updates, optional values, timezone normalization, decimal normalization, reordered lines, maximum-length fields, booleans/floats, unsupported actions, and unsupported versions.
- `CommitResultContext` is internal-only; public caller context cannot begin, complete, or fail commits.
- Trusted engine/action/entity mappings and tenant-owned committed-entity existence are enforced.
- Public reads require actor authorization; trusted internal queries use separate models.
- Future external operation exposure has an explicit allowlist and permanent internal-operation denylist.
- Active queries exclude expired collecting/awaiting actions while retaining committing actions.
- Stored historical actions remain readable/cancellable after product deactivation; new/revised/confirmable proposals require active products.
- `get_by_idempotency_key()` refreshes identity-map state.
- PostgreSQL tests require explicit destructive opt-in, strict `fonely_test` naming, a dedicated test role, and localhost/loopback host only.
- Migration `0002` safely backfills populated `0001` rows online and guards populated offline upgrades.
- `backend/uv.lock` exists and passes frozen synchronization.

Final locally verified results:

```text
uv sync --frozen --all-extras:  PASS
Package import:                 PASS
ruff check .:                   PASS
ruff format --check .:          PASS — 55 files
mypy src:                       PASS — 27 source files
pytest -m "not postgres" -q:    273 passed, 18 deselected
pytest -m postgres -q:          18 skipped, 273 deselected
pytest -q:                      273 passed, 18 skipped
Alembic head:                   0002
Offline migration smoke test:   PASS — 0 errors, 0 warnings
Bash syntax checks:              PASS
CI workflow YAML parse:         PASS
Remote PostgreSQL guard:        PASS (remote URL rejected)
```

Phase C remains blocked until GitHub Actions or another safe disposable local PostgreSQL instance executes all 18 PostgreSQL contracts successfully.

---

## Final committed-entity linkage correction

The remaining Phase B.1 transaction-linkage defect was corrected without starting Phase C.

### Implemented

- Added migration `0003 — committed_entity_linkage`.
- Added `UNIQUE(orders.pending_action_id)`.
- Added `UNIQUE(appointments.pending_action_id)`.
- Inventory movements remain one-to-many, but completion requires the selected movement's `pending_action_id` to match exactly.
- Committed-entity validation now requires:
  - Matching `business_id`.
  - Matching committed entity ID.
  - Matching `pending_action_id`.
- Existing trusted engine/action/entity-type mapping remains enforced.
- Idempotent completion succeeds only for the same correctly linked entity.

### Tests added

- Correct entity/business/pending-action linkage succeeds.
- Same business but wrong pending action is rejected.
- Cross-business committed entity is rejected.
- Nonexistent committed entity is rejected.
- Two orders cannot reference the same pending action.
- Idempotent completion of the same linked entity succeeds.
- Cumulative migration/ORM structural parity includes migration `0003`.

### Verified results after linkage correction

```text
Package import:                 PASS
ruff check .:                   PASS
ruff format --check .:          PASS — 56 files
mypy src:                       PASS — 27 source files
pytest -m "not postgres" -q:    276 passed, 23 deselected
pytest -m postgres -q:          23 skipped, 276 deselected
pytest -q:                      276 passed, 23 skipped
Alembic head:                   0003
Migration smoke script:         PASS — 0 errors, 0 warnings
0003 UNIQUE constraints added:  2
0003 UNIQUE constraints dropped: 2
CREATE EXTENSION operations:    0
```

### Remaining blocker

🚫 All 23 PostgreSQL contracts are collected but unexecuted because no safe local disposable PostgreSQL instance is available. Phase C remains unauthorized until CI or a safe local database executes them successfully.
