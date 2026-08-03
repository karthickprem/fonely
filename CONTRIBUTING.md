# Contributing to Fonely

## Before you start

1. Read [PLAN.md](docs/PLAN.md) for product context — what we're building and why.
2. Read [STATUS.md](docs/STATUS.md) for current state — what's done, what's in progress, what's blocked.
3. Understand which files you own. Fonely uses non-overlapping ownership — ask the team lead before touching files outside your scope.

## Development setup

### Prerequisites

- Python 3.12+
- Docker or Podman (for PostgreSQL)
- [uv](https://docs.astral.sh/uv/) (Python package manager)
- Git

### Install and run

```bash
cd backend
uv sync --frozen --all-extras
```

### Start PostgreSQL

```bash
docker compose -f infra/postgres/compose.yaml up -d
```

This starts PostgreSQL 16 on `localhost:55432` with database `fonely_test`. The data is disposable — tear it down with `docker compose -f infra/postgres/compose.yaml down -v`.

### Run migrations

```bash
cd backend
.venv/bin/alembic upgrade head
```

### Environment variables

Copy `.env.example` to `.env` and fill in values. For local development, the only required variable is the database URL:

```
DATABASE_URL=postgresql+asyncpg://fonely_test:fonely_test_local_only@localhost:55432/fonely_test
```

For PostgreSQL integration tests, set:

```
FONELY_TEST_DATABASE_URL=postgresql+asyncpg://fonely_test:fonely_test_local_only@localhost:55432/fonely_test
```

## Project structure

```
backend/src/fonely/
  api/                → HTTP routes
    channels/         → WhatsApp webhook (signature verification, dedup)
    internal/         → Internal API (conversations, appointments, onboarding)
  core/               → Config, middleware, resilience, PII audit
  domain/             → Pure business rules (no DB, no I/O)
    appointments/     → Booking commands, validation, commit contracts
    conversation/     → State machine, safety boundary, intent detection
    onboarding/       → Clinic configuration
    pending_actions/  → Proposal/confirmation lifecycle
    inventory/        → Stock management (future)
    orders/           → Order management (future)
  models/             → SQLAlchemy ORM and enums
  repositories/       → Database access (tenant-scoped)
  services/           → Application transactions (caller-owned sessions)
  workers/            → Background processing (notification delivery)

backend/migrations/   → Alembic versions (0001–0008)

backend/tests/
  unit/               → No database needed (~680 tests)
  integration/
    postgres/          → Real PostgreSQL required (~390 tests)
```

## Architecture rules

1. **AI proposes, database decides.** The LLM extracts facts and generates responses. It never writes to business tables directly.

2. **Every query is tenant-scoped.** All reads and writes include `business_id` from trusted context. Never load a tenant entity by integer ID alone.

3. **Mutations go through PendingAction.** The flow is: create proposal → mark awaiting confirmation → patient confirms → begin commit → insert rows → force constraints → complete commit. No shortcuts.

4. **Channels are thin adapters.** WhatsApp and voice handlers validate signatures, extract messages, and delegate to `ConversationService`. Business logic lives in domain and service layers.

5. **Test with real PostgreSQL.** Unit tests with mocks prove logic flow. PostgreSQL integration tests prove correctness — constraint enforcement, transaction isolation, concurrent booking conflicts.

## Making changes

### Adding a new API endpoint

1. Add the route in `api/internal/` (internal) or `api/channels/` (external webhook).
2. Use the existing auth pattern — HMAC Bearer token for internal, webhook signature for WhatsApp.
3. Create a service method for business logic. The route handler should only parse the request, call the service, and format the response.
4. Add a unit test and a PostgreSQL integration test.
5. Register the router in `app.py`.

### Adding a new Alembic migration

1. Check the current head: `.venv/bin/alembic heads`
2. Create the migration: `.venv/bin/alembic revision -m "description"` (use the next sequential number as prefix)
3. Set the correct `down_revision` to chain from the current head.
4. Implement both `upgrade()` and `downgrade()`.
5. Test the full cycle: upgrade → downgrade → re-upgrade.
6. Run migration parity check: `.venv/bin/python ../scripts/migration_policy.py`

Never create competing revision heads. The project maintains a single linear migration chain.

### Modifying appointment logic

1. Change domain rules in `domain/appointments/` (pure functions, no I/O).
2. Change the service transaction in `services/appointments.py`.
3. Add a PostgreSQL integration test proving the change works under real constraints.
4. Verify that cancel and reschedule still work — they share deferred constraint sets.

### Adding a notification type

1. Add the event type to `models/enums.py` (`NotificationEventType`).
2. Add a creation method in `services/notifications.py` following the idempotent pattern.
3. Add message formatting in `services/whatsapp_notification_sender.py`.
4. Wire the call into the relevant service method (inside the transaction, wrapped in try/except).

## Testing

```bash
# Fast feedback — unit tests only (no database)
.venv/bin/pytest -m "not postgres" -q

# Full verification — includes PostgreSQL integration tests
.venv/bin/pytest -q

# Type checking
.venv/bin/mypy src

# Linting
.venv/bin/ruff check .
.venv/bin/ruff format --check .

# Run a specific test file
.venv/bin/pytest tests/unit/appointments/test_service.py -v

# Run a specific test
.venv/bin/pytest tests/unit/appointments/test_service.py::test_confirm_and_commit -v
```

PostgreSQL tests require the `FONELY_TEST_DATABASE_URL` environment variable and a running PostgreSQL instance. They create and drop tables per test session — never point them at a production database.

## Commit conventions

```
feat(scope): add new capability
fix(scope): fix a bug
test(scope): add or fix tests
docs(scope): documentation only
refactor(scope): restructure without behavior change
```

Scopes: `appointments`, `conversation`, `notifications`, `whatsapp`, `onboarding`, `migrations`, `ci`, `tests`, `repo`.

## What NOT to do

- **Don't bypass PendingAction for mutations.** Every business state change goes through proposal → confirmation → commit.
- **Don't put business logic in channel adapters.** WhatsApp/voice handlers are thin — they parse, delegate, respond.
- **Don't merge without PostgreSQL integration tests** for any code that touches transactions or constraints.
- **Don't store patient message text.** Store a hash for dedup, never the content.
- **Don't log PII.** No phone numbers, patient names, or message content in logs. Use structured metadata (event type, entity ID).
- **Don't use unit-test-only evidence for transaction code.** Mocks don't prove PostgreSQL constraint behavior.
- **Don't edit files outside your ownership scope** without coordination.
