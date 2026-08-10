# Canonical Command/Receipt Dataflow Contract

## Happy path: user confirms a booking

```
1. Caller says "ஆம், confirm பண்ணுங்க" after a readback turn
2. STT → "ஆம், confirm பண்ணுங்க"
3. Runtime checks: dialogue.state == awaiting_confirmation
4. Runtime builds ProposeCommand:
   - context: TrustedCommandContext(business_id, session_id, conversation_id, booking_attempt)
   - target facts: service_id, resource_id, target_date, target_time, customer_name
     (accumulated from prior dialogue turns, NOT extracted from this LLM response)
   - idempotency_key: "voice-{session_id}-a{booking_attempt}"
   - payload_digest: sha256(canonical(target_facts))
5. CommandPort.propose(cmd) → CommandResult(proposal_id=N)
6. Runtime builds ConfirmCommand:
   - context: same TrustedCommandContext
   - proposal_id: N
   - idempotency_key: "voice-{session_id}-a{booking_attempt}-confirm"
7. CommandPort.confirm(cmd) → CommandResult(committed=True, receipt=CommitReceipt(...))
8. Receipt validation:
   - receipt.business_id == config.business_id
   - receipt.proposal_id == proposal_id from step 5
   - receipt.payload_digest == digest from step 4
   - receipt.committed_at_ns > 0
9. LLM generates confirmation response (this is consequential speech)
10. Speech classifier: COMMITTED_CREATE
11. Validator receives (text, speech_class, receipt):
    - Fail-closed stub: BLOCK (correct until accepted validator)
    - Future accepted validator: verify receipt binding → ALLOW
12. If ALLOW: TTS synthesize → audio in TurnResult.response_audio
13. If BLOCK: no audio, blocked_reason in TurnResult
14. Dialogue transitions to terminal
```

## Command invocation gate

Commands fire ONLY when ALL conditions hold:
- dialogue.state == "awaiting_confirmation" (user explicitly confirmed a readback)
- collected_facts has all required fields (reason, date, time, name for booking)
- session_mode == "live" AND command_port is not None
- NOT when LLM output merely contains commit vocabulary

## Receipt binding invariants

CommitReceipt must satisfy:
- commitment_id: unique, monotonic
- proposal_id: matches the proposal from this turn
- business_id: matches session config (cross-tenant = discard)
- operation: matches the intent (create/cancel/reschedule)
- idempotency_key: matches the propose key
- confirm_idempotency_key: matches the confirm key
- payload_digest: matches the digest of proposed target facts
- committed_at_ns: > 0, monotonic
- facts: contains exactly the proposed target data

## Adversarial rejection invariants

- Forged receipt (wrong commitment_id/business_id): runtime discards, speech BLOCK
- Wrong-business receipt: runtime discards at binding check
- Stale receipt (committed_at_ns == 0 or missing): runtime discards
- Digest conflict (payload_digest != proposed): runtime discards
- Replay (same idempotency_key, different facts): engine returns idempotency_payload_conflict
- Cross-tenant (business_id mismatch on engine): engine returns business_mismatch
- Duplicate confirm (idempotent): engine returns same receipt
- No user confirmation (dialogue not awaiting): command NOT invoked

## What must NOT happen

- Command invoked because LLM text contains "confirmed" without user confirmation
- Receipt fabricated without engine state change
- Consequential speech relabeled to NON_CONSEQUENTIAL
- TTS called twice for same turn
- Audio sent without validator ALLOW
- Terminal cause overwritten
- Provider exception swallowed without fail-closed result
