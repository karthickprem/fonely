# Migration 0015: Notification Manifest

## Problem

The notification outbox (`notification_outbox`) serves dual duty as both
delivery queue and immutable evidence. Retention cleanup deletes delivered
outbox rows, destroying the only record of what notifications were created
for a committed appointment operation. After cleanup, replay cannot verify
whether the complete recipient set was notified.

The current schema has no retention-independent immutable record of:
- which recipients were resolved for an operation
- what channel/sender identity was used
- what payload/snapshot was committed
- whether the complete set (patient + all owners) was created

## Proposed Table: `notification_manifests`

One row per committed appointment operation. Written atomically with the
appointment mutation and outbox events. Survives outbox retention cleanup.

### Columns

| Column | Type | Constraints | Purpose |
|--------|------|-------------|---------|
| `id` | SERIAL PK | | |
| `business_id` | INTEGER NOT NULL | FK → businesses.id | Tenant scope |
| `entity_type` | VARCHAR(50) NOT NULL | | Always "appointment" for now |
| `entity_id` | INTEGER NOT NULL | | Appointment ID |
| `operation` | VARCHAR(20) NOT NULL | CHECK IN ('create','cancel','reschedule') | Which mutation |
| `pending_action_id` | INTEGER | FK → pending_actions(business_id, id) composite | Links to cancel/reschedule commit; NULL for create |
| `appointment_commit_id` | INTEGER | FK → appointment_commits.id | Links to cancel/reschedule commit; NULL for create |
| `initiated_by_phone` | VARCHAR(20) | | Trusted actor phone from command boundary |
| `initiated_by_role` | VARCHAR(20) | | Trusted actor role from command boundary |
| `recipient_manifest` | JSONB NOT NULL | CHECK jsonb_typeof = 'array' | Deterministic ordered list of resolved recipients |
| `channel` | VARCHAR(20) NOT NULL | | "whatsapp" |
| `phone_number_id` | VARCHAR(100) NOT NULL | | WhatsApp sender identity at creation time |
| `equivalence_digest` | VARCHAR(64) NOT NULL | | SHA-256 of canonical manifest |
| `schema_version` | INTEGER NOT NULL DEFAULT 1 | CHECK > 0 | Forward compatibility |
| `outbox_event_ids` | INTEGER[] NOT NULL | | IDs of created notification_outbox rows |
| `created_at` | TIMESTAMPTZ NOT NULL | server_default=now() | |

### `recipient_manifest` JSONB Structure

Ordered array, deterministic by recipient resolution order (patient first,
then owners by BusinessUser.id):

```json
[
  {
    "recipient_type": "patient",
    "phone": "+919123456789",
    "name": "Karthick",
    "bu_id": null,
    "idempotency_key": "notif-create-patient-42",
    "outbox_event_id": 101,
    "snapshot": { ... NotificationSnapshot ... },
    "digest": "sha256hex..."
  },
  {
    "recipient_type": "owner",
    "phone": "+919000000001",
    "name": null,
    "bu_id": 1,
    "idempotency_key": "notif-create-owner-42-bu1",
    "outbox_event_id": 102,
    "snapshot": { ... },
    "digest": "sha256hex..."
  }
]
```

### Uniqueness

Partial unique index: one manifest per operation-instance.

```sql
CREATE UNIQUE INDEX uq_notification_manifest_operation
ON notification_manifests (business_id, entity_type, entity_id, operation)
WHERE pending_action_id IS NULL;

CREATE UNIQUE INDEX uq_notification_manifest_operation_pa
ON notification_manifests (business_id, entity_type, entity_id, operation, pending_action_id)
WHERE pending_action_id IS NOT NULL;
```

For `create`: one manifest per appointment (no pending_action_id in the
uniqueness since create has no AppointmentCommit).

For `cancel`/`reschedule`: one manifest per (appointment, pending_action_id)
since an appointment can be rescheduled multiple times, each with a different
PendingAction.

### Indexes

```sql
CREATE INDEX ix_notification_manifest_entity
ON notification_manifests (business_id, entity_type, entity_id);
```

### Foreign Keys

```sql
FK (business_id) → businesses(id)
FK (business_id, pending_action_id) → pending_actions(business_id, id)  -- composite, tenant-scoped
```

No FK to notification_outbox (outbox rows are deletable by retention).
No FK to appointment_commits (create has no commit).

## Lifecycle

### Write Path (atomic with mutation)

Inside the existing `begin_nested()` savepoint in AppointmentService:

1. Resolve recipients (BusinessUser query, patient from committed facts)
2. Validate: E.164 phones, zero-owner fail-closed
3. Build per-recipient NotificationSnapshot with immutable facts
4. Insert outbox events (ON CONFLICT DO NOTHING for idempotency)
5. Build manifest with all recipient snapshots, digests, outbox IDs
6. Insert manifest (ON CONFLICT DO NOTHING on uniqueness index)

If any step fails, the savepoint rolls back everything: appointment,
allocation, pending_action completion, outbox events, AND manifest.

### Read Path (replay)

1. Load manifest by (business_id, entity_type, entity_id, operation, pending_action_id)
2. If manifest exists:
   - Verify `equivalence_digest` matches recomputed canonical digest
   - Return existing outbox_event_ids (or the manifest's evidence if outbox rows are gone)
   - Never read mutable Business/BusinessUser/WhatsApp config
3. If no manifest and outbox events exist:
   - Legacy path: accept with info log if events match operation type
   - Fail closed if events are partial or ambiguous
4. If neither manifest nor outbox events:
   - Fail closed with NotificationEvidenceConflictError

### Retention

Manifest rows are NOT deleted by the notification retention cleanup.
Outbox rows (notification_outbox) remain deletable after delivery.
The manifest preserves the complete recipient/payload/digest evidence
independent of outbox lifecycle.

Manifest retention policy: retain for the lifetime of the appointment
(or a configurable period, e.g., 90 days after appointment date).

## Populated-Data Plan

### Current State (0014)

On upgrade to 0015, existing notification_outbox rows fall into three categories:

**Category A: Complete evidence with snapshot/digest (post-6ec5a72)**
- Outbox rows created by the new NotificationService have `equivalence_snapshot`
  and `equivalence_digest` embedded in `payload` JSONB
- Backfill: construct manifest from outbox evidence — group by
  `(business_id, entity_type, entity_id, event_type)`, extract snapshots,
  verify digests, write manifest
- Constraint: must verify ALL expected recipients are present (patient + owners)
  — partial sets are NOT backfilled

**Category B: Legacy evidence without snapshot/digest (pre-6ec5a72)**
- Outbox rows with old-style payload (no `equivalence_snapshot`)
- Idempotency keys: `appt-confirm-patient-{id}`, `appt-confirm-owner-{id}`
- Owner was `Business.primary_contact_phone`, not `BusinessUser`
- Backfill: NOT SAFE — cannot reconstruct immutable recipient identity
  or verify equivalence without the snapshot
- Policy: these operations are typed as `legacy_unmanifested` — replay
  falls through to the legacy accept-with-log path
- No manifest row created for these operations

**Category C: Already-deleted outbox rows (retention cleanup ran)**
- Operations whose outbox events were delivered and then deleted
- No evidence remains at all
- Policy: these are irrecoverable — typed as `legacy_irrecoverable`
- No manifest row created; replay returns the appointment's committed
  state (which is authoritative regardless of notification evidence)

### Migration Data Flow

```
UPGRADE 0015:
  1. CREATE TABLE notification_manifests (...)
  2. CREATE INDEXES
  3. Backfill from Category A rows (if any exist)
  4. Skip Category B and C (no manifest)

DOWNGRADE 0015:
  1. Preflight: SELECT count(*) FROM notification_manifests
  2. If count > 0: RAISE EXCEPTION 'cannot_downgrade_with_manifest_evidence'
  3. DROP TABLE notification_manifests
```

### Preflight

Upgrade: no data dependency — table is new, backfill is additive.
Downgrade: refuses if manifest rows exist (evidence would be lost).

### Locking

- `CREATE TABLE`: no lock on existing tables
- Backfill query: reads notification_outbox without FOR UPDATE
  (backfill is append-only to new table, no contention with workers)
- No `ALTER TABLE` on existing tables

### Concurrent-Write Safety

- Manifest INSERT uses ON CONFLICT DO NOTHING on the partial unique index
- Two concurrent commits for the same operation: one wins, other is no-op
- Worker delivery continues independently (no FK from outbox to manifest)

## Application Corrections After Schema Approval

The following changes to the rejected `6ec5a72` application code are required
after the manifest schema is approved and migration is written:

### 1. Reschedule Operation Identity
- Idempotency key: `notif-reschedule-patient-{appt_id}-pa{pending_action_id}`
  and `notif-reschedule-owner-{appt_id}-bu{bu_id}-pa{pending_action_id}`
- Each reschedule of the same appointment produces a distinct manifest/event set
- Current key `notif-reschedule-patient-{appt_id}` suppresses second reschedule

### 2. Old-Time Capture
- `old_start_at` must be read from `before_snapshot` (the immutable
  `appointment_authoritative_snapshot` captured BEFORE the savepoint)
- Current code reads `appointment.start_at` which may reflect the UPDATE
  within the same session (ORM identity-map mutation)

### 3. Complete Manifest Verification
- Replay must verify the COMPLETE recipient set (patient + all owners),
  not accept any single matching event
- Load manifest, verify digest, confirm outbox_event_ids count matches
  recipient_manifest length

### 4. Phone Validation
- Validate E.164 format on all recipient phones before insertion
- Use existing `normalize_phone` from `core.validators`
- Invalid phone: fail closed, do not insert partial evidence

### 5. Price Formatting
- Use fixed-point Decimal formatting: `format(Decimal(str(price)), 'f')`
- Not `Decimal.normalize()` which strips trailing zeros inconsistently

### 6. WhatsApp Sender Rendering
- Add `appointment_rescheduled` case to `WhatsAppNotificationSender._format_message`
- Include old_time → new_time in patient message
- Include patient name + old_time → new_time in owner message

### 7. Mapping-Independent Retry
- Outbox event payload contains `phone_number_id` at creation time
- Worker delivery uses the stored `phone_number_id`, not current mapping
- Already implemented in current worker via `_resolve_sender`
- Manifest also records `phone_number_id` for post-retention evidence

### 8. Actor Evidence
- Manifest records `initiated_by_phone` and `initiated_by_role` from
  the trusted `ActorContext` passed through the command boundary
- Not fabricated from Business.primary_contact_phone

### 9. AppointmentService Concurrency Tests
- Two independent sessions confirming the same pending_action
- Deterministic barrier via asyncio.Event
- Verify exactly one appointment + one manifest + correct outbox count
- Blocked contender returns exact existing evidence

### 10. Tenant-Scoped SQL
- All manifest queries scoped by `business_id`
- No cross-tenant manifest lookup
- Manifest FK uses composite (business_id, pending_action_id)

### 11. Retention-Cleanup → Replay Test
- Create appointment with manifest + outbox events
- Delete outbox rows (simulate retention cleanup)
- Replay: verify manifest provides authoritative evidence
- Verify zero new outbox rows created

## Risks

1. **Backfill ordering**: if concurrent appointments commit during
   upgrade, the backfill query may miss them. Mitigation: backfill
   runs in the same transaction as CREATE TABLE; new writes after
   upgrade use the manifest directly.

2. **Manifest size**: JSONB array with N recipients × full snapshot
   could be large for businesses with many owners. Mitigation: most
   dental clinics have 1-3 owners; monitor payload size.

3. **Downgrade data loss**: refusing downgrade when manifests exist
   is correct but may block rollback in emergencies. Mitigation:
   document manual `TRUNCATE notification_manifests` escape hatch.
