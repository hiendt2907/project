"""Async Ollama HTTP client — chat + embed with num_ctx=4096 on every request."""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel, Field, PrivateAttr

DEFAULT_NUM_CTX = 4096


class OllamaClient(BaseModel):
    """Routes through K8s Service DNS (same NS: service name only); never hardcode Pod IPs."""

    base_url: str = Field(
        default="http://ollama-service:11434",
        description=(
            "Must equal Service metadata.name + port (repo default: ollama-service:11434 per .cursorrules). "
            "Override with env if your Service name differs. Cross-NS: http://svc.otherns:11434."
        ),
    )
    timeout_s: float = Field(default=120.0, ge=1.0)

    model_config = {"arbitrary_types_allowed": True}

    _client: httpx.AsyncClient = PrivateAttr()

    def model_post_init(self, __context: Any) -> None:
        base = self.base_url.rstrip("/")
        self._client = httpx.AsyncClient(base_url=base, timeout=self.timeout_s)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _options(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        opts: dict[str, Any] = dict(extra or {})
        opts["num_ctx"] = DEFAULT_NUM_CTX
        return opts

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        stream: bool = False,
        options: dict[str, Any] | None = None,
        keep_alive: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/chat. `keep_alive` (vd \"5m\") giải phóng VRAM khi đổi model."""
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": stream,
            "options": self._options(options),
        }
        if keep_alive is not None:
            body["keep_alive"] = keep_alive
        r = await self._client.post("/api/chat", json=body)
        r.raise_for_status()
        return r.json()

    async def embed(
        self,
        *,
        model: str,
        input: str | list[str],
        options: dict[str, Any] | None = None,
        keep_alive: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/embed (e.g. nomic-embed-text)."""
        body: dict[str, Any] = {
            "model": model,
            "input": input,
            "options": self._options(options),
        }
        if keep_alive is not None:
            body["keep_alive"] = keep_alive
        r = await self._client.post("/api/embed", json=body)
        r.raise_for_status()
        return r.json()
