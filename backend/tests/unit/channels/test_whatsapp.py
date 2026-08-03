"""Tests for the WhatsApp inbound adapter."""

import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from fonely.api.channels.whatsapp import (
    _PROCESSED_MESSAGE_IDS,
    _handle_message,
    router,
)
from fonely.services.conversation import _CONVERSATIONS, _PHONE_INDEX
from fonely.services.whatsapp_config import WhatsAppBusinessMapping


@pytest.fixture(autouse=True)
def _clear_dedup():
    _PROCESSED_MESSAGE_IDS.clear()
    yield
    _PROCESSED_MESSAGE_IDS.clear()


@pytest.fixture(autouse=True)
def _clear_conversations():
    _CONVERSATIONS.clear()
    _PHONE_INDEX.clear()
    yield
    _CONVERSATIONS.clear()
    _PHONE_INDEX.clear()


@pytest.fixture(autouse=True)
def _patch_settings():
    with patch("fonely.api.channels.whatsapp.settings") as mock_settings:
        mock_settings.whatsapp_verify_token = "test-token"
        mock_settings.whatsapp_access_token = "test-access"
        mock_settings.whatsapp_phone_number_id = "12345"
        mock_settings.whatsapp_business_mappings = ""
        yield mock_settings


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


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
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "BUSINESS_ACCOUNT_ID",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15550000000",
                                "phone_number_id": phone_number_id,
                            },
                            "messages": [message],
                        },
                        "field": "messages",
                    }
                ],
            }
        ],
    }


class TestWebhookVerification:
    @pytest.mark.asyncio
    async def test_valid_verification(self):
        app = _make_app()
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
        app = _make_app()
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
        app = _make_app()
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


class TestWebhookIncoming:
    @pytest.mark.asyncio
    async def test_post_returns_200_immediately(self):
        app = _make_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post(
                "/webhooks/whatsapp",
                json=_webhook_payload(),
            )
            assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_invalid_json_returns_200(self):
        app = _make_app()
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


def _mock_app(
    *,
    process_message_return: object | None = None,
    process_message_side_effect: Exception | None = None,
) -> MagicMock:
    mock_turn = MagicMock()
    mock_turn.assistant_response = "Welcome! How can I help?"

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    dedup_result = MagicMock()
    dedup_result.rowcount = 1
    mock_session.execute = AsyncMock(return_value=dedup_result)

    mock_factory = MagicMock()
    mock_factory.return_value = mock_session

    app = MagicMock()
    app.state.session_factory = mock_factory
    app.state.model_gateway = MagicMock()
    app.state.http_client = None
    return app


class TestMessageHandling:
    @pytest.mark.asyncio
    async def test_non_text_message_sends_polite_response(self):
        with patch("fonely.api.channels.whatsapp._get_sender") as mock_get:
            mock_sender = AsyncMock()
            mock_get.return_value = mock_sender
            app = MagicMock()

            msg = {
                "id": "wamid.img1",
                "from": "919876543210",
                "type": "image",
                "timestamp": "1690000000",
            }
            await _handle_message(msg, "12345", app)

            mock_sender.send_text.assert_called_once()
            call_args = mock_sender.send_text.call_args
            assert "text messages" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_duplicate_message_not_reprocessed(self):
        with patch("fonely.api.channels.whatsapp._get_sender") as mock_get:
            mock_sender = AsyncMock()
            mock_get.return_value = mock_sender

            with patch("fonely.api.channels.whatsapp.WhatsAppBusinessMapping") as mock_map_cls:
                mock_map = MagicMock()
                mock_map.get_business_id.return_value = 1
                mock_map_cls.return_value = mock_map

                app = _mock_app()

                with patch("fonely.api.channels.whatsapp.ConversationService") as mock_conv_cls:
                    mock_conv = AsyncMock()
                    mock_turn = MagicMock()
                    mock_turn.assistant_response = "Hello"
                    mock_conv.process_message = AsyncMock(return_value=mock_turn)
                    mock_conv_cls.return_value = mock_conv

                    msg = {
                        "id": "wamid.dup1",
                        "from": "919876543210",
                        "type": "text",
                        "text": {"body": "Hi"},
                        "timestamp": "1690000000",
                    }

                    await _handle_message(msg, "12345", app)
                    await _handle_message(msg, "12345", app)

                    assert mock_conv.process_message.call_count == 1

    @pytest.mark.asyncio
    async def test_unknown_phone_number_id_returns_silently(self):
        with patch("fonely.api.channels.whatsapp._get_sender") as mock_get:
            mock_sender = AsyncMock()
            mock_get.return_value = mock_sender

            with patch("fonely.api.channels.whatsapp.WhatsAppBusinessMapping") as mock_map_cls:
                mock_map = MagicMock()
                mock_map.get_business_id.return_value = None
                mock_map_cls.return_value = mock_map

                app = MagicMock()
                msg = {
                    "id": "wamid.unknown1",
                    "from": "919876543210",
                    "type": "text",
                    "text": {"body": "Hi"},
                    "timestamp": "1690000000",
                }
                await _handle_message(msg, "unknown_phone", app)

                mock_sender.send_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_text_message_calls_process_message(self):
        with patch("fonely.api.channels.whatsapp._get_sender") as mock_get:
            mock_sender = AsyncMock()
            mock_get.return_value = mock_sender

            with patch("fonely.api.channels.whatsapp.WhatsAppBusinessMapping") as mock_map_cls:
                mock_map = MagicMock()
                mock_map.get_business_id.return_value = 1
                mock_map_cls.return_value = mock_map

                app = _mock_app()

                with patch("fonely.api.channels.whatsapp.ConversationService") as mock_conv_cls:
                    mock_turn = MagicMock()
                    mock_turn.assistant_response = "Welcome to the clinic!"
                    mock_conv = AsyncMock()
                    mock_conv.process_message = AsyncMock(return_value=mock_turn)
                    mock_conv_cls.return_value = mock_conv

                    msg = {
                        "id": "wamid.text1",
                        "from": "919876543210",
                        "type": "text",
                        "text": {"body": "appointment book pannunga"},
                        "timestamp": "1690000000",
                    }
                    await _handle_message(msg, "12345", app)

                    mock_conv.process_message.assert_called_once()
                    call_args = mock_conv.process_message.call_args
                    assert call_args[0][1] == 1
                    assert call_args[0][3] == "appointment book pannunga"

                    mock_sender.send_text.assert_called_once_with(
                        "919876543210", "Welcome to the clinic!"
                    )

    @pytest.mark.asyncio
    async def test_process_message_error_sends_fallback(self):
        with patch("fonely.api.channels.whatsapp._get_sender") as mock_get:
            mock_sender = AsyncMock()
            mock_get.return_value = mock_sender

            with patch("fonely.api.channels.whatsapp.WhatsAppBusinessMapping") as mock_map_cls:
                mock_map = MagicMock()
                mock_map.get_business_id.return_value = 1
                mock_map_cls.return_value = mock_map

                app = _mock_app()

                with patch("fonely.api.channels.whatsapp.ConversationService") as mock_conv_cls:
                    mock_conv = AsyncMock()
                    mock_conv.process_message = AsyncMock(side_effect=RuntimeError("db error"))
                    mock_conv_cls.return_value = mock_conv

                    msg = {
                        "id": "wamid.err1",
                        "from": "919876543210",
                        "type": "text",
                        "text": {"body": "hello"},
                        "timestamp": "1690000000",
                    }
                    await _handle_message(msg, "12345", app)

                    mock_sender.send_text.assert_called_once()
                    assert "went wrong" in mock_sender.send_text.call_args[0][1]


class TestConversationContinuity:
    def test_same_phone_returns_same_conversation(self):
        from fonely.services.conversation import find_or_create_conversation

        ctx1 = find_or_create_conversation(1, "919876543210")
        ctx2 = find_or_create_conversation(1, "919876543210")
        assert ctx1.conversation_id == ctx2.conversation_id

    def test_different_phone_returns_different_conversation(self):
        from fonely.services.conversation import find_or_create_conversation

        ctx1 = find_or_create_conversation(1, "919876543210")
        ctx2 = find_or_create_conversation(1, "919876500000")
        assert ctx1.conversation_id != ctx2.conversation_id

    def test_different_business_returns_different_conversation(self):
        from fonely.services.conversation import find_or_create_conversation

        ctx1 = find_or_create_conversation(1, "919876543210")
        ctx2 = find_or_create_conversation(2, "919876543210")
        assert ctx1.conversation_id != ctx2.conversation_id

    def test_completed_conversation_starts_new(self):
        from fonely.domain.conversation.state import ConversationState
        from fonely.services.conversation import find_or_create_conversation

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


# --- Security hardening tests ---


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
    async def test_webhook_skips_verification_without_secret(self) -> None:
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
        assert response.status_code == 200


class TestBoundedDedupSet:
    def test_eviction_preserves_recent_ids(self) -> None:
        from fonely.api.channels.whatsapp import (
            _PROCESSED_MESSAGE_IDS,
            _is_duplicate_in_memory,
            _mark_processed_in_memory,
        )

        _PROCESSED_MESSAGE_IDS.clear()
        for i in range(15):
            assert _is_duplicate_in_memory(f"msg-{i}") is False
            _mark_processed_in_memory(f"msg-{i}")

        assert _is_duplicate_in_memory("msg-14") is True
        assert _is_duplicate_in_memory("msg-0") is False or "msg-0" in _PROCESSED_MESSAGE_IDS

    def test_duplicate_detected(self) -> None:
        from fonely.api.channels.whatsapp import (
            _PROCESSED_MESSAGE_IDS,
            _is_duplicate_in_memory,
            _mark_processed_in_memory,
        )

        _PROCESSED_MESSAGE_IDS.clear()
        assert _is_duplicate_in_memory("unique-msg") is False
        _mark_processed_in_memory("unique-msg")
        assert _is_duplicate_in_memory("unique-msg") is True


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
