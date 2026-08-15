"""Populated downgrade safety for migration 0020 (callback_requested event type).

0020 adds ``callback_requested`` to the ``notification_outbox.event_type`` CHECK
constraint. Its downgrade recreates the pre-0020 constraint WITHOUT that value —
which would reject any existing callback_requested row (those carry a caller's
phone + booking intent). So 0020 refuses to downgrade while such rows exist,
mirroring 0013 (whatsapp_inbound_response) and 0019 (callback). This proves the
guard fires against real Alembic and a live database, and that clearing the rows
lets the downgrade through.

Head is read from disk (MIGRATION_HEAD), never a literal.
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

_PREV_REVISION = "0019"


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
        exists = await conn.scalar(text("SELECT 1 FROM businesses WHERE id = 903"))
        if not exists:
            await conn.execute(
                text(
                    "INSERT INTO businesses "
                    "(id, name, category, primary_contact_phone, timezone, subscription) "
                    "VALUES (903, 'Notify Clinic', 'dental_clinic', '+919000000903', "
                    "'Asia/Kolkata', 'trial')"
                )
            )


async def _insert_callback_notification(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO notification_outbox "
                "(id, business_id, event_type, entity_type, entity_id, recipient_type, "
                " recipient_phone, channel, payload, status, idempotency_key) "
                "VALUES (90300, 903, 'callback_requested', 'pending_action', 1, 'owner', "
                " '+919000000904', 'whatsapp', '{}'::jsonb, 'pending', 'cb-notif-mig')"
            )
        )


async def _delete_callback_notification(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM notification_outbox WHERE id = 90300"))


class TestPopulated0020Downgrade:
    async def test_stored_callback_notification_blocks_downgrade(
        self, pg_engine: AsyncEngine, postgres_database_url: str
    ) -> None:
        await _seed_business(pg_engine)
        await _insert_callback_notification(pg_engine)
        try:
            result = _run_alembic(postgres_database_url, "downgrade", _PREV_REVISION, check=False)
            assert result.returncode != 0
            assert "downgrade blocked" in result.stderr
            assert "callback_requested" in result.stderr

            async with pg_engine.connect() as conn:
                rev = await conn.scalar(text("SELECT version_num FROM alembic_version"))
                assert rev == MIGRATION_HEAD
                assert (
                    await conn.scalar(
                        text("SELECT count(*) FROM notification_outbox WHERE id = 90300")
                    )
                    == 1
                )
        finally:
            await _delete_callback_notification(pg_engine)
            _run_alembic(postgres_database_url, "upgrade", "head", check=False)

    async def test_downgrade_proceeds_once_notifications_cleared(
        self, pg_engine: AsyncEngine, postgres_database_url: str
    ) -> None:
        await _seed_business(pg_engine)
        try:
            _run_alembic(postgres_database_url, "downgrade", _PREV_REVISION)

            async with pg_engine.connect() as conn:
                rev = await conn.scalar(text("SELECT version_num FROM alembic_version"))
                assert rev == _PREV_REVISION
                constraint_def = await conn.scalar(
                    text(
                        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                        "WHERE conname = 'notification_event_type' "
                        "AND conrelid = 'notification_outbox'::regclass"
                    )
                )
                assert constraint_def is not None
                assert "callback_requested" not in constraint_def, (
                    "downgrade must restore the pre-0020 constraint WITHOUT callback_requested"
                )
        finally:
            _run_alembic(postgres_database_url, "upgrade", "head", check=False)
