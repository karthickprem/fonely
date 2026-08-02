"""Provider-neutral model gateway for LLM completion."""

import logging
import time
from dataclasses import dataclass
from typing import Protocol

import httpx

from fonely.core.config import settings

logger = logging.getLogger("fonely.services.model_gateway")


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, object]
    id: str = ""


@dataclass(frozen=True)
class ModelResponse:
    text: str
    tool_calls: list[ToolCall] | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    latency_ms: float = 0.0


class ModelGateway(Protocol):
    async def complete(
        self,
        system_prompt: str,
        messages: list[dict[str, str]],
        tools: list[dict[str, object]] | None = None,
        temperature: float = 0.3,
        max_tokens: int = 500,
    ) -> ModelResponse: ...


class SarvamModelGateway:
    def __init__(
        self,
        *,
        timeout: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = settings.sarvam_llm_url
        self._model = settings.sarvam_llm_model
        self._api_key = settings.sarvam_api_key
        self._timeout = timeout
        self._client = client

    async def complete(
        self,
        system_prompt: str,
        messages: list[dict[str, str]],
        tools: list[dict[str, object]] | None = None,
        temperature: float = 0.3,
        max_tokens: int = 500,
    ) -> ModelResponse:
        start = time.monotonic()
        request_messages = [{"role": "system", "content": system_prompt}, *messages]

        body: dict[str, object] = {
            "model": self._model,
            "messages": request_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            body["tools"] = tools

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }

        client = self._client or httpx.AsyncClient()
        owns_client = self._client is None
        try:
            response = await client.post(
                self._url,
                json=body,
                headers=headers,
                timeout=self._timeout,
            )
            response.raise_for_status()
            data = response.json()
        except httpx.TimeoutException:
            logger.warning(
                "model_timeout",
                extra={"model": self._model, "timeout": self._timeout},
            )
            raise
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "model_error",
                extra={
                    "model": self._model,
                    "status": exc.response.status_code,
                },
            )
            raise
        finally:
            if owns_client:
                await client.aclose()

        latency = (time.monotonic() - start) * 1000
        choice = data.get("choices", [{}])[0]
        message = choice.get("message", {})
        usage = data.get("usage", {})

        parsed_tools: list[ToolCall] | None = None
        raw_tool_calls = message.get("tool_calls")
        if raw_tool_calls:
            parsed_tools = [
                ToolCall(
                    name=tc.get("function", {}).get("name", ""),
                    arguments=tc.get("function", {}).get("arguments", {}),
                    id=tc.get("id", ""),
                )
                for tc in raw_tool_calls
            ]

        return ModelResponse(
            text=message.get("content", ""),
            tool_calls=parsed_tools,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            model=data.get("model", self._model),
            latency_ms=latency,
        )
