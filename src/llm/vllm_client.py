"""Async LLM client — chat + embed via the OpenAI-compatible API.

Backed by Ollama on the macOS host (Apple Silicon GPU), reachable from K8s pods
via OrbStack DNS:
  Chat / Embed endpoint → http://host.orb.internal:11434/v1

Keeps the ``openai.AsyncOpenAI`` transport for architectural decoupling.
Ollama's /v1/chat/completions and /v1/embeddings are OpenAI-standard.
"""

from __future__ import annotations

import logging
from typing import Any

import openai
from pydantic import BaseModel, Field, PrivateAttr

logger = logging.getLogger(__name__)

# Ollama doesn't enforce an API key; openai client requires a non-empty string.
_API_KEY = "ollama"

# Default context window passed as max_tokens when options.num_ctx is absent.
DEFAULT_MAX_TOKENS = 4096


def _base_url(url: str) -> str:
    """Normalise URL: strip trailing /v1 then re-append once."""
    return url.rstrip("/").removesuffix("/v1").rstrip("/") + "/v1"


class VLLMClient(BaseModel):
    """Async LLM client (Ollama backend) with Ollama-compatible chat/embed interface."""

    base_url: str = Field(
        default="http://host.orb.internal:11434/v1",
        description="Ollama chat/completions base URL (with or without /v1 suffix).",
    )
    embed_url: str = Field(
        default="http://host.orb.internal:11434/v1",
        description="Ollama embeddings base URL (with or without /v1 suffix).",
    )
    timeout_s: float = Field(default=120.0, ge=1.0)

    model_config = {"arbitrary_types_allowed": True}

    _chat_client: openai.AsyncOpenAI = PrivateAttr()
    _embed_client: openai.AsyncOpenAI = PrivateAttr()

    def model_post_init(self, _context: Any) -> None:
        self._chat_client = openai.AsyncOpenAI(
            api_key=_API_KEY,
            base_url=_base_url(self.base_url),
            timeout=self.timeout_s,
        )
        self._embed_client = openai.AsyncOpenAI(
            api_key=_API_KEY,
            base_url=_base_url(self.embed_url),
            timeout=self.timeout_s,
        )

    async def aclose(self) -> None:
        await self._chat_client.close()
        await self._embed_client.close()

    # ------------------------------------------------------------------
    # Chat — same signature as OllamaClient.chat()
    # ------------------------------------------------------------------

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        stream: bool = False,
        options: dict[str, Any] | None = None,
        format: str | None = None,  # "json" → response_format=json_object
        keep_alive: str | None = None,  # accepted for call-site compat, unused
    ) -> dict[str, Any]:
        """POST /v1/chat/completions.

        Returns ``{"message": {"role": "assistant", "content": "..."}}`` — same
        shape as OllamaClient so all response-parsing code in handlers is unchanged.

        Pass ``format="json"`` to enable ``response_format={"type": "json_object"}``
        (Ollama + OpenAI both honour this for structured CoT/ReAct output).
        """
        opts = options or {}
        temperature: float = float(opts.get("temperature", 0.0))
        max_tokens: int = int(opts.get("num_ctx", DEFAULT_MAX_TOKENS))

        kwargs: dict[str, Any] = dict(
            model=model,
            messages=messages,  # type: ignore[arg-type]
            stream=False,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if format == "json":
            kwargs["response_format"] = {"type": "json_object"}

        completion = await self._chat_client.chat.completions.create(**kwargs)
        content = (completion.choices[0].message.content or "").strip()
        return {"message": {"role": "assistant", "content": content}}

    # ------------------------------------------------------------------
    # Embed — same signature as OllamaClient.embed()
    # ------------------------------------------------------------------

    async def embed(
        self,
        *,
        model: str,
        input: str | list[str],
        options: dict[str, Any] | None = None,  # accepted for compat, unused
        keep_alive: str | None = None,  # accepted for compat, unused
    ) -> dict[str, Any]:
        """POST /v1/embeddings.

        Returns ``{"embeddings": [[float, ...]]}`` — same shape as OllamaClient
        so ``_embedding_from_response`` in handlers is unchanged.
        """
        response = await self._embed_client.embeddings.create(
            model=model,
            input=input,
        )
        vectors = [d.embedding for d in response.data]
        return {"embeddings": vectors}
