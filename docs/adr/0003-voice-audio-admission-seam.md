# Voice audio admission must be tenant-bound by database state

Status: accepted, not yet implemented
Date: 2026-08-12

## The gap

`/webhooks/exotel/audio-stream` authenticates and then drains. The
authentication is a shared secret, so it proves *Exotel is calling*, not
*which clinic this call belongs to*. The handler has no `business_id`, no
`call_id`, and no caller. A voice runtime mounted here would have nothing to
scope a doctor lookup or a booking to.

The obvious fix — read the tenant out of the stream's opening frame — is the
thing we have banned everywhere else: caller-supplied identity is never
authoritative. Anyone holding the media-stream token could then open a
session against any clinic.

## Decision

Admission resolves the tenant from state the server itself wrote, and
refuses when it cannot.

1. **Channel identity moves into the database.** `ExotelNumberMapping` reads
   `EXOTEL_NUMBER_MAPPINGS` from the environment — the identical defect
   migration 0016 removed from WhatsApp, still live on the path the dentist
   will actually call. A bad parse falls back to `{}`, so every call 404s and
   nothing distinguishes that from an unknown number. Two tenants can claim
   one number with nothing to stop them.

   The replacement is generic, not a second `business_exotel_channels`: one
   table keyed on `(provider, external_identifier)` with a global unique
   constraint, so a provider number belongs to exactly one tenant by
   construction. WhatsApp's 0016 table is the same shape and should fold into
   it rather than be duplicated per provider — the receptionist core is
   generic and channel identity is the clearest place that must stay true.

2. **Calls carry the provider's own call id.** `calls` has no `call_sid`
   column, so `call_status_webhook` correlates a completed callback by
   "latest live call for this phone", which misroutes the moment one caller
   has two calls in flight. A durable `provider_call_sid` makes both the
   completion update and the audio admission exact rather than heuristic.

3. **No admission without a prior server-observed ringing webhook.** The
   opening frame is read for its CallSid only — a lookup key, never a fact.
   That key selects the `calls` row the ringing webhook already wrote, and
   the tenant comes from that row. No row, no session: close 1008. An
   attacker holding the token still cannot conjure a call against a clinic
   they do not own.

4. **The runtime mounts behind the seam, not beside it.** The handler hands a
   typed `AudioSession(business_id, call_id, caller_phone)` to a pluggable
   consumer. Dev4's Pipecat pipeline becomes one implementation; the drain
   loop stays the default. The socket is never accepted before the tenant is
   known.

## Consequences

Two migrations, both taken from the live head. `0017` was previously spoken
for by Dev1's unlanded Exotel work; that milestone is rejected and unmerged,
and reserving numbers for work that is not on disk is exactly what produced
the false "0016 is reserved" claim this codebase already paid for. Dev1
rebases onto whatever head exists when the milestone is resubmitted.

The unconfigured case stays fail-closed and gets *louder*: an unregistered
number is now distinguishable from a misconfigured mapping, because there is
no parse step left to fail silently.
