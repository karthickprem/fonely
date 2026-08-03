"""Tests for provider-resilient HTTP client."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from fonely.core.resilience import CircuitOpenError, ResilientClient


def _mock_response(status: int = 200, headers: dict[str, str] | None = None) -> MagicMock:
    r = MagicMock(spec=httpx.Response)
    r.status_code = status
    r.headers = headers or {}
    r.raise_for_status = MagicMock()
    if status >= 400:
        r.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"{status}", request=MagicMock(), response=r
        )
    return r


def _mock_client(responses: list[MagicMock | Exception]) -> httpx.AsyncClient:
    client = AsyncMock(spec=httpx.AsyncClient)
    client.post = AsyncMock(side_effect=responses)
    return client


class TestSuccessfulRequest:
    @pytest.mark.asyncio
    async def test_passes_through(self) -> None:
        mock = _mock_client([_mock_response(200)])
        rc = ResilientClient("test", client=mock, max_retries=0)
        result = await rc.post("http://example.com")
        assert result.status_code == 200
        assert rc.circuit_state == "closed"
        assert rc.stats["consecutive_failures"] == 0


class TestRetry:
    @pytest.mark.asyncio
    async def test_retries_on_timeout(self) -> None:
        mock = _mock_client(
            [
                httpx.TimeoutException("timeout"),
                httpx.TimeoutException("timeout"),
                _mock_response(200),
            ]
        )
        rc = ResilientClient("test", client=mock, max_retries=2, retry_backoff=0.01)
        result = await rc.post("http://example.com")
        assert result.status_code == 200
        assert mock.post.call_count == 3

    @pytest.mark.asyncio
    async def test_retries_on_500(self) -> None:
        mock = _mock_client([_mock_response(500), _mock_response(200)])
        rc = ResilientClient("test", client=mock, max_retries=1, retry_backoff=0.01)
        result = await rc.post("http://example.com")
        assert result.status_code == 200

    @pytest.mark.asyncio
    async def test_does_not_retry_400(self) -> None:
        mock = _mock_client([_mock_response(400)])
        rc = ResilientClient("test", client=mock, max_retries=2, retry_backoff=0.01)
        result = await rc.post("http://example.com")
        assert result.status_code == 400
        assert mock.post.call_count == 1

    @pytest.mark.asyncio
    async def test_does_not_retry_404(self) -> None:
        mock = _mock_client([_mock_response(404)])
        rc = ResilientClient("test", client=mock, max_retries=2, retry_backoff=0.01)
        result = await rc.post("http://example.com")
        assert result.status_code == 404
        assert mock.post.call_count == 1

    @pytest.mark.asyncio
    async def test_respects_retry_after_header(self) -> None:
        r429 = _mock_response(429, headers={"Retry-After": "0.01"})
        mock = _mock_client([r429, _mock_response(200)])
        rc = ResilientClient("test", client=mock, max_retries=1, retry_backoff=0.01)
        result = await rc.post("http://example.com")
        assert result.status_code == 200

    @pytest.mark.asyncio
    async def test_backoff_increases(self) -> None:
        mock = _mock_client(
            [
                httpx.TimeoutException("t"),
                httpx.TimeoutException("t"),
                _mock_response(200),
            ]
        )
        rc = ResilientClient("test", client=mock, max_retries=2, retry_backoff=0.01)
        start = time.monotonic()
        await rc.post("http://example.com")
        elapsed = time.monotonic() - start
        assert elapsed >= 0.02


class TestCircuitBreaker:
    @pytest.mark.asyncio
    async def test_opens_after_threshold(self) -> None:
        failures = [httpx.TimeoutException("t")] * 5
        mock = _mock_client(failures)
        rc = ResilientClient(
            "test",
            client=mock,
            max_retries=0,
            circuit_breaker_threshold=5,
            retry_backoff=0.001,
        )
        for _ in range(5):
            with pytest.raises(httpx.TimeoutException):
                await rc.post("http://example.com")
        assert rc.circuit_state == "open"

    @pytest.mark.asyncio
    async def test_open_raises_immediately(self) -> None:
        mock = _mock_client([])
        rc = ResilientClient(
            "test",
            client=mock,
            max_retries=0,
            circuit_breaker_threshold=1,
            retry_backoff=0.001,
        )
        rc._failure_count = 5
        rc._circuit_state = "open"
        rc._circuit_opened_at = time.monotonic()
        with pytest.raises(CircuitOpenError):
            await rc.post("http://example.com")
        assert mock.post.call_count == 0

    @pytest.mark.asyncio
    async def test_half_open_after_reset(self) -> None:
        rc = ResilientClient(
            "test",
            max_retries=0,
            circuit_breaker_threshold=1,
            circuit_breaker_reset=0.01,
        )
        rc._circuit_state = "open"
        rc._circuit_opened_at = time.monotonic() - 1.0
        assert rc.circuit_state == "half_open"

    @pytest.mark.asyncio
    async def test_half_open_success_closes(self) -> None:
        mock = _mock_client([_mock_response(200)])
        rc = ResilientClient(
            "test",
            client=mock,
            max_retries=0,
            circuit_breaker_threshold=1,
            circuit_breaker_reset=0.01,
        )
        rc._circuit_state = "half_open"
        rc._failure_count = 5
        await rc.post("http://example.com")
        assert rc.circuit_state == "closed"
        assert rc._failure_count == 0

    @pytest.mark.asyncio
    async def test_half_open_failure_reopens(self) -> None:
        mock = _mock_client([httpx.TimeoutException("t")])
        rc = ResilientClient(
            "test",
            client=mock,
            max_retries=0,
            circuit_breaker_threshold=1,
            circuit_breaker_reset=0.01,
        )
        rc._circuit_state = "half_open"
        with pytest.raises(httpx.TimeoutException):
            await rc.post("http://example.com")
        assert rc.circuit_state == "open"


class TestModelGatewayFallback:
    @pytest.mark.asyncio
    async def test_circuit_open_returns_graceful_response(self) -> None:
        from fonely.services.model_gateway import SarvamModelGateway

        rc = ResilientClient("test", max_retries=0, circuit_breaker_threshold=1)
        rc._circuit_state = "open"
        rc._circuit_opened_at = time.monotonic()

        gw = SarvamModelGateway(client=rc)
        result = await gw.complete("system", [{"role": "user", "content": "hi"}])
        assert result.model == "fallback"
        assert "trouble connecting" in result.text


class TestWhatsAppSenderFallback:
    @pytest.mark.asyncio
    async def test_circuit_open_returns_failure(self) -> None:
        from fonely.services.whatsapp_sender import WhatsAppSender

        rc = ResilientClient("test", max_retries=0, circuit_breaker_threshold=1)
        rc._circuit_state = "open"
        rc._circuit_opened_at = time.monotonic()

        sender = WhatsAppSender(
            access_token="test-token",
            phone_number_id="123",
            client=rc,
        )
        result = await sender.send_text("919876543210", "Hello")
        assert result.success is False
        assert result.error == "circuit_open"


class TestStats:
    @pytest.mark.asyncio
    async def test_stats_track_requests_and_failures(self) -> None:
        mock = _mock_client([_mock_response(200), httpx.TimeoutException("t")])
        rc = ResilientClient("test", client=mock, max_retries=0)
        await rc.post("http://example.com")
        with pytest.raises(httpx.TimeoutException):
            await rc.post("http://example.com")
        stats = rc.stats
        assert stats["total_requests"] == 2
        assert stats["total_failures"] == 1
        assert stats["name"] == "test"
