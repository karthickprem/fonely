"""Focused PostgreSQL proof for the owner-command message facade.

Proves a bare YES/NO is bound to trusted business + owner phone, uses a
locked single-use proposal, rejects expiry, and fails closed on ambiguity.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fonely.services.owner_commands import OwnerCommandService
from fonely.workers.inbound_worker import ClaimedEvent, _process_domain

pytestmark = pytest.mark.postgres


async def _seed_owner(session: AsyncSession, business_id: int = 1) -> str:
    phone = f"+91900000000{business_id}"
    await session.execute(
        text(
            "INSERT INTO businesses "
            "(id, name, category, primary_contact_phone, timezone, subscription) "
            "VALUES (:id, :name, 'clinic', :phone, 'Asia/Kolkata', 'trial')"
        ),
        {"id": business_id, "name": f"Clinic {business_id}", "phone": phone},
    )
    await session.execute(
        text(
            "INSERT INTO business_users "
            "(id, business_id, phone, role, is_active) "
            "VALUES (:id, :id, :phone, 'owner', true)"
        ),
        {"id": business_id, "phone": phone},
    )
    await session.execute(
        text(
            "INSERT INTO operating_schedules "
            "(business_id, day_of_week, open_time, close_time, is_active) "
            "SELECT :id, day, '09:00', '18:00', true "
            "FROM generate_series(0, 6) AS day"
        ),
        {"id": business_id},
    )
    await session.flush()
    return phone


async def _preview(
    session: AsyncSession, business_id: int = 1, phone: str = "+919000000001"
) -> str:
    result = await OwnerCommandService(session).process_command(
        business_id, phone, "close tomorrow"
    )
    assert result.success is True
    assert result.proposal_id is not None
    return result.proposal_id


async def test_two_concurrent_yes_exactly_one_completes(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as setup:
        phone = await _seed_owner(setup)
        proposal_id = await _preview(setup, phone=phone)
        await setup.commit()

    start = asyncio.Event()

    async def confirm() -> tuple[bool, str]:
        async with pg_session_factory() as session:
            await start.wait()
            result = await OwnerCommandService(session).process_command(1, phone, "YES")
            await session.commit()
            return result.success, result.response_text

    first = asyncio.create_task(confirm())
    second = asyncio.create_task(confirm())
    start.set()
    results = await asyncio.gather(first, second)

    assert sum(1 for success, _ in results if success) == 1
    assert sum(1 for success, _ in results if not success) == 1

    async with pg_session_factory() as verify:
        row = (
            await verify.execute(
                text("SELECT status, expected_version FROM owner_command_proposals WHERE id = :id"),
                {"id": proposal_id},
            )
        ).one()
        assert row[0] == "completed"
        assert row[1] == 2
        assert (
            await verify.scalar(
                text("SELECT count(*) FROM schedule_exceptions WHERE business_id = 1")
            )
            == 1
        )


async def test_expired_yes_rejects_and_cannot_later_confirm(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as setup:
        phone = await _seed_owner(setup)
        proposal_id = await _preview(setup, phone=phone)
        await setup.execute(
            text("UPDATE owner_command_proposals SET expires_at = :past WHERE id = :id"),
            {
                "past": datetime.now(UTC) - timedelta(seconds=1),
                "id": proposal_id,
            },
        )
        await setup.commit()

    async with pg_session_factory() as session:
        first = await OwnerCommandService(session).process_command(1, phone, "YES")
        assert first.success is False
        assert "expired" in first.response_text.lower() or "pending" in first.response_text.lower()
        await session.commit()

    async with pg_session_factory() as session:
        second = await OwnerCommandService(session).process_command(1, phone, "YES")
        assert second.success is False
        await session.commit()

    async with pg_session_factory() as verify:
        assert (
            await verify.scalar(
                text("SELECT status FROM owner_command_proposals WHERE id = :id"),
                {"id": proposal_id},
            )
            == "expired"
        )
        assert await verify.scalar(text("SELECT count(*) FROM schedule_exceptions")) == 0


async def test_wrong_business_or_phone_cannot_consume_proposal(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as setup:
        phone1 = await _seed_owner(setup, 1)
        phone2 = await _seed_owner(setup, 2)
        proposal_id = await _preview(setup, 1, phone1)
        await setup.commit()

    async with pg_session_factory() as session:
        wrong_business = await OwnerCommandService(session).process_command(2, phone2, "YES")
        assert wrong_business.success is False
        wrong_phone = await OwnerCommandService(session).process_command(1, "+919999999999", "YES")
        assert wrong_phone.success is False
        await session.commit()

    async with pg_session_factory() as verify:
        assert (
            await verify.scalar(
                text("SELECT status FROM owner_command_proposals WHERE id = :id"),
                {"id": proposal_id},
            )
            == "pending_confirmation"
        )


async def test_ambiguous_pending_set_does_not_guess(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Simulate pre-index/corrupt duplicate rows and prove the facade asks.

    PostgreSQL's partial unique index normally prevents this. The test removes and
    restores it transactionally so the ambiguity guard itself is executable proof.
    """
    async with pg_session_factory() as setup:
        phone = await _seed_owner(setup)
        first_id = await _preview(setup, phone=phone)
        await setup.execute(text("DROP INDEX ix_owner_proposal_pending"))
        await setup.execute(
            text(
                "INSERT INTO owner_command_proposals "
                "(id, business_id, owner_user_id, command_type, command_payload, "
                " status, idempotency_key, expected_version, expires_at) "
                "VALUES ('ambiguous-2', 1, 1, 'close_day', "
                ' \'{"command_type":"close_day","target_date":"tomorrow"}\'::jsonb, '
                " 'pending_confirmation', 'ambiguous-2-key', 1, :expires_at)"
            ),
            {"expires_at": datetime.now(UTC) + timedelta(minutes=10)},
        )

        result = await OwnerCommandService(setup).process_command(1, phone, "YES")
        assert result.success is False
        assert "more than one" in result.response_text.lower()

        rows = (await setup.execute(text("SELECT id, status FROM owner_command_proposals"))).all()
        assert len(rows) == 2
        assert set(rows) == {
            ("ambiguous-2", "pending_confirmation"),
            (first_id, "pending_confirmation"),
        }
        await setup.rollback()


async def test_inbound_worker_owner_journey_new_no_new_yes(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as setup:
        phone = await _seed_owner(setup)
        await setup.commit()

    def claimed(event_id: int, body: str) -> ClaimedEvent:
        return ClaimedEvent(
            event_id=event_id,
            business_id=1,
            message_id=f"wamid.owner.{event_id}",
            sender_phone=phone,
            message_type="text",
            message_body=body,
            phone_number_id="phone-1",
            claim_token=uuid.uuid4(),
            claim_version=1,
            attempts=0,
            max_attempts=5,
        )

    async with pg_session_factory() as session:
        response, recipient = await _process_domain(
            claimed(1, "close tomorrow"), session, AsyncMock()
        )
        assert recipient == "owner"
        assert "yes" in response.lower()
        await session.commit()

    async with pg_session_factory() as verify:
        first = (
            await verify.execute(
                text("SELECT id, status FROM owner_command_proposals ORDER BY created_at, id")
            )
        ).one()
        assert first[1] == "pending_confirmation"

    async with pg_session_factory() as session:
        response, recipient = await _process_domain(claimed(2, "NO"), session, AsyncMock())
        assert recipient == "owner"
        assert "cancel" in response.lower()
        await session.commit()

    async with pg_session_factory() as verify:
        assert (
            await verify.scalar(
                text("SELECT status FROM owner_command_proposals WHERE id = :id"),
                {"id": first[0]},
            )
            == "rejected"
        )

    async with pg_session_factory() as session:
        response, recipient = await _process_domain(
            claimed(3, "close tomorrow"), session, AsyncMock()
        )
        assert recipient == "owner"
        assert "yes" in response.lower()
        await session.commit()

    async with pg_session_factory() as session:
        response, recipient = await _process_domain(claimed(4, "YES"), session, AsyncMock())
        assert recipient == "owner"
        await session.commit()

    async with pg_session_factory() as verify:
        rows = (
            (
                await verify.execute(
                    text("SELECT status FROM owner_command_proposals ORDER BY created_at, id")
                )
            )
            .scalars()
            .all()
        )
        assert rows == ["rejected", "completed"]
        assert (
            await verify.scalar(
                text("SELECT count(*) FROM schedule_exceptions WHERE business_id = 1")
            )
            == 1
        )
