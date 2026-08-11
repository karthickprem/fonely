"""Safe PostgreSQL integration-test fixtures.

Destructive execution requires an explicit opt-in, a database named
``fonely_test`` or ``fonely_test_<suffix>``, and a dedicated test-role username.
All migrations are applied before the session and downgraded afterward.
"""

import os
import re
import subprocess
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


@pytest.fixture(scope="session", autouse=True)
def migrated_postgres(postgres_database_url: str) -> Generator[None, None, None]:
    env = os.environ.copy()
    env["DATABASE_URL"] = postgres_database_url
    alembic = str(BACKEND_ROOT / ".venv" / "bin" / "alembic")
    subprocess.run(
        [alembic, "downgrade", "base"],
        cwd=BACKEND_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [alembic, "upgrade", "head"],
        cwd=BACKEND_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    yield
    subprocess.run(
        [alembic, "downgrade", "base"],
        cwd=BACKEND_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


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
                    "business_whatsapp_channels, "
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
