# Migration 0015: Notification Manifest — Revised Design

## Problem

The notification outbox is both delivery queue and evidence. Retention
cleanup deletes delivered rows, destroying the only record of what
notifications were created. Replay cannot verify after cleanup.

Current schema (0014) lacks:
- Retention-independent immutable recipient/channel/payload/digest record
- Operation-instance identity binding (especially for multiple reschedules)
- Complete recipient set verification
- Actor identity binding

## Table: `notification_manifests`

### Columns

| Column | Type | Nullable | Default | Constraints |
|--------|------|----------|---------|-------------|
| `id` | SERIAL | NO | auto | PK |
| `business_id` | INTEGER | NO | | FK → businesses(id) ON DELETE RESTRICT |
| `entity_type` | VARCHAR(50) | NO | | Always "appointment" |
| `entity_id` | INTEGER | NO | | Appointment ID |
| `operation` | VARCHAR(20) | NO | | CHECK IN ('create','cancel','reschedule') |
| `pending_action_id` | INTEGER | NO | | Every operation has a PA |
| `appointment_commit_id` | INTEGER | YES | | NULL for create (no AppointmentCommit); NOT NULL for cancel/reschedule |
| `initiated_by_phone` | VARCHAR(20) | NO | | Trusted actor phone from ActorContext |
| `initiated_by_role` | VARCHAR(20) | NO | | Trusted actor role from ActorContext |
| `initiated_by_bu_id` | INTEGER | YES | | BusinessUser.id if actor is owner; NULL if customer/system |
| `recipient_count` | INTEGER | NO | | CHECK > 0. Expected event count |
| `recipient_manifest` | JSONB | NO | | CHECK jsonb_typeof = 'array' AND jsonb_array_length > 0 |
| `channel` | VARCHAR(20) | NO | | "whatsapp" |
| `phone_number_id` | VARCHAR(100) | NO | | WhatsApp sender identity at creation |
| `equivalence_digest` | VARCHAR(64) | NO | | SHA-256 of canonical manifest |
| `schema_version` | INTEGER | NO | 1 | CHECK > 0 |
| `outbox_event_ids` | INTEGER[] | NO | | Historical references; no FK |
| `created_at` | TIMESTAMPTZ | NO | now() | |

### Operation Identity

Every committed appointment mutation has a PendingAction:
- **Create**: PA with `committed_entity_type='appointment'`, `committed_entity_id=appointment.id`
- **Cancel**: PA with `committed_entity_type='appointment_commit'`, `committed_entity_id=commit.id`
- **Reschedule**: PA with `committed_entity_type='appointment_commit'`, `committed_entity_id=commit.id`

`pending_action_id` is NOT NULL for all operations. This is the canonical
operation-instance identity. Two reschedules of the same appointment have
different PendingActions → different manifests.

### Foreign Keys

```sql
-- Tenant-scoped business
ALTER TABLE notification_manifests
  ADD CONSTRAINT fk_manifest_business
  FOREIGN KEY (business_id) REFERENCES businesses(id) ON DELETE RESTRICT;

-- Tenant-scoped pending action (composite)
ALTER TABLE notification_manifests
  ADD CONSTRAINT fk_manifest_pending_action
  FOREIGN KEY (business_id, pending_action_id)
  REFERENCES pending_actions(business_id, id) ON DELETE RESTRICT;

-- Appointment commit (simple, nullable)
-- Only for cancel/reschedule. create has NULL.
ALTER TABLE notification_manifests
  ADD CONSTRAINT fk_manifest_appointment_commit
  FOREIGN KEY (appointment_commit_id)
  REFERENCES appointment_commits(id) ON DELETE RESTRICT;

-- CHECK: cancel/reschedule require appointment_commit_id
ALTER TABLE notification_manifests
  ADD CONSTRAINT ck_manifest_commit_consistency
  CHECK (
    (operation = 'create' AND appointment_commit_id IS NULL)
    OR (operation IN ('cancel','reschedule') AND appointment_commit_id IS NOT NULL)
  );
```

All FKs use `ON DELETE RESTRICT`. PendingActions, appointments, and
AppointmentCommits cannot be deleted while a manifest references them.
This is intentional: retention ordering must delete manifests before
their referents.

### `outbox_event_ids` Column

`INTEGER[]` — historical references to `notification_outbox.id` values
that existed at creation time. **No FK constraint.** After retention
cleanup, outbox rows are gone but the manifest preserves the evidence.
Named `outbox_event_ids` (not `notification_event_ids`) to signal they
are archival references, not live joins.

### Uniqueness

```sql
-- One manifest per operation-instance (every operation has a PA)
CREATE UNIQUE INDEX uq_manifest_operation_instance
ON notification_manifests (business_id, pending_action_id);
```

Single index. No partial indexes needed since `pending_action_id` is NOT NULL
for all operations. PA uniqueness guarantees one manifest per operation-instance.

### Indexes

```sql
CREATE INDEX ix_manifest_entity
ON notification_manifests (business_id, entity_type, entity_id);
```

### `recipient_manifest` JSONB Structure

Deterministic ordered array. Canonical order: patient first, then owners
by `BusinessUser.id` ascending.

```json
[
  {
    "recipient_type": "patient",
    "phone_e164": "+919123456789",
    "name": "Karthick",
    "bu_id": null,
    "idempotency_key": "notif-create-patient-42-pa100",
    "outbox_event_id": 501,
    "snapshot": {
      "schema_version": 1,
      "operation": "create",
      "business_id": 1,
      "appointment_id": 42,
      "pending_action_id": 100,
      "recipient_type": "patient",
      "recipient_phone": "+919123456789",
      "recipient_bu_id": null,
      "clinic_name": "Smile Dental",
      "patient_phone": "+919123456789",
      "patient_name": "Karthick",
      "service_name": "Consultation",
      "resource_name": "Dr. Priya",
      "business_timezone": "Asia/Kolkata",
      "start_at": "2026-08-15T04:30:00+00:00",
      "price": "500",
      "phone_number_id": "phone-1"
    },
    "digest": "sha256hex..."
  },
  {
    "recipient_type": "owner",
    "phone_e164": "+919000000001",
    "name": null,
    "bu_id": 1,
    "idempotency_key": "notif-create-owner-42-bu1-pa100",
    "outbox_event_id": 502,
    "snapshot": { ... },
    "digest": "sha256hex..."
  }
]
```

### Canonical Digest Construction

Root `equivalence_digest` binds:
1. `schema_version`
2. `business_id`, `entity_type`, `entity_id`, `operation`, `pending_action_id`
3. `initiated_by_phone`, `initiated_by_role`, `initiated_by_bu_id`
4. `channel`, `phone_number_id`
5. Per-recipient: `recipient_type`, `phone_e164`, `bu_id`, `idempotency_key`, per-event `digest`

Canonicalization:
```python
canonical = json.dumps(digest_input, sort_keys=True, separators=(",", ":"))
equivalence_digest = hashlib.sha256(canonical.encode()).hexdigest()
```

Size constraint: `recipient_manifest` JSONB max 100KB (CHECK `octet_length(recipient_manifest::text) <= 102400`). Practical limit: ~30 recipients.

### Actor Identity

| Actor | `initiated_by_phone` | `initiated_by_role` | `initiated_by_bu_id` |
|-------|---------------------|--------------------|--------------------|
| Customer booking | customer phone | "customer" | NULL |
| Customer cancelling | customer phone | "customer" | NULL |
| Owner cancelling via command | owner phone | "owner" | BusinessUser.id |
| System (future) | "+0" | "system" | NULL |

All from trusted `ActorContext` at command boundary. Never fabricated.

## Decision Table: Replay Evidence Classification

| Condition | Classification | Appointment Replay | Notification Evidence | API Behavior |
|-----------|---------------|-------------------|----------------------|-------------|
| Manifest exists, digest valid, outbox rows present | `manifested_complete` | Return committed result | Return manifest evidence | Success with full evidence |
| Manifest exists, digest valid, outbox rows deleted (retention) | `manifested_retained` | Return committed result | Return manifest evidence (no delivery state) | Success with evidence; delivery state unavailable |
| Manifest exists, digest INVALID | `manifest_corrupted` | FAIL CLOSED | FAIL CLOSED | Error: evidence corruption |
| No manifest, outbox rows with v1 snapshot+digest, complete set | `legacy_complete_v1` | Return committed result | Accept outbox evidence with info log | Success; legacy note |
| No manifest, outbox rows legacy format (no snapshot), all recipients present | `legacy_unmanifested` | Return committed result | Notification evidence UNKNOWN | Success with `notification_evidence: "unverifiable"` flag |
| No manifest, outbox rows legacy format, partial/missing | `legacy_partial` | Return committed result | FAIL CLOSED for notification equivalence | Success with `notification_evidence: "partial_unverifiable"` flag |
| No manifest, no outbox rows at all | `legacy_irrecoverable` | Return committed result | Notification evidence IRRECOVERABLE | Success with `notification_evidence: "irrecoverable"` flag |
| No manifest, outbox rows with v1 snapshot, but partial set | `partial_new` | FAIL CLOSED | FAIL CLOSED | Error: incomplete evidence |

Key: appointment replay (committed mutation result) is always authoritative
from the appointment/commit row. Notification evidence is a separate axis.
The API response must carry both: `appointment_result` (always authoritative)
and `notification_evidence` (classified per above). Never invent notification
success from appointment commit alone.

## FOUNDER/PRODUCT POLICY DECISION REQUIRED

**Question**: When `notification_evidence` is `legacy_unmanifested` or
`legacy_irrecoverable`, should the API:

(a) Return appointment success with an explicit evidence limitation flag
    (caller can see the appointment succeeded but notification proof is
    unavailable), or

(b) Fail the replay entirely (treat unproven notification as a failed
    operation)?

Recommendation: (a) — the appointment is committed and authoritative.
Blocking replay on notification evidence would make already-completed
operations appear to fail, which is strictly worse for the clinic.
The limitation flag enables operator alerting without false failures.

**This is a product-level decision, not an engineering one. Awaiting CEO.**

## Retention Contract

### Manifest Retention
Manifests are retained for the **same period as their referent PendingAction**.
Current PendingAction retention: indefinite (no cleanup policy exists).
When PendingAction retention is implemented, manifest deletion must
happen BEFORE PendingAction deletion (FK ordering).

### Deletion Order (when retention is added)
1. Delete delivered `notification_outbox` rows (already supported)
2. Delete `notification_manifests` whose operation is terminal AND
   beyond retention horizon
3. Delete `pending_actions` (existing policy, runs after manifest cleanup)
4. Appointments/commits: existing policy

### Privacy Basis
Manifests contain recipient phone numbers and names. Deletion is
authorized under the same data-retention policy as PendingActions
and appointments. No separate consent model.

### No Manual TRUNCATE
Immutable evidence may not be destroyed to force downgrade.
Downgrade is refused while manifests exist (see below).

## Deployment/Rollout Sequence

Single-node staging/production. No concurrent application versions.

```
1. STOP application (maintenance window — brief)
2. Run: alembic upgrade 0015
   - CREATE TABLE notification_manifests
   - CREATE INDEXES
   - Backfill: (see below — may be empty on first deploy)
3. DEPLOY new application code (manifest-writing NotificationService)
4. START application
5. VERIFY: new appointments produce manifests
```

Writer quiescence is guaranteed by step 1 (stop application).
No dual-write compatibility needed for single-node deployment.

### Backfill

On main `6a15a40`, the notification service produces outbox rows with:
- `idempotency_key`: `appt-confirm-patient-{id}`, `appt-confirm-owner-{id}`,
  `appt-cancel-patient-{id}`, `appt-cancel-owner-{id}`
- No `equivalence_snapshot` or `equivalence_digest` in payload
- Owner phone from `Business.primary_contact_phone` (not BusinessUser)
- No reschedule notifications

Since `6ec5a72` was never integrated to main, there are NO Category A
(v1 snapshot) rows in production. All existing outbox rows are Category B
(legacy format) or Category C (already deleted).

**Backfill produces zero manifests.** All existing operations are
`legacy_unmanifested` or `legacy_irrecoverable`. The backfill step
is a no-op but must be present in the migration for correctness
(future upgrades from branches that DID create v1 rows).

### Backfill SQL (for completeness)

```sql
-- No-op for current main deployment.
-- If v1 outbox rows existed, this would group them into manifests.
-- Left as documentation; actual backfill is application-level
-- because canonical digest computation requires Python.
```

## Migration Quality

### Parent
`0014` (single head, linear chain)

### ORM Parity
`NotificationManifest` model added to `schema.py` with all columns,
constraints, and indexes matching the migration SQL.

### Offline SQL
Migration is pure DDL (CREATE TABLE + indexes + constraints).
No application imports. Can be rendered and reviewed as SQL.

### Fresh Upgrade
Empty database → all 15 migrations → `notification_manifests` table
exists with correct schema. Zero rows.

### Populated Upgrade
Database with existing outbox rows → 0015 adds `notification_manifests`.
Backfill is no-op (no v1 rows on main). Existing outbox rows untouched.

### Concurrent-Write Safety (during upgrade)
Application is stopped (step 1 of rollout). No concurrent writes.

### Downgrade

```python
def downgrade():
    # Acquire ACCESS EXCLUSIVE lock to prevent concurrent inserts
    op.execute("LOCK TABLE notification_manifests IN ACCESS EXCLUSIVE MODE")

    # Preflight: refuse if manifests exist
    count = op.get_bind().execute(
        text("SELECT count(*) FROM notification_manifests")
    ).scalar()
    if count > 0:
        raise RuntimeError(
            f"Cannot downgrade: {count} notification manifest(s) would be lost. "
            "Resolve retention before downgrading."
        )

    op.drop_table("notification_manifests")
```

The `LOCK TABLE ... ACCESS EXCLUSIVE` before the count prevents
concurrent inserts between the check and the drop. The lock is
held within the same transaction as the DROP.

No manual TRUNCATE escape. If manifests exist, the downgrade fails
and the operator must resolve retention first.

### Upgrade/Downgrade/Re-upgrade

1. `alembic upgrade 0015` → table created
2. `alembic downgrade 0014` → table dropped (if empty)
3. `alembic upgrade 0015` → table recreated

No data loss because step 2 only succeeds if table is empty.

### Live Contention (downgrade)

Test: insert a manifest row, attempt downgrade → expect refusal with
count in error message. Delete the row, retry downgrade → succeeds.
Test: concurrent insert during downgrade → blocked by ACCESS EXCLUSIVE
lock, insert waits, downgrade completes (or fails), insert either
succeeds (re-upgrade path) or fails (table gone).

## Application Correction Checklist (post-schema approval)

### 1. Reschedule Operation Key
- Key: `notif-reschedule-patient-{appt_id}-pa{pending_action_id}`
- Owner: `notif-reschedule-owner-{appt_id}-bu{bu_id}-pa{pending_action_id}`
- Each PA produces unique keys → multiple reschedules don't collide

### 2. Immutable `old_start_at`
- Read from `before_snapshot` (captured by `_authoritative_snapshot` BEFORE savepoint)
- Not from `appointment.start_at` (ORM identity-map may reflect post-UPDATE value)

### 3. Complete Manifest Verification
- Replay loads manifest by `(business_id, pending_action_id)`
- Verify `equivalence_digest` matches recomputed canonical
- Verify `recipient_count` matches `jsonb_array_length(recipient_manifest)`
- Return all `outbox_event_ids` (or manifest evidence if outbox deleted)

### 4. E.164 Phone Validation
- All recipient phones validated via `core.validators.normalize_phone`
- Dedup by normalized phone (not raw)
- Invalid phone → `NotificationConfigurationError` before any mutation

### 5. Fixed-Point Price
- `format(Decimal(str(price)), 'f')` — not `Decimal.normalize()` which strips trailing zeros

### 6. WhatsApp Reschedule Renderer
- Add `appointment_rescheduled` case to `_format_message`
- Patient: "Your appointment has been rescheduled from {old_time} to {new_time}"
- Owner: "{patient_name}'s appointment rescheduled from {old_time} to {new_time}"

### 7. Mapping-Independent Retry
- Current `ConfiguredWhatsAppSenderResolver.resolve()` rejects stored
  `phone_number_id` if current mapping changed (line 48-49)
- Fix: trusted credential lookup by committed `phone_number_id` directly,
  with access_token resolution independent of business→phone mapping
- Sender identity is immutable in the manifest; routing is operational

### 8. Actor Binding
- `initiated_by_phone` and `initiated_by_role` from trusted `ActorContext`
- `initiated_by_bu_id` from BusinessUser lookup if role is owner
- Never from `Business.primary_contact_phone`

### 9. Stranded Committing PA Race
- When cancellation `begin_commit` succeeds but concurrent already-cancelled
  `INVALID_STATE` is caught as success by owner flow, PA stays `committing`
- Fix: wrap `begin_commit` + post-begin validation in one savepoint,
  OR explicitly `fail_commit` on the caught path
- Independent-session race test required

### 10. True Concurrent AppointmentService Confirmation
- Two independent sessions confirming same PA
- asyncio.Event barrier, pg_blocking_pids observation
- Exactly one appointment + one manifest + correct outbox count
- Blocked contender returns exact existing evidence

### 11. Tenant-Scoped SQL
- All manifest queries scoped by `business_id`
- Composite FK (business_id, pending_action_id)
- No cross-tenant manifest lookup

### 12. Retention → Replay Test
- Create appointment with manifest + outbox events
- Delete outbox rows (simulate retention)
- Replay: manifest provides authoritative evidence
- Zero new outbox rows or mutations

## Required Migration Evidence Plan

1. **ORM parity**: model matches migration DDL exactly
2. **Offline SQL**: `alembic upgrade --sql 0014:0015` produces reviewable DDL
3. **Fresh upgrade**: empty DB → 0015 → table exists, schema correct
4. **Populated upgrade**: DB with legacy outbox → 0015 → table exists,
   zero manifests (backfill no-op), existing outbox untouched
5. **Writer quiescence**: application stopped during migration (single-node)
6. **Main 0014 legacy inventory**: 4 idempotency key patterns, no snapshot/digest,
   single owner from `Business.primary_contact_phone`, no reschedule notifications
7. **Locked downgrade refusal**: ACCESS EXCLUSIVE lock before count check,
   refuse with count if manifests exist
8. **Concurrent late-writer proof**: test insert during downgrade blocked by lock
9. **Upgrade/downgrade/re-upgrade**: roundtrip with no evidence loss
10. **Sanitized preflight**: error messages contain counts only, no PII
