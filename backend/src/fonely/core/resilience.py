"""Provider-resilient HTTP client with retry and circuit breaker."""

from __future__ import annotations

import asyncio
import logging
import random
import time

import httpx

logger = logging.getLogger("fonely.core.resilience")

_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


class CircuitOpenError(Exception):
    pass


class ResilientClient:
    def __init__(
        self,
        name: str,
        *,
        timeout: float = 10.0,
        max_retries: int = 2,
        retry_backoff: float = 0.5,
        circuit_breaker_threshold: int = 5,
        circuit_breaker_reset: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.name = name
        self._timeout = timeout
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff
        self._cb_threshold = circuit_breaker_threshold
        self._cb_reset = circuit_breaker_reset
        self._client = client

        self._failure_count = 0
        self._total_requests = 0
        self._total_failures = 0
        self._circuit_state = "closed"
        self._circuit_opened_at = 0.0

    @property
    def circuit_state(self) -> str:
        if (
            self._circuit_state == "open"
            and time.monotonic() - self._circuit_opened_at >= self._cb_reset
        ):
            self._circuit_state = "half_open"
        return self._circuit_state

    @property
    def stats(self) -> dict[str, object]:
        return {
            "name": self.name,
            "circuit_state": self.circuit_state,
            "total_requests": self._total_requests,
            "total_failures": self._total_failures,
            "consecutive_failures": self._failure_count,
        }

    async def post(self, url: str, **kwargs: object) -> httpx.Response:
        self._total_requests += 1

        state = self.circuit_state
        if state == "open":
            self._total_failures += 1
            raise CircuitOpenError(f"Circuit breaker open for {self.name}")

        kwargs.setdefault("timeout", self._timeout)

        last_exc: Exception | None = None
        for attempt in range(1 + self._max_retries):
            if attempt > 0 and state == "half_open":
                break

            try:
                client = self._client or httpx.AsyncClient()
                owns = self._client is None
                try:
                    response = await client.post(url, **kwargs)  # type: ignore[arg-type]
                finally:
                    if owns:
                        await client.aclose()

                if response.status_code in _RETRYABLE_STATUS_CODES:
                    if response.status_code == 429:
                        retry_after = response.headers.get("Retry-After")
                        if retry_after:
                            try:
                                wait = float(retry_after)
                                await asyncio.sleep(min(wait, 30.0))
                            except ValueError:
                                pass
                    if attempt < self._max_retries:
                        backoff = self._retry_backoff * (2**attempt)
                        jitter = random.uniform(0, backoff * 0.5)
                        logger.info(
                            "resilient_retry",
                            extra={
                                "provider": self.name,
                                "attempt": attempt + 1,
                                "status": response.status_code,
                                "backoff": round(backoff + jitter, 2),
                            },
                        )
                        await asyncio.sleep(backoff + jitter)
                        continue

                    self._record_failure()
                    response.raise_for_status()

                self._record_success()
                return response

            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                last_exc = exc
                if attempt < self._max_retries:
                    backoff = self._retry_backoff * (2**attempt)
                    jitter = random.uniform(0, backoff * 0.5)
                    logger.info(
                        "resilient_retry",
                        extra={
                            "provider": self.name,
                            "attempt": attempt + 1,
                            "error": type(exc).__name__,
                            "backoff": round(backoff + jitter, 2),
                        },
                    )
                    await asyncio.sleep(backoff + jitter)
                    continue

                self._record_failure()
                raise

            except httpx.HTTPStatusError:
                self._record_failure()
                raise

        if last_exc is not None:
            self._record_failure()
            raise last_exc

        raise RuntimeError("Unreachable")  # pragma: no cover

    def _record_success(self) -> None:
        if self._circuit_state == "half_open":
            logger.info(
                "circuit_closed",
                extra={"provider": self.name},
            )
        self._failure_count = 0
        self._circuit_state = "closed"

    def _record_failure(self) -> None:
        self._failure_count += 1
        self._total_failures += 1
        if self._failure_count >= self._cb_threshold:
            if self._circuit_state != "open":
                logger.warning(
                    "circuit_opened",
                    extra={
                        "provider": self.name,
                        "failures": self._failure_count,
                    },
                )
            self._circuit_state = "open"
            self._circuit_opened_at = time.monotonic()
