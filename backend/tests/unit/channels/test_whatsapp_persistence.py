"""Tests for WhatsApp adapter persistence wiring."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fonely.api.channels.whatsapp import _PROCESSED_MESSAGE_IDS, _handle_message
from fonely.services.conversation import _CONVERSATIONS, _PHONE_INDEX


@pytest.fixture(autouse=True)
def _clear():
    _PROCESSED_MESSAGE_IDS.clear()
    _CONVERSATIONS.clear()
    _PHONE_INDEX.clear()
    yield
    _PROCESSED_MESSAGE_IDS.clear()
    _CONVERSATIONS.clear()
    _PHONE_INDEX.clear()


@pytest.fixture(autouse=True)
def _patch_settings():
    with patch("fonely.api.channels.whatsapp.settings") as mock_settings:
        mock_settings.whatsapp_verify_token = "test-token"
        mock_settings.whatsapp_access_token = "test-access"
        mock_settings.whatsapp_phone_number_id = "12345"
        mock_settings.whatsapp_business_mappings = ""
        mock_settings.whatsapp_app_secret = ""
        yield mock_settings


def _text_message(
    text: str = "Hello",
    sender: str = "919876543210",
    message_id: str = "wamid.test1",
) -> dict:
    return {
        "id": message_id,
        "from": sender,
        "type": "text",
        "text": {"body": text},
        "timestamp": "1690000000",
    }


def _mock_app():
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.commit = AsyncMock()

    dedup_result = MagicMock()
    dedup_result.rowcount = 1
    mock_session.execute = AsyncMock(return_value=dedup_result)

    mock_factory = MagicMock()
    mock_factory.return_value = mock_session

    app = MagicMock()
    app.state.session_factory = mock_factory
    app.state.model_gateway = MagicMock()
    app.state.http_client = None
    return app, mock_session


class TestPersistentConversationLookup:
    @pytest.mark.asyncio
    async def test_uses_persistent_find_or_create(self):
        from fonely.domain.conversation.state import ConversationContext

        app, _mock_session = _mock_app()
        mock_ctx = ConversationContext(business_id=1)

        with (
            patch("fonely.api.channels.whatsapp._get_sender") as mock_get,
            patch("fonely.api.channels.whatsapp.WhatsAppBusinessMapping") as mock_map_cls,
            patch(
                "fonely.api.channels.whatsapp.find_or_create_conversation_persistent"
            ) as mock_find,
            patch("fonely.api.channels.whatsapp.ConversationService") as mock_conv_cls,
        ):
            mock_sender = AsyncMock()
            mock_get.return_value = mock_sender

            mock_map = MagicMock()
            mock_map.get_business_id.return_value = 1
            mock_map_cls.return_value = mock_map

            mock_find.return_value = mock_ctx

            mock_turn = MagicMock()
            mock_turn.assistant_response = "Hi!"
            mock_conv = AsyncMock()
            mock_conv.process_message = AsyncMock(return_value=mock_turn)
            mock_conv_cls.return_value = mock_conv

            await _handle_message(_text_message(), "12345", app)

            mock_find.assert_called_once()
            call_args = mock_find.call_args
            assert call_args[0][0] == 1
            assert "+919876543210" in call_args[0][1]


class TestSessionCommit:
    @pytest.mark.asyncio
    async def test_session_committed_after_process_message(self):
        from fonely.domain.conversation.state import ConversationContext

        app, mock_session = _mock_app()
        mock_ctx = ConversationContext(business_id=1)

        with (
            patch("fonely.api.channels.whatsapp._get_sender") as mock_get,
            patch("fonely.api.channels.whatsapp.WhatsAppBusinessMapping") as mock_map_cls,
            patch(
                "fonely.api.channels.whatsapp.find_or_create_conversation_persistent"
            ) as mock_find,
            patch("fonely.api.channels.whatsapp.ConversationService") as mock_conv_cls,
        ):
            mock_sender = AsyncMock()
            mock_get.return_value = mock_sender

            mock_map = MagicMock()
            mock_map.get_business_id.return_value = 1
            mock_map_cls.return_value = mock_map

            mock_find.return_value = mock_ctx

            mock_turn = MagicMock()
            mock_turn.assistant_response = "Booked!"
            mock_conv = AsyncMock()
            mock_conv.process_message = AsyncMock(return_value=mock_turn)
            mock_conv_cls.return_value = mock_conv

            await _handle_message(_text_message(), "12345", app)

            assert mock_session.commit.call_count >= 2


class TestCompletedConversation:
    @pytest.mark.asyncio
    async def test_completed_conversation_marked_in_db(self):
        from fonely.domain.conversation.state import (
            ConversationContext,
            ConversationState,
        )

        app, _mock_session = _mock_app()
        mock_ctx = ConversationContext(business_id=1)
        mock_ctx.state = ConversationState.COMPLETED

        with (
            patch("fonely.api.channels.whatsapp._get_sender") as mock_get,
            patch("fonely.api.channels.whatsapp.WhatsAppBusinessMapping") as mock_map_cls,
            patch(
                "fonely.api.channels.whatsapp.find_or_create_conversation_persistent"
            ) as mock_find,
            patch("fonely.api.channels.whatsapp.ConversationService") as mock_conv_cls,
            patch(
                "fonely.services.conversation_persistence.ConversationPersistenceService"
            ) as mock_persist_cls,
        ):
            mock_sender = AsyncMock()
            mock_get.return_value = mock_sender

            mock_map = MagicMock()
            mock_map.get_business_id.return_value = 1
            mock_map_cls.return_value = mock_map

            mock_find.return_value = mock_ctx

            mock_turn = MagicMock()
            mock_turn.assistant_response = "Confirmed!"
            mock_conv = AsyncMock()
            mock_conv.process_message = AsyncMock(return_value=mock_turn)
            mock_conv_cls.return_value = mock_conv

            mock_persist = AsyncMock()
            mock_persist_cls.return_value = mock_persist

            await _handle_message(_text_message(), "12345", app)

            mock_persist.mark_completed.assert_called_once_with(mock_ctx.conversation_id)
