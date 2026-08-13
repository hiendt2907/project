"""Typed ToolRegistry: Pydantic input models, JSON Schema export, async invoke."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from pydantic import BaseModel

from workers.tool_observation import prepare_tool_return_for_llm
from workers.tool_output_status import classify_tool_output

logger = logging.getLogger(__name__)

# Legacy fallback sets — prefer spec.metadata["capability"] ("readonly"/"mutate").
READONLY_TOOLS = frozenset(
    {"k8s_list_nodes", "k8s_node_conditions", "k8s_list_services", "k8s_list_ingress"}
)
LEGACY_MUTATING_TOOLS = frozenset(
    {
        "k8s_rollout_restart",
        "k8s_scale_deployment",
        "k8s_scale_resource",
        "k8s_patch_resource",
        "k8s_delete_pod",
        "kubectl_cluster",
        "sandbox_cleanup",
        "execute_in_sandbox",
        "gated_allowlisted_execute",
        "execute_shell_command",
    }
)

_TOOL_CACHE_EPOCH_KEY = "omni:cache:tool_epoch"

ToolHandler = Callable[[Any, Any], Awaitable[str]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    input_model: type[BaseModel]
    handler: ToolHandler
    metadata: dict[str, Any]


class ToolRegistry:
    """Đăng ký tool + model đầu vào; invoke chạy model_validate trước handler."""

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(
        self,
        name: str,
        input_model: type[BaseModel],
        handler: ToolHandler,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if name in self._specs:
            raise ValueError(f"duplicate tool name: {name!r}")
        self._specs[name] = ToolSpec(
            name=name,
            input_model=input_model,
            handler=handler,
            metadata=dict(metadata or {}),
        )

    def has(self, name: str) -> bool:
        return name in self._specs

    async def invoke(self, ctx: Any, name: str, raw_args: dict[str, Any]) -> str:
        spec = self._specs.get(name)
        if spec is None:
            raise KeyError(name)
        validated = spec.input_model.model_validate(raw_args)

        capability = str(spec.metadata.get("capability") or "").strip().lower()
        is_readonly = capability == "readonly" or name in READONLY_TOOLS
        is_mutating = capability == "mutate" or name in LEGACY_MUTATING_TOOLS
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
            try:
                epoch = await r_client.get(_TOOL_CACHE_EPOCH_KEY) or "0"
            except Exception:
                epoch = "0"
            cache_key = f"omni:cache:tool:e{epoch}:{name}:{args_digest}"
            
            if not force_refresh_flag and not k8s_mutated_flag:
                try:
                    cached_val = await r_client.get(cache_key)
                    if cached_val:
                        return cached_val
                except Exception:
                    pass
                    
        try:
            raw = await spec.handler(ctx, validated)
        except Exception as exc:
            # Handler never completed — release the PENDING idempotency lock so a
            # legitimate retry is not refused for the rest of the TTL window.
            if is_mutating and r_client is not None and idempotency_key:
                try:
                    await r_client.delete(idempotency_key)
                except Exception:
                    logger.warning("[IDEMPOTENCY_GUARD] failed to release lock %s", idempotency_key)
            logger.warning("[TOOL_ERROR] handler %s raised %s: %s", name, type(exc).__name__, exc)
            return prepare_tool_return_for_llm(
                ctx,
                f"[DATA] error\n[DIAGNOSIS] tool={name} exception={type(exc).__name__}: {str(exc)[:300]}\n"
                "[NEXT] Re-check args against the tool schema or pick an alternative tool; do not retry identical args.",
            )
        output = prepare_tool_return_for_llm(ctx, str(raw))
        # Trước đây chỉ khớp đúng 2 chuỗi "[DATA] error"/"[DATA] api_error" — lọt token
        # thất bại thật khác (VD "deployment_not_found") khiến idempotency lock không
        # được nhả ra để retry. classify_tool_output() là nguồn phân loại thật duy nhất,
        # dùng chung với proactive_observer._quick_verify_output.
        is_error_output = classify_tool_output(output) == "fail"

        if is_mutating and r_client is not None and idempotency_key and is_error_output:
            # Tool reported failure — no mutation happened; unlock for retry.
            try:
                await r_client.delete(idempotency_key)
            except Exception:
                logger.warning("[IDEMPOTENCY_GUARD] failed to release lock %s", idempotency_key)
        
        if is_readonly and r_client is not None and cache_key:
            if not is_error_output:
                ttl = getattr(getattr(ctx, "settings", None), "omni_readonly_tool_cache_ttl_sec", 300)
                try:
                    await r_client.setex(cache_key, ttl, output)
                except Exception:
                    pass
                    
        if is_mutating and r_client is not None and idempotency_key:
            if not is_error_output:
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
                    kbus = getattr(ctx, "kafka", None)
                    if kbus is not None:
                        await kbus.send_dict(
                            getattr(ctx.settings, "kafka_topic_tool_audit", "omni-tool-audit"),
                            {"data": json.dumps(audit_payload, ensure_ascii=False)},
                        )
                except Exception as e:
                    logger.error("[AUDIT] Failed to write ledger or update idempotency lock: %s", e)

        if is_mutating and r_client is not None and not is_error_output:
            # Bump the cache epoch — all readonly tool caches from before this
            # mutation are invalidated across every trace.
            try:
                await r_client.incr(_TOOL_CACHE_EPOCH_KEY)
            except Exception:
                logger.warning("[CACHE_EPOCH] failed to incr %s", _TOOL_CACHE_EPOCH_KEY)

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

    @staticmethod
    def _json_fit_whole_entries(entries: dict[str, Any], max_chars: int | None) -> str:
        """Serialize dict; nếu vượt max_chars thì DROP nguyên entry cuối cùng —
        output luôn là JSON hợp lệ (không bao giờ cắt giữa chuỗi)."""
        s = json.dumps(entries, ensure_ascii=False, separators=(",", ":"))
        if max_chars is None or len(s) <= max_chars:
            return s
        kept: dict[str, Any] = {}
        for name, val in entries.items():
            candidate = {**kept, name: val}
            cs = json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
            if len(cs) > max_chars:
                logger.warning(
                    "tool catalog truncated: dropped %r and later tools (max_chars=%d)",
                    name, max_chars,
                )
                break
            kept = candidate
        return json.dumps(kept, ensure_ascii=False, separators=(",", ":"))

    def tools_json_for_prompt(self, max_chars: int | None = None) -> str:
        """Chuỗi JSON gọn cho system prompt; vượt max_chars → drop nguyên tool."""
        out: dict[str, Any] = {}
        for name, spec in sorted(self._specs.items()):
            out[name] = spec.input_model.model_json_schema()
        return self._json_fit_whole_entries(out, max_chars)

    def tool_names(self) -> frozenset[str]:
        return frozenset(self._specs.keys())

    def metadata_for(self, name: str) -> dict[str, Any]:
        spec = self._specs.get(name)
        if spec is None:
            raise KeyError(name)
        return dict(spec.metadata)

    def list_tool_catalog(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, spec in sorted(self._specs.items()):
            out[name] = {
                "args_schema": spec.input_model.model_json_schema(),
                "metadata": dict(spec.metadata),
            }
        return out

    def tool_catalog_json_for_prompt(self, max_chars: int | None = None) -> str:
        # Mutate/remediation tools MUST survive truncation — emit them first so that
        # _json_fit_whole_entries only ever drops trailing read-only tools. Otherwise
        # alphabetical ordering silently drops k8s_scale_deployment et al. and the
        # agentic planner cannot propose the correct remediation.
        catalog = self.list_tool_catalog()
        mutate_first: dict[str, Any] = {}
        for name, val in catalog.items():
            cap = str((val.get("metadata") or {}).get("capability") or "")
            if cap == "mutate" or name in LEGACY_MUTATING_TOOLS:
                mutate_first[name] = val
        for name, val in catalog.items():
            if name not in mutate_first:
                mutate_first[name] = val
        return self._json_fit_whole_entries(mutate_first, max_chars)


_GLOBAL = ToolRegistry()


def get_tool_registry() -> ToolRegistry:
    return _GLOBAL


def register_tool(name: str, input_model: type[BaseModel], *, metadata: dict[str, Any] | None = None):
    """Decorator: gắn handler async(ctx, validated_model) vào registry global."""

    def decorator(fn: ToolHandler) -> ToolHandler:
        _GLOBAL.register(name, input_model, fn, metadata=metadata)
        return fn

    return decorator
