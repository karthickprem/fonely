"""Tests for inbound worker phases — reasoning and commit."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fonely.workers.inbound_worker import ClaimedEvent, _phase_b_reason


def _claimed(
    *,
    message_type: str = "text",
    message_body: str | None = "Hello",
    sender_phone: str = "919876543210",
) -> ClaimedEvent:
    return ClaimedEvent(
        event_id=1,
        business_id=1,
        message_id="wamid.test1",
        sender_phone=sender_phone,
        message_type=message_type,
        message_body=message_body,
        phone_number_id="12345",
        claim_token=None,
        attempts=0,
        max_attempts=5,
    )


class TestPhaseB:
    @pytest.mark.asyncio
    async def test_non_text_returns_polite_decline(self) -> None:
        result = await _phase_b_reason(
            _claimed(message_type="image"), MagicMock(), MagicMock()
        )
        assert "text messages" in result

    @pytest.mark.asyncio
    async def test_empty_body_returns_error(self) -> None:
        result = await _phase_b_reason(
            _claimed(message_body=""), MagicMock(), MagicMock()
        )
        assert "didn't receive" in result

    @pytest.mark.asyncio
    async def test_customer_text_returns_body_for_phase_c(self) -> None:
        mock_session = AsyncMock()
        mock_factory = MagicMock()
        mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_factory.return_value.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "fonely.workers.inbound_worker._is_owner",
            new_callable=AsyncMock,
            return_value=False,
        ):
            result = await _phase_b_reason(
                _claimed(message_body="book appointment"),
                mock_factory,
                MagicMock(),
            )
        assert result == "book appointment"

    @pytest.mark.asyncio
    async def test_owner_returns_command_response(self) -> None:
        mock_session = AsyncMock()
        mock_factory = MagicMock()
        mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_factory.return_value.__aexit__ = AsyncMock(return_value=None)

        owner_result = MagicMock()
        owner_result.response_text = "No appointments."

        with (
            patch(
                "fonely.workers.inbound_worker._is_owner",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("fonely.services.owner_commands.OwnerCommandService") as owner_cls,
        ):
            mock_svc = AsyncMock()
            mock_svc.process_command = AsyncMock(return_value=owner_result)
            owner_cls.return_value = mock_svc

            result = await _phase_b_reason(
                _claimed(message_body="show appointments"),
                mock_factory,
                MagicMock(),
            )
        assert result == "No appointments."
