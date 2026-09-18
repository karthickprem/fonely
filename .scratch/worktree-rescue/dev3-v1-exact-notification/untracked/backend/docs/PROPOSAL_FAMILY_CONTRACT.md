# Owner Command Proposal Family Contract v7

## §1 Command Schemas

Each command type has a fixed discriminated field set. No generic nullable fields. Absent fields are omitted from the JSON object, not null. Extra fields are rejected.

**doctor_leave:**
```json
{"family_schema":"owner-command-family-v1","business_id":1,"owner_user_id":10,"command_type":"doctor_leave","target_date":"2026-08-12","target_timezone":"Asia/Kolkata","resource_id":1,"reason":"Leave"}
```

**close_clinic:**
```json
{"family_schema":"owner-command-family-v1","business_id":1,"owner_user_id":10,"command_type":"close_clinic","target_date":"2026-08-12","target_timezone":"Asia/Kolkata","reason":"Holiday"}
```

**close_early:**
```json
{"family_schema":"owner-command-family-v1","business_id":1,"owner_user_id":10,"command_type":"close_early","target_date":"2026-08-12","target_timezone":"Asia/Kolkata","close_time":"17:00","reason":"Closing early"}
```

| Field | doctor_leave | close_clinic | close_early | Type | Range/Constraint | Source |
|---|---|---|---|---|---|---|
| family_schema | required | required | required | literal `"owner-command-family-v1"` | exact match | constant |
| business_id | required | required | required | int | >0 | trusted session |
| owner_user_id | required | required | required | int | >0 | `_require_active_owner` |
| command_type | required | required | required | string | discriminator enum | parser |
| target_date | required | required | required | ISO-8601 date | >= today_local | parser + resolver |
| target_timezone | required | required | required | IANA tz | valid ZoneInfo | business.timezone |
| resource_id | required | absent | absent | int | >0 | `_resolve_resource` |
| close_time | absent | absent | required | `"HH:MM"` | > now_local if today | parser |
| reason | required | required | required | string | 1-200 chars | parser, default if empty |

## §2 DB Schema

```sql
CREATE TABLE owner_command_proposals (
  id                  VARCHAR(36) PRIMARY KEY,
  business_id         INTEGER NOT NULL REFERENCES businesses(id),
  owner_user_id       INTEGER NOT NULL,
  invocation_id       VARCHAR(36) NOT NULL,
  family_id           VARCHAR(64) NOT NULL,
  attempt_number      INTEGER NOT NULL DEFAULT 1 CHECK (attempt_number > 0),
  command_type        VARCHAR(40) NOT NULL
                      CHECK (command_type IN ('close_clinic','close_early','doctor_leave')),
  command_payload     JSONB NOT NULL,
  preview_snapshot    JSONB NOT NULL,
  payload_digest      VARCHAR(64) NOT NULL,
  status              VARCHAR(30) NOT NULL DEFAULT 'pending_confirmation'
                      CHECK (status IN ('pending_confirmation','executing','completed','rejected','expired','failed')),
  result_evidence     JSONB,
  expected_version    INTEGER NOT NULL DEFAULT 1 CHECK (expected_version > 0),
  idempotency_key     VARCHAR(200) NOT NULL,
  expires_at          TIMESTAMPTZ NOT NULL,
  confirmed_at        TIMESTAMPTZ,
  completed_at        TIMESTAMPTZ,
  failure_code        VARCHAR(100),
  failure_message     VARCHAR(500),
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  FOREIGN KEY (business_id, owner_user_id) REFERENCES business_users(business_id, id)
);

UNIQUE(business_id, invocation_id);            -- transport dedup
UNIQUE(business_id, idempotency_key);          -- semantic dedup
UNIQUE(business_id, family_id, attempt_number); -- locked attempt allocation
CREATE UNIQUE INDEX uq_owner_proposal_owner_pending
  ON owner_command_proposals (business_id, owner_user_id)
  WHERE status IN ('pending_confirmation', 'executing');  -- at most one active per owner
CREATE INDEX ix_owner_proposal_family
  ON owner_command_proposals (business_id, family_id);
CREATE INDEX ix_owner_proposal_pending_expiry
  ON owner_command_proposals (status, expires_at)
  WHERE status = 'pending_confirmation';
```

`executing` is written to DB by T3 within the outer tx (before command savepoint) but is always resolved to a terminal state before outer commit. A committed row in `executing` indicates a crashed/incomplete transaction — recovery treats it as pending retry (see §5). Partial unique index covers both `pending_confirmation` and `executing` to prevent concurrent active proposals.

All reads scoped by `business_id`.

## §3 Digests and Lock

| Name | Preimage | Algorithm | Stored | Purpose |
|---|---|---|---|---|
| `family_id` | Per-command canonical JSON (§1) | `SHA-256(canonical.encode("utf-8")).hexdigest()` | VARCHAR(64) column | Family queries, lock derivation |
| `payload_digest` | `canonical_json({"command_payload": <dict>, "preview_snapshot": <dict>})` | SHA-256 hex | VARCHAR(64) column | Integrity at confirmation |
| observation digest | `canonical_json(sorted_targets + schedule_state)` | SHA-256 hex | NOT stored | Drift detection at confirmation |
| `lock_key` | `BLAKE2b(bytes.fromhex(family_id), key=b"fonely.owner_proposal_family.v1", digest_size=8)` | signed int64 via `struct.unpack(">q", ...)` | NOT stored | `pg_advisory_xact_lock` |

Canonical: `json.dumps(obj, sort_keys=True, separators=(",",":"), ensure_ascii=False)`. No `default=`.

**Literal vectors:**
```
doctor_leave (business_id=1, owner_user_id=10):
  canonical: {"business_id":1,"command_type":"doctor_leave","family_schema":"owner-command-family-v1","owner_user_id":10,"reason":"Leave","resource_id":1,"target_date":"2026-08-12","target_timezone":"Asia/Kolkata"}
  family_id: bfaf524896c7d0d7ed45cc08c9c9127003565b36733f065d830e7ff6708fb508
  lock_key:  -4888324136777874342

Same command, business_id=2:
  family_id: 295c56e63b3c70511fb4c8b5db23fd1aee24bef906aec465ce55a4bcf8db9958
  lock_key:  -3348873486638007229
```

## §4 Lookup Order

Under family lock, for each new invocation:

1. **Completed winner**: `WHERE business_id=:bid AND family_id=:fid AND status='completed' ORDER BY created_at ASC, id ASC LIMIT 1`. If found → replay evidence. STOP.
2. **Active**: `WHERE business_id=:bid AND family_id=:fid AND status IN ('pending_confirmation','executing') ORDER BY created_at DESC, id DESC LIMIT 1`. If `executing` → stale from crashed tx; treat as pending for retry. If `pending_confirmation` and `expires_at > now_utc` → return "already pending". If `pending_confirmation` and `expires_at <= now_utc` → CAS to expired (T6), continue to step 3.
3. **Latest terminal**: `WHERE business_id=:bid AND family_id=:fid ORDER BY attempt_number DESC LIMIT 1`. If retryable → INSERT attempt `MAX(attempt_number)+1` (T2). If non-retryable → "manual review". STOP.
4. **No history** → INSERT attempt 1 (T1). STOP.

## §5 Transitions

All transitions occur within the caller's outer transaction. Family advisory lock required for all. `executing` is never committed.

### Topology per transition

| # | Trigger | Outer TX | T3 executing write | SP | Inside SP | After SP | Durable at commit | Provenance |
|---|---|---|---|---|---|---|---|---|
| T1 | new, no history | open | — | nested INSERT | INSERT pending | — | pending | new family |
| T2 | retryable terminal | open | — | nested INSERT | INSERT pending | — | pending | retry |
| T3→T7 | YES → success | open | CAS pending→executing | command SP | sched+cancel+outbox + CAS executing→completed (LAST) | — | completed + all effects | atomic in SP |
| T3→T8 | YES → drift | open | CAS pending→executing | command SP, rolled back | recompute targets (read-only) | CAS executing→failed:target_drift | failed | known-nonmutating (SP rolled back pre-mutation) |
| T3→T9 | YES → sched conflict | open | CAS pending→executing | command SP, rolled back | upsert raises | CAS executing→failed:sched_conflict | failed | known-nonmutating (SP rolled back) |
| T3→T10 | YES → digest bad | open | CAS pending→executing | NOT entered | — | CAS executing→failed:integrity | failed | known-nonmutating (no SP) |
| T3→T11 | YES → date past | open | CAS pending→executing | command SP, rolled back | date check (read-only) | CAS executing→failed:date_past | failed | known-nonmutating (SP rolled back pre-mutation) |
| T3→T12 | YES → exception | open | CAS pending→executing | command SP, rolled back | partial mutation | CAS executing→failed:execution_error | failed | known-nonmutating (SP rolled back) |
| T4 | YES but expired | open | — | — | — | CAS pending→expired | expired | TTL elapsed |
| T5 | NO | open | — | — | — | CAS pending→rejected | rejected | owner rejected |
| T6 | stale on preview | open | — | — | — | CAS pending→expired | expired | TTL elapsed |

### Outer TX rollback (any transition)

If the outer transaction rolls back (deadlock, serialization failure, connection loss, explicit rollback) **before commit**:
- ALL writes are lost. Proposal remains in its prior committed state (pending_confirmation for confirm flow, or no row for preview flow).
- No effects (no sched exception, no cancellations, no outbox).
- Next invocation starts fresh from the prior committed state.

### Confirm flow detail

```
caller outer tx:
  compute family_id
  pg_advisory_xact_lock(lock_key)
  verify owner_user_id == proposal.owner_user_id
  if expires_at <= now_utc: CAS pending→expired, return            [T4]
  CAS pending→executing (ver N→N+1)                                [T3]
  verify payload_digest → if mismatch:                             [T10]
    CAS executing→failed:integrity (outside SP), return
  begin_nested (command savepoint):
    revalidate target_date → if past:                              [T11]
      raise → SP rolled back →
      CAS executing→failed:date_past (outside SP), return
    recompute targets+schedule → if drift:                         [T8]
      raise → SP rolled back →
      CAS executing→failed:target_drift (outside SP), return
    lock appointments FOR UPDATE ORDER BY id ASC
    upsert schedule exception (FOR UPDATE check) → if conflict:    [T9]
      raises → SP rolled back →
      CAS executing→failed:sched_conflict (outside SP), return
    cancel targets (authoritative locked IDs)
    CAS executing→completed + evidence                             [T7, LAST inside SP]
  on any other exception from SP:                                  [T12]
    SP rolled back by PG
    CAS executing→failed:execution_error (outside SP)
  outer commit → all durable atomically
```

Key points:
- T3 writes `executing` to DB BEFORE command SP opens.
- T7 CAS to `completed` is LAST statement INSIDE successful SP.
- T8/T9/T11/T12: failed CAS written OUTSIDE SP, AFTER SP rollback confirmed, still INSIDE outer tx.
- If outer tx rolls back (for any reason before commit): ALL writes lost. Proposal reverts to `pending_confirmation` at its prior committed version. No effects.

### Expiry and reject ordering

- `expires_at` stored as TIMESTAMPTZ. Compared against `datetime.now(UTC)`.
- YES on expired: T4 wins (expired written). Checked before any SP.
- NO on expired: T5 wins (rejected). Reject does not check expiry — explicit owner intent supersedes TTL.
- Concurrent YES + expire: family lock serializes. First acquires lock and evaluates.

## §6 Terminal Provenance

```python
_NONMUTATING_FAILURE_CODES = frozenset({
    "target_drift", "schedule_exception_conflict",
    "payload_integrity_mismatch", "target_date_past",
})

def _is_retryable_terminal(status, failure_code):
    return (status in ("rejected", "expired")
            or (status == "failed" and failure_code in _NONMUTATING_FAILURE_CODES))
```

| Terminal | Failure Code | Effects at commit | Retryable |
|---|---|---|---|
| completed | — | sched + cancel + outbox committed | No (replay) |
| rejected | — | none | Yes |
| expired | — | none | Yes |
| failed | target_drift | none (SP rolled back pre-mutation) | Yes |
| failed | schedule_exception_conflict | none (SP rolled back) | Yes |
| failed | payload_integrity_mismatch | none (pre-SP) | Yes |
| failed | target_date_past | none (SP rolled back pre-mutation) | Yes |
| failed | execution_error | none (SP rolled back) | No (operator review — exception was unexpected) |

### Postcommit dispatch

After outer commit ACK received:
- **completed**: outbox rows exist in `pending` status. Notification worker claims and delivers asynchronously. `result_evidence.cancelled_appointments` records appointment IDs.
- **failed/rejected/expired**: no outbox rows, no sched exceptions, no cancellations.
- No external effects before commit ACK in any transition.

### Lost-ACK recovery

If commit sent but ACK lost (caller uncertain):
1. Fresh transaction: acquire family lock.
2. Query proposal status.
3. If `completed` → commit succeeded, replay.
4. If `pending_confirmation` → commit failed (rolled back), retry confirm.
5. Cross-validate with `appointment_commits` and `notification_outbox` if needed.

## §7 Migration

### Fresh final shape (0015 never integrated to main)

CREATE TABLE with all §2 columns/constraints. Populated preflight before partial unique index: `SELECT count(*) FROM owner_command_proposals WHERE status='pending_confirmation' GROUP BY business_id, owner_user_id HAVING count(*)>1` must return 0 rows. Downgrade: `IF EXISTS (SELECT 1 FROM owner_command_proposals) THEN RAISE` before any DDL.

### Hypothetical populated 0015 upgrade

If 0015 were already integrated with rows lacking `family_id`/`invocation_id`/`attempt_number`:
1. ADD columns NULLABLE.
2. Backfill: if `command_payload->>'family_schema' = 'owner-command-family-v1'` → compute `family_id` from payload, set `attempt_number` from stored value or 1, set `invocation_id = id`.
3. Rows without `family_schema` discriminator → `family_id = 'legacy-unclassified-' || id` (unique, non-canonical). `attempt_number = 1`. Operator classifies manually.
4. Validate: no NULL `family_id`, no duplicate `(business_id, family_id, attempt_number)`.
5. If validation fails → RAISE with sanitized counts. Do not proceed.
6. SET NOT NULL. ADD constraints/indexes.

## §8 Probes

### Probe 1: Full lifecycle (covers T1, T2, T3+T7, T3+T8, T3+T10, T3+T11, T4, T5, T6, completed replay)

1. Seed business, owner, resource, tomorrow appointment.
2. Preview → assert pending, family_id matches vector, attempt_number=1. **(T1)**
3. YES → completed. Assert: appointment cancelled, sched exception, outbox pair, evidence has appointment_id. **(T3+T7)**
4. Same command → replay winner, same proposal_id, no new row/effects. **(completed replay)**
5. Fresh seed. Preview → inject drift → YES → failed:target_drift. **(T3+T8)**
6. Same command → attempt-2 created. **(T2)**
7. YES → completed. Same command → replay. **(T3+T7, replay)**
8. Fresh seed. Preview → expired proposal (set expires_at in past) → YES → expired. **(T4)**
9. Fresh seed. Preview → NO → rejected. Same command → attempt-2 (retryable). **(T5, T2)**
10. Fresh seed. Preview → stale. Next preview same family → expired + new attempt. **(T6, T2)**
11. Preview → inject digest corruption → YES → failed:integrity. **(T3+T10)**
12. Preview → inject date_past at confirm → YES → failed:date_past. **(T3+T11)**

### Probe 2: Concurrency + isolation + rollback + recovery

1. **Inner failure → post-SP failed CAS persists**: Inject exception inside command SP. Assert: SP rolled back, CAS executing→failed:execution_error written outside SP, outer commit succeeds. Fresh session: proposal is `failed`, no sched/cancel/outbox effects. **(T3→T12)**
2. **Explicit outer rollback restores pending**: Start confirm flow, CAS to executing, then explicitly rollback outer tx. Fresh session: proposal is `pending_confirmation` at original version. No effects. **(outer rollback)**
3. **Independent-session serialization**: Two sessions confirm same proposal. Family lock serializes. Winner completes. Loser acquires lock, sees completed → replay. No duplicate effects. **(lock serialization)**
4. **Deadlock/serialization failure**: Two sessions with overlapping lock order. PG detects deadlock, one rolls back. Rolled-back session retries, finds completed or pending. **(deadlock recovery)**
5. **Lost-ACK recovery**: Commit completed successfully. Fresh session (no assumed outcome): acquire family lock, query proposal status → if completed replay; if pending retry. Cross-validate AppointmentCommit existence independently. **(lost-ACK)**
6. **Cross-tenant isolation**: Two businesses, same payload → different family_ids. Each completes independently, no cross-contamination. **(isolation)**
7. **Migration probes**: Verify partial unique index covers `pending_confirmation` AND `executing` (INSERT two rows in those statuses for same owner → second fails). Verify `UNIQUE(business_id, family_id, attempt_number)`. Verify downgrade blocked on any row. **(migration)**

### Self-check

- Every §5 transition covered: T1(P1.2), T2(P1.6), T3→T7(P1.3), T3→T8(P1.5), T3→T10(P1.11), T3→T11(P1.12), T3→T12(P2.1), T4(P1.8), T5(P1.9), T6(P1.10).
- Outer rollback: P2.2. Lock serialization: P2.3. Deadlock: P2.4. Lost-ACK: P2.5. Cross-tenant: P2.6. Migration: P2.7.
- Literal vectors: §3. Expiry/reject ordering: §5. Postcommit dispatch: §6.
