# Contract v4 Self-Check: v3 Verdict → v4 Mapping

## v3 CHANGES_REQUIRED items → v4 location

| # | v3 Verdict Item | v4 Section | Status |
|---|---|---|---|
| 1 | Complete validated CollectedFacts with service ID+name | §CollectedFacts: `service_id`, `service_name` | ✓ |
| 2 | Resource selection policy/ID+name | §CollectedFacts: `resource_id`, `resource_name` + §Resource selection policy | ✓ |
| 3 | Slot identity/version | §CollectedFacts: `slot_id`, `slot_version` | ✓ |
| 4 | Date/time/timezone/duration | §CollectedFacts: `target_date`, `target_time`, `business_timezone`, `duration_minutes` | ✓ |
| 5 | Customer normalized name+E164 | §CollectedFacts: `customer_name` (NFC), `customer_phone` (E164 from ActorContext) | ✓ |
| 6 | TrustedCommandContext actor user ID/role/membership provenance | §TrustedCommandContext: `actor_user_id`, `actor_role`, `actor_membership_provenance` | ✓ |
| 7 | Business/session/conversation/request/attempt IDs | §TrustedCommandContext: all five present | ✓ |
| 8 | Equality rules | §TrustedCommandContext equality rules paragraph | ✓ |
| 9 | Versioned domain-separated canonical digest | §Canonical digest: `"fonely:voice:propose:v{schema_version}:"` prefix | ✓ |
| 10 | Object normalization | §Canonical serialization: sorted keys, ISO dates, NFC strings, no floats/nulls | ✓ |
| 11 | Extra-forbid | §Digest vector: "No extra keys permitted" | ✓ |
| 12 | Literal digest vector | §Digest vector: exact JSON example with all fields | ✓ |
| 13 | Typed receipt facts (not dict) | §CommitReceipt: `facts: CommittedFacts` + §CommittedFacts dataclass | ✓ |
| 14 | Validator-owned operation check | §Validator checks #5: `receipt.operation == binding.operation` | ✓ |
| 15 | Validator-owned status check | §Validator checks #2: `receipt.status == "committed"` | ✓ |
| 16 | Validator-owned proposal+commit IDs | §Validator checks #3,#4: `proposal_id`, `commitment_id` via `receipt is not None` | ✓ |
| 17 | Both idempotency keys | §Validator checks #6,#7: propose+confirm keys | ✓ |
| 18 | Actor/context/request binding | §Validator checks #10,#11,#12: session/conversation/request | ✓ |
| 19 | Full target digest | §Validator checks #8: `payload_digest` | ✓ |
| 20 | Schema version | §Validator checks #9: `schema_version` | ✓ |
| 21 | Optimistic version | §Validator checks #14: `receipt.version > 0` | ✓ |
| 22 | Authentic source | §Validator checks #15: `receipt.source` in allowed sources | ✓ |
| 23 | Freshness | §Validator checks #13: `committed_at_ns > 0` | ✓ |
| 24 | Replay eligibility | §Validator checks #16: generation eligibility via ledger | ✓ |
| 25 | Propose/confirm atomic effects | §Propose/confirm atomic effects: all states defined | ✓ |
| 26 | Exact idempotent replay receipt | §confirm idempotent replay: same CommitReceipt returned | ✓ |
| 27 | Durable outbox/media effect ledger | §Media effect ledger: durable, per generation, outbox owns retry | ✓ |
| 28 | At-most-once per generation not permanent | §Media effect ledger: "at-most-once per generation, not permanent" | ✓ |
| 29 | Concurrent same facts/same proposal replay | §Concurrent probes: same facts same proposal replay | ✓ |
| 30 | Two distinct proposals same slot barrier | §Concurrent probes: barrier before confirm | ✓ |
| 31 | Forged/wrong/stale via PipelineRuntime | §Concurrent probes: forged/wrong/stale section | ✓ |
| 32 | Map production adapter to AppointmentService | §Production adapter mapping: full field mapping | ✓ |
| 33 | Test engine prohibited in production | §Production adapter mapping: "Prohibited in production (enforced by source check)" | ✓ |

## Literal digest vector independent verification

Contract v4 digest vector:
```json
{
  "business_id": 1,
  "business_timezone": "Asia/Kolkata",
  "customer_name": "Karthick",
  "customer_phone": "+919000000000",
  "duration_minutes": 30,
  "resource_id": 1,
  "resource_name": "Dr. Priya",
  "schema_version": 1,
  "service_id": 10,
  "service_name": "scaling",
  "slot_id": "slot-2026-08-11-1830-1",
  "slot_version": 1,
  "target_date": "2026-08-11",
  "target_time": "18:30"
}
```

Verification against CollectedFacts fields:
- `business_id`: from TrustedCommandContext ✓
- `business_timezone`: from CollectedFacts ✓
- `customer_name`: from CollectedFacts ✓
- `customer_phone`: from CollectedFacts ✓
- `duration_minutes`: from CollectedFacts ✓
- `resource_id`: from CollectedFacts ✓
- `resource_name`: from CollectedFacts ✓
- `schema_version`: from ProposeCommand ✓
- `service_id`: from CollectedFacts ✓
- `service_name`: from CollectedFacts ✓
- `slot_id`: from CollectedFacts ✓
- `slot_version`: from CollectedFacts ✓
- `target_date`: from CollectedFacts ✓
- `target_time`: from CollectedFacts ✓

Key sort order: alphabetical ✓
No extra keys beyond CollectedFacts + business_id + schema_version ✓
No nulls (all required) ✓
No floats (duration_minutes is int) ✓

Expected digest (domain-separated):
```
input = "fonely:voice:propose:v1:" + '{"business_id":1,"business_timezone":"Asia/Kolkata",...}'
digest = sha256(input.encode("utf-8")).hexdigest()
```

## Prepared probe inputs and expected states

### PROBE-HAPPY: Full dialogue → receipt

| Turn | Caller STT | LLM Response | DialoguePhase | Facts changed |
|---|---|---|---|---|
| 1 | "Appointment book pannanum" | "என்ன reason?" | COLLECTING | — |
| 2 | "Scaling" | "எந்த date?" | COLLECTING | reason="scaling", service_id=10 |
| 3 | "Naalaikku" | "Dr. Priya 18:30 available. Time?" | COLLECTING | target_date=2026-08-11 |
| 4 | "6:30" | "பேரு?" | COLLECTING | target_time="18:30", resource_id=1 |
| 5 | "Karthick" | "Scaling, Dr. Priya, நாளை 6:30, Karthick. Correct-ஆ?" | AWAITING_CONFIRMATION | customer_name="Karthick" |
| 6 | "ஆமா" | "Booking confirmed." | TERMINAL | — |

Expected after turn 6:
- engine.proposal_count == 1
- engine.commitment_count == 1
- result.commit_receipt is not None
- result.commit_receipt.business_id == 1
- result.commit_receipt.status == "committed"
- result.commit_receipt.facts.service_name == "scaling"
- result.commit_receipt.committed_at_ns > 0
- result.allowed: BLOCK (fail-closed stub) or ALLOW (receipt-aware adapter)

### PROBE-FORGED-DIGEST: Wrong payload_digest

Setup: Same as PROBE-HAPPY but engine replaced with one returning `payload_digest="FORGED"`.

Expected:
- Validator receives receipt with mismatched digest
- Check #8 fails
- result.allowed == False
- No TTS audio

### PROBE-WRONG-BUSINESS: Cross-tenant receipt

Setup: Engine returns receipt.business_id=999, session config business_id=1.

Expected:
- Check #3 fails
- result.allowed == False

### PROBE-STALE: Zero timestamp

Setup: Engine returns receipt.committed_at_ns=0.

Expected:
- Check #13 fails
- result.allowed == False

### PROBE-WRONG-SOURCE: Prohibited source

Setup: Engine returns receipt.source="appointment_service" in test environment where only "test_engine" is allowed (or vice versa).

Expected:
- Check #15 fails
- result.allowed == False

### PROBE-CONCURRENT-SLOT: Barrier before confirm

Setup: Two PipelineRuntime instances share one TestBookingEngine. Both collect facts for same slot. asyncio.Barrier(2) before confirm.

Expected:
- Both propose successfully (different keys)
- One confirms → committed
- Other confirms → slot_taken
- engine.commitment_count == 1

### PROBE-REPLAY-SAME: Idempotent proposal

Setup: Two propose calls with same idempotency_key + same payload_digest.

Expected:
- Same proposal_id returned
- engine.proposal_count == 1

### PROBE-REPLAY-CONFLICT: Same key different facts

Setup: Two propose calls with same idempotency_key but different payload_digest.

Expected:
- Second returns error "idempotency_payload_conflict"
- engine.proposal_count == 1

### PROBE-NO-CONFIRM: LLM text without user confirmation

Setup: User says "hello", LLM says "Booking confirmed."

Expected:
- DialoguePhase != AWAITING_CONFIRMATION
- Command NOT invoked
- engine.proposal_count == 0

### PROBE-GENERATION-REPLAY: Same receipt different generation

Setup: Turn 6 confirms and receipt emitted for generation_id=6. Turn 7 in same session with new generation_id=7 and same receipt.

Expected:
- Turn 6: ledger records (commitment_id, version, generation_id=6)
- Turn 7 generation_id=7: eligible (new generation)
- Turn 7 generation_id=6 again: BLOCK (already emitted)
