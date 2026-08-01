# PostgreSQL Testing Guide

## Why PostgreSQL is required

Fonely uses PostgreSQL-specific features that cannot be tested with SQLite:

- `JSONB` columns for payloads, transcripts, and configuration.
- `postgresql.JSONB` column type in SQLAlchemy.
- `asyncpg` as the async PostgreSQL driver.
- Check constraints with `native_enum=False, create_constraint=True`.
- `INSERT ... ON CONFLICT DO NOTHING RETURNING` for idempotency.
- `FOR UPDATE SKIP LOCKED` for concurrent expiry.
- `Numeric` precision behavior matching PostgreSQL semantics.
- Future: range exclusion constraints for appointment overlap prevention.

---

## Local prerequisites

- Docker (or Podman) for the test database container.
- Python 3.12 with the project virtual environment installed.
- No system-level PostgreSQL installation is required.

---

## Docker Compose setup

### Start the test database

```bash
docker compose -f infra/postgres/compose.yaml up -d
```

### Podman alternative

```bash
podman compose -f infra/postgres/compose.yaml up -d
```

### Stop the test database

```bash
docker compose -f infra/postgres/compose.yaml down
```

### Reset the test database (destroy all data)

```bash
docker compose -f infra/postgres/compose.yaml down -v
```

This removes the `fonely_test_data` volume and all stored data.

---

## Test connection URL

The local test URL is:

```
postgresql+asyncpg://fonely_test:fonely_test_local_only@localhost:55432/fonely_test
```

- **Host:** localhost only (bound to 127.0.0.1).
- **Port:** 55432 (non-default to avoid conflicts with system PostgreSQL).
- **Database:** `fonely_test`.
- **User:** `fonely_test`.
- **Password:** `fonely_test_local_only` — a local-only placeholder, not a real secret.

---

## Safety rules

### Never use a production or staging database

The test suite performs destructive operations:

- `alembic downgrade base` — drops all tables.
- `TRUNCATE ... RESTART IDENTITY CASCADE` — deletes all data between tests.

**These operations will destroy all data in the target database.**

### Database name policy

The test script (`scripts/test-postgres.sh`) requires the database name to match:

```
^fonely_test(_[a-z0-9_]+)?$
```

Allowed examples:

- `fonely_test`
- `fonely_test_ci`
- `fonely_test_dev2`

Rejected examples:

- `production` — not a test name.
- `contest_production` — does not match the allowlist pattern.
- `my_test_db` — does not start with `fonely_test`.
- `fonely_test_UPPER` — uppercase not permitted.
- `test` — does not start with `fonely_test`.

The test fixture in `conftest.py` uses a weaker check (substring `"test"`). Until Dev1 hardens it to match the script's policy, running `pytest -m postgres` directly bypasses the stricter validation. Always use `scripts/test-postgres.sh` for safety.

### Destructive opt-in

Running `scripts/test-postgres.sh` requires:

```bash
export FONELY_ALLOW_DESTRUCTIVE_TEST_DB=1
```

This explicit variable confirms the developer understands the test suite will downgrade, upgrade, and truncate the target database.

### Localhost only

The test script only accepts `localhost`, `127.0.0.1`, or `::1` as the database host. Remote hosts are denied — there is no opt-in override. This prevents accidental destructive operations against shared or cloud databases.

The CI service container uses `localhost:5432`, which is permitted.

### URL parsing

The test script parses URLs with Python's `urllib.parse`, not Bash string operations. This correctly handles IPv6 addresses, percent-encoded credentials, and query strings. The parser never prints the password.

---

## How marked tests behave

PostgreSQL integration tests are marked with `@pytest.mark.postgres` in `backend/tests/integration/postgres/`.

- When `FONELY_TEST_DATABASE_URL` is **not set**: all 17 tests are collected but **skipped**.
- When `FONELY_TEST_DATABASE_URL` is set to a valid test URL: tests run against the live database.

The test session fixture (`migrated_postgres`) automatically:

1. Downgrades to base.
2. Upgrades to head.
3. Runs all PostgreSQL tests.
4. Downgrades to base after the session.

Each individual test truncates all tables after completion via the `clean_database` fixture.

---

## How to run tests

### Unit tests only (no database required)

```bash
cd backend
.venv/bin/pytest -m "not postgres" -q
```

### PostgreSQL integration tests (via test script — recommended)

```bash
FONELY_ALLOW_DESTRUCTIVE_TEST_DB=1 scripts/test-postgres.sh
```

### PostgreSQL integration tests (direct pytest)

```bash
export FONELY_TEST_DATABASE_URL="postgresql+asyncpg://fonely_test:fonely_test_local_only@localhost:55432/fonely_test"
cd backend
.venv/bin/pytest -m postgres -q
```

Note: direct pytest bypasses the test script's stricter database name validation. Use the test script when possible.

### Full suite (unit + PostgreSQL)

```bash
export FONELY_TEST_DATABASE_URL="postgresql+asyncpg://fonely_test:fonely_test_local_only@localhost:55432/fonely_test"
cd backend
.venv/bin/pytest -q
```

### Migration upgrade/downgrade only

```bash
export DATABASE_URL="postgresql+asyncpg://fonely_test:fonely_test_local_only@localhost:55432/fonely_test"
cd backend
.venv/bin/alembic upgrade head
.venv/bin/alembic downgrade base
.venv/bin/alembic upgrade head
```

### Alembic check (ORM/migration drift)

```bash
cd backend
.venv/bin/alembic check
```

### Offline migration smoke-test (no database)

```bash
scripts/check-migrations.sh
```

This script derives the revision chain from Alembic history (not filenames), supports alphanumeric revision IDs, and verifies that every migration file appears in the chain.

---

## Diagnosing common issues

### Connection refused

**Symptom:** `ConnectionRefusedError` or `connection refused` from asyncpg.

**Causes:**
- Docker/Podman container is not running.
- Wrong port (default PostgreSQL is 5432; Fonely test uses 55432).
- Container is still starting (health check not yet passing).

**Fix:**
```bash
docker compose -f infra/postgres/compose.yaml up -d
docker compose -f infra/postgres/compose.yaml ps   # check health
```

### Authentication failure

**Symptom:** `password authentication failed for user`.

**Causes:**
- Wrong credentials in the URL.
- Volume has data from a previous run with different credentials.

**Fix:**
```bash
docker compose -f infra/postgres/compose.yaml down -v
docker compose -f infra/postgres/compose.yaml up -d
```

### Migration mismatch

**Symptom:** `alembic check` reports differences, or upgrade fails.

**Causes:**
- ORM models were changed without generating a new migration.
- Manual database modifications.

**Fix:**
1. Verify the current head: `.venv/bin/alembic heads`.
2. Reset: downgrade to base and upgrade again.
3. If ORM/migration parity is broken, a new migration revision is needed.

### Enum/check constraint failure

**Symptom:** `IntegrityError` with `violates check constraint`.

**Causes:**
- Code is writing a string value not in the allowed enum set.
- Migration enum definition does not match ORM enum definition.

**Fix:**
1. Compare enum members in the migration file vs `models/enums.py`.
2. Ensure `native_enum=False, create_constraint=True, validate_strings=True` is used consistently.

### Stale test volume

**Symptom:** Tests fail on schema or column mismatches after migration changes.

**Fix:**
```bash
docker compose -f infra/postgres/compose.yaml down -v
docker compose -f infra/postgres/compose.yaml up -d
```

### Port collision

**Symptom:** `address already in use` when starting the container.

**Causes:**
- Another service is using port 55432.
- A previous container is still running.

**Fix:**
```bash
docker compose -f infra/postgres/compose.yaml down
lsof -i :55432   # find the process
```

---

## How CI runs the suite

The GitHub Actions workflow (`.github/workflows/backend-ci.yml`):

1. Triggers on all pushes and pull requests (not branch-restricted).
2. Starts a PostgreSQL 16 service container with test-only credentials.
3. Requires `backend/uv.lock` to exist — fails immediately if missing.
4. Installs Python 3.12 and dependencies via `uv sync --frozen --all-extras`.
5. Runs package import verification.
6. Runs Ruff lint and format checks.
7. Runs mypy type checking.
8. Runs `alembic upgrade head`.
9. Runs `alembic check`.
10. Runs non-PostgreSQL unit tests.
11. Runs PostgreSQL integration tests.
12. Runs migration downgrade to base and re-upgrade to head.

CI uses `FONELY_ALLOW_DESTRUCTIVE_TEST_DB=1` because the service container is disposable.

Third-party actions are pinned to verified commit SHAs for supply-chain security:

| Action | Tag | SHA verified |
|--------|-----|-------------|
| `actions/checkout` | v4.2.2 | `11bd7190...` |
| `actions/setup-python` | v5.3.0 | `0b93645e...` |
| `astral-sh/setup-uv` | v4.2.0 | `38f3f104...` |
| `actions/cache` | v4.1.2 | `6849a648...` |

No production credentials or repository secrets are used for the CI test database.

### Lockfile requirement

CI requires `backend/uv.lock`. Generate it with:

```bash
cd backend
uv lock
```

This is Dev1's responsibility since `pyproject.toml` is in Dev1's ownership boundary. CI will fail until the lockfile is committed.

---

## Warning

**Never connect test tooling to a production or staging database.** The test suite performs irreversible destructive operations (DROP TABLE, TRUNCATE). There is no undo. The database name, URL validation, and destructive opt-in variable are safety layers, not guarantees — human discipline is the final safeguard.
