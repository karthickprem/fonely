# Migration 0015: Notification Manifest — Final Design

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
| `actor_kind` | VARCHAR(20) | NO | | CHECK IN ('customer','owner','system') |
| `actor_phone` | VARCHAR(20) | YES | | Trusted phone from ActorContext; NULL only for system |
| `actor_bu_id` | INTEGER | YES | | BusinessUser.id if owner; NULL otherwise |
| `recipient_count` | INTEGER | NO | | CHECK > 0. Expected event count |
| `recipient_manifest` | JSONB | NO | | CHECK jsonb_typeof = 'array' AND jsonb_array_length > 0 |
| `channel` | VARCHAR(20) | NO | | "whatsapp" |
| `phone_number_id` | VARCHAR(100) | NO | | WhatsApp sender identity at creation |
| `equivalence_digest` | VARCHAR(64) | NO | | SHA-256 of canonical manifest |
| `schema_version` | INTEGER | NO | 1 | CHECK > 0 |
| `outbox_event_ids` | INTEGER[] | NO | | Archival references; no FK |
| `created_at` | TIMESTAMPTZ | NO | now() | |

### Actor Identity

No placeholder phone numbers. Actor kind is modeled explicitly:

```sql
CHECK (
  (actor_kind = 'system' AND actor_phone IS NULL AND actor_bu_id IS NULL)
  OR (actor_kind = 'customer' AND actor_phone IS NOT NULL AND actor_bu_id IS NULL)
  OR (actor_kind = 'owner' AND actor_phone IS NOT NULL AND actor_bu_id IS NOT NULL)
)
```

| Actor | `actor_kind` | `actor_phone` | `actor_bu_id` |
|-------|-------------|--------------|--------------|
| Customer booking/cancelling | "customer" | customer phone | NULL |
| Owner cancelling via command | "owner" | owner phone | BusinessUser.id |
| System-initiated (future) | "system" | NULL | NULL |

All from trusted `ActorContext` at command boundary. Never fabricated.

### Operation Identity

Every committed appointment mutation has a PendingAction:
- **Create**: PA with `committed_entity_type='appointment'`
- **Cancel**: PA with `committed_entity_type='appointment_commit'`
- **Reschedule**: PA with `committed_entity_type='appointment_commit'`

`pending_action_id` is NOT NULL for all operations. Two reschedules of
the same appointment have different PendingActions → different manifests.

The `appointment_commit_id` column from v1 design is **removed**.
`appointment_commits` lacks a tenant-scoped composite unique key
`(business_id, id)`, so a tenant-scoped FK is impossible with current
schema. Cancel/reschedule commit linkage is derived through the
tenant-scoped PendingAction's `committed_entity_type='appointment_commit'`
and `committed_entity_id`.

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
```

No FK to `appointment_commits` (no tenant-scoped composite unique exists).
No FK to `notification_outbox` (outbox rows are deletable by retention).

All FKs use `ON DELETE RESTRICT`. PendingActions cannot be deleted while
a manifest references them. Retention ordering: manifests before referents.

### `outbox_event_ids` Column

`INTEGER[]` — archival references to `notification_outbox.id` values that
existed at manifest creation. **No FK constraint.** After retention cleanup,
outbox rows are gone but the manifest preserves the evidence. These are
historical identifiers, not live joins.

### Uniqueness

```sql
CREATE UNIQUE INDEX uq_manifest_operation_instance
ON notification_manifests (business_id, pending_action_id);
```

Single index. `pending_action_id` is NOT NULL for all operations.
PA uniqueness guarantees one manifest per operation-instance.

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
      "price": "500.00",
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
    "snapshot": { "..." : "..." },
    "digest": "sha256hex..."
  }
]
```

### Canonical Digest Construction

Root `equivalence_digest` binds:
1. `schema_version`
2. `business_id`, `entity_type`, `entity_id`, `operation`, `pending_action_id`
3. `actor_kind`, `actor_phone`, `actor_bu_id`
4. `channel`, `phone_number_id`
5. Per-recipient: `recipient_type`, `phone_e164`, `bu_id`, `idempotency_key`, per-event `digest`

Canonicalization:
```python
canonical = json.dumps(digest_input, sort_keys=True, separators=(",", ":"))
equivalence_digest = hashlib.sha256(canonical.encode()).hexdigest()
```

Size constraint: `CHECK (octet_length(recipient_manifest::text) <= 102400)`.
Practical limit: ~30 recipients with full snapshots.

## Decision Table: Replay Evidence Classification

**Founder policy (confirmed)**: return authoritative committed appointment
result with explicit machine-readable `notification_evidence` status.
Never claim notification success/delivery. Never make a committed
appointment appear failed because historical notification proof is
unavailable.

| Condition | Classification | Appointment Result | `notification_evidence` | HTTP | Operator Alert |
|-----------|---------------|-------------------|------------------------|------|---------------|
| Manifest exists, digest valid, outbox present | `manifested_complete` | Committed result | `"verified"` | 200 | None |
| Manifest exists, digest valid, outbox deleted | `manifested_retained` | Committed result | `"verified_delivery_unknown"` | 200 | None |
| Manifest exists, digest INVALID | `manifest_corrupted` | **FAIL CLOSED** | N/A | 409/500 | `notification_manifest_corrupted` (business_id, entity_id, operation — no PII) |
| No manifest, legacy outbox, all recipients present | `legacy_unmanifested` | Committed result | `"unverifiable"` | 200 | `legacy_notification_unverifiable` (business_id, entity_id — no PII) |
| No manifest, legacy outbox, partial recipients | `legacy_partial` | Committed result | `"partial_unverifiable"` | 200 | `legacy_notification_partial` (business_id, entity_id — no PII) |
| No manifest, no outbox rows | `legacy_irrecoverable` | Committed result | `"irrecoverable"` | 200 | `legacy_notification_irrecoverable` (business_id, entity_id — no PII) |
| No manifest, new-format outbox (v1 snapshot), COMPLETE set | N/A — should not occur after rollout | **FAIL CLOSED** | N/A | 500 | `missing_manifest_with_v1_outbox` — indicates manifest write failed |
| No manifest, new-format outbox (v1 snapshot), PARTIAL set | `partial_new` | **FAIL CLOSED** | N/A | 500 | `partial_v1_outbox_without_manifest` |
| Manifest exists, corrupted snapshot in manifest | `manifest_corrupted` | **FAIL CLOSED** | N/A | 409/500 | `notification_manifest_corrupted` |

**Key invariant**: partial new-format rows are NEVER classified as legacy.
New-format evidence without a manifest indicates a write failure and
fails closed immediately.

### Domain Result Shape

```python
@dataclass
class AppointmentReplayResult:
    appointment: AppointmentConfirmationResult  # always authoritative
    notification_evidence: str  # "verified" | "verified_delivery_unknown" |
                                # "unverifiable" | "partial_unverifiable" |
                                # "irrecoverable"
```

All `notification_evidence` values except `"verified"` and
`"verified_delivery_unknown"` emit PII-safe operator alerts via the
existing metrics/logging infrastructure (business_id + entity_id only).

## Retention Contract

### Manifest Retention
Manifests are retained for the **same period as their referent PendingAction**.
Current PendingAction retention: indefinite (no cleanup policy exists).

When PendingAction retention is implemented:
1. Delete delivered `notification_outbox` rows (existing policy)
2. Delete `notification_manifests` beyond retention horizon
3. Delete `pending_actions` (runs after manifest cleanup, FK enforces order)

After manifest deletion, replay of that operation returns
`notification_evidence: "irrecoverable"` (manifest gone, outbox gone).

### Privacy Basis
Manifests contain recipient phone numbers and names. Subject to the same
data-retention and deletion-request policy as PendingActions and
appointments. No separate consent model.

### No Destructive Bypass
Immutable evidence may not be destroyed to force downgrade or bypass
retention. No `TRUNCATE` escape hatch in normal procedure.

## Deployment/Rollout Sequence

Single-node staging/production. No concurrent application versions.

```
1. STOP application (maintenance window)
2. Run: alembic upgrade 0015
   - CREATE TABLE notification_manifests (pure DDL)
   - Zero backfill (no v1 outbox rows on current main)
3. DEPLOY new application code (manifest-writing NotificationService)
4. START application
5. VERIFY: new appointments produce manifests
```

Writer quiescence is guaranteed by step 1 (application stopped).
No dual-write compatibility needed for single-node deployment.

### Backfill

On main `6a15a40`, the notification service produces outbox rows with:
- Keys: `appt-confirm-patient-{id}`, `appt-confirm-owner-{id}`,
  `appt-cancel-patient-{id}`, `appt-cancel-owner-{id}`
- No `equivalence_snapshot` or `equivalence_digest` in payload
- Owner phone from `Business.primary_contact_phone` (not BusinessUser)
- No reschedule notifications

Since `6ec5a72` was never integrated to main, there are **zero v1-format
outbox rows** in production. All existing outbox rows are legacy format.

**Backfill produces zero manifests.** The migration is pure DDL — no
application code imports, no data transformation. All existing operations
are classified `legacy_unmanifested` or `legacy_irrecoverable` by the
application's decision table, not by the migration.

Non-main branch shapes (e.g., `6ec5a72`'s embedded-JSONB format) are
out of scope for this migration.

## Mapping-Independent Credential Resolution

Current `ConfiguredWhatsAppSenderResolver.resolve()` (line 47-49):
```python
mapped_business = self._business_mappings.get(phone_number_id)
if mapped_business != business_id:
    raise NotificationDeliveryError("channel_identity_mismatch")
```

This rejects a committed `phone_number_id` if the current mapping rotated.
Retry of a committed event fails even though the event is valid.

### Required Fix

Trusted credential lookup by committed sender identity:

1. Worker reads `phone_number_id` from committed event payload
2. Credential resolver accepts `phone_number_id` if:
   - The `phone_number_id` has a valid access token in the credential store
   - The `business_id` on the event matches the `business_id` that ORIGINALLY
     owned this `phone_number_id` (recorded in the manifest, not in current mapping)
3. Mapping rotation (reassigning a phone_number_id to a different business)
   must not break in-flight retries for the original business
4. One tenant cannot use another's sender: the manifest records which
   `phone_number_id` was authorized at commit time, and the credential
   resolver verifies the event's `business_id` against the manifest's
   recorded ownership, not current mutable mapping

This is an application-level fix, not a schema change.

## Downgrade Safety

```python
def downgrade():
    # ACCESS EXCLUSIVE prevents concurrent inserts during check+drop
    op.execute("LOCK TABLE notification_manifests IN ACCESS EXCLUSIVE MODE")

    count = op.get_bind().execute(
        text("SELECT count(*) FROM notification_manifests")
    ).scalar()
    if count > 0:
        raise RuntimeError(
            f"Cannot downgrade: {count} notification manifest(s) exist. "
            "Resolve retention before downgrading."
        )

    op.drop_table("notification_manifests")
```

### Concurrent Contention Outcomes

**Scenario A: Downgrade succeeds (table empty)**
1. Downgrade acquires ACCESS EXCLUSIVE lock
2. Concurrent INSERT blocked (waits for lock)
3. Count check: 0 manifests
4. DROP TABLE executes within same transaction
5. Transaction commits → lock released
6. Blocked INSERT fails: `relation "notification_manifests" does not exist`
7. Application error surfaces as notification creation failure → appointment
   savepoint rolls back (correct fail-closed behavior)

**Scenario B: Downgrade refused (manifests exist)**
1. Downgrade acquires ACCESS EXCLUSIVE lock
2. Concurrent INSERT blocked (waits for lock)
3. Count check: N > 0 manifests
4. RuntimeError raised → transaction rolls back → lock released
5. Blocked INSERT proceeds normally (table still exists)
6. No data loss, no evidence corruption

Both outcomes are terminal and correct. No insert can slip between
the count check and the DROP because the ACCESS EXCLUSIVE lock is
held for the entire transaction.

## Application Correction Checklist (post-schema approval)

### 1. Reschedule Operation Key
- `notif-reschedule-patient-{appt_id}-pa{pending_action_id}`
- `notif-reschedule-owner-{appt_id}-bu{bu_id}-pa{pending_action_id}`
- Each PA produces unique keys → multiple reschedules don't collide

### 2. Immutable `old_start_at`
- Read from `before_snapshot` (captured by `_authoritative_snapshot` BEFORE savepoint)
- Not from `appointment.start_at` (ORM identity-map may reflect post-UPDATE value)

### 3. Complete Manifest Verification
- Replay loads manifest by `(business_id, pending_action_id)`
- Verify `equivalence_digest` matches recomputed canonical
- Verify `recipient_count` matches `jsonb_array_length(recipient_manifest)`
- Classify per decision table; never invent notification success

### 4. E.164 Phone Validation and Dedup
- All recipient phones validated via `core.validators.normalize_phone`
- Dedup by normalized E.164 phone (not raw)
- Invalid phone → `NotificationConfigurationError` before any mutation

### 5. Fixed-Point Price
- `format(Decimal(str(price)), 'f')` — deterministic, no trailing-zero stripping

### 6. WhatsApp Reschedule Renderer
- Add `appointment_rescheduled` case to `_format_message`
- Patient: "Your appointment has been rescheduled from {old_time} to {new_time}"
- Owner: "{patient_name}'s appointment rescheduled from {old_time} to {new_time}"

### 7. Mapping-Independent Credential Resolution
- See dedicated section above
- Trusted lookup by committed sender identity with business-scoped ownership check

### 8. Actor Binding
- `actor_kind` from `ActorContext.verified_role` mapped to customer/owner/system
- `actor_phone` from `ActorContext.normalized_phone` (NULL for system)
- `actor_bu_id` from BusinessUser lookup if role is owner

### 9. Stranded Committing PA Race
- `begin_commit` succeeds → concurrent already-cancelled `INVALID_STATE`
  caught as success → PA stays `committing`
- Fix: wrap begin_commit + post-begin validation in one savepoint,
  OR explicitly `fail_commit` on the caught path
- Independent-session race test required

### 10. True Concurrent AppointmentService Confirmation
- Two independent sessions confirming same PA
- asyncio.Event barrier, pg_blocking_pids observation
- Exactly one appointment + one manifest + correct outbox count
- Blocked contender returns exact existing evidence with correct
  `notification_evidence` classification

### 11. Tenant-Scoped SQL
- All manifest queries scoped by `business_id`
- Composite FK `(business_id, pending_action_id)`
- No cross-tenant manifest lookup

### 12. Retention → Replay Test
- Create appointment with manifest + outbox events
- Delete outbox rows (simulate retention)
- Replay: manifest provides `verified_delivery_unknown` evidence
- Zero new outbox rows or mutations
- Delete manifest → replay returns `irrecoverable`

## Required Migration Evidence Plan

1. **ORM parity**: `NotificationManifest` model matches migration DDL exactly
2. **Offline SQL**: `alembic upgrade --sql 0014:0015` produces reviewable DDL
3. **Fresh upgrade**: empty DB → 0015 → table exists, schema correct
4. **Populated upgrade**: DB with legacy outbox → 0015 → table exists,
   zero manifests, existing outbox untouched
5. **Writer quiescence**: application stopped during migration (single-node)
6. **Main 0014 legacy inventory fixtures**: 4 idempotency key patterns,
   no snapshot/digest, single owner from `Business.primary_contact_phone`,
   no reschedule notifications — tested with exact legacy fixtures
7. **Locked downgrade refusal**: ACCESS EXCLUSIVE → count > 0 → refuse
8. **Concurrent late-writer proof**: Scenario A (INSERT fails after DROP)
   and Scenario B (INSERT proceeds after refused downgrade) — both tested
9. **Upgrade/downgrade/re-upgrade**: roundtrip with no evidence loss
10. **Sanitized preflight**: error messages contain counts only, no PII
