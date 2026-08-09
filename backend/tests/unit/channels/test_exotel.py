"""Unit tests for Exotel telephony adapter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fonely.api.channels.exotel import router
from fonely.core.config import settings
from fonely.services.exotel_config import ExotelNumberMapping

_TEST_SECRET = "test-exotel-webhook-secret-value"


def _create_app(
    mapping: dict[str, int] | None = None,
) -> tuple[FastAPI, AsyncMock]:
    app = FastAPI()
    app.include_router(router)
    app.state.exotel_mapping = ExotelNumberMapping(mapping or {"08012345678": 1})

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one.return_value = 42
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()

    mock_factory = MagicMock()
    mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
    app.state.session_factory = mock_factory
    app.state._mock_session = mock_session
    return app, mock_session


@pytest.fixture(autouse=True)
def _configure_secret():
    with patch.object(settings, "exotel_webhook_secret", _TEST_SECRET):
        yield


def _auth_headers() -> dict[str, str]:
    return {"X-Exotel-Webhook-Secret": _TEST_SECRET}


def _ringing_body() -> dict[str, str]:
    return {
        "CallSid": "call-123",
        "Status": "ringing",
        "To": "08012345678",
        "From": "+919876543210",
    }


class TestWebhookAuth:
    def test_valid_secret_accepted(self) -> None:
        app, _ = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status",
            json=_ringing_body(),
            headers=_auth_headers(),
        )
        assert response.status_code == 200

    def test_missing_header_returns_401(self) -> None:
        app, _ = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status",
            json=_ringing_body(),
        )
        assert response.status_code == 401
        assert response.text == "unauthorized"

    def test_empty_header_returns_401(self) -> None:
        app, _ = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status",
            json=_ringing_body(),
            headers={"X-Exotel-Webhook-Secret": ""},
        )
        assert response.status_code == 401

    def test_wrong_secret_returns_401(self) -> None:
        app, _ = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status",
            json=_ringing_body(),
            headers={"X-Exotel-Webhook-Secret": "wrong-secret-value"},
        )
        assert response.status_code == 401
        assert response.text == "unauthorized"

    def test_empty_config_secret_fails_closed(self) -> None:
        with patch.object(settings, "exotel_webhook_secret", ""):
            app, _ = _create_app()
            client = TestClient(app)
            response = client.post(
                "/webhooks/exotel/call-status",
                json=_ringing_body(),
                headers={"X-Exotel-Webhook-Secret": "anything"},
            )
        assert response.status_code == 401

    def test_unauthorized_request_never_parses_body_or_writes_db(self) -> None:
        app, mock_session = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status",
            json=_ringing_body(),
            headers={"X-Exotel-Webhook-Secret": "wrong"},
        )
        assert response.status_code == 401
        mock_session.execute.assert_not_awaited()
        mock_session.commit.assert_not_awaited()

    def test_secret_not_in_response_body(self) -> None:
        app, _ = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status",
            json=_ringing_body(),
            headers={"X-Exotel-Webhook-Secret": "wrong"},
        )
        assert _TEST_SECRET not in response.text
        assert "wrong" not in response.text

    def test_constant_time_comparison_used(self) -> None:
        with patch("fonely.api.channels.exotel.hmac.compare_digest", return_value=True) as mock_cmp:
            app, _ = _create_app()
            client = TestClient(app)
            client.post(
                "/webhooks/exotel/call-status",
                json=_ringing_body(),
                headers=_auth_headers(),
            )
        mock_cmp.assert_called_once_with(_TEST_SECRET, _TEST_SECRET)

    def test_duplicate_auth_header_returns_401(self) -> None:
        app, _ = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status",
            json=_ringing_body(),
            headers=[
                ("X-Exotel-Webhook-Secret", _TEST_SECRET),
                ("X-Exotel-Webhook-Secret", _TEST_SECRET),
            ],
        )
        assert response.status_code == 401

    def test_whitespace_padded_secret_returns_401(self) -> None:
        app, _ = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status",
            json=_ringing_body(),
            headers={"X-Exotel-Webhook-Secret": f" {_TEST_SECRET} "},
        )
        assert response.status_code == 401

    def test_wrong_content_type_returns_415(self) -> None:
        app, _ = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status",
            content="CallSid=call-123&Status=ringing",
            headers={
                "X-Exotel-Webhook-Secret": _TEST_SECRET,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        assert response.status_code == 415

    def test_oversize_content_length_returns_413(self) -> None:
        app, mock_session = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status",
            content=b'{"x": 1}',
            headers={
                "X-Exotel-Webhook-Secret": _TEST_SECRET,
                "Content-Type": "application/json",
                "Content-Length": "999999",
            },
        )
        assert response.status_code == 413
        mock_session.execute.assert_not_awaited()

    def test_oversize_unauthenticated_rejected_before_parse(self) -> None:
        app, mock_session = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status",
            content=b'{"x": 1}',
            headers={
                "X-Exotel-Webhook-Secret": "wrong",
                "Content-Type": "application/json",
            },
        )
        assert response.status_code == 401
        mock_session.execute.assert_not_awaited()


class TestCallStatusWebhook:
    def test_ringing_returns_200(self) -> None:
        app, mock_session = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status",
            json=_ringing_body(),
            headers=_auth_headers(),
        )
        assert response.status_code == 200
        assert response.text == "ok"
        mock_session.execute.assert_awaited()
        mock_session.commit.assert_awaited()

    def test_answered_returns_200(self) -> None:
        app, _ = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status",
            json={
                "CallSid": "call-123",
                "Status": "answered",
                "To": "08012345678",
                "From": "+919876543210",
            },
            headers=_auth_headers(),
        )
        assert response.status_code == 200

    def test_completed_updates_call(self) -> None:
        app, mock_session = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status",
            json={
                "CallSid": "call-123",
                "Status": "completed",
                "To": "08012345678",
                "From": "+919876543210",
                "Duration": "120",
            },
            headers=_auth_headers(),
        )
        assert response.status_code == 200
        mock_session.execute.assert_awaited()
        mock_session.commit.assert_awaited()

    def test_missing_call_sid_returns_400(self) -> None:
        app, _ = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status",
            json={"Status": "ringing", "To": "08012345678"},
            headers=_auth_headers(),
        )
        assert response.status_code == 400

    def test_unknown_number_returns_404(self) -> None:
        app, _ = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status",
            json={
                "CallSid": "call-123",
                "Status": "ringing",
                "To": "09999999999",
                "From": "+919876543210",
            },
            headers=_auth_headers(),
        )
        assert response.status_code == 404

    def test_failed_status_returns_200(self) -> None:
        app, _ = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status",
            json={
                "CallSid": "call-123",
                "Status": "failed",
                "To": "08012345678",
                "From": "+919876543210",
            },
            headers=_auth_headers(),
        )
        assert response.status_code == 200


class TestAudioStreamWebSocket:
    def test_connects_and_closes(self) -> None:
        app, _ = _create_app()
        client = TestClient(app)
        with client.websocket_connect("/webhooks/exotel/audio-stream") as ws:
            ws.send_bytes(b"\x00" * 320)
            ws.close()


class TestNumberMapping:
    def test_known_number_returns_business_id(self) -> None:
        mapping = ExotelNumberMapping({"08012345678": 1, "08087654321": 2})
        assert mapping.get_business_id("08012345678") == 1
        assert mapping.get_business_id("08087654321") == 2

    def test_unknown_number_returns_none(self) -> None:
        mapping = ExotelNumberMapping({"08012345678": 1})
        assert mapping.get_business_id("09999999999") is None

    def test_empty_mappings(self) -> None:
        mapping = ExotelNumberMapping({})
        assert mapping.get_business_id("08012345678") is None

    def test_loads_from_settings(self) -> None:
        with patch("fonely.services.exotel_config.settings") as mock_settings:
            mock_settings.exotel_number_mappings = '{"080123": 5}'
            mapping = ExotelNumberMapping()
            assert mapping.get_business_id("080123") == 5

    def test_invalid_json_falls_back_to_empty(self) -> None:
        with patch("fonely.services.exotel_config.settings") as mock_settings:
            mock_settings.exotel_number_mappings = "not json"
            mapping = ExotelNumberMapping()
            assert mapping.get_business_id("anything") is None
