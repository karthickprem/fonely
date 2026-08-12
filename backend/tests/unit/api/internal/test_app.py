"""Unit tests for application factory and health endpoints."""

import logging
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
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


# ---------------------------------------------------------------------------
# Router registration
#
# Each router group is gated on the credential it cannot work without, so an
# unset credential removes the routes entirely. That is the right behaviour --
# we should not answer on a capability we cannot perform -- but on its own it
# is indistinguishable from a wrong URL, and an operator can lose an afternoon
# to booking calls that 404 for a reason nothing reports. So every case below
# asserts BOTH halves: the routes really are gone, and startup named the
# setting that would bring them back. Absence must never be the only signal.
# ---------------------------------------------------------------------------

# Every gate create_app() consults, and a value that switches it on. Listed
# once so that adding a gated router group without extending this test fails
# the enabled-case assertion rather than passing unnoticed.
_GATES = {
    "internal_api_secret": "test-secret",
    "whatsapp_verify_token": "verify-token",
    "exotel_webhook_secret": "webhook-secret",
}

_ROUTE_PREFIXES = {
    "internal_api_secret": "/internal/v1",
    "whatsapp_verify_token": "/webhooks/whatsapp",
    "exotel_webhook_secret": "/webhooks/exotel",
}


@contextmanager
def _app_with(**gates: str) -> Iterator[FastAPI]:
    """Build an app with exactly the named gates set and every other one unset.

    Gates are passed explicitly rather than left to the mock's default,
    because an unconfigured MagicMock attribute is truthy -- it would enable
    every router group and quietly invert what each case here is testing.
    """
    with patch("fonely.app.settings") as mock_settings:
        for name in _GATES:
            setattr(mock_settings, name, gates.get(name, ""))
        mock_settings.database_url = "postgresql+asyncpg://localhost/test"
        mock_settings.readiness_timeout_seconds = 3.0
        yield create_app()


def _prefixes(app: FastAPI) -> set[str]:
    """Every path the app actually serves.

    `include_router` nests rather than flattens on this FastAPI version: each
    group lands as an opaque wrapper holding the router on `original_router`,
    so a scan of `app.routes` alone sees only the routes declared in the
    factory and none of the included groups -- which would make every
    assertion here pass for the wrong reason. Descend instead of trusting the
    top level, and handle both shapes so a FastAPI upgrade that goes back to
    flattening does not quietly empty this set.
    """
    found: set[str] = set()

    def walk(routes: object) -> None:
        for route in routes:  # type: ignore[attr-defined]
            if isinstance(route, APIRoute):
                found.add(route.path)
            nested = getattr(route, "original_router", None) or route
            child_routes = getattr(nested, "routes", None)
            if child_routes is not None and child_routes is not routes:
                walk(child_routes)

    walk(app.routes)
    return found


def _has_prefix(app: FastAPI, prefix: str) -> bool:
    return any(path.startswith(prefix) for path in _prefixes(app))


def test_every_router_group_registers_when_its_credential_is_set() -> None:
    with _app_with(**_GATES) as app:
        for gate, prefix in _ROUTE_PREFIXES.items():
            assert _has_prefix(app, prefix), f"{gate} is set but {prefix} did not register"


@pytest.mark.parametrize("gate", sorted(_GATES))
def test_router_group_is_absent_when_its_credential_is_missing(gate: str) -> None:
    enabled = {name: value for name, value in _GATES.items() if name != gate}
    with _app_with(**enabled) as app:
        assert not _has_prefix(app, _ROUTE_PREFIXES[gate])
        # The other groups are unaffected -- one missing credential must not
        # take down capabilities that have theirs.
        for other in enabled:
            assert _has_prefix(app, _ROUTE_PREFIXES[other])


@pytest.mark.parametrize(
    ("gate", "env_var"),
    [
        ("internal_api_secret", "INTERNAL_API_SECRET"),
        ("whatsapp_verify_token", "WHATSAPP_VERIFY_TOKEN"),
        ("exotel_webhook_secret", "EXOTEL_WEBHOOK_SECRET"),
    ],
)
def test_disabled_group_names_the_setting_that_enables_it(
    gate: str, env_var: str, caplog: pytest.LogCaptureFixture
) -> None:
    enabled = {name: value for name, value in _GATES.items() if name != gate}
    with caplog.at_level(logging.WARNING, logger="fonely.app"), _app_with(**enabled):
        pass

    summary = [r for r in caplog.records if r.message == "router_groups_disabled"]
    assert summary, "no startup summary of disabled router groups"
    missing = summary[0].missing_settings  # type: ignore[attr-defined]
    assert any(env_var in entry for entry in missing), (
        f"{gate} is unset but {env_var} was not named in {missing}"
    )
    # The summary carries the machine-readable list; the per-entry lines are
    # what a human tailing the log actually sees.
    assert any(env_var in r.getMessage() for r in caplog.records)


def test_nothing_is_reported_disabled_when_everything_is_configured(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="fonely.app"), _app_with(**_GATES):
        pass
    assert not [r for r in caplog.records if r.message == "router_groups_disabled"]


async def test_booking_404s_but_not_silently_without_the_internal_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The exact failure this reporting exists for.

    With no INTERNAL_API_SECRET a patient booking call 404s, which reads as a
    wrong URL. The 404 is correct -- there is no credential to authenticate
    against -- so what has to hold is that the reason was stated at startup.
    """
    with (
        caplog.at_level(logging.WARNING, logger="fonely.app"),
        _app_with(whatsapp_verify_token="verify-token") as app,
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/internal/v1/appointment-proposals",
                json={},
                headers={"X-Business-ID": "1", **_auth_headers()},
            )

    assert response.status_code == 404
    assert any("INTERNAL_API_SECRET" in r.getMessage() for r in caplog.records)
