# Contract v5.2 Independent Self-Check

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

## 2. Production mapping verification (25 pairs)

| # | Voice field | Backend field | Verified |
|---|---|---|---|
| 1 | context.business_id | CreatePendingActionCommand.business_id | ✓ exists in commands.py:30 |
| 2 | context.actor_user_id | ActorContext (session lookup) | ✓ ActorContext in commands.py:15 |
| 3 | context.actor_role | ActorContext.verified_role | ✓ commands.py:22 |
| 4 | context.conversation_id | ConversationContext.conversation_id | ✓ state.py:124 |
| 5 | context.booking_attempt | ConversationContext.booking_attempt | ✓ state.py:131 |
| 6 | facts.service_id | PendingAppointmentEnvelope.service_id | ✓ payloads.py |
| 7 | facts.service_name | PendingAppointmentEnvelope.service_name | ✓ payloads.py |
| 8 | facts.resource_id | PendingAppointmentEnvelope.resource_id | ✓ payloads.py |
| 9 | facts.resource_name | PendingAppointmentEnvelope.resource_name | ✓ payloads.py |
| 10 | facts.target_date | PendingAppointmentEnvelope.start_at (date) | ✓ payloads.py |
| 11 | facts.target_time | PendingAppointmentEnvelope.start_at (time) | ✓ payloads.py |
| 12 | facts.duration_minutes | PendingAppointmentEnvelope.duration_minutes | ✓ payloads.py |
| 13 | facts.customer_phone | ActorContext.normalized_phone | ✓ commands.py:21 |
| 14 | facts.business_timezone | PendingAppointmentEnvelope.business_timezone | ✓ payloads.py |
| 15 | idempotency_key | PendingAction.idempotency_key | ✓ schema.py:1005 |
| 16 | payload_digest | PendingAction.payload_digest | ✓ snapshots.py |
| 17 | schema_version | PendingAction.payload_schema_version | ✓ payloads.py PAYLOAD_SCHEMA_VERSION |
| 18 | ConfirmCommand.proposal_id | CompleteCommitCommand.pending_action_id | ✓ commands.py:30 |
| 19 | ConfirmCommand.expected_version | CompleteCommitCommand.expected_version | ✓ commands.py |
| 20 | CommitReceipt.commitment_id | Appointment.id | ✓ schema.py:703 |
| 21 | CommitReceipt.proposal_id | PendingAction.id | ✓ schema.py:981 |
| 22 | CommitReceipt.version | PendingAction.version | ✓ schema.py:984 |
| 23 | CommitReceipt.facts | AppointmentConfirmationResult → CommittedFacts | ✓ results.py:113 |
| 24 | CommitReceipt.source | "appointment_service" | ✓ contract-defined |
| 25 | CommitReceipt.committed_at_ns | from confirm timestamp | ✓ contract-defined |

## 3. Confirm outcome matrix verification (13 rows)

| # | Scenario | Expected | Consistent with engine semantics |
|---|---|---|---|
| 1 | propose new key | success, proposal_id, version=1 | ✓ |
| 2 | propose same key same digest | success, same proposal_id | ✓ idempotent |
| 3 | propose same key diff digest | error: idempotency_payload_conflict | ✓ |
| 4 | propose same key diff business | error: idempotency_business_mismatch | ✓ |
| 5 | propose slot booked | error: slot_already_booked | ✓ |
| 6 | confirm valid | success, CommitReceipt | ✓ |
| 7 | confirm already committed same key | success, same receipt | ✓ idempotent |
| 8 | confirm already committed diff key | success, same receipt | ✓ key informational |
| 9 | confirm wrong expected_digest | error: payload_digest_mismatch | ✓ NEW in v5.2 |
| 10 | confirm wrong expected_version | error: version_conflict | ✓ |
| 11 | confirm wrong business | error: business_mismatch, PENDING | ✓ |
| 12 | confirm not found | error: proposal_not_found | ✓ |
| 13 | confirm slot race | error: slot_already_booked, SLOT_TAKEN | ✓ |

## 4. Happy probe fixture

| Turn | Caller text | LLM response | Phase after | Facts changed |
|---|---|---|---|---|
| 1 | "Appointment book pannanum" | "என்ன reason?" | COLLECTING | — |
| 2 | "Scaling" | "எந்த date?" | COLLECTING | reason, service_id=10 |
| 3 | "Naalaikku" | "Dr. Priya 18:30. Time?" | COLLECTING | target_date=2026-08-11 |
| 4 | "6:30" | "பேரு?" | COLLECTING | target_time="18:30", resource_id=1 |
| 5 | "Karthick" | "Scaling, Dr. Priya, நாளை 6:30, Karthick. Correct-ஆ?" | AWAITING_CONFIRMATION | customer_name |
| 6 | "ஆமா" | "Booking confirmed." | TERMINAL | — |

Expected outputs after turn 6:
- engine.proposal_count == 1
- engine.commitment_count == 1
- result.commit_receipt.facts.service_name == "scaling"
- result.commit_receipt.committed_at_ns > 0
- result.commit_receipt.source == "test_engine"
- result.commit_receipt.payload_digest == computed digest
- result.allowed == True (receipt-aware adapter)
- TTS.calls == 1 (for this turn, excluding greeting)
- MediaPort.sent_audio includes exactly 1 response audio (+ greeting)
- result.speech_class == COMMITTED_CREATE

## 5. Adversarial probe fixture

### Part A: concurrent same-slot

- rt1 and rt2 share one TestBookingEngine
- Both collect: resource_id=1, target_date=2026-08-11, target_time="18:30"
- asyncio.Barrier(2) before confirm
- Expected: one commitment_count==1, other gets slot_already_booked
- Both through PipelineRuntime

### Part B: forged/conflicting receipts

| Scenario | Engine/adapter | Expected result |
|---|---|---|
| Wrong payload_digest | ForgedEngine returns digest="FORGED" | Validator #8/#23 BLOCK |
| Wrong business_id | Engine returns business_id=999 | Validator #3 BLOCK |
| committed_at_ns=0 | Engine returns timestamp=0 | Validator #19 BLOCK |
| Wrong version | Engine returns version=99 | Validator #18 BLOCK |
| Wrong source | Engine returns source="fake" | Validator #22 BLOCK |
| Same key diff facts | Engine propose with changed digest | Engine: idempotency_payload_conflict |
| LLM "confirmed" no facts | No AWAITING_CONFIRMATION phase | engine.proposal_count==0 |

All through PipelineRuntime, not engine direct.

## 6. Credential preflight

| Credential | Source | Session status |
|---|---|---|
| SARVAM_API_KEY | /scratch/karthick/fonely/.env | Requires per-command reload |
| CARTESIA_API_KEY | /scratch/karthick/fonely/.env | Requires per-command reload |
| CARTESIA_VOICE_ID | /scratch/karthick/fonely/.env | Requires per-command reload |
| ANTHROPIC_API_KEY | settings.json (dummy) | ✓ Persistent in session |
| ANTHROPIC_BASE_URL | settings.json (AMD gateway) | ✓ Persistent in session |
| ANTHROPIC_CUSTOM_HEADERS | settings.json | ✓ Persistent in session |

Note: Sarvam/Cartesia credentials in `.env` must be loaded per bash invocation since `export` doesn't persist across isolated worktree commands. At runtime launch, a single `source .env` before the server process will make them available for the process lifetime.
