# Dev5 Availability Selection Design Reset

**Status:** design only; implementation stopped pending approval  
**Durable WIP SHA:** `6363361`

## Problem and aggregate

Process-local conversation locks cannot serialize selection across replicas, and substring time matching cannot distinguish acceptance from rejection/correction. PostgreSQL `conversations` is the aggregate root. Its JSON facts hold one strict versioned `AvailabilitySelectionState`: conversation/business identity, offer ID/revision/expiry, slots, selected token, selection status, proposal ID/version/expiry, and a conversation revision.

Parent offer and selected ref validate atomically. Missing, malformed, cross-tenant, cross-conversation, cross-offer, or revision-mismatched state clears both and fails closed.

## Serialized selection command

`SelectAvailabilityOfferCommand` contains trusted business/actor, conversation ID, expected conversation/turn revision, offer ID/revision, trusted request ID, and raw utterance.

In one caller-owned transaction:

1. Load `(business_id, conversation_id) FOR UPDATE`; verify actor/subject and expected revision.
2. Validate the persisted aggregate.
3. Classify the whole utterance as exact acceptance, rejection, correction, or ambiguity. Negation/correction takes precedence over time matching. Multiple times require deterministic correction relation or clarification.
4. Explicit date/service/resource changes invalidate the whole offer and return to canonical fact resolution. They never match the old offer.
5. Unique acceptance verifies token membership and expiry.
6. Lock order: conversation → resource schedule → domain rows.
7. Re-run authoritative exact-slot availability. The offer is not a reservation.
8. Create proposal with semantic identity bound to business, conversation, offer ID/revision, slot token, exact slot facts, and selection attempt. Proposal expiry is persisted/derived once from offer-selection state, never recomputed from current time on replay.
9. Same offer/same slot duplicate returns identical proposal ID/version/expiry/readback. Same offer/different slot conflicts and reloads the winner.
10. Persist selection state, proposal facts, turn, and exact readback atomically; commit before returning/speaking.

Offer issuance is likewise atomic: lock conversation, query authoritative availability, persist complete offer plus alternatives turn, commit, then speak. Critical offer/selection persistence failures propagate and roll back; they are never swallowed by best-effort `_persist_turn`.

## Utterance rules

- `5 PM doesn't work; anything else?` rejects 5 PM and creates no proposal.
- `not 5 PM, make it 6 PM` may select only a uniquely offered 6 PM.
- `August 11 at 11 AM`, `15th at 5 PM`, and `option 2 at 5 PM` cannot silently select an old-date slot.
- Raw/model times or fabricated tokens cannot issue/select an offer.
- Ambiguous same-wall-time choices ask for ordinal/resource clarification.

## Failure and recovery

Any persistence, availability, or proposal error rolls back selection/readback. A restart after committed offer restores the aggregate. A lost response replays via trusted request/semantic identity and returns the immutable existing proposal/readback. Dev5 stops at proposal; B3/Dev3 owns confirmation and committed receipt.

## Decisive PostgreSQL probes

1. Offer issuance commit then process restart/fresh session selection.
2. Persistence failure before offer response: no spoken response and no offer/turn row.
3. Atomic parent/ref tamper cases: orphan, cross-offer, cross-tenant, revision mismatch clear both.
4. Whole-utterance negation/correction/date-change cases above.
5. Two independent selectors, same offer/same slot: deterministic overlap/row lock, one proposal, loser returns identical proposal/readback/expiry.
6. Two selectors, same offer/different slots: one winner; loser conflicts/reloads, never returns local alternative.
7. Independent competing booking or schedule mutation holds canonical resource lock; selection blocks; observer proves non-completion; after release recheck fails closed with zero Dev5 PendingAction/wrong appointment/allocation.
8. Inject selection/proposal persistence failures and prove rollback plus outer transaction usability.

## Ownership

Dev5 owns selection application logic, conversation aggregate serialization, and tests. Dev3 retains AppointmentService/confirmation. No migration, channel, voice, Exotel, notification, or owner-command changes. Any needed repository method for tenant-scoped conversation `FOR UPDATE` requires CEO-approved ownership expansion.
