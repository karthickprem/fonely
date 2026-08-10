"""OpenAI-compatible LLM adapter for PipelineRuntime.

Uses the same AMD gateway as the Anthropic adapter but routes
to OpenAI models (gpt-5.6-luna, gpt-4o, etc.) via the
/v1/chat/completions endpoint.
"""
from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger("fonely.voice.llm_openai")


class OpenAILLMAdapter:
    """LLM adapter using OpenAI chat completions API via AMD gateway."""

    def __init__(
        self,
        *,
        model: str = "gpt-5.6-luna",
        base_url: str | None = None,
        api_key: str | None = None,
        max_completion_tokens: int = 300,
    ) -> None:
        self._model = model
        self._base_url = base_url or os.environ.get("ANTHROPIC_BASE_URL", "")
        self._max_tokens = max_completion_tokens
        self._headers = self._build_headers(api_key)
        self.call_count = 0

    def _build_headers(self, api_key: str | None) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            headers["Ocp-Apim-Subscription-Key"] = api_key
        else:
            for line in os.environ.get("ANTHROPIC_CUSTOM_HEADERS", "").split("\n"):
                if ":" in line.strip():
                    k, v = line.strip().split(":", 1)
                    headers[k.strip()] = v.strip()
        headers.setdefault("user", os.environ.get("ANTHROPIC_GATEWAY_USER", "karthick"))
        return headers

    async def generate(self, system: str, messages: list[dict[str, str]]) -> str:
        self.call_count += 1
        body = {
            "model": self._model,
            "max_completion_tokens": self._max_tokens,
            "messages": [{"role": "system", "content": system}] + messages,
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"{self._base_url}/v1/chat/completions",
                headers=self._headers,
                json=body,
            )
            if r.status_code != 200:
                logger.error("openai_llm_error", extra={"status": r.status_code, "body": r.text[:200]})
                return ""
            data = r.json()
            return data["choices"][0]["message"]["content"]

    async def close(self) -> None:
        pass
