# ruff: noqa: SIM117
"""Production app composition tests — Exotel routes through create_app.

Proves routes are mounted/absent through the real production create_app()
path, not manual test-app construction.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from fonely.app import create_app


def _exotel_settings(**overrides):
    """Patch settings for Exotel-enabled app."""
    defaults = {
        "internal_api_secret": "",
        "whatsapp_verify_token": "",
        "exotel_webhook_secret": "strong-production-secret-at-least-32-chars",
        "exotel_number_mappings": '{"08012345678": 1}',
        "exotel_sid": "AC_production_test",
        "debug": True,
        "host": "0.0.0.0",
        "port": 8000,
        "log_format": "json",
        "log_level": "INFO",
        "database_url": "sqlite+aiosqlite://",
        "db_pool_size": 1,
        "db_max_overflow": 0,
        "db_pool_timeout": 5,
        "db_pool_recycle": 300,
        "sarvam_api_key": "",
        "readiness_timeout_seconds": 5,
    }
    defaults.update(overrides)
    return defaults


class TestExotelAppComposition:
    def test_callback_route_mounted_with_complete_config(self) -> None:
        with patch("fonely.app.settings") as ms:
            for k, v in _exotel_settings().items():
                setattr(ms, k, v)
            app = create_app()
        with TestClient(app) as client:
            response = client.post(
                "/webhooks/exotel/call-status",
                json={
                    "CallSid": "a" * 32,
                    "EventType": "terminal",
                    "Status": "completed",
                    "From": "+919000000001",
                    "To": "08012345678",
                    "Duration": "60",
                },
                headers={"X-Exotel-Webhook-Secret": "strong-production-secret-at-least-32-chars"},
            )
        assert response.status_code in (200, 503)

    def test_media_route_mounted_with_complete_config(self) -> None:
        with patch("fonely.app.settings") as ms:
            for k, v in _exotel_settings().items():
                setattr(ms, k, v)
            app = create_app()
        with TestClient(app) as client:
            with pytest.raises(WebSocketDisconnect) as exc:
                with client.websocket_connect("/webhooks/exotel/media"):
                    pass
            assert exc.value.code in (4401, 1013)

    def test_routes_absent_without_secret(self) -> None:
        with patch("fonely.app.settings") as ms:
            for k, v in _exotel_settings(exotel_webhook_secret="").items():
                setattr(ms, k, v)
            app = create_app()
        with TestClient(app) as client:
            response = client.post("/webhooks/exotel/call-status")
        assert response.status_code in (404, 405)

    def test_routes_absent_with_weak_secret(self) -> None:
        with patch("fonely.app.settings") as ms:
            for k, v in _exotel_settings(exotel_webhook_secret="short").items():
                setattr(ms, k, v)
            app = create_app()
        with TestClient(app) as client:
            response = client.post("/webhooks/exotel/call-status")
        assert response.status_code in (404, 405)

    def test_routes_absent_without_mappings(self) -> None:
        with patch("fonely.app.settings") as ms:
            for k, v in _exotel_settings(exotel_number_mappings="").items():
                setattr(ms, k, v)
            app = create_app()
        with TestClient(app) as client:
            response = client.post("/webhooks/exotel/call-status")
        assert response.status_code in (404, 405)

    def test_routes_absent_without_account_sid(self) -> None:
        with patch("fonely.app.settings") as ms:
            for k, v in _exotel_settings(exotel_sid="").items():
                setattr(ms, k, v)
            app = create_app()
        with TestClient(app) as client:
            response = client.post("/webhooks/exotel/call-status")
        assert response.status_code in (404, 405)

    def test_invalid_mappings_keeps_routes_absent(self) -> None:
        with patch("fonely.app.settings") as ms:
            for k, v in _exotel_settings(exotel_number_mappings="not json").items():
                setattr(ms, k, v)
            app = create_app()
        with TestClient(app) as client:
            response = client.post("/webhooks/exotel/call-status")
        assert response.status_code in (404, 405)


class TestDirectionAwareTenantRouting:
    def test_inbound_resolves_by_to_number(self) -> None:
        from fonely.api.channels.exotel_admission import resolve_business_id
        from fonely.services.exotel_config import ExotelNumberMapping

        mapping = ExotelNumberMapping({"08012345678": 1})
        assert resolve_business_id(mapping, "08012345678", "+919876543210", "inbound") == 1

    def test_inbound_rejects_caller_as_tenant(self) -> None:
        from fonely.api.channels.exotel_admission import resolve_business_id
        from fonely.services.exotel_config import ExotelNumberMapping

        mapping = ExotelNumberMapping({"08012345678": 1})
        assert resolve_business_id(mapping, "+919876543210", "08012345678", "inbound") is None

    def test_outbound_resolves_by_from_number(self) -> None:
        from fonely.api.channels.exotel_admission import resolve_business_id
        from fonely.services.exotel_config import ExotelNumberMapping

        mapping = ExotelNumberMapping({"08012345678": 1})
        assert resolve_business_id(mapping, "+919876543210", "08012345678", "outbound-api") == 1

    def test_cross_tenant_spoof_rejected(self) -> None:
        """Caller number equals another tenant's virtual number — rejected."""
        from fonely.api.channels.exotel_admission import resolve_business_id
        from fonely.services.exotel_config import ExotelNumberMapping

        mapping = ExotelNumberMapping({"08012345678": 1, "08087654321": 2})
        assert resolve_business_id(mapping, "08012345678", "08087654321") is None

    def test_unknown_direction_ambiguous_rejected(self) -> None:
        from fonely.api.channels.exotel_admission import resolve_business_id
        from fonely.services.exotel_config import ExotelNumberMapping

        mapping = ExotelNumberMapping({"08012345678": 1, "08087654321": 2})
        assert resolve_business_id(mapping, "08012345678", "08087654321", None) is None


class TestSampleRateValidation:
    def test_float_rate_rejected(self) -> None:
        from fonely.api.channels.exotel_stream import (
            ExotelStartValidationError,
            validate_start_event,
        )

        msg = {
            "event": "start",
            "start": {
                "stream_sid": "MZ",
                "call_sid": "CA",
                "account_sid": "AC",
                "from": "+919000000001",
                "to": "08012345678",
                "media_format": {
                    "encoding": "audio/x-raw",
                    "sample_rate": 16000.5,
                },
            },
        }
        with pytest.raises(ExotelStartValidationError, match="float"):
            validate_start_event(msg)
