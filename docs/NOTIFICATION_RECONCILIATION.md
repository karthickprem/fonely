# Notification Reconciliation Operator Procedure

## Policy

Only exact versioned v1+ notification evidence authorizes automated replay or repair. All non-exact historical formats produce `legacy_unverifiable` and require manual reconciliation through this procedure.

No automated backfill, sidecar database, or weaker equivalence contract is authorized.

## Outcome Codes

| Code | Metric Label | Meaning | Operator Response |
|------|-------------|---------|-------------------|
| `exact_existing` | `outcome=exact_existing` | Both v1 patient and owner rows exist and are fully equivalent | No action required |
| `exact_repaired` | `outcome=exact_repaired` | One v1 member was missing; the surviving member's snapshot was validated and the missing member was atomically inserted | Verify final pair count is exactly two |
| `legacy_unverifiable` | `outcome=legacy_unverifiable` | Committed rows lack v1 evidence markers (equivalence_snapshot, equivalence_digest) | See Legacy Reconciliation below |
| `evidence_conflict` | `outcome=evidence_conflict` | v1 markers exist but digest, snapshot, facts, or payload disagree | See Evidence Conflict below |
| `cross_tenant_conflict` | `outcome=cross_tenant_conflict` | Global idempotency key belongs to a different business | Escalate as integrity incident |
| `missing_evidence` | `outcome=missing_evidence` | No outbox rows exist for the expected idempotency keys | See Missing Evidence below |
| `insert_failed` | `outcome=insert_failed` | Atomic pair insertion failed (constraint violation, connection error) | Retry or investigate |

## Metric

Counter: `notification_reconciliation_total`

Labels (all bounded, no PII):
- `operation`: `create` | `cancel` | `reschedule`
- `format`: `v1` | `legacy` | `none` | `mixed` | `partial`
- `outcome`: one of the codes above

Incremented exactly once per terminal replay attempt. Process-local; resets on restart.

## Alerting

Alert on any sustained nonzero rate of:
- `legacy_unverifiable`
- `evidence_conflict`
- `cross_tenant_conflict`
- `insert_failed`

## Legacy Reconciliation (`legacy_unverifiable`)

### What happened

The appointment was committed before v1 evidence was deployed. Outbox rows exist but lack `equivalence_snapshot` and `equivalence_digest`. The system cannot verify that the stored notification content matches the immutable appointment facts.

### What NOT to do

- Do not repair or reconstruct the missing v1 evidence from current business configuration
- Do not infer year, timezone offset, or fold from rendered date/time strings
- Do not substitute current owner phone, clinic name, or WhatsApp mapping for historical values
- Do not fabricate the missing pair member
- Do not log or export patient phone, name, appointment ID, payload, or idempotency key to general channels

### Operator steps

1. Identify scope using aggregate, read-only database queries (counts only):
   ```sql
   SELECT event_type, recipient_type, status, count(*)
   FROM notification_outbox
   WHERE business_id = :bid
     AND entity_id = :appt_id
   GROUP BY 1, 2, 3;
   ```

2. Check if both patient and owner rows exist for the entity.

3. If both exist and the appointment is in a terminal state (confirmed/cancelled/rescheduled), the notification was likely delivered. Leave rows unchanged.

4. If one member is missing:
   - Preserve the surviving row
   - Do NOT insert the missing member
   - If the appointment mutation succeeded, the patient/owner may not have received their notification
   - Create a NEW audited remediation notification with a new idempotency key if business impact requires it

5. Record only sanitized aggregate findings in incident channels. Never include PII.

## Evidence Conflict (`evidence_conflict`)

### What happened

v1 markers exist but the content is inconsistent: digest mismatch, snapshot disagrees between patient and owner rows, or persisted payload does not match the expected rendering.

### Operator steps

1. Preserve all rows unchanged — they are integrity evidence
2. Compare the stored `equivalence_snapshot` against immutable `appointment_commits` and `pending_actions` evidence under restricted, audited access
3. Determine whether the inconsistency is due to a code defect, concurrent modification, or data corruption
4. Do not overwrite or "fix" the conflicting evidence
5. Escalate to engineering with the sanitized discrepancy description (field names and types, not values)

## Missing Evidence (`missing_evidence`)

### What happened

The replay path found no outbox rows for the expected idempotency keys. This can occur if:
- The original transaction rolled back after the appointment was committed but before outbox rows were flushed
- The outbox rows were deleted (should not happen in normal operation)

### Operator steps

1. Verify the appointment exists and is in the expected terminal state
2. Check `pending_actions` for the corresponding action's terminal state
3. If the appointment was committed but outbox rows never existed, the notification was never sent
4. Create a NEW remediation notification if business impact requires it
5. Do not fabricate historical outbox rows

## Cross-Tenant Conflict

Escalate immediately as a data integrity incident. A global idempotency key collision across tenants indicates either a key generation defect or data corruption.

## General Rules

- Metrics are process-local and reset on restart. Logs and database rows are the authoritative evidence.
- Never include patient PII (phone, name) in tickets, logs, or Slack messages.
- All row-level investigation requires authorized database access through audited tooling.
- Remediation notifications must use new idempotency identities — never reuse historical keys.
- No appointment mutation is replayed or undone merely because notification evidence is incomplete.
