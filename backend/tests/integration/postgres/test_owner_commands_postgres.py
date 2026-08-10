"""PostgreSQL integration tests for owner command system."""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fonely.repositories.owner_command_proposals import OwnerCommandProposalRepository
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


# ---------------------------------------------------------------------------
# Durable proposal lifecycle tests
# ---------------------------------------------------------------------------


async def _seed_clinic_with_appointment(session: AsyncSession) -> None:
    """Seed clinic data with one confirmed appointment for Dr. Priya tomorrow."""
    await _seed_clinic(session)

    from zoneinfo import ZoneInfo

    tomorrow = datetime.now(ZoneInfo("Asia/Kolkata")).date() + timedelta(days=1)
    await session.execute(
        text(
            "INSERT INTO pending_actions "
            "(id, business_id, action_type, payload_schema_version, proposed_payload, "
            "status, expires_at, idempotency_key, version, payload_digest) VALUES "
            "(1, 1, 'appointment', 1, :payload, 'confirmed', :exp, 'pa-prop-1', 3, "
            "'cccc1111dddd2222eeee3333ffff4444aaaa5555bbbb6666cccc7777dddd8888')"
        ),
        {
            "payload": "{}",
            "exp": datetime.now(UTC) + timedelta(hours=24),
        },
    )
    await session.execute(
        text("SELECT setval(pg_get_serial_sequence('pending_actions', 'id'), 1, true)")
    )
    tomorrow_10am = datetime.combine(
        tomorrow, datetime.min.time().replace(hour=4, minute=30), tzinfo=UTC
    )
    await session.execute(
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
            "'confirmed', 'customer_conversation', 'pa-prop-1', 1, 1)"
        ),
        {"start": tomorrow_10am, "end": tomorrow_10am + timedelta(minutes=20)},
    )


async def test_preview_creates_durable_proposal(
    pg_session: AsyncSession,
) -> None:
    """Process 'Dr. Priya leave tomorrow' and assert a pending proposal row exists."""
    await _seed_clinic_with_appointment(pg_session)

    gateway = _mock_gateway(
        {"command": "doctor_leave", "doctor_name": "Dr. Priya", "date": "tomorrow"}
    )
    service = OwnerCommandService(pg_session, gateway)
    result = await service.process_command(1, "+914428350001", "Dr. Priya leave tomorrow")

    assert result.command_type == "doctor_leave"
    assert result.success is True
    assert result.proposal_id is not None

    # Verify proposal persisted in database
    row = await pg_session.execute(
        text(
            "SELECT id, status, command_type, payload_digest, preview_snapshot "
            "FROM owner_command_proposals WHERE id = :pid"
        ),
        {"pid": result.proposal_id},
    )
    proposal = row.one()
    assert proposal[1] == "pending_confirmation"
    assert proposal[2] == "doctor_leave"
    assert len(proposal[3]) == 64  # SHA-256 hex digest
    preview = proposal[4]
    assert preview["command_type"] == "doctor_leave"
    assert preview["affected_count"] == 1
    assert len(preview["appointments"]) == 1
    assert preview["appointments"][0]["patient"] == "Karthick"


async def test_confirm_yes_executes_and_completes(
    pg_session: AsyncSession,
) -> None:
    """Preview then YES: proposal transitions to completed with evidence."""
    await _seed_clinic_with_appointment(pg_session)

    gateway = _mock_gateway(
        {"command": "doctor_leave", "doctor_name": "Dr. Priya", "date": "tomorrow"}
    )
    service = OwnerCommandService(pg_session, gateway)

    # Phase 1: create preview
    preview_result = await service.process_command(1, "+914428350001", "Dr. Priya leave tomorrow")
    assert preview_result.success is True
    assert preview_result.proposal_id is not None

    # Phase 2: confirm
    confirm_result = await service.process_command(1, "+914428350001", "YES")
    assert confirm_result.success is True
    assert confirm_result.command_type == "doctor_leave"
    assert confirm_result.affected_appointments == 1

    # Verify proposal transitioned to completed with evidence
    row = await pg_session.execute(
        text("SELECT status, result_evidence FROM owner_command_proposals WHERE id = :pid"),
        {"pid": preview_result.proposal_id},
    )
    proposal = row.one()
    assert proposal[0] == "completed"
    evidence = proposal[1]
    assert evidence is not None
    assert evidence["outcome"] in ("completed", "completed_with_drift")
    assert evidence["command_type"] == "doctor_leave"
    assert evidence["cancelled_count"] >= 1

    # Verify appointment was cancelled
    appt_status = await pg_session.scalar(text("SELECT status FROM appointments WHERE id = 1"))
    assert appt_status == "cancelled"

    # Verify schedule exception was created
    exc_count = await pg_session.scalar(
        text("SELECT count(*) FROM schedule_exceptions WHERE resource_id = 1")
    )
    assert exc_count == 1


async def test_confirm_no_rejects_proposal(
    pg_session: AsyncSession,
) -> None:
    """Preview then NO: proposal transitions to rejected, no side effects."""
    await _seed_clinic_with_appointment(pg_session)

    gateway = _mock_gateway(
        {"command": "doctor_leave", "doctor_name": "Dr. Priya", "date": "tomorrow"}
    )
    service = OwnerCommandService(pg_session, gateway)

    # Phase 1: create preview
    preview_result = await service.process_command(1, "+914428350001", "Dr. Priya leave tomorrow")
    assert preview_result.success is True
    proposal_id = preview_result.proposal_id

    # Phase 2: reject
    reject_result = await service.process_command(1, "+914428350001", "NO")
    assert reject_result.success is True
    assert reject_result.command_type == "reject"

    # Verify proposal transitioned to rejected
    row = await pg_session.execute(
        text("SELECT status FROM owner_command_proposals WHERE id = :pid"),
        {"pid": proposal_id},
    )
    assert row.scalar_one() == "rejected"

    # Verify appointment was NOT cancelled
    appt_status = await pg_session.scalar(text("SELECT status FROM appointments WHERE id = 1"))
    assert appt_status == "confirmed"

    # Verify no schedule exception was created
    exc_count = await pg_session.scalar(
        text("SELECT count(*) FROM schedule_exceptions WHERE business_id = 1 AND resource_id = 1")
    )
    assert exc_count == 0


async def test_expired_proposal_rejected(
    pg_session: AsyncSession,
) -> None:
    """Preview, manually expire, then YES: fails with expiry message."""
    await _seed_clinic_with_appointment(pg_session)

    gateway = _mock_gateway(
        {"command": "doctor_leave", "doctor_name": "Dr. Priya", "date": "tomorrow"}
    )
    service = OwnerCommandService(pg_session, gateway)

    # Phase 1: create preview
    preview_result = await service.process_command(1, "+914428350001", "Dr. Priya leave tomorrow")
    assert preview_result.success is True
    proposal_id = preview_result.proposal_id

    # Manually expire the proposal
    await pg_session.execute(
        text("UPDATE owner_command_proposals SET expires_at = :past WHERE id = :pid"),
        {"past": datetime(2020, 1, 1, tzinfo=UTC), "pid": proposal_id},
    )

    # Phase 2: attempt confirmation on expired proposal
    confirm_result = await service.process_command(1, "+914428350001", "YES")
    assert confirm_result.success is False
    assert "expired" in confirm_result.response_text.lower()

    # Verify appointment was NOT cancelled
    appt_status = await pg_session.scalar(text("SELECT status FROM appointments WHERE id = 1"))
    assert appt_status == "confirmed"


async def test_duplicate_pending_proposal_blocked(
    pg_session: AsyncSession,
) -> None:
    """Second destructive command for same owner is blocked by partial unique index."""
    await _seed_clinic(pg_session)

    gateway = _mock_gateway(
        {"command": "doctor_leave", "doctor_name": "Dr. Priya", "date": "tomorrow"}
    )
    service = OwnerCommandService(pg_session, gateway)

    # First command creates a pending proposal
    first_result = await service.process_command(1, "+914428350001", "Dr. Priya leave tomorrow")
    assert first_result.success is True
    assert first_result.proposal_id is not None

    # Second command for same owner should be blocked
    gateway2 = _mock_gateway(
        {"command": "doctor_leave", "doctor_name": "Dr. Arjun", "date": "tomorrow"}
    )
    service2 = OwnerCommandService(pg_session, gateway2)
    second_result = await service2.process_command(1, "+914428350001", "Dr. Arjun leave tomorrow")
    assert second_result.success is False
    assert "pending" in second_result.response_text.lower()


async def test_cross_tenant_proposal_isolation(
    pg_session: AsyncSession,
) -> None:
    """Proposal for business 1 is not accessible from business 2."""
    await _seed_clinic(pg_session)

    # Seed a second business
    await pg_session.execute(
        text(
            "INSERT INTO businesses "
            "(id, name, category, primary_contact_phone, timezone, subscription) "
            "VALUES (2, 'Other Dental', 'clinic', '+914428350002', "
            "'Asia/Kolkata', 'trial')"
        )
    )
    await pg_session.execute(
        text(
            "INSERT INTO business_users (business_id, phone, role, is_active) "
            "VALUES (2, '+914428350002', 'owner', true)"
        )
    )

    gateway = _mock_gateway(
        {"command": "doctor_leave", "doctor_name": "Dr. Priya", "date": "tomorrow"}
    )
    service = OwnerCommandService(pg_session, gateway)

    # Create proposal for business 1
    result = await service.process_command(1, "+914428350001", "Dr. Priya leave tomorrow")
    assert result.success is True
    proposal_id = result.proposal_id
    assert proposal_id is not None

    # Attempt to load proposal via repository scoped to business 2
    repo = OwnerCommandProposalRepository(pg_session)
    loaded = await repo.get_by_id(2, proposal_id)
    assert loaded is None

    # Verify it IS accessible from business 1
    loaded_correct = await repo.get_by_id(1, proposal_id)
    assert loaded_correct is not None
    assert loaded_correct.business_id == 1


# ---------------------------------------------------------------------------
# Retry identity regression tests
# ---------------------------------------------------------------------------


def _setup_whatsapp(monkeypatch: pytest.MonkeyPatch) -> None:
    from fonely.services import notifications, whatsapp_config

    mappings = '{"phone-1": 1}'
    monkeypatch.setattr(whatsapp_config.settings, "whatsapp_business_mappings", mappings)
    monkeypatch.setattr(notifications.settings, "whatsapp_business_mappings", mappings)
    monkeypatch.setattr(notifications.settings, "whatsapp_phone_number_id", "phone-1")


def _leave_gateway() -> AsyncMock:
    return _mock_gateway(
        {"command": "doctor_leave", "doctor_name": "Dr. Priya", "date": "tomorrow"}
    )


async def _fail_preview_yes(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preview + inject cancel failure + YES → failed proposal committed."""
    async with factory() as session:
        _setup_whatsapp(monkeypatch)
        service = OwnerCommandService(session, _leave_gateway())
        preview = await service.process_command(1, "+914428350001", "Dr. Priya leave tomorrow")
        assert preview.success is True

        async def _fail(*a: object, **kw: object) -> None:
            raise RuntimeError("injected_fail")

        service._cancel_via_service = _fail  # type: ignore[assignment]
        confirm = await service.process_command(1, "+914428350001", "YES")
        assert confirm.success is False
        await session.commit()


async def test_multi_generation_exact_keys(
    pg_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail 3x → attempt-4 succeeds. Assert exact key pattern and counts."""
    async with pg_session_factory() as setup:
        await _seed_clinic_with_appointment(setup)
        await setup.commit()

    for _ in range(3):
        await _fail_preview_yes(pg_session_factory, monkeypatch)

    async with pg_session_factory() as session:
        _setup_whatsapp(monkeypatch)
        service = OwnerCommandService(session, _leave_gateway())
        preview = await service.process_command(1, "+914428350001", "Dr. Priya leave tomorrow")
        assert preview.success is True
        confirm = await service.process_command(1, "+914428350001", "YES")
        assert confirm.success is True
        await session.commit()

    async with pg_session_factory() as verify:
        rows = (
            await verify.execute(
                text(
                    "SELECT idempotency_key, status FROM owner_command_proposals "
                    "WHERE business_id = 1 ORDER BY created_at"
                )
            )
        ).all()
        assert len(rows) >= 4
        assert rows[0][1] == "failed"
        for i, (key, status) in enumerate(rows[1:-1], start=2):
            assert f"-attempt-{i}" in key
            assert status == "failed"
        assert rows[-1][1] == "completed"

        completed = sum(1 for _, s in rows if s == "completed")
        failed = sum(1 for _, s in rows if s == "failed")
        assert completed == 1
        assert failed == 3


async def test_completed_retry_replay_exact(
    pg_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After fail→retry→complete, same intent replays with exact proposal ID."""
    async with pg_session_factory() as setup:
        await _seed_clinic_with_appointment(setup)
        await setup.commit()

    await _fail_preview_yes(pg_session_factory, monkeypatch)

    async with pg_session_factory() as session:
        _setup_whatsapp(monkeypatch)
        service = OwnerCommandService(session, _leave_gateway())
        preview = await service.process_command(1, "+914428350001", "Dr. Priya leave tomorrow")
        assert preview.success is True
        confirm = await service.process_command(1, "+914428350001", "YES")
        assert confirm.success is True
        completed_id = confirm.proposal_id
        await session.commit()

    count_before = None
    async with pg_session_factory() as verify:
        count_before = await verify.scalar(
            text("SELECT count(*) FROM owner_command_proposals WHERE business_id = 1")
        )

    async with pg_session_factory() as session:
        _setup_whatsapp(monkeypatch)
        service = OwnerCommandService(session, _leave_gateway())
        replay = await service.process_command(1, "+914428350001", "Dr. Priya leave tomorrow")
        assert "already" in replay.response_text.lower()
        assert replay.proposal_id == completed_id

    async with pg_session_factory() as verify:
        count_after = await verify.scalar(
            text("SELECT count(*) FROM owner_command_proposals WHERE business_id = 1")
        )
        assert count_after == count_before


async def test_global_idempotency_savepoint_preserves_outer(
    pg_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Global idempotency unique conflict uses savepoint, not session rollback."""
    async with pg_session_factory() as setup:
        await _seed_clinic_with_appointment(setup)
        await setup.commit()

    async with pg_session_factory() as session:
        _setup_whatsapp(monkeypatch)
        service = OwnerCommandService(session, _leave_gateway())
        first = await service.process_command(1, "+914428350001", "Dr. Priya leave tomorrow")
        assert first.success is True
        confirm = await service.process_command(1, "+914428350001", "YES")
        assert confirm.success is True
        await session.commit()

    async with pg_session_factory() as session:
        _setup_whatsapp(monkeypatch)
        await session.execute(
            text(
                "INSERT INTO business_daily_context "
                "(business_id, context_date, context_type, content, created_by_phone) "
                "VALUES (1, CURRENT_DATE, 'note', 'pre_conflict_sentinel', '+914428350001')"
            )
        )
        await session.flush()

        service = OwnerCommandService(session, _leave_gateway())
        replay = await service.process_command(1, "+914428350001", "Dr. Priya leave tomorrow")
        assert "already" in replay.response_text.lower()

        sentinel = await session.scalar(
            text(
                "SELECT count(*) FROM business_daily_context "
                "WHERE content = 'pre_conflict_sentinel'"
            )
        )
        assert sentinel == 1
        await session.commit()

    async with pg_session_factory() as verify:
        committed = await verify.scalar(
            text(
                "SELECT count(*) FROM business_daily_context "
                "WHERE content = 'pre_conflict_sentinel'"
            )
        )
        assert committed == 1


async def test_retry_key_isolation_across_tenants(
    pg_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry prefix/count is isolated per business_id."""
    async with pg_session_factory() as setup:
        await _seed_clinic_with_appointment(setup)
        await setup.execute(
            text(
                "INSERT INTO businesses "
                "(id, name, category, primary_contact_phone, timezone, subscription) "
                "VALUES (2, 'Other Clinic', 'clinic', '+919000000002', "
                "'Asia/Kolkata', 'trial')"
            )
        )
        await setup.execute(
            text(
                "INSERT INTO business_users (business_id, phone, role, is_active) "
                "VALUES (2, '+919000000002', 'owner', true)"
            )
        )
        await setup.commit()

    await _fail_preview_yes(pg_session_factory, monkeypatch)

    async with pg_session_factory() as verify:
        b1_count = await verify.scalar(
            text("SELECT count(*) FROM owner_command_proposals WHERE business_id = 1")
        )
        b2_count = await verify.scalar(
            text("SELECT count(*) FROM owner_command_proposals WHERE business_id = 2")
        )
        assert b1_count >= 1
        assert b2_count == 0


async def test_pending_partial_unique_savepoint_preserves_outer(
    pg_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Partial unique conflict (one pending per owner) uses savepoint correctly."""
    async with pg_session_factory() as setup:
        await _seed_clinic_with_appointment(setup)
        await setup.commit()

    async with pg_session_factory() as session:
        _setup_whatsapp(monkeypatch)
        await session.execute(
            text(
                "INSERT INTO business_daily_context "
                "(business_id, context_date, context_type, content, created_by_phone) "
                "VALUES (1, CURRENT_DATE, 'note', 'partial_uq_sentinel', '+914428350001')"
            )
        )
        await session.flush()

        service = OwnerCommandService(session, _leave_gateway())
        first = await service.process_command(1, "+914428350001", "Dr. Priya leave tomorrow")
        assert first.success is True

        second = await service.process_command(1, "+914428350001", "Dr. Priya leave tomorrow")
        assert second.success is False
        assert "pending" in second.response_text.lower()

        sentinel = await session.scalar(
            text(
                "SELECT count(*) FROM business_daily_context WHERE content = 'partial_uq_sentinel'"
            )
        )
        assert sentinel == 1
        await session.commit()

    async with pg_session_factory() as verify:
        committed = await verify.scalar(
            text(
                "SELECT count(*) FROM business_daily_context WHERE content = 'partial_uq_sentinel'"
            )
        )
        assert committed == 1
