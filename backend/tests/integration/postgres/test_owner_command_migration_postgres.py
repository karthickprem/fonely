"""Live PostgreSQL migration evidence for owner command proposal revision 0015."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.postgres
BACKEND_ROOT = Path(__file__).parents[3]


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
        sanitized = result.stderr.replace(database_url, "[REDACTED_DATABASE_URL]")
        pytest.fail(f"Alembic {' '.join(args)} failed:\n{sanitized}")
    return result


async def test_populated_0014_upgrade_and_empty_roundtrip(
    pg_engine: AsyncEngine, postgres_database_url: str
) -> None:
    _run_alembic(postgres_database_url, "downgrade", "0014")
    async with pg_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO businesses "
                "(id,name,category,primary_contact_phone,timezone,subscription) "
                "VALUES (950,'Migration Owner Clinic','clinic','+919500000000',"
                "'Asia/Kolkata','trial')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO business_users (business_id,phone,role,is_active) "
                "VALUES (950,'+919500000000','owner',true)"
            )
        )
    _run_alembic(postgres_database_url, "upgrade", "0015")
    async with pg_engine.connect() as conn:
        assert await conn.scalar(text("SELECT version_num FROM alembic_version")) == "0015"
        assert (
            await conn.scalar(text("SELECT to_regclass('owner_command_proposals') IS NOT NULL"))
            is True
        )
    _run_alembic(postgres_database_url, "downgrade", "0014")
    _run_alembic(postgres_database_url, "upgrade", "0015")


async def test_populated_0015_downgrade_fails_closed(
    pg_engine: AsyncEngine, postgres_database_url: str
) -> None:
    async with pg_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO businesses "
                "(id,name,category,primary_contact_phone,timezone,subscription) "
                "VALUES (951,'Evidence Clinic','clinic','+919510000000',"
                "'Asia/Kolkata','trial') ON CONFLICT DO NOTHING"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO business_users "
                "(id,business_id,phone,role,is_active) "
                "VALUES (951,951,'+919510000000','owner',true) ON CONFLICT DO NOTHING"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO owner_command_proposals "
                "(id,business_id,owner_user_id,owner_phone_snapshot,command_type,"
                "command_payload,preview_snapshot,payload_digest,status,expected_version,"
                "idempotency_key,expires_at) VALUES "
                "('migration-proposal',951,951,'+919510000000','close_clinic',"
                "'{}','{}',:digest,'pending_confirmation',1,'migration-key',now()+interval '5 min')"
            ),
            {"digest": "a" * 64},
        )
    result = _run_alembic(postgres_database_url, "downgrade", "0014", check=False)
    assert result.returncode != 0
    assert "owner command proposal evidence exists" in result.stderr
    async with pg_engine.connect() as conn:
        assert await conn.scalar(text("SELECT version_num FROM alembic_version")) == "0015"
    async with pg_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM owner_command_proposals WHERE id='migration-proposal'")
        )
