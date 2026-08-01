# PostgreSQL Findings Register

This register preserves discovered PostgreSQL test-infrastructure defects and their resolution status. Historical findings are not deleted when fixed.

## Finding 1 — Destructive fixture lacked explicit opt-in

- **Severity:** P1 safety
- **Status:** Resolved
- **Area:** `backend/tests/integration/postgres/conftest.py`
- **Original risk:** Direct marked pytest execution could downgrade/truncate a database without `FONELY_ALLOW_DESTRUCTIVE_TEST_DB=1`.
- **Resolution:** `_test_database_url()` now requires exact opt-in before destructive setup.
- **Current invariant:** No PostgreSQL contract may run without the explicit opt-in.

## Finding 2 — Alembic uses DATABASE_URL while tests use FONELY_TEST_DATABASE_URL

- **Severity:** P1 safety/design
- **Status:** Resolved and documented
- **Area:** PostgreSQL session fixture and Alembic configuration
- **Original risk:** The two environment variable names could target different databases.
- **Resolution:** The fixture validates `FONELY_TEST_DATABASE_URL` and explicitly assigns that same approved URL to `DATABASE_URL` in the Alembic subprocess environment.
- **Current invariant:** Migration subprocesses receive the validated test URL; unrelated ambient `DATABASE_URL` does not select the migration target.

## Finding 3 — Database-name validation was too weak

- **Severity:** P2 safety
- **Status:** Resolved
- **Area:** `backend/tests/integration/postgres/conftest.py`
- **Original risk:** A substring check accepted names such as `my_test_production`.
- **Resolution:** The fixture requires `fonely_test` or `fonely_test_<lowercase_suffix>`, a dedicated username containing `test`, and a loopback host.

## Finding 4 — Reproducible lockfile was absent

- **Severity:** P2 reproducibility
- **Status:** Resolved
- **Area:** `backend/pyproject.toml`, `backend/uv.lock`, CI
- **Resolution:** The lockfile is committed, `jsonschema==4.26.0` is resolved for QA, and CI uses `uv sync --frozen --all-extras`.

## Finding 5 — Deprecated cache action blocked job setup

- **Severity:** P0 CI execution
- **Status:** Resolved on Dev2 branch by `8d75733`
- **Area:** `.github/workflows/backend-ci.yml`
- **Observed evidence:** Initial main runs failed during setup before project steps executed.
- **Resolution:** Pin `actions/cache` v4.2.4 at `0400d5f644dc74513175e3cd8d07132dd4860809`.
- **Verification:** Run `30685195177` passed setup and reached project checks.

## Finding 6 — Session async engine crossed pytest event loops

- **Severity:** P0 PostgreSQL verification
- **Status:** Resolved on Dev2 branch by `b5d7312`; verified in CI
- **Area:** pytest-asyncio configuration and PostgreSQL fixtures
- **Observed evidence:** Run `30685195177` produced 22 failures with `Future attached to a different loop` and `Event loop is closed`.
- **Resolution:** Use session loop scope for the session engine and compatible async fixtures/tests.
- **Verification:** Run `30686343063` removed the cross-loop errors and produced 22 PostgreSQL passes.

## Finding 7 — Migration-head contract expected 0002

- **Severity:** P1 CI correctness
- **Status:** Resolved by `40e3fbb`; independently reviewed
- **File:** `backend/tests/integration/postgres/test_pending_actions_postgres.py`
- **Observed evidence:** Run `30686343063` expected `0002` but Alembic correctly reported `0003`.
- **Resolution:** Rename the misleading post-session-downgrade test to assert the observable invariant: the session fixture keeps the database at current head `0003` during tests.
- **Verification:** Run `30687004089` passed all 23 contracts and workflow downgrade/re-upgrade.
- **Owner:** Dev2.

## Current PostgreSQL gate

```text
Latest inspected run: 30687004089
PostgreSQL contracts: 23 passed
Cross-event-loop defect: resolved
Migration head contract: resolved
Final downgrade/re-upgrade: passed
Foundation PostgreSQL CI gate: green
```

No production or staging database may be used for these tests.
