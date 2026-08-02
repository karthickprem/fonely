"""End-to-end PostgreSQL tests for internal text appointment slice."""

from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fonely.app import create_app
from fonely.core.validators import utcnow
from fonely.models.schema import Appointment, PendingAction, ResourceAllocation

pytestmark = pytest.mark.postgres


def _headers(
    business_id: int = 1,
    phone: str = "+919123456789",
    role: str = "customer",
) -> dict[str, str]:
    return {
        "X-Business-ID": str(business_id),
        "X-Actor-Phone": phone,
        "X-Actor-Role": role,
        "X-Correlation-ID": "e2e-test",
    }


async def _seed_salon(session: AsyncSession) -> None:
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
    await session.commit()


@pytest.fixture
async def seeded_app(
    pg_engine: AsyncEngine,
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncClient:
    async with pg_session_factory() as session:
        await _seed_salon(session)

    app = create_app()
    app.state.engine = pg_engine
    app.state.session_factory = pg_session_factory
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client  # type: ignore[misc]


async def _create_proposal(
    client: AsyncClient,
    *,
    idempotency_key: str = "e2e-appt-1",
    headers: dict[str, str] | None = None,
) -> dict[str, object]:
    now = utcnow()
    slot = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=2)
    response = await client.post(
        "/internal/v1/appointment-proposals",
        json={
            "service_id": 1,
            "resource_id": 1,
            "start_at": slot.isoformat(),
            "customer_phone": "+919123456789",
            "idempotency_key": idempotency_key,
            "expires_at": (now + timedelta(minutes=15)).isoformat(),
        },
        headers=headers or _headers(),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_proposal_and_confirm_e2e(
    seeded_app: AsyncClient,
    pg_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    proposal = await _create_proposal(seeded_app)
    assert proposal["status"] == "awaiting_confirmation"
    assert proposal["slot_is_held"] is False
    pa_id = proposal["pending_action_id"]

    async with pg_session_factory() as session:
        appt_count = await session.scalar(select(func.count(Appointment.id)))
        assert appt_count == 0

    confirm_response = await seeded_app.post(
        f"/internal/v1/appointment-proposals/{pa_id}/confirm",
        json={"expected_version": proposal["version"]},
        headers=_headers(),
    )
    assert confirm_response.status_code == 200, confirm_response.text
    result = confirm_response.json()
    assert result["status"] == "committed"
    assert result["appointment_id"] > 0
    assert result["service_name"] == "Haircut"
    assert result["correlation_id"] == "e2e-test"

    async with pg_session_factory() as session:
        appt = (
            await session.execute(
                select(Appointment).where(Appointment.id == result["appointment_id"])
            )
        ).scalar_one()
        assert appt.business_id == 1
        assert appt.status == "confirmed"
        assert appt.source == "customer_conversation"

        alloc = (
            await session.execute(
                select(ResourceAllocation).where(ResourceAllocation.appointment_id == appt.id)
            )
        ).scalar_one()
        assert alloc.status == "active"

        pa = (
            await session.execute(select(PendingAction).where(PendingAction.id == pa_id))
        ).scalar_one()
        assert pa.status == "confirmed"
        assert pa.committed_entity_id == appt.id


async def test_replay_returns_same_appointment(
    seeded_app: AsyncClient,
) -> None:
    proposal = await _create_proposal(seeded_app, idempotency_key="replay-test")
    pa_id = proposal["pending_action_id"]

    r1 = await seeded_app.post(
        f"/internal/v1/appointment-proposals/{pa_id}/confirm",
        json={"expected_version": proposal["version"]},
        headers=_headers(),
    )
    assert r1.status_code == 200

    r2 = await seeded_app.post(
        f"/internal/v1/appointment-proposals/{pa_id}/confirm",
        json={"expected_version": 999},
        headers=_headers(),
    )
    assert r2.status_code == 200
    assert r2.json()["appointment_id"] == r1.json()["appointment_id"]


async def test_cross_tenant_confirm_returns_404(
    seeded_app: AsyncClient,
) -> None:
    proposal = await _create_proposal(seeded_app, idempotency_key="cross-tenant")
    pa_id = proposal["pending_action_id"]

    response = await seeded_app.post(
        f"/internal/v1/appointment-proposals/{pa_id}/confirm",
        json={"expected_version": proposal["version"]},
        headers=_headers(business_id=999),
    )
    assert response.status_code == 404


async def test_unrelated_customer_confirm_returns_403(
    seeded_app: AsyncClient,
) -> None:
    proposal = await _create_proposal(seeded_app, idempotency_key="unrelated-cust")
    pa_id = proposal["pending_action_id"]

    response = await seeded_app.post(
        f"/internal/v1/appointment-proposals/{pa_id}/confirm",
        json={"expected_version": proposal["version"]},
        headers=_headers(phone="+919999999999"),
    )
    assert response.status_code == 403


async def test_overlapping_slot_returns_retryable(
    seeded_app: AsyncClient,
) -> None:
    now = utcnow()
    slot = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=2)

    p1 = await _create_proposal(seeded_app, idempotency_key="winner-e2e")
    r1 = await seeded_app.post(
        f"/internal/v1/appointment-proposals/{p1['pending_action_id']}/confirm",
        json={"expected_version": p1["version"]},
        headers=_headers(),
    )
    assert r1.status_code == 200

    overlap_slot = slot + timedelta(minutes=15)
    p2_resp = await seeded_app.post(
        "/internal/v1/appointment-proposals",
        json={
            "service_id": 1,
            "resource_id": 1,
            "start_at": overlap_slot.isoformat(),
            "customer_phone": "+919123456789",
            "idempotency_key": "loser-e2e",
            "expires_at": (now + timedelta(minutes=15)).isoformat(),
        },
        headers=_headers(),
    )
    assert p2_resp.status_code == 201
    p2 = p2_resp.json()

    r2 = await seeded_app.post(
        f"/internal/v1/appointment-proposals/{p2['pending_action_id']}/confirm",
        json={"expected_version": p2["version"]},
        headers=_headers(),
    )
    assert r2.status_code == 200
    data = r2.json()
    assert data["status"] == "retryable_failure"
    assert data["error_code"] == "resource_unavailable"
    assert data["retryable"] is True
