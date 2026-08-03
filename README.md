# Fonely

Multilingual AI virtual receptionist for Indian dental clinics. Handles appointment booking, cancellation, and rescheduling via WhatsApp and voice — in Tamil, Tanglish, and Indian English.

## What Fonely does

A patient messages the clinic's WhatsApp number (or calls) to book an appointment. Fonely understands the request in their language, checks the doctor's availability, and books the slot — all without the clinic owner answering the phone. The owner gets notified of every booking. Nobody misses a call, nobody waits on hold, and the receptionist desk doesn't need to be staffed during peak hours.

## Architecture

```
WhatsApp / Voice / Internal API
        │
        ▼
Channel adapters (thin, stateless — no business logic)
        │
        ▼
ConversationService (state machine, safety classification, fact extraction)
        │
        ▼
AppointmentService (propose → confirm → commit with PostgreSQL constraints)
        │
        ▼
PostgreSQL (authoritative state, exclusion constraints, transaction evidence)
        │
        ▼
Notification outbox → WhatsApp delivery (patient confirmation + owner alert)
```

The AI proposes. PostgreSQL decides. The model is never the system of record and never directly mutates business tables.

## Quick start

Prerequisites: Python 3.12, Docker (for PostgreSQL), [uv](https://docs.astral.sh/uv/)

```bash
# Clone and install
git clone git@github.com:karthickprem/fonely.git
cd fonely/backend
uv sync --frozen --all-extras

# Start disposable PostgreSQL
docker compose -f ../infra/postgres/compose.yaml up -d

# Run migrations
.venv/bin/alembic upgrade head

# Run unit tests (no database needed)
.venv/bin/pytest -m "not postgres" -q

# Run PostgreSQL integration tests
export FONELY_TEST_DATABASE_URL=postgresql+asyncpg://fonely_test:fonely_test_local_only@localhost:55432/fonely_test
.venv/bin/pytest -m postgres -q

# Type checking and linting
.venv/bin/mypy src
.venv/bin/ruff check .
.venv/bin/ruff format --check .

# Start the application
.venv/bin/python run.py
```

## Project structure

```
backend/
  src/fonely/
    api/              HTTP routes and webhook handlers
      channels/       WhatsApp webhook (signature verification, message dedup)
      internal/       Internal API (conversations, appointments, onboarding)
    core/             Config, middleware, resilience, metrics, PII audit
    domain/           Pure business rules (no database, no I/O)
      appointments/   Booking commands, validation contracts, availability
      conversation/   State machine, safety classification, intent detection
      onboarding/     Clinic configuration and setup
      pending_actions/ Proposal → confirmation lifecycle
      inventory/      Stock management (future vertical)
      orders/         Order management (future vertical)
    models/           SQLAlchemy ORM models and enums
    repositories/     Database access (all queries tenant-scoped by business_id)
    services/         Application transactions (caller-owned sessions)
    workers/          Background processing (notification delivery)
  migrations/         Alembic versions (0001–0008)
  tests/
    unit/             Fast tests, no database needed (~680 tests)
    integration/
      postgres/       Real PostgreSQL required (~390 tests)

docs/                 Product plan, status, QA strategies, team model
evals/                Adversarial evaluation corpus (211 cases, 377 turns)
infra/postgres/       Docker Compose for local test database
scripts/              Migration checks, backup verification, deployment readiness
src/, public/         Voice feasibility prototype (not connected to backend)
```

## Key documents

| Document | What it covers |
|---|---|
| [PLAN.md](docs/PLAN.md) | Product strategy, phase roadmap, pilot plan |
| [STATUS.md](docs/STATUS.md) | Current evidence snapshot and blockers |
| [CONTRIBUTING.md](CONTRIBUTING.md) | How to make changes safely |
| [Architecture walkthrough](docs/ARCHITECTURE.md) | Trace one booking from WhatsApp to PostgreSQL commit |
| [DATA_INVENTORY.md](docs/DATA_INVENTORY.md) | PII classification and retention policies |
| [STAGING_DEPLOYMENT.md](docs/STAGING_DEPLOYMENT.md) | Docker deployment for staging |
| [PostgreSQL testing guide](docs/testing/POSTGRESQL.md) | How to run and write PG integration tests |
| [Test strategy](docs/qa/TEST_STRATEGY.md) | Testing philosophy and coverage approach |

## Current status

Fonely is **pre-pilot**. The backend booking path (create, cancel, reschedule) is implemented and PostgreSQL-tested. WhatsApp channel adapter, notification delivery, and conversation engine are implemented. Voice channel is in R&D. The first pilot target is independent urban dental clinics.

See [STATUS.md](docs/STATUS.md) for the latest verified evidence.

## Verification language

This project uses precise terminology:

- **Implemented:** code exists
- **Unit-tested:** passed without a database
- **PostgreSQL-tested:** passed against a real disposable PostgreSQL instance
- **CI-verified:** GitHub Actions run completed successfully
- **Staging-validated:** tested in staging environment
- **Pilot-validated:** observed with real businesses and callers

One level never implies the next.
