# Fonely — Current Project Status

**Evidence snapshot:** 2026-08-03. See [PLAN.md](PLAN.md) for product strategy.

## Executive status

```
Main branch:     973fd13 (+ 468cbaa resilience pending CI verification)
Alembic head:    0008
Migrations:      0001–0008 (schema, pending actions, linkage, appointments,
                 inventory/orders, onboarding, notifications, conversations)
Tables:          28
Source lines:    ~15,000
Test lines:      ~21,000
Tests:           1,326 collected (1,050+ non-PG, 280 PG integration)
CI:              Green
First vertical:  Dental clinics (Chennai)
```

## What's production-proven (PostgreSQL-tested)

| Capability | Evidence |
|-----------|----------|
| Appointment create + confirm | Full lifecycle, overlap exclusion, savepoint recovery |
| Appointment cancel | Lifecycle, tenant isolation, idempotency |
| Appointment reschedule | Lifecycle, atomicity on conflict, evidence preservation |
| Onboarding lifecycle | Submit → review → approve → activate → writes operational tables |
| Notification outbox | Same-transaction evidence, rollback, worker delivery, dead-letter |
| Conversation persistence | Restart survival, phone continuity, expired cleanup |
| Conversation booking flow | Facts → availability → propose → confirm → committed appointment |
| E2E API route test | HTTP request through FastAPI → committed PostgreSQL row |
| Migration roundtrips | All 8 migrations upgrade/downgrade/re-upgrade proven |
| Backup/restore | Disposable PG dump/restore with content verification |

## What's built but not PostgreSQL-proven

| Capability | Gap |
|-----------|-----|
| Observability metrics | In-process metrics — histogram memory leak, gauge race condition, path normalization bug (fixes assigned to Dev1) |

## What's built as adapters (no database interaction)

| Capability | Tests |
|-----------|-------|
| WhatsApp webhook handler | 31 unit tests, HMAC signature verification |
| WhatsApp Cloud API sender | Timeout handling, PII-safe logging |
| Model gateway (Sarvam) | Provider resilience, circuit breaker, retry |
| Tanglish fact extraction | 28 unit tests, Tamil numeral/date/time resolution |
| Safety boundary | English + Tamil/Tanglish medical/urgent classification |
| Structured error handling | Known exceptions mapped, no stack traces leaked |
| Rate limiting | Per-IP sliding window, health endpoints exempt |
| Security headers | nosniff, DENY, HSTS, no-store |

## What's NOT built

| Gap | Priority | Phase |
|-----|----------|-------|
| Owner command system (daily updates) | P0 — core differentiator | G |
| Real WhatsApp notification sender | P1 | I |
| Voice pipeline (production) | P1 | F |
| Staging deployment | P1 — blocked on Docker | J |
| Session commit in HTTP route | P1 — known bug | E |
| Data retention automated cleanup | P1 — Dev2 working | K |
| Exotel telephony integration | P2 | F |
| Meta WhatsApp Business API setup | P2 — external | E |
| Privacy/consent management | P2 | K |
| Cancellation/rescheduling in conversation | P2 | E |
| Photo/PDF onboarding import | P3 | H |
| Payments/provisioning | P3 | L |
| Engineer handoff documentation | P2 | — |

## Active developer assignments

| Developer | Task | Status |
|-----------|------|--------|
| Dev1 | Observability bug fixes (histogram leak, gauge race, path normalization) | Working |
| Dev2 | Data retention and privacy (cleanup service, PII audit) | Working |
| Dev3 | Session commit fix + full HTTP E2E test | Assigned |
| Dev4 | Pipecat voice lab (Sarvam STT + Claude Haiku + Sarvam TTS) | R&D branch |

## Production bugs found after merge (lessons)

| Bug | Found by | Root cause |
|-----|----------|-----------|
| False confirmation (no DB row) | Independent reviewer | None fallback in ConversationService |
| Validation port crash on cancel/reschedule | Dev2 PostgreSQL tests | assert CreateAppointmentData only |
| Snapshot format mismatch | Dev2 PostgreSQL tests | Python dict ≠ PG function output |
| Reschedule allocation FK violation | Dev2 PostgreSQL tests | Wrong PendingAction ID used |
| Session commit gap in HTTP route | Dev3 E2E test | async with session never commits |

All found because PostgreSQL tests caught what unit tests couldn't.

## Known code quality issues (not yet fixed)

- Histogram stores all values forever (memory leak) — Dev1 fixing
- Gauge increment/decrement not atomic — Dev1 fixing
- Path normalization matches embedded digits (/v1 → /v{id}) — Dev1 fixing
- Rate limit IP dict grows unbounded — Dev1 fixing
- Content-Length header not validated — Dev1 fixing
- 429 retry double-waits (Retry-After + backoff) — minor
- ResilientClient counter increments not thread-safe — minor

## Infrastructure

| Component | Status |
|-----------|--------|
| Dockerfile | Built, non-root, healthcheck |
| Docker Compose staging | Built, PG16 + migration init |
| Deployment runbook | Written |
| Docker on dev machine | NOT AVAILABLE |
| Staging server | NOT PROVISIONED |
| Domain name / SSL | NOT PROVISIONED |
| Meta WhatsApp Business | NOT REGISTERED |
| Exotel account | CONFIGURED (API keys in .env) |
| Sarvam API | CONFIGURED AND WORKING |

## Voice lab (R&D branch, not in main)

```
Branch: dev4/voice-rd-lab
Stack: Pipecat 1.7 + Sarvam STT + Claude Haiku + Sarvam HTTP streaming TTS
Evidence: Chrome proof — 1.26s speech-end to bot-start
Status: Working for text + audio, Tamil voice quality needs tuning
```
