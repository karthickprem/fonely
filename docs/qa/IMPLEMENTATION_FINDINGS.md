# Implementation Findings

Concrete defects and design risks discovered in the current codebase through
code review. Each finding includes severity, location, failure scenario,
expected behavior, suggested owner, and blocking phase.

Severity scale:
- **P0**: Will cause a runtime failure or data corruption. Must be fixed before
  the affected phase ships.
- **P1**: Correctness risk under specific conditions, or a known gap that
  must be documented/addressed before production.
- **P2**: Code quality or maintainability concern. Should be fixed but does
  not block any phase.

---

## Finding 1

**Severity:** P1

**File:** `backend/src/fonely/services/pending_actions.py` (entire file)
and `backend/src/fonely/domain/pending_actions/lifecycle.py` (entire file)

**Finding:** The `PendingActionService` does not call any function from the
`lifecycle.py` module. The lifecycle module defines pure state-transition
functions (`revise_state`, `awaiting_confirmation_state`, `begin_commit_state`,
`complete_commit_state`, `fail_commit_state`, `expire_state`) that are tested
independently but are never invoked by the service layer. The service
reimplements all transition logic inline using direct repository calls.

This means the two layers can diverge silently. For example, `fail_commit_state`
in `lifecycle.py:102` accepts a `safe_message` parameter, while the service's
`fail_commit` method (line 365) derives the safe message internally from the
`_SAFE_COMMIT_MESSAGES` dictionary lookup on `command.error_code`. If someone
modifies the lifecycle function's behavior, the change will have no effect on
production because the service does not call it.

**Failure scenario:** A developer modifies `fail_commit_state` to add a new
validation check, believing it will be enforced in production. The service
bypasses that function entirely, so the validation is never executed.

**Expected behavior:** The service should delegate to the lifecycle functions
for state transition logic, or the lifecycle module should be removed to avoid
a misleading second source of truth.

**Suggested owner:** Dev1

**Blocking phase:** None (code quality). Should be reconciled before adding
new transition logic.

---

## Finding 2

**Severity:** Product security decision (not currently classified as a code defect)

**File:** `backend/src/fonely/services/authorization.py:77`

**Finding:** Customer action ownership in `require_existing_action_permission`
uses the verified caller phone stored in `initiated_by`. This may be the intended
customer identity for a phone-first product. Shared devices/lines create ambiguity,
but adding transient `session_id` to ownership is not automatically safer: it can
prevent the same customer from managing an earlier order on a later call.

**Decision scenario:** Two people use the same verified phone number. Phone-only
identity treats them as one customer, while session-only identity treats one
customer's later call as a different customer. Either policy can cause incorrect
authorization without an explicit product identity model.

**Expected behavior:** The product/security owner must choose the assurance level
per operation. Options include verified caller number, order/booking reference,
OTP for sensitive changes, customer PIN, or owner-approved escalation. Document
the chosen recovery path and test it before enabling customer cancellation or
modification in production.

**Suggested owner:** Founder/product security with Dev1 implementation support

**Blocking phase:** Before customer self-service mutation is pilot-enabled.

---

## Finding 3

**Severity:** P1

**File:** `backend/src/fonely/models/schema.py:431-446`

**Finding:** The `Appointment` model lacks a PostgreSQL exclusion constraint
to prevent overlapping appointments for the same resource. The existing index
`ix_appointments_resource_lookup` on `(resource_id, start_at, end_at)` is
a B-tree index for query acceleration only. It does not enforce any uniqueness
or overlap constraint.

The model docstring (lines 432-439) explicitly acknowledges this:

```
ix_appointments_resource_lookup is a query acceleration index only.
It does NOT prevent time overlap. PostgreSQL exclusion constraint:
  EXCLUDE USING gist (resource_id WITH =, tstzrange(start_at, end_at) WITH &&)
  WHERE (status IN ('held', 'confirmed'))
will be added in the appointment domain phase.
```

Until this constraint is deployed, the database cannot prevent two concurrent
transactions from booking overlapping time ranges for the same resource.

**Failure scenario:** Two callers simultaneously book appointments for the
same barber at 10:00-10:30. Both transactions pass application-level checks
(each sees the slot as available before the other commits). Both insert
successfully because no database constraint prevents the overlap. Result:
double-booking.

**Expected behavior:** A PostgreSQL exclusion constraint using GiST index
should reject the second insert at the database level, guaranteeing
mutual exclusion regardless of application-level race conditions.

**Suggested owner:** Dev1

**Blocking phase:** D (appointment domain phase). Must be deployed before
appointment functionality goes live with real businesses.

---

## Finding 4

**Severity:** P2

**File:** `backend/src/fonely/core/database.py:9-13`

**Finding:** The database engine is created with no explicit connection pool
size configuration. SQLAlchemy's `create_async_engine` defaults to
`pool_size=5` and `max_overflow=10`, giving a maximum of 15 concurrent
database connections.

```python
engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
    pool_pre_ping=True,
)
```

At Tier 2+ scale (15+ simultaneous calls), each call involves multiple
database operations. If all concurrent calls execute database queries
simultaneously, the connection pool will be exhausted, causing requests
to queue or timeout.

**Failure scenario:** During peak load with 15+ concurrent calls, database
queries start timing out because all pool connections are occupied. Callers
experience long pauses or the call fails entirely.

**Expected behavior:** Pool size should be explicitly configured based on
expected concurrency, either via `pool_size`/`max_overflow` parameters or
through the `database_url` query string. A connection pool exhaustion alert
should be added to observability.

**Suggested owner:** Dev1

**Blocking phase:** Stage 3 (early production). Acceptable for pilot with
3 concurrent calls.

---

## Finding 5

**Severity:** P2

**File:** `backend/src/fonely/core/database.py:18-22`

**Finding:** The `get_db` session generator does not commit or roll back the
session. It only calls `session.close()` in the `finally` block:

```python
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session() as session:
        try:
            yield session
        finally:
            await session.close()
```

Transaction management is left entirely to the caller. If a caller forgets
to commit or if an exception occurs after a partial write, the behavior
depends on whether the session is in autocommit mode (it is not -- the
default `async_session` uses transactional mode).

**Failure scenario:** A caller performs a database write through the session,
but an exception occurs before the caller commits. The session is closed
without an explicit rollback. While SQLAlchemy will roll back the underlying
connection on close, this pattern can lead to confusion and potential issues
with connection pool recycling if the connection enters an unexpected state.

**Expected behavior:** The `get_db` function should either explicitly roll back
on exception and commit on success, or the pattern should be documented as
"callers must manage their own transactions." The current `PendingActionService`
appears to rely on the caller to manage transaction boundaries, which is a
valid architectural choice but should be made explicit.

**Suggested owner:** Dev1

**Blocking phase:** None (design choice, but should be documented).

---

## Finding 6

**Severity:** P2

**File:** `backend/src/fonely/core/config.py:27-28`

**Finding:** The database URL defaults to a local PostgreSQL connection string
with no authentication:

```python
database_url: str = "postgresql+asyncpg://localhost:5432/fonely"
```

This default contains no username or password. While `.env` files are expected
to override this in production, if the `.env` file is missing or the
`DATABASE_URL` variable is not set, the application will attempt to connect
to a local PostgreSQL instance with the current OS username and no password.
This could succeed on development machines with permissive `pg_hba.conf`
settings, masking a misconfiguration.

**Failure scenario:** Application deployed to production without `DATABASE_URL`
set. It silently connects to a local PostgreSQL (if one exists) or fails with
a confusing connection error that does not indicate the root cause (missing
environment variable).

**Expected behavior:** Either require `DATABASE_URL` to be set explicitly (no
default), or validate at startup that the configured URL is not the default
local value when running in a non-debug environment.

**Suggested owner:** Dev1

**Blocking phase:** Stage 2 (pilot-ready). Trivial to fix.

---

## Finding 7

**Severity:** P2

**File:** `backend/src/fonely/models/schema.py:478-520`

**Finding:** The `PendingAction` model stores `initiated_by` as a plain
`String(20)` column, but there is no index on `(business_id, initiated_by)`.
The `require_existing_action_permission` function in `authorization.py:77`
checks `action.initiated_by != actor.normalized_phone`, but this comparison
happens in Python after the action is already loaded -- so the missing index
does not affect correctness.

However, if a future query needs to find all pending actions initiated by a
specific phone number within a business (e.g., "show me all my orders"),
this query will require a full table scan on `pending_actions` filtered by
`business_id` and `initiated_by`.

**Failure scenario:** As the pending_actions table grows, queries like "find
all actions for phone X in business Y" become slow. No correctness issue.

**Expected behavior:** Consider adding an index on
`(business_id, initiated_by)` if this query pattern is expected. Low priority.

**Suggested owner:** Dev1

**Blocking phase:** None.

---

## Finding 8

**Severity:** P1

**File:** `backend/src/fonely/domain/pending_actions/payloads.py:61-72`

**Finding:** The `_PAYLOAD_REGISTRY` only contains entries for `ORDER` and
`OWNER_STOCK_UPDATE` action types. The `PendingActionType` enum defines five
action types: `ORDER`, `APPOINTMENT`, `OWNER_STOCK_UPDATE`, `OWNER_PRICE_UPDATE`,
and `OWNER_SCHEDULE_UPDATE`. Attempting to create a pending action for
`APPOINTMENT`, `OWNER_PRICE_UPDATE`, or `OWNER_SCHEDULE_UPDATE` will raise
`UnsupportedPayloadSchemaError` at the `validate_payload` call.

Similarly, `_COMMIT_POLICY` in `pending_actions.py:68-78` only maps `ORDER`,
`APPOINTMENT`, and `OWNER_STOCK_UPDATE`. An `APPOINTMENT` pending action can
pass the commit policy check but will fail at payload validation since no
`AppointmentEnvelope` payload model exists yet.

**Failure scenario:** If the LLM selects an appointment tool call before the
appointment payload schema is implemented, the system will raise
`UnsupportedPayloadSchemaError`. This is correct protective behavior, but the
gap should be documented so that the appointment phase knows exactly which
components need to be created.

**Expected behavior:** Documented as expected. The `APPOINTMENT`,
`OWNER_PRICE_UPDATE`, and `OWNER_SCHEDULE_UPDATE` payload envelopes must be
created in their respective implementation phases. No action needed now.

**Suggested owner:** Dev1

**Blocking phase:** D (appointment phase) and E (owner management phase).

---

## Finding 9

**Severity:** P0 for reproducible QA/CI

**File:** `backend/pyproject.toml:26-34`, `backend/uv.lock`, and `.github/workflows/backend-ci.yml:73-104`

**Resolution status:** Resolved concurrently by Dev1 after QA.2 review.

**Original finding:** `scripts/validate-evals.py` required `jsonschema>=4.26,<5`, but the dependency was not declared or locked, so a clean frozen environment could not run QA validation.

**Resolution verified:** `backend/pyproject.toml` now declares `jsonschema>=4.26,<5` in `dev`; `backend/uv.lock` includes `jsonschema==4.26.0`; the project venv imports it; and `.github/workflows/backend-ci.yml` runs both corpus validation and the Chennai coverage profile after frozen dependency installation.

**Verification commands:**

```text
backend/.venv/bin/python scripts/validate-evals.py
backend/.venv/bin/python scripts/report-eval-coverage.py --profile chennai-pilot
```

**Suggested owner:** Dev1 / dependency and CI owner

**Blocking phase:** Resolved for structural QA automation; first real GitHub Actions execution remains to be observed.

---

## Summary

| # | Severity | File | Short Description | Blocking Phase |
|---|----------|------|-------------------|----------------|
| 1 | P1 | services/pending_actions.py, domain/lifecycle.py | Lifecycle module unused by service; parallel implementations can diverge | None |
| 2 | Product decision | services/authorization.py:77 | Define customer identity assurance for later-call mutation | Before self-service pilot |
| 3 | P1 | models/schema.py:431-446 | No exclusion constraint for appointment overlap; double-booking possible | D |
| 4 | P2 | core/database.py:9-13 | No explicit connection pool size; defaults too small for production | Stage 3 |
| 5 | P2 | core/database.py:18-22 | Session generator does not manage transaction boundaries explicitly | None |
| 6 | P2 | core/config.py:27-28 | Database URL defaults to unauthenticated local connection | Stage 2 |
| 7 | P2 | models/schema.py:478-520 | No index on (business_id, initiated_by) for future query patterns | None |
| 8 | P1 | domain/payloads.py:61-72 | Payload registry missing APPOINTMENT and owner management envelopes | D, E |
| 9 | Resolved | pyproject.toml, uv.lock, backend-ci.yml | jsonschema declared/locked and QA commands added to CI | Observe first CI run |
