"""Typed ToolRegistry: Pydantic input models, JSON Schema export, async invoke."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from pydantic import BaseModel

from workers.tool_observation import prepare_tool_return_for_llm

logger = logging.getLogger(__name__)

ToolHandler = Callable[[Any, Any], Awaitable[str]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    input_model: type[BaseModel]
    handler: ToolHandler


class ToolRegistry:
    """Đăng ký tool + model đầu vào; invoke chạy model_validate trước handler."""

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, name: str, input_model: type[BaseModel], handler: ToolHandler) -> None:
        if name in self._specs:
            raise ValueError(f"duplicate tool name: {name!r}")
        self._specs[name] = ToolSpec(name=name, input_model=input_model, handler=handler)

    def has(self, name: str) -> bool:
        return name in self._specs

    async def invoke(self, ctx: Any, name: str, raw_args: dict[str, Any]) -> str:
        spec = self._specs.get(name)
        if spec is None:
            raise KeyError(name)
        validated = spec.input_model.model_validate(raw_args)
        
        import hashlib
        import json
        import time
        READONLY_TOOLS = {"k8s_list_nodes", "k8s_node_conditions", "k8s_list_services", "k8s_list_ingress"}
        MUTATING_TOOLS = {
            "k8s_rollout_restart", "k8s_scale_deployment", "k8s_patch_resource",
            "kubectl_cluster",
            "sandbox_cleanup", "execute_in_sandbox", "gated_allowlisted_execute",
            "execute_shell_command",
        }
        
        is_readonly = name in READONLY_TOOLS
        is_mutating = name in MUTATING_TOOLS
        force_refresh_flag = raw_args.get("force_refresh", False)
        k8s_mutated_flag = getattr(ctx, "k8s_mutated", False)
        stable_id = getattr(ctx, "inbound_trace_id", "")
        
        cache_key = ""
        idempotency_key = ""
        r_client = getattr(ctx, "redis", None)
        
        if is_mutating and r_client is not None and stable_id:
            idempotency_ttl = getattr(getattr(ctx, "settings", None), "idempotency_ttl_sec", 120)
            idempotency_key = f"omni:tool_executed:{name}:{stable_id}"
            # Atomic PENDING Lock
            acquired = await r_client.set(idempotency_key, "pending", nx=True, ex=idempotency_ttl)
            if not acquired:
                logger.warning("[IDEMPOTENCY_GUARD] Tool %s with trace_id %s already executed/pending.", name, stable_id)
                return f"[IDEMPOTENCY_GUARD] Tool {name} is already executed or pending for stable_id={stable_id}. Skipped to prevent double-execution."
        
        if is_readonly and r_client is not None:
            cache_raw = {k: v for k, v in raw_args.items() if k != "force_refresh"}
            args_digest = hashlib.sha256(json.dumps(cache_raw, sort_keys=True).encode()).hexdigest()
            cache_key = f"omni:cache:tool:{name}:{args_digest}"
            
            if not force_refresh_flag and not k8s_mutated_flag:
                try:
                    cached_val = await r_client.get(cache_key)
                    if cached_val:
                        return cached_val
                except Exception:
                    pass
                    
        raw = await spec.handler(ctx, validated)
        output = prepare_tool_return_for_llm(ctx, str(raw))
        
        if is_readonly and r_client is not None and cache_key:
            if "[DATA] api_error" not in output and "[DATA] error" not in output:
                ttl = getattr(getattr(ctx, "settings", None), "omni_readonly_tool_cache_ttl_sec", 300)
                try:
                    await r_client.setex(cache_key, ttl, output)
                except Exception:
                    pass
                    
        if is_mutating and r_client is not None and idempotency_key:
            if "[DATA] error" not in output and "[DATA] api_error" not in output:
                idempotency_ttl = getattr(getattr(ctx, "settings", None), "idempotency_ttl_sec", 120)
                try:
                    await r_client.setex(idempotency_key, idempotency_ttl, "success")
                    # Ghi Sổ Cái (Audit Ledger)
                    reasoning = getattr(ctx, "inbound_reasoning", "No reasoning provided.")
                    audit_payload = {
                        "trace_id": stable_id,
                        "tool_name": name,
                        "args": json.dumps(raw_args, ensure_ascii=False),
                        "reasoning_summary": reasoning,
                        "timestamp": time.time(),
                        "status": "success"
                    }
                    await r_client.xadd("events:audit", {"data": json.dumps(audit_payload, ensure_ascii=False)})
                except Exception as e:
                    logger.error("[AUDIT] Failed to write ledger or update idempotency lock: %s", e)

        return output

    def json_schema_for(self, name: str) -> dict[str, Any]:
        spec = self._specs.get(name)
        if spec is None:
            raise KeyError(name)
        return spec.input_model.model_json_schema()

    def all_schemas_json(self) -> str:
        """Compact JSON string — tool name → JSON Schema."""
        out: dict[str, Any] = {}
        for name, spec in sorted(self._specs.items()):
            out[name] = spec.input_model.model_json_schema()
        return json.dumps(out, ensure_ascii=False, separators=(",", ":"))

    def list_tool_schemas(self) -> dict[str, Any]:
        """Tool name → JSON Schema dict (prompt / OpenAI-style tool list)."""
        return {name: self.json_schema_for(name) for name in sorted(self._specs.keys())}

    def tools_json_for_prompt(self, max_chars: int | None = None) -> str:
        """Chuỗi JSON gọn cho system prompt; cắt theo max_chars nếu có."""
        s = self.all_schemas_json()
        if max_chars is not None and len(s) > max_chars:
            return s[: max_chars - 1] + "…"
        return s

    def tool_names(self) -> frozenset[str]:
        return frozenset(self._specs.keys())


_GLOBAL = ToolRegistry()


def get_tool_registry() -> ToolRegistry:
    return _GLOBAL


def register_tool(name: str, input_model: type[BaseModel]):
    """Decorator: gắn handler async(ctx, validated_model) vào registry global."""

    def decorator(fn: ToolHandler) -> ToolHandler:
        _GLOBAL.register(name, input_model, fn)
        return fn

    return decorator
