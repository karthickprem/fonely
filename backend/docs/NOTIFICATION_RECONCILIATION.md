# Notification Reconciliation Runbook

## Overview

Every appointment mutation (create, cancel, reschedule) writes a patient + owner
notification pair to the `notification_outbox` table inside the same database
transaction. Each event carries an `equivalence_snapshot` and `equivalence_digest`
in its JSONB payload so that replays can verify correctness without re-deriving
presentation strings.

## Metric: `notification_reconciliation_total`

Labels: `outcome`, `operation`

| Outcome | Meaning | Action |
|---|---|---|
| `fresh_insert` | Both events inserted successfully | None |
| `exact_existing` | Replay found both events, all facts match | None |
| `repaired_missing` | One event missing, repaired in savepoint | Investigate why one was lost |
| `missing_evidence` | Neither patient nor owner event found | Data loss. Manual reconciliation required |
| `legacy_fail_closed` | Pre-v1 event without equivalence snapshot | Cannot auto-verify. Manual check needed |

## Manual Reconciliation Steps

1. Query the notification outbox for the appointment:
   ```sql
   SELECT id, idempotency_key, recipient_type, status, payload
   FROM notification_outbox
   WHERE entity_type = 'appointment' AND entity_id = <appointment_id>
   ORDER BY id;
   ```

2. If events are missing, check whether the appointment transaction committed:
   ```sql
   SELECT id, status, created_at FROM appointments WHERE id = <appointment_id>;
   ```

3. For legacy events (no `equivalence_snapshot`), compare payload fields manually
   against the appointment record and business configuration.

4. To repair a missing event, insert it directly with the correct idempotency key
   pattern:
   - Create: `appt-confirm-patient-{aid}`, `appt-confirm-owner-{aid}`
   - Cancel: `appt-cancel-patient-{aid}`, `appt-cancel-owner-{aid}`
   - Reschedule: `appt-reschedule-patient-{aid}-pa{paid}`, `appt-reschedule-owner-{aid}-pa{paid}`

## Idempotency Key Format

| Operation | Patient Key | Owner Key |
|---|---|---|
| create | `appt-confirm-patient-{appointment_id}` | `appt-confirm-owner-{appointment_id}` |
| cancel | `appt-cancel-patient-{appointment_id}` | `appt-cancel-owner-{appointment_id}` |
| reschedule | `appt-reschedule-patient-{appointment_id}-pa{pending_action_id}` | `appt-reschedule-owner-{appointment_id}-pa{pending_action_id}` |
