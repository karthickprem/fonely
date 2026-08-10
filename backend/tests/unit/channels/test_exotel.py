"""Unit tests for Exotel telephony adapter — contract-compliant vertical."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fonely.api.channels.exotel import (
    router,
)
from fonely.core.config import settings
from fonely.domain.calls.events import (
    ExotelCallbackParseError,
    parse_exotel_callback,
)
from fonely.domain.calls.transitions import (
    InvalidCallTransitionError,
    is_terminal,
    validate_transition,
)
from fonely.services.exotel_config import ExotelNumberMapping
from tests.fixtures.exotel_callbacks.fixtures import (
    ANSWERED_INBOUND,
    ANSWERED_OUTBOUND,
    BUSY_OUTBOUND,
    COMPLETED_INBOUND,
    COMPLETED_OUTBOUND,
    FAILED_OUTBOUND,
    MISSING_EVENT_TYPE_ANSWERED,
    MISSING_EVENT_TYPE_TERMINAL,
    MISSING_OPTIONAL_FIELDS,
    NEGATIVE_DURATION,
    NO_ANSWER_OUTBOUND,
)

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
    mock_result.one_or_none.return_value = None
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


# ============================================================================
# Domain: Event parsing
# ============================================================================


class TestExotelCallbackEventParsing:
    def test_answered_outbound_parses(self) -> None:
        event = parse_exotel_callback(ANSWERED_OUTBOUND)
        assert event.call_sid == "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
        assert event.event_type == "answered"
        assert event.status == "in-progress"
        assert event.caller_phone == "+919876543210"
        assert event.called_number == "08012345678"
        assert event.direction == "outbound-api"
        assert event.custom_field == "1:corr-001"

    def test_completed_outbound_parses_duration(self) -> None:
        event = parse_exotel_callback(COMPLETED_OUTBOUND)
        assert event.status == "completed"
        assert event.duration == 120
        assert event.conversation_duration == 95
        assert event.event_type == "terminal"

    def test_failed_outbound_parses(self) -> None:
        event = parse_exotel_callback(FAILED_OUTBOUND)
        assert event.status == "failed"
        assert event.event_type == "terminal"
        assert event.duration is None

    def test_busy_parses(self) -> None:
        event = parse_exotel_callback(BUSY_OUTBOUND)
        assert event.status == "busy"

    def test_no_answer_parses(self) -> None:
        event = parse_exotel_callback(NO_ANSWER_OUTBOUND)
        assert event.status == "no-answer"

    def test_inbound_answered_parses(self) -> None:
        event = parse_exotel_callback(ANSWERED_INBOUND)
        assert event.direction == "inbound"

    def test_inbound_completed_parses(self) -> None:
        event = parse_exotel_callback(COMPLETED_INBOUND)
        assert event.duration == 180
        assert event.conversation_duration == 150

    def test_missing_optional_fields_accepted(self) -> None:
        event = parse_exotel_callback(MISSING_OPTIONAL_FIELDS)
        assert event.duration is None
        assert event.direction is None
        assert event.custom_field is None

    def test_negative_duration_treated_as_none(self) -> None:
        event = parse_exotel_callback(NEGATIVE_DURATION)
        assert event.duration is None

    def test_missing_event_type_inferred_terminal(self) -> None:
        event = parse_exotel_callback(MISSING_EVENT_TYPE_TERMINAL)
        assert event.event_type == "terminal"

    def test_missing_event_type_inferred_answered(self) -> None:
        event = parse_exotel_callback(MISSING_EVENT_TYPE_ANSWERED)
        assert event.event_type == "answered"

    def test_missing_call_sid_raises(self) -> None:
        with pytest.raises(ExotelCallbackParseError, match="CallSid"):
            parse_exotel_callback({"Status": "completed", "From": "x", "To": "y"})

    def test_unrecognized_status_raises(self) -> None:
        with pytest.raises(ExotelCallbackParseError, match="unrecognized"):
            parse_exotel_callback({"CallSid": "abc", "Status": "unknown", "From": "x", "To": "y"})

    def test_missing_from_raises(self) -> None:
        with pytest.raises(ExotelCallbackParseError, match="From or To"):
            parse_exotel_callback({"CallSid": "abc", "Status": "completed", "To": "y"})


# ============================================================================
# Domain: State transitions
# ============================================================================


class TestCallStatusTransitions:
    def test_none_to_any_allowed(self) -> None:
        for status in ("queued", "in-progress", "completed", "failed", "busy", "no-answer"):
            assert validate_transition(None, status) == status

    def test_in_progress_to_terminal_allowed(self) -> None:
        for terminal in ("completed", "failed", "busy", "no-answer"):
            assert validate_transition("in-progress", terminal) == terminal

    def test_terminal_to_same_is_idempotent(self) -> None:
        assert validate_transition("completed", "completed") == "completed"

    def test_terminal_to_different_terminal_raises(self) -> None:
        with pytest.raises(InvalidCallTransitionError):
            validate_transition("completed", "failed")

    def test_terminal_to_non_terminal_raises(self) -> None:
        with pytest.raises(InvalidCallTransitionError):
            validate_transition("completed", "in-progress")

    def test_is_terminal(self) -> None:
        assert is_terminal("completed")
        assert is_terminal("failed")
        assert not is_terminal("in-progress")
        assert not is_terminal("queued")


# ============================================================================
# Adapter: Auth
# ============================================================================


class TestWebhookAuth:
    def test_valid_secret_accepted(self) -> None:
        app, _ = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status",
            json=ANSWERED_OUTBOUND,
            headers=_auth_headers(),
        )
        assert response.status_code == 200

    def test_missing_header_returns_401(self) -> None:
        app, _ = _create_app()
        client = TestClient(app)
        response = client.post("/webhooks/exotel/call-status", json=ANSWERED_OUTBOUND)
        assert response.status_code == 401

    def test_wrong_secret_returns_401_no_db(self) -> None:
        app, mock_session = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status",
            json=ANSWERED_OUTBOUND,
            headers={"X-Exotel-Webhook-Secret": "wrong-secret-value-padding"},
        )
        assert response.status_code == 401
        mock_session.execute.assert_not_awaited()

    def test_empty_config_fails_closed(self) -> None:
        with patch.object(settings, "exotel_webhook_secret", ""):
            app, _ = _create_app()
            client = TestClient(app)
            response = client.post(
                "/webhooks/exotel/call-status",
                json=ANSWERED_OUTBOUND,
                headers={"X-Exotel-Webhook-Secret": "anything-at-all-padded"},
            )
        assert response.status_code == 401

    def test_short_secret_fails_closed(self) -> None:
        with patch.object(settings, "exotel_webhook_secret", "short"):
            app, _ = _create_app()
            client = TestClient(app)
            response = client.post(
                "/webhooks/exotel/call-status",
                json=ANSWERED_OUTBOUND,
                headers={"X-Exotel-Webhook-Secret": "short"},
            )
        assert response.status_code == 401

    def test_duplicate_header_returns_401(self) -> None:
        app, _ = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status",
            json=ANSWERED_OUTBOUND,
            headers=[
                ("X-Exotel-Webhook-Secret", _TEST_SECRET),
                ("X-Exotel-Webhook-Secret", _TEST_SECRET),
            ],
        )
        assert response.status_code == 401


# ============================================================================
# Adapter: Content handling
# ============================================================================


class TestContentHandling:
    def test_json_accepted(self) -> None:
        app, _ = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status",
            json=COMPLETED_OUTBOUND,
            headers=_auth_headers(),
        )
        assert response.status_code == 200

    def test_form_urlencoded_rejected(self) -> None:
        app, _ = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status",
            content="CallSid=abc&Status=completed",
            headers={
                **_auth_headers(),
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        assert response.status_code == 415

    def test_invalid_json_returns_400(self) -> None:
        app, _ = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status",
            content=b"not json",
            headers={**_auth_headers(), "Content-Type": "application/json"},
        )
        assert response.status_code == 400

    def test_invalid_callback_payload_returns_400(self) -> None:
        app, _ = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status",
            json={"irrelevant": "data"},
            headers=_auth_headers(),
        )
        assert response.status_code == 400

    def test_oversize_content_length_returns_413(self) -> None:
        app, mock_session = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status",
            content=b'{"x": 1}',
            headers={
                **_auth_headers(),
                "Content-Type": "application/json",
                "Content-Length": "999999",
            },
        )
        assert response.status_code == 413
        mock_session.execute.assert_not_awaited()


# ============================================================================
# Adapter: Business routing
# ============================================================================


class TestBusinessRouting:
    def test_unknown_number_returns_404(self) -> None:
        app, _ = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status",
            json={**ANSWERED_OUTBOUND, "To": "09999999999"},
            headers=_auth_headers(),
        )
        assert response.status_code == 404

    def test_unknown_number_log_has_no_phone(self, caplog) -> None:  # type: ignore[no-untyped-def]
        app, _ = _create_app()
        client = TestClient(app)
        with caplog.at_level("WARNING", logger="fonely.api.channels.exotel"):
            client.post(
                "/webhooks/exotel/call-status",
                json={**ANSWERED_OUTBOUND, "To": "09999999999"},
                headers=_auth_headers(),
            )
        records = [r for r in caplog.records if r.name == "fonely.api.channels.exotel"]
        assert len(records) == 1
        serialized = repr(records[0].__dict__)
        assert "09999999999" not in serialized
        assert "+919876543210" not in serialized


# ============================================================================
# Adapter: Durable persistence
# ============================================================================


class TestDurablePersistence:
    def test_callback_persists_before_200(self) -> None:
        app, mock_session = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status",
            json=ANSWERED_OUTBOUND,
            headers=_auth_headers(),
        )
        assert response.status_code == 200
        mock_session.execute.assert_awaited()
        mock_session.commit.assert_awaited()

    def test_persistence_failure_returns_500(self) -> None:
        app, mock_session = _create_app()
        mock_session.execute.side_effect = Exception("db down")
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status",
            json=ANSWERED_OUTBOUND,
            headers=_auth_headers(),
        )
        assert response.status_code == 500

    def test_all_status_values_accepted(self) -> None:
        for fixture in (
            ANSWERED_OUTBOUND,
            COMPLETED_OUTBOUND,
            FAILED_OUTBOUND,
            BUSY_OUTBOUND,
            NO_ANSWER_OUTBOUND,
            ANSWERED_INBOUND,
            COMPLETED_INBOUND,
        ):
            app, _ = _create_app()
            client = TestClient(app)
            response = client.post(
                "/webhooks/exotel/call-status",
                json=fixture,
                headers=_auth_headers(),
            )
            assert response.status_code == 200, f"Failed for {fixture.get('Status')}"


# ============================================================================
# Adapter: Privacy
# ============================================================================


class TestPrivacy:
    def test_success_log_has_no_phone(self, caplog) -> None:  # type: ignore[no-untyped-def]
        app, _ = _create_app()
        client = TestClient(app)
        with caplog.at_level("INFO", logger="fonely.api.channels.exotel"):
            client.post(
                "/webhooks/exotel/call-status",
                json=COMPLETED_OUTBOUND,
                headers=_auth_headers(),
            )
        records = [
            r
            for r in caplog.records
            if r.name == "fonely.api.channels.exotel" and "processed" in r.getMessage()
        ]
        assert len(records) >= 1
        serialized = repr(records[0].__dict__)
        assert "+919876543210" not in serialized
        assert "08012345678" not in serialized

    def test_secret_not_in_response(self) -> None:
        app, _ = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status",
            json=ANSWERED_OUTBOUND,
            headers={"X-Exotel-Webhook-Secret": "wrong-secret-value-padding"},
        )
        assert _TEST_SECRET not in response.text


# ============================================================================
# Feature gate
# ============================================================================


class TestFeatureGate:
    def test_empty_secret_does_not_mount_route(self) -> None:
        from fonely.app import create_app

        with patch("fonely.app.settings") as mock_settings:
            mock_settings.internal_api_secret = ""
            mock_settings.whatsapp_verify_token = ""
            mock_settings.exotel_webhook_secret = ""
            app = create_app()
        paths = {route.path for route in app.routes}
        assert "/webhooks/exotel/call-status" not in paths


# ============================================================================
# Number mapping
# ============================================================================


class TestNumberMapping:
    def test_known_number_returns_business_id(self) -> None:
        mapping = ExotelNumberMapping({"08012345678": 1, "08087654321": 2})
        assert mapping.get_business_id("08012345678") == 1

    def test_unknown_number_returns_none(self) -> None:
        mapping = ExotelNumberMapping({"08012345678": 1})
        assert mapping.get_business_id("09999999999") is None


# ============================================================================
# WebSocket placeholder
# ============================================================================


class TestAudioStreamWebSocket:
    def test_connects_and_closes(self) -> None:
        app, _ = _create_app()
        client = TestClient(app)
        with client.websocket_connect("/webhooks/exotel/audio-stream") as ws:
            ws.send_bytes(b"\x00" * 320)
            ws.close()
