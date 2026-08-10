# Migration 0016 — Exotel Durable Intake, Correlation, and Call Identity v2

Status: DESIGN ONLY — not approved for implementation.
Parent: integrated 0015 (Dev3).
Owner: Dev1. Requires CEO authorization.

---

## 1. Correlation Lifecycle

### States

```
registering → active → closed_grace → expired
                ↓                        ↓
              failed                  (same terminal)
```

| State | Meaning | Callback matching | Pending reconciliation |
|-------|---------|-------------------|----------------------|
| `registering` | Stream handler authenticated start, runtime not yet started | NO — too early | NO |
| `active` | Runtime startup succeeded | YES — MATCHED | YES — reconcile pending→received |
| `closed_grace` | Normal stream close, grace period for delayed callbacks | YES — MATCHED | YES |
| `failed` | Runtime startup failed or immediate abort | NO — stale | NO — pending stays pending |
| `expired` | Grace TTL elapsed after close | NO — stale | NO |

### Transitions

- `registering → active`: runtime factory returns successfully (not exception)
- `registering → failed`: runtime factory raises or stream aborts before first frame
- `active → closed_grace`: normal provider stop/disconnect, runtime completes
- `active → failed`: runtime error during active call
- `closed_grace → expired`: grace TTL elapses (configurable, default 5 minutes)

### Implementation on `call_correlation_bindings`

```sql
correlation_status VARCHAR(20) NOT NULL DEFAULT 'registering'
    CHECK (correlation_status IN (
        'registering', 'active', 'closed_grace', 'failed', 'expired'
    )),
activated_at    TIMESTAMPTZ,
closed_at       TIMESTAMPTZ,
grace_expires_at TIMESTAMPTZ,
```

Stream handler:
1. INSERT with `correlation_status = 'registering'`
2. After `runtime_factory(transport, session)` returns normally:
   `UPDATE SET correlation_status = 'active', activated_at = NOW()`
3. On runtime exception:
   `UPDATE SET correlation_status = 'failed', closed_at = NOW()`
4. On normal close:
   `UPDATE SET correlation_status = 'closed_grace', closed_at = NOW(), grace_expires_at = NOW() + interval`

Grace expiry sweep:
```sql
UPDATE call_correlation_bindings
SET correlation_status = 'expired'
WHERE correlation_status = 'closed_grace'
  AND grace_expires_at < NOW()
```

### Callback matching per state

```sql
SELECT ... FROM call_correlation_bindings
WHERE provider = :p AND provider_call_id = :cid
  AND correlation_status IN ('active', 'closed_grace')
  AND (grace_expires_at IS NULL OR grace_expires_at > NOW())
```

- Match → MATCHED
- No row or only `registering`/`failed`/`expired` → PENDING (quarantine)
- Match with wrong business/number/direction → CONFLICT

### No stale MATCHED after startup failure

`failed` is never in the matching set. A callback arriving after startup failure quarantines normally.

### Delayed terminal enrichment preserved

`closed_grace` stays matchable — a late Duration update arriving within the grace period correlates correctly.

---

## 2. E.164 Normalization

### Policy

All phone numbers in mapping configuration and provider callbacks are required to be canonical E.164 format: `+<country><number>` (e.g. `+919876543210`).

Non-E.164 values (local format `08012345678`, missing `+`, etc.) are rejected at boundaries:

| Boundary | Rejection |
|----------|-----------|
| `EXOTEL_NUMBER_MAPPINGS` startup | Keys must be E.164; non-E.164 key → `InvalidNumberMappingError`, route not mounted |
| Callback `From`/`To` | Non-E.164 → parse error 400 |
| Stream start `from`/`to` | Non-E.164 → `ExotelStartValidationError` |

### Validation

```python
_E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")

def validate_e164(number: str, field: str) -> str:
    number = number.strip()
    if not _E164_RE.match(number):
        raise ExotelCallbackParseError(f"{field} must be E.164: {number!r}")
    return number
```

### Comparison

Mapping lookup and correlation comparison use the validated canonical form.
No country-code inference or local-number conversion — values must arrive
in E.164 from the provider or configuration.

### Sandbox dependency

If Exotel sandbox delivers local-format numbers (OQ-1), the E.164 requirement
must be relaxed or a trusted country context added. This is deferred to
post-sandbox verification. The design enforces E.164 as the default; the
adapter's normalize function is the single point of change.

---

## 3. Event Identity, Enrichment, and Conflict Storage

### Event dedup key

```
(business_id, provider, provider_call_id, event_type)
```

### Enrichment UPSERT

```sql
INSERT INTO inbound_call_events (...)
VALUES (...)
ON CONFLICT (business_id, provider, provider_call_id, event_type)
DO UPDATE SET
    -- Enrichable facts: accept NULL→value or monotonic increase
    duration = CASE
        WHEN EXCLUDED.duration IS NOT NULL
         AND (inbound_call_events.duration IS NULL
              OR EXCLUDED.duration >= inbound_call_events.duration)
        THEN EXCLUDED.duration
        ELSE inbound_call_events.duration
    END,
    conversation_duration = CASE
        WHEN EXCLUDED.conversation_duration IS NOT NULL
         AND (inbound_call_events.conversation_duration IS NULL
              OR EXCLUDED.conversation_duration >= inbound_call_events.conversation_duration)
        THEN EXCLUDED.conversation_duration
        ELSE inbound_call_events.conversation_duration
    END,
    provider_ended_at = CASE
        WHEN EXCLUDED.provider_ended_at IS NOT NULL
         AND inbound_call_events.provider_ended_at IS NULL
        THEN EXCLUDED.provider_ended_at
        ELSE inbound_call_events.provider_ended_at
    END,
    enrichment_count = inbound_call_events.enrichment_count + 1
WHERE
    -- Immutable fields must match for enrichment
    inbound_call_events.status = EXCLUDED.status
    AND inbound_call_events.caller_phone = EXCLUDED.caller_phone
    AND inbound_call_events.called_number = EXCLUDED.called_number
    AND inbound_call_events.direction IS NOT DISTINCT FROM EXCLUDED.direction
RETURNING id, (xmax = 0) AS inserted,
    (enrichment_count > 0 AND xmax != 0) AS enriched
```

**Key**: `payload_digest` is NOT updated on enrichment. The original digest
is preserved. `enrichment_count` tracks how many times facts were enriched.

### Result interpretation

| inserted | enriched | Meaning | Action |
|----------|----------|---------|--------|
| true | false | New event | Normal intake |
| false | true | Enrichment accepted | Return 200, update worker if needed |
| false | false | WHERE clause failed (immutable mismatch) | Conflict |
| false | false + same digest | Exact duplicate | DuplicateCallEventError → 200 |

When the WHERE clause fails (0 rows updated, not inserted), the handler
must determine if it's an exact duplicate or a conflict by comparing digests:

```sql
SELECT payload_digest FROM inbound_call_events
WHERE business_id = :bid AND provider = :p
  AND provider_call_id = :cid AND event_type = :etype
```

Same digest → DuplicateCallEventError. Different digest with immutable
field mismatch → conflict.

### Conflict storage

Conflicts are stored in a separate table — they cannot share the UNIQUE key:

```sql
CREATE TABLE inbound_call_event_conflicts (
    id              SERIAL PRIMARY KEY,
    original_event_id INTEGER NOT NULL REFERENCES inbound_call_events(id),
    business_id     INTEGER NOT NULL REFERENCES businesses(id),
    provider        VARCHAR(20) NOT NULL,
    provider_call_id VARCHAR(128) NOT NULL,
    event_type      VARCHAR(20) NOT NULL,
    conflicting_status VARCHAR(20) NOT NULL,
    conflicting_caller_phone VARCHAR(20) NOT NULL,
    conflicting_called_number VARCHAR(20) NOT NULL,
    conflicting_direction VARCHAR(20),
    conflicting_duration INTEGER,
    conflicting_payload_digest VARCHAR(64) NOT NULL,
    conflict_reason VARCHAR(100) NOT NULL,
    received_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    retained_until  TIMESTAMPTZ NOT NULL
);

CREATE INDEX ix_conflicts_retention
    ON inbound_call_event_conflicts (retained_until);
```

Retention: `retained_until = received_at + configured_retention` (default 30 days).
Conflict rows are immutable evidence. Never worker-eligible, never auto-matched.
FK to `original_event_id` links the conflict to the event it conflicted with.

---

## 4. Global Head-of-Line Claim

### Global worker claim query

The worker does NOT know the call in advance. It claims the globally oldest
eligible event, subject to head-of-line ordering per call:

```sql
SELECT e.id, e.provider, e.provider_call_id, e.business_id,
       e.event_type, e.status, e.caller_phone, e.called_number,
       e.duration, e.direction, e.claim_version
FROM inbound_call_events e
WHERE e.intake_status IN ('received', 'failed')
  AND (e.next_attempt_at <= NOW() OR e.next_attempt_at IS NULL)
  AND e.attempts < e.max_attempts
  AND NOT EXISTS (
      SELECT 1 FROM inbound_call_events older
      WHERE older.business_id = e.business_id
        AND older.provider = e.provider
        AND older.provider_call_id = e.provider_call_id
        AND older.intake_status IN ('received', 'failed', 'processing')
        AND older.received_at < e.received_at
        AND older.id != e.id
  )
ORDER BY e.received_at, e.id
LIMIT 1
FOR UPDATE OF e SKIP LOCKED
```

### Guarantees

- `NOT EXISTS` ensures no later event for the same call is claimed while
  an older one is unfinished (received/failed/processing)
- `ORDER BY received_at, id` provides deterministic total order
- `SKIP LOCKED` prevents worker contention
- `FOR UPDATE OF e` locks only the claimed row

### Two-worker proof

Worker A claims event 1 (answered, received_at=T1) for call X.
Worker B tries to claim event 2 (terminal, received_at=T2>T1) for call X.
`NOT EXISTS` finds event 1 in `processing` state → event 2 is excluded.
Worker B skips call X and claims the next eligible event for a different call.
After Worker A completes event 1, Worker B's next poll claims event 2.

### Late lower-state no-op

Worker claims a late `ringing` event after `in_progress` was processed.
`validate_transition("in_progress", "ringing")` raises `LateCallEventError`.
Worker catches it, marks intake event `completed` (no domain mutation).

### Expired processing reclaim

Events in `processing` with expired lease are reclaimable. The claim query's
`IN ('received', 'failed')` does NOT include processing — a separate sweep
handles expired leases:

```sql
UPDATE inbound_call_events
SET intake_status = CASE
        WHEN attempts >= max_attempts THEN 'dead_letter'
        ELSE 'failed'
    END,
    claim_token = NULL, claimed_at = NULL, lease_expires_at = NULL,
    dead_lettered_at = CASE WHEN attempts >= max_attempts THEN NOW() END
WHERE intake_status = 'processing'
  AND lease_expires_at < NOW()
```

---

## 5. Provider-Qualified Domain and Historical Facts

### `calls` table changes

```sql
ALTER TABLE calls ADD COLUMN provider VARCHAR(20);
ALTER TABLE calls ADD COLUMN provider_call_id VARCHAR(128);
ALTER TABLE calls ADD COLUMN call_status VARCHAR(20);
ALTER TABLE calls ADD COLUMN provider_started_at TIMESTAMPTZ;
ALTER TABLE calls ADD COLUMN provider_ended_at TIMESTAMPTZ;

CREATE UNIQUE INDEX uq_calls_provider_identity
    ON calls (business_id, provider, provider_call_id)
    WHERE provider IS NOT NULL AND provider_call_id IS NOT NULL;
```

All new columns NULLABLE — existing rows unaffected.

### Semantic distinction

| Column | Meaning | Source |
|--------|---------|--------|
| `calls.started_at` | When Fonely created the call record | Processing time (NOW()) |
| `calls.ended_at` | When Fonely processed the terminal event | Processing time (NOW()) |
| `calls.duration_sec` | Provider-reported total duration | Provider callback |
| `calls.provider_started_at` | Provider-reported call start | Provider callback (if available) |
| `calls.provider_ended_at` | Provider-reported call end | Provider callback (if available) |
| `calls.call_status` | Canonical terminal status (completed/failed/busy/no_answer) | Provider callback, mapped to canonical |

### Worker mutation

```python
if is_terminal(claimed.status):
    UPDATE calls SET
        call_status = claimed.status,  # preserves failed/busy/no_answer
        ended_at = NOW(),              # processing time
        duration_sec = claimed.duration,
        provider_ended_at = claimed.provider_ended_at
    WHERE id = :id AND business_id = :bid
```

`NOW()` is never substituted for provider timestamps. They are separate columns.

---

## 6. Downgrade, Retention, and Evidence Safety

### Retention policy

| Row type | Retention | Rationale |
|----------|-----------|-----------|
| completed | 90 days (configurable) | Provider lifecycle evidence |
| dead_letter | 90 days | Failed processing evidence |
| conflict | 30 days | Security/anomaly evidence |
| correlation (expired) | 30 days | Call lifecycle evidence |

### Downgrade procedure

```sql
-- 1. Acquire exclusive lock on both tables
LOCK TABLE inbound_call_events IN ACCESS EXCLUSIVE MODE;
LOCK TABLE call_correlation_bindings IN ACCESS EXCLUSIVE MODE;
LOCK TABLE inbound_call_event_conflicts IN ACCESS EXCLUSIVE MODE;

-- 2. Check for non-expired evidence
DO $$
DECLARE
    n_unprocessed INTEGER;
    n_active_corr INTEGER;
    n_unexpired_events INTEGER;
    n_unexpired_conflicts INTEGER;
BEGIN
    SELECT COUNT(*) INTO n_unprocessed FROM inbound_call_events
    WHERE intake_status NOT IN ('completed', 'dead_letter');

    SELECT COUNT(*) INTO n_active_corr FROM call_correlation_bindings
    WHERE correlation_status IN ('registering', 'active', 'closed_grace');

    SELECT COUNT(*) INTO n_unexpired_events FROM inbound_call_events
    WHERE received_at > NOW() - INTERVAL '90 days';

    SELECT COUNT(*) INTO n_unexpired_conflicts FROM inbound_call_event_conflicts
    WHERE retained_until > NOW();

    IF n_unprocessed > 0 THEN
        RAISE EXCEPTION 'Cannot downgrade: % unprocessed events', n_unprocessed;
    END IF;
    IF n_active_corr > 0 THEN
        RAISE EXCEPTION 'Cannot downgrade: % active correlations', n_active_corr;
    END IF;
    IF n_unexpired_events > 0 THEN
        RAISE EXCEPTION 'Cannot downgrade: % events within retention period', n_unexpired_events;
    END IF;
    IF n_unexpired_conflicts > 0 THEN
        RAISE EXCEPTION 'Cannot downgrade: % conflicts within retention', n_unexpired_conflicts;
    END IF;
END $$;

-- 3. Drop (tables locked, no concurrent insert possible)
DROP TABLE inbound_call_event_conflicts;
DROP TABLE call_correlation_bindings;
DROP INDEX IF EXISTS uq_calls_provider_identity;
ALTER TABLE calls DROP COLUMN IF EXISTS provider_ended_at;
ALTER TABLE calls DROP COLUMN IF EXISTS provider_started_at;
ALTER TABLE calls DROP COLUMN IF EXISTS call_status;
ALTER TABLE calls DROP COLUMN IF EXISTS provider_call_id;
ALTER TABLE calls DROP COLUMN IF EXISTS provider;
DROP TABLE inbound_call_events;
```

### Lock ordering

ACCESS EXCLUSIVE prevents concurrent INSERT between preflight check and DROP.
Tables locked in dependency order (events → bindings → conflicts) to prevent deadlock.

---

## 7. Durable Quarantine and Conflict Evidence

### Pending quarantine

`intake_status = 'pending_correlation'` — non-worker-eligible. Worker claim
query uses `IN ('received', 'failed')` which excludes it.

### Pending sweep

```sql
UPDATE inbound_call_events
SET intake_status = 'dead_letter', dead_lettered_at = NOW()
WHERE intake_status = 'pending_correlation'
  AND pending_expires_at < NOW()
```

Idempotent: already-dead-lettered rows are excluded by the WHERE clause.
Operator-visible: dead_lettered_at timestamp and intake_status are queryable.
Retained under the 90-day retention policy.

### Final-attempt dead-letter sweep

```sql
UPDATE inbound_call_events
SET intake_status = 'dead_letter', dead_lettered_at = NOW(),
    claim_token = NULL, claimed_at = NULL, lease_expires_at = NULL
WHERE intake_status = 'processing'
  AND lease_expires_at < NOW()
  AND attempts >= max_attempts
```

Idempotent and fenced by intake_status + lease_expires_at.

### Mandatory wiring

When routes are enabled (mounted by `_mount_exotel_routes`), correlation
and admission are wired as app.state dependencies. The handlers check:

- Callback: `_get_intake(request.app) is None → 503`
- Stream: `correlation is None or admission is None → websocket.close(1013)`

Missing wiring = unready. Zero persistence occurs without these dependencies.

---

## 8. Schema / Rollout

### Tables created

1. `inbound_call_events` (columns: id, provider, provider_call_id, business_id,
   event_type, status, caller_phone, called_number, duration, conversation_duration,
   direction, custom_field, payload_digest, enrichment_count, intake_status,
   attempts, max_attempts, claim_token, claim_version, claimed_at,
   lease_expires_at, next_attempt_at, completed_at, dead_lettered_at,
   pending_expires_at, provider_started_at, provider_ended_at, received_at)

2. `call_correlation_bindings` (columns: id, provider, provider_account_id,
   provider_call_id, provider_stream_id, business_id, called_number,
   direction, sample_rate, correlation_status, created_at, activated_at,
   closed_at, grace_expires_at, expires_at, terminal_at)

3. `inbound_call_event_conflicts` (columns: id, original_event_id,
   business_id, provider, provider_call_id, event_type, conflicting_status,
   conflicting_caller/called/direction/duration/digest, conflict_reason,
   received_at, retained_until)

### `calls` alterations

5 new nullable columns: provider, provider_call_id, call_status,
provider_started_at, provider_ended_at. Partial unique index on
(business_id, provider, provider_call_id).

### CHECK constraints

- `intake_status IN ('received', 'pending_correlation', 'processing', 'completed', 'failed', 'dead_letter')`
- `attempts >= 0 AND attempts <= max_attempts`
- `intake_status != 'processing' OR claim_token IS NOT NULL`
- `correlation_status IN ('registering', 'active', 'closed_grace', 'failed', 'expired')`

### UNIQUE constraints

- `inbound_call_events`: (business_id, provider, provider_call_id, event_type)
- `call_correlation_bindings`: (provider, provider_call_id)

### FK / ON DELETE

- `inbound_call_events.business_id → businesses(id)` — no cascade
- `call_correlation_bindings.business_id → businesses(id)` — no cascade
- `inbound_call_event_conflicts.original_event_id → inbound_call_events(id)` — no cascade
- `inbound_call_event_conflicts.business_id → businesses(id)` — no cascade
- No FK from events to calls (worker creates call row during processing)

### Indexes

- `ix_inbound_call_events_poll`: (intake_status, next_attempt_at, received_at) WHERE intake_status IN ('received', 'failed')
- `ix_inbound_call_events_lease`: (lease_expires_at) WHERE intake_status = 'processing'
- `ix_inbound_call_events_pending`: (pending_expires_at) WHERE intake_status = 'pending_correlation'
- `ix_conflicts_retention`: (retained_until)
- `uq_calls_provider_identity`: (business_id, provider, provider_call_id) WHERE provider IS NOT NULL

### ORM parity

All tables/columns have SQLAlchemy model definitions. `alembic check` shows
no drift after upgrade.

### Parent

0016, parent = integrated 0015. Created only after 0015 is on main.

### Populated calls preflight

Existing `calls` rows have NULL for all new columns. No backfill. No UPDATE.
All new columns are NULLABLE.

### Offline SQL

Pure DDL. No data migration or long-running statement.

### Concurrent writers / rollout

Existing writers (appointment service, WhatsApp) do not set new columns.
NULL is valid. No rolling-deploy conflict.

### Routes disabled until ready

Routes remain disabled until schema readiness (`_verify_schema`) and
runtime factory wiring both pass. Readiness exposes blocked configuration
truthfully.

---

## 9. Acceptance Evidence (post-migration)

### Correlation lifecycle

| Test | Start state | Event | Expected end state |
|------|-------------|-------|--------------------|
| Callback before stream start | no binding | callback arrives | PENDING quarantine |
| Callback during active call | active | callback arrives | MATCHED → intake |
| Callback after normal close within grace | closed_grace | callback arrives | MATCHED → intake |
| Callback after grace expiry | expired | callback arrives | PENDING quarantine |
| Callback after startup failure | failed | callback arrives | PENDING quarantine |

### Event identity / enrichment

| Test | First event | Second event | Expected |
|------|-------------|-------------|----------|
| Exact duplicate | completed, dur=60 | identical | DuplicateCallEventError → 200 |
| Duration enrichment NULL→value | completed, dur=NULL | dur=60 | Accept, enrichment_count=1 |
| Duration enrichment increase | completed, dur=60 | dur=90 | Accept, enrichment_count=1 |
| Duration decrease (conflict) | completed, dur=60 | dur=30 | Conflict row in conflicts table |
| Immutable status change | completed | failed same key | Conflict row, original preserved |
| Conflict digest integrity | completed | conflicting | Conflict row has conflicting_payload_digest |

### Global head-of-line ordering

| Test | Setup | Expected |
|------|-------|----------|
| Two workers, same call | Event 1 (T1), Event 2 (T2) | Worker A claims 1; Worker B skips 2 (NOT EXISTS blocks) |
| After Worker A completes | Event 1 completed | Worker B claims Event 2 |
| Different calls independent | Call X event, Call Y event | Both claimed concurrently |

### E.164

| Test | Input | Expected |
|------|-------|----------|
| Valid E.164 | +919876543210 | Accepted |
| Local format | 08012345678 | Rejected (not E.164) |
| Missing + | 919876543210 | Rejected |
| Equivalent formatted | +91 9876 543210 | Rejected (spaces) |
| Empty | "" | Rejected |

### Downgrade safety

| Test | State | Expected |
|------|-------|----------|
| Unprocessed events exist | received count > 0 | EXCEPTION, no drop |
| Active correlations exist | active count > 0 | EXCEPTION, no drop |
| Within retention period | events < 90 days | EXCEPTION, no drop |
| All expired/completed | clean | Drop succeeds |
| Concurrent late insert | locked tables | INSERT blocks until lock released |

### Other

- Schema-not-ready matrix (all table/column checks)
- Stale mark_failed → logged, not silently ignored
- Pending TTL expiry → dead_letter
- Max-attempt expired processing → dead_letter
- Mandatory correlation/admission wiring → 503 when absent
