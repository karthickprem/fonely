# Fonely — Product and Engineering Roadmap

> **Document status:** Living product strategy and phase roadmap. Updated 2026-08-03. Product flows are targets unless marked "implemented." See [STATUS.md](STATUS.md) for current evidence.

## Product thesis

Fonely is a **multilingual AI virtual receptionist** for Indian dental clinics (expanding to other MSMEs). It is not a one-time-setup chatbot — it learns daily from the clinic owner and handles both WhatsApp messages and voice calls on the same number.

The first commercial pilot targets independent urban dental clinics with 1–3 dentists in Chennai.

**Core product promise:** "Never lose a patient enquiry because nobody answered the phone."

**Key differentiators:**
- Natural Tamil/Tanglish/Indian-English conversation
- Deterministic bookings — zero hallucinated appointments
- Same number handles WhatsApp + voice calls
- Clinic owner updates context daily via WhatsApp (leave, schedule changes, offers)
- Dental safety boundary — never gives medical advice, escalates appropriately
- MSME pricing with capped usage, not enterprise SaaS

## Core safety principle

**The model is the ears and mouth. The database is the source of truth. Deterministic code is the gatekeeper.**

The model must not:
- Invent stock, prices, schedules, or availability.
- Calculate authoritative totals.
- Write business tables directly.
- Call internal commit operations.
- Announce transaction success before the deterministic engine commits.

## Two user roles on the same number

### Patient (books appointments)
```
Patient WhatsApps or calls +91 44 XXXX XXXX
→ Fonely greets in their language
→ Understands intent (booking, enquiry, cancellation)
→ Collects facts conversationally (service, doctor, time)
→ Checks real availability from database
→ Proposes slot with exact price
→ Patient confirms
→ Appointment committed to PostgreSQL
→ Patient gets WhatsApp confirmation
→ Clinic owner gets notification
```

### Clinic owner (manages the day)
```
Owner WhatsApps the same number (identified by registered phone)
→ "Dr. Priya is on leave tomorrow"
→ Fonely creates ScheduleException, stops booking Dr. Priya
→ Automatically cancels affected appointments
→ Notifies affected patients

→ "Close early today at 5 PM"
→ Fonely adjusts today's schedule
→ Stops offering slots after 5 PM

→ "Show me tomorrow's appointments"
→ Fonely sends structured summary

→ "New patient special: consultation free this week"
→ Fonely updates offer context for conversations
```

Owner commands use the same proposal/confirmation pattern as bookings — no accidental changes.

## Channel architecture

```
+91 44 XXXX XXXX (Fonely clinic number)
  ├── WhatsApp message → webhook → ConversationService → PostgreSQL
  ├── WhatsApp from owner phone → OwnerCommandService → PostgreSQL
  └── Phone call → Exotel → Voice pipeline → ConversationService → PostgreSQL

All channels → same booking engine, same availability, same safety rules
```

## Target transaction path

```
Caller speaks or types
→ Safety boundary check (deterministic, not LLM)
→ Fact extraction (Tanglish-aware, LLM + deterministic resolver)
→ Availability check from OperatingSchedule + ScheduleException
→ PendingAction proposal created
→ Caller hears/reads the authoritative confirmation snapshot
→ Caller confirms (deterministic detection, not LLM judgment)
→ Deterministic engine commits in PostgreSQL
→ Committed result returned
→ Notification outbox event created (same transaction)
→ Background worker sends WhatsApp confirmation
→ Owner receives notification
```

## Lifecycle-safe public-tool contract

The versioned target contract is `evals/tool-contract.v1.json`.

Patient tools:
- `check_availability`
- `create_pending_appointment`
- `confirm_pending_appointment`
- `cancel_pending_appointment`
- `reschedule_appointment`
- `get_business_information`
- `escalate_to_owner`

Owner tools (via WhatsApp commands):
- `update_schedule` — mark leave, change hours, add exceptions
- `cancel_appointments_bulk` — cancel all for a doctor/day
- `get_daily_summary` — tomorrow's appointment list
- `update_offers` — temporary promotions
- `manage_staff` — add/deactivate resources

Internal operations (`begin_commit`, `complete_commit`, etc.) must never be LLM-callable.

## Development phases

### Phase A — Production backend foundation ✅ COMPLETE
- Python backend, tenant-aware ORM, Alembic migrations 0001–0003
- Strict values, enums, safe database/session foundations
- PostgreSQL CI green

### Phase B — PendingAction lifecycle ✅ COMPLETE
- Proposal, confirmation, commit, failure, cancellation, expiry
- Idempotency, optimistic concurrency, canonical digests
- Actor authorization, committed-entity linkage

### Phase C — Inventory and order engine ✅ COMPLETE
- Migration 0005, deterministic inventory/order transactions
- Append-only movements, immutable line items (trigger-enforced)
- Lock ordering, concurrency race tests, populated roundtrip
- Note: not needed for dental pilot but foundation for future verticals

### Phase D — Appointment and scheduling engine ✅ COMPLETE
- Migration 0004, services/resources/eligibility/schedules
- Create, confirm, cancel, reschedule — all PostgreSQL-proven
- Exclusion constraints for overlap prevention
- Savepoint-managed, caller-owned transactions
- Immutable appointment commit evidence

### Phase E — Conversation and channel layer ✅ MOSTLY COMPLETE
- Conversation state machine (10 states, explicit transitions)
- Tamil/Tanglish/English safety boundary (deterministic regex)
- LLM-based Tanglish fact extraction + deterministic resolver
- Confirmation detection (Tamil + English)
- Provider-neutral model gateway (Sarvam, injectable)
- Internal text API (appointment proposals + confirm)
- WhatsApp inbound adapter (webhook, dedup, signature verification)
- Conversation persistence (migration 0008, survives restart)
- Provider resilience (circuit breaker, retry, graceful degradation)
- In-process metrics and alerting

Remaining:
- Session commit gap in HTTP conversation route (P1)
- E2E HTTP booking flow proven but uses direct service call for final steps

### Phase F — Production voice pipeline 🔄 IN PROGRESS
- Pipecat + Sarvam STT + Claude Haiku LLM + Sarvam TTS
- Browser voice lab (Dev4, R&D branch)
- Chrome proof: 1.26s speech-end to bot-start
- Silero VAD + Smart Turn automatic endpointing

Remaining:
- Voice quality tuning for natural Tamil
- Exotel telephony integration
- Same-number WhatsApp + voice routing

### Phase G — Owner experience ❌ NOT STARTED
- Owner identification by registered phone
- Owner command parser ("Dr. Priya leave tomorrow" → ScheduleException)
- Dynamic daily context (transient updates the LLM can reference)
- Proactive daily briefing
- Bulk appointment cancellation from owner command
- Owner notification preferences

This is a core product differentiator, not an afterthought.

### Phase H — Onboarding and configuration ✅ MOSTLY COMPLETE
- Migration 0006, onboarding draft → review → approve → activate
- Dental clinic fixture (Smile Dental, 5 services, 2 dentists, schedules)
- Owner-only approval, optimistic versioning, idempotent submission
- Activation writes Services/Resources/Eligibility/Schedules

Remaining:
- Photo/PDF/spreadsheet import adapters
- WhatsApp-guided onboarding
- Test-mode activation and readiness checks

### Phase I — Notifications ✅ MOSTLY COMPLETE
- Migration 0007, transactional outbox
- Outbox events in same transaction as appointment
- Background worker with retry, backoff, dead-letter
- LoggingNotificationSender (placeholder)

Remaining:
- Real WhatsApp notification sender
- Message templates (Meta approval required)
- SMS fallback
- Owner daily summary notification

### Phase J — Production operations 🔄 PARTIAL
- Structured JSON logging with correlation ID
- Rate limiting, CORS, security headers, request protection
- Structured error handling (no stack traces leaked)
- Dockerfile, Docker Compose staging
- Deployment readiness verifier, backup/restore verification
- In-process metrics (/metrics endpoint)
- Alert thresholds (/health/alerts)

Remaining:
- Staging deployment (blocked: no Docker on dev machine)
- Monitoring/APM integration
- CI/CD pipeline
- Load/soak testing
- Incident response runbook

### Phase K — Privacy and compliance 🔄 IN PROGRESS
- Conversation turns store message hash, not text
- PII-safe logging throughout
- Data retention policies (Dev2 working)
- Tenant isolation on all queries

Remaining:
- Automated PII cleanup
- PII access audit logging
- Data export for patient requests
- Consent management
- Clinical safety review by a practicing dentist

### Phase L — Payments and provisioning ❌ NOT STARTED
- Subscription provisioning and entitlements
- Capped usage plans (included minutes + overage)
- Business/number activation
- Billing records

### Phase M — Controlled pilot ❌ NOT STARTED
- 3–5 consenting dental clinics in Chennai
- Tamil/Tanglish + Indian English
- One provider path, manual monitoring
- Human fallback, owner feedback
- Measurable go/no-go: booking completion rate, naturalness score

## Pricing model (hypothesis, not validated)

Capped usage plans:
- Pilot: ₹2,999/month — 200 included minutes
- Starter: ₹4,999/month — 500 included minutes
- Growth: ₹7,999/month — 1,000 included minutes
- Overage: ₹5–6/min

A good receptionist handles a booking in 60–120 seconds. Human-like ≠ long conversations.

## Provider strategy

Provider-independent voice layer — one provider per entire call, route at call start:

```
Voice Session Manager
  ├── Sarvam STT + Claude/Sarvam LLM + Sarvam TTS (production default)
  ├── OpenAI Realtime Mini (premium/demo)
  └── Future Provider Adapter

STT and TTS dominate cost. LLM is <5% of call cost.
```

Do NOT:
- Demo with premium voice and ship a worse production voice
- Switch providers mid-call (voice/pace changes)
- Build a custom speech model (use existing providers)
- Commit to a provider before A/B testing with real Tamil speakers

## What the pilot does not require

- Mobile application
- Large web dashboard
- CRM integrations
- Outbound calling or marketing
- Delivery logistics
- General workflow DSL
- Microservices, Kafka, Kubernetes
- Multi-region deployment
- Every Indian language
- Multiple verticals before one succeeds
- Custom speech/voice model

## Current sequence (as of 2026-08-03)

```
✅ Done:
  Phases A–D: Backend foundation through appointment engine
  Phase E: Conversation orchestrator with WhatsApp adapter
  Phase H: Onboarding persistence with dental fixture
  Phase I: Notification outbox
  Phase J: Staging infrastructure (built, not deployed)

🔄 Active:
  Dev1: Observability bug fixes
  Dev2: Data retention and privacy
  Dev3: Session commit fix + HTTP E2E test
  Dev4: Pipecat voice lab

📋 Next priority:
  1. Fix known bugs (observability, session commit)
  2. Phase G: Owner command system (critical product differentiator)
  3. Phase F: Voice quality tuning + Exotel integration
  4. Deploy to staging (needs Docker-capable machine)
  5. Meta WhatsApp Business API setup
  6. Real dental clinic configuration and testing
  7. Controlled pilot with 3–5 clinics
```

## Decision checklist

For every significant change ask:
1. What customer problem does this solve?
2. Which invariant must never break?
3. Is this foundation, domain behavior, application orchestration, or adapter logic?
4. Does PostgreSQL need to enforce it?
5. What happens under retry, concurrency, rollback, and partial provider failure?
6. How is tenant isolation preserved?
7. How will operators observe, diagnose, support, and repair it?
8. What are the scalability and cost implications?
9. Can a small team operate it safely?
10. Is it needed now or merely conceivable?
11. What exact evidence will validate it?
12. Does the plan document cover this, or are we improvising?
