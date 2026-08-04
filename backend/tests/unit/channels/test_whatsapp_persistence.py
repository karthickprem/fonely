"""Tests for inbound worker domain processing — returns response text."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fonely.workers.inbound_worker import _process_domain


class TestInboundWorkerDomainProcessing:
    @pytest.mark.asyncio
    async def test_text_event_returns_conversation_response(self) -> None:
        from fonely.domain.conversation.state import ConversationContext

        event = MagicMock()
        event.id = 1
        event.message_id = "wamid.test1"
        event.business_id = 1
        event.sender_phone = "919876543210"
        event.message_type = "text"
        event.message_body = "Hello doctor"

        mock_session = AsyncMock()
        mock_gateway = MagicMock()
        mock_ctx = ConversationContext(business_id=1)

        mock_turn = MagicMock()
        mock_turn.assistant_response = "Welcome!"

        with (
            patch(
                "fonely.workers.inbound_worker._is_owner",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "fonely.workers.inbound_worker.find_or_create_conversation_persistent",
                new_callable=AsyncMock,
                return_value=mock_ctx,
            ),
            patch("fonely.workers.inbound_worker.ConversationService") as conv_cls,
            patch("fonely.workers.inbound_worker.AppointmentService"),
            patch("fonely.workers.inbound_worker.InternalValidationPort"),
        ):
            mock_conv = AsyncMock()
            mock_conv.process_message = AsyncMock(return_value=mock_turn)
            conv_cls.return_value = mock_conv

            result = await _process_domain(event, mock_session, mock_gateway)

            assert result == "Welcome!"
            mock_conv.process_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_non_text_event_returns_polite_decline(self) -> None:
        event = MagicMock()
        event.id = 2
        event.message_id = "wamid.img1"
        event.business_id = 1
        event.sender_phone = "919876543210"
        event.message_type = "image"
        event.message_body = None

        result = await _process_domain(event, AsyncMock(), MagicMock())
        assert "text messages" in result

    @pytest.mark.asyncio
    async def test_owner_message_returns_command_response(self) -> None:
        event = MagicMock()
        event.id = 3
        event.message_id = "wamid.owner1"
        event.business_id = 1
        event.sender_phone = "919000000001"
        event.message_type = "text"
        event.message_body = "show tomorrow appointments"

        mock_session = AsyncMock()
        mock_gateway = MagicMock()

        owner_result = MagicMock()
        owner_result.response_text = "No appointments tomorrow."

        with (
            patch(
                "fonely.workers.inbound_worker._is_owner",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("fonely.services.owner_commands.OwnerCommandService") as owner_cls,
        ):
            mock_owner_svc = AsyncMock()
            mock_owner_svc.process_command = AsyncMock(return_value=owner_result)
            owner_cls.return_value = mock_owner_svc

            result = await _process_domain(event, mock_session, mock_gateway)

            assert result == "No appointments tomorrow."
            mock_owner_svc.process_command.assert_awaited_once()
