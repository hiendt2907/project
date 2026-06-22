"""Lightweight LLM test double: `chat_structured` / `chat_plain` forward to `.chat`."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock


class CompatLLM:
    """Assign `llm.chat = AsyncMock(...)` once; structured/plain callers still work."""

    def __init__(self) -> None:
        self.chat = AsyncMock()

    async def chat_structured(self, **kwargs: Any) -> Any:
        return await self.chat(**kwargs)

    async def chat_plain(self, **kwargs: Any) -> Any:
        return await self.chat(**kwargs)

    async def embed(self, **kwargs: Any) -> Any:
        return {"embeddings": [[]]}

    async def aclose(self) -> None:
        return None
