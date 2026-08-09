"""Populated downgrade safety for durable inbox migrations 0013-0014.

Proves that preflight checks prevent lossy downgrade, that safe downgrade
succeeds, and that re-upgrade preserves dedup evidence. Exercises real
Alembic against a live PostgreSQL database with populated rows in every
relevant status.
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


NOW = datetime(2026, 8, 9, 10, tzinfo=UTC)


async def _seed_business(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        exists = await conn.scalar(text("SELECT 1 FROM businesses WHERE id = 900"))
        if not exists:
            await conn.execute(
                text(
                    "INSERT INTO businesses "
                    "(id, name, category, primary_contact_phone, timezone, subscription) "
                    "VALUES (900, 'Migration Clinic', 'dental_clinic', '+919000000000', "
                    "'Asia/Kolkata', 'trial')"
                )
            )


class TestPopulated0014Downgrade:
    async def test_inflight_received_blocks_downgrade(
        self, pg_engine: AsyncEngine, postgres_database_url: str
    ) -> None:
        await _seed_business(pg_engine)
        async with pg_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO whatsapp_inbound_events "
                    "(message_id, business_id, phone_number_id, sender_phone, "
                    " message_type, message_body, status, attempts, max_attempts, "
                    " provider_timestamp) "
                    "VALUES ('wamid.mig-received', 900, 'phone-900', '919000000001', "
                    " 'text', 'hello', 'received', 0, 5, :ts)"
                ),
                {"ts": NOW},
            )
        try:
            result = _run_alembic(postgres_database_url, "downgrade", "0013", check=False)
            assert result.returncode != 0
            assert "non-terminal inbound events" in result.stderr
            async with pg_engine.connect() as conn:
                rev = await conn.scalar(text("SELECT version_num FROM alembic_version"))
                assert rev == "0014"
        finally:
            async with pg_engine.begin() as conn:
                await conn.execute(
                    text(
                        "DELETE FROM whatsapp_inbound_events "
                        "WHERE message_id = 'wamid.mig-received'"
                    )
                )
            _run_alembic(postgres_database_url, "upgrade", "head", check=False)

    async def test_inflight_processing_blocks_downgrade(
        self, pg_engine: AsyncEngine, postgres_database_url: str
    ) -> None:
        await _seed_business(pg_engine)
        async with pg_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO whatsapp_inbound_events "
                    "(message_id, business_id, phone_number_id, sender_phone, "
                    " message_type, message_body, status, attempts, max_attempts, "
                    " provider_timestamp, claim_token, claimed_at, lease_expires_at) "
                    "VALUES ('wamid.mig-processing', 900, 'phone-900', '919000000001', "
                    " 'text', 'hello', 'processing', 1, 5, :ts, "
                    " 'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11', :ts, :ts)"
                ),
                {"ts": NOW},
            )
        try:
            result = _run_alembic(postgres_database_url, "downgrade", "0013", check=False)
            assert result.returncode != 0
            assert "non-terminal inbound events" in result.stderr
            async with pg_engine.connect() as conn:
                rev = await conn.scalar(text("SELECT version_num FROM alembic_version"))
                assert rev == "0014"
        finally:
            async with pg_engine.begin() as conn:
                await conn.execute(
                    text(
                        "DELETE FROM whatsapp_inbound_events "
                        "WHERE message_id = 'wamid.mig-processing'"
                    )
                )
            _run_alembic(postgres_database_url, "upgrade", "head", check=False)

    async def test_domain_processed_blocks_downgrade(
        self, pg_engine: AsyncEngine, postgres_database_url: str
    ) -> None:
        await _seed_business(pg_engine)
        async with pg_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO whatsapp_inbound_events "
                    "(message_id, business_id, phone_number_id, sender_phone, "
                    " message_type, message_body, status, attempts, max_attempts, "
                    " provider_timestamp) "
                    "VALUES ('wamid.mig-domproc', 900, 'phone-900', '919000000001', "
                    " 'text', 'hello', 'domain_processed', 1, 5, :ts)"
                ),
                {"ts": NOW},
            )
        try:
            result = _run_alembic(postgres_database_url, "downgrade", "0013", check=False)
            assert result.returncode != 0
            assert "non-terminal inbound events" in result.stderr
            async with pg_engine.connect() as conn:
                rev = await conn.scalar(text("SELECT version_num FROM alembic_version"))
                assert rev == "0014"
        finally:
            async with pg_engine.begin() as conn:
                await conn.execute(
                    text(
                        "DELETE FROM whatsapp_inbound_events WHERE message_id = 'wamid.mig-domproc'"
                    )
                )
            _run_alembic(postgres_database_url, "upgrade", "head", check=False)

    async def test_completed_rows_become_tombstones_not_inflight(
        self, pg_engine: AsyncEngine, postgres_database_url: str
    ) -> None:
        await _seed_business(pg_engine)
        async with pg_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO whatsapp_inbound_events "
                    "(message_id, business_id, phone_number_id, sender_phone, "
                    " message_type, status, attempts, max_attempts, "
                    " provider_timestamp, completed_at) "
                    "VALUES ('wamid.mig-completed', 900, 'phone-900', '919000000001', "
                    " 'text', 'completed', 1, 5, :ts, :ts)"
                ),
                {"ts": NOW},
            )
            await conn.execute(
                text(
                    "INSERT INTO whatsapp_inbound_events "
                    "(message_id, business_id, phone_number_id, sender_phone, "
                    " message_type, status, attempts, max_attempts, "
                    " provider_timestamp, dead_lettered_at) "
                    "VALUES ('wamid.mig-deadletter', 900, 'phone-900', '919000000001', "
                    " 'text', 'dead_letter', 5, 5, :ts, :ts)"
                ),
                {"ts": NOW},
            )
        try:
            _run_alembic(postgres_database_url, "downgrade", "0013")
            async with pg_engine.connect() as conn:
                completed = await conn.scalar(
                    text(
                        "SELECT 1 FROM whatsapp_processed_messages "
                        "WHERE message_id = 'wamid.mig-completed'"
                    )
                )
                dead = await conn.scalar(
                    text(
                        "SELECT 1 FROM whatsapp_processed_messages "
                        "WHERE message_id = 'wamid.mig-deadletter'"
                    )
                )
                assert completed == 1
                assert dead == 1
            _run_alembic(postgres_database_url, "upgrade", "head")
            async with pg_engine.connect() as conn:
                completed_inbox = await conn.scalar(
                    text(
                        "SELECT 1 FROM whatsapp_inbound_events "
                        "WHERE message_id = 'wamid.mig-completed'"
                    )
                )
                assert completed_inbox == 1
        finally:
            async with pg_engine.begin() as conn:
                await conn.execute(
                    text(
                        "DELETE FROM whatsapp_inbound_events "
                        "WHERE message_id IN ('wamid.mig-completed', 'wamid.mig-deadletter')"
                    )
                )
            _run_alembic(postgres_database_url, "upgrade", "head", check=False)

    async def test_response_failed_blocks_downgrade(
        self, pg_engine: AsyncEngine, postgres_database_url: str
    ) -> None:
        await _seed_business(pg_engine)
        async with pg_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO whatsapp_inbound_events "
                    "(message_id, business_id, phone_number_id, sender_phone, "
                    " message_type, status, attempts, max_attempts, "
                    " provider_timestamp, dead_lettered_at) "
                    "VALUES ('wamid.mig-respfailed', 900, 'phone-900', '919000000001', "
                    " 'text', 'response_failed', 5, 5, :ts, :ts)"
                ),
                {"ts": NOW},
            )
        try:
            result = _run_alembic(postgres_database_url, "downgrade", "0013", check=False)
            assert result.returncode != 0
            assert "response_failed" in result.stderr
            async with pg_engine.connect() as conn:
                rev = await conn.scalar(text("SELECT version_num FROM alembic_version"))
                assert rev == "0014"
        finally:
            async with pg_engine.begin() as conn:
                await conn.execute(
                    text(
                        "DELETE FROM whatsapp_inbound_events "
                        "WHERE message_id = 'wamid.mig-respfailed'"
                    )
                )
            _run_alembic(postgres_database_url, "upgrade", "head", check=False)

    async def test_safe_remediation_permits_downgrade_after_blocking(
        self, pg_engine: AsyncEngine, postgres_database_url: str
    ) -> None:
        await _seed_business(pg_engine)
        async with pg_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO whatsapp_inbound_events "
                    "(message_id, business_id, phone_number_id, sender_phone, "
                    " message_type, message_body, status, attempts, max_attempts, "
                    " provider_timestamp) "
                    "VALUES ('wamid.mig-remediate', 900, 'phone-900', '919000000001', "
                    " 'text', 'hello', 'received', 0, 5, :ts)"
                ),
                {"ts": NOW},
            )
        result = _run_alembic(postgres_database_url, "downgrade", "0013", check=False)
        assert result.returncode != 0
        async with pg_engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE whatsapp_inbound_events SET status='completed', "
                    "completed_at=NOW(), message_body=NULL "
                    "WHERE message_id = 'wamid.mig-remediate'"
                )
            )
        try:
            _run_alembic(postgres_database_url, "downgrade", "0013")
            _run_alembic(postgres_database_url, "upgrade", "head")
        finally:
            async with pg_engine.begin() as conn:
                await conn.execute(
                    text(
                        "DELETE FROM whatsapp_inbound_events "
                        "WHERE message_id = 'wamid.mig-remediate'"
                    )
                )
            _run_alembic(postgres_database_url, "upgrade", "head", check=False)

    async def test_retryable_failed_blocks_downgrade(
        self, pg_engine: AsyncEngine, postgres_database_url: str
    ) -> None:
        await _seed_business(pg_engine)
        async with pg_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO whatsapp_inbound_events "
                    "(message_id, business_id, phone_number_id, sender_phone, "
                    " message_type, status, attempts, max_attempts, "
                    " provider_timestamp) "
                    "VALUES ('wamid.mig-retryable', 900, 'phone-900', '919000000001', "
                    " 'text', 'failed', 3, 5, :ts)"
                ),
                {"ts": NOW},
            )
        try:
            result = _run_alembic(postgres_database_url, "downgrade", "0013", check=False)
            assert result.returncode != 0
            assert "non-terminal inbound events" in result.stderr
            async with pg_engine.connect() as conn:
                rev = await conn.scalar(text("SELECT version_num FROM alembic_version"))
                assert rev == "0014"
        finally:
            async with pg_engine.begin() as conn:
                await conn.execute(
                    text(
                        "DELETE FROM whatsapp_inbound_events "
                        "WHERE message_id = 'wamid.mig-retryable'"
                    )
                )
            _run_alembic(postgres_database_url, "upgrade", "head", check=False)

    async def test_exhausted_failed_also_blocks_downgrade(
        self, pg_engine: AsyncEngine, postgres_database_url: str
    ) -> None:
        await _seed_business(pg_engine)
        async with pg_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO whatsapp_inbound_events "
                    "(message_id, business_id, phone_number_id, sender_phone, "
                    " message_type, status, attempts, max_attempts, "
                    " provider_timestamp) "
                    "VALUES ('wamid.mig-exhausted', 900, 'phone-900', '919000000001', "
                    " 'text', 'failed', 5, 5, :ts)"
                ),
                {"ts": NOW},
            )
        try:
            result = _run_alembic(postgres_database_url, "downgrade", "0013", check=False)
            assert result.returncode != 0
            assert "non-terminal inbound events" in result.stderr
            async with pg_engine.connect() as conn:
                rev = await conn.scalar(text("SELECT version_num FROM alembic_version"))
                assert rev == "0014"
        finally:
            async with pg_engine.begin() as conn:
                await conn.execute(
                    text(
                        "DELETE FROM whatsapp_inbound_events "
                        "WHERE message_id = 'wamid.mig-exhausted'"
                    )
                )
            _run_alembic(postgres_database_url, "upgrade", "head", check=False)

    async def test_dead_letter_becomes_tombstone(
        self, pg_engine: AsyncEngine, postgres_database_url: str
    ) -> None:
        await _seed_business(pg_engine)
        async with pg_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO whatsapp_inbound_events "
                    "(message_id, business_id, phone_number_id, sender_phone, "
                    " message_type, status, attempts, max_attempts, "
                    " provider_timestamp, dead_lettered_at) "
                    "VALUES ('wamid.mig-dl-tomb', 900, 'phone-900', '919000000001', "
                    " 'text', 'dead_letter', 5, 5, :ts, :ts)"
                ),
                {"ts": NOW},
            )
        try:
            _run_alembic(postgres_database_url, "downgrade", "0013")
            async with pg_engine.connect() as conn:
                is_tombstone = await conn.scalar(
                    text(
                        "SELECT 1 FROM whatsapp_processed_messages "
                        "WHERE message_id = 'wamid.mig-dl-tomb'"
                    )
                )
                assert is_tombstone == 1
            _run_alembic(postgres_database_url, "upgrade", "head")
        finally:
            async with pg_engine.begin() as conn:
                await conn.execute(
                    text(
                        "DELETE FROM whatsapp_inbound_events WHERE message_id = 'wamid.mig-dl-tomb'"
                    )
                )
            _run_alembic(postgres_database_url, "upgrade", "head", check=False)

    async def test_retryable_failed_remediation_permits_downgrade(
        self, pg_engine: AsyncEngine, postgres_database_url: str
    ) -> None:
        await _seed_business(pg_engine)
        async with pg_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO whatsapp_inbound_events "
                    "(message_id, business_id, phone_number_id, sender_phone, "
                    " message_type, status, attempts, max_attempts, "
                    " provider_timestamp) "
                    "VALUES ('wamid.mig-fail-rem', 900, 'phone-900', '919000000001', "
                    " 'text', 'failed', 2, 5, :ts)"
                ),
                {"ts": NOW},
            )
        result = _run_alembic(postgres_database_url, "downgrade", "0013", check=False)
        assert result.returncode != 0
        async with pg_engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE whatsapp_inbound_events SET status='completed', "
                    "completed_at=NOW(), message_body=NULL "
                    "WHERE message_id = 'wamid.mig-fail-rem'"
                )
            )
        try:
            _run_alembic(postgres_database_url, "downgrade", "0013")
            async with pg_engine.connect() as conn:
                is_tombstone = await conn.scalar(
                    text(
                        "SELECT 1 FROM whatsapp_processed_messages "
                        "WHERE message_id = 'wamid.mig-fail-rem'"
                    )
                )
                assert is_tombstone == 1
            _run_alembic(postgres_database_url, "upgrade", "head")
        finally:
            async with pg_engine.begin() as conn:
                await conn.execute(
                    text(
                        "DELETE FROM whatsapp_inbound_events "
                        "WHERE message_id = 'wamid.mig-fail-rem'"
                    )
                )
            _run_alembic(postgres_database_url, "upgrade", "head", check=False)


class TestPopulated0013Downgrade:
    async def test_inbound_response_notifications_block_0013_downgrade(
        self, pg_engine: AsyncEngine, postgres_database_url: str
    ) -> None:
        await _seed_business(pg_engine)
        async with pg_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO notification_outbox "
                    "(business_id, event_type, entity_type, entity_id, "
                    " recipient_type, recipient_phone, channel, "
                    " idempotency_key, payload, status) "
                    "VALUES (900, 'whatsapp_inbound_response', 'whatsapp_inbound_event', "
                    " 1, 'patient', '+919000000001', 'whatsapp', "
                    " 'mig-test-inbound-resp', '{}'::jsonb, 'pending')"
                )
            )
        try:
            _run_alembic(postgres_database_url, "downgrade", "0013", check=False)
            result = _run_alembic(postgres_database_url, "downgrade", "0012", check=False)
            assert result.returncode != 0
            assert "whatsapp_inbound_response" in result.stderr
            async with pg_engine.connect() as conn:
                rev = await conn.scalar(text("SELECT version_num FROM alembic_version"))
                assert rev == "0013"
        finally:
            async with pg_engine.begin() as conn:
                await conn.execute(
                    text(
                        "DELETE FROM notification_outbox "
                        "WHERE business_id = 900 "
                        "AND event_type = 'whatsapp_inbound_response'"
                    )
                )
            _run_alembic(postgres_database_url, "upgrade", "head", check=False)

    async def test_empty_outbox_permits_0013_downgrade(
        self, pg_engine: AsyncEngine, postgres_database_url: str
    ) -> None:
        await _seed_business(pg_engine)
        async with pg_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO notification_outbox "
                    "(business_id, event_type, entity_type, entity_id, "
                    " recipient_type, recipient_phone, channel, "
                    " idempotency_key, payload, status) "
                    "VALUES (900, 'appointment_confirmed', 'appointment', "
                    " 1, 'patient', '+919000000001', 'whatsapp', "
                    " 'mig-test-appt-confirm', '{}'::jsonb, 'pending')"
                )
            )
        try:
            _run_alembic(postgres_database_url, "downgrade", "0013")
            _run_alembic(postgres_database_url, "downgrade", "0012")
            _run_alembic(postgres_database_url, "upgrade", "head")
        finally:
            async with pg_engine.begin() as conn:
                await conn.execute(text("DELETE FROM notification_outbox WHERE business_id = 900"))
            _run_alembic(postgres_database_url, "upgrade", "head", check=False)

    async def test_re_upgrade_after_clean_downgrade_preserves_data(
        self, pg_engine: AsyncEngine, postgres_database_url: str
    ) -> None:
        await _seed_business(pg_engine)
        async with pg_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO whatsapp_inbound_events "
                    "(message_id, business_id, phone_number_id, sender_phone, "
                    " message_type, status, attempts, max_attempts, "
                    " provider_timestamp, completed_at) "
                    "VALUES ('wamid.mig-reupgrade', 900, 'phone-900', '919000000001', "
                    " 'text', 'completed', 1, 5, :ts, :ts)"
                ),
                {"ts": NOW},
            )
        try:
            _run_alembic(postgres_database_url, "downgrade", "0013")
            _run_alembic(postgres_database_url, "upgrade", "head")
            async with pg_engine.connect() as conn:
                row = await conn.execute(
                    text(
                        "SELECT status, phone_number_id FROM whatsapp_inbound_events "
                        "WHERE message_id = 'wamid.mig-reupgrade'"
                    )
                )
                result = row.first()
                assert result is not None
                assert result[0] == "completed"
        finally:
            async with pg_engine.begin() as conn:
                await conn.execute(
                    text(
                        "DELETE FROM whatsapp_inbound_events "
                        "WHERE message_id = 'wamid.mig-reupgrade'"
                    )
                )
            _run_alembic(postgres_database_url, "upgrade", "head", check=False)
