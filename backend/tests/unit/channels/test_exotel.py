"""Unit tests for Exotel adapter — strict validation, intake contract, negatives."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fonely.api.channels.exotel import router
from fonely.core.config import settings
from fonely.domain.calls.events import ExotelCallbackParseError, parse_exotel_callback
from fonely.domain.calls.transitions import (
    InvalidCallTransitionError,
    LateCallEventError,
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
from tests.fixtures.exotel_callbacks.test_intake import InMemoryCallEventIntake

_TEST_SECRET = "test-exotel-webhook-secret-value"


def _create_app(
    mapping: dict[str, int] | None = None,
) -> tuple[FastAPI, InMemoryCallEventIntake]:
    app = FastAPI()
    app.include_router(router)
    app.state.exotel_mapping = ExotelNumberMapping(mapping or {"08012345678": 1})
    intake = InMemoryCallEventIntake()
    app.state.exotel_intake = intake
    return app, intake


@pytest.fixture(autouse=True)
def _configure_secret():
    with patch.object(settings, "exotel_webhook_secret", _TEST_SECRET):
        yield


def _auth_headers() -> dict[str, str]:
    return {"X-Exotel-Webhook-Secret": _TEST_SECRET}


# ============================================================================
# Domain: Strict event parsing
# ============================================================================


class TestStrictEventParsing:
    def test_answered_outbound_parses(self) -> None:
        event = parse_exotel_callback(ANSWERED_OUTBOUND)
        assert event.call_sid == "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
        assert event.event_type == "answered"
        assert event.status == "in-progress"
        assert event.direction == "outbound-api"
        assert event.custom_field == "1:corr-001"

    def test_completed_outbound_parses_duration(self) -> None:
        event = parse_exotel_callback(COMPLETED_OUTBOUND)
        assert event.status == "completed"
        assert event.duration == 120
        assert event.conversation_duration == 95

    def test_inbound_completed_parses(self) -> None:
        event = parse_exotel_callback(COMPLETED_INBOUND)
        assert event.duration == 180
        assert event.direction == "inbound"

    def test_missing_optional_fields_accepted(self) -> None:
        event = parse_exotel_callback(MISSING_OPTIONAL_FIELDS)
        assert event.duration is None
        assert event.direction is None

    def test_missing_event_type_inferred_for_terminal(self) -> None:
        event = parse_exotel_callback(MISSING_EVENT_TYPE_TERMINAL)
        assert event.event_type == "terminal"

    def test_missing_event_type_inferred_for_answered(self) -> None:
        event = parse_exotel_callback(MISSING_EVENT_TYPE_ANSWERED)
        assert event.event_type == "answered"

    # --- Strict rejections (not coercion) ---

    def test_negative_duration_rejected(self) -> None:
        with pytest.raises(ExotelCallbackParseError, match="negative Duration"):
            parse_exotel_callback(NEGATIVE_DURATION)

    def test_non_numeric_duration_rejected(self) -> None:
        with pytest.raises(ExotelCallbackParseError, match="invalid Duration"):
            parse_exotel_callback({**COMPLETED_OUTBOUND, "Duration": "abc"})

    def test_invalid_event_type_rejected(self) -> None:
        with pytest.raises(ExotelCallbackParseError, match="invalid EventType"):
            parse_exotel_callback({**COMPLETED_OUTBOUND, "EventType": "ringing"})

    def test_invalid_direction_rejected(self) -> None:
        with pytest.raises(ExotelCallbackParseError, match="invalid Direction"):
            parse_exotel_callback({**COMPLETED_OUTBOUND, "Direction": "sideways"})

    def test_conversation_duration_exceeds_duration_rejected(self) -> None:
        with pytest.raises(ExotelCallbackParseError, match="ConversationDuration"):
            parse_exotel_callback(
                {**COMPLETED_OUTBOUND, "Duration": "60", "ConversationDuration": "90"}
            )

    def test_missing_call_sid_rejected(self) -> None:
        with pytest.raises(ExotelCallbackParseError, match="CallSid"):
            parse_exotel_callback({"Status": "completed", "From": "x", "To": "y"})

    def test_unrecognized_status_rejected(self) -> None:
        sid = "a" * 32
        with pytest.raises(ExotelCallbackParseError, match="unrecognized"):
            parse_exotel_callback({"CallSid": sid, "Status": "unknown", "From": "x", "To": "y"})

    def test_missing_from_rejected(self) -> None:
        sid = "a" * 32
        with pytest.raises(ExotelCallbackParseError, match="From or To"):
            parse_exotel_callback({"CallSid": sid, "Status": "completed", "To": "y"})

    def test_missing_to_rejected(self) -> None:
        sid = "a" * 32
        with pytest.raises(ExotelCallbackParseError, match="From or To"):
            parse_exotel_callback({"CallSid": sid, "Status": "completed", "From": "x"})


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

    def test_terminal_to_non_terminal_is_harmless_noop(self) -> None:
        with pytest.raises(LateCallEventError):
            validate_transition("completed", "in-progress")


# ============================================================================
# Adapter: Auth
# ============================================================================


class TestWebhookAuth:
    def test_valid_secret_accepted(self) -> None:
        app, _ = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status", json=ANSWERED_OUTBOUND, headers=_auth_headers()
        )
        assert response.status_code == 200

    def test_missing_header_returns_401(self) -> None:
        app, _ = _create_app()
        client = TestClient(app)
        response = client.post("/webhooks/exotel/call-status", json=ANSWERED_OUTBOUND)
        assert response.status_code == 401

    def test_wrong_secret_no_intake_call(self) -> None:
        app, intake = _create_app()
        client = TestClient(app)
        client.post(
            "/webhooks/exotel/call-status",
            json=ANSWERED_OUTBOUND,
            headers={"X-Exotel-Webhook-Secret": "wrong-secret-value-padding"},
        )
        assert not intake.persist_called

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
            "/webhooks/exotel/call-status", json=COMPLETED_OUTBOUND, headers=_auth_headers()
        )
        assert response.status_code == 200

    def test_form_urlencoded_rejected(self) -> None:
        app, _ = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status",
            content="CallSid=abc&Status=completed",
            headers={**_auth_headers(), "Content-Type": "application/x-www-form-urlencoded"},
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
        app, intake = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status", json={"irrelevant": "data"}, headers=_auth_headers()
        )
        assert response.status_code == 400
        assert not intake.persist_called

    def test_negative_duration_rejected_at_parse(self) -> None:
        app, intake = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status", json=NEGATIVE_DURATION, headers=_auth_headers()
        )
        assert response.status_code == 400
        assert not intake.persist_called


# ============================================================================
# Adapter: Tenant routing
# ============================================================================


class TestTenantRouting:
    def test_inbound_routes_by_called_number(self) -> None:
        app, intake = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status", json=ANSWERED_INBOUND, headers=_auth_headers()
        )
        assert response.status_code == 200
        assert intake.events[0].business_id == 1

    def test_outbound_routes_by_caller_when_to_unknown(self) -> None:
        app, intake = _create_app({"08012345678": 1})
        client = TestClient(app)
        payload = {**ANSWERED_OUTBOUND, "From": "08012345678", "To": "+919876543210"}
        response = client.post(
            "/webhooks/exotel/call-status", json=payload, headers=_auth_headers()
        )
        assert response.status_code == 200
        assert intake.events[0].business_id == 1

    def test_unknown_both_numbers_returns_404(self) -> None:
        app, _ = _create_app()
        client = TestClient(app)
        payload = {**ANSWERED_OUTBOUND, "To": "09999999999", "From": "+918888888888"}
        response = client.post(
            "/webhooks/exotel/call-status", json=payload, headers=_auth_headers()
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
        assert len(records) >= 1
        serialized = repr(records[0].__dict__)
        assert "09999999999" not in serialized
        assert "+919876543210" not in serialized


# ============================================================================
# Adapter: Intake contract
# ============================================================================


class TestIntakeContract:
    def test_persist_called_before_200(self) -> None:
        app, intake = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status", json=ANSWERED_OUTBOUND, headers=_auth_headers()
        )
        assert response.status_code == 200
        assert intake.persist_called
        assert intake.persist_count == 1

    def test_all_statuses_reach_intake(self) -> None:
        for fixture in (
            ANSWERED_OUTBOUND,
            COMPLETED_OUTBOUND,
            FAILED_OUTBOUND,
            BUSY_OUTBOUND,
            NO_ANSWER_OUTBOUND,
        ):
            app, intake = _create_app()
            client = TestClient(app)
            response = client.post(
                "/webhooks/exotel/call-status", json=fixture, headers=_auth_headers()
            )
            assert response.status_code == 200, f"Failed for {fixture.get('Status')}"
            assert intake.persist_called

    def test_duplicate_callback_returns_200(self) -> None:
        app, intake = _create_app()
        client = TestClient(app)
        client.post("/webhooks/exotel/call-status", json=ANSWERED_OUTBOUND, headers=_auth_headers())
        response = client.post(
            "/webhooks/exotel/call-status", json=ANSWERED_OUTBOUND, headers=_auth_headers()
        )
        assert response.status_code == 200
        assert intake.persist_count == 2  # called twice
        assert len(intake.events) == 1  # stored once

    def test_answered_then_completed_stores_both(self) -> None:
        app, intake = _create_app()
        client = TestClient(app)
        client.post("/webhooks/exotel/call-status", json=ANSWERED_OUTBOUND, headers=_auth_headers())
        client.post(
            "/webhooks/exotel/call-status", json=COMPLETED_OUTBOUND, headers=_auth_headers()
        )
        assert len(intake.events) == 2
        assert intake.events[0].event_type == "answered"
        assert intake.events[1].event_type == "terminal"

    def test_intake_not_configured_returns_503(self) -> None:
        app = FastAPI()
        app.include_router(router)
        app.state.exotel_mapping = ExotelNumberMapping({"08012345678": 1})
        # no exotel_intake set
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status", json=ANSWERED_OUTBOUND, headers=_auth_headers()
        )
        assert response.status_code == 503

    def test_intake_failure_returns_500(self) -> None:
        app, intake = _create_app()

        async def _failing_persist(*a, **kw):  # type: ignore[no-untyped-def]
            raise RuntimeError("db down")

        intake.persist = _failing_persist  # type: ignore[assignment]
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status", json=ANSWERED_OUTBOUND, headers=_auth_headers()
        )
        assert response.status_code == 500

    def test_event_record_has_correct_fields(self) -> None:
        app, intake = _create_app()
        client = TestClient(app)
        client.post(
            "/webhooks/exotel/call-status", json=COMPLETED_OUTBOUND, headers=_auth_headers()
        )
        record = intake.events[0]
        assert record.business_id == 1
        assert record.call_sid == COMPLETED_OUTBOUND["CallSid"]
        assert record.event_type == "terminal"
        assert record.status == "completed"
        assert record.duration == 120
        assert record.payload_digest  # non-empty hash


# ============================================================================
# Adapter: Privacy
# ============================================================================


class TestPrivacy:
    def test_success_log_has_no_phone(self, caplog) -> None:  # type: ignore[no-untyped-def]
        app, _ = _create_app()
        client = TestClient(app)
        with caplog.at_level("INFO", logger="fonely.api.channels.exotel"):
            client.post(
                "/webhooks/exotel/call-status", json=COMPLETED_OUTBOUND, headers=_auth_headers()
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
# Feature gate + mapping
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


class TestNumberMapping:
    def test_known_number_returns_business_id(self) -> None:
        mapping = ExotelNumberMapping({"08012345678": 1, "08087654321": 2})
        assert mapping.get_business_id("08012345678") == 1

    def test_unknown_number_returns_none(self) -> None:
        mapping = ExotelNumberMapping({"08012345678": 1})
        assert mapping.get_business_id("09999999999") is None


# ============================================================================
# Adapter: Semantic idempotency and OOO through full stack
# ============================================================================


class TestSemanticIdempotency:
    def test_terminal_before_answered_stores_terminal_only(self) -> None:
        """OOO: terminal arrives first (answered lost or delayed)."""
        app, intake = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status", json=COMPLETED_OUTBOUND, headers=_auth_headers()
        )
        assert response.status_code == 200
        assert len(intake.events) == 1
        assert intake.events[0].status == "completed"

    def test_second_terminal_different_status_is_conflict_409(self) -> None:
        """Second terminal callback with different status for same CallSid
        has a different digest — ConflictingCallEventError → 409."""
        app, intake = _create_app()
        client = TestClient(app)
        client.post(
            "/webhooks/exotel/call-status", json=COMPLETED_OUTBOUND, headers=_auth_headers()
        )
        failed_same_sid = {**FAILED_OUTBOUND, "CallSid": COMPLETED_OUTBOUND["CallSid"]}
        response = client.post(
            "/webhooks/exotel/call-status", json=failed_same_sid, headers=_auth_headers()
        )
        assert response.status_code == 409
        assert len(intake.events) == 1
        assert intake.events[0].status == "completed"

    def test_same_terminal_callback_twice_is_idempotent_200(self) -> None:
        """Exact duplicate terminal callback — 200, no second store."""
        app, intake = _create_app()
        client = TestClient(app)
        client.post(
            "/webhooks/exotel/call-status", json=COMPLETED_OUTBOUND, headers=_auth_headers()
        )
        response = client.post(
            "/webhooks/exotel/call-status", json=COMPLETED_OUTBOUND, headers=_auth_headers()
        )
        assert response.status_code == 200
        assert len(intake.events) == 1

    def test_different_call_sids_independent(self) -> None:
        """Two different calls with independent state."""
        app, intake = _create_app()
        client = TestClient(app)
        client.post(
            "/webhooks/exotel/call-status", json=COMPLETED_OUTBOUND, headers=_auth_headers()
        )
        client.post("/webhooks/exotel/call-status", json=FAILED_OUTBOUND, headers=_auth_headers())
        assert len(intake.events) == 2
        sids = {e.call_sid for e in intake.events}
        assert len(sids) == 2

    def test_full_lifecycle_answered_then_completed(self) -> None:
        """Normal lifecycle: answered → completed for same CallSid."""
        app, intake = _create_app()
        client = TestClient(app)
        r1 = client.post(
            "/webhooks/exotel/call-status", json=ANSWERED_OUTBOUND, headers=_auth_headers()
        )
        r2 = client.post(
            "/webhooks/exotel/call-status", json=COMPLETED_OUTBOUND, headers=_auth_headers()
        )
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert len(intake.events) == 2
        assert intake.events[0].status == "in-progress"
        assert intake.events[1].status == "completed"

    def test_payload_digest_differs_for_different_events(self) -> None:
        """Each persisted record has a unique payload digest."""
        app, intake = _create_app()
        client = TestClient(app)
        client.post("/webhooks/exotel/call-status", json=ANSWERED_OUTBOUND, headers=_auth_headers())
        client.post(
            "/webhooks/exotel/call-status", json=COMPLETED_OUTBOUND, headers=_auth_headers()
        )
        digests = {e.payload_digest for e in intake.events}
        assert len(digests) == 2

    def test_late_answered_after_completed_is_harmless_200(self) -> None:
        """Late lower-state after terminal is a harmless no-op, not 500."""
        app, intake = _create_app()
        client = TestClient(app)
        client.post(
            "/webhooks/exotel/call-status", json=COMPLETED_OUTBOUND, headers=_auth_headers()
        )
        late_answered = {**ANSWERED_OUTBOUND, "CallSid": COMPLETED_OUTBOUND["CallSid"]}
        response = client.post(
            "/webhooks/exotel/call-status", json=late_answered, headers=_auth_headers()
        )
        assert response.status_code == 200
        assert len(intake.events) == 1


# ============================================================================
# Domain: Transition matrix exhaustive
# ============================================================================


class TestTransitionMatrixExhaustive:
    def test_queued_to_in_progress(self) -> None:
        assert validate_transition("queued", "in-progress") == "in-progress"

    def test_queued_to_completed(self) -> None:
        assert validate_transition("queued", "completed") == "completed"

    def test_queued_to_failed(self) -> None:
        assert validate_transition("queued", "failed") == "failed"

    def test_in_progress_to_busy_allowed(self) -> None:
        assert validate_transition("in-progress", "busy") == "busy"

    def test_in_progress_to_no_answer_allowed(self) -> None:
        assert validate_transition("in-progress", "no-answer") == "no-answer"

    def test_failed_to_completed_raises(self) -> None:
        with pytest.raises(InvalidCallTransitionError):
            validate_transition("failed", "completed")

    def test_busy_to_no_answer_raises(self) -> None:
        with pytest.raises(InvalidCallTransitionError):
            validate_transition("busy", "no-answer")

    def test_no_answer_to_in_progress_is_harmless_noop(self) -> None:
        with pytest.raises(LateCallEventError):
            validate_transition("no-answer", "in-progress")

    def test_every_terminal_is_idempotent(self) -> None:
        for status in ("completed", "failed", "busy", "no-answer"):
            assert validate_transition(status, status) == status


class TestStrictIdValidation:
    def test_boolean_call_sid_rejected(self) -> None:
        with pytest.raises(ExotelCallbackParseError, match="CallSid must be string"):
            parse_exotel_callback({"CallSid": True, "Status": "completed", "From": "x", "To": "y"})

    def test_structured_call_sid_rejected(self) -> None:
        with pytest.raises(ExotelCallbackParseError, match="CallSid must be string"):
            parse_exotel_callback(
                {"CallSid": {"nested": "obj"}, "Status": "completed", "From": "x", "To": "y"}
            )

    def test_short_call_sid_rejected(self) -> None:
        with pytest.raises(ExotelCallbackParseError, match="invalid CallSid format"):
            parse_exotel_callback(
                {"CallSid": "short", "Status": "completed", "From": "x", "To": "y"}
            )

    def test_boolean_duration_rejected(self) -> None:
        with pytest.raises(ExotelCallbackParseError, match="boolean"):
            parse_exotel_callback({**COMPLETED_OUTBOUND, "Duration": True})

    def test_float_duration_rejected(self) -> None:
        with pytest.raises(ExotelCallbackParseError, match="float"):
            parse_exotel_callback({**COMPLETED_OUTBOUND, "Duration": 3.14})

    def test_inf_duration_rejected(self) -> None:
        with pytest.raises(ExotelCallbackParseError, match="float"):
            parse_exotel_callback({**COMPLETED_OUTBOUND, "Duration": float("inf")})

    def test_event_type_terminal_with_in_progress_rejected(self) -> None:
        with pytest.raises(ExotelCallbackParseError, match="inconsistent"):
            parse_exotel_callback({**ANSWERED_OUTBOUND, "EventType": "terminal"})

    def test_event_type_answered_with_completed_rejected(self) -> None:
        with pytest.raises(ExotelCallbackParseError, match="inconsistent"):
            parse_exotel_callback({**COMPLETED_OUTBOUND, "EventType": "answered"})


class TestConflictDetection:
    def test_exact_duplicate_returns_200(self) -> None:
        app, intake = _create_app()
        client = TestClient(app)
        client.post(
            "/webhooks/exotel/call-status", json=COMPLETED_OUTBOUND, headers=_auth_headers()
        )
        response = client.post(
            "/webhooks/exotel/call-status", json=COMPLETED_OUTBOUND, headers=_auth_headers()
        )
        assert response.status_code == 200
        assert len(intake.events) == 1

    def test_conflicting_terminal_returns_409(self) -> None:
        app, intake = _create_app()
        client = TestClient(app)
        client.post(
            "/webhooks/exotel/call-status", json=COMPLETED_OUTBOUND, headers=_auth_headers()
        )
        modified = {**COMPLETED_OUTBOUND, "Duration": "999"}
        response = client.post(
            "/webhooks/exotel/call-status", json=modified, headers=_auth_headers()
        )
        assert response.status_code == 409
        assert len(intake.events) == 1


class TestAmbiguityAwareRouting:
    def test_to_mapped_routes_correctly(self) -> None:
        app, intake = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status", json=ANSWERED_INBOUND, headers=_auth_headers()
        )
        assert response.status_code == 200
        assert intake.events[0].business_id == 1

    def test_from_mapped_routes_correctly(self) -> None:
        app, intake = _create_app({"08012345678": 1})
        client = TestClient(app)
        payload = {**ANSWERED_OUTBOUND, "From": "08012345678", "To": "+919876543210"}
        response = client.post(
            "/webhooks/exotel/call-status", json=payload, headers=_auth_headers()
        )
        assert response.status_code == 200
        assert intake.events[0].business_id == 1

    def test_both_mapped_same_business_accepted(self) -> None:
        app, _intake = _create_app({"08012345678": 1, "+919876543210": 1})
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status", json=ANSWERED_OUTBOUND, headers=_auth_headers()
        )
        assert response.status_code == 200

    def test_both_mapped_different_business_rejected(self) -> None:
        app, _ = _create_app({"08012345678": 1, "+919876543210": 2})
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status", json=ANSWERED_OUTBOUND, headers=_auth_headers()
        )
        assert response.status_code == 404
