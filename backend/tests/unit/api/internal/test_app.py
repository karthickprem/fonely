"""Unit tests for application factory and health endpoints."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from fonely.app import create_app


@pytest.fixture
def app():  # type: ignore[no-untyped-def]
    application = create_app()
    engine = MagicMock()
    conn = AsyncMock()

    @asynccontextmanager
    async def _connect():  # type: ignore[no-untyped-def]
        yield conn

    engine.connect = _connect
    application.state.engine = engine
    application.state.session_factory = MagicMock()
    return application


@pytest.fixture
async def client(app):  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_liveness(client: AsyncClient) -> None:
    response = await client.get("/health/live")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_readiness_checks_database(client: AsyncClient) -> None:
    response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"


async def test_correlation_id_returned(client: AsyncClient) -> None:
    response = await client.get(
        "/health/live",
        headers={"X-Correlation-ID": "test-123"},
    )
    assert response.headers.get("X-Correlation-ID") == "test-123"


async def test_correlation_id_generated_when_absent(client: AsyncClient) -> None:
    response = await client.get("/health/live")
    assert "X-Correlation-ID" in response.headers
    assert len(response.headers["X-Correlation-ID"]) > 0


async def test_missing_business_id_returns_400(client: AsyncClient) -> None:
    response = await client.post(
        "/internal/v1/appointment-proposals",
        json={
            "service_id": 1,
            "start_at": "2026-08-05T10:00:00Z",
            "customer_phone": "+919123456789",
            "idempotency_key": "test",
            "expires_at": "2026-08-05T11:00:00Z",
        },
        headers={"X-Actor-Phone": "+919123456789"},
    )
    assert response.status_code == 400
