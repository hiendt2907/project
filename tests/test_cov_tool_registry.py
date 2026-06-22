"""
Coverage tests for workers.tool_registry.
Targets: register, has, invoke (readonly cache, mutating idempotency, audit ledger),
json_schema_for, all_schemas_json, list_tool_schemas, tools_json_for_prompt,
tool_names, metadata_for, list_tool_catalog, tool_catalog_json_for_prompt,
get_tool_registry, register_tool decorator.
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis.aioredis
import pytest
from pydantic import BaseModel

os.environ.setdefault("OMNI_KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
os.environ.setdefault("OMNI_ENV_MODE", "dev")

from workers.tool_registry import ToolRegistry, ToolSpec, get_tool_registry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _SimpleInput(BaseModel):
    namespace: str = "default"
    name: str = ""


class _MutatingInput(BaseModel):
    pod: str
    namespace: str = "default"


async def _simple_handler(ctx, validated) -> str:
    return f"ok:{validated.namespace}"


async def _mutating_handler(ctx, validated) -> str:
    return f"deleted:{validated.pod}"


async def _error_handler(ctx, validated) -> str:
    return "[DATA] error: something went wrong"


def _make_ctx(redis=None, kafka=None, **overrides):
    settings_defaults = {
        "omni_readonly_tool_cache_ttl_sec": 300,
        "idempotency_ttl_sec": 120,
        "kafka_topic_tool_audit": "omni-tool-audit",
    }
    settings_defaults.update(overrides.pop("settings_extra", {}))
    settings = SimpleNamespace(**settings_defaults)

    ctx = SimpleNamespace(
        settings=settings,
        redis=redis or fakeredis.aioredis.FakeRedis(decode_responses=True),
        kafka=kafka,
        inbound_trace_id="",
        inbound_reasoning="",
        k8s_mutated=False,
    )
    ctx.__dict__.update(overrides)
    return ctx


# ---------------------------------------------------------------------------
# register / has
# ---------------------------------------------------------------------------

class TestRegister:
    def test_register_and_has(self):
        reg = ToolRegistry()
        reg.register("my_tool", _SimpleInput, _simple_handler)
        assert reg.has("my_tool")
        assert not reg.has("nonexistent")

    def test_duplicate_register_raises(self):
        reg = ToolRegistry()
        reg.register("tool_dup", _SimpleInput, _simple_handler)
        with pytest.raises(ValueError, match="duplicate tool name"):
            reg.register("tool_dup", _SimpleInput, _simple_handler)

    def test_register_with_metadata(self):
        reg = ToolRegistry()
        reg.register("meta_tool", _SimpleInput, _simple_handler, metadata={"capability": "readonly"})
        assert reg.metadata_for("meta_tool")["capability"] == "readonly"

    def test_register_none_metadata_defaults_to_empty_dict(self):
        reg = ToolRegistry()
        reg.register("no_meta", _SimpleInput, _simple_handler, metadata=None)
        assert reg.metadata_for("no_meta") == {}


# ---------------------------------------------------------------------------
# invoke — basic
# ---------------------------------------------------------------------------

class TestInvokeBasic:
    @pytest.mark.asyncio
    async def test_invoke_unknown_tool_raises_key_error(self):
        reg = ToolRegistry()
        ctx = _make_ctx()
        with pytest.raises(KeyError):
            await reg.invoke(ctx, "nonexistent", {})

    @pytest.mark.asyncio
    async def test_invoke_calls_handler_and_returns_output(self):
        reg = ToolRegistry()
        reg.register("simple", _SimpleInput, _simple_handler)
        ctx = _make_ctx(redis=None)
        ctx.redis = None
        result = await reg.invoke(ctx, "simple", {"namespace": "test-ns"})
        assert "test-ns" in result

    @pytest.mark.asyncio
    async def test_invoke_validates_input_model(self):
        reg = ToolRegistry()

        async def _strict_handler(ctx, validated):
            return f"ns:{validated.namespace}"

        reg.register("strict", _SimpleInput, _strict_handler)
        ctx = _make_ctx(redis=None)
        ctx.redis = None
        result = await reg.invoke(ctx, "strict", {"namespace": "prod", "extra_field": "ignored"})
        assert "prod" in result


# ---------------------------------------------------------------------------
# invoke — readonly cache
# ---------------------------------------------------------------------------

class TestInvokeReadonlyCache:
    @pytest.mark.asyncio
    async def test_readonly_tool_caches_result(self):
        reg = ToolRegistry()
        reg.register("k8s_list_nodes", _SimpleInput, _simple_handler)

        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis=redis)

        result1 = await reg.invoke(ctx, "k8s_list_nodes", {"namespace": "ns1"})
        result2 = await reg.invoke(ctx, "k8s_list_nodes", {"namespace": "ns1"})
        assert result1 == result2

    @pytest.mark.asyncio
    async def test_readonly_cache_hit_returns_cached(self):
        reg = ToolRegistry()
        call_count = 0

        async def _counting_handler(ctx, validated):
            nonlocal call_count
            call_count += 1
            return f"result-{call_count}"

        reg.register("k8s_list_nodes", _SimpleInput, _counting_handler)
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis=redis)

        await reg.invoke(ctx, "k8s_list_nodes", {"namespace": "ns"})
        await reg.invoke(ctx, "k8s_list_nodes", {"namespace": "ns"})

        assert call_count == 1  # second call served from cache

    @pytest.mark.asyncio
    async def test_force_refresh_bypasses_cache(self):
        reg = ToolRegistry()
        call_count = 0

        async def _counting_handler(ctx, validated):
            nonlocal call_count
            call_count += 1
            return f"result-{call_count}"

        reg.register("k8s_list_nodes", _SimpleInput, _counting_handler)
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis=redis)

        await reg.invoke(ctx, "k8s_list_nodes", {"namespace": "ns"})
        await reg.invoke(ctx, "k8s_list_nodes", {"namespace": "ns", "force_refresh": True})

        assert call_count == 2

    @pytest.mark.asyncio
    async def test_k8s_mutated_bypasses_cache(self):
        reg = ToolRegistry()
        call_count = 0

        async def _counting_handler(ctx, validated):
            nonlocal call_count
            call_count += 1
            return f"result-{call_count}"

        reg.register("k8s_list_nodes", _SimpleInput, _counting_handler)
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis=redis)
        ctx.k8s_mutated = True

        await reg.invoke(ctx, "k8s_list_nodes", {"namespace": "ns"})
        await reg.invoke(ctx, "k8s_list_nodes", {"namespace": "ns"})

        assert call_count == 2

    @pytest.mark.asyncio
    async def test_readonly_error_output_not_cached(self):
        reg = ToolRegistry()
        call_count = 0

        async def _error_counting_handler(ctx, validated):
            nonlocal call_count
            call_count += 1
            return "[DATA] api_error: connection refused"

        reg.register("k8s_list_nodes", _SimpleInput, _error_counting_handler)
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis=redis)

        await reg.invoke(ctx, "k8s_list_nodes", {"namespace": "ns"})
        await reg.invoke(ctx, "k8s_list_nodes", {"namespace": "ns"})

        assert call_count == 2  # error output not cached

    @pytest.mark.asyncio
    async def test_readonly_no_redis_skips_cache(self):
        reg = ToolRegistry()

        async def _handler(ctx, validated):
            return "data"

        reg.register("k8s_list_nodes", _SimpleInput, _handler)
        ctx = _make_ctx()
        ctx.redis = None  # no Redis

        result = await reg.invoke(ctx, "k8s_list_nodes", {"namespace": "ns"})
        assert result == "data"


# ---------------------------------------------------------------------------
# invoke — mutating idempotency
# ---------------------------------------------------------------------------

class TestInvokeMutatingIdempotency:
    @pytest.mark.asyncio
    async def test_mutating_idempotency_prevents_double_execution(self):
        reg = ToolRegistry()
        call_count = 0

        async def _mut_handler(ctx, validated):
            nonlocal call_count
            call_count += 1
            return "deleted"

        reg.register("k8s_delete_pod", _MutatingInput, _mut_handler, metadata={"capability": "mutate"})
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis=redis)
        ctx.inbound_trace_id = "trace-abc"

        result1 = await reg.invoke(ctx, "k8s_delete_pod", {"pod": "bad-pod"})
        result2 = await reg.invoke(ctx, "k8s_delete_pod", {"pod": "bad-pod"})

        assert call_count == 1
        assert "IDEMPOTENCY_GUARD" in result2

    @pytest.mark.asyncio
    async def test_mutating_no_trace_id_skips_idempotency(self):
        reg = ToolRegistry()
        call_count = 0

        async def _mut_handler(ctx, validated):
            nonlocal call_count
            call_count += 1
            return "deleted"

        reg.register("k8s_delete_pod", _MutatingInput, _mut_handler, metadata={"capability": "mutate"})
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis=redis)
        ctx.inbound_trace_id = ""  # no trace id → skip idempotency

        await reg.invoke(ctx, "k8s_delete_pod", {"pod": "pod1"})
        await reg.invoke(ctx, "k8s_delete_pod", {"pod": "pod1"})

        assert call_count == 2

    @pytest.mark.asyncio
    async def test_legacy_mutating_tool_triggers_idempotency(self):
        reg = ToolRegistry()
        call_count = 0

        async def _handler(ctx, validated):
            nonlocal call_count
            call_count += 1
            return "done"

        reg.register("k8s_rollout_restart", _SimpleInput, _handler)
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis=redis)
        ctx.inbound_trace_id = "trace-legacy"

        await reg.invoke(ctx, "k8s_rollout_restart", {"namespace": "prod"})
        result2 = await reg.invoke(ctx, "k8s_rollout_restart", {"namespace": "prod"})

        assert call_count == 1
        assert "IDEMPOTENCY_GUARD" in result2

    @pytest.mark.asyncio
    async def test_mutating_error_output_does_not_update_idempotency_to_success(self):
        reg = ToolRegistry()

        async def _err_handler(ctx, validated):
            return "[DATA] error: pod not found"

        reg.register("k8s_delete_pod", _MutatingInput, _err_handler, metadata={"capability": "mutate"})
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis=redis)
        ctx.inbound_trace_id = "trace-err"

        result = await reg.invoke(ctx, "k8s_delete_pod", {"pod": "missing-pod"})
        # On error the lock must NOT flip to "success"; it is released (deleted) so the
        # failed mutate can be retried instead of being permanently blocked (idempotency-leak fix).
        key = "omni:tool_executed:k8s_delete_pod:trace-err"
        val = await redis.get(key)
        assert val != "success"
        assert val is None  # released for retry

    @pytest.mark.asyncio
    async def test_mutating_success_writes_audit_to_kafka(self):
        reg = ToolRegistry()

        async def _handler(ctx, validated):
            return "deleted"

        reg.register("k8s_delete_pod", _MutatingInput, _handler, metadata={"capability": "mutate"})

        kafka_captures = []

        async def _send_dict(topic, msg):
            kafka_captures.append({"topic": topic, "msg": msg})

        kafka = MagicMock()
        kafka.send_dict = AsyncMock(side_effect=_send_dict)

        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis=redis, kafka=kafka)
        ctx.inbound_trace_id = "trace-audit"
        ctx.inbound_reasoning = "pod was rogue"

        await reg.invoke(ctx, "k8s_delete_pod", {"pod": "rogue-pod"})

        audit_msgs = [m for m in kafka_captures if m["topic"] == "omni-tool-audit"]
        assert len(audit_msgs) == 1
        raw_data = audit_msgs[0]["msg"]["data"]
        audit_data = json.loads(raw_data) if isinstance(raw_data, str) else raw_data
        assert audit_data["trace_id"] == "trace-audit"
        assert audit_data["status"] == "success"

    @pytest.mark.asyncio
    async def test_mutating_no_kafka_no_audit_no_error(self):
        reg = ToolRegistry()

        async def _handler(ctx, validated):
            return "ok"

        reg.register("k8s_delete_pod", _MutatingInput, _handler, metadata={"capability": "mutate"})
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis=redis, kafka=None)
        ctx.inbound_trace_id = "trace-no-kafka"

        # Should not raise even without Kafka
        result = await reg.invoke(ctx, "k8s_delete_pod", {"pod": "pod1"})
        assert result == "ok"


# ---------------------------------------------------------------------------
# Schema methods
# ---------------------------------------------------------------------------

class TestSchemaMethods:
    def test_json_schema_for_returns_schema(self):
        reg = ToolRegistry()
        reg.register("schema_tool", _SimpleInput, _simple_handler)
        schema = reg.json_schema_for("schema_tool")
        assert "properties" in schema

    def test_json_schema_for_unknown_raises(self):
        reg = ToolRegistry()
        with pytest.raises(KeyError):
            reg.json_schema_for("nonexistent")

    def test_all_schemas_json_returns_valid_json(self):
        reg = ToolRegistry()
        reg.register("tool_a", _SimpleInput, _simple_handler)
        reg.register("tool_b", _MutatingInput, _mutating_handler)
        s = reg.all_schemas_json()
        parsed = json.loads(s)
        assert "tool_a" in parsed
        assert "tool_b" in parsed

    def test_list_tool_schemas_returns_dict(self):
        reg = ToolRegistry()
        reg.register("ts_tool", _SimpleInput, _simple_handler)
        schemas = reg.list_tool_schemas()
        assert "ts_tool" in schemas

    def test_tools_json_for_prompt_no_limit(self):
        reg = ToolRegistry()
        reg.register("prompt_tool", _SimpleInput, _simple_handler)
        result = reg.tools_json_for_prompt()
        assert "prompt_tool" in result

    def test_tools_json_for_prompt_truncated(self):
        import json as _json
        reg = ToolRegistry()
        reg.register("bigschema", _SimpleInput, _simple_handler)
        result = reg.tools_json_for_prompt(max_chars=5)
        # Truncation drops whole entries → output is ALWAYS valid JSON (never cut
        # mid-string, never a trailing "…"). A tool too big to fit is dropped entirely.
        parsed = _json.loads(result)
        assert isinstance(parsed, dict)
        assert "bigschema" not in parsed

    def test_tool_names_returns_frozenset(self):
        reg = ToolRegistry()
        reg.register("tool_x", _SimpleInput, _simple_handler)
        reg.register("tool_y", _MutatingInput, _mutating_handler)
        names = reg.tool_names()
        assert isinstance(names, frozenset)
        assert "tool_x" in names
        assert "tool_y" in names

    def test_metadata_for_unknown_raises(self):
        reg = ToolRegistry()
        with pytest.raises(KeyError):
            reg.metadata_for("nonexistent")

    def test_list_tool_catalog_structure(self):
        reg = ToolRegistry()
        reg.register("cat_tool", _SimpleInput, _simple_handler, metadata={"cap": "read"})
        catalog = reg.list_tool_catalog()
        assert "cat_tool" in catalog
        entry = catalog["cat_tool"]
        assert "args_schema" in entry
        assert "metadata" in entry
        assert entry["metadata"]["cap"] == "read"

    def test_tool_catalog_json_for_prompt_no_limit(self):
        reg = ToolRegistry()
        reg.register("cj_tool", _SimpleInput, _simple_handler)
        result = reg.tool_catalog_json_for_prompt()
        assert "cj_tool" in result

    def test_tool_catalog_json_for_prompt_truncated(self):
        import json as _json
        reg = ToolRegistry()
        reg.register("cj_tool", _SimpleInput, _simple_handler)
        result = reg.tool_catalog_json_for_prompt(max_chars=5)
        # Whole-entry drop → valid JSON, no trailing "…"; oversized tool is omitted.
        parsed = _json.loads(result)
        assert isinstance(parsed, dict)
        assert "cj_tool" not in parsed


# ---------------------------------------------------------------------------
# get_tool_registry / register_tool decorator
# ---------------------------------------------------------------------------

def test_get_tool_registry_returns_singleton():
    r1 = get_tool_registry()
    r2 = get_tool_registry()
    assert r1 is r2


def test_register_tool_decorator_registers_in_global():
    from workers.tool_registry import register_tool, get_tool_registry

    class _Input(BaseModel):
        val: int = 0

    unique_name = "decorator_test_tool_unique_42"
    registry = get_tool_registry()

    if not registry.has(unique_name):
        @register_tool(unique_name, _Input)
        async def _handler(ctx, validated):
            return "decorated"

    assert registry.has(unique_name)
