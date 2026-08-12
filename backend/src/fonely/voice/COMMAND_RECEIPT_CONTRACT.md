# Command/Receipt Contract v5.3

## CollectedFacts

| Field | Type | Source |
|---|---|---|
| service_id | int | Catalog lookup |
| service_name | str | Catalog snapshot |
| resource_id | int | Availability filtered by capability |
| resource_name | str | Catalog snapshot |
| slot_id | str | Availability result |
| slot_version | int | Availability optimistic version |
| target_date | date | Resolved via TrustedClock |
| target_time | str | HH:MM 24h business timezone |
| business_timezone | str | IANA from TrustedClock |
| duration_minutes | int | Service catalog |
| customer_name | str | NFC-normalized |
| customer_phone | str | E164 from ActorContext |

All required. Readback: assistant must contain service_name, resource_name, date, time, name — deterministic match.

## TrustedCommandContext

| Field | Type | Binding rule |
|---|---|---|
| actor_user_id | int | Session-consistent |
| actor_role | str | Session-consistent |
| actor_membership_provenance | str | Session-consistent |
| business_id | int | Operational equality |
| session_id | str | Operational equality |
| conversation_id | str | Operational equality |
| request_id | str | Unique per attempt |
| booking_attempt | int | Operational equality |

## Canonical digest

Prefix: `fonely:voice:propose:v1:`

Rules: sorted keys, `YYYY-MM-DD` dates, `HH:MM` times, NFC strings, decimal ints, no floats/nulls/extra keys, `(",":":")`, UTF-8.

### Literal vector

```
{"business_id":1,"business_timezone":"Asia/Kolkata","customer_name":"Karthick","customer_phone":"+919000000000","duration_minutes":30,"resource_id":1,"resource_name":"Dr. Priya","schema_version":1,"service_id":10,"service_name":"scaling","slot_id":"slot-2026-08-11-1830-1","slot_version":1,"target_date":"2026-08-11","target_time":"18:30"}
```

**SHA-256: `ddc0ee462146c3364cadde36a9f66a7754502cb8628e8144bb83a6cf5fd7b532`**

## ProposeCommand

| Field | Type |
|---|---|
| context | TrustedCommandContext |
| facts | CollectedFacts |
| schema_version | int (1) |
| idempotency_key | `voice-{session_id}-a{booking_attempt}` |
| payload_digest | SHA-256 of prefixed canonical vector |

## ConfirmCommand

| Field | Type |
|---|---|
| context | TrustedCommandContext |
| proposal_id | int |
| expected_payload_digest | str (must equal propose digest) |
| expected_version | int (from propose result) |
| idempotency_key | `voice-{session_id}-a{booking_attempt}-confirm` |

## CommitReceipt

| Field | Type |
|---|---|
| commitment_id | int >0 |
| proposal_id | int >0 |
| business_id | int >0 |
| operation | "create"/"cancel"/"reschedule" |
| status | "committed" |
| propose_idempotency_key | str |
| confirm_idempotency_key | str |
| payload_digest | str |
| schema_version | int |
| actor_session_id | str |
| actor_user_id | int |
| actor_role | str |
| actor_membership_provenance | str |
| conversation_id | str |
| request_id | str |
| booking_attempt | int |
| version | int >0 |
| committed_at_ns | int >0 |
| source | "test_engine"/"appointment_service" |
| facts | CommittedFacts (typed) |

CommittedFacts: same 12 fields as CollectedFacts (slot_version included).

## Validator checks (inside validator, no runtime shortcut)

| # | Check |
|---|---|
| 1 | receipt is not None |
| 2 | receipt.status == "committed" |
| 3 | receipt.business_id == binding.business_id |
| 4 | receipt.proposal_id == binding.proposal_id |
| 5 | receipt.operation == binding.operation |
| 6 | receipt.propose_idempotency_key == binding.propose_key |
| 7 | receipt.confirm_idempotency_key == binding.confirm_key |
| 8 | receipt.payload_digest == binding.payload_digest |
| 9 | receipt.schema_version == binding.schema_version |
| 10 | receipt.actor_session_id == binding.actor_session_id |
| 11 | receipt.actor_user_id == binding.actor_user_id |
| 12 | receipt.actor_role == binding.actor_role |
| 13 | receipt.actor_membership_provenance == binding.provenance |
| 14 | receipt.conversation_id == binding.conversation_id |
| 15 | receipt.request_id == binding.request_id |
| 16 | receipt.booking_attempt == binding.booking_attempt |
| 17 | receipt.commitment_id > 0 |
| 18 | receipt.version == binding.expected_version |
| 19 | receipt.committed_at_ns > 0 |
| 20 | receipt.committed_at_ns <= now_ns + max_skew_ns |
| 21 | now_ns - receipt.committed_at_ns <= max_age_ns |
| 22 | receipt.source in allowed_sources |
| 23 | Recompute digest from receipt.facts + business_id + schema_version; must equal receipt.payload_digest |
| 24 | (commitment_id, version) not emitted this process |

Any failure → BLOCK. Fail-closed stub: BLOCK all consequential. Receipt-aware adapter: all 24 → ALLOW.

## Confirm outcome matrix

| Precondition | Same propose key | Same digest | Effect | Return |
|---|---|---|---|---|
| No existing | — | — | Create PENDING | success, proposal_id, version=1 |
| Existing PENDING | yes | yes | No change | success, same proposal_id |
| Existing PENDING | yes | no | No change | idempotency_payload_conflict |
| Existing (diff biz) | yes | — | No change | idempotency_business_mismatch |
| Slot reserved | — | — | No proposal | slot_already_booked |
| PENDING, slot free | confirm valid | — | Reserve, COMMITTED | CommitReceipt |
| COMMITTED | same confirm key | — | No change | same CommitReceipt |
| COMMITTED | diff confirm key | — | No change | same CommitReceipt |
| Any | wrong expected_digest | — | No change | payload_digest_mismatch |
| Any | wrong expected_version | — | No change | version_conflict |
| Any (diff biz) | — | — | PENDING unchanged | business_mismatch |
| None | — | — | No change | proposal_not_found |
| PENDING, slot race | — | — | →SLOT_TAKEN | slot_already_booked |

No failure deletes or corrupts a proposal.

## Media effect

At-most-once per process: in-memory set of `(commitment_id, version)`. Process restart clears. Production: durable notification outbox (existing `WhatsAppDeliveryAttempt` pattern) owns cross-restart dedup. No exactly-once claim across processes.

## Production mapping

| Voice | Backend |
|---|---|
| context.business_id | CreatePendingActionCommand.business_id |
| context.actor_user_id | ActorContext (session) |
| context.actor_role | ActorContext.verified_role |
| context.actor_membership_provenance | ActorContext construction method |
| context.conversation_id | ConversationContext.conversation_id |
| context.booking_attempt | ConversationContext.booking_attempt |
| context.session_id | ActorContext.session_id |
| facts.service_id | PendingAppointmentEnvelope.service_id |
| facts.service_name | PendingAppointmentEnvelope.service_name |
| facts.resource_id | PendingAppointmentEnvelope.resource_id |
| facts.resource_name | PendingAppointmentEnvelope.resource_name |
| facts.target_date | PendingAppointmentEnvelope.start_at (date) |
| facts.target_time | PendingAppointmentEnvelope.start_at (time) |
| facts.duration_minutes | PendingAppointmentEnvelope.duration_minutes |
| facts.business_timezone | PendingAppointmentEnvelope.business_timezone |
| facts.customer_phone | ActorContext.normalized_phone |
| idempotency_key | PendingAction.idempotency_key |
| payload_digest | PendingAction.payload_digest |
| schema_version | PAYLOAD_SCHEMA_VERSION |
| ConfirmCommand.proposal_id | CompleteCommitCommand.pending_action_id |
| ConfirmCommand.expected_version | CompleteCommitCommand.expected_version |
| CommitReceipt.commitment_id | Appointment.id |
| CommitReceipt.proposal_id | PendingAction.id |
| CommitReceipt.version | PendingAction.version |
| CommitReceipt.facts | AppointmentConfirmationResult → CommittedFacts |
| CommitReceipt.source | "appointment_service" |

Test engine: `fonely.voice.test_engine`, not imported by production. Production `allowed_sources = {"appointment_service"}`; test receipts fail check #22.

## PROBE 1: Happy

6-turn PipelineRuntime + receipt-aware adapter + TestBookingEngine.

Assert:
- engine.proposal_count == 1, commitment_count == 1
- result.commit_receipt is not None, .source == "test_engine"
- result.commit_receipt.facts.service_name == "scaling"
- result.commit_receipt.payload_digest == computed test vector digest
- result.commit_receipt.committed_at_ns > 0
- result.speech_class == COMMITTED_CREATE
- **result.allowed == True** (receipt-aware adapter)
- **TTS.calls for this turn == 1**
- **MediaPort.sent_audio includes exactly 1 response** (besides greeting)
- Dialogue terminal after this turn

## PROBE 2: Combined adversarial

**Part A** — two PipelineRuntime, one TestBookingEngine, same slot, asyncio.Barrier before confirm:

Assert: one CommitReceipt, one slot_already_booked, engine.commitment_count == 1.

**Part B** — forged/conflicting (all through PipelineRuntime):

| Scenario | Expected |
|---|---|
| Wrong payload_digest | Validator #8/#23 BLOCK |
| Wrong business_id | Validator #3 BLOCK |
| committed_at_ns = 0 | Validator #19 BLOCK |
| Wrong version | Validator #18 BLOCK |
| Wrong source | Validator #22 BLOCK |
| Stale (max_age exceeded) | Validator #21 BLOCK |
| Same key diff facts | idempotency_payload_conflict |
| LLM "confirmed" no facts | engine.proposal_count == 0 |

## Self-check

| Chief item | v5.3 |
|---|---|
| Literal digest vector | §Canonical: SHA `ddc0ee46...` ✓ |
| Facts/actor/role/membership/session/conversation/request/attempt | §TrustedCommandContext + §CommitReceipt all fields ✓ |
| Typed propose/confirm with operation/status/IDs/keys/digest/schema/version/source/max-age | §ProposeCommand + §ConfirmCommand + §CommitReceipt + §Validator #1-24 ✓ |
| Confirm outcome matrix same/diff keys/digests/committed | §Confirm outcome: 13 rows ✓ |
| Validator recomputes, no shortcut | §Validator #23 recompute, "no runtime shortcut" ✓ |
| Happy: ALLOW + one effect + one TTS + one audio + terminal | §PROBE 1: 10 assertions incl allowed==True, TTS==1, audio==1 ✓ |
| Combined concurrent + forged adversarial | §PROBE 2: Part A barrier + Part B 8 scenarios ✓ |
| Honest media scope + production outbox | §Media effect: per-process + WhatsAppDeliveryAttempt pattern ✓ |
| Complete production mapping | §Production: 26 pairs ✓ |
| Test-engine exclusion | §Production: not imported, source check fails ✓ |
