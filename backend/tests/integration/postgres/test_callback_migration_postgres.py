"""Populated downgrade safety for migration 0019 (callback pending-action type).

0019 adds ``callback`` to the ``pending_actions.action_type`` CHECK constraint.
Its downgrade recreates the pre-0019 constraint WITHOUT ``callback`` — which
would reject any existing callback row. Those rows carry caller PII and booking
intent, so 0019 refuses to downgrade while callbacks exist (mirroring 0018's
DPDP guard). This proves the guard fires against real Alembic and a live
database, and that clearing the callbacks lets the downgrade through — a guard
that could not be satisfied would just be a broken migration, and a guard nobody
ran is not a guard at all.

The head is read from disk (MIGRATION_HEAD) — never a literal — so this test
does not need editing when the next migration lands.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.integration.postgres.conftest import MIGRATION_HEAD

pytestmark = pytest.mark.postgres
BACKEND_ROOT = Path(__file__).parents[3]

_PREV_REVISION = "0018"


def _run_alembic(
    database_url: str, *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    result = subprocess.run(
        [str(BACKEND_ROOT / ".venv" / "bin" / "alembic"), *args],
        cwd=BACKEND_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        stderr = result.stderr.replace(database_url, "[REDACTED_DATABASE_URL]")
        pytest.fail(f"Alembic {' '.join(args)} failed:\n{stderr}")
    return result


async def _seed_business(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        exists = await conn.scalar(text("SELECT 1 FROM businesses WHERE id = 902"))
        if not exists:
            await conn.execute(
                text(
                    "INSERT INTO businesses "
                    "(id, name, category, primary_contact_phone, timezone, subscription) "
                    "VALUES (902, 'Callback Clinic', 'dental_clinic', '+919000000902', "
                    "'Asia/Kolkata', 'trial')"
                )
            )


async def _insert_callback(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO pending_actions "
                "(id, business_id, action_type, payload_schema_version, proposed_payload, "
                " payload_digest, status, expires_at, idempotency_key, initiated_by, version) "
                "VALUES (90200, 902, 'callback', 1, '{}'::jsonb, 'd-cb-mig', "
                " 'awaiting_confirmation', now() + interval '1 hour', 'cb-mig', "
                " '+919000000903', 1)"
            )
        )


async def _delete_callback(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM pending_actions WHERE id = 90200"))


class TestPopulated0019Downgrade:
    async def test_stored_callback_blocks_downgrade(
        self, pg_engine: AsyncEngine, postgres_database_url: str
    ) -> None:
        await _seed_business(pg_engine)
        await _insert_callback(pg_engine)
        try:
            result = _run_alembic(postgres_database_url, "downgrade", _PREV_REVISION, check=False)
            assert result.returncode != 0
            assert "refusing lossy downgrade" in result.stderr
            assert "callback" in result.stderr

            # The refusal must leave the database where it was — at head, with the
            # callback row intact (a guard that raised after altering the
            # constraint would report failure and still have damaged state).
            async with pg_engine.connect() as conn:
                rev = await conn.scalar(text("SELECT version_num FROM alembic_version"))
                assert rev == MIGRATION_HEAD
                assert (
                    await conn.scalar(text("SELECT count(*) FROM pending_actions WHERE id = 90200"))
                    == 1
                )
        finally:
            await _delete_callback(pg_engine)
            _run_alembic(postgres_database_url, "upgrade", "head", check=False)

    async def test_downgrade_proceeds_once_callbacks_cleared(
        self, pg_engine: AsyncEngine, postgres_database_url: str
    ) -> None:
        """The guard is satisfiable: with no callback rows, downgrade proceeds and
        the pre-0019 constraint (without 'callback') is restored."""
        await _seed_business(pg_engine)
        # No callback inserted.
        try:
            _run_alembic(postgres_database_url, "downgrade", _PREV_REVISION)

            async with pg_engine.connect() as conn:
                rev = await conn.scalar(text("SELECT version_num FROM alembic_version"))
                assert rev == _PREV_REVISION
                constraint_def = await conn.scalar(
                    text(
                        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                        "WHERE conname = 'action_type' "
                        "AND conrelid = 'pending_actions'::regclass"
                    )
                )
                assert constraint_def is not None
                assert "callback" not in constraint_def, (
                    "downgrade must restore the pre-0019 constraint WITHOUT 'callback'"
                )
        finally:
            _run_alembic(postgres_database_url, "upgrade", "head", check=False)
