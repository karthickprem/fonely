"""End-to-end API route tests through the real FastAPI composition root.

These tests prove that composition root, middleware, auth, routing, and
ConversationService all wire together to produce a committed appointment
in PostgreSQL. The model gateway is mocked; the database is real.
"""

from datetime import UTC, datetime, time, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fonely.core.validators import utcnow
from fonely.models.schema import Appointment, PendingAction, ResourceAllocation
from fonely.services.conversation import _CONVERSATIONS
from fonely.services.model_gateway import ModelResponse

pytestmark = pytest.mark.postgres

_SECRET = "test-secret-e2e"
_AUTH_HEADERS = {
    "Authorization": f"Bearer {_SECRET}",
    "X-Business-ID": "1",
    "X-Actor-Phone": "+919123456789",
    "X-Actor-Role": "customer",
}


def _mock_gateway() -> AsyncMock:
    gw = AsyncMock()
    gw.complete.return_value = ModelResponse(text="Sure, let me help!")
    return gw


async def _seed_dental_clinic(session: AsyncSession) -> None:
    await session.execute(
        text(
            "INSERT INTO businesses "
            "(id, name, category, primary_contact_phone, timezone, subscription) "
            "VALUES (1, 'Smile Dental Clinic', 'dental', '+919000000001', "
            "'Asia/Kolkata', 'trial')"
        )
    )
    await session.execute(
        text(
            "INSERT INTO services "
            "(id, business_id, name, duration_minutes, buffer_before_minutes, "
            "buffer_after_minutes, price, is_active) "
            "VALUES (1, 1, 'General Consultation', 30, 0, 0, 300.00, true)"
        )
    )
    await session.execute(
        text(
            "INSERT INTO resources (id, business_id, name, resource_type, is_active) "
            "VALUES (1, 1, 'Dr. Priya', 'staff', true)"
        )
    )
    await session.execute(
        text(
            "INSERT INTO service_resource_eligibility "
            "(business_id, service_id, resource_id, is_active) "
            "VALUES (1, 1, 1, true)"
        )
    )
    for day in range(1, 7):
        await session.execute(
            text(
                "INSERT INTO operating_schedules "
                "(business_id, day_of_week, open_time, close_time, is_active) "
                "VALUES (1, :day, '10:00', '13:00', true)"
            ),
            {"day": day},
        )
        await session.execute(
            text(
                "INSERT INTO operating_schedules "
                "(business_id, day_of_week, open_time, close_time, is_active) "
                "VALUES (1, :day, '17:00', '20:30', true)"
            ),
            {"day": day},
        )
    await session.commit()


@pytest.fixture(autouse=True)
def _clear_conversations():
    _CONVERSATIONS.clear()
    yield
    _CONVERSATIONS.clear()


def _create_app_with_test_db(database_url: str) -> object:
    with (
        patch("fonely.core.config.settings.internal_api_secret", _SECRET),
        patch("fonely.core.config.settings.database_url", database_url),
        patch("fonely.core.config.settings.sarvam_api_key", ""),
        patch("fonely.core.config.settings.whatsapp_verify_token", ""),
        patch("fonely.core.config.settings.rate_limit_per_minute", 60),
    ):
        from fonely.app import create_app

        app = create_app()
    return app


async def test_full_booking_through_http(
    pg_engine: AsyncEngine,
    pg_session_factory: async_sessionmaker[AsyncSession],
    postgres_database_url: str,
) -> None:
    async with pg_session_factory() as session:
        await _seed_dental_clinic(session)

    gateway = _mock_gateway()

    app = _create_app_with_test_db(postgres_database_url)

    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with patch("fonely.app.settings.database_url", postgres_database_url):
            app.state.model_gateway = gateway  # type: ignore[union-attr]

            r = await client.post(
                "/internal/v1/conversations",
                json={"business_id": 1},
                headers=_AUTH_HEADERS,
            )
            assert r.status_code == 201, f"Create conversation failed: {r.text}"
            data = r.json()
            conv_id = data["conversation_id"]
            assert data["state"] == "greeting"

            r2 = await client.post(
                f"/internal/v1/conversations/{conv_id}/messages",
                json={"message": "I want to book a consultation"},
                headers=_AUTH_HEADERS,
            )
            assert r2.status_code == 200, f"Message 1 failed: {r2.text}"
            turn1 = r2.json()
            assert turn1["state"] == "fact_collection"

            now = utcnow()
            target = now + timedelta(days=1)
            if target.isoweekday() == 7:
                target += timedelta(days=1)
            slot_start = datetime.combine(target.date(), time(10, 30), tzinfo=UTC)

            ctx = _CONVERSATIONS[conv_id]
            ctx.collected_facts["service_id"] = 1
            ctx.collected_facts["service_name"] = "General Consultation"
            ctx.collected_facts["resource_id"] = 1
            ctx.collected_facts["resource_name"] = "Dr. Priya"
            ctx.collected_facts["customer_phone"] = "+919123456789"
            ctx.collected_facts["start_at"] = slot_start

            r3 = await client.post(
                f"/internal/v1/conversations/{conv_id}/messages",
                json={"message": "yes that works"},
                headers=_AUTH_HEADERS,
            )
            assert r3.status_code == 200, f"Message 2 failed: {r3.text}"

            r4 = await client.post(
                f"/internal/v1/conversations/{conv_id}/messages",
                json={"message": "yes"},
                headers=_AUTH_HEADERS,
            )
            assert r4.status_code == 200, f"Confirm failed: {r4.text}"
            confirm_data = r4.json()
            assert confirm_data["state"] == "completed"
            assert "confirmed" in confirm_data["assistant_response"].lower()

    async with pg_session_factory() as session:
        appt = (
            await session.execute(
                select(Appointment).where(
                    Appointment.business_id == 1,
                    Appointment.service_id == 1,
                )
            )
        ).scalar_one()
        assert appt.status == "confirmed"
        assert appt.pending_action_id is not None
        assert appt.resource_id == 1

        alloc = (
            await session.execute(
                select(ResourceAllocation).where(ResourceAllocation.appointment_id == appt.id)
            )
        ).scalar_one()
        assert alloc.status == "active"

        pa = (
            await session.execute(
                select(PendingAction).where(PendingAction.id == appt.pending_action_id)
            )
        ).scalar_one()
        assert pa.status == "confirmed"
        assert pa.committed_entity_id == appt.id


async def test_medical_escalation_through_http(
    pg_engine: AsyncEngine,
    pg_session_factory: async_sessionmaker[AsyncSession],
    postgres_database_url: str,
) -> None:
    async with pg_session_factory() as session:
        await _seed_dental_clinic(session)

    gateway = _mock_gateway()
    app = _create_app_with_test_db(postgres_database_url)

    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        app.state.model_gateway = gateway  # type: ignore[union-attr]

        r = await client.post(
            "/internal/v1/conversations",
            json={"business_id": 1},
            headers=_AUTH_HEADERS,
        )
        assert r.status_code == 201
        conv_id = r.json()["conversation_id"]

        r2 = await client.post(
            f"/internal/v1/conversations/{conv_id}/messages",
            json={"message": "I want to book"},
            headers=_AUTH_HEADERS,
        )
        assert r2.status_code == 200

        r3 = await client.post(
            f"/internal/v1/conversations/{conv_id}/messages",
            json={"message": "my tooth is bleeding badly"},
            headers=_AUTH_HEADERS,
        )
        assert r3.status_code == 200
        escalation = r3.json()
        assert escalation["safety_classification"] == "medical"

    async with pg_session_factory() as session:
        count = await session.scalar(select(func.count(Appointment.id)))
        assert count == 0


async def test_security_headers_present(
    pg_engine: AsyncEngine,
    postgres_database_url: str,
) -> None:
    app = _create_app_with_test_db(postgres_database_url)
    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/health/live")
        assert r.status_code == 200
        assert r.headers.get("X-Content-Type-Options") == "nosniff"
        assert r.headers.get("X-Frame-Options") == "DENY"


async def test_unauthenticated_request_rejected(
    pg_engine: AsyncEngine,
    postgres_database_url: str,
) -> None:
    app = _create_app_with_test_db(postgres_database_url)
    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/internal/v1/conversations",
            json={"business_id": 1},
        )
        assert r.status_code in (401, 503)
