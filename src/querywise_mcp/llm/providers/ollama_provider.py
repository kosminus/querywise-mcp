"""Ollama LLM provider — local models via Ollama REST API."""

import json
import time
from collections.abc import AsyncIterator

import httpx

from querywise_mcp.config import settings
from querywise_mcp.llm.base_provider import (
    BaseLLMProvider,
    LLMConfig,
    LLMMessage,
    LLMProviderType,
    LLMResponse,
)


class OllamaProvider(BaseLLMProvider):
    provider_type = LLMProviderType.OLLAMA

    def __init__(self, api_key: str | None = None):
        # api_key is ignored — Ollama is local, no auth needed
        self._base_url = settings.ollama_base_url.rstrip("/")
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=120.0)

    async def complete(
        self,
        messages: list[LLMMessage],
        config: LLMConfig,
    ) -> LLMResponse:
        ollama_messages = [{"role": m.role, "content": m.content} for m in messages]

        payload: dict = {
            "model": config.model,
            "messages": ollama_messages,
            "stream": False,
            "format": "json",
            "options": {
                "temperature": config.temperature,
                "top_p": config.top_p,
                "num_predict": config.max_tokens,
            },
        }

        if config.stop_sequences:
            payload["options"]["stop"] = config.stop_sequences

        start = time.monotonic()
        try:
            resp = await self._client.post("/api/chat", json=payload)
            resp.raise_for_status()
        except httpx.ConnectError as err:
            raise ConnectionError(
                f"Cannot connect to Ollama at {self._base_url}. "
                "Is Ollama running? Start it with: ollama serve"
            ) from err
        elapsed_ms = (time.monotonic() - start) * 1000

        data = resp.json()
        message_content = data.get("message", {}).get("content", "")

        return LLMResponse(
            content=message_content,
            model=data.get("model", config.model),
            input_tokens=data.get("prompt_eval_count", 0),
            output_tokens=data.get("eval_count", 0),
            finish_reason=data.get("done_reason", "stop"),
            latency_ms=elapsed_ms,
        )

    async def stream(
        self,
        messages: list[LLMMessage],
        config: LLMConfig,
    ) -> AsyncIterator[str]:
        ollama_messages = [{"role": m.role, "content": m.content} for m in messages]

        payload: dict = {
            "model": config.model,
            "messages": ollama_messages,
            "stream": True,
            "options": {
                "temperature": config.temperature,
                "top_p": config.top_p,
                "num_predict": config.max_tokens,
            },
        }

        try:
            async with self._client.stream("POST", "/api/chat", json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    chunk = json.loads(line)
                    content = chunk.get("message", {}).get("content", "")
                    if content:
                        yield content
                    if chunk.get("done", False):
                        break
        except httpx.ConnectError as err:
            raise ConnectionError(
                f"Cannot connect to Ollama at {self._base_url}. "
                "Is Ollama running? Start it with: ollama serve"
            ) from err

    async def generate_embedding(self, text: str) -> list[float]:
        """Generate embeddings using Ollama's embedding endpoint.

        Tries the new /api/embed endpoint first (Ollama 0.4+), then falls
        back to the legacy /api/embeddings endpoint for older versions.
        """
        model = settings.ollama_embedding_model

        try:
            return await self._embed_new_api(text, model)
        except httpx.HTTPStatusError as err:
            if err.response.status_code == 404:
                return await self._embed_legacy_api(text, model)
            raise
        except httpx.ConnectError as err:
            raise ConnectionError(
                f"Cannot connect to Ollama at {self._base_url}. "
                "Is Ollama running? Start it with: ollama serve"
            ) from err

    async def _embed_new_api(self, text: str, model: str) -> list[float]:
        """Ollama 0.4+ /api/embed endpoint."""
        resp = await self._client.post(
            "/api/embed", json={"model": model, "input": text}
        )
        resp.raise_for_status()
        data = resp.json()
        embeddings = data.get("embeddings", [])
        if not embeddings:
            raise ValueError(
                f"No embeddings returned from Ollama model '{model}'. "
                f"Make sure the model is pulled: ollama pull {model}"
            )
        return embeddings[0]

    async def _embed_legacy_api(self, text: str, model: str) -> list[float]:
        """Legacy /api/embeddings endpoint for older Ollama versions."""
        try:
            resp = await self._client.post(
                "/api/embeddings", json={"model": model, "prompt": text}
            )
            resp.raise_for_status()
        except httpx.ConnectError as err:
            raise ConnectionError(
                f"Cannot connect to Ollama at {self._base_url}. "
                "Is Ollama running? Start it with: ollama serve"
            ) from err
        data = resp.json()
        embedding = data.get("embedding", [])
        if not embedding:
            raise ValueError(
                f"No embeddings returned from Ollama model '{model}'. "
                f"Make sure the model is pulled: ollama pull {model}"
            )
        return embedding

    def list_models(self) -> list[str]:
        """Return commonly used Ollama models. Use /api/tags for dynamic listing."""
        return [
            "llama3.1:8b",
            "llama3.1:70b",
            "mistral:7b",
            "codellama:13b",
            "qwen2.5:7b",
        ]
