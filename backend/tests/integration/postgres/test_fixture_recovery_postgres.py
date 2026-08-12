"""CEO #30 — the PostgreSQL session fixture must recover from a poisoned start.

A run killed mid-migration can leave the test database in a state the naive
`alembic downgrade base` cannot recover from: alembic_version pointing at an
unknown revision, or tables present with no version row. The old fixture ran the
downgrade with check=True, so the next run died on a generic setup ERROR that
read like a product regression — and three sessions, the reviewer included,
chased phantom regressions because of it.

These tests prove the repair path directly: they CREATE a throwaway private
database, actually corrupt it into each poisoned state, and assert the fixture's
recovery helper brings it back to a clean head — or, when the database is
genuinely unrecoverable, fails LOUD and DISTINCT (a named exception with reset
instructions) rather than a bare CalledProcessError.

The tests manage their OWN private `fonely_test_<suffix>` databases and drop
them; they never touch the session database or any other session's database.
"""

import uuid

import asyncpg  # type: ignore[import-untyped]
import pytest

from tests.integration.postgres.conftest import (
    MIGRATION_HEAD,
    PostgresTestDatabaseUnrecoverableError,
    _bring_to_clean_head,
    _reset_public_schema,
)

# `postgres_database_url` is a session-scoped fixture defined in conftest.py; it
# is available to the tests below without importing it (importing it as a symbol
# would shadow the fixture and trip ruff's redefinition check).

pytestmark = pytest.mark.postgres


def _base_dsn(session_url: str) -> str:
    """Plain-driver DSN for the maintenance 'postgres' database, derived from the
    session URL so we inherit its host/port/user without hardcoding anything."""
    plain = session_url.replace("postgresql+asyncpg://", "postgresql://")
    head, _, _tail = plain.rpartition("/")
    return f"{head}/postgres"


async def _create_scratch_db(base_dsn: str) -> str:
    name = f"fonely_test_recov_{uuid.uuid4().hex[:12]}"
    conn = await asyncpg.connect(base_dsn)
    try:
        await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()
    return name


async def _drop_scratch_db(base_dsn: str, name: str) -> None:
    conn = await asyncpg.connect(base_dsn)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await conn.close()


def _scratch_url(session_url: str, name: str) -> str:
    plain_head = session_url.rsplit("/", 1)[0]
    return f"{plain_head}/{name}"


async def _current_version(scratch_url: str) -> str | None:
    dsn = scratch_url.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn)
    try:
        version = await conn.fetchval("SELECT version_num FROM alembic_version")
        return str(version) if version is not None else None
    except asyncpg.exceptions.UndefinedTableError:
        return None
    finally:
        await conn.close()


async def _poison_bad_revision(scratch_url: str) -> None:
    """Migrate to head, then point alembic_version at a revision that does not
    exist — exactly what a run killed mid-migration can leave behind."""
    _bring_to_clean_head(scratch_url)
    dsn = scratch_url.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("UPDATE alembic_version SET version_num = 'deadbeef_partial'")
    finally:
        await conn.close()


async def _poison_orphan_tables(scratch_url: str) -> None:
    """Migrate to head, then drop alembic_version so tables exist with no
    version row — the naive downgrade no-ops and the next upgrade collides."""
    _bring_to_clean_head(scratch_url)
    dsn = scratch_url.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("DROP TABLE alembic_version")
    finally:
        await conn.close()


@pytest.fixture
async def scratch_db(postgres_database_url: str):  # type: ignore[no-untyped-def]
    """A throwaway private database, created and dropped here. Yields its URL."""
    base = _base_dsn(postgres_database_url)
    name = await _create_scratch_db(base)
    try:
        yield _scratch_url(postgres_database_url, name)
    finally:
        await _drop_scratch_db(base, name)


@pytest.mark.parametrize(
    "poison", [_poison_bad_revision, _poison_orphan_tables], ids=["bad-rev", "orphan-tables"]
)
async def test_fixture_recovers_from_a_killed_run(scratch_db: str, poison) -> None:  # type: ignore[no-untyped-def]
    # Corrupt the database into a state a killed mid-migration run leaves behind.
    await poison(scratch_db)

    # The recovery helper must bring it back to a clean head without raising.
    _bring_to_clean_head(scratch_db)

    # Row-level proof it is genuinely at head, not merely "did not error".
    assert await _current_version(scratch_db) == MIGRATION_HEAD


async def test_reset_surfaces_the_underlying_cause_not_a_silent_wipe(scratch_db: str) -> None:
    """The reset path must SAY why it fired, so a migration's deliberate
    downgrade refusal (e.g. a data-protection guard) is visible and never mistaken
    for a silent no-op. Absence of the guard's message must not read as success.
    """
    # Poison so the normal downgrade fails; the recovery then force-resets.
    await _poison_bad_revision(scratch_db)

    with pytest.warns(UserWarning) as recorded:
        _bring_to_clean_head(scratch_db)

    messages = "\n".join(str(w.message) for w in recorded)
    # It announces the force-reset AND names the non-corrupt alternative cause,
    # AND carries the underlying alembic stderr — so a guard refusal is not swallowed.
    assert "force-resetting" in messages
    assert "downgrade refusal" in messages
    assert "Underlying" in messages
    # And it still lands the database at a clean head.
    assert await _current_version(scratch_db) == MIGRATION_HEAD


async def test_unrecoverable_database_fails_loud_and_distinct(
    scratch_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Simulate a database that cannot be brought to head even after a reset:
    # make the post-reset `upgrade head` fail. The fixture must raise its OWN
    # distinct error naming the database and the reset command — never a bare
    # CalledProcessError that reads like a product failure.
    import tests.integration.postgres.conftest as conftest

    real_alembic = conftest._alembic

    def _always_fail_upgrade(command: list[str], database_url: str):  # type: ignore[no-untyped-def]
        if command[:2] == ["upgrade", "head"]:
            import subprocess

            return subprocess.CompletedProcess(
                args=command, returncode=1, stdout="", stderr="simulated unrecoverable upgrade"
            )
        return real_alembic(command, database_url)

    monkeypatch.setattr(conftest, "_alembic", _always_fail_upgrade)

    with pytest.raises(PostgresTestDatabaseUnrecoverableError) as exc:
        conftest._bring_to_clean_head(scratch_db)

    message = str(exc.value)
    # The message must be actionable: it names the database and the reset command,
    # so whoever reads the red knows this is a database problem, not a test bug.
    assert "UNRECOVERABLE" in message
    assert "DROP SCHEMA public CASCADE" in message


async def test_reset_public_schema_truly_empties(scratch_db: str) -> None:
    # The repair-of-last-resort must remove tables AND alembic_version regardless
    # of poison, so the follow-up upgrade starts from a known base.
    await _poison_orphan_tables(scratch_db)  # leaves product tables present
    _reset_public_schema(scratch_db)

    dsn = scratch_db.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn)
    try:
        table_count = await conn.fetchval(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'"
        )
    finally:
        await conn.close()
    assert table_count == 0
