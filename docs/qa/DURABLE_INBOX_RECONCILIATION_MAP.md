# Durable Inbox Reconciliation Map

Task: `DEV2-DURABLE-INBOX-FINAL-CORRECTION-20260804-01`

## Fixed bases

- Frozen Dev2 source artifact: `0eb5eb09f98f7850491ca1150dea502d48931773`
- Historical exact-SHA CI: `30913408997` — success
- Reconciliation base: `6b60596156faefba5f6d4e993b8ceac0811b7087`
- Target branch: `integration/dev2-durable-inbox-on-6b605961`
- Migration sequence preserved: `0011 -> 0012 -> 0013 -> 0014`

## Dev2 commits carried forward, in dependency order

1. `ab8fd94` durable inbound event queue foundation
2. `f79378f` initial typing correction
3. `ba473d8` named migration uniqueness parity
4. `6313794` deterministic readiness deadline test
5. `46f7b61` migration 0013 constraints/channel identity
6. `ae0adc0` webhook fail-closed persistence/channel identity
7. `a0987c3` tenant-scoped inbound repository attempts
8. `ebef02b` per-event worker/outbound enqueue foundation
9. `eb5dc27` worker deployment wiring
10. `ed97bc6` inbound retention policies
11. `b211900` repository formatting
12. `0865c74` worker formatting
13. `f220b77` caller-owned conversation transaction boundary
14. `54112b2` deterministic claims/order/lease foundation
15. `f92a44e` three-phase worker foundation
16. `29e4833` notification/inbound lifecycle coupling
17. `f9845da` strict webhook validation/auth
18. `2146e86` migration 0014 claim infrastructure/dedup removal
19. `27f20a7` lock-free provider replay and claim fencing
20. `e114bd2` delivery attempt evidence/tenant sender routing
21. `629eb91` initial PostgreSQL acceptance suite
22. `ec8957c` explicit test-only logging sender
23. `c31b369` caller transaction test corrections
24. `1de7f86` reschedule caller commit correction
25. `c5731dd` replay/order/lease/retention review fixes
26. `ce206a3` provider reconciliation/channel identity
27. `eb36b7b` expanded PostgreSQL delivery tests
28. `bb58517` independent review corrections
29. `893abf6` notification claim/status monotonicity fixes
30. `d6cb441` final-attempt failure lifecycle
31. `81cc227` trusted reverse tenant mapping
32. `51b7307` notification PG mapping fixture
33. `dc32490` terminal provider failure and inbound worker mapping
34. `2a75c89` non-idempotent HTTP no-retry and stale callback protection
35. `0eb5eb0` final old-base crash-window evidence

## Overlap map

Only one source file changed in both the integrated Dev1 range and the Dev2 range:

### `backend/src/fonely/services/conversation.py`

| Shared area | Dev1 invariant to preserve | Dev2 invariant to preserve | Intended merged behavior |
|---|---|---|---|
| `_validate_facts` / availability flow | Authoritative `AvailabilityService`; exact local-time/policy rejection reasons | No change intended | Keep Dev1 implementation unchanged |
| `_check_availability_and_propose` | mutation-first availability; deterministic resource locking; reschedule exclusion; durable `booking_attempt` idempotency identity | No change intended | Keep Dev1 implementation unchanged |
| `_handle_confirmation`, `_confirm_booking`, `_confirm_cancellation`, `_confirm_reschedule` | exact replay equivalence; booking_attempt rotation on rejection/conflict; truthful conflict recovery | remove internal `session.commit()` so route/worker owns one outer transaction | Keep Dev1 replay/conflict/attempt logic, remove only internal commits |
| `_evict_stale` and cache globals | durable conversation `booking_attempt` persistence and active-context semantics | rollback-safe cache invalidation; remove stale `_PHONE_INDEX` references | Keep Dev1 context fields, add Dev2 cache cleanup/invalidation helper |

## Non-overlapping Dev2 areas

The webhook, inbound worker/repository, notification delivery evidence, sender resolver, migrations 0012–0014, retention, Docker/Compose, and inbox-specific tests do not overlap integrated Dev1 source changes and should carry forward in dependency order.

## Conflict-resolution rules

1. Never replace the complete current-main `conversation.py` with the old-base file.
2. Resolve `conversation.py` manually at function granularity.
3. Dev1 availability, booking-attempt, replay, locking, and conflict behavior wins wherever unrelated to transaction ownership.
4. Dev2 caller-owned transaction boundary and cache invalidation are added without reverting Dev1 code.
5. All other conflicts are resolved semantically against current-main callers and tests; no unconditional ours/theirs.
