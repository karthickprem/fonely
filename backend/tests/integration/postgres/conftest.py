"""Safe PostgreSQL integration-test fixtures.

Destructive execution requires an explicit opt-in, a database named
``fonely_test`` or ``fonely_test_<suffix>``, and a dedicated test-role username.
All migrations are applied before the session and downgraded afterward.
"""

import asyncio
import os
import re
import subprocess
import warnings
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from urllib.parse import urlparse

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

BACKEND_ROOT = Path(__file__).parents[3]


class PostgresTestDatabaseUnrecoverableError(RuntimeError):
    """The test database is in a state the fixture could not repair.

    Raised INSTEAD OF a bare subprocess.CalledProcessError so a poisoned
    database never masquerades as a product regression. A killed run can leave
    alembic_version pointing at a partial/unknown revision, or tables present
    with no version row — states the naive downgrade/upgrade cannot recover
    from. The message names the database and the exact reset command, because
    "the database is unusable" and "a test failed" demand different responses
    from whoever reads the red (CEO #30).
    """


def _reset_public_schema(database_url: str) -> None:
    """Force a truly empty database: drop and recreate the public schema.

    This is the repair of last resort — it removes every table AND the
    alembic_version row regardless of which poisoned state the previous run
    left behind, so the subsequent `upgrade head` starts from a known base.
    Uses asyncpg directly (the only driver installed) rather than alembic,
    because alembic itself is what cannot cope with the poisoned state.
    """
    import asyncpg  # type: ignore[import-untyped]  # local import: repair path only

    dsn = database_url.replace("postgresql+asyncpg://", "postgresql://")

    async def _run() -> None:
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        finally:
            await conn.close()

    # Run on a dedicated thread with its own event loop, so this works both from
    # the sync session fixture AND from an async test that already holds a
    # running loop (asyncio.run would raise "cannot be called from a running
    # event loop" in the latter).
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(lambda: asyncio.run(_run())).result()


def _migration_head() -> str:
    """The single head of the migration chain, read from the scripts on disk.

    Tests that assert "the database is fully migrated" used to hardcode the
    revision, so every new migration broke a dozen unrelated tests and the
    only available fix was to retype the number. Deriving it keeps the
    assertion meaningful — it still fails if the upgrade did not run — without
    making it a maintenance tax on the next migration.
    """
    from alembic.script import ScriptDirectory

    script = ScriptDirectory(str(BACKEND_ROOT / "migrations"))
    heads = script.get_heads()
    if len(heads) != 1:
        raise RuntimeError(f"expected exactly one migration head, found {heads!r}")
    return heads[0]


MIGRATION_HEAD = _migration_head()


def _test_database_url() -> str:
    url = os.environ.get("FONELY_TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("FONELY_TEST_DATABASE_URL not set — PostgreSQL tests skipped")
    if os.environ.get("FONELY_ALLOW_DESTRUCTIVE_TEST_DB") != "1":
        pytest.fail("FONELY_ALLOW_DESTRUCTIVE_TEST_DB=1 is required")
    if not url.startswith("postgresql+asyncpg://"):
        pytest.fail("FONELY_TEST_DATABASE_URL must use postgresql+asyncpg")
    parsed = urlparse(url.replace("+asyncpg", ""))
    database_name = parsed.path.rsplit("/", 1)[-1].lower()
    username = (parsed.username or "").lower()
    hostname = (parsed.hostname or "").lower()
    if not re.fullmatch(r"fonely_test(?:_[a-z0-9_]+)?", database_name):
        pytest.fail("Database name must be fonely_test or fonely_test_<suffix>")
    if "test" not in username:
        pytest.fail("PostgreSQL user must be a dedicated test role containing 'test'")
    if hostname not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail("PostgreSQL integration tests require a local database host")
    return url


@pytest.fixture(scope="session")
def postgres_database_url() -> str:
    return _test_database_url()


def _alembic(command: list[str], database_url: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    return subprocess.run(
        [str(BACKEND_ROOT / ".venv" / "bin" / "alembic"), *command],
        cwd=BACKEND_ROOT,
        env=env,
        check=False,  # callers inspect returncode; we never want a bare CalledProcessError
        capture_output=True,
        text=True,
    )


def _bring_to_clean_head(database_url: str) -> None:
    """Migrate the database to a clean head, REPAIRING a poisoned start state.

    A prior run killed mid-migration can leave the database in a state the naive
    `downgrade base` cannot recover from (unknown alembic_version revision, or
    tables present with no version row). So:
      1. Try the normal path: downgrade base -> upgrade head.
      2. If EITHER step fails, do not trust the database — force a truly empty
         schema and upgrade from there.
      3. If the upgrade STILL fails, the database is unrecoverable: raise a
         distinct, loud error naming it and the reset command — never a generic
         setup error that reads like a product failure.

    IMPORTANT — the reset is not silent. A downgrade can fail for a LEGITIMATE,
    non-corrupt reason: a migration may deliberately REFUSE its own downgrade to
    protect data (e.g. 0018 raises rather than drop DPDP notice-evidence
    columns). Force-resetting answers that refusal with DROP SCHEMA, which is
    fine on a private test DB but must never read as "there was nothing to
    protect". So the underlying downgrade/upgrade stderr is surfaced on the reset
    path — absence of the guard's message must not read as the guard passing.
    """
    down = _alembic(["downgrade", "base"], database_url)
    if down.returncode == 0:
        up = _alembic(["upgrade", "head"], database_url)
        if up.returncode == 0:
            return  # clean path succeeded
        reset_cause = f"'upgrade head' failed after a clean downgrade:\n{up.stderr}"
    else:
        reset_cause = f"'downgrade base' failed:\n{down.stderr}"

    # Repair path: the poisoned state (or a deliberate downgrade refusal) defeated
    # the normal migrate. Surface WHY before we force-reset, so a guarded-schema
    # refusal is visible and not mistaken for a silent no-op.
    db_name = urlparse(database_url.replace("+asyncpg", "")).path.lstrip("/")
    warnings.warn(
        f"[pg-fixture] force-resetting test database {db_name!r} via "
        f"DROP SCHEMA public CASCADE. This can be a poisoned start state OR a "
        f"migration's DELIBERATE downgrade refusal — read the cause and do not "
        f"assume the guard passed. Underlying {reset_cause}",
        stacklevel=2,
    )
    _reset_public_schema(database_url)
    repaired = _alembic(["upgrade", "head"], database_url)
    if repaired.returncode != 0:
        raise PostgresTestDatabaseUnrecoverableError(
            f"PostgreSQL test database {db_name!r} is UNRECOVERABLE: a schema reset "
            f"followed by 'alembic upgrade head' still failed. This is a database "
            f"problem, NOT a product test failure. Reset it manually and re-run:\n"
            f"    psql '{database_url.replace('+asyncpg', '')}' "
            f"-c 'DROP SCHEMA public CASCADE; CREATE SCHEMA public;'\n"
            f"alembic stderr:\n{repaired.stderr}"
        )


@pytest.fixture(scope="session", autouse=True)
def migrated_postgres(postgres_database_url: str) -> Generator[None, None, None]:
    _bring_to_clean_head(postgres_database_url)
    yield
    # Teardown downgrade is best-effort: a repaired/clean DB downgrades fine, but
    # a run killed DURING teardown must not itself raise a poisoned-state error
    # over whatever the real failure was. The next run's setup repair handles it.
    _alembic(["downgrade", "base"], postgres_database_url)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def pg_engine(
    postgres_database_url: str,
    migrated_postgres: None,
) -> AsyncGenerator[AsyncEngine, None]:
    engine = create_async_engine(postgres_database_url, pool_pre_ping=True)
    yield engine
    await engine.dispose()


@pytest.fixture(scope="session")
def pg_session_factory(
    pg_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(pg_engine, expire_on_commit=False)


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def clean_database(
    pg_engine: AsyncEngine,
    postgres_database_url: str,
) -> AsyncGenerator[None, None]:
    try:
        yield
    finally:
        # Migration tests can fail while parked on an older revision. Restore the
        # full table set first so cleanup itself never masks the original failure.
        env = os.environ.copy()
        env["DATABASE_URL"] = postgres_database_url
        subprocess.run(
            [str(BACKEND_ROOT / ".venv" / "bin" / "alembic"), "upgrade", "head"],
            cwd=BACKEND_ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        async with pg_engine.begin() as connection:
            await connection.execute(
                text(
                    "TRUNCATE TABLE whatsapp_delivery_attempts, "
                    "business_whatsapp_channels, business_channel_identities, "
                    "whatsapp_inbound_events, business_daily_context, "
                    "conversation_turns, conversations, "
                    "notification_outbox, notification_manifests, "
                    "business_configuration_commits, business_onboarding_drafts, "
                    "owner_audit_log, appointment_commits, resource_allocations, "
                    "appointments, service_resource_eligibility, inventory_operations, "
                    "inventory_movements, inventory_reservations, order_line_items, orders, "
                    "inventory_balances, calls, pending_actions, resources, services, products, "
                    "schedule_exceptions, operating_schedules, business_users, "
                    "business_locales, business_capabilities, businesses "
                    "RESTART IDENTITY CASCADE"
                )
            )


async def seed_whatsapp_channel(
    session: AsyncSession,
    business_id: int = 1,
    phone_number_id: str = "phone-1",
) -> None:
    """Give a seeded business an active WhatsApp channel.

    Channel identity moved from WHATSAPP_BUSINESS_MAPPINGS into the
    business_whatsapp_channels table in migration 0016. Notification and
    booking-commit paths resolve the sending number from this row, so a test
    that seeds a clinic without one now reproduces a genuinely unconfigured
    business and fails commit with whatsapp_mapping_missing.
    """
    await session.execute(
        text(
            "INSERT INTO business_whatsapp_channels "
            "(business_id, phone_number_id, status, is_primary) "
            "VALUES (:bid, :pnid, 'active', true) "
            "ON CONFLICT (phone_number_id) DO NOTHING"
        ),
        {"bid": business_id, "pnid": phone_number_id},
    )
    await session.flush()


@pytest_asyncio.fixture(loop_scope="session")
async def pg_session(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession, None]:
    async with pg_session_factory() as session:
        yield session
        await session.rollback()
