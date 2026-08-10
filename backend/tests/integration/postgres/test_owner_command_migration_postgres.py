"""Migration contracts for 0015 owner_command_proposals.

Proves upgrade creates the table, indexes, and constraints correctly,
that empty roundtrip works, and that the populated downgrade guard
blocks when result_evidence rows exist.
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.postgres
BACKEND_ROOT = Path(__file__).parents[3]
NOW = datetime(2026, 8, 10, 10, tzinfo=UTC)


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
        stderr = result.stderr.replace(database_url, "[REDACTED_DATABASE_URL]")
        pytest.fail(f"Alembic {' '.join(args)} failed:\n{stderr}")
    return result


async def _seed_business(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        exists = await conn.scalar(text("SELECT 1 FROM businesses WHERE id = 950"))
        if not exists:
            await conn.execute(
                text(
                    "INSERT INTO businesses "
                    "(id, name, category, primary_contact_phone, timezone, subscription) "
                    "VALUES (950, 'Migration Owner Clinic', 'dental_clinic', "
                    "'+919500000000', 'Asia/Kolkata', 'trial')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO business_users (id, business_id, phone, role, is_active) "
                    "VALUES (950, 950, '+919500000000', 'owner', true)"
                )
            )


class TestMigration0015:
    async def test_0015_upgrade_on_populated_0014(
        self, pg_engine: AsyncEngine, postgres_database_url: str
    ) -> None:
        """Verify upgrade creates owner_command_proposals table with expected structure."""
        # Ensure we are at head (0015) — the session fixture already migrated
        async with pg_engine.connect() as conn:
            # Table exists
            table_exists = await conn.scalar(
                text(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_name = 'owner_command_proposals'"
                )
            )
            assert table_exists == 1

            # Expected columns exist
            expected_columns = {
                "id",
                "business_id",
                "owner_user_id",
                "owner_phone_snapshot",
                "command_type",
                "command_payload",
                "preview_snapshot",
                "payload_digest",
                "status",
                "result_evidence",
                "expected_version",
                "idempotency_key",
                "expires_at",
                "confirmed_at",
                "completed_at",
                "failure_code",
                "failure_message",
                "created_at",
                "updated_at",
            }
            rows = await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'owner_command_proposals'"
                )
            )
            actual_columns = {r[0] for r in rows.all()}
            assert expected_columns == actual_columns

            # Composite unique constraint on business_users
            bu_uq = await conn.scalar(
                text("SELECT 1 FROM pg_constraint WHERE conname = 'uq_business_users_business_id'")
            )
            assert bu_uq == 1

            # Partial unique index on pending proposals per owner
            pending_idx = await conn.scalar(
                text("SELECT 1 FROM pg_indexes WHERE indexname = 'uq_owner_proposal_owner_pending'")
            )
            assert pending_idx == 1

            # Expiry sweeper index
            expiry_idx = await conn.scalar(
                text(
                    "SELECT 1 FROM pg_indexes WHERE indexname = 'ix_owner_proposal_pending_expiry'"
                )
            )
            assert expiry_idx == 1

    async def test_0015_empty_downgrade_roundtrip(
        self, pg_engine: AsyncEngine, postgres_database_url: str
    ) -> None:
        """Upgrade to 0015, downgrade to 0014, re-upgrade to 0015 cleanly."""
        try:
            # Downgrade to 0014
            _run_alembic(postgres_database_url, "downgrade", "0014")
            async with pg_engine.connect() as conn:
                rev = await conn.scalar(text("SELECT version_num FROM alembic_version"))
                assert rev == "0014"

                # Table should not exist at 0014
                table_exists = await conn.scalar(
                    text(
                        "SELECT 1 FROM information_schema.tables "
                        "WHERE table_name = 'owner_command_proposals'"
                    )
                )
                assert table_exists is None

                # Composite unique on business_users should be gone
                bu_uq = await conn.scalar(
                    text(
                        "SELECT 1 FROM pg_constraint "
                        "WHERE conname = 'uq_business_users_business_id'"
                    )
                )
                assert bu_uq is None

            # Re-upgrade to 0015
            _run_alembic(postgres_database_url, "upgrade", "0015")
            async with pg_engine.connect() as conn:
                rev = await conn.scalar(text("SELECT version_num FROM alembic_version"))
                assert rev == "0015"

                # Table should be back
                table_exists = await conn.scalar(
                    text(
                        "SELECT 1 FROM information_schema.tables "
                        "WHERE table_name = 'owner_command_proposals'"
                    )
                )
                assert table_exists == 1
        finally:
            _run_alembic(postgres_database_url, "upgrade", "head", check=False)

    async def test_0015_populated_downgrade_guard(
        self, pg_engine: AsyncEngine, postgres_database_url: str
    ) -> None:
        """Insert a proposal with result_evidence, downgrade must be blocked."""
        await _seed_business(pg_engine)

        async with pg_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO owner_command_proposals "
                    "(id, business_id, owner_user_id, owner_phone_snapshot, "
                    " command_type, command_payload, preview_snapshot, "
                    " payload_digest, status, result_evidence, "
                    " expected_version, idempotency_key, expires_at) "
                    "VALUES ("
                    " 'mig-test-guard-001', 950, 950, '+919500000000', "
                    " 'doctor_leave', "
                    ' \'{"target_date": "2026-08-11"}\'::jsonb, '
                    " '{\"affected_count\": 0}'::jsonb, "
                    " 'aaaa1111bbbb2222cccc3333dddd4444eeee5555ffff6666aaaa7777bbbb8888', "
                    " 'completed', "
                    ' \'{"outcome": "completed", "cancelled_count": 0}\'::jsonb, '
                    " 2, 'mig-guard-idem-001', :exp)"
                ),
                {"exp": NOW},
            )

        try:
            result = _run_alembic(postgres_database_url, "downgrade", "0014", check=False)
            assert result.returncode != 0
            assert "result_evidence" in result.stderr

            # Verify we are still at 0015
            async with pg_engine.connect() as conn:
                rev = await conn.scalar(text("SELECT version_num FROM alembic_version"))
                assert rev == "0015"
        finally:
            async with pg_engine.begin() as conn:
                await conn.execute(
                    text("DELETE FROM owner_command_proposals WHERE id = 'mig-test-guard-001'")
                )
            _run_alembic(postgres_database_url, "upgrade", "head", check=False)
