"""ADR 0001 adversarial probes — acceptance tests for admission.

Proves the three acceptance criteria from the amended ADR:
1. Forged X-Forwarded-For → zero intake, zero quarantine, zero DB effect
2. Direct app request (no gateway secret) → zero effect
3. Gateway-authenticated fixture → succeeds

Tests go through the actual mounted route via TestClient, not helpers.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fonely.api.channels.exotel import router
from fonely.core.config import settings
from fonely.domain.calls.correlation import InMemoryCorrelationStore
from fonely.services.exotel_config import ExotelNumberMapping
from tests.fixtures.exotel_callbacks.test_intake import InMemoryCallEventIntake

_GATEWAY_SECRET = "gateway-injected-secret-at-least-32-chars"

_CALLBACK_FIXTURE = {
    "CallSid": "a" * 32,
    "EventType": "terminal",
    "Status": "completed",
    "From": "+919000000001",
    "To": "08012345678",
    "Duration": "60",
}


def _create_admitted_app() -> tuple[FastAPI, InMemoryCallEventIntake]:
    """Create app with route mounted, intake wired, mapping configured."""
    app = FastAPI()
    app.include_router(router)
    app.state.exotel_mapping = ExotelNumberMapping({"08012345678": 1})
    intake = InMemoryCallEventIntake()
    app.state.exotel_intake = intake
    return app, intake


@pytest.fixture(autouse=True)
def _configure_secret():
    with patch.object(settings, "exotel_webhook_secret", _GATEWAY_SECRET):
        yield


# ============================================================================
# ADR 0001 Probe 1: Forged X-Forwarded-For → zero effect
# ============================================================================


class TestForgedXFFZeroEffect:
    """A request with a spoofed X-Forwarded-For header — simulating an
    attacker who forges the forwarded-for chain — must produce zero
    intake rows, zero quarantine rows, and zero database effect.

    In production, the gateway strips client-supplied XFF before injecting
    its own. At the app level, what matters is: WITHOUT the correct
    gateway secret, nothing reaches the database regardless of headers.
    """

    def test_forged_xff_without_secret_rejected(self) -> None:
        """Forged XFF, no gateway secret → 401, no intake."""
        app, intake = _create_admitted_app()
        client = TestClient(app)

        response = client.post(
            "/webhooks/exotel/call-status",
            json=_CALLBACK_FIXTURE,
            headers={
                "X-Forwarded-For": "103.21.0.1",
            },
        )

        assert response.status_code == 401
        assert not intake.persist_called
        assert len(intake.events) == 0

    def test_forged_xff_with_wrong_secret_rejected(self) -> None:
        """Forged XFF + wrong secret → 401, no intake."""
        app, intake = _create_admitted_app()
        client = TestClient(app)

        response = client.post(
            "/webhooks/exotel/call-status",
            json=_CALLBACK_FIXTURE,
            headers={
                "X-Forwarded-For": "103.21.0.1",
                "X-Exotel-Webhook-Secret": "attacker-guessed-wrong-value!!",
            },
        )

        assert response.status_code == 401
        assert not intake.persist_called
        assert len(intake.events) == 0

    def test_forged_xff_with_correct_secret_succeeds(self) -> None:
        """Forged XFF but correct gateway secret → 200.

        This is the scenario where the gateway DID authenticate the
        request (injected the correct secret). The XFF is informational
        at the app level — the secret is the trust boundary.
        """
        app, intake = _create_admitted_app()
        client = TestClient(app)

        response = client.post(
            "/webhooks/exotel/call-status",
            json=_CALLBACK_FIXTURE,
            headers={
                "X-Forwarded-For": "103.21.0.1",
                "X-Exotel-Webhook-Secret": _GATEWAY_SECRET,
            },
        )

        assert response.status_code == 200
        assert intake.persist_called
        assert len(intake.events) == 1


# ============================================================================
# ADR 0001 Probe 2: Direct app request → zero effect
# ============================================================================


class TestDirectAppZeroEffect:
    """A request that bypasses the gateway entirely — no secret header,
    no XFF — must produce zero intake rows and zero database effect.
    """

    def test_no_headers_at_all(self) -> None:
        """Bare POST with no auth headers → 401."""
        app, intake = _create_admitted_app()
        client = TestClient(app)

        response = client.post(
            "/webhooks/exotel/call-status",
            json=_CALLBACK_FIXTURE,
        )

        assert response.status_code == 401
        assert not intake.persist_called

    def test_empty_secret_header(self) -> None:
        """Empty secret header value → 401."""
        app, intake = _create_admitted_app()
        client = TestClient(app)

        response = client.post(
            "/webhooks/exotel/call-status",
            json=_CALLBACK_FIXTURE,
            headers={"X-Exotel-Webhook-Secret": ""},
        )

        assert response.status_code == 401
        assert not intake.persist_called

    def test_duplicate_secret_headers(self) -> None:
        """Two secret headers (even if both correct) → 401."""
        app, intake = _create_admitted_app()
        client = TestClient(app)

        response = client.post(
            "/webhooks/exotel/call-status",
            json=_CALLBACK_FIXTURE,
            headers=[
                ("X-Exotel-Webhook-Secret", _GATEWAY_SECRET),
                ("X-Exotel-Webhook-Secret", _GATEWAY_SECRET),
            ],
        )

        assert response.status_code == 401
        assert not intake.persist_called

    def test_whitespace_padded_secret(self) -> None:
        """Secret with leading/trailing whitespace → 401."""
        app, intake = _create_admitted_app()
        client = TestClient(app)

        response = client.post(
            "/webhooks/exotel/call-status",
            json=_CALLBACK_FIXTURE,
            headers={"X-Exotel-Webhook-Secret": f" {_GATEWAY_SECRET} "},
        )

        assert response.status_code == 401
        assert not intake.persist_called

    def test_auth_before_body_parse(self) -> None:
        """Auth rejection happens before body is consumed.

        Sending a large body with wrong auth: if auth is checked first,
        the large body is never read → no parse attempt.
        """
        app, intake = _create_admitted_app()
        client = TestClient(app)

        response = client.post(
            "/webhooks/exotel/call-status",
            content=b"x" * 50_000,
            headers={
                "Content-Type": "application/json",
                "X-Exotel-Webhook-Secret": "wrong",
            },
        )

        assert response.status_code == 401
        assert not intake.persist_called


# ============================================================================
# ADR 0001 Probe 3: Gateway-authenticated fixture → succeeds
# ============================================================================


class TestGatewayAuthenticatedSucceeds:
    """A properly gateway-authenticated request reaches intake."""

    def test_valid_callback_accepted(self) -> None:
        app, intake = _create_admitted_app()
        client = TestClient(app)

        response = client.post(
            "/webhooks/exotel/call-status",
            json=_CALLBACK_FIXTURE,
            headers={"X-Exotel-Webhook-Secret": _GATEWAY_SECRET},
        )

        assert response.status_code == 200
        assert intake.persist_called
        assert len(intake.events) == 1
        record = intake.events[0]
        assert record.business_id == 1
        assert record.provider == "exotel"
        assert record.provider_call_id == "a" * 32

    def test_duplicate_callback_returns_200(self) -> None:
        """Duplicate (retry) also returns 200 — provider stops retrying."""
        app, intake = _create_admitted_app()
        client = TestClient(app)
        headers = {"X-Exotel-Webhook-Secret": _GATEWAY_SECRET}

        r1 = client.post(
            "/webhooks/exotel/call-status",
            json=_CALLBACK_FIXTURE,
            headers=headers,
        )
        r2 = client.post(
            "/webhooks/exotel/call-status",
            json=_CALLBACK_FIXTURE,
            headers=headers,
        )

        assert r1.status_code == 200
        assert r2.status_code == 200
        assert len(intake.events) == 1

    def test_secret_not_leaked_in_response(self) -> None:
        """The configured secret never appears in any response."""
        app, _intake = _create_admitted_app()
        client = TestClient(app)

        r_ok = client.post(
            "/webhooks/exotel/call-status",
            json=_CALLBACK_FIXTURE,
            headers={"X-Exotel-Webhook-Secret": _GATEWAY_SECRET},
        )
        r_fail = client.post(
            "/webhooks/exotel/call-status",
            json=_CALLBACK_FIXTURE,
            headers={"X-Exotel-Webhook-Secret": "wrong"},
        )

        assert _GATEWAY_SECRET not in r_ok.text
        assert _GATEWAY_SECRET not in r_fail.text
        assert _GATEWAY_SECRET not in str(r_ok.headers)
        assert _GATEWAY_SECRET not in str(r_fail.headers)


# ============================================================================
# Correlation probes
# ============================================================================


class TestCorrelationProbes:
    """Prove correlation outcomes through the actual route."""

    def test_matched_correlation_proceeds_to_intake(self) -> None:
        """Callback correlates against an admitted session → intake."""
        from fonely.domain.calls.correlation import CorrelationRecord

        app, intake = _create_admitted_app()
        store = InMemoryCorrelationStore()

        import asyncio

        asyncio.run(
            store.register_admitted_call(
                CorrelationRecord(
                    provider="exotel",
                    provider_account_id="AC_test",
                    provider_call_id="a" * 32,
                    called_number="08012345678",
                    business_id=1,
                    direction=None,
                )
            )
        )

        app.state.exotel_correlation = store
        app.state.exotel_account_id = "AC_test"
        client = TestClient(app)

        response = client.post(
            "/webhooks/exotel/call-status",
            json=_CALLBACK_FIXTURE,
            headers={"X-Exotel-Webhook-Secret": _GATEWAY_SECRET},
        )

        assert response.status_code == 200
        assert len(intake.events) == 1

    def test_conflict_correlation_dead_letters(self) -> None:
        """Callback with wrong business → conflict → 200 but no intake."""
        from fonely.domain.calls.correlation import CorrelationRecord

        app, intake = _create_admitted_app()
        store = InMemoryCorrelationStore()

        import asyncio

        asyncio.run(
            store.register_admitted_call(
                CorrelationRecord(
                    provider="exotel",
                    provider_account_id="AC_test",
                    provider_call_id="a" * 32,
                    called_number="08012345678",
                    business_id=999,
                    direction=None,
                )
            )
        )

        app.state.exotel_correlation = store
        app.state.exotel_account_id = "AC_test"
        client = TestClient(app)

        response = client.post(
            "/webhooks/exotel/call-status",
            json=_CALLBACK_FIXTURE,
            headers={"X-Exotel-Webhook-Secret": _GATEWAY_SECRET},
        )

        assert response.status_code == 200
        assert len(intake.events) == 0

    def test_pending_correlation_still_reaches_intake(self) -> None:
        """No session record yet → pending → still persists to intake.

        Inbound calls must not be rejected for being unknown.
        """
        app, intake = _create_admitted_app()
        store = InMemoryCorrelationStore()
        app.state.exotel_correlation = store
        app.state.exotel_account_id = "AC_test"
        client = TestClient(app)

        response = client.post(
            "/webhooks/exotel/call-status",
            json=_CALLBACK_FIXTURE,
            headers={"X-Exotel-Webhook-Secret": _GATEWAY_SECRET},
        )

        assert response.status_code == 200
        assert len(intake.events) == 1
