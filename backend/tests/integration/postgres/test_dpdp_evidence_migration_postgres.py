"""Populated downgrade safety for migration 0018 (DPDP notice evidence).

The four ``dpdp_notice_*`` columns are the only record that a given patient was
read the notice before their data was collected. Nothing else in the schema can
rebuild them -- the transcript that could have carried the same fact is redacted
at 90 days -- and once dropped, "notice was given" is indistinguishable from
"no notice was ever played". That is exactly the state a regulator asks about.

So 0018 refuses to downgrade while evidence exists. This file proves the guard
actually fires against real Alembic and a live database, and that clearing the
evidence deliberately lets the downgrade through: a guard that cannot be
satisfied would just be a broken migration, and a guard nobody ran is not a
guard at all.
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.integration.postgres.conftest import MIGRATION_HEAD

pytestmark = pytest.mark.postgres
BACKEND_ROOT = Path(__file__).parents[3]

NOW = datetime(2026, 8, 12, 10, tzinfo=UTC)
DIGEST = "b" * 64


def _run_alembic(
    database_url: str,
    *args: str,
    check: bool = True,
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
        # The URL carries credentials; never let it reach the failure report.
        stderr = result.stderr.replace(database_url, "[REDACTED_DATABASE_URL]")
        pytest.fail(f"Alembic {' '.join(args)} failed:\n{stderr}")
    return result


async def _seed_business(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        exists = await conn.scalar(text("SELECT 1 FROM businesses WHERE id = 901"))
        if not exists:
            await conn.execute(
                text(
                    "INSERT INTO businesses "
                    "(id, name, category, primary_contact_phone, timezone, subscription) "
                    "VALUES (901, 'Notice Clinic', 'dental_clinic', '+919000000901', "
                    "'Asia/Kolkata', 'trial')"
                )
            )


async def _insert_call_with_evidence(engine: AsyncEngine, *, with_evidence: bool) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO calls "
                "(id, business_id, caller_phone, outcome, started_at, ended_at, "
                " dpdp_notice_completed_at, dpdp_notice_version, dpdp_notice_locale, "
                " dpdp_notice_content_digest) "
                "VALUES (9010, 901, '+919000000902', 'booked', :ts, :ts, "
                " :completed, :version, :locale, :digest)"
            ),
            {
                "ts": NOW,
                "completed": NOW if with_evidence else None,
                "version": "1" if with_evidence else None,
                "locale": "ta-IN" if with_evidence else None,
                "digest": DIGEST if with_evidence else None,
            },
        )


async def _delete_call(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM calls WHERE id = 9010"))


class TestPopulated0018Downgrade:
    async def test_stored_notice_evidence_blocks_downgrade(
        self, pg_engine: AsyncEngine, postgres_database_url: str
    ) -> None:
        await _seed_business(pg_engine)
        await _insert_call_with_evidence(pg_engine, with_evidence=True)
        try:
            result = _run_alembic(postgres_database_url, "downgrade", "0017", check=False)
            assert result.returncode != 0
            assert "refusing lossy downgrade" in result.stderr
            assert "DPDP notice evidence" in result.stderr

            # The refusal must leave the database where it was. A guard that
            # raises after dropping a column would report failure and still
            # have destroyed the evidence.
            async with pg_engine.connect() as conn:
                rev = await conn.scalar(text("SELECT version_num FROM alembic_version"))
                assert rev == MIGRATION_HEAD
                digest = await conn.scalar(
                    text("SELECT dpdp_notice_content_digest FROM calls WHERE id = 9010")
                )
                assert digest == DIGEST
        finally:
            await _delete_call(pg_engine)
            _run_alembic(postgres_database_url, "upgrade", "head", check=False)

    async def test_downgrade_proceeds_once_evidence_is_cleared(
        self, pg_engine: AsyncEngine, postgres_database_url: str
    ) -> None:
        """The guard is satisfiable, and a call with no notice does not trip it.

        Calls predating the notice carry all-NULL evidence and must not block
        an operator forever -- otherwise the only way out of the guard would be
        to edit the migration, which is how guards get deleted.
        """
        await _seed_business(pg_engine)
        await _insert_call_with_evidence(pg_engine, with_evidence=False)
        try:
            _run_alembic(postgres_database_url, "downgrade", "0017")

            async with pg_engine.connect() as conn:
                rev = await conn.scalar(text("SELECT version_num FROM alembic_version"))
                assert rev == "0017"
                columns = (
                    (
                        await conn.execute(
                            text(
                                "SELECT column_name FROM information_schema.columns "
                                "WHERE table_name = 'calls' AND column_name LIKE 'dpdp_notice%'"
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                assert columns == []
                # The call row itself survives the downgrade.
                assert await conn.scalar(text("SELECT count(*) FROM calls WHERE id = 9010")) == 1
        finally:
            _run_alembic(postgres_database_url, "upgrade", "head", check=False)
            await _delete_call(pg_engine)
