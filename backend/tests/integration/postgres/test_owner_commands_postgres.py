"""PostgreSQL integration tests for owner command system."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fonely.services.model_gateway import ModelResponse
from fonely.services.owner_commands import OwnerCommandService, get_daily_context

pytestmark = pytest.mark.postgres


@pytest.fixture(autouse=True)
def _whatsapp_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    from fonely.services import notifications, whatsapp_config

    mapping = '{"phone-1": 1}'
    monkeypatch.setattr(whatsapp_config.settings, "whatsapp_business_mappings", mapping)
    monkeypatch.setattr(notifications.settings, "whatsapp_business_mappings", mapping)
    monkeypatch.setattr(notifications.settings, "whatsapp_phone_number_id", "phone-1")


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

    gateway = _mock_gateway(
        {"command": "doctor_leave", "doctor_name": "Dr. Priya", "date": "tomorrow"}
    )
    service = OwnerCommandService(pg_session, gateway)
    preview = await service.process_command(1, "+914428350001", "Dr. Priya leave tomorrow")

    assert preview.command_type == "doctor_leave"
    assert preview.success is True
    assert preview.affected_appointments == 1
    assert "Karthick" in preview.response_text
    assert preview.proposal_id is not None
    assert await pg_session.scalar(text("SELECT count(*) FROM schedule_exceptions")) == 0
    assert (
        await pg_session.scalar(text("SELECT status FROM appointments WHERE id = 1")) == "confirmed"
    )

    result = await service.process_command(1, "+914428350001", "YES")
    assert result.success is True
    assert result.affected_appointments == 1
    assert "Notifications queued" in result.response_text

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


async def _seed_owner_target(
    session: AsyncSession,
) -> datetime:
    from zoneinfo import ZoneInfo

    tomorrow = datetime.now(ZoneInfo("Asia/Kolkata")).date() + timedelta(days=1)
    start = datetime.combine(tomorrow, datetime.min.time().replace(hour=4, minute=30), tzinfo=UTC)
    await session.execute(
        text(
            "INSERT INTO pending_actions "
            "(id, business_id, action_type, payload_schema_version, proposed_payload, "
            "status, expires_at, idempotency_key, version, payload_digest) VALUES "
            "(1000, 1, 'appointment', 1, '{}', 'confirmed', :exp, 'owner-target', 3, "
            "'aaaa1111bbbb2222cccc3333dddd4444eeee5555ffff6666aaaa7777bbbb8888')"
        ),
        {"exp": datetime.now(UTC) + timedelta(hours=24)},
    )
    await session.execute(
        text(
            "INSERT INTO appointments "
            "(id, business_id, resource_id, service_id, customer_name, customer_phone, "
            "start_at, end_at, effective_start_at, effective_end_at, "
            "service_name_snapshot, resource_name_snapshot, duration_minutes_snapshot, "
            "buffer_before_minutes_snapshot, buffer_after_minutes_snapshot, "
            "business_timezone_snapshot, status, source, idempotency_key, "
            "pending_action_id, version) VALUES "
            "(1000, 1, 1, 1, 'Patient', '+919123456789', :start, :end, :start, :end, "
            "'Consultation', 'Dr. Priya Krishnan', 20, 0, 0, 'Asia/Kolkata', "
            "'confirmed', 'customer_conversation', 'owner-target', 1000, 1)"
        ),
        {"start": start, "end": start + timedelta(minutes=20)},
    )
    return start


async def test_owner_preview_persists_exact_targets_without_mutation(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_clinic(session)
        await _seed_owner_target(session)
        gateway = _mock_gateway(
            {"command": "doctor_leave", "doctor_name": "Dr. Priya", "date": "tomorrow"}
        )
        result = await OwnerCommandService(session, gateway).process_command(
            1, "+914428350001", "Dr. Priya leave tomorrow"
        )
        await session.commit()

    assert result.proposal_id is not None
    async with pg_session_factory() as observer:
        proposal = (
            await observer.execute(
                text(
                    "SELECT status, preview_snapshot, payload_digest FROM "
                    "owner_command_proposals WHERE id = :id"
                ),
                {"id": result.proposal_id},
            )
        ).one()
        assert proposal[0] == "pending_confirmation"
        assert proposal[1]["appointments"][0]["appointment_id"] == 1000
        assert len(proposal[2]) == 64
        assert await observer.scalar(text("SELECT count(*) FROM schedule_exceptions")) == 0
        assert (
            await observer.scalar(text("SELECT status FROM appointments WHERE id = 1000"))
            == "confirmed"
        )


async def test_owner_target_drift_fails_closed(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_clinic(session)
        await _seed_owner_target(session)
        gateway = _mock_gateway(
            {"command": "doctor_leave", "doctor_name": "Dr. Priya", "date": "tomorrow"}
        )
        service = OwnerCommandService(session, gateway)
        preview = await service.process_command(1, "+914428350001", "Dr. Priya leave tomorrow")
        await session.commit()

    async with pg_session_factory() as session:
        await session.execute(text("UPDATE appointments SET version = version + 1 WHERE id = 1000"))
        await session.commit()

    async with pg_session_factory() as session:
        result = await OwnerCommandService(session, AsyncMock()).process_command(
            1, "+914428350001", "YES"
        )
        await session.commit()
    assert result.success is False

    async with pg_session_factory() as observer:
        assert (
            await observer.scalar(text("SELECT status FROM appointments WHERE id = 1000"))
            == "confirmed"
        )
        assert await observer.scalar(text("SELECT count(*) FROM schedule_exceptions")) == 0
        assert (
            await observer.scalar(
                text("SELECT status FROM owner_command_proposals WHERE id = :id"),
                {"id": preview.proposal_id},
            )
            == "failed"
        )


async def test_owner_lost_response_replays_exact_completed_evidence(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_clinic(session)
        await _seed_owner_target(session)
        gateway = _mock_gateway(
            {"command": "doctor_leave", "doctor_name": "Dr. Priya", "date": "tomorrow"}
        )
        service = OwnerCommandService(session, gateway)
        await service.process_command(1, "+914428350001", "Dr. Priya leave tomorrow")
        await session.commit()

    async with pg_session_factory() as session:
        first = await OwnerCommandService(session, AsyncMock()).process_command(
            1, "+914428350001", "YES"
        )
        await session.commit()

    async with pg_session_factory() as session:
        gateway = _mock_gateway(
            {"command": "doctor_leave", "doctor_name": "Dr. Priya", "date": "tomorrow"}
        )
        replay = await OwnerCommandService(session, gateway).process_command(
            1, "+914428350001", "Dr. Priya leave tomorrow"
        )
        await session.commit()

    assert replay == first
    async with pg_session_factory() as observer:
        assert await observer.scalar(text("SELECT count(*) FROM appointment_commits")) == 1
        assert await observer.scalar(text("SELECT count(*) FROM owner_audit_log")) == 1
        assert (
            await observer.scalar(
                text(
                    "SELECT count(*) FROM notification_outbox "
                    "WHERE event_type = 'appointment_cancelled'"
                )
            )
            == 2
        )
        evidence = await observer.scalar(
            text("SELECT result_evidence FROM owner_command_proposals WHERE status='completed'")
        )
        assert evidence["schema_version"] == 1
        assert evidence["status"] == "completed"
        assert evidence["appointment_ids"] == [1000]
        assert evidence["queued_outbox_count"] == 2


async def test_owner_wrong_tenant_cannot_confirm_proposal(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_clinic(session)
        await _seed_owner_target(session)
        gateway = _mock_gateway(
            {"command": "doctor_leave", "doctor_name": "Dr. Priya", "date": "tomorrow"}
        )
        await OwnerCommandService(session, gateway).process_command(
            1, "+914428350001", "Dr. Priya leave tomorrow"
        )
        await session.execute(
            text(
                "INSERT INTO businesses (id, name, category, primary_contact_phone, "
                "timezone, subscription) VALUES "
                "(2, 'Other', 'clinic', '+914428350002', 'Asia/Kolkata', 'trial')"
            )
        )
        await session.execute(
            text(
                "INSERT INTO business_users (business_id, phone, role, is_active) "
                "VALUES (2, '+914428350002', 'owner', true)"
            )
        )
        await session.commit()

    async with pg_session_factory() as session:
        result = await OwnerCommandService(session, AsyncMock()).process_command(
            2, "+914428350002", "YES"
        )
        await session.rollback()
    assert result.success is False
    assert result.command_type == "unknown"


async def test_concurrent_owner_confirmations_execute_once(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_clinic(session)
        await _seed_owner_target(session)
        gateway = _mock_gateway(
            {"command": "doctor_leave", "doctor_name": "Dr. Priya", "date": "tomorrow"}
        )
        await OwnerCommandService(session, gateway).process_command(
            1, "+914428350001", "Dr. Priya leave tomorrow"
        )
        await session.commit()

    async def confirm() -> object:
        async with pg_session_factory() as session:
            result = await OwnerCommandService(session, AsyncMock()).process_command(
                1, "+914428350001", "YES"
            )
            await session.commit()
            return result

    first, second = await asyncio.gather(confirm(), confirm())
    assert first == second
    async with pg_session_factory() as observer:
        assert await observer.scalar(text("SELECT count(*) FROM appointment_commits")) == 1
        assert await observer.scalar(text("SELECT count(*) FROM owner_audit_log")) == 1
        assert (
            await observer.scalar(
                text(
                    "SELECT count(*) FROM notification_outbox "
                    "WHERE event_type = 'appointment_cancelled'"
                )
            )
            == 2
        )


async def _insert_second_owner_target(session: AsyncSession, start: datetime) -> None:
    await session.execute(
        text(
            "INSERT INTO pending_actions "
            "(id, business_id, action_type, payload_schema_version, proposed_payload, "
            "status, expires_at, idempotency_key, version, payload_digest) VALUES "
            "(1001, 1, 'appointment', 1, '{}', 'confirmed', :exp, 'owner-target-2', 3, "
            "'bbbb1111cccc2222dddd3333eeee4444ffff5555aaaa6666bbbb7777cccc8888')"
        ),
        {"exp": datetime.now(UTC) + timedelta(hours=24)},
    )
    await session.execute(
        text(
            "INSERT INTO appointments "
            "(id, business_id, resource_id, service_id, customer_name, customer_phone, "
            "start_at, end_at, effective_start_at, effective_end_at, "
            "service_name_snapshot, resource_name_snapshot, duration_minutes_snapshot, "
            "buffer_before_minutes_snapshot, buffer_after_minutes_snapshot, "
            "business_timezone_snapshot, status, source, idempotency_key, "
            "pending_action_id, version) VALUES "
            "(1001, 1, 1, 1, 'Later Patient', '+919123456789', :start, :end, :start, :end, "
            "'Consultation', 'Dr. Priya Krishnan', 20, 0, 0, 'Asia/Kolkata', "
            "'confirmed', 'customer_conversation', 'owner-target-2', 1001, 1)"
        ),
        {"start": start, "end": start + timedelta(minutes=20)},
    )


async def test_booking_between_preview_and_yes_is_included_under_lock(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_clinic(session)
        start = await _seed_owner_target(session)
        gateway = _mock_gateway(
            {"command": "close_clinic", "date": "tomorrow", "reason": "Emergency"}
        )
        preview = await OwnerCommandService(session, gateway).process_command(
            1, "+914428350001", "close clinic tomorrow"
        )
        await session.commit()

    async with pg_session_factory() as session:
        await _insert_second_owner_target(session, start + timedelta(hours=1))
        await session.commit()

    async with pg_session_factory() as session:
        result = await OwnerCommandService(session, AsyncMock()).process_command(
            1, "+914428350001", "YES"
        )
        await session.commit()

    assert result.success is True
    assert result.affected_appointments == 2
    assert result.affected_patients == 1
    assert result.proposal_id == preview.proposal_id
    async with pg_session_factory() as observer:
        statuses = (
            await observer.execute(
                text("SELECT id, status FROM appointments WHERE id IN (1000,1001) ORDER BY id")
            )
        ).all()
        assert statuses == [(1000, "cancelled"), (1001, "cancelled")]
        assert (
            await observer.scalar(
                text("SELECT count(*) FROM appointment_commits WHERE appointment_id IN (1000,1001)")
            )
            == 2
        )
        assert (
            await observer.scalar(
                text(
                    "SELECT count(*) FROM notification_outbox "
                    "WHERE event_type='appointment_cancelled'"
                )
            )
            == 4
        )


async def test_concurrent_duplicate_proposal_creation_has_one_pending(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_clinic(session)
        await _seed_owner_target(session)
        await session.commit()

    async def preview() -> object:
        async with pg_session_factory() as session:
            gateway = _mock_gateway(
                {"command": "doctor_leave", "doctor_name": "Dr. Priya", "date": "tomorrow"}
            )
            result = await OwnerCommandService(session, gateway).process_command(
                1, "+914428350001", "Dr. Priya leave tomorrow"
            )
            await session.commit()
            return result

    first, second = await asyncio.gather(preview(), preview())
    assert first.proposal_id == second.proposal_id
    async with pg_session_factory() as observer:
        assert (
            await observer.scalar(
                text(
                    "SELECT count(*) FROM owner_command_proposals "
                    "WHERE status='pending_confirmation'"
                )
            )
            == 1
        )


async def test_bare_yes_does_not_skip_newer_terminal_intent(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_clinic(session)
        await _seed_owner_target(session)
        gateway = _mock_gateway(
            {"command": "doctor_leave", "doctor_name": "Dr. Priya", "date": "tomorrow"}
        )
        service = OwnerCommandService(session, gateway)
        await service.process_command(1, "+914428350001", "Dr. Priya leave tomorrow")
        await session.execute(
            text(
                "UPDATE owner_command_proposals SET status='rejected', "
                "expected_version=expected_version+1"
            )
        )
        await session.commit()

    async with pg_session_factory() as session:
        result = await OwnerCommandService(session, AsyncMock()).process_command(
            1, "+914428350001", "YES"
        )
        await session.rollback()

    assert result.success is False
    assert "no current command" in result.response_text.lower()
    async with pg_session_factory() as observer:
        assert (
            await observer.scalar(text("SELECT status FROM appointments WHERE id=1000"))
            == "confirmed"
        )


async def test_revised_close_early_intent_never_reuses_old_preview(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from zoneinfo import ZoneInfo

    target_date = (datetime.now(ZoneInfo("Asia/Kolkata")).date() + timedelta(days=1)).isoformat()
    async with pg_session_factory() as session:
        await _seed_clinic(session)
        first_gateway = _mock_gateway(
            {
                "command": "close_early",
                "date": target_date,
                "close_time": "17:00",
                "reason": "Original",
            }
        )
        first = await OwnerCommandService(session, first_gateway).process_command(
            1, "+914428350001", "close at 5"
        )
        first_row = (
            await session.execute(
                text(
                    "SELECT payload_digest,preview_snapshot FROM owner_command_proposals "
                    "WHERE id=:id"
                ),
                {"id": first.proposal_id},
            )
        ).one()
        revised_gateway = _mock_gateway(
            {
                "command": "close_early",
                "date": target_date,
                "close_time": "15:00",
                "reason": "Emergency",
            }
        )
        blocked = await OwnerCommandService(session, revised_gateway).process_command(
            1, "+914428350001", "close at 3"
        )
        assert blocked.proposal_id == first.proposal_id
        assert "15:00" not in blocked.response_text
        assert await session.scalar(text("SELECT count(*) FROM owner_command_proposals")) == 1
        persisted = (
            await session.execute(
                text(
                    "SELECT payload_digest,preview_snapshot FROM owner_command_proposals "
                    "WHERE id=:id"
                ),
                {"id": first.proposal_id},
            )
        ).one()
        assert persisted == first_row
        await session.rollback()


async def test_newer_rejected_intent_blocks_bare_yes_old_completed_replay(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from zoneinfo import ZoneInfo

    day1 = (datetime.now(ZoneInfo("Asia/Kolkata")).date() + timedelta(days=1)).isoformat()
    day2 = (datetime.now(ZoneInfo("Asia/Kolkata")).date() + timedelta(days=2)).isoformat()
    async with pg_session_factory() as session:
        await _seed_clinic(session)
        first_gateway = _mock_gateway({"command": "close_clinic", "date": day1})
        service = OwnerCommandService(session, first_gateway)
        await service.process_command(1, "+914428350001", "close day one")
        await service.process_command(1, "+914428350001", "YES")
        second_gateway = _mock_gateway({"command": "close_clinic", "date": day2})
        second_service = OwnerCommandService(session, second_gateway)
        second = await second_service.process_command(1, "+914428350001", "close day two")
        await second_service.process_command(1, "+914428350001", "NO")
        await session.commit()

    async with pg_session_factory() as session:
        result = await OwnerCommandService(session, AsyncMock()).process_command(
            1, "+914428350001", "YES"
        )
        await session.rollback()
    assert result.success is False
    assert "no current command" in result.response_text.lower()
    async with pg_session_factory() as observer:
        assert (
            await observer.scalar(
                text("SELECT status FROM owner_command_proposals WHERE id=:id"),
                {"id": second.proposal_id},
            )
            == "rejected"
        )
        assert await observer.scalar(text("SELECT count(*) FROM schedule_exceptions")) == 1


async def test_schedule_exception_drift_marks_proposal_failed(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from zoneinfo import ZoneInfo

    target_day = datetime.now(ZoneInfo("Asia/Kolkata")).date() + timedelta(days=1)
    async with pg_session_factory() as session:
        await _seed_clinic(session)
        gateway = _mock_gateway(
            {"command": "close_clinic", "date": target_day.isoformat(), "reason": "Owner"}
        )
        preview = await OwnerCommandService(session, gateway).process_command(
            1, "+914428350001", "close clinic"
        )
        await session.commit()

    async with pg_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO schedule_exceptions "
                "(business_id,resource_id,exception_date,is_closed,reason) "
                "VALUES (1,NULL,:day,true,'Different reason')"
            ),
            {"day": target_day},
        )
        await session.commit()

    async with pg_session_factory() as session:
        result = await OwnerCommandService(session, AsyncMock()).process_command(
            1, "+914428350001", "YES"
        )
        await session.commit()
    assert result.success is False

    async with pg_session_factory() as observer:
        assert (
            await observer.scalar(
                text("SELECT status FROM owner_command_proposals WHERE id=:id"),
                {"id": preview.proposal_id},
            )
            == "failed"
        )
        assert await observer.scalar(text("SELECT count(*) FROM schedule_exceptions")) == 1
