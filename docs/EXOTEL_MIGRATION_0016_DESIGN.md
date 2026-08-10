# Migration 0016 — Exotel Durable Intake, Correlation, and Call Identity

Status: DESIGN ONLY — no migration file, no schema change.
Parent: 0015 (Dev3, pending integration).
Owner: Dev1. Requires CEO authorization before implementation.

---

## 1. Provider-Qualified Call Identity on `calls`

### New columns on `calls`

```sql
ALTER TABLE calls ADD COLUMN provider VARCHAR(20);
ALTER TABLE calls ADD COLUMN provider_call_id VARCHAR(128);
ALTER TABLE calls ADD COLUMN call_status VARCHAR(20);
```

### Unique constraint

```sql
CREATE UNIQUE INDEX uq_calls_provider_identity
    ON calls (business_id, provider, provider_call_id)
    WHERE provider IS NOT NULL AND provider_call_id IS NOT NULL;
```

This is a partial unique index — existing rows with NULL provider/provider_call_id
are unaffected. New Exotel-originated rows are guaranteed unique per
(business, provider, call_id).

### FK strategy

`inbound_call_events.business_id` references `businesses(id)` directly.
No FK from `inbound_call_events` to `calls` — the worker creates the
call record during processing, so the FK target may not exist at intake
persist time. The worker looks up by `(business_id, provider, provider_call_id)`
using the unique index.

### Why `provider` on `calls`

Without `provider`, two providers with the same call_id collide. The
advisory lock key, intake dedup key, and worker lookup must all include
`provider`. Single-provider today (Exotel only), but the column prevents
a structural defect that would surface as an inexplicable production
incident when a second provider is added.

---

## 2. Provider Occurrence Facts

### Columns on `inbound_call_events`

```sql
provider_started_at   TIMESTAMPTZ,
provider_ended_at     TIMESTAMPTZ,
```

- `provider_started_at`: derived from Exotel's callback if a start timestamp
  is present (not currently documented; OQ-dependent). NULL if absent.
- `provider_ended_at`: derived from terminal callback timestamp if present.
- `received_at`: when Fonely received the callback (existing). This is
  processing time, NOT occurrence time.

### Duration semantics

- `duration`: total call time in seconds (Exotel's `Duration`). For terminal
  callbacks, this may arrive stale (~2 min async update per Exotel docs).
- `conversation_duration`: connected/conversation time (Exotel's
  `ConversationDuration`). Present only in terminal callbacks.

Both are stored as received — no substitution of `NOW()` for delayed values.

### On `calls` table

```sql
ALTER TABLE calls ADD COLUMN provider_started_at TIMESTAMPTZ;
ALTER TABLE calls ADD COLUMN provider_ended_at TIMESTAMPTZ;
```

The worker copies provider facts from the intake event to the call record
during domain mutation. `started_at`/`ended_at` on `calls` remain
Fonely-processing timestamps. Provider timestamps are separate columns.

---

## 3. Event Identity and Duration Enrichment

### Dedup key

```
(business_id, provider, provider_call_id, event_type)
```

### Same-terminal enrichment policy

Exotel may re-deliver a terminal callback with updated Duration after
~2 minutes. This produces the same dedup key with a different digest.

**Policy**: if the re-delivery has the same `(call_sid, event_type, status)`
but different Duration/ConversationDuration, it is a **monotonic fact
enrichment**, not a conflict:

| Existing | New | Action |
|----------|-----|--------|
| duration IS NULL | duration = 60 | UPDATE: accept enrichment |
| duration = 0 | duration = 60 | UPDATE: accept enrichment |
| duration = 60 | duration = 60 | Exact duplicate: DuplicateCallEventError → 200 |
| duration = 60 | duration = 90 | Accept monotonic increase |
| duration = 60 | duration = 30 | CONFLICT: reject, immutable fact would decrease |
| status = completed | status = failed | CONFLICT: immutable status changed |

**Immutable fields** (trigger conflict on change):
call_sid, event_type, status, caller_phone, called_number, direction

**Enrichable fields** (accept monotonic increase or NULL→value):
duration, conversation_duration, provider_ended_at

### Implementation

```sql
-- On duplicate key:
ON CONFLICT (business_id, provider, provider_call_id, event_type) DO UPDATE SET
    duration = CASE
        WHEN EXCLUDED.duration IS NOT NULL
         AND (inbound_call_events.duration IS NULL
              OR EXCLUDED.duration >= inbound_call_events.duration)
        THEN EXCLUDED.duration
        ELSE inbound_call_events.duration
    END,
    conversation_duration = CASE ... same pattern ...
    provider_ended_at = CASE ... same pattern ...
    payload_digest = EXCLUDED.payload_digest,
    enrichment_count = inbound_call_events.enrichment_count + 1
WHERE
    -- Immutable fields must match
    inbound_call_events.status = EXCLUDED.status
    AND inbound_call_events.caller_phone = EXCLUDED.caller_phone
    AND inbound_call_events.called_number = EXCLUDED.called_number
RETURNING id, (xmax = 0) AS inserted, ...
```

If immutable fields don't match → 0 rows updated → ConflictingCallEventError.
New column `enrichment_count INTEGER NOT NULL DEFAULT 0` tracks re-deliveries.

---

## 4. Durable PENDING Quarantine

### State model

`intake_status` adds a new value: `pending_correlation`.

```sql
CHECK (intake_status IN (
    'received', 'pending_correlation', 'processing',
    'completed', 'failed', 'dead_letter'
))
```

### Lifecycle

1. Status callback arrives BEFORE media/start for this CallSid.
2. Adapter authenticates, validates, routes to tenant.
3. No admitted correlation record exists for `(provider, call_id)`.
4. Adapter persists to `inbound_call_events` with `intake_status = 'pending_correlation'`.
5. Returns 200 to provider (persist-before-ACK).

### Non-worker-eligible

Workers query:
```sql
WHERE intake_status IN ('received', 'failed')
```
`pending_correlation` is NOT in this set — workers never claim it.

### Reconciliation

When `exotel_media_websocket` receives a trusted start event and
registers correlation via `register_admitted_call`:

```sql
UPDATE inbound_call_events
SET intake_status = 'received'
WHERE provider = :provider
  AND provider_call_id = :call_id
  AND business_id = :bid
  AND intake_status = 'pending_correlation'
```

This makes the event worker-eligible.

### Who writes

- **Adapter** (callback handler) writes `pending_correlation` rows.
- **Stream handler** reconciles them to `received` after correlation.
- **Worker** only claims `received`/`failed`.

### Lock order

Reconciliation acquires the correlation registration first, then updates
intake events. No cross-table FK, so no deadlock between correlation and
intake.

### Timeout/expiry

```sql
ALTER TABLE inbound_call_events ADD COLUMN
    pending_expires_at TIMESTAMPTZ;
```

Set to `received_at + configured_pending_ttl` (default 5 minutes).
A periodic sweep moves expired `pending_correlation` events to `dead_letter`:

```sql
UPDATE inbound_call_events
SET intake_status = 'dead_letter', dead_lettered_at = NOW()
WHERE intake_status = 'pending_correlation'
  AND pending_expires_at < NOW()
```

### Restart behavior

`pending_correlation` events survive restart (durable). The reconciliation
query runs on every new stream start, catching any pending events that
arrived before the restart.

### Never-admitted CallSid

If no media/start ever arrives, the pending TTL expires → dead_letter.
Manual review can inspect dead-lettered events. Manual review cannot
mutate domain state except through an audited operator command.

---

### Expired processing at max_attempts

If a worker crashes on the final attempt (attempts == max_attempts) and
the lease expires, the event is stuck in `processing` with no live claimant.
The claim query excludes `processing` events only when `lease_expires_at >= NOW()`.
An expired `processing` at max_attempts must be deterministically dead-lettered:

```sql
-- Periodic sweep (same as pending expiry):
UPDATE inbound_call_events
SET intake_status = 'dead_letter', dead_lettered_at = NOW()
WHERE intake_status = 'processing'
  AND lease_expires_at < NOW()
  AND attempts >= max_attempts
```

Events with `attempts < max_attempts` and expired lease are reclaimable
by the normal claim query (already handled).

---

## 5. Durable CONFLICT Evidence

### Storage

Conflicting events are persisted with `intake_status = 'conflict'`:

```sql
CHECK (intake_status IN (
    'received', 'pending_correlation', 'processing',
    'completed', 'failed', 'dead_letter', 'conflict'
))
```

### Lifecycle

1. Callback arrives with same dedup key but immutable field mismatch.
2. Adapter persists a NEW row with `intake_status = 'conflict'` (not updating
   the existing row). Original event is preserved.
3. Returns 200 to provider.
4. Security alert emitted.

### Retention

Conflict rows are retained for the configured retention period (default 30 days).
They are never worker-eligible, never auto-matched, never reconciled.

### Why 200 not 409

The provider should stop retrying. 409 may cause infinite retries depending
on provider behavior (OQ-3). Conflicts are logged and alerted, not exposed
as HTTP errors to the provider.

---

## 6. Head-of-Line Claiming and Event Ordering

### Per-call claim query

```sql
SELECT id, provider, provider_call_id, ...
FROM inbound_call_events
WHERE business_id = :bid
  AND provider = :provider
  AND provider_call_id = :call_id
  AND intake_status IN ('received', 'failed')
  AND (next_attempt_at <= NOW() OR next_attempt_at IS NULL)
  AND attempts < max_attempts
ORDER BY received_at
LIMIT 1 FOR UPDATE SKIP LOCKED
```

This is per-call head-of-line: only the oldest unprocessed event for a
given call is claimed. Later events for the same call wait until the
head event is completed.

### Advisory lock key

```python
blake2b(f"{business_id}:{provider}:{provider_call_id}", digest_size=8)
```

Includes `provider` — two providers with the same call_id get different locks.

### Lease/fencing

- Claim: sets `claim_token`, `claim_version`, `lease_expires_at`
- mark_completed: requires `claim_token AND claim_version AND business_id AND
  intake_status = 'processing' AND lease_expires_at >= NOW()`
- mark_failed: same fencing as mark_completed (including lease check)

### Stale mark_failed outcome

If `mark_failed` returns False (0 rows updated), the worker logs
`stale_claim_failure` with event_id and claim_token. This indicates the
lease expired and another worker reclaimed the event. The worker does NOT
retry — it returns False and the event will be re-processed by the new claimant.

### Late nonterminal no-op

A late lower-state event (e.g. `ringing` after `in_progress`) is persisted
durably (different event_type key) and processed by the worker. The worker
calls `validate_transition` which raises `LateCallEventError` — the worker
catches it, marks the intake event `completed` (no domain mutation), and
logs `worker_late_noop`.

### Validation at parse

- `answered` EventType requires status `in_progress` only
- `terminal` EventType requires terminal status only
- `queued`/`ringing` EventType requires matching status only
- Contradictions rejected at parse (400)
- Direction validated against supported set
- Phone numbers validated for length (max 20 chars per schema)

---

## 7. Durable Admitted Correlation Binding

### Separate table: `call_correlation_bindings`

```sql
CREATE TABLE call_correlation_bindings (
    id              SERIAL PRIMARY KEY,
    provider        VARCHAR(20) NOT NULL,
    provider_account_id VARCHAR(64) NOT NULL,
    provider_call_id VARCHAR(128) NOT NULL,
    provider_stream_id VARCHAR(128),
    business_id     INTEGER NOT NULL REFERENCES businesses(id),
    called_number   VARCHAR(20) NOT NULL,
    direction       VARCHAR(20),
    sample_rate     INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ,
    terminal_at     TIMESTAMPTZ,

    UNIQUE (provider, provider_call_id)
);
```

### Immutable binding

Once created by the stream handler after gateway authentication, the
binding is immutable. Fields cannot be overwritten.

### Equivalent replay

If `register_admitted_call` is called with the same `(provider, call_id)`
and identical fields → idempotent no-op (INSERT ON CONFLICT DO NOTHING
after field comparison).

### Conflicting overwrite

If `register_admitted_call` is called with the same `(provider, call_id)`
but different `business_id`, `called_number`, `direction`, or
`provider_account_id` → ConflictingCorrelationError. The stream handler
rejects the connection. The original binding is preserved.

### Activation after runtime success

The correlation binding is created BEFORE the runtime factory is invoked.
If runtime startup fails or the stream is immediately aborted:

```sql
UPDATE call_correlation_bindings
SET terminal_at = NOW()
WHERE provider = :provider AND provider_call_id = :call_id
```

This prevents a stale MATCHED binding from correlating callbacks to a
runtime that never ran. The stream handler's `finally` block sets
`terminal_at` on both normal completion and failure.

### E.164 normalization

Before mapping lookup and correlation comparison, normalize phone numbers:
- Strip leading/trailing whitespace
- If number starts with `0` (Indian local format), do NOT add country code
  (this is configuration-dependent and sandbox-verifiable)
- Mapping keys and callback values compared after normalization
- Malformed numbers (empty, >20 chars) fail closed

Full E.164 normalization (country code insertion) is deferred to post-sandbox
(OQ-1 will reveal actual format).

### Callback correlation

When a status callback arrives, the handler queries:

```sql
SELECT business_id, direction, called_number
FROM call_correlation_bindings
WHERE provider = :provider AND provider_call_id = :call_id
  AND (expires_at IS NULL OR expires_at > NOW())
  AND terminal_at IS NULL
```

- Match → MATCHED: eligible for intake
- No match → PENDING: quarantine (§4)
- Match with different business/number/direction → CONFLICT: dead-letter (§5)

---

## 8. Migration Guarantees

### Parent

Revision 0016, parent = 0015 (Dev3). Created only after 0015 is integrated
into main.

### ORM parity

All new columns/tables have corresponding SQLAlchemy model definitions.
`alembic check` shows no drift after upgrade.

### Populated preflight

Main has existing `calls` rows. New columns `provider`, `provider_call_id`,
`call_status`, `provider_started_at`, `provider_ended_at` are all NULLABLE —
no backfill required. Existing rows have NULL for all new columns.

### Offline SQL

The migration is pure DDL (CREATE TABLE, ALTER TABLE ADD COLUMN, CREATE INDEX).
No data migration. No backfill. No long-running UPDATE.

### Concurrent writers

New columns are nullable, so existing writers (appointment service, WhatsApp
worker) are unaffected — they don't set the new columns and NULL is valid.

### Rollout

No rolling-deploy issue — old code ignores new columns, new code populates them.

### Downgrade

```sql
DO $$
DECLARE n INTEGER;
BEGIN
    SELECT COUNT(*) INTO n FROM inbound_call_events
    WHERE intake_status NOT IN ('completed', 'dead_letter', 'conflict');
    IF n > 0 THEN
        RAISE EXCEPTION 'Cannot downgrade: % unprocessed events', n;
    END IF;
    SELECT COUNT(*) INTO n FROM call_correlation_bindings
    WHERE terminal_at IS NULL AND (expires_at IS NULL OR expires_at > NOW());
    IF n > 0 THEN
        RAISE EXCEPTION 'Cannot downgrade: % active correlations', n;
    END IF;
END $$;

DROP TABLE IF EXISTS call_correlation_bindings;
DROP INDEX IF EXISTS uq_calls_provider_identity;
ALTER TABLE calls DROP COLUMN IF EXISTS provider_ended_at;
ALTER TABLE calls DROP COLUMN IF EXISTS provider_started_at;
ALTER TABLE calls DROP COLUMN IF EXISTS call_status;
ALTER TABLE calls DROP COLUMN IF EXISTS provider_call_id;
ALTER TABLE calls DROP COLUMN IF EXISTS provider;
DROP TABLE IF EXISTS inbound_call_events;
```

Downgrade refuses if unprocessed events or active correlations exist.

### Upgrade/downgrade/re-upgrade

Re-upgrade recreates tables from scratch. No data recovery needed —
downgrade already verified everything was completed/dead-lettered.

---

## 9. Production Composition Sequencing

### Route mounting gate (in `_mount_exotel_routes`)

Routes are mounted when ALL of:
1. `EXOTEL_WEBHOOK_SECRET` is strong (≥32 ASCII chars)
2. `EXOTEL_NUMBER_MAPPINGS` is valid non-empty JSON
3. `EXOTEL_SID` (account) is set

Routes are NOT mounted until schema readiness is verified at first request.
The callback handler returns 503 when intake service is not wired.
The media handler returns 1013 when runtime factory is not wired.

### Callback route mandatory dependencies

When the callback route is enabled (mounted), correlation and admission
are MANDATORY — not optional. Missing wiring produces 503, never
fail-open processing. The handler checks `_get_correlation(request.app)`
and `_get_intake(request.app)` — both None → 503.

### Schema readiness gate (in worker)

Worker's `_verify_schema` checks `calls.provider_call_id` column exists
via `information_schema` with `current_schema()` and `COUNT(*)`. Fails
with `SchemaNotReadyError` before any processing.

### Readiness truth

- Routes mounted + schema absent = callbacks return 503, media returns 1013
- Routes mounted + schema present + no runtime = callbacks persist, media 1013
- Routes mounted + schema present + runtime wired = fully operational

Readiness endpoint does NOT claim Exotel readiness — it checks only DB
connectivity. Exotel readiness is a configuration/deployment concern.

---

## 10. Acceptance Evidence (post-migration)

### Tests to add after 0016 is applied

| Test | Table | Invariant |
|------|-------|-----------|
| Production `create_app` callback happy path | inbound_call_events | Event persisted via real app |
| Production `create_app` media happy path | call_correlation_bindings | Correlation registered |
| Pending → received reconciliation | inbound_call_events | Quarantine lifted on stream start |
| Pending → dead_letter expiry | inbound_call_events | TTL enforced |
| Conflict durable evidence | inbound_call_events | Conflict row survives 200 |
| Duration enrichment (NULL→value) | inbound_call_events | Monotonic accept |
| Duration enrichment (value→higher) | inbound_call_events | Monotonic accept |
| Duration enrichment (value→lower) | inbound_call_events | Conflict reject |
| Immutable status change → conflict | inbound_call_events | Status cannot change |
| Provider timestamps preserved | inbound_call_events | provider_started/ended_at |
| Two-worker head-of-line | inbound_call_events | Only oldest event claimed |
| Worker restart re-claims | inbound_call_events | Lease-expired events reclaimable |
| Tenant collision across providers | calls | Different lock keys |
| Provider collision same call_id | calls | provider column distinguishes |
| Schema-not-ready matrix | — | SchemaNotReadyError before processing |
| Equivalent correlation replay | call_correlation_bindings | Idempotent |
| Conflicting correlation overwrite | call_correlation_bindings | Rejected |
| Stale mark_failed logged | inbound_call_events | 0-row result → log |
| Late nonterminal no-op | inbound_call_events | LateCallEventError → completed |
| E.164 normalization before mapping | — | Equivalent representations match |
