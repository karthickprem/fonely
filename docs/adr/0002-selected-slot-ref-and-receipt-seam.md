# ADR 0002 — SelectedSlotRef and Committed Receipt Seam

- Status: ACCEPTED (Chief Architect, 2026-08-10)
- Scope: conversation/voice runtime -> appointment application seam
- Does NOT broaden the current 0015 review round

## Context

Karthick reproduced a live voice defect: the caller said "today", the agent
offered alternatives, and the next utterance was re-interpreted against
`now.date()` — so the agent asked for the date again.

Root cause in `backend/src/fonely/services/conversation.py`:
`_check_availability_and_propose` renders alternatives to free text, deletes
`collected_facts['start_at']`, and retains no offered-slot identity. The
following turn therefore re-extracts time with no memory of what was offered.

Separately, `ConversationTurn` carries proposal id/version but no committed
receipt, and `_confirm_booking` speaks generic success. In the legacy
`voice-lab/booking_pipeline.py` runtime the command port is declared but never
called, so a model can speak "Booking confirm ஆயிடுச்சு" with no receipt at all.

## Decision

### AvailabilityOffer

An alternatives response persists an active offer:

```
AvailabilityOffer { offer_id, revision, expires_at, slots[] }
```

- The next utterance ("5:00") selects **only** from the active offer. It must
  never be reinterpreted against `now.date()`.
- Correction of date, service, or resource **invalidates the entire offer** and
  any selected ref.
- Ambiguous time (two 5:00 slots, or two resources) asks for clarification —
  never guesses.
- An expired or drifted offer triggers a fresh authoritative query and a new
  readback. Never silent rebinding.
- Do **not** delete date context when offering alternatives.

### SelectedSlotRef

Contains an opaque `slot_token` plus trusted display facts:

- business / service / resource IDs
- absolute start and end
- business timezone
- offer/availability revision
- expiry

The token is **server-generated** over those canonical facts and the
schedule/resource revision. The model or caller cannot construct or alter it.

It is a short-lived **quote** — not a reservation, and not proof the slot is
still free. Proposal creation validates token, tenant, and expiry, then
**rechecks authoritative availability under deterministic locks**.

### Receipt notification state

`queued | not_queued | not_applicable`, with these meanings binding:

- `queued` — the required outbox intent is durably visible **in the same
  committed transaction**.
- `not_queued` — permitted **only** for an explicitly optional notification, or
  a separately committed operation whose contract allows notification failure
  and says so truthfully.
- If notification is **required** for the operation, outbox failure rolls the
  operation back. A committed receipt with `not_queued` must therefore be
  impossible for such operations.
- `not_applicable` — policy-defined.
- `sent` / `delivered` — require durable provider-attempt or delivery evidence.
  **Never inferred from outbox creation.**

### Receipt return path

- Normal confirm returns the speakable **immutable committed receipt**.
- Recovery lookup exists only for lost ACK / reconnect. It returns the *same*
  receipt and never reconstructs mutable facts.
- Success TTS is gated on the receipt: no receipt, no success speech.

## Consequences

- Dev4 builds the test-only conformance fake against this contract.
- Application implementation is later Dev3-owned work; it is not in 0015.
- The immutable-fact rule here is the same one violated at
  `backend/src/fonely/services/appointments.py:799` (fixed at `374e07f`):
  old facts come from the immutable `before_snapshot`, new facts from committed
  after-state, never from post-UPDATE ORM identity-map state.
