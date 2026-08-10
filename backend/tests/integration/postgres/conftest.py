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
from typing import Any
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


_SUITE_LOCK_ID = 0x466F6E656C795447  # "FonelyTG" as int64


def _reset_schema(database_url: str) -> None:
    """Drop and recreate public schema for a deterministic clean slate.

    Recovers from any residual migration state — partial downgrade,
    populated guard failure, or cross-session schema drift — without
    depending on alembic downgrade succeeding.
    """
    import asyncio

    async def _do_reset() -> None:
        engine = create_async_engine(database_url, isolation_level="AUTOCOMMIT")
        try:
            async with engine.begin() as conn:
                await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
                await conn.execute(text("CREATE SCHEMA public"))
                await conn.execute(text("GRANT ALL ON SCHEMA public TO PUBLIC"))
        finally:
            await engine.dispose()

    asyncio.run(_do_reset())


class _SuiteLock:
    """Session-lifetime advisory lock held via a dedicated event loop."""

    def __init__(self, database_url: str) -> None:
        import asyncio

        import asyncpg

        parsed = urlparse(database_url.replace("+asyncpg", ""))
        self._loop = asyncio.new_event_loop()
        self._conn: Any = self._loop.run_until_complete(
            asyncpg.connect(
                host=parsed.hostname or "localhost",
                port=parsed.port or 5432,
                user=parsed.username,
                password=parsed.password,
                database=parsed.path.lstrip("/"),
            )
        )
        acquired = self._loop.run_until_complete(
            self._conn.fetchval(f"SELECT pg_try_advisory_lock({_SUITE_LOCK_ID})")
        )
        if not acquired:
            self._loop.run_until_complete(self._conn.close())
            self._loop.close()
            pytest.fail(
                "Another test suite holds the database lock. "
                "Wait for it to finish or use a separate database "
                "(e.g. fonely_test_<suffix>)."
            )

    def release(self) -> None:
        import warnings

        try:
            self._loop.run_until_complete(self._conn.close())
        except Exception:
            warnings.warn(
                "Advisory lock connection did not close cleanly",
                stacklevel=2,
            )
        finally:
            self._loop.close()


@pytest.fixture(scope="session", autouse=True)
def migrated_postgres(postgres_database_url: str) -> Generator[None, None, None]:
    env = os.environ.copy()
    env["DATABASE_URL"] = postgres_database_url
    alembic = str(BACKEND_ROOT / ".venv" / "bin" / "alembic")
    lock = _SuiteLock(postgres_database_url)
    try:
        _reset_schema(postgres_database_url)
        subprocess.run(
            [alembic, "upgrade", "head"],
            cwd=BACKEND_ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        yield
        result = subprocess.run(
            [alembic, "downgrade", "base"],
            cwd=BACKEND_ROOT,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            import warnings

            warnings.warn(
                f"Teardown downgrade failed (exit {result.returncode})",
                stacklevel=1,
            )
    finally:
        lock.release()


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
                    "whatsapp_inbound_events, business_daily_context, "
                    "conversation_turns, conversations, "
                    "notification_outbox, "
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


@pytest_asyncio.fixture(loop_scope="session")
async def pg_session(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession, None]:
    async with pg_session_factory() as session:
        yield session
        await session.rollback()
