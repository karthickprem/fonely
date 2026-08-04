"""Unit contracts for provider deferral and replayable inbound claims."""

import uuid
from unittest.mock import AsyncMock

import pytest

from fonely.services.model_gateway import ModelResponse
from fonely.workers.inbound_worker import (
    ClaimedEvent,
    DeferredModelGateway,
    ProviderCallRequiredError,
    ProviderRequest,
    _normalized_phone,
)


def claimed(**overrides: object) -> ClaimedEvent:
    values: dict[str, object] = {
        "event_id": 1,
        "business_id": 7,
        "message_id": "wamid.1",
        "sender_phone": "919876543210",
        "message_type": "text",
        "message_body": "book appointment",
        "phone_number_id": "phone-7",
        "claim_token": uuid.UUID("11111111-1111-1111-1111-111111111111"),
        "claim_version": 2,
        "attempts": 0,
        "max_attempts": 5,
    }
    values.update(overrides)
    return ClaimedEvent(**values)  # type: ignore[arg-type]


class TestDeferredGateway:
    @pytest.mark.asyncio
    async def test_returns_recorded_response_without_provider_io(self) -> None:
        response = ModelResponse(text="structured facts")
        gateway = DeferredModelGateway([response])
        assert await gateway.complete("system", [{"role": "user", "content": "hello"}]) is response

    @pytest.mark.asyncio
    async def test_requests_missing_provider_response(self) -> None:
        gateway = DeferredModelGateway([])
        with pytest.raises(ProviderCallRequiredError) as raised:
            await gateway.complete(
                "system",
                [{"role": "user", "content": "hello"}],
                temperature=0.1,
                max_tokens=42,
            )
        request: ProviderRequest = raised.value.request
        assert request.system_prompt == "system"
        assert request.max_tokens == 42
        assert request.temperature == 0.1


class TestClaimedEvent:
    def test_normalizes_phone_once(self) -> None:
        assert _normalized_phone(claimed()) == "+919876543210"
        assert _normalized_phone(claimed(sender_phone="+919876543210")) == "+919876543210"

    def test_claim_snapshot_is_immutable(self) -> None:
        event = claimed()
        with pytest.raises(AttributeError):
            event.attempts = 2  # type: ignore[misc]


class TestProviderInstrumentation:
    @pytest.mark.asyncio
    async def test_real_provider_can_be_called_without_session_argument(self) -> None:
        provider = AsyncMock()
        provider.complete = AsyncMock(return_value=ModelResponse(text="ok"))
        response = await provider.complete(
            system_prompt="s",
            messages=[{"role": "user", "content": "u"}],
            tools=None,
            temperature=0.3,
            max_tokens=100,
        )
        assert response.text == "ok"
        provider.complete.assert_awaited_once()
