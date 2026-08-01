# PostgreSQL Findings

Defects discovered in existing source, migration, or test files that Dev2 is not permitted to edit. Each finding includes the file, line, failure scenario, recommended fix, and whether it blocks PostgreSQL execution.

---

## Finding 1 — Test fixture performs destructive operations without checking FONELY_ALLOW_DESTRUCTIVE_TEST_DB

**Severity:** P1

**File:** `backend/tests/integration/postgres/conftest.py:46-74`

**Reproduction/failure scenario:**

The `migrated_postgres` session fixture automatically runs `alembic downgrade base` (line 52-58) and `alembic upgrade head` (line 59-65) at session start, and `alembic downgrade base` again at session end (line 67-73). These are destructive operations that drop all tables. The fixture does not check whether `FONELY_ALLOW_DESTRUCTIVE_TEST_DB=1` is set, so any invocation of `pytest -m postgres` with a valid `FONELY_TEST_DATABASE_URL` will immediately destroy the target database's schema without explicit opt-in.

**Recommended fix:**

Add a check at the top of `migrated_postgres` (or in `_test_database_url()`) that reads `os.environ.get("FONELY_ALLOW_DESTRUCTIVE_TEST_DB")` and calls `pytest.fail()` if it is not `"1"`. This ensures the destructive opt-in is propagated from the test script into the fixture.

```python
def _test_database_url() -> str:
    url = os.environ.get("FONELY_TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("FONELY_TEST_DATABASE_URL not set — PostgreSQL tests skipped")
    if os.environ.get("FONELY_ALLOW_DESTRUCTIVE_TEST_DB") != "1":
        pytest.fail(
            "FONELY_ALLOW_DESTRUCTIVE_TEST_DB=1 is required for PostgreSQL tests "
            "(the fixture performs downgrade/upgrade/truncate)"
        )
    # ... existing validation ...
```

**Blocks PostgreSQL execution:** No — tests will run, but without the safety check.

---

## Finding 2 — Test fixture passes DATABASE_URL to Alembic, not FONELY_TEST_DATABASE_URL

**Severity:** P1

**File:** `backend/tests/integration/postgres/conftest.py:49`

**Reproduction/failure scenario:**

The fixture sets `env["DATABASE_URL"] = postgres_database_url` (line 49) when calling Alembic as a subprocess. However, Alembic's `env.py` (line 20) uses `settings.database_url` from `fonely.core.config`. If `Settings.database_url` reads from `DATABASE_URL` rather than `FONELY_TEST_DATABASE_URL`, the fixture works. But if the application settings model reads a different env var, or if `DATABASE_URL` is already set in the environment to a production URL, the fixture could target the wrong database. The naming inconsistency between the test env var (`FONELY_TEST_DATABASE_URL`) and the Alembic env var (`DATABASE_URL`) is a latent safety risk.

**Recommended fix:**

Verify that `fonely.core.config.Settings.database_url` reads from `DATABASE_URL` (not from `FONELY_TEST_DATABASE_URL`), and document this coupling. Alternatively, have the fixture explicitly set both `DATABASE_URL` and unset any conflicting env vars before calling Alembic.

**Blocks PostgreSQL execution:** No — but risk of targeting the wrong database if `DATABASE_URL` is pre-set.

---

## Finding 3 — Test fixture database name validation uses substring "test" only

**Severity:** P2

**File:** `backend/tests/integration/postgres/conftest.py:35`

**Reproduction/failure scenario:**

The `_test_database_url()` function checks `if "test" not in database_name` (line 35). This permits database names like `my_test_production` or `contest_db`. While the test script (`scripts/test-postgres.sh`) applies stricter validation (rejecting known production names), the fixture itself does not. If a developer runs pytest directly without the test script, the weaker validation applies.

**Recommended fix:**

Add the same blocked-name list from the test script to the fixture, or extract shared validation into a utility that both the fixture and the script can use (noting Dev2 cannot create that utility since it would live in `backend/tests/`).

**Blocks PostgreSQL execution:** No.

---

## Finding 4 — No uv lockfile exists for reproducible CI builds

**Severity:** P2

**File:** `backend/pyproject.toml` (project root)

**Reproduction/failure scenario:**

The CI workflow runs `uv sync --all-extras` but there is no `uv.lock` file in the repository. This means CI builds are not reproducible — dependency versions may differ between runs. This is not a code defect but an infrastructure gap.

**Recommended fix:**

Run `uv lock` to generate `uv.lock` and commit it to version control. Until then, CI reproducibility is incomplete.

**Blocks PostgreSQL execution:** No.

---

*No additional findings at this time. This file will be updated if further defects are discovered during validation.*
