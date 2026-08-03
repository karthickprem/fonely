"""PostgreSQL integration tests for owner command system."""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.services.model_gateway import ModelResponse
from fonely.services.owner_commands import OwnerCommandService, get_daily_context

pytestmark = pytest.mark.postgres


def _mock_gateway(response_json: dict) -> AsyncMock:
    gateway = AsyncMock()
    gateway.complete.return_value = ModelResponse(text=json.dumps(response_json))
    return gateway


async def _seed_clinic(session: AsyncSession) -> None:
    await session.execute(
        text(
            "INSERT INTO businesses "
            "(id, name, category, primary_contact_phone, timezone, subscription) "
            "VALUES (1, 'Smile Dental', 'clinic', '+914428350001', "
            "'Asia/Kolkata', 'trial')"
        )
    )
    await session.execute(
        text(
            "INSERT INTO business_users (business_id, phone, role, is_active) "
            "VALUES (1, '+914428350001', 'owner', true)"
        )
    )
    await session.execute(
        text(
            "INSERT INTO resources (id, business_id, name, resource_type, is_active) "
            "VALUES (1, 1, 'Dr. Priya Krishnan', 'staff', true), "
            "(2, 1, 'Dr. Arjun Venkatesh', 'staff', true)"
        )
    )
    await session.execute(
        text(
            "INSERT INTO services (id, business_id, name, duration_minutes, is_active) "
            "VALUES (1, 1, 'General Consultation', 20, true), "
            "(2, 1, 'Root Canal', 60, true)"
        )
    )


async def test_doctor_leave_creates_exception_and_cancels(
    pg_session: AsyncSession,
) -> None:
    await _seed_clinic(pg_session)

    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
    await pg_session.execute(
        text(
            "INSERT INTO pending_actions "
            "(id, business_id, action_type, payload_schema_version, proposed_payload, "
            "status, expires_at, idempotency_key, version, payload_digest) VALUES "
            "(1, 1, 'appointment', 1, :payload, 'confirmed', :exp, 'pa-1', 3, "
            "'aaaa1111bbbb2222cccc3333dddd4444eeee5555ffff6666aaaa7777bbbb8888')"
        ),
        {
            "payload": "{}",
            "exp": datetime.now(UTC) + timedelta(hours=24),
        },
    )
    tomorrow_10am = datetime.combine(
        tomorrow, datetime.min.time().replace(hour=4, minute=30), tzinfo=UTC
    )
    await pg_session.execute(
        text(
            "INSERT INTO appointments "
            "(id, business_id, resource_id, service_id, customer_name, customer_phone, "
            "start_at, end_at, effective_start_at, effective_end_at, "
            "service_name_snapshot, resource_name_snapshot, "
            "duration_minutes_snapshot, buffer_before_minutes_snapshot, "
            "buffer_after_minutes_snapshot, business_timezone_snapshot, "
            "status, source, idempotency_key, pending_action_id, version) VALUES "
            "(1, 1, 1, 1, 'Karthick', '+919123456789', :start, :end, :start, :end, "
            "'Consultation', 'Dr. Priya', 20, 0, 0, 'Asia/Kolkata', "
            "'confirmed', 'customer_conversation', 'pa-1', 1, 1)"
        ),
        {"start": tomorrow_10am, "end": tomorrow_10am + timedelta(minutes=20)},
    )

    gateway = _mock_gateway(
        {"command": "doctor_leave", "doctor_name": "Dr. Priya", "date": "tomorrow"}
    )
    service = OwnerCommandService(pg_session, gateway)
    result = await service.process_command(1, "+914428350001", "Dr. Priya leave tomorrow")

    assert result.command_type == "doctor_leave"
    assert result.success is True
    assert result.affected_appointments == 1
    assert "Karthick" in result.response_text

    exc_count = await pg_session.scalar(
        text("SELECT count(*) FROM schedule_exceptions WHERE resource_id = 1")
    )
    assert exc_count == 1

    appt_status = await pg_session.scalar(text("SELECT status FROM appointments WHERE id = 1"))
    assert appt_status == "cancelled"


async def test_get_summary_returns_appointment_list(
    pg_session: AsyncSession,
) -> None:
    await _seed_clinic(pg_session)

    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
    tomorrow_10am = datetime.combine(
        tomorrow, datetime.min.time().replace(hour=4, minute=30), tzinfo=UTC
    )

    await pg_session.execute(
        text(
            "INSERT INTO pending_actions "
            "(id, business_id, action_type, payload_schema_version, proposed_payload, "
            "status, expires_at, idempotency_key, version, payload_digest) VALUES "
            "(1, 1, 'appointment', 1, :payload, 'confirmed', :exp, 'pa-sum', 3, "
            "'bbbb1111cccc2222dddd3333eeee4444ffff5555aaaa6666bbbb7777cccc8888')"
        ),
        {
            "payload": "{}",
            "exp": datetime.now(UTC) + timedelta(hours=24),
        },
    )
    await pg_session.execute(
        text(
            "INSERT INTO appointments "
            "(id, business_id, resource_id, service_id, customer_name, customer_phone, "
            "start_at, end_at, effective_start_at, effective_end_at, "
            "service_name_snapshot, resource_name_snapshot, "
            "duration_minutes_snapshot, buffer_before_minutes_snapshot, "
            "buffer_after_minutes_snapshot, business_timezone_snapshot, "
            "status, source, idempotency_key, pending_action_id, version) VALUES "
            "(1, 1, 1, 1, 'Karthick', '+919123456789', :start, :end, :start, :end, "
            "'Consultation', 'Dr. Priya', 20, 0, 0, 'Asia/Kolkata', "
            "'confirmed', 'customer_conversation', 'pa-sum', 1, 1)"
        ),
        {"start": tomorrow_10am, "end": tomorrow_10am + timedelta(minutes=20)},
    )

    gateway = _mock_gateway({"command": "get_summary", "date": "tomorrow"})
    service = OwnerCommandService(pg_session, gateway)
    result = await service.process_command(1, "+914428350001", "show tomorrow appointments")

    assert result.command_type == "get_summary"
    assert result.success is True
    assert "Karthick" in result.response_text
    assert "Consultation" in result.response_text
    assert "1 appointment" in result.response_text


async def test_add_offer_creates_daily_context(pg_session: AsyncSession) -> None:
    await _seed_clinic(pg_session)

    gateway = _mock_gateway({"command": "add_offer", "description": "Free consultation this week"})
    service = OwnerCommandService(pg_session, gateway)
    result = await service.process_command(1, "+914428350001", "this week consultation free")

    assert result.command_type == "add_offer"
    assert result.success is True
    assert "Free consultation" in result.response_text

    from datetime import date

    today = date.today()
    contexts = await get_daily_context(1, today, pg_session)
    assert len(contexts) == 1
    assert contexts[0].content == "Free consultation this week"
    assert contexts[0].context_type == "offer"


async def test_unknown_command_returns_help(pg_session: AsyncSession) -> None:
    await _seed_clinic(pg_session)

    gateway = _mock_gateway({"command": "unknown"})
    service = OwnerCommandService(pg_session, gateway)
    result = await service.process_command(1, "+914428350001", "asdfghjkl")

    assert result.command_type == "unknown"
    assert result.success is False
    assert "Dr. Priya leave" in result.response_text
