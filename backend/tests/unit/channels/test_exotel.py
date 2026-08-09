"""Unit tests for Exotel telephony adapter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from fonely.api.channels.exotel import _read_bounded_body, call_status_webhook, router
from fonely.app import create_app
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


def _request(
    app: FastAPI,
    receive: AsyncMock,
    *,
    secret: str,
    content_length: int | None = None,
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    headers = [
        (b"content-type", b"application/json"),
        (b"x-exotel-webhook-secret", secret.encode("latin-1")),
    ]
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode("ascii")))
    if extra_headers:
        headers.extend(extra_headers)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/webhooks/exotel/call-status",
        "headers": headers,
        "app": app,
    }
    return Request(scope, receive)


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

    def test_short_config_secret_fails_closed(self) -> None:
        with patch.object(settings, "exotel_webhook_secret", "too-short"):
            app, _ = _create_app()
            client = TestClient(app)
            response = client.post(
                "/webhooks/exotel/call-status",
                json=_ringing_body(),
                headers={"X-Exotel-Webhook-Secret": "too-short"},
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
        encoded = _TEST_SECRET.encode("ascii")
        mock_cmp.assert_called_once_with(encoded, encoded)

    async def test_non_ascii_presented_secret_returns_401_without_500(self) -> None:
        app, mock_session = _create_app()
        receive = AsyncMock(
            side_effect=[{"type": "http.request", "body": b"{}", "more_body": False}]
        )
        request = _request(app, receive, secret="é" * 32)
        response = await call_status_webhook(request)
        assert response.status_code == 401
        receive.assert_not_awaited()
        mock_session.execute.assert_not_awaited()

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

    async def test_unauthorized_request_does_not_consume_body(self) -> None:
        app, mock_session = _create_app()
        receive = AsyncMock(side_effect=AssertionError("unauthorized body must not be consumed"))
        request = _request(app, receive, secret="wrong")
        response = await call_status_webhook(request)
        assert response.status_code == 401
        receive.assert_not_awaited()
        mock_session.execute.assert_not_awaited()

    async def test_chunked_oversize_rejected_before_buffering_all_chunks(self) -> None:
        app, mock_session = _create_app()
        chunk = b"x" * 40_000
        receive = AsyncMock(
            side_effect=[
                {"type": "http.request", "body": chunk, "more_body": True},
                {"type": "http.request", "body": chunk, "more_body": True},
                AssertionError("third chunk must not be consumed"),
            ]
        )
        request = _request(app, receive, secret=_TEST_SECRET)
        response = await call_status_webhook(request)
        assert response.status_code == 413
        assert receive.await_count == 2
        mock_session.execute.assert_not_awaited()

    async def test_single_multimegabyte_chunk_rejected_after_one_receive(self) -> None:
        app, mock_session = _create_app()
        receive = AsyncMock(
            side_effect=[
                {
                    "type": "http.request",
                    "body": b"x" * 2_000_000,
                    "more_body": True,
                },
                AssertionError("second receive must not occur"),
            ]
        )
        request = _request(app, receive, secret=_TEST_SECRET)
        response = await call_status_webhook(request)
        assert response.status_code == 413
        assert receive.await_count == 1
        mock_session.execute.assert_not_awaited()

    async def test_exact_boundary_is_accepted_by_bounded_reader(self) -> None:
        receive = AsyncMock(
            side_effect=[
                {
                    "type": "http.request",
                    "body": b"x" * 65_536,
                    "more_body": False,
                }
            ]
        )
        request = _request(_create_app()[0], receive, secret=_TEST_SECRET)
        body = await _read_bounded_body(request)
        assert body is not None
        assert len(body) == 65_536
        assert receive.await_count == 1

    async def test_boundary_plus_one_is_rejected_after_one_receive(self) -> None:
        app, mock_session = _create_app()
        receive = AsyncMock(
            side_effect=[
                {
                    "type": "http.request",
                    "body": b"x" * 65_537,
                    "more_body": False,
                }
            ]
        )
        request = _request(app, receive, secret=_TEST_SECRET)
        response = await call_status_webhook(request)
        assert response.status_code == 413
        assert receive.await_count == 1
        mock_session.execute.assert_not_awaited()

    async def test_client_disconnect_fails_safely_without_db(self) -> None:
        app, mock_session = _create_app()
        receive = AsyncMock(side_effect=[{"type": "http.disconnect"}])
        request = _request(app, receive, secret=_TEST_SECRET)
        response = await call_status_webhook(request)
        assert response.status_code == 413
        mock_session.execute.assert_not_awaited()


class TestProductionRouteMounting:
    def test_empty_config_does_not_mount_exotel_routes(self) -> None:
        with patch("fonely.app.settings") as mock_settings:
            mock_settings.internal_api_secret = ""
            mock_settings.whatsapp_verify_token = ""
            mock_settings.exotel_webhook_secret = ""
            app = create_app()
        paths = {route.path for route in app.routes}
        assert "/webhooks/exotel/call-status" not in paths
        assert "/webhooks/exotel/audio-stream" not in paths

    async def test_short_config_unmounts_route_and_fails_readiness(self) -> None:
        with patch("fonely.app.settings") as mock_settings:
            mock_settings.internal_api_secret = ""
            mock_settings.whatsapp_verify_token = ""
            mock_settings.exotel_webhook_secret = "short"
            mock_settings.readiness_timeout_seconds = 3.0
            app = create_app()
        paths = {route.path for route in app.routes}
        assert "/webhooks/exotel/call-status" not in paths
        assert app.state.exotel_webhook_auth_ready is False

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health/ready")
        assert response.status_code == 503
        assert response.json() == {"status": "unavailable"}

    async def test_non_ascii_config_unmounts_route_and_fails_readiness(self) -> None:
        with patch("fonely.app.settings") as mock_settings:
            mock_settings.internal_api_secret = ""
            mock_settings.whatsapp_verify_token = ""
            mock_settings.exotel_webhook_secret = "é" * 32
            mock_settings.readiness_timeout_seconds = 3.0
            app = create_app()
        paths = {route.path for route in app.routes}
        assert "/webhooks/exotel/call-status" not in paths
        assert app.state.exotel_webhook_auth_ready is False

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health/ready")
        assert response.status_code == 503
        assert response.json() == {"status": "unavailable"}


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

    def test_unknown_number_log_has_no_phone_or_call_sid(self, caplog) -> None:  # type: ignore[no-untyped-def]
        app, _ = _create_app()
        client = TestClient(app)
        with caplog.at_level("WARNING", logger="fonely.api.channels.exotel"):
            response = client.post(
                "/webhooks/exotel/call-status",
                json={
                    "CallSid": "fictional-sensitive-call-id",
                    "Status": "ringing",
                    "To": "09999999999",
                    "From": "+919876543210",
                },
                headers=_auth_headers(),
            )
        assert response.status_code == 404
        sensitive = (
            "09999999999",
            "+919876543210",
            "fictional-sensitive-call-id",
            _TEST_SECRET,
        )
        records = [
            record for record in caplog.records if record.name == "fonely.api.channels.exotel"
        ]
        assert len(records) == 1
        serialized = repr(records[0].__dict__)
        assert all(value not in serialized for value in sensitive)

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
