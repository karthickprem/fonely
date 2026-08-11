"""Unit tests for Exotel telephony adapter."""

from __future__ import annotations

import base64
from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from fonely.api.channels.exotel import router
from fonely.services.exotel_config import ExotelNumberMapping

_SECRET = "exotel-test-secret"


@pytest.fixture(autouse=True)
def exotel_settings() -> Iterator[MagicMock]:
    """Give every test a configured secret.

    Autouse because the secret is now load-bearing for both entry points --
    without it every request is a 503, and a test that forgot to set it would
    pass its status assertion for entirely the wrong reason. Tests that need
    it absent set it to "" on the yielded mock.
    """
    with patch("fonely.api.channels.exotel.settings") as mock_settings:
        mock_settings.exotel_webhook_secret = _SECRET
        yield mock_settings


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


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {_SECRET}"}


def _basic(username: str, password: str) -> dict[str, str]:
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {encoded}"}


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
            headers=_auth(),
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
            headers=_auth(),
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
            headers=_auth(),
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
            headers=_auth(),
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
            headers=_auth(),
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
            headers=_auth(),
        )
        assert response.status_code == 200


class TestCallStatusBodyEncoding:
    """Exotel posts form fields, not JSON.

    The handler only ever called request.json(), so a genuine callback would
    have raised before reaching any logic. Every pre-existing test posted
    JSON, which is why nothing caught it.
    """

    def test_form_encoded_callback_is_accepted(self) -> None:
        app = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status",
            data={
                "CallSid": "call-123",
                "Status": "ringing",
                "To": "08012345678",
                "From": "+919876543210",
            },
            headers=_auth(),
        )
        assert response.status_code == 200
        app.state._mock_session.commit.assert_awaited()

    def test_form_encoded_completed_updates_call(self) -> None:
        app = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status",
            data={
                "CallSid": "call-123",
                "Status": "completed",
                "To": "08012345678",
                "From": "+919876543210",
                "Duration": "120",
            },
            headers=_auth(),
        )
        assert response.status_code == 200

    def test_unparseable_body_is_a_400_not_a_500(self) -> None:
        app = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status",
            content=b"\x00\x01not-a-body",
            headers={"Content-Type": "application/json", **_auth()},
        )
        assert response.status_code == 400


class TestCallStatusAuthentication:
    """The route writes `calls` rows for a tenant, so it must not be open.

    Every case asserts the database was never touched as well as the status
    code -- a 401 returned after the insert would be worthless.
    """

    def _post(self, app: FastAPI, **kwargs: object) -> object:
        client = TestClient(app)
        return client.post(
            "/webhooks/exotel/call-status",
            json={
                "CallSid": "call-123",
                "Status": "ringing",
                "To": "08012345678",
                "From": "+919876543210",
            },
            **kwargs,  # type: ignore[arg-type]
        )

    def test_no_credential_is_rejected(self) -> None:
        app = _create_app()
        response = self._post(app)
        assert response.status_code == 401  # type: ignore[attr-defined]
        app.state._mock_session.execute.assert_not_awaited()

    def test_wrong_bearer_is_rejected(self) -> None:
        app = _create_app()
        response = self._post(app, headers={"Authorization": "Bearer not-the-secret"})
        assert response.status_code == 401  # type: ignore[attr-defined]
        app.state._mock_session.execute.assert_not_awaited()

    def test_basic_password_is_accepted(self) -> None:
        """Credentials embedded in the callback URL — Exotel's documented way."""
        app = _create_app()
        response = self._post(app, headers=_basic("exotel", _SECRET))
        assert response.status_code == 200  # type: ignore[attr-defined]

    def test_basic_whole_pair_is_accepted(self) -> None:
        app = _create_app()
        encoded = base64.b64encode(_SECRET.encode()).decode()
        response = self._post(app, headers={"Authorization": f"Basic {encoded}"})
        assert response.status_code == 200  # type: ignore[attr-defined]

    def test_basic_wrong_password_is_rejected(self) -> None:
        app = _create_app()
        response = self._post(app, headers=_basic("exotel", "wrong"))
        assert response.status_code == 401  # type: ignore[attr-defined]
        app.state._mock_session.execute.assert_not_awaited()

    def test_query_token_is_accepted(self) -> None:
        app = _create_app()
        client = TestClient(app)
        response = client.post(
            f"/webhooks/exotel/call-status?token={_SECRET}",
            json={
                "CallSid": "call-123",
                "Status": "ringing",
                "To": "08012345678",
                "From": "+919876543210",
            },
        )
        assert response.status_code == 200

    def test_wrong_query_token_is_rejected(self) -> None:
        app = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status?token=nope",
            json={"CallSid": "call-123", "Status": "ringing", "To": "08012345678"},
        )
        assert response.status_code == 401
        app.state._mock_session.execute.assert_not_awaited()

    @pytest.mark.parametrize(
        "header",
        [
            "Basic not-valid-base64!!",
            "Basic " + base64.b64encode(b"\xff\xfe").decode(),
            "Bearer",
            "Basic",
            "Digest something",
            "",
        ],
    )
    def test_malformed_credentials_are_rejected_not_crashed(self, header: str) -> None:
        app = _create_app()
        response = self._post(app, headers={"Authorization": header})
        assert response.status_code == 401  # type: ignore[attr-defined]

    def test_non_ascii_credential_is_rejected_not_crashed(self) -> None:
        """compare_digest raises on non-ASCII str; a 500 here would leak shape."""
        app = _create_app()
        client = TestClient(app)
        response = client.post(
            "/webhooks/exotel/call-status?token=கா",
            json={"CallSid": "call-123", "Status": "ringing", "To": "08012345678"},
        )
        assert response.status_code == 401

    def test_unconfigured_secret_returns_503_and_rejects(self, exotel_settings: MagicMock) -> None:
        exotel_settings.exotel_webhook_secret = ""
        app = _create_app()
        response = self._post(app, headers=_auth())
        assert response.status_code == 503  # type: ignore[attr-defined]
        app.state._mock_session.execute.assert_not_awaited()

    def test_empty_secret_does_not_admit_empty_credential(self, exotel_settings: MagicMock) -> None:
        """An unset secret must never make an empty credential valid."""
        exotel_settings.exotel_webhook_secret = ""
        app = _create_app()
        response = self._post(app, headers={"Authorization": "Bearer "})
        assert response.status_code == 503  # type: ignore[attr-defined]
        app.state._mock_session.execute.assert_not_awaited()


class TestAudioStreamWebSocket:
    def test_connects_with_valid_credential(self) -> None:
        app = _create_app()
        client = TestClient(app)
        with client.websocket_connect("/webhooks/exotel/audio-stream", headers=_auth()) as ws:
            ws.send_bytes(b"\x00" * 320)
            ws.close()

    def test_connects_with_query_token(self) -> None:
        """The media-stream applet takes a URL, so this is often the only carrier."""
        app = _create_app()
        client = TestClient(app)
        with client.websocket_connect(f"/webhooks/exotel/audio-stream?token={_SECRET}") as ws:
            ws.send_bytes(b"\x00" * 320)
            ws.close()

    def test_unauthenticated_connection_is_refused(self) -> None:
        app = _create_app()
        client = TestClient(app)
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect("/webhooks/exotel/audio-stream") as ws,
        ):
            ws.send_bytes(b"\x00" * 320)

    def test_wrong_credential_connection_is_refused(self) -> None:
        app = _create_app()
        client = TestClient(app)
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect("/webhooks/exotel/audio-stream?token=wrong") as ws,
        ):
            ws.send_bytes(b"\x00" * 320)

    def test_refused_before_handshake_completes(self) -> None:
        """Rejection must precede accept().

        Accepting and then closing would confirm the endpoint to an
        unauthenticated caller and hand them a live socket for the duration
        of the close. Starlette surfaces a pre-accept close as a rejection at
        connect time, which is what this asserts.
        """
        app = _create_app()
        client = TestClient(app)
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect("/webhooks/exotel/audio-stream"),
        ):
            pass

    def test_unconfigured_secret_refuses_connection(self, exotel_settings: MagicMock) -> None:
        exotel_settings.exotel_webhook_secret = ""
        app = _create_app()
        client = TestClient(app)
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect("/webhooks/exotel/audio-stream"),
        ):
            pass


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
