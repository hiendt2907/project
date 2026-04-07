"""Async Ollama HTTP client — chat + embed with num_ctx=4096 on every request."""

from __future__ import annotations

import logging
from typing import Any

import httpx
from pydantic import BaseModel, Field, PrivateAttr

logger = logging.getLogger(__name__)

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

    def model_post_init(self, _context: Any) -> None:
        base = self.base_url.rstrip("/")
        self._client = httpx.AsyncClient(base_url=base, timeout=self.timeout_s)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _options(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        opts: dict[str, Any] = dict(extra or {})
        opts["num_ctx"] = DEFAULT_NUM_CTX
        return opts

    @staticmethod
    def _messages_to_generate_payload(messages: list[dict[str, Any]]) -> tuple[str, str]:
        """Split chat messages into (system, prompt) for legacy ``POST /api/generate``."""
        sys_parts: list[str] = []
        user_parts: list[str] = []
        for m in messages:
            role = str(m.get("role") or "").strip()
            c = str(m.get("content") or "")
            if role == "system":
                sys_parts.append(c)
            elif role == "user":
                user_parts.append(c)
            elif role == "assistant":
                user_parts.append(f"[assistant]\n{c}")
        system = "\n\n".join(sys_parts).strip()
        prompt = "\n\n".join(user_parts).strip()
        return system, prompt

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        stream: bool = False,
        options: dict[str, Any] | None = None,
        keep_alive: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/chat. `keep_alive` (vd \"5m\") giải phóng VRAM khi đổi model.

        On **404** (very old Ollama without ``/api/chat``), retry once with ``POST /api/generate``
        (non-stream only).
        """
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": stream,
            "options": self._options(options),
        }
        if keep_alive is not None:
            body["keep_alive"] = keep_alive
        r = await self._client.post("/api/chat", json=body)
        if r.status_code == 404 and not stream:
            system, prompt = self._messages_to_generate_payload(messages)
            gen: dict[str, Any] = {
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": self._options(options),
            }
            if system:
                gen["system"] = system
            if keep_alive is not None:
                gen["keep_alive"] = keep_alive
            r = await self._client.post("/api/generate", json=gen)
        r.raise_for_status()
        j = r.json()
        if "message" not in j and "response" in j:
            return {"message": {"role": "assistant", "content": str(j.get("response") or "")}}
        return j

    async def embed(
        self,
        *,
        model: str,
        input: str | list[str],
        options: dict[str, Any] | None = None,
        keep_alive: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/embed (e.g. nomic-embed-text).

        If the server returns **404** (older Ollama without ``/api/embed``), retry once with
        legacy ``POST /api/embeddings`` and ``prompt`` (string input only).
        """
        body: dict[str, Any] = {
            "model": model,
            "input": input,
            "options": self._options(options),
        }
        if keep_alive is not None:
            body["keep_alive"] = keep_alive
        r = await self._client.post("/api/embed", json=body)
        if r.status_code == 400:
            ilen = len(input) if isinstance(input, str) else sum(len(x) for x in input) if isinstance(input, list) else 0
            logger.warning(
                "event=ollama_embed_400 status=400 model=%s input_len=%s",
                model,
                ilen,
            )
        if r.status_code == 404 and isinstance(input, str):
            legacy: dict[str, Any] = {
                "model": model,
                "prompt": input,
                "options": self._options(options),
            }
            if keep_alive is not None:
                legacy["keep_alive"] = keep_alive
            r = await self._client.post("/api/embeddings", json=legacy)
        r.raise_for_status()
        return r.json()
