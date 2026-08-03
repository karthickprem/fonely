# Fonely Architecture — Trace One Transaction

This document walks through a complete appointment booking from WhatsApp message to PostgreSQL commit. Every file and function is real — you can follow along in the source code.

## The layers

```
┌──────────────────────────────────────────────────┐
│  Channel adapters                                │
│  api/channels/whatsapp.py    (webhook handler)   │
│  api/internal/conversations.py (internal API)    │
├──────────────────────────────────────────────────┤
│  Application services                            │
│  services/conversation.py    (state + LLM)       │
│  services/appointments.py    (booking engine)    │
│  services/notifications.py   (outbox events)     │
├──────────────────────────────────────────────────┤
│  Domain rules (pure, no I/O)                     │
│  domain/conversation/safety.py  (classification) │
│  domain/conversation/state.py   (state machine)  │
│  domain/appointments/          (commands, rules)  │
│  domain/pending_actions/       (lifecycle)        │
├──────────────────────────────────────────────────┤
│  Persistence                                     │
│  repositories/appointments.py  (tenant-scoped)   │
│  models/schema.py              (ORM + enums)     │
│  migrations/versions/          (0001–0008)        │
├──────────────────────────────────────────────────┤
│  PostgreSQL                                      │
│  Exclusion constraints, deferred FKs, functions   │
└──────────────────────────────────────────────────┘
```

## The booking path

A patient sends "I want to book a consultation with Dr. Priya" via WhatsApp. Here's every step.

### Step 1: WhatsApp webhook receives the message

**File:** `api/channels/whatsapp.py`
**Functions:** `handle_webhook()` → `_process_webhook()` → `_handle_message()`

1. WhatsApp sends a POST to `/webhooks/whatsapp`.
2. `handle_webhook()` verifies the webhook signature (HMAC-SHA256 with the app secret).
3. `_process_webhook()` parses the WhatsApp Cloud API payload and extracts message entries.
4. `_handle_message()` does the work:
   - Checks message dedup using a bounded `OrderedDict` (last 10,000 IDs).
   - Maps the incoming `phone_number_id` to a `business_id` via `WhatsAppBusinessMapping`.
   - Calls `find_or_create_conversation()` to get or create a conversation context keyed by `(business_id, customer_phone)`.
   - Creates an `ActorContext` with `CallerRole.CUSTOMER` — the trusted identity for this request.
   - Delegates to `ConversationService.process_message()`.

### Step 2: ConversationService processes the message

**File:** `services/conversation.py`
**Class:** `ConversationService`
**Function:** `process_message()` → `_process_inner()`

1. Acquires a per-conversation async lock (prevents concurrent processing of the same conversation).
2. Checks the turn limit (configurable, prevents runaway conversations).
3. Calls `classify_intent()` from `domain/conversation/safety.py`:
   - **Deterministic regex** checks for medical/urgent content (e.g., "pain", "bleeding", "emergency").
   - If medical → returns an escalation response immediately, skips LLM entirely.
   - If booking intent → continues to fact extraction.
4. State machine transition via `domain/conversation/state.py` — tracks whether we're greeting, collecting facts, confirming, or done.

### Step 3: Fact extraction

**File:** `services/conversation.py` → `_extract_facts()`
**Helpers:** `services/fact_extractor.py` (`FactExtractor`), `services/fact_resolver.py` (`FactResolver`)

The system needs to collect: service, doctor, date, time, patient name, phone number.

1. `_extract_datetime()` tries deterministic regex first for dates and times (handles "tomorrow", "நாளை", "6:30 PM", "evening").
2. `FactExtractor.extract()` calls the LLM to extract structured facts from Tanglish messages (e.g., "Dr. Priya kitta consultation book pannanum" → service: consultation, resource: Dr. Priya).
3. `FactResolver.resolve()` maps extracted names to actual database entities:
   - Service name → `Service` row (fuzzy match, tenant-scoped)
   - Resource name → `Resource` row (fuzzy match, tenant-scoped)
   - Date expression → `date` object
   - Time expression → `time` object
4. `_identify_missing_facts()` checks what's still needed and asks the patient.

### Step 4: Availability check

**File:** `services/conversation_tools.py`
**Function:** `check_availability()`

Once all facts are collected:

1. Queries `OperatingSchedule` for the day of week — is the clinic open?
2. Queries `ScheduleException` for the specific date — holiday or special hours?
3. Queries existing `Appointment` rows for the resource on that date — what's already booked?
4. Generates available time slots, respecting service duration and buffer times.
5. If the requested time is available → creates a proposal. If not → offers alternatives.

### Step 5: Create appointment proposal

**File:** `services/appointments.py`
**Function:** `AppointmentService.create_proposal()`

1. Builds a stub `PendingAppointmentEnvelope` with the extracted facts.
2. Validates via `InternalValidationPort` (`api/internal/validation.py`):
   - Service exists, is active, belongs to this tenant.
   - Resource exists, is active, belongs to this tenant.
   - Resource-service eligibility exists and is active.
   - Resolves full facts: duration, buffer times, timezone, price.
3. Creates a `PendingAction` row (idempotent by key — safe to retry).
4. Marks it `awaiting_confirmation`.
5. Returns the proposal facts to the conversation for the patient to review.

### Step 6: Patient confirms

**File:** `domain/conversation/safety.py`
**Function:** `detect_confirmation()`

The patient says "yes" or "ok" or "சரி" or "book pannunga".

This is **deterministic regex**, not LLM-dependent. The system will not hallucinate a confirmation.

### Step 7: Commit the appointment

**File:** `services/appointments.py`
**Function:** `AppointmentService.confirm_and_commit()`

This is the critical transaction. Everything happens inside a single database session:

1. Load the `PendingAction` and verify the caller has permission.
2. Check for idempotent replay — if this action was already committed, return the existing result.
3. `begin_commit()` — transition the PendingAction to `committing` state (optimistic lock via version).
4. `lock_resource_schedule()` — `SELECT ... FOR UPDATE` on the resource's schedule to prevent concurrent bookings.
5. Inside a **savepoint** (`session.begin_nested()`):
   - Insert the `Appointment` row.
   - Insert the `ResourceAllocation` row (with time range for the exclusion constraint).
   - **Force deferred constraints immediate** — PostgreSQL checks the exclusion constraint `ex_resource_allocations_active_overlap` right now. If another appointment overlaps, it raises `IntegrityError`.
   - Restore constraints to deferred.
   - `complete_commit()` — transition PendingAction to `completed`.
   - Force post-completion constraints immediate.
   - Insert notification outbox events (patient confirmation + owner alert) — same transaction, so if the appointment rolls back, notifications roll back too.
6. If the exclusion constraint fires → the savepoint rolls back, PendingAction is marked `failed` with `resource_unavailable`, and the patient gets "that slot was just taken."
7. If everything succeeds → the caller commits the session. The appointment is now authoritative.

### Step 8: Session commits

**File:** `api/internal/conversations.py`
**Function:** `send_message()`

```python
await session.commit()
```

Everything persists atomically, or nothing does. The patient only sees "confirmed" after the committed row exists in PostgreSQL.

### Step 9: Notification worker delivers

**File:** `workers/notification_worker.py`
**Function:** `run_notification_worker()`

A separate async worker process:

1. Polls `notification_outbox` for events with status `pending`.
2. For each event, calls `NotificationSender.send()`:
   - `WhatsAppNotificationSender` (`services/whatsapp_notification_sender.py`) formats the message and sends via WhatsApp Cloud API.
   - `LoggingNotificationSender` logs the event (used when WhatsApp is not configured).
3. On success → marks event `delivered`.
4. On failure → increments attempt count, schedules retry with exponential backoff.
5. After max retries → marks event `dead_letter` for manual review.

Two notifications are sent:
- **Patient:** "Appointment confirmed — Dr. Priya, Tuesday Aug 12, 6:30 PM at Smile Dental. To cancel or reschedule, reply to this message."
- **Owner:** "New appointment booked — Patient: Karthick (+91...), Dr. Priya, Tuesday Aug 12, 6:30 PM."

## Key invariants

| Invariant | How it's enforced |
|---|---|
| No double-booking | PostgreSQL exclusion constraint on `resource_allocations` time ranges |
| Confirmation only after commit | Response generated after `session.commit()` returns |
| Tenant isolation | Every query includes `business_id` from trusted `ActorContext` |
| Notification atomicity | Outbox events inserted in same transaction as appointment |
| No LLM-hallucinated confirmation | Confirmation detected by deterministic regex, not LLM |
| Idempotent booking | `PendingAction` idempotency key prevents duplicate commits |
| Immutable evidence | `AppointmentCommit` stores before/after snapshots from PostgreSQL's own function |

## Database tables in this flow

| Table | Role |
|---|---|
| `businesses` | Tenant identity, timezone, contact info |
| `services` | What can be booked (e.g., "General Consultation") |
| `resources` | Who provides it (e.g., "Dr. Priya") |
| `resource_service_eligibility` | Which resources can deliver which services |
| `operating_schedules` | Weekly hours per resource |
| `schedule_exceptions` | Date-specific overrides (holidays, special hours) |
| `pending_actions` | Proposal/confirmation lifecycle with version locking |
| `appointments` | The booking — authoritative after commit |
| `resource_allocations` | Time range allocation with exclusion constraint |
| `appointment_commits` | Immutable evidence: operation, before/after snapshots |
| `notification_outbox` | Delivery queue for patient/owner messages |
| `db_conversation_turns` | Conversation history (message hashes, not content) |

## Cancel and reschedule

Cancellation and rescheduling follow the same pattern:

- **Cancel:** `create_cancellation_proposal()` → patient confirms → `confirm_cancellation()` — marks allocation cancelled, appointment cancelled, records commit evidence, sends cancellation notifications.
- **Reschedule:** `create_reschedule_proposal()` → patient confirms → `confirm_reschedule()` — releases old allocation, inserts new allocation (exclusion constraint checks the new time), updates appointment, records commit evidence.

Both operations use the same PendingAction lifecycle, the same deferred constraint pattern, and the same savepoint-based error handling.
