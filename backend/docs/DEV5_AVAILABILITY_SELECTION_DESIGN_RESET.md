# Dev5 Availability Selection Correction Design

**Status:** design only; implementation stopped pending independent approval  
**Current design SHA:** to be filled by this revision's commit  
**Base:** `6a15a4043ac745189a8652f8870380c122971a86`  
**Migration:** none

## Confirmed defects

The existing alternatives flow persists no offered-slot identity, deletes or later reparses date/time context, and selects the first matching time substring without understanding rejection or correction. Offer and selected-ref persistence are independently best-effort, so a caller can hear an offer that is not durably recoverable after restart.

Process-local internal API conversation recovery is an existing separate limitation. This correction does not introduce cross-replica conversation CAS/row-lock architecture. WhatsApp already serializes the tenant/phone path with a PostgreSQL advisory lock.

## Narrow correction

### Typed persisted offer aggregate

Keep one strict, versioned aggregate in the existing conversation JSON facts:

```text
AvailabilityOfferState {
  business_id, conversation_id,
  offer_id, revision, issued_at, expires_at,
  slots[], selected_slot_ref | null
}
```

Each slot contains an opaque random token and trusted server facts: service/resource IDs and names, absolute UTC start/end, business timezone, ordinal, and offer revision. The token carries no facts.

Offer and selected ref restore and validate atomically. If the parent is missing/invalid/tampered, or the ref is orphaned, cross-offer, cross-tenant, cross-conversation, cross-service, or revision-mismatched, clear both and fail closed. No migration or new repository is introduced.

### Whole-utterance selection semantics

Selection evaluates the complete utterance through deterministic/canonical fact resolution, never the first time substring:

- `5 PM doesn't work; anything else?` rejects 5 PM and creates no proposal.
- `not 5 PM, make it 6 PM` selects only a uniquely offered 6 PM.
- Multiple time mentions without a deterministically resolved correction ask for clarification.
- Canonical resolution compares an explicit date restatement to the offer date. The same date plus an offered time (for example, `tomorrow at 5 PM` when the offer is for tomorrow) may select that offered slot; a genuinely different date/service/resource invalidates the whole offer and falls through to canonical fact extraction. It never selects an old-date slot.
- `August 11 at 11 AM`, `15th at 5 PM`, and `option 2 at 5 PM` must not silently select the prior date.
- Exact ordinal/token/time acceptance is valid only when it uniquely belongs to the active offer.

### Durable response ordering

Offer issuance and selection are authoritative conversation turns:

1. Build the offer only from `AvailabilityDecision.alternatives` returned by the authoritative availability service.
2. Persist the complete offer state plus exact alternatives turn in the existing conversation row.
3. Commit the caller/channel transaction.
4. Only after successful commit may the alternatives response be returned or spoken.

Selection follows the same rule: selected ref, proposal ID/version, and exact readback turn must be durably persisted before response. Critical offer/selection persistence failures propagate and roll back; `_persist_turn` may not swallow them. Best-effort persistence may remain only for non-authoritative informational turns if explicitly separated.

### Authoritative slot recheck and proposal

Every proposal path—offer-selected or generic exact date/time—uses the same authoritative lock/recheck boundary. An offer ref remains a quote, not a reservation:

1. For offer selection, verify trusted business/conversation/service/resource/offer/revision membership and expiry. Generic exact-time input resolves candidates through the existing trusted catalogue/date resolver, never raw model IDs.
2. Acquire the existing canonical resource-schedule lock before proposal creation.
3. Re-run `AvailabilityService.check_exact_slot` under that lock for both paths.
4. If a competing booking or schedule mutation won, fail closed, create no Dev5 PendingAction/appointment/allocation, and persist a fresh offer or clarification before response.
5. If still available, call existing `AppointmentService.create_proposal` using the exact trusted slot facts and current existing idempotency/expiry contract. Dev5 does not redesign proposal expiry or confirmation.
6. Clear the offer/ref only after proposal/readback state is durably written.

## Current-channel transaction sequence

For WhatsApp/inbound-worker flow, the existing channel transaction owns conversation processing and commit. Dev5 makes critical offer/selection persistence mandatory within that transaction; the worker commits before enqueueing/sending the response. The process-local internal API limitation is documented but not expanded in this milestone.

## Decisive PostgreSQL proofs

1. **Restart persistence:** offer and alternatives turn commit, process cache clears, fresh session restores and selects the exact slot/date.
2. **Atomic parent/ref validation:** tampered parent, orphan ref, cross-offer ref, cross-tenant ref, and revision mismatch clear both with zero proposal writes.
3. **Whole-utterance semantics:** negation, correction, multiple times, explicit month/date/day changes, and `option 2 at 5 PM` never select the rejected/old-date slot.
4. **Persistence failure:** inject failure before offer-turn commit and during selection/proposal persistence; no response is emitted and no partial offer/ref/proposal/readback survives.
5. **Real overlapping resource-lock proof:**
   - persist an offer;
   - independent booking or schedule-mutation session acquires the canonical resource lock and performs a conflicting change;
   - selection starts in another task/session;
   - fresh observer proves it is blocked and task is incomplete;
   - release and commit the winner;
   - selection resumes, exact-slot recheck rejects, and final state contains zero Dev5 PendingAction/wrong appointment/allocation.
6. **Raw/fabricated selection:** raw model time or fabricated token cannot issue or select an offer; issuance is reachable only from authoritative alternatives.
7. **Lock boundary for all proposal paths:** same offered date+time restatement, true date change, and generic exact date/time each enter the canonical resource lock plus exact-slot recheck. A competing occupation committed while the contender is blocked yields zero proposal for every path.

## Ownership and non-goals

Dev5 owns offer aggregate encoding, conversation selection/invalidation/persistence behavior, and tests. Dev3 retains AppointmentService and confirmation/receipt. No migration, conversation-row CAS/locking redesign, stable proposal-expiry redesign, channel/voice/Exotel/notification/owner-command changes, or self-approval.
