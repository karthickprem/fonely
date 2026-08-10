"""PostgreSQL evidence for durable availability-offer conversation state."""

import asyncio
import time as monotonic_time
from datetime import UTC, datetime, time, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fonely.api.internal.validation import InternalValidationPort
from fonely.domain.conversation.state import ConversationState, ConversationTurn
from fonely.domain.pending_actions.commands import ActorContext
from fonely.models.enums import CallerRole
from fonely.models.schema import Appointment, PendingAction, ResourceAllocation
from fonely.services.appointments import AppointmentService
from fonely.services.availability import AvailableSlot
from fonely.services.availability_offers import create_availability_offer
from fonely.services.conversation import _CONVERSATIONS, ConversationService
from fonely.services.conversation_persistence import ConversationPersistenceService
from fonely.services.conversation_tools import BusinessContext, ResourceInfo, ServiceInfo
from fonely.services.model_gateway import ModelResponse

pytestmark = pytest.mark.postgres


def _offer(conversation_id: str, *, business_id: int = 1):
    start = datetime(2099, 8, 10, 11, 30, tzinfo=UTC)
    return create_availability_offer(
        business_id=business_id,
        conversation_id=conversation_id,
        service_id=1,
        business_timezone="Asia/Kolkata",
        alternatives=(
            AvailableSlot(
                start_at=start,
                end_at=start + timedelta(minutes=30),
                resource_id=1,
                resource_name="Dr. Priya",
            ),
        ),
        now=datetime(2099, 8, 10, 4, 0, tzinfo=UTC),
    )


async def _seed_business(session: AsyncSession, business_id: int = 1) -> None:
    await session.execute(
        text(
            "INSERT INTO businesses "
            "(id, name, category, primary_contact_phone, timezone, subscription) "
            "VALUES (:id, :name, 'dental', :phone, 'Asia/Kolkata', 'trial')"
        ),
        {
            "id": business_id,
            "name": f"Business {business_id}",
            "phone": f"+91900000000{business_id}",
        },
    )
    await session.commit()


async def test_offer_and_selected_ref_survive_fresh_session(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_business(session)
        persistence = ConversationPersistenceService(session)
        ctx = await persistence.load_or_create(1, "+919123456789")
        ctx.state = ConversationState.FACT_COLLECTION
        ctx.collected_facts = {
            "service_id": 1,
            "start_at": datetime(2099, 8, 10, 10, 0, tzinfo=UTC),
        }
        ctx.availability_offer = _offer(ctx.conversation_id)
        ctx.selected_slot_ref = ctx.availability_offer.slots[0]
        turn = ConversationTurn(
            turn_id="turn-1",
            conversation_id=ctx.conversation_id,
            business_id=1,
            state=ctx.state,
            user_message="5 PM",
            assistant_response="Selected 5 PM",
            collected_facts=dict(ctx.collected_facts),
            missing_facts=[],
        )
        ctx.turns.append(turn)
        await persistence.save_turn(ctx, turn)
        await session.commit()
        conversation_id = ctx.conversation_id
        original_offer = ctx.availability_offer

    async with pg_session_factory() as fresh_session:
        restored = await ConversationPersistenceService(fresh_session).load_by_id(conversation_id)

    assert restored is not None
    assert restored.availability_offer == original_offer
    assert restored.selected_slot_ref == original_offer.slots[0]
    assert restored.collected_facts["start_at"] == datetime(2099, 8, 10, 10, 0, tzinfo=UTC)


def _actor() -> ActorContext:
    return ActorContext(
        business_id=1,
        normalized_phone="+919123456789",
        verified_role=CallerRole.CUSTOMER,
    )


def _business_context() -> BusinessContext:
    return BusinessContext(
        business_id=1,
        name="Business 1",
        timezone="Asia/Kolkata",
        services=[
            ServiceInfo(
                id=1,
                name="Consultation",
                duration_minutes=30,
                buffer_before_minutes=0,
                buffer_after_minutes=0,
                price="300",
            )
        ],
        resources=[ResourceInfo(id=1, name="Dr. Priya", resource_type="staff")],
        eligibility=[(1, 1)],
    )


async def _seed_scheduling(session: AsyncSession) -> None:
    await _seed_business(session)
    await session.execute(
        text(
            "INSERT INTO services "
            "(id,business_id,name,duration_minutes,buffer_before_minutes,"
            "buffer_after_minutes,price,is_active) "
            "VALUES (1,1,'Consultation',30,0,0,300,true)"
        )
    )
    await session.execute(
        text(
            "INSERT INTO resources (id,business_id,name,resource_type,is_active) "
            "VALUES (1,1,'Dr. Priya','staff',true)"
        )
    )
    await session.execute(
        text(
            "INSERT INTO service_resource_eligibility "
            "(business_id,service_id,resource_id,is_active) VALUES (1,1,1,true)"
        )
    )
    await session.execute(
        text(
            "INSERT INTO operating_schedules "
            "(business_id,day_of_week,open_time,close_time,is_active) "
            "SELECT 1,day,'09:00','20:00',true FROM generate_series(0,6) AS day"
        )
    )
    await session.commit()


@pytest.fixture(autouse=True)
def _clear_conversations() -> None:
    _CONVERSATIONS.clear()


async def test_stale_offer_slot_rechecks_and_creates_no_proposal(
    pg_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kolkata = ZoneInfo("Asia/Kolkata")
    day = datetime.now(kolkata).date() + timedelta(days=2)
    offered_start = datetime.combine(day, time(17, 0), tzinfo=kolkata).astimezone(UTC)

    async with pg_session_factory() as setup:
        await _seed_scheduling(setup)
        persistence = ConversationPersistenceService(setup)
        ctx = await persistence.load_or_create(1, _actor().normalized_phone)
        ctx.state = ConversationState.FACT_COLLECTION
        ctx.collected_facts = {
            "_operation": "book",
            "service_id": 1,
            "service_name": "Consultation",
            "resource_id": 1,
            "resource_name": "Dr. Priya",
            "customer_phone": _actor().normalized_phone,
            "start_at": offered_start - timedelta(minutes=30),
        }
        ctx.availability_offer = create_availability_offer(
            business_id=1,
            conversation_id=ctx.conversation_id,
            service_id=1,
            business_timezone="Asia/Kolkata",
            alternatives=(
                AvailableSlot(
                    offered_start,
                    offered_start + timedelta(minutes=30),
                    1,
                    "Dr. Priya",
                ),
            ),
            now=datetime.now(UTC),
        )
        turn = ConversationTurn(
            turn_id="turn-offer",
            conversation_id=ctx.conversation_id,
            business_id=1,
            state=ctx.state,
            user_message="offer",
            assistant_response="5 PM",
            collected_facts=dict(ctx.collected_facts),
            missing_facts=["start_at"],
        )
        ctx.turns.append(turn)
        await persistence.save_turn(ctx, turn)
        await setup.commit()
        conversation_id = ctx.conversation_id

    async with pg_session_factory() as contender:
        await contender.execute(
            text(
                "INSERT INTO appointments "
                "(business_id,service_id,resource_id,customer_phone,start_at,end_at,"
                "effective_start_at,effective_end_at,service_name_snapshot,"
                "resource_name_snapshot,duration_minutes_snapshot,"
                "buffer_before_minutes_snapshot,buffer_after_minutes_snapshot,"
                "business_timezone_snapshot,status,source,idempotency_key) "
                "VALUES (1,1,1,:phone,:start,:end,:start,:end,'Consultation','Dr. Priya',"
                "30,0,0,'Asia/Kolkata','confirmed','owner_manual','dev5-competing')"
            ),
            {
                "phone": "+919999999999",
                "start": offered_start,
                "end": offered_start + timedelta(minutes=30),
            },
        )
        appointment_id = await contender.scalar(
            text("SELECT id FROM appointments WHERE idempotency_key='dev5-competing'")
        )
        await contender.execute(
            text(
                "INSERT INTO resource_allocations "
                "(business_id,appointment_id,resource_id,allocation_type,source,"
                "effective_start_at,effective_end_at,status,idempotency_key,version) "
                "VALUES (1,:appointment_id,1,'manual_appointment','owner_manual',"
                ":start,:end,'active','dev5-competing-allocation',1)"
            ),
            {
                "appointment_id": appointment_id,
                "start": offered_start,
                "end": offered_start + timedelta(minutes=30),
            },
        )
        await contender.commit()

    monkeypatch.setattr(
        "fonely.services.conversation_tools.get_business_context",
        AsyncMock(return_value=_business_context()),
    )
    gateway = AsyncMock()
    gateway.complete.return_value = ModelResponse(text="unused")

    async with pg_session_factory() as selection_session:
        service = ConversationService(
            selection_session,
            gateway,
            appointment_service=AppointmentService(
                selection_session, validation=InternalValidationPort(selection_session)
            ),
        )
        turn = await service.process_message(conversation_id, 1, _actor(), "5:00 PM")
        await selection_session.commit()

    assert "isn't available" in turn.assistant_response
    async with pg_session_factory() as verify:
        assert await verify.scalar(select(func.count(PendingAction.id))) == 0
        assert await verify.scalar(select(func.count(Appointment.id))) == 1
        assert await verify.scalar(select(func.count(ResourceAllocation.id))) == 1


async def _pid(session: AsyncSession) -> int:
    value = await session.scalar(text("SELECT pg_backend_pid()"))
    assert isinstance(value, int)
    return value


async def _observe_blocker(
    factory: async_sessionmaker[AsyncSession], blocked_pid: int, blocker_pid: int
) -> None:
    start = monotonic_time.monotonic()
    while monotonic_time.monotonic() - start < 5:
        async with factory() as observer:
            row = (
                await observer.execute(
                    text(
                        "SELECT :blocker = ANY(pg_blocking_pids(:blocked)), wait_event_type "
                        "FROM pg_stat_activity WHERE pid = :blocked"
                    ),
                    {"blocker": blocker_pid, "blocked": blocked_pid},
                )
            ).one_or_none()
        if row is not None and row[0] is True:
            assert row[1] == "Lock"
            return
        await asyncio.sleep(0.01)
    raise AssertionError("Timed out waiting for resource-lock contention")


async def test_selection_blocks_then_rejects_competing_schedule_mutation(
    pg_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kolkata = ZoneInfo("Asia/Kolkata")
    day = datetime.now(kolkata).date() + timedelta(days=3)
    offered_start = datetime.combine(day, time(17, 0), tzinfo=kolkata).astimezone(UTC)

    async with pg_session_factory() as setup:
        await _seed_scheduling(setup)
        persistence = ConversationPersistenceService(setup)
        ctx = await persistence.load_or_create(1, _actor().normalized_phone)
        ctx.state = ConversationState.FACT_COLLECTION
        ctx.collected_facts = {
            "_operation": "book",
            "service_id": 1,
            "service_name": "Consultation",
            "resource_id": 1,
            "resource_name": "Dr. Priya",
            "customer_phone": _actor().normalized_phone,
            "start_at": offered_start - timedelta(minutes=30),
        }
        ctx.availability_offer = create_availability_offer(
            business_id=1,
            conversation_id=ctx.conversation_id,
            service_id=1,
            business_timezone="Asia/Kolkata",
            alternatives=(
                AvailableSlot(
                    offered_start,
                    offered_start + timedelta(minutes=30),
                    1,
                    "Dr. Priya",
                ),
            ),
            now=datetime.now(UTC),
        )
        turn = ConversationTurn(
            turn_id="turn-lock-offer",
            conversation_id=ctx.conversation_id,
            business_id=1,
            state=ctx.state,
            user_message="offer",
            assistant_response="5 PM",
            collected_facts=dict(ctx.collected_facts),
            missing_facts=["start_at"],
        )
        ctx.turns.append(turn)
        await persistence.save_turn(ctx, turn)
        await setup.commit()
        conversation_id = ctx.conversation_id

    monkeypatch.setattr(
        "fonely.services.conversation_tools.get_business_context",
        AsyncMock(return_value=_business_context()),
    )
    gateway = AsyncMock()
    gateway.complete.return_value = ModelResponse(text="unused")

    async with pg_session_factory() as holder:
        await holder.execute(text("SET LOCAL lock_timeout = '8s'"))
        await holder.execute(
            text("SELECT 1 FROM resources WHERE business_id=1 AND id=1 FOR UPDATE")
        )
        holder_pid = await _pid(holder)
        blocked_pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()

        async def select_offer() -> object:
            async with pg_session_factory() as selection_session:
                await selection_session.execute(text("SET LOCAL lock_timeout = '8s'"))
                blocked_pid.set_result(await _pid(selection_session))
                service = ConversationService(
                    selection_session,
                    gateway,
                    appointment_service=AppointmentService(
                        selection_session,
                        validation=InternalValidationPort(selection_session),
                    ),
                )
                result = await service.process_message(conversation_id, 1, _actor(), "5:00 PM")
                await selection_session.commit()
                return result

        task = asyncio.create_task(select_offer())
        await _observe_blocker(pg_session_factory, await blocked_pid, holder_pid)
        assert not task.done(), "selection must remain blocked before schedule mutation commits"
        await holder.execute(
            text(
                "INSERT INTO schedule_exceptions "
                "(business_id,resource_id,exception_date,is_closed,reason) "
                "VALUES (1,1,:day,true,'provider unavailable')"
            ),
            {"day": day},
        )
        await holder.commit()
        result = await asyncio.wait_for(task, timeout=10)

    assert "isn't available" in result.assistant_response  # type: ignore[attr-defined]
    async with pg_session_factory() as verify:
        assert await verify.scalar(select(func.count(PendingAction.id))) == 0
        assert await verify.scalar(select(func.count(Appointment.id))) == 0
        assert await verify.scalar(select(func.count(ResourceAllocation.id))) == 0


async def test_tampered_persisted_offer_is_not_restored(
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_session_factory() as session:
        await _seed_business(session)
        persistence = ConversationPersistenceService(session)
        ctx = await persistence.load_or_create(1, "+919123456789")
        ctx.availability_offer = _offer(ctx.conversation_id)
        turn = ConversationTurn(
            turn_id="turn-1",
            conversation_id=ctx.conversation_id,
            business_id=1,
            state=ctx.state,
            user_message="test",
            assistant_response="test",
            collected_facts={},
            missing_facts=[],
        )
        ctx.turns.append(turn)
        await persistence.save_turn(ctx, turn)
        await session.commit()
        conversation_id = ctx.conversation_id

        await session.execute(
            text(
                "UPDATE conversations "
                "SET collected_facts = jsonb_set(collected_facts, "
                "'{_availability_selection,offer,business_id}', '2'::jsonb) "
                "WHERE id = :id"
            ),
            {"id": conversation_id},
        )
        await session.commit()

    async with pg_session_factory() as fresh_session:
        restored = await ConversationPersistenceService(fresh_session).load_by_id(conversation_id)

    assert restored is not None
    assert restored.availability_offer is None
