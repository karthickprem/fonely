# Exotel Durable Intake — Migration & Worker Design

Status: ISOLATED DESIGN — not applied. Pending Dev3 0015 integration and
known Alembic head.
Owner: Dev1. No competing migration revision created.

---

## Migration: exotel_inbound_events table

Follows the WhatsAppInboundEvent pattern (migration 0012). Revision number
assigned only after Dev3's 0015 is integrated and head is known.

### Schema

```sql
CREATE TABLE exotel_inbound_events (
    id              SERIAL PRIMARY KEY,
    call_sid        VARCHAR(100) NOT NULL,
    business_id     INTEGER NOT NULL REFERENCES businesses(id),
    event_type      VARCHAR(20) NOT NULL,  -- 'answered' | 'terminal'
    status          VARCHAR(20) NOT NULL,  -- documented Exotel statuses
    caller_phone    VARCHAR(20) NOT NULL,
    called_number   VARCHAR(20) NOT NULL,
    duration        INTEGER,
    conversation_duration INTEGER,
    direction       VARCHAR(20),
    custom_field    VARCHAR(200),
    payload_digest  VARCHAR(64) NOT NULL,

    -- Durable inbox state
    intake_status   VARCHAR(20) NOT NULL DEFAULT 'received',
    attempts        INTEGER NOT NULL DEFAULT 0,
    max_attempts    INTEGER NOT NULL DEFAULT 5,
    next_attempt_at TIMESTAMPTZ,
    claim_token     UUID,
    claim_version   INTEGER NOT NULL DEFAULT 1,
    claimed_at      TIMESTAMPTZ,
    lease_expires_at TIMESTAMPTZ,

    -- Timestamps
    provider_timestamp TIMESTAMPTZ,
    received_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,
    dead_lettered_at TIMESTAMPTZ,

    -- Constraints
    CONSTRAINT uq_exotel_inbound_call_event
        UNIQUE (business_id, call_sid, event_type),
    CONSTRAINT ck_exotel_inbound_status_valid
        CHECK (intake_status IN ('received', 'processing',
               'domain_processed', 'completed', 'failed', 'dead_letter')),
    CONSTRAINT ck_exotel_inbound_attempts_non_negative
        CHECK (attempts >= 0),
    CONSTRAINT ck_exotel_inbound_attempts_bounded
        CHECK (attempts <= max_attempts),
    CONSTRAINT ck_exotel_inbound_claim_consistency
        CHECK (
            (intake_status = 'processing' AND claim_token IS NOT NULL
             AND claimed_at IS NOT NULL AND lease_expires_at IS NOT NULL)
            OR (intake_status != 'processing' AND claim_token IS NULL
                AND claimed_at IS NULL AND lease_expires_at IS NULL)
        )
);

CREATE INDEX ix_exotel_inbound_events_poll
    ON exotel_inbound_events (intake_status, next_attempt_at);
CREATE INDEX ix_exotel_inbound_events_call_sid
    ON exotel_inbound_events (business_id, call_sid);
```

### Idempotency

The UNIQUE constraint `(business_id, call_sid, event_type)` enforces
semantic idempotency:
- One `answered` event per (business, call)
- One `terminal` event per (business, call)
- Duplicate INSERT hits the constraint → adapter returns 200

### Downgrade

```sql
DROP TABLE exotel_inbound_events;
```

No data preservation needed — the table is a processing queue, not
an authoritative record. Historical call data lives in `calls` after
worker processing.

---

## Worker: ExotelInboundWorker

Follows the InboundWorker pattern (workers/inbound_worker.py).

### Responsibilities

1. Poll `exotel_inbound_events` for eligible events (received/failed
   with next_attempt_at <= now, or expired processing leases)
2. Claim one event (SELECT FOR UPDATE SKIP LOCKED)
3. Validate forward-only state transition against `calls` table
4. Apply domain mutation:
   - Create/update call record with CallSid identity
   - Update call status, duration, ended_at
5. Mark event as completed
6. On failure: increment attempts, set next_attempt_at with backoff
7. After max_attempts: mark as dead_letter

### Domain mutation (worker only)

The worker — not the adapter — mutates the calls table:

```python
# Worker processing pseudocode
async def _process_event(session, event):
    existing = await session.execute(
        select(Call).where(
            Call.business_id == event.business_id,
            Call.provider_call_sid == event.call_sid,
        ).with_for_update()
    )
    call = existing.scalar_one_or_none()

    if call is None:
        call = Call(
            business_id=event.business_id,
            caller_phone=event.caller_phone,
            provider_call_sid=event.call_sid,
            started_at=event.received_at,
        )
        session.add(call)

    if is_terminal(event.status):
        call.ended_at = datetime.now(UTC)
        call.duration_sec = event.duration

    await session.flush()
```

### Required schema change for calls table

The `calls` table needs a `provider_call_sid` column for CallSid-based
identity (currently uses phone-based correlation which can match the
wrong call):

```sql
ALTER TABLE calls ADD COLUMN provider_call_sid VARCHAR(100);
CREATE UNIQUE INDEX uq_calls_provider_call_sid
    ON calls (business_id, provider_call_sid)
    WHERE provider_call_sid IS NOT NULL;
```

This is included in the same migration as the inbound events table.

---

## Integration with accepted Dev2 infrastructure

The ExotelInboundWorker reuses:
- `deterministic_lock_key(business_id, caller_phone)` for advisory locks
- Same polling/claiming/backoff pattern as InboundEventRepository
- Same lease/claim_token/claim_version semantics
- Same dead_letter lifecycle

It does NOT reuse:
- WhatsAppInboundEvent model (different fields)
- whatsapp_inbound_events table (different schema)
- WhatsApp-specific processing (message routing, owner detection)

---

## Open questions affecting this design

From EXOTEL_PROVIDER_CONTRACT.md:
- OQ-3: CallSid in callback == Sid in API response (affects provider_call_sid)
- OQ-5: Callback retry behavior (affects max_attempts default)
- OQ-7: Callback delivery ordering (affects head-of-line blocking design)
