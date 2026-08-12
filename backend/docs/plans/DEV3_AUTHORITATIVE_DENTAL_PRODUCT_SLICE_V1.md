# FONELY CEO — DEV3 DELIVERY ASSIGNMENT

## Milestone:
Authoritative Dental Product Slice v1
WhatsApp Synthetic Staging E2E + Stable Voice Application Contract

## Priority:
P0 — immediate product delivery

## Owner:
Dev3 — sole non-voice developer

## Parallel partner:
Dev4 — sole voice developer

## Starting main:
Exact integrated main SHA:
cc3aa65c6bc2529868a9f8ac10812ebaca467554

## Integrated migration head:
0015

## Integrated evidence:
- Pre-integration CI fully green
- Post-integration CI run 31449054019 fully green
- All 21 named steps passed
- Zero required steps skipped

## EXECUTION MODE

Work autonomously from start to terminal delivery.

Do not:

- send routine progress narration,
- ask whether to continue,
- create parallel implementation branches,
- broaden into voice internals,
- revive inventory/order scope,
- merge retired branches wholesale,
- start Exotel migration 0016,
- deploy publicly,
- use real customer data,
- claim staging/pilot/production readiness without corresponding evidence.

Return only when you have either:

1. one clean, pushed, fully gated exact CANDIDATE SHA; or
2. one genuine blocker that requires founder credentials, public infrastructure, migration authorization, or a product-policy decision.

Commit durable WIP at every natural pause so session/worktree loss cannot destroy work.

Do not merge or push to main without CEO authorization for the exact candidate SHA.

## MISSION

Deliver the first complete, trustworthy Fonely dental product journey through the real FastAPI/PostgreSQL application:

synthetic signed WhatsApp message
→ durable inbound event
→ ordered inbound worker
→ Tamil/Tanglish conversation
→ trusted clinic context
→ authoritative availability
→ explicit offered-slot identity
→ exact readback
→ explicit patient confirmation
→ PostgreSQL appointment + allocation
→ PendingAction completion
→ notification manifest
→ patient and owner outbox events
→ fake Meta delivery adapter
→ delivery callback reconciliation
→ restart/replay proof

At the same time, expose the smallest stable typed application contract Dev4 needs to build voice in parallel.

The voice runtime must be able to ask the authoritative application:

- what clinic/business context is trusted,
- what facts are still required,
- which slots are authoritatively available,
- which offered slot the patient selected,
- whether a proposal was created,
- whether explicit confirmation is required,
- whether the operation committed,
- what immutable receipt may be spoken,
- whether escalation is required.

PostgreSQL and application services remain authoritative.

The model, voice runtime, WhatsApp adapter, and callers must never directly mutate authoritative booking state.

## CUSTOMER OUTCOME

A synthetic Tamil/Tanglish dental patient must be able to:

1. ask for an appointment naturally,
2. receive clarification only for missing or ambiguous facts,
3. ask unrelated clinic questions during booking,
4. correct service, doctor, date, or time,
5. receive only real authoritative available choices,
6. select an offered choice without the model inventing a slot,
7. hear/read the exact service, doctor/resource, date, time, clinic, and price facts,
8. explicitly confirm,
9. receive success only after PostgreSQL commit,
10. receive a replay-safe committed result after retries or restart,
11. cancel or reschedule through the same confirmation discipline,
12. receive truthful notification-evidence status,
13. escalate safely when the request is medical, urgent, unsupported, or asks for a human.

The clinic owner must receive durable notification evidence for every committed operation.

## PRODUCT BOUNDARY

### IN SCOPE

- WhatsApp text booking, cancellation, and rescheduling
- Tamil, Tanglish, and Indian English text behavior
- Trusted clinic/business context
- Authoritative availability
- Durable offered-slot selection
- Exact proposal/readback
- Explicit confirmation
- Appointment commit receipt
- Notification manifest and outbox
- Fake-provider delivery and callback reconciliation
- Restart and duplicate handling
- Application ports needed by Dev4
- Synthetic single-clinic staging composition
- Operator-visible failure evidence
- Main-based integration candidate

### OUT OF SCOPE

- STT implementation
- TTS implementation
- voice prompt/dialogue implementation
- browser voice UI
- Exotel edge implementation
- Exotel migration 0016
- public TLS/DNS
- real Meta credentials
- real patient traffic
- inventory/order workflows
- additional industries
- new languages
- microservices/Kafka/Celery/Kubernetes
- generic workflow engines
- dashboards unrelated to operating this slice
- tenant-unsafe retention redesign
- wholesale merge of retired branches

## NON-NEGOTIABLE ARCHITECTURE

1. PostgreSQL is authoritative.
2. Every tenant-owned read/write is scoped by trusted business_id.
3. Trusted tenant and actor context are injected by the application.
4. Never accept model-generated tenant, actor, service, resource, slot, price, role, or committed state as authoritative.
5. Business mutation occurs only through typed application commands.
6. Every external retryable mutation has semantic idempotency backed by PostgreSQL uniqueness.
7. PendingAction remains the proposal/confirmation boundary.
8. Caller owns the outer transaction.
9. Internal services do not commit.
10. No success response before outer commit.
11. Notification manifest/outbox creation is atomic with appointment mutation.
12. Provider calls occur outside database transactions and authoritative locks.
13. Immutable committed facts drive replay and customer-visible receipt.
14. Mutable catalog/configuration changes cannot corrupt committed replay.
15. Multi-row locks use deterministic total ordering.
16. Concurrency evidence uses independent PostgreSQL sessions and real overlap.
17. Channel adapters remain thin and stateless.
18. Do not add platform abstractions not required by this exact product slice.

## WORKTREE AND GIT SAFETY

Before editing:

1. Verify remote main is exactly:
   cc3aa65c6bc2529868a9f8ac10812ebaca467554

2. Inspect:
   - branch,
   - status,
   - tracked and untracked files,
   - current migration head,
   - all visible worktrees,
   - relevant retired handoffs,
   - complete current-main production path.

3. Create a clean isolated worktree and branch:
   suggested branch:
   dev3/dental-whatsapp-staging-e2e

4. Base exactly on accepted main.

5. Do not modify:
   - Dev4 worktrees,
   - retired Dev1 worktree,
   - retired Dev2 worktree,
   - retired Dev5 worktree,
   - user-owned untracked files.

6. Retired branches are read-only evidence:
   - Dev2 staging WIP:
     /tmp/dev2-staging-runtime
     local WIP 503ea16895a4cf9e7ae21f3783a05852f3fd2833
   - Dev5 rejected availability candidate:
     3ddd030
   - Dev1 Exotel WIP/design:
     b35ab88
     f429251

Do not merge or rebase any of those branches wholesale.

If useful logic is needed, inspect it, rederive the smallest correct main-based implementation, and prove it independently.

## FIRST PHASE — COMPLETE TRACE BEFORE EDITING

Read every production line and relevant test for:

- app composition,
- WhatsApp webhook,
- inbound event repository,
- inbound worker,
- conversation persistence,
- ConversationService,
- safety classifier,
- fact resolution,
- AvailabilityService,
- appointment proposal/confirmation,
- cancellation,
- rescheduling,
- PendingAction,
- notification manifest,
- notification outbox,
- notification worker,
- WhatsApp sender,
- delivery callback reconciliation,
- business onboarding/configuration,
- clinic schedules/exceptions,
- operator evidence,
- staging topology and configuration,
- internal API response models.

Trace the complete path:

HTTP webhook
→ signature verification
→ tenant mapping
→ durable inbound event
→ worker claim
→ conversation loading
→ LLM/fact extraction
→ authoritative application command
→ transaction
→ response outbox
→ provider send
→ callback reconciliation
→ replay/restart

For every fact, identify internally whether it is:

- trusted current configuration,
- untrusted patient text,
- model interpretation,
- authoritative application result,
- immutable committed fact,
- provider delivery evidence.

Do not edit until that trace is understood.

## MILESTONE A — STABLE APPLICATION CONTRACT FOR CHANNELS

Create the smallest typed contract usable by WhatsApp today and Dev4 voice later.

Do not create a generic workflow framework.

Prefer typed dataclasses/Pydantic/domain result types and application facades over a large new abstraction hierarchy.

The contract must represent:

1. TrustedConversationContext
   - business_id
   - conversation/session ID
   - trusted actor/subject
   - clinic timezone
   - current local date/time
   - supported language/register
   - clinic identity

2. BookingDraft
   - operation
   - service identity
   - resource identity/preference
   - date/time intent
   - patient identity
   - collected facts
   - missing facts
   - rejected/corrected facts
   - revision/version

3. AvailabilityOffer
   - opaque offer ID
   - offer revision
   - business/conversation/service/resource bindings
   - absolute aware UTC start/end
   - clinic-local display facts
   - expiry
   - opaque selection token
   - deterministic ordering

4. SelectedSlot
   - must be a member of the active offer
   - offer ID/revision/token binding
   - no raw model-created slot
   - no selection from stale/tampered offer

5. AppointmentProposalResult
   - proposal/PendingAction identity
   - exact immutable facts for readback
   - expiry/version
   - explicit-confirmation requirement

6. AppointmentCommitReceipt
   - committed appointment ID/version
   - operation identity
   - exact service/resource snapshots
   - exact start/end/timezone
   - patient facts
   - price facts
   - notification evidence status
   - immutable replay identity

7. EscalationResult
   - medical/urgent/unsupported/human-help class
   - safe patient message
   - whether a real operator notification/handoff was created
   - never claim an alert/transfer that did not occur

The contract must not expose internal commit methods as public LLM tools.

## MILESTONE B — DURABLE OFFERED-SLOT SEAM

The current natural-language alternatives path is not sufficient if it relies on matching free-form time text without durable offer identity.

Implement the accepted useful principles from the retired availability work, but not its rejected branch wholesale:

- availability choices come only from AvailabilityService,
- persist one typed active offer aggregate,
- issue opaque tokens,
- bind tenant/conversation/service/resource/date/time/revision,
- validate selected token membership,
- distinguish acceptance, rejection, correction, ambiguity, and scope changes,
- canonical date/time handling,
- fail closed on malformed/orphan/cross-offer state,
- exact-slot recheck under the canonical resource-schedule lock,
- no proposal if the slot changed,
- critical persistence failures propagate,
- restart restores the active offer safely,
- timeout/rollback cannot leave process cache ahead of PostgreSQL.

Every proposal path—selected offer or direct exact-time request—must use the same authoritative lock and recheck boundary.

No migration unless genuinely required.

If current conversation JSON persistence can safely store the aggregate, prefer it.

If schema enforcement is truly required:

- stop before creating migration 0016,
- produce the exact need,
- preserve migration ordering because Exotel 0016 is reserved for a later separately authorized milestone.

## MILESTONE C — NATURAL-LANGUAGE PRODUCT BEHAVIOR

Use the existing conversation architecture; do not replace it with an unrestricted LLM agent.

Required behaviors:

- "I need a dental appointment" activates booking.
- Tamil/Tanglish equivalents activate booking.
- "Tomorrow" cannot become patient name.
- "Not 5, make it 6" rejects 5 and selects only an offered 6.
- "5 PM doesn't work" creates no proposal.
- Changing service clears incompatible resource/time facts.
- Changing doctor/resource clears incompatible time.
- Changing date clears old offered selection.
- Same-date restatement preserves valid offer provenance.
- Known fields are not asked repeatedly.
- The application chooses the required next field.
- LLM may phrase the question but may not choose authority.
- Readback uses immutable proposal facts.
- Confirmation must be explicit.
- Success comes only from AppointmentCommitReceipt.
- Medical questions cannot mutate booking state.
- If the patient asks a clinic question mid-booking, answer from trusted context and resume the booking safely.
- Human escalation must be real or worded as a request—not a false completed transfer.

Do not solve all of this with one giant prompt.

Hard safety and state guarantees remain deterministic.

## MILESTONE D — SYNTHETIC WHATSAPP STAGING E2E

Build one reproducible synthetic staging harness that exercises actual production composition.

It must not call service methods directly as a substitute for the real path.

Required composition:

- production FastAPI app,
- PostgreSQL at current migration head,
- inbound worker,
- notification worker,
- explicit fake Meta sender,
- synthetic clinic/business mappings,
- synthetic patient and owner phones,
- deterministic model/fact adapter where necessary,
- real application services,
- real repositories and transactions.

### MANDATORY HAPPY PATH

1. Seed one synthetic dental clinic:
   - business,
   - timezone,
   - services,
   - dentists/resources,
   - eligibility,
   - business/resource schedules,
   - owner BusinessUsers,
   - WhatsApp phone-number mapping.

2. Send a signed Meta-style webhook:
   - Tamil or Tanglish appointment request,
   - correct provider message envelope,
   - trusted phone_number_id.

3. Verify:
   - webhook authenticates,
   - event persists before 200,
   - worker claims in order,
   - conversation is loaded/created durably,
   - facts are collected,
   - authoritative options are returned.

4. Patient selects an opaque offered slot.

5. Patient receives exact proposal/readback.

6. Patient explicitly confirms.

7. Verify committed state:
   - one appointment,
   - one active allocation,
   - completed PendingAction,
   - one notification manifest,
   - patient + all deduplicated owner events,
   - response outbox,
   - no partial writes.

8. Run fake Meta delivery:
   - attempts recorded,
   - accepted outcome stored,
   - delivery callback reconciled monotonically.

9. Restart:
   - clear process-local caches,
   - reconstruct services/sessions,
   - replay duplicate provider message/confirmation,
   - return exact committed result,
   - no duplicate appointment/allocation/manifest/events.

### MANDATORY CUSTOMER VARIATIONS

- appointment booking,
- cancellation,
- rescheduling,
- different dentist,
- unavailable requested time,
- patient rejects first options,
- patient changes date/service/resource,
- duplicate inbound provider message,
- expired offer,
- stale slot occupied by competitor,
- clinic closure/exception,
- human-help request,
- medical question,
- malformed mapping,
- invalid signature,
- provider send failure,
- ambiguous provider acceptance,
- application restart.

### MANDATORY NEGATIVE INVARIANTS

- no tenant cross-read/write,
- no appointment before confirmation,
- no success before commit,
- no invented availability,
- no model-created authoritative IDs,
- no duplicate booking on retry,
- no notification evidence loss,
- no false "delivered" wording,
- no false "owner alerted" wording,
- no partial state after failure.

## MILESTONE E — STAGING OPERABILITY FROM CLEAN MAIN

Use the retired Dev2 worktree only as read-only evidence.

Create the safe topology from current main; do not merge its rejected history.

Minimum topology:

- PostgreSQL,
- migration service,
- API,
- inbound worker,
- notification worker.

Do not deploy the current retention worker until it is tenant-scoped and evidence-safe.

Required properties:

- one immutable buildable image,
- DB-aware readiness,
- fail-closed WhatsApp config,
- strict duplicate-key-safe business mapping parser,
- correct component variable ownership,
- cooperative worker stop,
- claimed work drained or fenced-release/requeue,
- task failure exits process,
- safe resource cleanup,
- separate bootstrap/migration/runtime DB roles,
- no credential-bearing URLs in repository/logs,
- safe generated connection URLs,
- API shutdown grace,
- private atomic backup,
- clean fail-fast restore,
- migration and readiness after restore,
- truthful docs.

If Docker is unavailable locally:

- complete all static and non-Docker gates,
- use an authorized Docker-capable environment if available,
- otherwise return Docker runtime as a genuine blocker,
- do not call staging validated.

## SECURITY REQUIREMENTS

- HMAC verification mandatory.
- Mapping configuration validated at startup.
- Internal APIs remain private.
- Shared internal bearer is acceptable only behind controlled private ingress for this milestone.
- No raw customer payloads, phones, secrets, credentials, database URLs, authorization headers, or transcripts in logs/evidence.
- Synthetic data only.
- Least-privilege DB roles.
- Health/metrics endpoints not exposed publicly without an ingress policy.
- No model-generated authority.
- No provider calls in transactions.
- No customer success before commit.
- No false delivery claims.

## TEST REQUIREMENTS

### UNIT / DOMAIN

- booking activation across Tamil/Tanglish/English,
- date/time/name parsing,
- correction and negation,
- offer membership,
- offer expiry,
- cross-tenant/cross-conversation rejection,
- field invalidation,
- readback,
- confirmation,
- medical safety,
- evidence result propagation.

### POSTGRESQL

- offer persistence/restart,
- exact slot recheck,
- real overlapping competitor,
- appointment atomicity,
- notification atomicity,
- same-PA concurrency,
- duplicate inbound event,
- duplicate confirmation,
- cancellation/reschedule,
- retention-independent manifest replay,
- tenant isolation,
- rollback/session usability.

### ROUTE / WORKER E2E

- signed webhook to durable inbox,
- worker to proposal,
- confirmation to commit,
- outbox to fake provider,
- callback reconciliation,
- restart and replay,
- error and dead-letter behavior.

### CONCURRENCY CLAIMS REQUIRE

- independent sessions,
- real overlap,
- deterministic barrier,
- fresh observer where needed,
- proof of contender non-completion,
- release,
- terminal outcomes,
- timeout and cleanup.

A sequential test is not concurrency evidence.

## FUNCTIONAL PROOF ARTIFACT

Produce one sanitized report containing:

- exact SHA,
- clinic fixture,
- patient request transcript,
- state transitions,
- offered slots,
- selected token,
- readback,
- committed receipt,
- manifest/outbox identities,
- provider-attempt/callback outcome,
- restart/replay result,
- failure-path outcomes,
- timing,
- all skipped external gates.

No PII or credentials.

## REQUIRED GATES

Run once at final candidate:

1. focused unit/domain tests,
2. offer/persistence tests,
3. PostgreSQL atomicity and concurrency,
4. route/worker E2E,
5. functional proof,
6. Ruff,
7. format check,
8. full mypy,
9. Alembic heads/check/parity,
10. offline migration rendering,
11. fresh/populated migration cycle,
12. full non-PG,
13. full PG on fresh isolated DB,
14. backup/restore tests,
15. readiness verifier,
16. git diff check,
17. secret/PII scan,
18. clean tree,
19. exact-head hosted CI.

Do not rerun full suites without a code change or final-gate need.

## DELIVERY AND INTEGRATION

Final candidate report:

```
CANDIDATE SHA:
BRANCH:
BASE:
REMOTE SHA:
FILES CHANGED:
PRODUCT OUTCOME:
APPLICATION CONTRACT:
WHATSAPP E2E:
APPOINTMENT/CONCURRENCY:
NOTIFICATION EVIDENCE:
RESTART/REPLAY:
STAGING TOPOLOGY:
SECURITY/TENANT:
FUNCTIONAL PROOF:
UNIT:
NON-PG:
PG:
MIGRATIONS:
BACKUP/RESTORE:
RUFF/FORMAT/MYPY:
READINESS:
HOSTED CI:
SKIPPED/BLOCKED:
KNOWN LIMITATIONS:
WORKTREE STATUS:
NEXT INTEGRATION STEP:
```

Do not self-approve.

After one bounded review and required corrections:

- report exact final SHA,
- wait for CEO exact-SHA authorization,
- fast-forward main only,
- push without force,
- verify remote main SHA,
- verify post-integration CI,
- only then begin the next non-voice milestone.

## EFFICIENCY RULES

- One branch.
- One milestone.
- One reviewer.
- Maximum five blockers.
- One correction pass.
- No routine progress messages.
- No speculative abstractions.
- No optimization before integration.
- No broad documentation rewrite except direct contradictions.
- No additional channel or industry work.
- No merging retired branches wholesale.
- No "production-ready" claim without staging and operational evidence.

## TERMINAL STOP CONDITIONS

Return a genuine blocker only for:

- unavailable real provider credentials,
- unavailable Docker/staging host after all other work is exhausted,
- migration conflict requiring founder/CEO decision,
- unavoidable Dev4 ownership collision,
- destructive external operation requiring approval,
- product-policy contradiction.

Everything else is your responsibility to resolve autonomously.

## PRIMARY SUCCESS CRITERION

A synthetic Tamil/Tanglish dental patient completes a real WhatsApp booking against PostgreSQL, receives a committed result, the clinic receives durable notification evidence, and a duplicate/restart cannot create a second booking.

Deliver that outcome from integrated main.

---

## IMPLEMENTATION SLICE INDEX

| Slice | Goal | Status |
|-------|------|--------|
| 0 | Trace + fixed contract types | DONE (trace completed, types in Slice 1) |
| 1 | Durable offer + selection seam | ACCEPTED at dab77b4 (M1) — held, not on main |
| 2 | Complete mounted WhatsApp booking E2E | NOT STARTED |
| 3 | Safe single-node staging topology | NOT STARTED |
| 4 | Operations / privacy closure | NOT STARTED |

## D3-M2 — TIME UNDERSTANDING + OWNER CHANGES CLINIC (2026-08-11)

Three-part checklist frozen by the reviewer. Status:

- Part 1 (P0 bare/dotted time + date-cannot-default): DONE.
  New domain/booking/datetime_parse.py — parse_time_of_day / parse_relative_date,
  independent fields, never guess. fact_resolver._combine_datetime and
  conversation._extract_datetime rewritten so NO path defaults a date to today.
  Tests: 45 parser unit, test_bare_time_no_wrong_day_postgres (bare/dotted/
  worded reply after a TOMORROW offer books TOMORROW; bare time with no date
  books nothing; vague time is asked about, never guessed).

- Part 2 (Tamil/Tanglish + relative dates + split-shift): DONE.
  Parser handles Tamil/Tanglish + relative dates. Missing-half question is
  precise (asks date vs time). test_split_shift_availability_postgres — a
  clinic open 09:30-13:00 & 17:00-20:30, closed Sunday, never offers a
  midday-gap slot, never offers Sunday; evening alternatives stay evening.

- Part 3 (owner changes clinic by chatting): DONE for per-date changes.
  The owner-command engine (OwnerCommandService: doctor_leave / close_early /
  close_clinic writing ScheduleException rows that availability reads) already
  existed on this base; identity/role come from the verified sender phone
  (_is_owner), never the message. test_owner_changes_clinic_e2e_postgres proves
  the DoD: owner texts a leave -> conflicting appointment cancelled + surfaced
  to the owner -> a new patient asking the withdrawn slot is refused; a
  non-owner phone sending the same instruction changes nothing.

  HONEST GAP: the reviewer's examples also included "Saturday we close at 3
  now" — a PERMANENT weekly hours change. There is NO write path for permanent
  weekly operating_schedules changes on base cc3aa65 (upsert_schedule is a
  plain INSERT; the "second activation replaces timetable" fix e41a082 is NOT
  on this base). The engine covers per-date leave/closure/early-close, which
  satisfies "changing hours" for a day and the DoD demonstration, but not a
  permanent weekly change. Permanent weekly change needs either e41a082 on main
  (then rebase) or a new targeted on_conflict_do_update schedule write — flagged
  to the reviewer rather than silently scoped out.

### CEO #17 — ACCEPTED at 80c9492 (review 2026-08-12)

Test-only lock of the bare-meridiem gate. Reviewer reproduced independently in
an isolated worktree (blob-identity check: conversation.py at 80c9492 ==
d2f646d, NON_TEST_FILES=0) and ran BOTH anchored mutations himself, grepping the
changed line first to prove each landed:
- dot-strip neutralized -> 3 fail (PM./pm./am.), negations untouched;
- "no"/"not" added to _filler -> 7 fail (negations), punctuation untouched.
The two mutations partition CLEANLY (kill disjoint sets), which is what earned
acceptance: punctuation-normalization and the negation guard are each
independently locked, not one assertion wearing two hats. Reviewer did NOT
re-run the full 1280 suite or PG corpus at 80c9492 — judged covered since the
only production file is byte-identical to d2f646d and the diff is one test file.

Stale-baseline note: CEO #17's behavior already landed in f3da3dc (an ancestor
of d2f646d); the assignment was frozen at af451a22. Surfaced rather than
rebuilt — the rescope to regression-locking + anchored mutations came from Dev3.
Candidate 80c9492 (tag d3-ceo17-80c9492). Test-only; no runtime change.

### ITEM #19 + CEO #32 — ACCEPTED at d2f646d (review 2026-08-12)

Reviewer reproduced independently (isolated worktree, import-proven; private DB
fonely_test_ceo32; clean FF chain 9960095 -> 92834c2 -> d2f646d): corpus 26/26,
non-PG 1259, ruff+format+mypy(src) all exit 0, and the kill_terminate mutation
(numbered prompt x5) confirmed the liveness test genuinely detects the
terminating stage. They further stretched the probe to 14 unresolvable answers
and confirmed the ladder runs exactly ONCE and never restarts — termination
sticks, invariant closed. #19 verdict stands: 4/5 items were proven earlier,
item 4 was the gap, this closes it; I5-independent-of-I1 proven (wrong_doctor
mutation fires I5 alone, I1/I2 pass). #19 unblocked; integration stays reviewer-
side; nothing to main.

TWO NON-BLOCKING NOTES from the acceptance (do NOT act now):
1. HARNESS CAVEAT (not a defect): I3 (no-response-repeats-3x) is only meaningful
   for conversations SHORT enough not to fall through to the mock LLM, which
   returns a constant "ok" (harness line ~167, gw.complete -> ModelResponse
   text="ok"). Do NOT lengthen a corpus case beyond the deterministic path and
   expect I3 to hold — it will fail on the mock's constant, looking like a
   repetition bug. A genuine long-conversation liveness case needs the mock to
   vary its response first. (The 6-answer disambig-liveness-bound stays within
   the deterministic path, so it is valid.)
2. CEO #33 (filed by reviewer, NOT mine to fix now, NOT blocking): the give-up
   text "Please call the clinic directly" is coherent on WhatsApp but wrong on
   voice (the caller already dialled the clinic). Needs a channel-aware string —
   a shared concern, not dental. Low severity while voice runtime is unwired;
   demo-visible once M3 lands.

### ITEM #19 — CEO #32 FIX: TERMINATING BOUND (candidate d2f646d, 2026-08-12)

9960095 was ACCEPTED AS WORK-IN-PROGRESS by the reviewer, NOT a third rejection:
four of five rescope-2 items proven by their own execution (corpus 26/26; the
resolver-off and bound-off mutations both reproduced; and they ADDED the
outstanding I5-independent-of-I1 proof — a wrong-candidate mutation fires ONLY
"I5 violated: booked resource_id 1, expected 2" off the row, I1/I2 green). One
real defect remained, rescoped to CEO #32 rather than bouncing the milestone.

CEO #32 DEFECT: the reject#2 bound did not terminate — it swapped one repeating
question for another. After two plain asks it switched to the numbered prompt,
then emitted THAT text every subsequent turn forever. The reject#2 liveness case
was only 4 turns, so the numbered text had appeared just twice and the deadlock
was invisible; the "unconditionally"/"never deadlocks" claims were false. Class:
liveness on a real call (poor-STT caller gets trapped), not booking correctness.

FIX: a genuinely terminating ladder mirroring the time-selection escape hatch —
ask 0-1 plain, ask 2 numbered choice, ask >=3 DROP the ambiguity flags and
_end_turn with a call-the-clinic message. State is genuinely left; no question
(plain or numbered) can recur. Liveness case renamed disambig-liveness-bound and
extended 3 -> 6 unresolvable answers (LONGER than the ladder, so a swap-only
bound breaches I3). I7 docstring corrected: escalates then TERMINATES.

BEFORE/AFTER PROOF: disabling the terminating stage makes disambig-liveness-bound
fail with the numbered prompt repeated 5x (the exact CEO #32 finding); the fix
ends the conversation and passes. Reverted; tree clean.

GATES: corpus 26/26 (private DB, dropped); non-PG 1259; ruff + mypy(src) clean.
Candidate d2f646d (tag d3-item19-d2f646d). Reviewer's standing note: #19 stays
open and does NOT integrate to main until CEO #32 lands (he will not put a false
liveness invariant into main). Evidence boundary unchanged: TEXT/MOCK only.

POST-MORTEM internalized: a liveness case whose length matches the bound proves
only the bound's first step. Choose the length that would catch a non-terminating
SECOND strategy. Applied here (6 > ladder of 3).

### ITEM #19 — REJECT #2 FIX (candidate 9960095, 2026-08-12)

REJECT #2 at 936bdee (reviewer's two-rejection rescope; scope now frozen at the
five-item checklist below). The 936bdee collision fix was confirmed closed AND
its good behaviors preserved by the reviewer, but it introduced a DEADLOCK:
requiring naming evidence (title/name adjacency) is correct in open speech but
wrong once WE have asked "which doctor?" — a bare "rao" answer has no adjacent
title/name, was discarded as vocabulary, the _resource_ambiguous flag never
cleared, and the question repeated forever (row-level: resource_id NULL). A dead
end is not failing closed.

THE LESSON (reviewer, named because it is the same one twice): both defects came
from changing a matcher's permissiveness GLOBALLY when the right answer was
CONTEXTUAL. Bare-overlap too loose in open speech; naming-evidence too strict in
a reply to our own question. No single global threshold is right in both states;
the conversation already knows which state it is in, so the matcher should too.

FIX (rescope-2, five frozen items):
1. 936bdee open-speech matcher kept EXACTLY as-is.
2. New _resolve_disambiguation_reply runs ONLY when _resource_ambiguous is set,
   matching against the CANDIDATE SET only (2-3 offered doctors). The tiny known
   surface makes relaxed matching safe: bare surname "rao", "rao please", scored
   by shared-token count so "priya rao" out-scores the shared "priya"; "priya"
   alone stays ambiguous; "dr smith" matches none.
3. Ordinal/positional answers — "the second one"/"first"/"last" and numbered
   "1"/"2" — resolved against the offered order. (Item 3 implemented, not
   deferred.)
4. Loop bounded UNCONDITIONALLY: after two plain asks the agent escalates to a
   numbered choice (a different response), so no question repeats without limit
   even if resolution never succeeds. While a disambiguation is pending,
   open-speech matching is skipped (the turn is an answer, not fresh naming).
5. Permanent row-level corpus cases: disambig-bare-surname / -surname-polite /
   -ordinal (all book Dr. Priya Rao id=2 off the ROW), -nonmatch-reask,
   -deadlock-bound. I3 (no response repeats >3x, asserted on EVERY case) is the
   deadlock guard. Mani/General stay permanent roster fixtures. New I7 header.

BEFORE/AFTER PROOF (executed, both directions):
- Disable the resolver -> the three booking answers fail (nothing books) — the
  reviewer's exact deadlock.
- Disable the loop bound -> disambig-deadlock-bound fails with 4 identical
  "which doctor?" responses (I3 breached). Notably the bound holds even with the
  resolver disabled — item 4 survives when items 2/3 are defeated, which is the
  property the reviewer said mattered most.
- Both reverted; fix passes all.

GATES: corpus 26/26 (private DB, dropped after); non-PG 1259; ruff + mypy(src)
clean. Candidate 9960095 (tag d3-item19-9960095). Evidence boundary unchanged:
TEXT/MOCK only, no audio. Deferred by reviewer to next SHA (not gating): their
own reproduction, the pick-one mutation, and I5-independent-of-I1. No rebase,
nothing to main, exact-SHA authorization only.

### ITEM #19 — REJECT #1 FIX (candidate 936bdee, 2026-08-12)

BLOCKING regression the reviewer found at bb50461: token-overlap scoring in the
new matcher silently booked the wrong doctor when a time/service word collided
with a stored name. "aaru mani" (Tanglish 6 o'clock) booked "Dr. Mani";
"General Consultation" booked "Dr. General" — class-1 silent wrong-doctor. A
regression: the old substring matcher ("dr. mani" not a substring of "aaru
mani") did not mis-book here; token-overlap removed the floor under
over-matching.

FIX — distinguish a token APPEARING from a token used to NAME someone
(_naming_tokens): a stored-name token counts only when adjacent to a title
("dr priya") or to another name token ("priya rao", "kumar arun"). A lone name
token among non-name words ("aaru MANI kaalai") is vocabulary and is ignored.
This closes the collision WHILE preserving cross-token ambiguity — the reviewer
warned the same permissiveness produces both, so the fix is not "tighten until
Mani disappears". Also: _names_a_resource now keys on _NAMING_TITLES
(dr/doctor/dho/vaidhyar) only, so "miss"/"mr"/"ms" as ordinary words no longer
false-fire the unknown-doctor re-ask.

PERMANENT REGRESSION GUARDS (per reviewer): the harness roster now permanently
includes Dr. Mani (time-word collision) and Dr. General (service-word
collision) — a roster of well-behaved names cannot surface this class, which is
why 16/16 was green over the live defect. New I6 invariant + forbid_resource_id:
a forbidden resource_id must NEVER be committed, read off the appointments ROW,
checked unconditionally. New cases: timeword-collision-mani,
serviceword-collision-general, title-only, cross-token-ambiguous, no-mention;
the existing "mani" time cases carry forbid_resource_id=4.

BEFORE/AFTER PROOF: reverting the matcher to bare-overlap FAILS exactly the
collision cases (serviceword-collision-general: "I6 violated: booked
resource_id 5"; the mani time cases go ambiguous); the fix passes all. Corpus
21/21 (private DB); non-PG 1259; ruff + mypy(src) clean. Candidate 936bdee
(tag d3-item19-936bdee). Same TEXT/MOCK-only evidence boundary as before — no
audio. Standard terms: no rebase, nothing to main, exact-SHA authorization only.

### ITEM #19 — SPOKEN RESOURCE-NAME NORMALIZATION (candidate 2026-08-12)

D3-M4 ACCEPTED at exact 4bc395f (reviewer reproduced in isolated worktree +
own PostgreSQL, unit 78 / PG corpus 12 / non-PG 1259 / ruff+format+mypy clean,
and mutation-proved both fixes). Item #19 is the CALL I reported honestly during
D3-M4 (spoken "dr priya" vs stored "Dr. Priya"), ruled a separate item because
patients say the doctor's name aloud on every call.

Frozen scope delivered:
1. Normalize spoken names on the REAL path (_extract_facts in process_message):
   _resource_name_tokens strips honorifics (dr/dr./doctor/dho/vaidhyar/mr/ms/…),
   casing, punctuation, and collapses whitespace; matching is title/word-order
   /case/punctuation-insensitive. _match_spoken_resources scores by shared-token
   count and returns the top scorers.
2. Ambiguity FAILS CLOSED: >1 top-scoring resource -> _resource_ambiguous,
   clears any provisional resource_id, asks "which doctor". Never picks.
3. Unknown name FAILS CLOSED: a title word present but zero match (_names_a_
   resource) -> _resource_unknown, re-asks with the roster; never falls through
   to any-available. (This was a real gap the harness caught — see below.)
4. Corpus drives spoken names end to end: _LEAD now says "doctor arun" (stored
   "Dr. Arun"), so every time-case exercises the spoken path; seed has three
   eligible doctors incl. two sharing a first name for real ambiguity. Four new
   cases: spoken-reordered books the right row; ambiguous "dr priya" fails
   closed; fuller "priya rao" resolves it; unknown "dr smith" refuses.
5. Row-level assertion (I5): committed resource_id read from the appointments
   ROW (not transcript) must equal the intended doctor. Independent detection
   power — a wrong-doctor booking fails I5 even with the right time and reply.

HARNESS FOUND A DEFECT (the point of the harness): an unknown named doctor
("dr smith") left resource_id unset and fell through to the generic prompt with
no explicit refusal — the mocked gateway then answered "ok" and a later "yes"
could proceed without a doctor chosen. Fixed with the _resource_unknown branch.
The I5 fail-closed assertion (part b: must re-ask for the doctor) is what caught
it — real detection power, not I1-shadowed.

MUTATION PROOF: forcing _match_spoken_resources to pick-one ([:1]) reproduced
the silent wrong-doctor booking — resource-ambiguous-priya failed with exactly
"I5 violated: booked resource 1 for an ambiguous spoken name", while the control
resource-spoken-reordered still passed. Reverted; tree byte-identical to bb50461.

EVIDENCE BOUNDARY (stated explicitly per reviewer): TEXT/MOCK evidence only —
unit helper matrix, PG corpus through process_message, row-level resource_id
assertions, mutation proof. NO audio: no STT output, no real speech, no Tamil
pronunciation. This does NOT prove the demo call works; real-speech proof stays
with the voice lane.

GATES: corpus 16/16 (run on a PRIVATE DB fonely_test_res19 — the shared
fonely_test instance is under concurrent DDL from other sessions and conftest's
session-scoped downgrade/upgrade races them; a dedicated DB is contention-free
and satisfies the same conftest name guard). non-PG 1259 passed. ruff + mypy
src clean. (Pre-existing untyped test-helper mypy notes in the harness file
predate 4bc395f and are not gated; reviewer runs mypy on src.)

HARD NON-OVERLAP respected: no voice runtime, no task-#27 audio files, no
migration 0016/0017, no new Alembic revision, no shared integration conftest
edits. Candidate SHA: bb50461. Standard terms: no rebase, nothing to main,
exact-SHA authorization from the reviewer only.

### D3-M4 — BOOKING CONVERSATION SURVIVES REAL SPEECH (candidate 2026-08-11)

Adversarial harness driving the REAL conversation path (`_process_domain` ->
`process_message`), not `_extract_datetime` in isolation. Speech-shaped corpus,
invariants asserted on EVERY input:
- I1 never book a time the patient did not name/select
- I2 never book a slot not offered
- I3 no question repeats unboundedly (bounded identical asks)
- I4 a correction supersedes the earlier reading

CORPUS: 10 scripted conversations across 8 categories. Per-category result:
- no_punctuation (2 cases: nopunct-morning, nopunct-evening-offer) — PASS,
  found nothing. Lowercase run-on "10 30 am" and bare "5 15"+"5 30 pm" book
  correctly.
- disfluency (1: disfluency) — PASS, found nothing. "um at ten thirty am" books
  10:30.
- stt_artifact (1: stt-trailing-period) — PASS, found nothing. "5:30 PM."
  (trailing period) resolves.
- code_mixing (2: codemix-tanglish, codemix-evening-word) — PASS, found
  nothing. "naalaikku pathu mani kaalai" -> 10:00; "aaru mani"+"maalai" -> 18:00.
- split_turn (1: split-turn) — PASS, found nothing. time-then-date-then-meridiem
  across three turns books 18:00.
- negation (1: negation-no-time) — **FOUND A DEFECT (I1 silent mis-booking)**.
  "not 5 pm" booked 5:00 PM. FIXED: `_time_is_directly_negated` +
  `_correction_replacement_spec` reject the negated time; "not 5 pm" now books
  nothing until "6 pm" is named. Now PASS.
- correction (1: correction-evening) — **FOUND A DEFECT (I4 correction lost)**.
  "no no make it 6 pm" after a 10:30 AM proposal booked nothing — the negative
  confirmation branch dropped the proposal but never re-extracted the new time.
  FIXED: the AWAITING_CONFIRMATION negative branch now carries the rejected
  proposal's DATE forward as _pending_date, re-extracts the replacement time,
  and re-proposes the corrected slot. Now PASS (books 18:00).
- vague (1: vague-time) — PASS, found nothing. "sometime"/"whenever" books
  nothing (correctly refuses to guess).

TWO CLASSES FIXED (per checklist item 4): silent mis-booking (negation) and
correction-supersedes (I4). No unbounded repetition surfaced (I3 held on all 10).

REPORTED, NOT FIXED (proposed as a call, per checklist item 4): resource-name
punctuation. The harness lead uses the stored form "Dr. Priya"; a spoken
"dr priya" (no period/caps) does NOT match `_extract_facts` resource matching.
Out of scope for the TIME-understanding milestone; isolated deliberately so the
corpus tests time, not fuzzy resource matching. Proposed as a separate call.

FOLDED IN (checklist item 5): task #16 (`parse_time_spec("the evening one")`
no longer reads ordinal "one" as hour 1 — needs a real clock token) and task
#17 (bare "am"/"pm" standalone gate + trailing-punctuation 'PM.'/'pm please').

Same integration hold as D3-M1/M2/M3: no push to main, no rebase onto the CEO
branch. Awaiting exact-SHA authorization.

REJECT #1 of D3-M4 (c38df96, review 2026-08-11) — two required items, both
fixed in the follow-up candidate:
1. REGRESSION in Task #16, missed by the corpus: the ordinal guard was too
   broad — "one thirty"/"one fifteen"/"one in the afternoon" all collapsed to
   None (safe direction, but a real capability loss; "two thirty" still parsed,
   an indefensible asymmetry). FIX: gate the suppression on PROVENANCE — a "one"
   reads as hour 1 on positive evidence (explicit minute from thirty/fifteen/
   half/quarter/tail, a clock token, or hour position); it is suppressed only in
   a slot-picking phrase ("the evening one"/"the first one"). Verified by a new
   unit matrix (TestOrdinalOneVsHourOne, 16 cases) AND two new corpus cases
   (ordinal-one-no-time, one-thirty-out-of-hours).
2. I4 CLAIMED BUT NOT ASSERTED: the header said I4 ran on every conversation;
   it existed only as one case's expected time. FIX: added Case.superseded_local
   and a real I4 assertion (committed time must never equal the superseded
   reading) on the negation + correction cases; corrected the header to state
   precisely where each invariant applies. Also fixed the _max_identical_repeats
   docstring (counts total occurrences, not longest run — strictly stronger).
Reviewer logged the "dr priya" resource-name gap as item #19 (mine, after M4).
Corpus now 12 cases; 12/12 pass. Non-PG 1259 passed; ruff+mypy clean.

RE-REVIEW READINESS — the reviewer named two things they will check BY
EXECUTION after main moves. Both self-verified proactively at 4bc395f:
1. "two thirty" and the other word-number times still parse after the guard
   change. VERIFIED: exhaustive matrix of all 12 word-numbers x {thirty,
   fifteen, half past, quarter past, o'clock, pm, am} = 84/84 parse correctly;
   only the ordinal "one" slot-picking phrases suppress, as intended.
2. The I4 assertion actually FAILS if correction handling is broken (an
   assertion that cannot fail is absence-reads-as-success). VERIFIED by a
   mutation experiment: temporarily forcing negated_time=False in
   _extract_datetime makes negation-no-time fail with exactly "[negation-no-time]
   I4 violated: booked the SUPERSEDED reading 17:00:00", while control cases
   still pass; reverted, tree byte-identical to 4bc395f afterward.

DURABILITY (cannot push — credential wall): 4bc395f is pinned by two local
branches (dev3/dental-whatsapp-staging-e2e, d3m4-backup) AND tag
d3m4-reject1-4bc395f, all in the shared /scratch/karthick/fonely/.git object
store, so it survives this worktree's removal. Holding here per reviewer
instruction; will rebase onto the integration tip only after main moves.

NOTE on PG flakiness observed 2026-08-11: the shared PG instance is under
concurrent DDL from other live sessions; conftest's session-scoped
downgrade-base/upgrade-head races them (symptoms rotate: pg_type dup,
businesses_pkey dup, downgrade-base non-zero — different case each run). Corpus
logic is proven: 12/12 twice earlier this session, 11/12 once with the sole
failure a cross-session businesses_pkey collision (not logic). Not a code defect.

### D3-M3 ACCEPTED at af451a2 (review 2026-08-11)

Time-understanding residuals + coupling guard. Accepted after four defects
found across three review rounds, all closed by execution-verified fixes:
- D1 bare-time-books-wrong-day (round 1), D2 ambiguity-loop (round 1),
  D3 resolution-scanned-whole-offer (round 2 introduced, rescoped),
  D4 bare-"am"-books-morning (round 2 introduced, rescoped).
Final state: ambiguity stored as {display, token}; resolution targets only the
two candidate tokens; bare "am"/"pm" resolve standalone-only; meaning-words
(morning/evening/kaalai/maalai/…) resolve anywhere; "pagal"/"பகல்" dropped
(daytime-broad, would be a guess); split-turn meridiem carried via
_pending_time_explicit; ambiguity question hard-bounded at 2 asks.

Same integration hold as D3-M2/M1: main integration sequenced by the reviewer/
Karthick. Do NOT push to main, do NOT rebase onto the CEO branch. Reviewer
recommended b522c82 first to Karthick (fast-forwards from cc3aa65, no force,
no merge); awaiting his exact-SHA authorization.

NON-BLOCKING FOLLOW-UP (do NOT reopen D3-M3 for it — fix in a later pass when
next touching _bare_meridiem_word): the standalone gate rejects any second
token, so some natural answers fall through to the bound instead of resolving:
'PM.' (trailing period — most likely off an STT transcript), 'pm please',
'pm ah', 'pm da' (Tanglish). NONE mis-book — they cost one extra turn then hit
the fallback (safe direction). Fix direction: strip trailing punctuation from
the token ('PM.' -> 'PM'). CAUTION: the same strictness correctly makes 'no am'
and 'not pm' return None; a looser gate must not break that.

### D3-M2 ACCEPTED at f3a4d1f (review 2026-08-11)

Code gate passed; main integration SEQUENCED BEHIND the CEO branch. Do NOT
push to main and do NOT rebase onto the CEO branch itself — wait for it to
land on main, then rebase onto the new main.

INTEGRATION ORDER (from reviewer):
- ceo/onboarding-fixes-and-demo-edge (0132433) goes FIRST — it carries
  e41a082 (my permanent-weekly-hours dependency) and the Exotel auth P0 Dev4
  must mount behind. f3a4d1f and the CEO branch are divergent (11 vs 8 ahead
  of cc3aa65; both fast-forwardable from main, not from each other).
- After the CEO branch lands: rebase f3a4d1f onto the new main, re-run gates,
  then the reviewer issues an exact-SHA authorization. THAT unblocks the
  permanent-weekly-hours gap AND lets me switch the staging-e2e tenant seed
  from raw SQL to POST /internal/v1/businesses.

LOAD-BEARING INVARIANT (reviewer flagged): removing the clinic-hours lean
(parser no longer guesses PM) and the modulo-12 offer disambiguation are
load-bearing on each other. 'aaru mani' now parses 06:00 not 18:00; it
self-heals because check_and_offer returns alternatives and the modulo-12
match lands the bare reply on one. A future edit must NOT remove one without
the other, or an out-of-hours Tamil time stops recovering.

TWO RESIDUALS (logged by reviewer, NOT blocking, fix when next touching
_extract_datetime / check_and_offer — do not resubmit for these):
1. Ambiguous bare time (two candidate slots) declines selection, then
   _extract_datetime pops _active_offer and asks for a DATE — discards a
   valid offer and asks the wrong question. Near-zero real impact (needs a
   clinic offering both 5:30 AM and 5:30 PM). Fix: ask "which one" instead of
   dropping the offer.
2. A bare time whose intended meridiem is PM ('aaru mani' = 6 PM -> 06:00)
   anchors "nearest slots" to the morning; the Tamil-speaking patient we care
   most about gets an unhelpful first offer (recovers by saying "evening").
   Consider ranking alternatives across BOTH meridiem readings when
   meridiem_explicit is False.

NOT MY DEFECT (reviewer confirmed): the PG flaky pair
test_scheduling_mutation_concurrency_postgres[close_early]/[doctor_leave] is
Dev2's (history 4c765c6, fe18b7b), not in my diff, raised in Dev2's lane.

## TRACKED FOLLOW-UPS (from M1 review of dab77b4, 2026-08-11)

### P0 — bare-time selection books the wrong day (owned by Dev3, GATES M3)
A patient offered "10:00 AM, 10:30 AM" for TOMORROW who replies with a bare
time — "10:30", "10:30 please", "ok 10:30" — does NOT select (the selection
regex requires am/pm). The message falls through to `_extract_datetime`'s raw
parse, which matches, sets `target_date = now.date()` because "tomorrow" is
not in THIS message, pops the active offer, and books TODAY 10:30 — the wrong
day. "10.30" (dot, common in India) matches neither and sets nothing.
Two-part fix when picked up (before M3 voice booking):
1. Selection must accept a BARE time (no am/pm, and dot separators) and match
   it against the offered slot times.
2. The conversation must carry the active offer's DATE forward instead of
   defaulting to `now.date()`. This second half is the more dangerous one —
   it misfires OUTSIDE the offer path too (any bare time defaults to today).
Not an M1 defect: outside the six frozen conditions. Tracked as its own item.

### Non-blocking, recorded (fix when cheap; do not regress)
1. `AvailabilityOffer.generate_token` secret is a source literal
   ("fonely-offer-key"). Given server-side `collected_facts` + authoritative
   availability re-check at commit, this is a CORRUPTION CHECKSUM, not a
   security control. Do NOT describe it as a "keyed MAC" in any outsider-
   facing document. Move the key to settings when cheap.
2. `build_offer` computes `expires_at` inside the slot loop → empty
   `available_slots` raises `UnboundLocalError` instead of refusing cleanly.
   Latent: both orchestrator call sites guard against empty lists.
3. `_extract_facts` resource loop now `break`s on any name match, including an
   ineligible resource named before an eligible one — a behaviour change vs.
   the prior scan-and-continue. Revisit when correction eligibility matters.

### Deferred dependency
Switch staging-e2e tenant seed from raw SQL to `POST /internal/v1/businesses`
(e41a082) AFTER that route lands on integrated main and M1 is rebased onto it.
Do not rebase onto an unintegrated branch to satisfy this.
