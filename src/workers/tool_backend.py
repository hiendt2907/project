"""Tool execution backends — default in-process registry; MCP pilot reserved (see docs/mcp_integration.md)."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from workers.handlers import WorkerHandlerContext
from workers.tools import TOOL_REGISTRY


@runtime_checkable
class ToolBackend(Protocol):
    async def invoke(self, ctx: WorkerHandlerContext, name: str, args: dict[str, Any]) -> Any:
        """Execute tool ``name`` with JSON-like ``args``."""
        ...


class RegistryToolBackend:
    """Delegates to ``TOOL_REGISTRY`` (current production behavior)."""

    __slots__ = ("_registry",)

    def __init__(self, registry: dict[str, Any] | None = None) -> None:
        self._registry = registry if registry is not None else TOOL_REGISTRY

    async def invoke(self, ctx: WorkerHandlerContext, name: str, args: dict[str, Any]) -> Any:
        fn = self._registry[name]
        return await fn(ctx, args)
