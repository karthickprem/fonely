"""PostgreSQL integration tests for owner command system."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from fonely.services.owner_commands import OwnerCommandService, get_daily_context

pytestmark = pytest.mark.postgres


@pytest.fixture(autouse=True)
def _whatsapp_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    from fonely.services import notifications, whatsapp_config

    mappings = '{"phone-1": 1}'
    monkeypatch.setattr(whatsapp_config.settings, "whatsapp_business_mappings", mappings)
    monkeypatch.setattr(notifications.settings, "whatsapp_business_mappings", mappings)
    monkeypatch.setattr(notifications.settings, "whatsapp_phone_number_id", "phone-1")


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
    await session.execute(
        text(
            "INSERT INTO service_resource_eligibility "
            "(business_id, service_id, resource_id, is_active) VALUES (1, 1, 1, true)"
        )
    )
    await session.execute(
        text(
            "INSERT INTO operating_schedules "
            "(business_id, day_of_week, open_time, close_time, is_active) "
            "SELECT 1, day, '09:00', '18:00', true FROM generate_series(0, 6) AS day"
        )
    )


async def test_doctor_leave_creates_exception_and_cancels(
    pg_session: AsyncSession,
) -> None:
    await _seed_clinic(pg_session)

    from zoneinfo import ZoneInfo

    tomorrow = datetime.now(ZoneInfo("Asia/Kolkata")).date() + timedelta(days=1)
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
    await pg_session.execute(
        text("SELECT setval(pg_get_serial_sequence('pending_actions', 'id'), 1, true)")
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

    service = OwnerCommandService(pg_session)
    preview = await service.process_command(1, "+914428350001", "Dr. Priya leave tomorrow")
    assert preview.success is True
    assert preview.proposal_id is not None

    confirm = await service.process_command(1, "+914428350001", "YES")
    assert confirm.success is True
    assert confirm.affected_appointments == 1
    assert "Karthick" in confirm.response_text

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

    from zoneinfo import ZoneInfo

    tomorrow = datetime.now(ZoneInfo("Asia/Kolkata")).date() + timedelta(days=1)
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

    service = OwnerCommandService(pg_session)
    result = await service.process_command(1, "+914428350001", "show tomorrow appointments")

    assert result.command_type == "get_summary"
    assert result.success is True
    assert "Karthick" in result.response_text
    assert "Consultation" in result.response_text
    assert "1 appointment" in result.response_text


async def test_add_offer_creates_daily_context(pg_session: AsyncSession) -> None:
    await _seed_clinic(pg_session)

    service = OwnerCommandService(pg_session)
    result = await service.process_command(1, "+914428350001", "this week consultation free")

    assert result.command_type == "add_offer"
    assert result.success is True

    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    contexts = await get_daily_context(1, today, pg_session)
    assert len(contexts) == 1
    assert contexts[0].context_type == "offer"


async def test_unknown_command_returns_help(pg_session: AsyncSession) -> None:
    await _seed_clinic(pg_session)

    service = OwnerCommandService(pg_session)
    result = await service.process_command(1, "+914428350001", "asdfghjkl")

    assert result.command_type == "unknown"
    assert result.success is False
    assert "Dr. Priya leave" in result.response_text
