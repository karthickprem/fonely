# 0015 Minimal Contract Implementation Checklist

## Chief Clauses → Code/Test Changes

### 1. Trusted business/actor/invocation idempotency
- [ ] `process_command` receives trusted `business_id` from verified session
- [ ] `_require_active_owner` validates exactly one active owner for phone
- [ ] Each preview creates a unique `idempotency_key` from command payload digest
- [ ] Transport replay (same message retried) hits `UNIQUE(business_id, idempotency_key)` → returns existing
- [ ] No `family_id`, no `attempt_number`, no `invocation_id` column — deferred
- [ ] No advisory lock — deferred (single pending per owner is sufficient for one-shot)
- **Code**: simplify `_persist_preview` to remove family lock/retry logic
- **Code**: remove `count_by_key_prefix`, `find_completed_by_key_prefix` from repository

### 2. Four durable states only
- [ ] Status CHECK: `pending_confirmation`, `rejected`, `expired`, `completed`
- [ ] No `executing` or `failed` in status enum
- [ ] Migration 0015 CREATE TABLE uses 4-state CHECK
- **Code**: remove all `executing`/`failed` status writes from owner_commands.py
- **Code**: T3 confirm goes directly `pending_confirmation → completed` inside command SP

### 3. One pending per owner
- [ ] Partial unique index: `WHERE status = 'pending_confirmation'`
- [ ] Preview checks for existing pending before creating
- [ ] Expired pending discovered on next preview → CAS to expired, then new preview
- **Code**: keep existing `_handle_destructive_preview` expiry check

### 4. Atomic one-shot confirm
- [ ] Command savepoint: revalidate facts + lock appointments + upsert exception + cancel + outbox + CAS `pending→completed` LAST inside SP
- [ ] SP rollback → proposal stays `pending_confirmation`, no effects
- [ ] Outer tx rollback → same: pending, no effects
- [ ] No intermediate `executing` state persisted
- **Code**: rewrite `_handle_confirm` — remove CAS to executing, put completed CAS inside SP

### 5. Exact replay / legacy fail-closed
- [ ] Completed proposal: return evidence on same command
- [ ] All non-v1 notification evidence: `legacy_unverifiable`
- [ ] Notification pair with v1 evidence: exact replay/repair
- **Code**: keep existing notification service (already correct)

### 6. YES/NO/expiry
- [ ] YES on valid pending → T3 confirm
- [ ] YES on expired → T4 CAS to expired
- [ ] NO → T5 CAS to rejected
- [ ] Stale on preview → T6 CAS to expired
- **Code**: keep existing YES/NO/expiry logic, simplify to remove executing path

### 7. Drift abort/repreview
- [ ] Confirm revalidates: target date, targets+schedule digest, appointments FOR UPDATE
- [ ] Drift → SP rollback, proposal stays pending, return "send again"
- [ ] No `failed` state — just stays pending for repreview
- **Code**: drift detection stays, but instead of CAS to failed, just return error (proposal stays pending)

### 8. Migration 0015
- [ ] Final shape: no family_id, no attempt_number, no invocation_id
- [ ] Remove those columns from CREATE TABLE and ORM
- [ ] Status CHECK: 4 states only
- [ ] Partial unique: pending_confirmation only
- [ ] Downgrade: fail on ANY row
- **Code**: rewrite migration and ORM model

### 9. Operator reconciliation
- [ ] Notification runbook: legacy_unverifiable, evidence_conflict, operator procedure
- [ ] Owner recipient preflight
- **Code**: keep existing runbook (already correct)

### 10. PG Probes
- [ ] Happy path: preview → YES → completed, appointment cancelled, outbox, evidence, replay
- [ ] Adversarial: two sessions confirm same proposal (lock serializes), reject/expiry new preview, rollback→pending, two-business isolation
- **Code**: rewrite PG tests to match simplified lifecycle

## Contradictions Found: NONE
The narrowed scope is strictly a subset of the prior design. Removing retry generations, family_id, advisory lock, executing/failed simplifies without contradicting any approved invariant.
