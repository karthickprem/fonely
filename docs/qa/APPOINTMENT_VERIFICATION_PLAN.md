# Appointment Capability — Independent Verification Plan (Pre-D)

## Status and scope

This is Dev2's verification design for the founder-approved generic appointment capability, first configured for a salon. It is not production implementation, test implementation, or approval of Dev3 D1/D2. The synchronized baseline is commit `058a48a`; the separately assigned Dev3 worktree contains an in-progress D1/D2 draft, which was read only to identify verification seams and interface questions.

The governing product decisions override older target text that mentions temporary holds: **creating a proposal does not reserve capacity**. Capacity is created only by a deterministic committed operation. A salon stylist is the first `Resource` configuration, not a salon-specific schema fork.

Verification layers used below:

- **Pure** — deterministic unit tests without database or network.
- **Migration** — Alembic source/recorder, populated-upgrade, downgrade and ORM parity.
- **PG** — future real PostgreSQL contract after D1/D2 approval and stable interfaces.
- **Eval** — conversational/tool-selection evidence only; never proof of database behavior.
- **Pilot** — monitored real-business observation after all engineering gates.

Current coverage status values are `foundation`, `conversation_only`, `partial`, and `missing`. `foundation` means reusable generic infrastructure exists; `conversation_only` means synthetic eval evidence exists without deterministic/database proof; `partial` means multiple implementation/evidence layers exist but are incomplete; `missing` means there is no meaningful implementation or eval coverage. No row is `verified` because appointment PostgreSQL behavior has not run.

## Requirement traceability matrix

| ID | Product behavior | Dev3 implementation gate | Pure unit test | Migration/parity test | Future PG test | Eval case IDs | Pilot observation | Status; gap/owner |
|---|---|---|---|---|---|---|---|---|
| SVC-01 | Service duration is authoritative per service | D1 schema/check; D2 pure duration/derived-end contract; D3 reads and persists authoritative service facts | duration boundaries and derived end | non-null/check/backfill/parity | service duration constraints | APPT-001,003,036 | configured vs actual duration corrections | foundation; D1/D2/D3/Dev2 as gated |
| SVC-02 | Before/after buffers are non-negative and authoritative | D1 schema/check; D2 pure buffer/effective-window contract; D3 reads and persists authoritative buffer facts | zero/max/negative and arithmetic | defaults/backfill/checks | buffer constraints/snapshots | APPT-004,005,035 | buffer-related owner corrections | conversation_only; D1/D2/D3/Dev2 as gated |
| SVC-03 | Inactive service is neither offered nor booked | D2 active-service policy and typed error/result; D3 tenant/active repository filter and confirmation revalidation | inactive service policy | index/parity if added | inactive service rejected | APPT-028,036 | invalid-service attempts | partial; D1/D2/D3/Dev2 as gated |
| SVC-04 | Inactive staff/resource is neither offered nor booked | D2 inactive-resource policy and typed error/result; D3 availability filter and confirmation revalidation after deactivation | inactive resource policy | parity | inactive resource rejected | APPT-029 | wrong-staff incidents | partial; D1/D2/D3/Dev2 as gated |
| SVC-05 | Only eligible staff can perform a service | D1 eligibility relation; D2 pure eligibility policy; D3 tenant/active eligibility query and confirmation revalidation | eligibility matrix | unique/FKs/indexes/parity | eligibility and tenant graph | APPT-030,050 | owner correction by eligibility | conversation_only; D1/D2/D3/Dev2 as gated |
| SVC-06 | Named staff request selects exactly that staff | D2 named-resource policy/no-fallback result; D3 tenant-scoped resolution and selected-resource persistence | named match/not-found/inactive | — | exact resource selected | APPT-002,009,043, MLT-009,016, MLH-009 | substitution complaints | conversation_only; D1/D2/D3/Dev2 as gated |
| SVC-07 | “Any staff” selects one concrete eligible resource before proposal | D2 deterministic candidate-selection contract; D3 queries eligible resources and persists one selected resource | deterministic candidate selection | — | selected resource persisted | APPT-011,050, MLT-001, MLH-002, MLS-002,004 | offered-choice acceptance | conversation_only; D1/D2/D3/Dev2 as gated |
| SVC-08 | Named resource never silently falls back | D2 no-substitution policy; D3 rejects unavailable named resource and writes no substitute | named unavailable, alternatives separate | — | no substitute row committed | APPT-002,029,043 | silent-substitution count = 0 | conversation_only; D1/D2/D3/Dev2 as gated |
| SVC-09 | Multiple eligible staff may be simultaneously available | D2 multi-candidate result contract; D3 repository returns all eligible open resources | multi-resource result set | — | same time/different resources | APPT-011,050 | missed valid choices | conversation_only; D1/D2/D3/Dev2 as gated |
| CAP-01 | Same resource cannot have overlapping active allocation | D1 exclusion constraint; D2 pure overlap policy; D3 appointment/allocation transaction and constraint translation | overlap truth table | exclusion catalog/parity | all overlap shapes | APPT-010,013-015 | double bookings = 0 | conversation_only; D1/D2/D3/Dev2 as gated |
| CAP-02 | Different resources may overlap | D1 resource-scoped exclusion; D2 pure resource-capacity policy; D3 persists concurrent allocations | resource-scoped overlap | exclusion columns | different resource same time | APPT-011 | simultaneous utilization | conversation_only; D1/D2/D3/Dev2 as gated |
| CAP-03 | Exact end/start adjacency is allowed (`[)`) | D1 half-open range expression; D2 pure boundary arithmetic; D3 persists adjacent allocations | boundary adjacency | constraint expression | exact adjacency | APPT-012,042,045,046 | boundary failures | conversation_only; D1/D2/D3/Dev2 as gated |
| CAP-04 | Buffers can create a conflict when service times do not | D1 effective-window fields/constraint; D2 pure buffer arithmetic; D3 persists effective allocation and translates conflict | buffer-only overlap | snapshot/effective checks | buffer-only exclusion conflict | APPT-004,005,035 | setup/cleanup collision reports | conversation_only; D1/D2/D3/Dev2 as gated |
| CAP-05 | Owner manual appointments occupy capacity | D1 source/allocation type; D2 manual-operation contract; D3 owner-authorized manual appointment transaction | source/allocation policy | enum/check/parity | manual booking conflict | APPT-031 (mis-modelled) | manual/phone collision count | conversation_only; D1/D2/D3/Dev2 as gated |
| CAP-06 | Walk-ins occupy capacity | D1 source/allocation type; D2 walk-in contract; D3 owner-authorized walk-in transaction | walk-in allocation policy | enum/check/parity | walk-in conflict | APPT-022 | walk-in collision count | conversation_only; D1/D2/D3/Dev2 as gated |
| CAP-07 | Owner blocks use ResourceAllocation, not fake appointments | D1 owner-block allocation invariant; D2 block contract; D3 owner-authorized block transaction | owner-block link policy | check/exclusion/parity | owner block conflict | APPT-031 | block conflict/warning | conversation_only; D1/D2/D3/Dev2 as gated |
| CAP-08 | Cancellation releases active allocation | D1 active predicate; D2 cancellation transition/result; D3 atomic cancellation and allocation release | transition/result policy | partial predicate | cancel then rebook | APPT-019,044, MLT-005, MLH-006 | released-slot correctness | conversation_only; D1/D2/D3/Dev2 as gated |
| CAP-09 | Reschedule atomically moves capacity | D2 reschedule transition/result; D3 atomic old/new appointment and allocation transaction | transaction command/result policy | — | successful move | APPT-020, MLH-011 | partial-move incidents = 0 | conversation_only; D1/D2/D3/Dev2 as gated |
| CAP-10 | Released/cancelled allocations no longer block | D1 active predicate; D2 allocation-status policy; D3 persists release/cancellation transitions | allocation status policy | predicate inspection | released/cancelled reuse | APPT-044 | stale-block incidents | conversation_only; D1/D2/D3/Dev2 as gated |
| SCH-01 | Business schedule is default | D1 nullable resource scope; D2 pure default/inheritance rule; D3 tenant-scoped schedule query | default lookup | schedule columns/indexes | inherited default | APPT-039,047 | schedule disagreement | conversation_only; D1/D2/D3/Dev2 as gated |
| SCH-02 | Staff schedule replaces business schedule for that weekday | D2 pure replacement-not-merge rule; D3 repository resolves resource rows before business defaults | replacement matrix | scoped unique indexes | staff override | APPT-006,040 | override errors | missing; D1/D2/D3/Dev2 as gated |
| SCH-03 | Split shifts are supported | D1 uniqueness permits split shifts; D2 pure slot generation; D3 schedule query loads all shifts | split-shift generation | schedule uniqueness/check | two shifts same day | APPT-006 | gap bookings = 0 | conversation_only; D1/D2/D3/Dev2 as gated |
| SCH-04 | Breaks remove availability | D2 selects one pure break representation/rule; D3 reads persisted representation for availability | break window subtraction | depends on approved representation | break conflict | APPT-007 | break-time bookings = 0 | conversation_only; D1/D2/D3/Dev2 as gated |
| SCH-05 | Business holidays close availability | D1 exception schema/checks; D2 pure holiday precedence; D3 tenant-scoped exception query and confirmation revalidation | holiday override | exception checks/indexes | business closed exception | APPT-008,009,039 | holiday booking incidents | conversation_only; D1/D2/D3/Dev2 as gated |
| SCH-06 | Staff leave closes only that resource | D1 resource exception scope; D2 pure leave precedence; D3 resource exception query and confirmation revalidation | resource leave | scoped exception index | resource leave | APPT-009,029 | leave booking incidents | conversation_only; D1/D2/D3/Dev2 as gated |
| SCH-07 | Modified hours replace regular hours | D1 modified-hours consistency; D2 pure replacement rule; D3 exception query and confirmation revalidation | modified-hours cases | consistency check | modified exception | APPT-008 | modified-hours errors | conversation_only; D1/D2/D3/Dev2 as gated |
| SCH-08 | Exception takes precedence over regular schedule | D2 pure exception precedence; D3 repository supplies applicable business/resource exceptions | precedence table | — | precedence query | APPT-008,009 | exception misses | conversation_only; D1/D2/D3/Dev2 as gated |
| SCH-09 | Effective interval fits one contiguous shift | D2 pure contiguous-shift containment; D3 confirmation loads schedule and revalidates effective window | boundaries/gap/buffers | — | reject gap-spanning window | APPT-006,035,045,046 | cross-gap incidents | conversation_only; D1/D2/D3/Dev2 as gated |
| SCH-10 | Schedule interpretation uses business timezone | D2 pure timezone conversion; D3 loads business timezone and schedule data | local-day conversion | timestamptz/time parity | Kolkata conversion | APPT-024-026,039 | wrong local-time incidents | conversation_only; D1/D2/D3/Dev2 as gated |
| SCH-11 | Owner block and schedule exceptions do not silently cancel bookings | D2 pure conflict-warning contract; D3 owner transaction preserves appointments and returns conflicts | conflict warning policy | — | preserve existing booking | APPT-031 | warnings resolved by owner | missing; D1/D2/D3/Dev2 as gated |
| PA-01 | Create produces PendingAction proposal only | D2 strict appointment proposal envelope; D3 creates PendingAction only and writes no appointment/allocation | strict create payload | payload parity/backfill if needed | proposal has no appointment/allocation | APPT-002,007,011,016,017,022,025,036; MLT-001,009; MLH-002,009; MLS-002,004; MED-013 | proposal abandonment | partial; D1/D2/D3/Dev2 as gated |
| PA-02 | Proposal creates no temporary hold | D2 no-hold contract; D3 proposal transaction writes no capacity rows | proposal facts omit hold semantics | no hold backfill | no allocation after proposal | APPT-016-018,048 conflict with decision | race at confirmation | conversation_only; D1/D2/D3/Dev2 as gated |
| PA-03 | Mutation requires explicit confirmation | D2 confirmation snapshot/transition contract; D3 confirmation transaction performs mutation only after confirmation | confirmation transition | — | no commit pre-confirmation | APPT-001,012,017,027,041,042,045,046,050 | premature mutations = 0 | partial; D1/D2/D3/Dev2 as gated |
| PA-04 | Proposal expires at exact boundary | Generic lifecycle plus D2 appointment expiry contract; D3 enforces expiry during reads/confirmation | before/equal/after expiry | — | exact expiry rejection | APPT-018; AUTH-022 | expired confirmations | foundation; D1/D2/D3/Dev2 as gated |
| PA-05 | Equivalent retry is idempotent | Generic digest plus D2 operation-equivalence contract; D3 idempotent repository/transaction behavior | canonical equivalence | parity of canonicalizer | duplicate create retry | AUTH-020 analogous | duplicate appointments = 0 | foundation; D1/D2/D3/Dev2 as gated |
| PA-06 | Same key with different facts conflicts | Generic digest plus D2 conflict contract; D3 rejects conflicting persisted retry | digest mismatch | — | conflicting retry | AUTH-020 analogous | conflict recovery | foundation; D1/D2/D3/Dev2 as gated |
| PA-07 | Stale expected version is rejected | Generic optimistic-version contract plus D2 target-version policy; D3 conditional database update | stale command | — | stale version | AUTH-021 | stale retry frequency | foundation; D1/D2/D3/Dev2 as gated |
| PA-08 | Slot may be lost between proposal and confirmation safely | D2 resource-unavailable retry contract; D3 confirmation transaction revalidates and handles exclusion loser | resource-unavailable mapping | — | hot-slot race | APPT-016 | retry/alternative acceptance | conversation_only; D1/D2/D3/Dev2 as gated |
| PA-09 | No success response before database commit | D2 pre-commit/committed result types; D3 transaction runner rolls back state; integration adapter emits success only post-commit | typed pre/post commit outcomes | — | rollback cannot report success | APPT-037; AUTH-013 | false success = 0 | partial; D1/D2/D3/Dev2 as gated |
| PA-10 | Cancellation is proposal then explicit confirmation | D2 cancel operation envelope/snapshot; D3 cancellation proposal and confirmation transaction | cancel snapshot/transition | AppointmentCommit schema | cancel proposal/commit | APPT-019, MLT-005, MLH-006 currently broad | cancellation correction | partial; D1/D2/D3/Dev2 as gated |
| PA-11 | Reschedule is proposal then explicit confirmation | D2 reschedule operation envelope/old-new snapshot; D3 reschedule proposal and confirmation transaction | old/new snapshot | AppointmentCommit schema | reschedule proposal/commit | APPT-020,021,031; MLH-011 currently broad | reschedule correction | partial; D1/D2/D3/Dev2 as gated |
| PA-12 | Failed reschedule preserves old appointment and allocation | D2 retryable failure contract; D3 transaction rollback preserves old appointment/allocation | failure result | — | exclusion/rollback injection | APPT-021 | lost-old-booking = 0 | conversation_only; D1/D2/D3/Dev2 as gated |
| PA-13 | Create commit links PendingAction to Appointment exactly | D1 create linkage; D2 create-to-Appointment mapping; D3 writes and completes exact linked Appointment | entity mapping | unique/FK/parity | exact create linkage | AUTH-013,015 | orphan/link mismatch = 0 | foundation; D1/D2/D3/Dev2 as gated |
| PA-14 | Cancel/reschedule commit links PendingAction to AppointmentCommit | D1 AppointmentCommit linkage; D2 cancel/reschedule mapping; D3 writes and completes exact linked AppointmentCommit | operation mapping | unique/FKs/parity | exact mutation linkage | none | orphan/link mismatch = 0 | missing; D1/D2/D3/Dev2 as gated |
| SEC-01 | Service/resource lookup is tenant-scoped | D1 tenant-integrity schema; D2 pure ownership policy; D3 tenant-scoped service/resource/eligibility queries and commit revalidation | tenant mismatch policy | FKs/indexes | cross-tenant lookup/link | APPT-033; AUTH-006,008 | leakage = 0 | partial; D1/D2/D3/Dev2 as gated |
| SEC-02 | Customer reads only own appointment | D2 customer ownership policy; D3 tenant/caller-scoped appointment read | actor ownership | — | own read succeeds | AUTH-001,028 | denied-own incidents | partial; D1/D2/D3/Dev2 as gated |
| SEC-03 | Other caller cannot read/mutate appointment | D2 denial/not-found policy; D3 tenant/caller-scoped read and mutation filters | actor denial | — | guessed ID hidden | APPT-032; AUTH-002,007,019 | PII leaks = 0 | partial; D1/D2/D3/Dev2 as gated |
| SEC-04 | Active owner/manager may act within tenant | D2 role policy; D3 checks active same-tenant membership before database operation | role/membership matrix | — | active membership allowed | APPT-040; AUTH-003,004 | owner access failures | foundation; D1/D2/D3/Dev2 as gated |
| SEC-05 | Inactive manager is denied | D2 inactive-membership denial policy; D3 rechecks membership at execution time | inactive membership | — | inactive manager denied | AUTH-005,025,030 | unauthorized mutations = 0 | foundation; D1/D2/D3/Dev2 as gated |
| SEC-06 | Forged role is ignored; verified context is injected | D2 trusted-context policy; D3 ignores model role and checks injected identity/membership | forged role | — | membership defeats forged context | AUTH-009,010,027 | spoofing incidents = 0 | foundation; D1/D2/D3/Dev2 as gated |
| SEC-07 | Wrong committed entity type/operation is rejected | D2 entity/operation mapping; D3 validates persisted linked entity before completion | mapping table | AppointmentCommit parity | wrong mapping rejected | AUTH-015 | linkage errors = 0 | foundation; D1/D2/D3/Dev2 as gated |
| SEC-08 | Responses/errors do not leak appointment PII | D2 redacted result/error contract; D3 scoped queries and safe not-found behavior | redaction | — | not-found equivalence | APPT-032; AUTH-019,023 | PII incidents = 0 | conversation_only; D1/D2/D3/Dev2 as gated |
| SEC-09 | Internal commit operations are never public/LLM-callable | D2 internal/public operation classification; future dispatcher integration enforces allowlist | allowlist exclusion | — | forged operation rejected | AUTH-013,014,016,027 | internal-tool attempts = 0 | foundation; D2/integration |
| TIME-01 | Asia/Kolkata local time converts to correct UTC instant | D2 pure Asia/Kolkata conversion contract; D3 loads business timezone and persists UTC instants | fixed conversion | timestamptz parity | round-trip | APPT-024-026,045,046 | wrong-time incidents | conversation_only; D1/D2/D3/Dev2 as gated |
| TIME-02 | Results are independent of server `TZ` | D2 explicit ZoneInfo/server-independent contract; D3 database session timezone must not alter results | run under multiple TZ envs | — | DB session timezone variants | none | deployment timezone incidents | missing; D1/D2/D3/Dev2 as gated |
| TIME-03 | Ambiguous/nonexistent DST wall times are rejected | D2 pure DST fold/gap rejection policy; D3 persists only resolved aware instants | fold/gap tests in DST zone | — | timestamptz storage after resolution | none | DST incident audit | missing; D1/D2/D3/Dev2 as gated |
| TIME-04 | Past start time is rejected | D2 pure past-time policy; D3 confirmation rechecks database operation against current time | exact-now/past/future | — | commit rechecks current time | APPT-038 | past booking attempts | conversation_only; D1/D2/D3/Dev2 as gated |
| TIME-05 | Booking horizon is bounded | D2 pure booking-horizon policy/config contract; D3 loads config and enforces at availability/confirmation | exact horizon boundary | config parity if persisted | beyond-horizon rejection | APPT-041 hints recurring only | far-future errors | missing; D1/D2/D3/Dev2 as gated |
| TIME-06 | Proposal expiry equality is expired | Generic exact-expiry policy plus D2 appointment contract; D3 conditional update at boundary | `expires_at == now` | — | boundary race | APPT-018; AUTH-022 | expired-success count = 0 | foundation; D1/D2/D3/Dev2 as gated |

**Requirement count: 59.** Existing eval mappings are evidence of language/tool intent coverage only. They do not establish the migration, constraint, transaction, authorization, or concurrency columns.

## Future PostgreSQL verification blueprint

Implement these only after D1/D2 approval and stable names. Each race uses independent `AsyncSession`/connections on the existing session loop. Ordinary tests use per-test cleanup; migration-revision tests must not run concurrently with ordinary tests.

### A. Migration `0003 → 0004` (9 tests)

| Future test | Fixtures/data | Sessions/synchronization | Expected rows/state | Exact invariant proved |
|---|---|---|---|---|
| `test_0004_upgrades_empty_0003_database` | database at `0003`, no app rows | Alembic subprocess, single migration connection | all D1 tables/columns at `0004` | clean upgrade and single head |
| `test_0004_upgrades_valid_populated_database` | two tenants, valid services/resources/appointments | migrate after committed seed | IDs preserved; snapshots and one allocation/appointment | populated upgrade safety |
| `test_0004_rejects_legacy_null_service` | `0003` appointment with null service | migration connection | migration fails; revision remains `0003` | unresolved authoritative service cannot be guessed |
| `test_0004_rejects_cross_tenant_service_or_resource` | appointment business A linked to service/resource B | migration connection | migration fails without partial D1 objects | tenant graph validation |
| `test_0004_rejects_existing_active_overlap` | two same-resource overlapping legacy rows | migration connection | migration fails before exclusion activation | no silent data rewrite/cancellation |
| `test_0004_backfills_snapshots_and_effective_windows` | service duration 30, buffers 10/5; appointment 10:00 | migration connection | effective `[09:50,10:35)`, names/duration/buffers copied | deterministic immutable backfill |
| `test_0004_btree_gist_available_and_idempotent` | extension absent then present | privileged test migration connection | extension exists once | required operator classes available |
| `test_0004_downgrade_and_reupgrade_preserve_extension_policy` | fully populated `0004` | Alembic `0004→0003→0004` | D1 tables removed/recreated; extension retained | reversible schema and explicit extension retention |
| `test_0004_orm_alembic_and_live_catalog_parity` | empty migrated DB | recorder plus live `alembic check` | no drift | ORM/Alembic parity including PG constructs |

### B. Exclusion constraint and allocation kinds (14 tests)

Common seed: business A, resource R1, active allocation `[10:00,11:00)`; service duration/buffers chosen per case. Each candidate is inserted in a fresh transaction. Rejections assert SQLSTATE `23P01` and exact constraint `ex_resource_allocations_active_overlap`; rollback leaves only the seed row.

| Test(s) | Candidate / expected | Rows and allocation state | Invariant |
|---|---|---|---|
| `test_allocation_identical_overlap_rejected` | `[10:00,11:00)` / loser | one active seed | identical overlap |
| `test_allocation_new_starts_inside_rejected` | `[10:30,11:30)` / loser | one active seed | left overlap |
| `test_allocation_new_ends_inside_rejected` | `[09:30,10:30)` / loser | one active seed | right overlap |
| `test_allocation_new_contains_existing_rejected` | `[09:00,12:00)` / loser | one active seed | candidate contains |
| `test_allocation_existing_contains_new_rejected` | `[10:15,10:45)` / loser | one active seed | existing contains |
| `test_allocation_exact_adjacency_allowed` | `[11:00,12:00)` / winner | two active rows | half-open `[)` |
| `test_allocation_buffer_only_overlap_rejected` | bare adjacent, effective starts 10:55 / loser | one active seed | effective range, not bare time |
| `test_different_resources_same_time_allowed` | R2 `[10:00,11:00)` / winner | one active each R1/R2 | resource-scoped capacity |
| `test_released_allocation_does_not_block` | seed released / winner | released seed + active candidate | partial predicate |
| `test_cancelled_allocation_does_not_block` | seed cancelled / winner | cancelled seed + active candidate | partial predicate |
| `test_owner_block_conflicts_with_booking` | owner block seed, appointment candidate / loser | one active owner block | unified ledger |
| `test_walk_in_conflicts_with_booking` | walk-in seed / loser | one active walk-in allocation | unified ledger |
| `test_manual_appointment_conflicts_with_booking` | manual seed / loser | manual appointment + one allocation only | unified ledger |
| `test_active_appointment_has_one_active_allocation` | attempt second active link | loser on partial unique index | one active allocation per appointment |

### C. Concurrent booking (2 tests)

`test_concurrent_confirm_same_resource_exactly_one_wins` seeds two distinct `awaiting_confirmation` appointment PendingActions for the same selected R1/effective range. Session A and B begin independently. A flushes appointment+allocation and signals an `asyncio.Event`; B attempts the same insert and is confirmed blocked using `pg_stat_activity`/`pg_locks` (or an approved repository test hook), then A commits. Exactly A or B wins—not a preselected caller: one `Appointment`, one active `ResourceAllocation`, one successful create commit; winner PendingAction `confirmed` linked to that Appointment. Loser transaction rolls back all appointment/allocation/link rows, then records retryable `resource_unavailable` and returns PendingAction to `awaiting_confirmation`. No process-local lock or serialization is allowed.

`test_waiting_confirmation_wins_when_first_transaction_rolls_back` uses the same barrier, but A rolls back after flush. B must commit; final database has one appointment/allocation owned by B and none from A. This distinguishes genuine exclusion locking from unconditional rejection.

### D. Cancellation (6 tests)

| Test | Required sessions/data | Winner/loser and final state | Invariant |
|---|---|---|---|
| `test_confirmed_cancellation_releases_allocation` | own confirmed appointment + cancel proposal | one transaction | appointment cancelled; allocation cancelled/released; one AppointmentCommit; PA confirmed | atomic release and linkage |
| `test_duplicate_cancellation_is_idempotent` | replay same idempotency key | sequential independent sessions | same commit/result; no duplicate AppointmentCommit | unique pending-action/idempotency |
| `test_cancellation_stale_version_rejected` | target version advanced | one session | no rows changed; PA safely fails | optimistic concurrency |
| `test_cancellation_wrong_caller_denied` | other phone | one session | no write; no PII | ownership authorization |
| `test_cancellation_cross_tenant_hidden` | business B actor, A appointment | one session | not found/denied; no write | tenant scope |
| `test_completed_or_no_show_cancellation_rejected` | terminal targets | parameterized | appointment/allocation unchanged; no commit row | terminal-state policy |

### E. Rescheduling (6 tests)

| Test | Sessions/synchronization | Expected rows/state | Invariant |
|---|---|---|---|
| `test_reschedule_atomically_moves_allocation` | one transaction | same or replacement appointment per approved D2 contract; old allocation released, one new active; one AppointmentCommit; PA confirmed | atomic move |
| `test_reschedule_conflict_preserves_old_allocation` | destination occupied | exclusion loser | old appointment/allocation unchanged; no new active row; retryable PA | rollback safety |
| `test_reschedule_failure_after_update_rolls_back_everything` | injected repository failure after old release | one transaction/savepoint | exact pre-state restored; no commit row | all-or-nothing |
| `test_reschedule_to_different_staff` | R1 old, eligible R2 new | one transaction | R1 released, R2 active | concrete new resource and eligibility |
| `test_duplicate_reschedule_retry_is_idempotent` | same key/facts replay | sequential sessions | one move and one AppointmentCommit | idempotency |
| `test_concurrent_reschedules_exactly_one_wins` | two sessions, barrier before conditional update | one winner, stale loser | one final active allocation; no orphan | target version + allocation exclusion |

### F. Owner/manual operations (6 tests)

| Test | Data/sessions | Expected result | Invariant |
|---|---|---|---|
| `test_owner_manual_booking_obeys_exclusion` | active owner membership; occupied R1 | conflict; no partial appointment | owner path cannot bypass capacity |
| `test_walk_in_obeys_exclusion` | occupied R1 | conflict; no partial appointment | walk-in path cannot bypass capacity |
| `test_owner_block_obeys_exclusion` | occupied R1 | conflict; no block row | block shares ledger |
| `test_unblock_releases_capacity` | active owner block | block released then booking succeeds | release predicate |
| `test_customer_cannot_call_owner_capacity_operations` | customer actor | authorization denial, zero rows | owner boundary |
| `test_inactive_manager_cannot_mutate_schedule_or_capacity` | inactive membership | denial, zero rows | live membership check |

### G. Proposal and commit boundary (3 PostgreSQL tests + 1 adapter contract)

| Test | Required fixtures/data and boundary | Expected rows/state | Invariant |
|---|---|---|---|
| `test_create_proposal_writes_no_capacity_or_appointment` | Valid business, active service/resource, eligibility and schedule; D3 creates and commits one proposal | one PendingAction in `awaiting_confirmation`; zero Appointment, ResourceAllocation and AppointmentCommit rows | proposal is not a hold and reserves no capacity |
| `test_proposal_does_not_block_another_proposal` | Two independent callers commit proposals for identical selected resource/time | both PendingActions coexist in `awaiting_confirmation`; zero allocations and appointments | no hidden hold or proposal exclusion |
| `test_confirmation_rollback_cannot_leave_success_state` | Begin confirmation, insert appointment/allocation, inject failure before outer commit and roll back | zero Appointment, ResourceAllocation and AppointmentCommit rows; PendingAction is not `confirmed`; no committed-success result | database rollback cannot leave success state or partial capacity |
| `test_caller_facing_success_emitted_only_after_outer_commit` | Non-PostgreSQL transaction-runner/tool-adapter integration contract; pause before commit and inject commit failure | pre-commit typed value is not caller-facing success; only a completed outer commit can emit success | application response boundary; deliberately not counted as a PostgreSQL contract |

The first three tests are real PostgreSQL contracts. The fourth is an integration/tool-adapter contract because PostgreSQL can prove rollback state but cannot observe what the application says to a caller.

### H. Historical completed/no-show allocations (2 tests)

| Test | Setup | Expected rows/state | Invariant |
|---|---|---|---|
| `test_completed_appointment_keeps_historical_allocation_active` | Confirmed appointment with active allocation; D3 marks appointment completed | Appointment is `completed`; ResourceAllocation remains `active`; effective interval is unchanged | appointment business status does not rewrite historical capacity evidence |
| `test_no_show_appointment_keeps_historical_allocation_active` | Confirmed appointment with active allocation; D3 marks appointment no-show | Appointment is `no_show`; ResourceAllocation remains `active`; effective interval is unchanged | no-show preserves historical occupied interval |

These intervals are historical and therefore do not block future clock time, but their allocation rows remain accurate audit history. Completed/no-show must not imply allocation release.

**Future PostgreSQL test count: 48** = migration (9) + exclusion/allocation (14) + concurrent booking (2) + cancellation (6) + rescheduling (6) + owner/manual operations (6) + proposal/commit boundary (3) + completed/no-show historical allocations (2). One additional non-PostgreSQL transaction-runner/tool-adapter contract proves that caller-facing success is emitted only after the outer commit. Pure schedule, time, payload, and authorization tests remain separate and are intentionally not counted as PostgreSQL contracts until interfaces stabilize.

## Migration `0004` verification requirements

Dev2's post-D1 implementation review must verify all of the following without rewriting the migration:

1. **Extension:** `pg_extension` contains `btree_gist`; repeated upgrade is safe; deployment privilege is documented. Downgrade retains the shared extension rather than dropping it.
2. **Exclusion name:** catalog and ORM use exactly `ex_resource_allocations_active_overlap` (or an AI-cofounder-approved replacement fixed before tests).
3. **Columns/operators:** `business_id WITH =`, `resource_id WITH =`, and `tstzrange(effective_start_at,effective_end_at,'[)') WITH &&`, using GiST.
4. **Bounds:** inspect `pg_get_constraintdef`; exact `[)` allows end/start adjacency.
5. **Predicate:** exact `status = 'active'`; released and cancelled rows do not block. Test every allocation type through the same predicate.
6. **Partial uniqueness:** verify `uq_allocation_active_appointment` predicate and `uq_allocation_idempotency`; verify business/resource and exception partial unique indexes with `pg_indexes`.
7. **Schedule checks:** day-of-week range remains; `close_time > open_time`; exception closed/modified-hours consistency; split shifts remain representable.
8. **Eligibility:** unique `(service_id,resource_id)`, business/service and business/resource lookup indexes, active flag, and tenant-consistent linkage. Independent FKs are insufficient unless migration validation plus service checks make the invariant explicit.
9. **Allocation/appointment link:** owner block requires null `appointment_id`; every non-block active allocation requires an appointment; at most one active allocation per appointment; business/resource/appointment ownership is consistent.
10. **AppointmentCommit:** one unique `pending_action_id`; operation only cancel/reschedule; exact same-tenant appointment and PendingAction; create commits map to `Appointment`, cancel/reschedule commits map to `AppointmentCommit` through PendingAction completion policy.
11. **Backfill:** valid legacy rows get immutable service/resource/duration/buffer/effective snapshots and exactly one allocation; null service, cross-tenant links, and overlapping active records fail clearly without partial upgrade.
12. **Downgrade:** drops D1 tables/indexes/checks/FKs in reverse dependency order, restores `0003` uniqueness/checks, removes added columns, preserves pre-existing data that `0003` can represent, and retains `btree_gist`.
13. **Parity:** extend the migration recorder for `0004`, explicit check/FK creation, PostgreSQL `ExcludeConstraint`, partial-index predicates and extension SQL; compare live catalog and ORM, run `alembic check`, offline SQL smoke, one-head check, downgrade and re-upgrade.

## Evaluation coverage and contract conflicts

The machine-readable audit is `evals/reports/appointment-coverage-map.pre-d.json`. APPT-001–050 are all synthetic, domain-unreviewed and pilot-untested. They provide useful conversational scenarios but do not prove database effects stated in `expected_database_effect`.

Appointment-related cross-domain IDs mapped in the audit include:

- Authorization/security: `AUTH-002`, `AUTH-006`–`AUTH-010`, `AUTH-012`–`AUTH-016`, `AUTH-019`, `AUTH-021`, `AUTH-022`, `AUTH-025`, `AUTH-027`, `AUTH-028`, `AUTH-030`.
- Tamil/Tanglish: `MLT-001`, `MLT-004`, `MLT-005`, `MLT-009`, `MLT-011`, `MLT-016`, `MLT-019`.
- Hindi/Hinglish: `MLH-002`, `MLH-006`, `MLH-007`, `MLH-009`, `MLH-011`.
- Other South Indian: `MLS-002`, `MLS-003`, `MLS-004`.
- Medical/clinical/cosmetic boundary: `MED-001`–`MED-016`, especially routine booking `MED-013`; all require domain review, and safety cases require clinical review before use as approval evidence.

Contract conflicts to resolve only after D1/D2 approval:

1. `create_pending_appointment` says “proposal or temporary hold”; founder policy is proposal only, no capacity allocation.
2. The model currently supplies `end_at`; deterministic code must derive it from service duration.
3. Proposal permits null resource; the persisted proposal must contain one concrete eligible selected resource. “Any” is an input preference, not stored ambiguity.
4. Named staff must not silently fall back; alternatives require explicit caller selection and a revised proposal.
5. `cancel_pending_appointment.action_id` is ambiguous between PendingAction and Appointment; cancellation needs explicit target appointment identity plus proposal action identity.
6. `reschedule_appointment.action_id` is similarly ambiguous and currently permits direct `commit`; proposal and confirmation responsibilities must be explicit.
7. Create completion maps to `Appointment`; cancel/reschedule completion maps to `AppointmentCommit`.
8. Owner block/manual/walk-in operations must have real deterministic owner services before any public exposure; existing APPT-022/031 tool expectations do not prove those effects.
9. Physical consumable inventory is outside appointment Phase D; multi-service/resource capacity must not pull in Phase C inventory behavior.
10. APPT-017/018/048 and the tool write-policy definition encode temporary holds and must be revised after the stable contract is approved, not during this pre-D audit.

## D1/D2 interface questions for Dev3 and the AI cofounder

1. Are effective interval snapshots mandatory non-null for all post-`0004` appointments, including legacy rows?
2. How does the schema enforce same-tenant `ServiceResourceEligibility`, schedule-resource, exception-resource, appointment-resource/service, allocation-appointment, and AppointmentCommit links—composite FKs, migration validation plus service checks, or both?
3. Are breaks represented as split schedules, schedule exceptions, or owner-block allocations? One authoritative representation is required.
4. Does a resource-specific schedule replace all business shifts for that weekday? The verification plan assumes replacement.
5. Are overnight shifts unsupported by `close_time > open_time`, and if so is that an explicit product limitation?
6. Can an exception open a normally closed day? Can business and resource exceptions coexist, and which wins?
7. What exact booking horizon and DST ambiguous/nonexistent-time policy should pure tests enforce?
8. Which appointment states may be cancelled/rescheduled, and is duplicate cancellation an idempotent success or invalid state?
9. Does rescheduling mutate one Appointment or cancel old/create replacement? The founder requirement only fixes atomic capacity movement; D2 must fix observable IDs and snapshots.
10. What exact repository error maps SQLSTATE `23P01`/constraint name to `resource_unavailable`?
11. How is a loser PendingAction restored to `awaiting_confirmation` after a transaction-level exclusion failure without leaving `committing` or losing the failure code?
12. What stable operation/idempotency keys cover create, cancel, reschedule, manual, walk-in, block and unblock?
13. Will owner/manual/walk-in/block services be deferred beyond D2, and therefore remain unexposed until their deterministic effects exist?
14. Does `AppointmentCommit.after_snapshot` for cancellation represent a cancelled appointment and released allocation, and how is exact PendingAction entity mapping exposed to generic completion?
15. May Dev2's controlled race tests inspect `pg_locks`/`pg_stat_activity` under CI credentials, or will Dev3 provide a test-only synchronization hook?

## Security, review and pilot gaps

- Independent scalar FKs can permit cross-tenant graphs; D1/D2 must define a database-enforced or explicitly validated tenant-integrity strategy.
- Verified role is application-injected, but active membership must still gate owner/manager mutations at execution time. Shared customer phones remain an identity-recovery risk for cancellation/rescheduling.
- Appointment reason, phone and schedule data are PII; no retention/redaction/export policy is yet approved. Not-found responses must not reveal another caller's booking.
- Internal commit operations are structurally non-public but no production dispatcher exists. Forged internal-operation eval and dispatcher contract tests are missing.
- Owner operations need immutable actor/before/after audit evidence and cannot be simulated by appointment public tools.
- All language cases are synthetic. Tamil/Tanglish, Hindi/Hinglish, Telugu, Kannada and Malayalam require native review; current South Indian counts are too small for quality claims.
- Clinic/diagnostic use requires domain and clinical review. `MED-001`–`MED-016` are unreviewed; cosmetic-service escalation cases are not clearly separated from ordinary salon booking. No diagnosis, medication advice, or treatment claims may be inferred from scheduling capability.
- Pilot observations remain untested: zero double-bookings/false successes/PII leaks, no silent substitution, owner correction rate, slot-loss recovery, cancellation release, reschedule rollback, local-time accuracy and manual/walk-in collision handling.

## Exact handoff after D1/D2

After Dev3 submits D1/D2 and the AI cofounder approves stable table/constraint names, payload discriminators, timezone/schedule rules, status policy and error interfaces, Dev2 will:

1. Review migration `0004` against the catalog requirements above.
2. Extend parity verification for extension, exclusion and partial predicates.
3. Implement the 48 real-PostgreSQL contracts in a separate authorized change, preserving independent concurrent sessions, plus the separate post-commit caller-success adapter contract.
4. Propose—without silently changing—the eval/tool-contract corrections and missing cases listed in the JSON audit.
5. Run the full static, non-PostgreSQL, PostgreSQL, migration downgrade/re-upgrade, eval and Chennai gates.

Repository/service transaction tests wait until Dev3 is separately authorized for those interfaces; this plan does not treat D1/D2 as permission to begin them.
