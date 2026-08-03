"""Unit tests for Exotel telephony adapter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from fonely.api.channels.exotel import router
from fonely.services.exotel_config import ExotelNumberMapping


def _create_app(mapping: dict[str, int] | None = None) -> FastAPI:
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
    return app


class TestCallStatusWebhook:
    def test_ringing_returns_200(self) -> None:
        app = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status",
            json={
                "CallSid": "call-123",
                "Status": "ringing",
                "To": "08012345678",
                "From": "+919876543210",
            },
        )
        assert response.status_code == 200
        assert response.text == "ok"
        app.state._mock_session.execute.assert_awaited()
        app.state._mock_session.commit.assert_awaited()

    def test_answered_returns_200(self) -> None:
        app = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status",
            json={
                "CallSid": "call-123",
                "Status": "answered",
                "To": "08012345678",
                "From": "+919876543210",
            },
        )
        assert response.status_code == 200

    def test_completed_updates_call(self) -> None:
        app = _create_app()
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
        )
        assert response.status_code == 200
        app.state._mock_session.execute.assert_awaited()
        app.state._mock_session.commit.assert_awaited()

    def test_missing_call_sid_returns_400(self) -> None:
        app = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status",
            json={"Status": "ringing", "To": "08012345678"},
        )
        assert response.status_code == 400

    def test_unknown_number_returns_404(self) -> None:
        app = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status",
            json={
                "CallSid": "call-123",
                "Status": "ringing",
                "To": "09999999999",
                "From": "+919876543210",
            },
        )
        assert response.status_code == 404

    def test_failed_status_returns_200(self) -> None:
        app = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status",
            json={
                "CallSid": "call-123",
                "Status": "failed",
                "To": "08012345678",
                "From": "+919876543210",
            },
        )
        assert response.status_code == 200


class TestAudioStreamWebSocket:
    def test_connects_and_closes(self) -> None:
        app = _create_app()
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
