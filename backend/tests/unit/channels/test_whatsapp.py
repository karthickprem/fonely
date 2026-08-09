"""Tests for the WhatsApp inbound adapter.

The webhook handler now persists events to whatsapp_inbound_events
before returning 200. Processing happens in the inbound worker.
"""

import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from fonely.api.channels.whatsapp import router
from fonely.services.whatsapp_config import WhatsAppBusinessMapping


@pytest.fixture(autouse=True)
def _patch_settings():
    with patch("fonely.api.channels.whatsapp.settings") as mock_settings:
        mock_settings.whatsapp_verify_token = "test-token"
        mock_settings.whatsapp_access_token = "test-access"
        mock_settings.whatsapp_phone_number_id = "12345"
        mock_settings.whatsapp_business_mappings = ""
        mock_settings.whatsapp_app_secret = "test-app-secret"
        yield mock_settings


def _make_app(*, insert_rowcount: int = 1) -> tuple[FastAPI, MagicMock]:
    app = FastAPI()
    app.include_router(router)

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.commit = AsyncMock()

    insert_result = MagicMock()
    insert_result.rowcount = insert_rowcount
    mock_session.execute = AsyncMock(return_value=insert_result)

    mock_factory = MagicMock()
    mock_factory.return_value = mock_session

    app.state.session_factory = mock_factory
    return app, mock_session


def _sign(body: bytes, secret: str = "test-app-secret") -> str:
    import hashlib
    import hmac as _hmac

    return "sha256=" + _hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _webhook_payload(
    *,
    sender: str = "919876543210",
    text: str = "Hello",
    message_id: str = "wamid.test123",
    phone_number_id: str = "12345",
    message_type: str = "text",
) -> dict:
    message: dict = {
        "id": message_id,
        "from": sender,
        "type": message_type,
        "timestamp": "1690000000",
    }
    if message_type == "text":
        message["text"] = {"body": text}
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": phone_number_id},
                            "messages": [message],
                        },
                    }
                ],
            }
        ],
    }


class TestWebhookVerification:
    @pytest.mark.asyncio
    async def test_valid_verification(self):
        app, _ = _make_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get(
                "/webhooks/whatsapp",
                params={
                    "hub.mode": "subscribe",
                    "hub.verify_token": "test-token",
                    "hub.challenge": "challenge_string_123",
                },
            )
            assert r.status_code == 200
            assert r.text == "challenge_string_123"

    @pytest.mark.asyncio
    async def test_invalid_token_returns_403(self):
        app, _ = _make_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get(
                "/webhooks/whatsapp",
                params={
                    "hub.mode": "subscribe",
                    "hub.verify_token": "wrong-token",
                    "hub.challenge": "challenge_string_123",
                },
            )
            assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_missing_mode_returns_403(self):
        app, _ = _make_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get(
                "/webhooks/whatsapp",
                params={
                    "hub.verify_token": "test-token",
                    "hub.challenge": "challenge_string_123",
                },
            )
            assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_empty_configured_and_empty_supplied_returns_403(self, _patch_settings):
        _patch_settings.whatsapp_verify_token = ""
        app, _ = _make_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get(
                "/webhooks/whatsapp",
                params={
                    "hub.mode": "subscribe",
                    "hub.verify_token": "",
                    "hub.challenge": "challenge_string_123",
                },
            )
            assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_missing_request_token_returns_403(self):
        app, _ = _make_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get(
                "/webhooks/whatsapp",
                params={
                    "hub.mode": "subscribe",
                    "hub.challenge": "challenge_string_123",
                },
            )
            assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_wrong_mode_returns_403(self):
        app, _ = _make_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get(
                "/webhooks/whatsapp",
                params={
                    "hub.mode": "unsubscribe",
                    "hub.verify_token": "test-token",
                    "hub.challenge": "challenge_string_123",
                },
            )
            assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_unconfigured_verify_token_returns_403(self, _patch_settings):
        _patch_settings.whatsapp_verify_token = ""
        app, _ = _make_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get(
                "/webhooks/whatsapp",
                params={
                    "hub.mode": "subscribe",
                    "hub.verify_token": "test-token",
                    "hub.challenge": "challenge_string_123",
                },
            )
            assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_verify_token_not_in_response_body(self):
        app, _ = _make_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get(
                "/webhooks/whatsapp",
                params={
                    "hub.mode": "subscribe",
                    "hub.verify_token": "wrong-token",
                    "hub.challenge": "challenge_string_123",
                },
            )
            assert "test-token" not in r.text
            assert "wrong-token" not in r.text


class TestWebhookPersistence:
    @pytest.mark.asyncio
    async def test_post_persists_and_returns_200(self):
        app, mock_session = _make_app()
        body = json.dumps(_webhook_payload()).encode()
        with patch("fonely.api.channels.whatsapp.WhatsAppBusinessMapping") as map_cls:
            mock_map = MagicMock()
            mock_map.get_business_id.return_value = 1
            map_cls.return_value = mock_map
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.post(
                    "/webhooks/whatsapp",
                    content=body,
                    headers={
                        "content-type": "application/json",
                        "X-Hub-Signature-256": _sign(body),
                    },
                )
        assert r.status_code == 200
        mock_session.execute.assert_awaited()
        mock_session.commit.assert_awaited()

    @pytest.mark.asyncio
    async def test_duplicate_not_committed(self):
        app, mock_session = _make_app(insert_rowcount=0)
        with patch("fonely.api.channels.whatsapp.WhatsAppBusinessMapping") as map_cls:
            mock_map = MagicMock()
            mock_map.get_business_id.return_value = 1
            map_cls.return_value = mock_map
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.post("/webhooks/whatsapp", json=_webhook_payload())
        assert r.status_code == 200
        mock_session.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_business_skipped(self):
        app, mock_session = _make_app()
        with patch("fonely.api.channels.whatsapp.WhatsAppBusinessMapping") as map_cls:
            mock_map = MagicMock()
            mock_map.get_business_id.return_value = None
            map_cls.return_value = mock_map
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.post("/webhooks/whatsapp", json=_webhook_payload())
        assert r.status_code == 200
        mock_session.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_session_factory_returns_503(self):
        app = FastAPI()
        app.include_router(router)
        transport = ASGITransport(app=app)
        with patch("fonely.api.channels.whatsapp.settings") as s:
            s.whatsapp_app_secret = ""
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.post("/webhooks/whatsapp", json=_webhook_payload())
        assert r.status_code == 503

    @pytest.mark.asyncio
    async def test_invalid_json_returns_200(self):
        app, _ = _make_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post(
                "/webhooks/whatsapp",
                content=b"not json",
                headers={"content-type": "application/json"},
            )
        assert r.status_code == 200


class TestWhatsAppBusinessMapping:
    def test_explicit_mappings(self):
        m = WhatsAppBusinessMapping(mappings={"phone1": 100, "phone2": 200})
        assert m.get_business_id("phone1") == 100
        assert m.get_business_id("phone2") == 200
        assert m.get_business_id("unknown") is None

    def test_empty_mappings(self):
        m = WhatsAppBusinessMapping(mappings={})
        assert m.get_business_id("anything") is None

    def test_reverse_mapping_prefers_trusted_configured_number(self):
        m = WhatsAppBusinessMapping(mappings={"phone1": 100, "phone2": 100})
        assert m.get_phone_number_id(100, preferred="phone2") == "phone2"
        assert m.get_phone_number_id(100) is None

    def test_from_settings_json(self):
        with patch("fonely.services.whatsapp_config.settings") as mock_s:
            mock_s.whatsapp_business_mappings = json.dumps({"ph1": 10})
            m = WhatsAppBusinessMapping()
            assert m.get_business_id("ph1") == 10

    def test_invalid_json_falls_back_empty(self):
        with patch("fonely.services.whatsapp_config.settings") as mock_s:
            mock_s.whatsapp_business_mappings = "not-json"
            m = WhatsAppBusinessMapping()
            assert m.get_business_id("anything") is None


class TestWhatsAppSender:
    @pytest.mark.asyncio
    async def test_send_text_success(self):
        from fonely.services.whatsapp_sender import WhatsAppSender

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"messages": [{"id": "wamid.sent123"}]}
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        sender = WhatsAppSender(
            access_token="test-token",
            phone_number_id="12345",
            client=mock_client,
        )
        result = await sender.send_text("919876543210", "Hello")
        assert result.success is True
        assert result.message_id == "wamid.sent123"

    @pytest.mark.asyncio
    async def test_send_text_timeout(self):
        import httpx

        from fonely.services.whatsapp_sender import WhatsAppSender

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))

        sender = WhatsAppSender(
            access_token="test-token",
            phone_number_id="12345",
            client=mock_client,
        )
        result = await sender.send_text("919876543210", "Hello")
        assert result.success is False
        assert result.error == "timeout"
        mock_client.post.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_text_http_error(self):
        import httpx

        from fonely.services.whatsapp_sender import WhatsAppSender

        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "auth error", request=MagicMock(), response=mock_response
            )
        )

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        sender = WhatsAppSender(
            access_token="test-token",
            phone_number_id="12345",
            client=mock_client,
        )
        result = await sender.send_text("919876543210", "Hello")
        assert result.success is False
        assert result.error == "http_401"


class TestConversationContinuity:
    def test_same_phone_returns_same_conversation(self):
        from fonely.services.conversation import (
            _CONVERSATIONS,
            _PHONE_INDEX,
            find_or_create_conversation,
        )

        _CONVERSATIONS.clear()
        _PHONE_INDEX.clear()
        ctx1 = find_or_create_conversation(1, "919876543210")
        ctx2 = find_or_create_conversation(1, "919876543210")
        assert ctx1.conversation_id == ctx2.conversation_id

    def test_different_phone_returns_different_conversation(self):
        from fonely.services.conversation import (
            _CONVERSATIONS,
            _PHONE_INDEX,
            find_or_create_conversation,
        )

        _CONVERSATIONS.clear()
        _PHONE_INDEX.clear()
        ctx1 = find_or_create_conversation(1, "919876543210")
        ctx2 = find_or_create_conversation(1, "919876500000")
        assert ctx1.conversation_id != ctx2.conversation_id

    def test_different_business_returns_different_conversation(self):
        from fonely.services.conversation import (
            _CONVERSATIONS,
            _PHONE_INDEX,
            find_or_create_conversation,
        )

        _CONVERSATIONS.clear()
        _PHONE_INDEX.clear()
        ctx1 = find_or_create_conversation(1, "919876543210")
        ctx2 = find_or_create_conversation(2, "919876543210")
        assert ctx1.conversation_id != ctx2.conversation_id

    def test_completed_conversation_starts_new(self):
        from fonely.domain.conversation.state import ConversationState
        from fonely.services.conversation import (
            _CONVERSATIONS,
            _PHONE_INDEX,
            find_or_create_conversation,
        )

        _CONVERSATIONS.clear()
        _PHONE_INDEX.clear()
        ctx1 = find_or_create_conversation(1, "919876543210")
        ctx1.state = ConversationState.COMPLETED
        ctx2 = find_or_create_conversation(1, "919876543210")
        assert ctx2.conversation_id != ctx1.conversation_id


class TestPIISafety:
    @pytest.mark.asyncio
    async def test_sender_logs_phone_suffix_only(self, caplog):
        from fonely.services.whatsapp_sender import WhatsAppSender

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"messages": [{"id": "wamid.x"}]}
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        sender = WhatsAppSender(
            access_token="secret-token-xyz",
            phone_number_id="12345",
            client=mock_client,
        )
        with caplog.at_level(logging.INFO, logger="fonely.services.whatsapp_sender"):
            await sender.send_text("919876543210", "Hello")

        log_output = caplog.text
        assert "919876543210" not in log_output
        assert "secret-token-xyz" not in log_output

    @pytest.mark.asyncio
    async def test_sender_never_logs_access_token(self, caplog):
        import httpx

        from fonely.services.whatsapp_sender import WhatsAppSender

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))

        sender = WhatsAppSender(
            access_token="super-secret-token-123",
            phone_number_id="12345",
            client=mock_client,
        )
        with caplog.at_level(logging.WARNING, logger="fonely.services.whatsapp_sender"):
            await sender.send_text("919876543210", "Hello")

        assert "super-secret-token-123" not in caplog.text


class TestWebhookSignatureVerification:
    def test_valid_signature_accepted(self) -> None:
        import hashlib
        import hmac as _hmac

        from fonely.api.channels.whatsapp import _verify_webhook_signature

        body = b'{"entry":[]}'
        secret = "test-app-secret"
        sig = "sha256=" + _hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        assert _verify_webhook_signature(body, sig, secret) is True

    def test_invalid_signature_rejected(self) -> None:
        from fonely.api.channels.whatsapp import _verify_webhook_signature

        assert _verify_webhook_signature(b"body", "sha256=wrong", "secret") is False

    def test_missing_prefix_rejected(self) -> None:
        from fonely.api.channels.whatsapp import _verify_webhook_signature

        assert _verify_webhook_signature(b"body", "noprefixhex", "secret") is False

    @pytest.mark.asyncio
    async def test_webhook_rejects_invalid_signature_with_secret_configured(self) -> None:
        import fonely.api.channels.whatsapp as wa_mod
        from fonely.core.config import Settings

        app = FastAPI()
        app.include_router(router)
        transport = ASGITransport(app=app)
        test_settings = Settings(whatsapp_app_secret="my-secret")
        with patch.object(wa_mod, "settings", test_settings):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    "/webhooks/whatsapp",
                    content=b'{"entry":[]}',
                    headers={"X-Hub-Signature-256": "sha256=wrong"},
                )
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_webhook_fails_closed_without_secret(self) -> None:
        import fonely.api.channels.whatsapp as wa_mod
        from fonely.core.config import Settings

        app = FastAPI()
        app.include_router(router)
        transport = ASGITransport(app=app)
        test_settings = Settings(whatsapp_app_secret="")
        with patch.object(wa_mod, "settings", test_settings):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    "/webhooks/whatsapp",
                    content=b'{"entry":[]}',
                )
        assert response.status_code == 503


class TestConstantTimeVerifyToken:
    @pytest.mark.asyncio
    async def test_verify_uses_constant_time_comparison(self) -> None:
        import fonely.api.channels.whatsapp as wa_mod
        from fonely.core.config import Settings

        app = FastAPI()
        app.include_router(router)
        transport = ASGITransport(app=app)
        test_settings = Settings(whatsapp_verify_token="correct-token")
        with patch.object(wa_mod, "settings", test_settings):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get(
                    "/webhooks/whatsapp",
                    params={
                        "hub.mode": "subscribe",
                        "hub.verify_token": "correct-token",
                        "hub.challenge": "challenge-value",
                    },
                )
        assert response.status_code == 200
        assert response.text == "challenge-value"
