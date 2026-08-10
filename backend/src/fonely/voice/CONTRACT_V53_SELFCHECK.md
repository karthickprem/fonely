# Contract v5.3 Self-Check

## 1. Literal digest vector recompute

Input canonical JSON (14 keys, sorted, compact):
```
{"business_id":1,"business_timezone":"Asia/Kolkata","customer_name":"Karthick","customer_phone":"+919000000000","duration_minutes":30,"resource_id":1,"resource_name":"Dr. Priya","schema_version":1,"service_id":10,"service_name":"scaling","slot_id":"slot-2026-08-11-1830-1","slot_version":1,"target_date":"2026-08-11","target_time":"18:30"}
```

Domain-prefixed input: `fonely:voice:propose:v1:` + above

**Recomputed SHA-256: `ddc0ee462146c3364cadde36a9f66a7754502cb8628e8144bb83a6cf5fd7b532`**
**Contract SHA-256: `ddc0ee462146c3364cadde36a9f66a7754502cb8628e8144bb83a6cf5fd7b532`**
**Match: YES**

Keys: exactly CollectedFacts (12) + business_id + schema_version = 14. Zero extra, zero missing.

## 2. v5.2 → v5.3 delta

| Change | v5.2 | v5.3 |
|---|---|---|
| TrustedCommandContext.actor_membership_provenance | absent | added |
| TrustedCommandContext.session_id | absent | added |
| TrustedCommandContext.conversation_id | absent | added |
| TrustedCommandContext.request_id | absent | added |
| TrustedCommandContext.booking_attempt | absent | added |
| CommitReceipt.propose_idempotency_key | absent | added |
| CommitReceipt.confirm_idempotency_key | absent | added |
| CommitReceipt.actor_session_id | absent | added |
| CommitReceipt.actor_user_id | absent | added |
| CommitReceipt.actor_role | absent | added |
| CommitReceipt.actor_membership_provenance | absent | added |
| CommitReceipt.conversation_id | absent | added |
| CommitReceipt.request_id | absent | added |
| CommitReceipt.booking_attempt | absent | added |
| CommitReceipt.status | absent | added ("committed") |
| CommitReceipt.schema_version | absent | added |
| CommitReceipt.version | absent | added (>0) |
| CommitReceipt.source | absent | added ("test_engine"/"appointment_service") |
| CommitReceipt.facts | dict | CommittedFacts (typed, 12 fields) |
| ConfirmCommand.expected_payload_digest | absent | added |
| ConfirmCommand.expected_version | absent | added |
| ConfirmCommand.idempotency_key | absent | added |
| ProposeCommand.context | absent | TrustedCommandContext |
| ProposeCommand.facts | absent | CollectedFacts |
| ProposeCommand.schema_version | absent | added |
| ProposeCommand.idempotency_key | pattern absent | `voice-{session_id}-a{booking_attempt}` |
| ProposeCommand.payload_digest | absent | SHA-256 of prefixed canonical |
| Validator checks | 16 | 24 (added #3-16 context binding, #18 version, #20-21 freshness/staleness, #22 source, #23 recompute, #24 replay) |
| Confirm outcome matrix | 10 rows | 13 rows (added payload_digest_mismatch, version_conflict, business_mismatch) |
| Media effect | "per generation" | "per process, honest scope" + production outbox mapping |
| Production mapping | 25 pairs | 26 pairs (added actor_membership_provenance) |
| Test-engine exclusion | "Prohibited in production" | Explicit: not imported, source check #22 fails |
| Happy probe | 8 assertions | 10 assertions (added allowed==True, TTS==1, audio==1) |
| Adversarial probe | separate concurrent + forged | combined Part A barrier + Part B 8 scenarios |

## 3. Chief items self-check

| # | Chief requirement | v5.3 location | Verified |
|---|---|---|---|
| 1 | Literal digest vector | §Canonical digest: exact JSON + SHA `ddc0ee46...` | ✓ recomputed |
| 2 | Complete facts | §CollectedFacts: 12 fields, all required | ✓ |
| 3 | Trusted actor role membership | §TrustedCommandContext: actor_user_id, actor_role, actor_membership_provenance | ✓ |
| 4 | Session binding | §TrustedCommandContext: session_id | ✓ |
| 5 | Conversation binding | §TrustedCommandContext: conversation_id | ✓ |
| 6 | Request binding | §TrustedCommandContext: request_id (unique per attempt) | ✓ |
| 7 | Booking attempt binding | §TrustedCommandContext: booking_attempt | ✓ |
| 8 | Typed propose with all fields | §ProposeCommand: context, facts, schema_version, idempotency_key, payload_digest | ✓ |
| 9 | Typed confirm with all fields | §ConfirmCommand: context, proposal_id, expected_payload_digest, expected_version, idempotency_key | ✓ |
| 10 | Receipt operation | §CommitReceipt: operation ("create"/"cancel"/"reschedule") | ✓ |
| 11 | Receipt status | §CommitReceipt: status ("committed") | ✓ |
| 12 | Receipt proposal+commit IDs | §CommitReceipt: proposal_id, commitment_id (both >0) | ✓ |
| 13 | Both idempotency keys | §CommitReceipt: propose_idempotency_key, confirm_idempotency_key | ✓ |
| 14 | Full target digest | §CommitReceipt: payload_digest | ✓ |
| 15 | Schema version | §CommitReceipt: schema_version | ✓ |
| 16 | Version (optimistic) | §CommitReceipt: version >0 | ✓ |
| 17 | Expected optimistic version | §ConfirmCommand: expected_version | ✓ |
| 18 | Authentic source | §CommitReceipt: source, Validator #22 | ✓ |
| 19 | Max-age freshness | §Validator #20 (future skew), #21 (max_age_ns) | ✓ |
| 20 | Confirm outcome: same key same digest | §Confirm outcome: row 2 (success, same proposal_id) | ✓ |
| 21 | Confirm outcome: same key diff digest | §Confirm outcome: row 3 (idempotency_payload_conflict) | ✓ |
| 22 | Confirm outcome: already committed | §Confirm outcome: rows 7+8 (same receipt regardless of confirm key) | ✓ |
| 23 | Validator recomputes all, no shortcut | §Validator #23: recompute from receipt.facts; "no runtime shortcut" header | ✓ |
| 24 | Happy: accepted receipt | §PROBE 1: result.allowed == True (receipt-aware adapter) | ✓ |
| 25 | Happy: consequential ALLOW | §PROBE 1: speech_class == COMMITTED_CREATE, allowed == True | ✓ |
| 26 | Happy: exactly one command effect | §PROBE 1: engine.commitment_count == 1 | ✓ |
| 27 | Happy: exactly one TTS | §PROBE 1: TTS.calls == 1 | ✓ |
| 28 | Happy: exactly one audio | §PROBE 1: MediaPort.sent_audio == 1 | ✓ |
| 29 | Happy: terminal | §PROBE 1: Dialogue terminal after this turn | ✓ |
| 30 | Combined barrier same-slot | §PROBE 2 Part A: asyncio.Barrier, one CommitReceipt, one slot_already_booked | ✓ |
| 31 | Combined forged/conflicting | §PROBE 2 Part B: 8 scenarios with canonical errors | ✓ |
| 32 | Atomic media or honest scope | §Media effect: per-process + "No exactly-once claim across processes" | ✓ |
| 33 | Production outbox mapping | §Media effect: WhatsAppDeliveryAttempt pattern | ✓ |
| 34 | Complete production field map | §Production mapping: 26 pairs | ✓ |
| 35 | Test-engine structural exclusion | §Production mapping: not imported, source check fails | ✓ |

## 4. Production mapping verification (26 pairs)

| # | Voice field | Backend field | Verified |
|---|---|---|---|
| 1 | context.business_id | CreatePendingActionCommand.business_id | ✓ |
| 2 | context.actor_user_id | ActorContext (session) | ✓ |
| 3 | context.actor_role | ActorContext.verified_role | ✓ |
| 4 | context.actor_membership_provenance | ActorContext construction method | ✓ |
| 5 | context.conversation_id | ConversationContext.conversation_id | ✓ |
| 6 | context.booking_attempt | ConversationContext.booking_attempt | ✓ |
| 7 | context.session_id | ActorContext.session_id | ✓ |
| 8 | facts.service_id | PendingAppointmentEnvelope.service_id | ✓ |
| 9 | facts.service_name | PendingAppointmentEnvelope.service_name | ✓ |
| 10 | facts.resource_id | PendingAppointmentEnvelope.resource_id | ✓ |
| 11 | facts.resource_name | PendingAppointmentEnvelope.resource_name | ✓ |
| 12 | facts.target_date | PendingAppointmentEnvelope.start_at (date) | ✓ |
| 13 | facts.target_time | PendingAppointmentEnvelope.start_at (time) | ✓ |
| 14 | facts.duration_minutes | PendingAppointmentEnvelope.duration_minutes | ✓ |
| 15 | facts.business_timezone | PendingAppointmentEnvelope.business_timezone | ✓ |
| 16 | facts.customer_phone | ActorContext.normalized_phone | ✓ |
| 17 | idempotency_key | PendingAction.idempotency_key | ✓ |
| 18 | payload_digest | PendingAction.payload_digest | ✓ |
| 19 | schema_version | PAYLOAD_SCHEMA_VERSION | ✓ |
| 20 | ConfirmCommand.proposal_id | CompleteCommitCommand.pending_action_id | ✓ |
| 21 | ConfirmCommand.expected_version | CompleteCommitCommand.expected_version | ✓ |
| 22 | CommitReceipt.commitment_id | Appointment.id | ✓ |
| 23 | CommitReceipt.proposal_id | PendingAction.id | ✓ |
| 24 | CommitReceipt.version | PendingAction.version | ✓ |
| 25 | CommitReceipt.facts | AppointmentConfirmationResult → CommittedFacts | ✓ |
| 26 | CommitReceipt.source | "appointment_service" | ✓ |

## 5. Confirm outcome matrix verification (13 rows)

| # | Scenario | Expected | Consistent |
|---|---|---|---|
| 1 | propose new key | success, proposal_id, version=1 | ✓ |
| 2 | propose same key same digest | success, same proposal_id | ✓ idempotent |
| 3 | propose same key diff digest | idempotency_payload_conflict | ✓ |
| 4 | propose same key diff business | idempotency_business_mismatch | ✓ |
| 5 | propose slot booked | slot_already_booked | ✓ |
| 6 | confirm valid | CommitReceipt | ✓ |
| 7 | confirm already committed same key | same CommitReceipt | ✓ idempotent |
| 8 | confirm already committed diff key | same CommitReceipt | ✓ key informational |
| 9 | confirm wrong expected_digest | payload_digest_mismatch | ✓ NEW |
| 10 | confirm wrong expected_version | version_conflict | ✓ NEW |
| 11 | confirm wrong business | business_mismatch, PENDING preserved | ✓ |
| 12 | confirm not found | proposal_not_found | ✓ |
| 13 | confirm slot race | slot_already_booked, →SLOT_TAKEN | ✓ |

No failure deletes or corrupts a proposal. ✓
