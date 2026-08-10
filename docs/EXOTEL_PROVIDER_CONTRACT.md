# Exotel Provider Contract — Fonely Inbound Adapter

Status: DRAFT — pending sandbox verification and independent review.
Owner: Dev1. Route remains disabled/feature-gated until full acceptance.
Base: authoritative main 4c765c6.

---

## 1. Primary Documentation Evidence

Sources consulted (2026-08-10):

- `developer.exotel.com/api/make-a-call-api` — provisioning parameters
- `developer.exotel.com/docs/voice-v1/api-reference/call-details` — response fields
- `developer.exotel.com/docs/voice-v1/api-reference/connect-two-numbers` — callback provisioning
- `developer.exotel.com/docs/references/authentication` — API auth

### What the documentation covers

- REST API authentication (HTTP Basic with API key/token)
- Call provisioning with StatusCallback URL, events, and content type
- Call status values: queued, in-progress, completed, failed, busy, no-answer
- Response field `Sid` as unique alpha-numeric call identifier
- `StatusCallbackEvents` accepts: `terminal`, `answered`, or both
- `StatusCallbackContentType` accepts: `multipart/form-data` or `application/json`
- `CustomField` (max 128 chars) passed to StatusCallback and applets
- `Duration` updates asynchronously (~2 minutes after call ends)
- `ConversationDuration` in Call Details as connected-party seconds
- `Direction`: inbound, outbound-dial, outbound-api
- `EventType` distinguishes answered and terminal events in callbacks

### What the documentation does NOT cover

- Callback payload schema (no field list for what Exotel POSTs)
- Default StatusCallbackContentType (not specified; likely multipart/form-data)
- Callback authentication, signatures, or source verification
- Callback retry/replay/ordering/deduplication semantics
- IP ranges for Exotel callback sources
- Callback timeout behavior
- Whether CallSid in callbacks equals Sid from the API response
- Mapping of status values to answered/terminal EventType categories

---

## 2. Callback Provisioning Contract

### Provisioning at call creation

When Fonely initiates an outbound call via Exotel's Make a Call API, it
MUST provision:

```
StatusCallback=<fonely_callback_url>
StatusCallbackEvents=terminal,answered
StatusCallbackContentType=application/json
CustomField=<business_id>:<call_correlation_id>
```

Rationale:
- `application/json` is explicitly supported and eliminates multipart parsing
- Both `terminal` and `answered` give full lifecycle visibility
- `CustomField` carries tenant correlation without relying on callback
  field identity (which is undocumented)

### For inbound calls (Exotel Applet-based routing)

Exotel routes inbound calls to Fonely's virtual number through Applets.
The Applet StatusCallback configuration is set in the Exotel dashboard,
not via API. The same callback URL and JSON content type apply.

---

## 3. Canonical Typed Event Model

Since Exotel does not document the callback payload schema, this contract
defines the expected fields based on the documented API response fields
and common Exotel integration patterns. Each field is marked with its
evidence source.

### Expected callback fields (JSON)

```python
@dataclass(frozen=True)
class ExotelCallbackEvent:
    # Provider identity
    CallSid: str          # Documented as Sid in API response; assumed
                          # identical in callback (UNVERIFIED — requires
                          # sandbox confirmation)

    # Event classification
    EventType: str        # "terminal" or "answered" (from StatusCallbackEvents)
    Status: str           # One of the documented status values

    # Parties
    From: str             # Calling party (documented in API response)
    To: str               # Called party (documented in API response)

    # Timing (terminal events only)
    Duration: str | None          # Total seconds (documented; updates async)
    ConversationDuration: str | None  # Connected seconds (from Call Details)
    StartTime: str | None         # ISO or Exotel timestamp format
    EndTime: str | None           # ISO or Exotel timestamp format

    # Correlation
    CustomField: str | None       # Fonely-supplied tenant correlation

    # Undocumented but commonly observed
    Direction: str | None         # inbound | outbound-dial | outbound-api
    ParentCallSid: str | None     # Null for top-level calls
```

### Status value taxonomy

| Status        | EventType   | Terminal? | Billable? |
|---------------|-------------|-----------|-----------|
| `in-progress` | `answered`  | No        | Yes       |
| `completed`   | `terminal`  | Yes       | Yes       |
| `failed`      | `terminal`  | Yes       | No        |
| `busy`        | `terminal`  | Yes       | No        |
| `no-answer`   | `terminal`  | Yes       | No        |
| `queued`      | Neither     | No        | No        |

Note: This mapping is INFERRED from the documented status values and
EventType options. It has not been confirmed against actual Exotel
callback behavior.

### Multipart/form-data normalization

If the default StatusCallbackContentType turns out to be multipart
(sandbox evidence required), the adapter MUST:

1. Accept both `multipart/form-data` and `application/json`
2. Normalize multipart fields into the same typed event model
3. Treat all field values as strings (multipart has no type system)
4. Parse numeric fields (Duration, ConversationDuration) explicitly
5. Log the received content type for contract monitoring

---

## 4. Provider Authentication Assessment

### Current state: NO provider-native callback authentication

Exotel's documentation provides:
- HTTP Basic auth for outbound API calls (Fonely → Exotel)
- No documented mechanism for authenticating inbound callbacks (Exotel → Fonely)

Specifically absent:
- No HMAC/signature header on callbacks
- No documented source IP ranges
- No mutual TLS
- No callback-specific auth tokens
- No nonce/timestamp replay protection

### Compensating controls (required before production)

Since Exotel provides no native callback verification, Fonely MUST
implement one of these trust boundaries:

#### Option A: Reverse-proxy/gateway header injection (RECOMMENDED)

Deploy the callback endpoint behind a reverse proxy (nginx/Cloudflare/
AWS ALB) that:

1. Restricts source IPs to Exotel's published or observed ranges
   (must be confirmed with Exotel support — not documented)
2. Injects a gateway-verified header (e.g., `X-Gateway-Verified: true`)
3. The adapter checks this header before processing

Advantages: No Exotel cooperation needed; defense-in-depth with the
existing interim shared-secret; IP restriction is the strongest
available control.

#### Option B: Exotel-side custom header (if supported)

Some Exotel plans or integrations allow setting custom headers on
callbacks. If available:

1. Configure a high-entropy secret header in the Exotel dashboard
2. The adapter verifies it with constant-time comparison

This requires Exotel account investigation and is plan-dependent.

#### Option C: Correlation-based verification

Use the `CustomField` round-trip to verify callbacks:

1. On call creation, generate a cryptographic nonce and store it
   keyed by CallSid
2. Set `CustomField` to include the nonce
3. On callback, verify the nonce matches the stored value for that CallSid
4. Reject callbacks with unknown/expired/mismatched nonces

Advantages: Works without infrastructure changes.
Disadvantages: Only works for outbound calls Fonely initiated;
inbound calls routed by Exotel Applets may not carry the nonce.

### Interim defense (already implemented in accepted SHA 86beb13)

The existing `X-Exotel-Webhook-Secret` shared-secret header provides
possession-based authentication. It is:
- NOT provider-native (Exotel does not send it)
- NOT replay-safe
- Only effective if the callback provisioning or gateway injects it

This is retained as an innermost layer; the gateway/IP control above
is the outer boundary.

---

## 5. CallSid Identity and Idempotency

### CallSid as the provider call identity

`Sid` is documented as the "Unique alpha-numeric call identifier" in
API responses. This contract assumes the callback carries this value
as `CallSid` (UNVERIFIED — sandbox confirmation required).

### Idempotency contract

```
UNIQUE constraint: (business_id, provider_call_sid)
```

- Every callback is processed idempotently by (business_id, CallSid)
- Duplicate callbacks for the same CallSid update state monotonically
  (never revert to an earlier status)
- Unknown CallSid values for outbound calls are rejected
  (correlation verification via stored nonce)
- Unknown CallSid values for inbound calls are accepted
  (the provider assigns the identity; Fonely has no prior record)

### State transition rules

```
                    ┌─────────┐
                    │ ringing │ (Fonely internal; not from callback)
                    └────┬────┘
                         │ answered callback
                         ▼
                    ┌─────────────┐
                    │ in-progress │
                    └──────┬──────┘
                           │ terminal callback
              ┌────────────┼────────────────┐
              ▼            ▼                ▼
         ┌─────────┐ ┌────────┐     ┌──────────┐
         │completed│ │ failed │     │busy/no-ans│
         └─────────┘ └────────┘     └──────────┘
```

Transition rules:
- Forward-only: a terminal status is never overwritten
- Answered before terminal: if `completed` arrives without prior
  `answered`/`in-progress`, accept it (the answered callback may
  have been lost)
- Duplicate terminal: idempotent no-op (same status) or reject
  (different terminal status — log as provider anomaly)
- Out-of-order: terminal before answered for the same CallSid
  means the answered callback was lost or delayed; the terminal
  status takes precedence

### Replay and duplicate semantics

Since Exotel does not document retry behavior:
- Assume callbacks MAY be retried on timeout
- Assume callbacks MAY arrive out of order
- Assume callbacks MAY arrive duplicated
- The adapter MUST handle all three safely via idempotent
  state transitions keyed on (business_id, CallSid, Status)

---

## 6. Duration and Identity Validation

### Duration fields

- `Duration`: total call duration in seconds. May update
  asynchronously (~2 minutes after call ends per documentation).
  Accept only non-negative integer values.
- `ConversationDuration`: connected-party duration in seconds.
  Must be <= Duration when both are present. Accept only
  non-negative integer values.
- Either field may be absent, null, empty string, or "0" on
  non-terminal or failed calls.

### Caller/callee identity

- `From` and `To` are phone numbers in provider format
  (may include country code, may not be E.164)
- The adapter normalizes to E.164 before storage
- Phone numbers are NEVER logged in full; last 4 digits only
  in diagnostic telemetry

### Business identity

- `To` (for inbound) or `From` (for outbound) maps to business_id
  via ExotelNumberMapping
- Unmapped numbers produce a controlled 404; no provider data is
  logged (consistent with accepted adapter behavior)

---

## 7. Ingress Bounds and Resource Protection

### Request limits

| Control                     | Value    | Rationale                         |
|-----------------------------|----------|-----------------------------------|
| Body size                   | 64 KiB   | Callback payloads are small       |
| Content-Type                | JSON or multipart/form-data | Per provisioning |
| Request timeout             | 10s      | Callback processing should be fast|
| Concurrent callback limit   | Per-IP rate limit via middleware  |         |

### ASGI-level protections (from accepted adapter)

- Bounded body reader: pre-copy size check per chunk
- Content-type enforcement before body parse
- Auth check before any body consumption
- Typed read outcomes (oversize/disconnect/malformed/ok)

### What this contract does NOT bound

- Upstream ASGI server memory materialization (httptools/uvicorn)
- Total concurrent connections (deployment infrastructure concern)
- Network-level DDoS (load balancer/CDN concern)

---

## 8. Privacy-Safe Telemetry

### What is logged

| Field                | Logged? | Format                          |
|----------------------|---------|---------------------------------|
| CallSid              | Yes     | Full (provider-assigned opaque) |
| business_id          | Yes     | Integer                         |
| EventType            | Yes     | String                          |
| Status               | Yes     | String                          |
| Duration             | Yes     | Integer                         |
| Direction            | Yes     | String                          |
| From/To phone        | NO      | Never logged                    |
| CustomField          | NO      | May contain tenant data         |
| Auth header value    | NO      | Never logged                    |
| Raw request body     | NO      | Never logged                    |
| Caller IP            | NO      | Rate limiting only              |

### Telemetry events

```
exotel_callback_received    — business_id, event_type, status, call_sid
exotel_callback_processed   — business_id, call_sid, transition
exotel_callback_rejected    — reason (auth/parse/unknown/duplicate)
exotel_callback_anomaly     — type (out_of_order/impossible_transition)
```

---

## 9. Contract Fixtures

### Fixture requirements (sandbox-sourced preferred)

Each fixture MUST be:
- Sourced from actual Exotel sandbox output where possible
- Stripped of real phone numbers and account identifiers
- Representative of one specific callback scenario
- Machine-parseable as the typed event model

### Required fixture scenarios

| ID   | Scenario                                    | EventType | Status    |
|------|---------------------------------------------|-----------|-----------|
| FX-01| Outbound call answered                      | answered  | in-progress|
| FX-02| Outbound call completed normally             | terminal  | completed |
| FX-03| Outbound call failed (network)               | terminal  | failed    |
| FX-04| Outbound call busy                           | terminal  | busy      |
| FX-05| Outbound call no-answer                      | terminal  | no-answer |
| FX-06| Inbound call answered                        | answered  | in-progress|
| FX-07| Inbound call completed                       | terminal  | completed |
| FX-08| Duplicate terminal callback (same status)    | terminal  | completed |
| FX-09| Out-of-order: terminal before answered        | terminal  | completed |
| FX-10| Multipart/form-data callback                 | terminal  | completed |
| FX-11| JSON callback with missing optional fields   | terminal  | completed |
| FX-12| Malformed Duration (negative/non-numeric)    | terminal  | completed |

### Fixture source tracking

Until sandbox access is available, fixtures are synthetic and marked:
```
source: synthetic | sandbox
verified_against: <exotel_account_sid or "pending">
captured_at: <ISO timestamp or "pending">
```

---

## 10. Adapter → Durable Inbox Integration

### Functional proof requirement

The Exotel adapter MUST prove that an authenticated, validated callback
results in exactly one durable event in the system, following the
established WhatsApp inbound pattern:

1. Authenticate request (shared-secret + gateway verification)
2. Parse and validate typed event
3. Persist durable event BEFORE returning 200
4. Return 200 only after durable persistence succeeds
5. Background worker claims and processes the event
6. Failed processing retries via the durable inbox

### Integration with Dev2's durable inbox (accepted 7b09b55)

The Exotel adapter follows the same pattern as WhatsApp:
- `InboundEventRepository` for durable persistence
- `InboundWorker` for claiming and processing
- Atomic processing with rollback on failure
- Dead-letter after max retries

### What this contract does NOT cover

- Voice pipeline integration (Dev4 scope)
- Audio stream WebSocket authentication (separate contract)
- Owner command routing from voice calls
- Conversation service integration for voice

---

## 11. Threat Model

### Threat: Forged callbacks

- Attack: Attacker sends crafted callback to create/modify call records
- Mitigation: Shared-secret header + gateway IP restriction + correlation nonce
- Residual risk: If gateway is bypassed and secret is compromised

### Threat: Replay attacks

- Attack: Attacker replays a legitimate callback to re-trigger processing
- Mitigation: Idempotent state transitions; duplicate callbacks are no-ops
- Residual risk: First delivery of a forged callback with valid format

### Threat: Information disclosure via error responses

- Attack: Attacker probes the callback endpoint for information
- Mitigation: Generic error responses (401/400/404); no provider data in responses
- Residual risk: Timing differences between auth failure and parse failure

### Threat: Resource exhaustion

- Attack: High-volume callback flood
- Mitigation: Body size limit, rate limiting, auth-first processing
- Residual risk: Application-layer DDoS above rate limit threshold

### Threat: Phone number harvesting via logs

- Attack: Compromise log storage to extract customer phone numbers
- Mitigation: Phone numbers never logged; CallSid is opaque provider identity
- Residual risk: None for phone numbers; CallSid correlation requires provider access

---

## 12. Rollout and Operator Runbook

### Pre-enablement checklist

- [ ] Sandbox-verified callback fixtures replace synthetic ones
- [ ] Gateway IP restriction configured and tested
- [ ] Shared-secret provisioned via secret manager (>= 32 ASCII chars)
- [ ] Readiness check includes Exotel adapter health
- [ ] Monitoring dashboard includes Exotel telemetry events
- [ ] Durable inbox integration tested end-to-end
- [ ] Feature gate verified: route absent without configuration

### Enablement sequence

1. Deploy with `exotel_webhook_secret` empty (route disabled)
2. Configure gateway IP restriction for Exotel source IPs
3. Set `exotel_webhook_secret` to high-entropy value
4. Verify `/health/ready` returns 200
5. Configure Exotel StatusCallback in dashboard/API
6. Send test call; verify durable event created
7. Monitor telemetry for anomalies

### Incident response

| Symptom                        | Action                              |
|--------------------------------|-------------------------------------|
| All callbacks returning 401    | Check secret rotation; verify gateway header |
| Callbacks returning 404        | Check ExotelNumberMapping configuration |
| Duplicate call records         | Check CallSid idempotency constraint |
| Missing call completions       | Check Exotel callback delivery; use Call Details API to reconcile |
| Duration always null           | Normal for ~2 min after call; check after delay |

### Secret rotation procedure

1. Generate new secret (>= 32 random ASCII chars)
2. Update secret manager
3. Rolling restart (new secret active on all instances)
4. Update gateway header injection with new value
5. Verify callbacks succeed with new secret
6. Remove old secret from all systems

---

## 13. Implementation Architecture

### File boundary (this contract)

```
backend/src/fonely/api/channels/exotel.py     — HTTP adapter (auth + parse + persist)
backend/src/fonely/domain/calls/events.py      — Typed ExotelCallbackEvent model
backend/src/fonely/domain/calls/transitions.py — State transition validation
backend/src/fonely/repositories/calls.py       — Durable call event persistence
backend/tests/unit/channels/test_exotel.py     — Adapter unit tests
backend/tests/fixtures/exotel_callbacks/        — Contract fixtures (JSON)
docs/EXOTEL_PROVIDER_CONTRACT.md               — This document
```

### NOT in scope (other owners)

```
backend/src/fonely/workers/                    — Inbound worker (Dev2 pattern)
backend/src/fonely/services/owner_commands.py  — Owner command routing
backend/src/fonely/services/conversation.py    — Conversation integration
src/                                           — Voice/audio pipeline (Dev4)
.github/workflows/                             — CI evidence system (Dev2)
```

### Design principles

1. Parse, don't validate: use Pydantic for typed event construction
2. Persist before respond: 200 only after durable write
3. Idempotent by identity: (business_id, CallSid) is the natural key
4. Forward-only state: terminal statuses are permanent
5. Privacy by default: phone numbers never reach logs
6. Feature-gated: route absent without valid configuration

---

## 14. Open Questions (Require Sandbox or Exotel Support)

| ID | Question | Impact | Resolution path |
|----|----------|--------|-----------------|
| OQ-1 | Exact callback payload field names | Typed event model accuracy | Sandbox test call |
| OQ-2 | Default StatusCallbackContentType | Whether multipart parsing is required | Sandbox or Exotel support |
| OQ-3 | CallSid in callback == Sid in API response | Identity correlation | Sandbox test call |
| OQ-4 | Exotel callback source IP ranges | Gateway IP restriction | Exotel support ticket |
| OQ-5 | Callback retry behavior on timeout | Idempotency requirements | Sandbox timeout test |
| OQ-6 | Custom header support on callbacks | Auth option B viability | Exotel plan review |
| OQ-7 | Callback delivery ordering guarantee | Out-of-order handling necessity | Exotel support |
| OQ-8 | Inbound call CustomField availability | Correlation nonce for inbound | Applet configuration review |

### Sandbox verification plan

1. Create Exotel sandbox account
2. Provision outbound call with StatusCallback and JSON content type
3. Capture exact callback payload for each status transition
4. Verify CallSid matches API response Sid
5. Test duplicate/delayed callback behavior
6. Test multipart default (omit StatusCallbackContentType)
7. Document source IP from callback requests
8. Verify CustomField round-trip
