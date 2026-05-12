"""Abstract LLM transport — injectable in tests via ``build_llm_client``."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class LlmClient(Protocol):
    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        stream: bool = False,
        options: dict[str, Any] | None = None,
        format: str | None = None,
        keep_alive: str | None = None,
        llm_call_kind: str | None = None,
    ) -> dict[str, Any]: ...

    async def chat_plain(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        stream: bool = False,
        options: dict[str, Any] | None = None,
        keep_alive: str | None = None,
    ) -> dict[str, Any]: ...

    async def chat_structured(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        stream: bool = False,
        options: dict[str, Any] | None = None,
        keep_alive: str | None = None,
    ) -> dict[str, Any]: ...

    async def embed(
        self,
        *,
        model: str,
        input: str | list[str],
        options: dict[str, Any] | None = None,
        keep_alive: str | None = None,
    ) -> dict[str, Any]: ...

    async def aclose(self) -> None: ...
