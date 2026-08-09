"""PostgreSQL failure and replay evidence for transactional appointment notifications."""

from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fonely.core.validators import utcnow
from fonely.domain.appointments.commands import (
    ConfirmPendingAppointmentCommand,
    CreatePendingAppointmentCommand,
)
from fonely.domain.pending_actions.commands import ActorContext
from fonely.domain.pending_actions.payloads import (
    AppointmentFacts,
    CreateAppointmentData,
    PendingAppointmentEnvelope,
)
from fonely.models.enums import CallerRole
from fonely.models.schema import (
    Appointment,
    NotificationOutboxEvent,
    PendingAction,
    ResourceAllocation,
)
from fonely.services.appointments import AppointmentService
from fonely.services.notifications import NotificationService

pytestmark = pytest.mark.postgres


@pytest.fixture(autouse=True)
def _whatsapp_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    from fonely.services import notifications, whatsapp_config

    mapping = '{"phone-1": 1}'
    monkeypatch.setattr(whatsapp_config.settings, "whatsapp_business_mappings", mapping)
    monkeypatch.setattr(notifications.settings, "whatsapp_business_mappings", mapping)


class StubValidationPort:
    def __init__(self, facts: AppointmentFacts) -> None:
        self._facts = facts

    async def validate_for_actor(
        self, actor: ActorContext, payload: PendingAppointmentEnvelope
    ) -> PendingAppointmentEnvelope:
        assert isinstance(payload.data, CreateAppointmentData)
        return PendingAppointmentEnvelope(
            data=CreateAppointmentData(
                facts=self._facts,
                customer_name=payload.data.customer_name,
                customer_phone=payload.data.customer_phone,
                reason=payload.data.reason,
                call_id=payload.data.call_id,
            )
        )

    async def validate_stored(
        self, business_id: int, payload: PendingAppointmentEnvelope
    ) -> PendingAppointmentEnvelope:
        return payload

    async def validate_idempotent_retry(
        self,
        actor: ActorContext,
        proposed: PendingAppointmentEnvelope,
        stored: PendingAppointmentEnvelope,
    ) -> None:
        return None

    async def validate_completion_evidence(
        self,
        business_id: int,
        payload: PendingAppointmentEnvelope,
        committed_entity_type: str,
        committed_entity_id: int,
    ) -> None:
        return None


async def _seed_catalog(session: AsyncSession) -> None:
    await session.execute(
        text(
            "INSERT INTO businesses "
            "(id, name, category, primary_contact_phone, timezone, subscription) "
            "VALUES (1, 'Salon', 'salon', '+919000000001', 'Asia/Kolkata', 'trial')"
        )
    )
    await session.execute(
        text(
            "INSERT INTO services "
            "(id, business_id, name, duration_minutes, buffer_before_minutes, "
            "buffer_after_minutes, price, is_active) "
            "VALUES (1, 1, 'Haircut', 30, 0, 0, 500.00, true)"
        )
    )
    await session.execute(
        text(
            "INSERT INTO resources (id, business_id, name, resource_type, is_active) "
            "VALUES (1, 1, 'Priya', 'staff', true)"
        )
    )
    await session.execute(
        text(
            "INSERT INTO service_resource_eligibility "
            "(business_id, service_id, resource_id, is_active) "
            "VALUES (1, 1, 1, true)"
        )
    )
    await session.flush()


def _facts(start: datetime) -> AppointmentFacts:
    end = start + timedelta(minutes=30)
    return AppointmentFacts(
        service_id=1,
        service_name="Haircut",
        resource_id=1,
        resource_name="Priya",
        start_at=start,
        end_at=end,
        effective_start_at=start,
        effective_end_at=end,
        duration_minutes=30,
        price="500.00",
        business_timezone="Asia/Kolkata",
    )


def _actor() -> ActorContext:
    return ActorContext(
        business_id=1,
        normalized_phone="+919123456789",
        verified_role=CallerRole.CUSTOMER,
    )


async def _proposal(
    session: AsyncSession,
    start: datetime,
    *,
    key: str,
) -> tuple[AppointmentService, int, int]:
    service = AppointmentService(session, validation=StubValidationPort(_facts(start)))
    proposal = await service.create_proposal(
        CreatePendingAppointmentCommand(
            actor=_actor(),
            service_id=1,
            resource_id=1,
            start_at=start,
            customer_name="Patient",
            customer_phone="+919123456789",
            expires_at=utcnow() + timedelta(minutes=15),
            idempotency_key=key,
        )
    )
    return service, proposal.pending_action_id, proposal.version


async def _fresh_counts(
    factory: async_sessionmaker[AsyncSession], pending_action_id: int
) -> dict[str, Any]:
    async with factory() as observer:
        action = await observer.get(PendingAction, pending_action_id)
        return {
            "pa_status": action.status if action else None,
            "pa_version": action.version if action else None,
            "appointments": await observer.scalar(select(func.count(Appointment.id))),
            "allocations": await observer.scalar(select(func.count(ResourceAllocation.id))),
            "outbox": await observer.scalar(select(func.count(NotificationOutboxEvent.id))),
        }


@pytest.mark.parametrize(
    ("boundary", "error_message"),
    [
        ("after_begin", "forced_after_begin"),
        ("after_mutation", "forced_after_mutation"),
        ("after_outbox", "forced_after_outbox"),
        ("complete_commit", "forced_complete_commit"),
    ],
)
async def test_create_failure_boundaries_leave_no_partial_state(
    pg_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    error_message: str,
) -> None:
    start = utcnow().replace(minute=0, second=0, microsecond=0) + timedelta(hours=6)
    async with pg_session_factory() as session:
        await _seed_catalog(session)
        service, action_id, action_version = await _proposal(
            session, start, key=f"failure-{boundary}"
        )
        await session.commit()

    async with pg_session_factory() as session:
        service = AppointmentService(session, validation=StubValidationPort(_facts(start)))
        if boundary == "after_begin":
            original = service._pa_service.begin_commit

            async def fail_after_begin(*args: Any, **kwargs: Any) -> Any:
                await original(*args, **kwargs)
                raise RuntimeError(error_message)

            monkeypatch.setattr(service._pa_service, "begin_commit", fail_after_begin)
        elif boundary == "after_mutation":
            original = service._repo.insert_allocation

            async def fail_after_mutation(*args: Any, **kwargs: Any) -> Any:
                await original(*args, **kwargs)
                raise RuntimeError(error_message)

            monkeypatch.setattr(service._repo, "insert_allocation", fail_after_mutation)
        elif boundary == "after_outbox":
            original = NotificationService.create_appointment_notifications

            async def fail_after_outbox(*args: Any, **kwargs: Any) -> Any:
                await original(*args, **kwargs)
                raise RuntimeError(error_message)

            monkeypatch.setattr(
                NotificationService, "create_appointment_notifications", fail_after_outbox
            )
        else:
            monkeypatch.setattr(
                service._pa_service,
                "complete_commit",
                AsyncMock(side_effect=RuntimeError(error_message)),
            )

        with pytest.raises(RuntimeError, match=error_message):
            await service.confirm_and_commit(
                ConfirmPendingAppointmentCommand(
                    actor=_actor(),
                    pending_action_id=action_id,
                    expected_version=action_version,
                )
            )
        await session.rollback()

    state = await _fresh_counts(pg_session_factory, action_id)
    assert state == {
        "pa_status": "awaiting_confirmation",
        "pa_version": action_version,
        "appointments": 0,
        "allocations": 0,
        "outbox": 0,
    }


async def test_outer_commit_failed_then_fresh_retry_executes_once(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    start = utcnow().replace(minute=0, second=0, microsecond=0) + timedelta(hours=8)
    async with pg_session_factory() as session:
        await _seed_catalog(session)
        _, action_id, action_version = await _proposal(session, start, key="outer-rollback")
        await session.commit()

    async with pg_session_factory() as session:
        service = AppointmentService(session, validation=StubValidationPort(_facts(start)))
        await service.confirm_and_commit(
            ConfirmPendingAppointmentCommand(
                actor=_actor(),
                pending_action_id=action_id,
                expected_version=action_version,
            )
        )
        await session.rollback()

    rolled_back = await _fresh_counts(pg_session_factory, action_id)
    assert rolled_back["pa_status"] == "awaiting_confirmation"
    assert rolled_back["appointments"] == 0
    assert rolled_back["allocations"] == 0
    assert rolled_back["outbox"] == 0

    async with pg_session_factory() as session:
        service = AppointmentService(session, validation=StubValidationPort(_facts(start)))
        await service.confirm_and_commit(
            ConfirmPendingAppointmentCommand(
                actor=_actor(),
                pending_action_id=action_id,
                expected_version=action_version,
            )
        )
        await session.commit()

    committed = await _fresh_counts(pg_session_factory, action_id)
    assert committed["pa_status"] == "confirmed"
    assert committed["appointments"] == 1
    assert committed["allocations"] == 1
    assert committed["outbox"] == 2


async def test_lost_success_response_replay_repairs_without_duplicates(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    start = utcnow().replace(minute=0, second=0, microsecond=0) + timedelta(hours=10)
    async with pg_session_factory() as session:
        await _seed_catalog(session)
        _, action_id, action_version = await _proposal(session, start, key="lost-response")
        await session.commit()

    async with pg_session_factory() as session:
        service = AppointmentService(session, validation=StubValidationPort(_facts(start)))
        first = await service.confirm_and_commit(
            ConfirmPendingAppointmentCommand(
                actor=_actor(),
                pending_action_id=action_id,
                expected_version=action_version,
            )
        )
        await session.commit()

    async with pg_session_factory() as session:
        service = AppointmentService(session, validation=StubValidationPort(_facts(start)))
        replay = await service.confirm_and_commit(
            ConfirmPendingAppointmentCommand(
                actor=_actor(),
                pending_action_id=action_id,
                expected_version=action_version,
            )
        )
        await session.commit()

    assert replay == first
    committed = await _fresh_counts(pg_session_factory, action_id)
    assert committed["appointments"] == 1
    assert committed["allocations"] == 1
    assert committed["outbox"] == 2
