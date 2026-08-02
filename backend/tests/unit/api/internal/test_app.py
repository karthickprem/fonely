"""Unit tests for application factory and health endpoints."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from fonely.app import create_app

_PATCHES = (
    patch("fonely.app.settings"),
    patch("fonely.api.internal.appointments.settings"),
)


def _configure(mock_app: MagicMock, mock_route: MagicMock, secret: str) -> None:
    for m in (mock_app, mock_route):
        m.internal_api_secret = secret
        m.database_url = "postgresql+asyncpg://localhost/test"
        m.readiness_timeout_seconds = 3.0


@pytest.fixture
def app():  # type: ignore[no-untyped-def]
    p1, p2 = patch("fonely.app.settings"), patch("fonely.api.internal.appointments.settings")
    mock_app = p1.start()
    mock_route = p2.start()
    _configure(mock_app, mock_route, "test-secret")
    application = create_app()

    engine = MagicMock()
    conn = AsyncMock()

    @asynccontextmanager
    async def _connect():  # type: ignore[no-untyped-def]
        yield conn

    engine.connect = _connect
    application.state.engine = engine
    application.state.session_factory = MagicMock()
    yield application
    p1.stop()
    p2.stop()


@pytest.fixture
async def client(app):  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-secret"}


async def test_liveness(client: AsyncClient) -> None:
    response = await client.get("/health/live")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_readiness_checks_database(client: AsyncClient) -> None:
    response = await client.get("/health/ready")
    assert response.status_code == 200


async def test_correlation_id_returned(client: AsyncClient) -> None:
    response = await client.get(
        "/health/live",
        headers={"X-Correlation-ID": "test-123"},
    )
    assert response.headers.get("X-Correlation-ID") == "test-123"


async def test_correlation_id_generated_when_absent(client: AsyncClient) -> None:
    response = await client.get("/health/live")
    assert "X-Correlation-ID" in response.headers


async def test_missing_business_id_returns_400(client: AsyncClient) -> None:
    response = await client.post(
        "/internal/v1/appointment-proposals",
        json={
            "service_id": 1,
            "resource_id": 1,
            "start_at": "2026-08-05T10:00:00Z",
            "customer_phone": "+919123456789",
            "idempotency_key": "test",
            "expires_at": "2026-08-05T11:00:00Z",
        },
        headers={
            "X-Actor-Phone": "+919123456789",
            **_auth_headers(),
        },
    )
    assert response.status_code == 400


async def test_missing_auth_returns_401(client: AsyncClient) -> None:
    response = await client.post(
        "/internal/v1/appointment-proposals",
        json={
            "service_id": 1,
            "resource_id": 1,
            "start_at": "2026-08-05T10:00:00Z",
            "customer_phone": "+919123456789",
            "idempotency_key": "test",
            "expires_at": "2026-08-05T11:00:00Z",
        },
        headers={
            "X-Business-ID": "1",
            "X-Actor-Phone": "+919123456789",
        },
    )
    assert response.status_code == 401


async def test_wrong_auth_returns_401(client: AsyncClient) -> None:
    response = await client.post(
        "/internal/v1/appointment-proposals",
        json={
            "service_id": 1,
            "resource_id": 1,
            "start_at": "2026-08-05T10:00:00Z",
            "customer_phone": "+919123456789",
            "idempotency_key": "test",
            "expires_at": "2026-08-05T11:00:00Z",
        },
        headers={
            "X-Business-ID": "1",
            "X-Actor-Phone": "+919123456789",
            "Authorization": "Bearer wrong-secret",
        },
    )
    assert response.status_code == 401
