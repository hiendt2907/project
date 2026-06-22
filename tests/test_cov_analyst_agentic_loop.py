"""
tests/test_cov_analyst_agentic_loop.py

Coverage tests for src/workers/analyst_agentic_loop.py (57.8% → improved).
Focuses on uncovered branches: helper functions, edge cases, async loops.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.llm_mock_compat import CompatLLM
# Helpers
# ---------------------------------------------------------------------------

def _make_settings(**kw: Any) -> SimpleNamespace:
    defaults: dict[str, Any] = {
        "god_mode": False,
        "chat_model": "qwen2.5:7b",
        "model_helper": "qwen2.5:1.5b",
        "model_reasoning_engine": "qwen2.5:7b",
        "model_heavy_lifter": "qwen2.5:7b",
        "diag_evidence_llm_model": "",
        "omni_diagnostic_react_enabled": False,
        "omni_diagnostic_react_readonly_max": 3,
        "omni_trace_memory_tool_output_max_chars": 4000,
        "omni_blind_lane_llm_enabled": False,
        "omni_planner_llm_sole_evaluator": False,
        "omni_shadow_os_mode": False,
        "omni_llm_first_autonomy_enabled": False,
        "omni_post_verify_state_llm_enabled": True,
        "omni_post_verify_react_max_steps": 3,
        "omni_post_verify_react_readonly_max": 4,
        "omni_post_mutate_state_verify_max_steps": 3,
        "tool_output_max_chars": 1500,
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _make_ctx(**kw: Any) -> SimpleNamespace:
    defaults: dict[str, Any] = {
        "settings": _make_settings(),
        "redis": None,
        "llm": None,
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# _normalize_describe_resource_type
# ---------------------------------------------------------------------------

class TestNormalizeDescribeResourceType:
    def test_pod_lowercase(self):
        from workers.analyst_agentic_loop import _normalize_describe_resource_type
        assert _normalize_describe_resource_type("pod") == "Pod"

    def test_pods_lowercase(self):
        from workers.analyst_agentic_loop import _normalize_describe_resource_type
        assert _normalize_describe_resource_type("pods") == "Pod"

    def test_deployment_lowercase(self):
        from workers.analyst_agentic_loop import _normalize_describe_resource_type
        assert _normalize_describe_resource_type("deployment") == "Deployment"

    def test_deployments_lowercase(self):
        from workers.analyst_agentic_loop import _normalize_describe_resource_type
        assert _normalize_describe_resource_type("deployments") == "Deployment"

    def test_service_lowercase(self):
        from workers.analyst_agentic_loop import _normalize_describe_resource_type
        assert _normalize_describe_resource_type("service") == "Service"

    def test_services_lowercase(self):
        from workers.analyst_agentic_loop import _normalize_describe_resource_type
        assert _normalize_describe_resource_type("services") == "Service"

    def test_configmap_lowercase(self):
        from workers.analyst_agentic_loop import _normalize_describe_resource_type
        assert _normalize_describe_resource_type("configmap") == "ConfigMap"

    def test_configmaps_lowercase(self):
        from workers.analyst_agentic_loop import _normalize_describe_resource_type
        assert _normalize_describe_resource_type("configmaps") == "ConfigMap"

    def test_secret_lowercase(self):
        from workers.analyst_agentic_loop import _normalize_describe_resource_type
        assert _normalize_describe_resource_type("secret") == "Secret"

    def test_secrets_lowercase(self):
        from workers.analyst_agentic_loop import _normalize_describe_resource_type
        assert _normalize_describe_resource_type("secrets") == "Secret"

    def test_exact_pascal_case_preserved(self):
        from workers.analyst_agentic_loop import _normalize_describe_resource_type
        assert _normalize_describe_resource_type("Pod") == "Pod"
        assert _normalize_describe_resource_type("Deployment") == "Deployment"
        assert _normalize_describe_resource_type("Service") == "Service"
        assert _normalize_describe_resource_type("ConfigMap") == "ConfigMap"
        assert _normalize_describe_resource_type("Secret") == "Secret"

    def test_none_returns_none(self):
        from workers.analyst_agentic_loop import _normalize_describe_resource_type
        assert _normalize_describe_resource_type(None) is None

    def test_empty_string_returns_none(self):
        from workers.analyst_agentic_loop import _normalize_describe_resource_type
        assert _normalize_describe_resource_type("") is None

    def test_whitespace_only_returns_none(self):
        from workers.analyst_agentic_loop import _normalize_describe_resource_type
        assert _normalize_describe_resource_type("   ") is None

    def test_unknown_string_returns_none(self):
        from workers.analyst_agentic_loop import _normalize_describe_resource_type
        assert _normalize_describe_resource_type("CronJob") is None


# ---------------------------------------------------------------------------
# coerce_k8s_readonly_args
# ---------------------------------------------------------------------------

class TestCoerceK8sReadonlyArgs:
    def test_deployment_state_renames_name_to_deployment(self):
        from workers.analyst_agentic_loop import coerce_k8s_readonly_args
        args = {"name": "nginx", "namespace": "default"}
        result = coerce_k8s_readonly_args("k8s_get_deployment_state", args)
        assert result["deployment"] == "nginx"
        assert "name" not in result

    def test_verify_rollout_renames_name(self):
        from workers.analyst_agentic_loop import coerce_k8s_readonly_args
        args = {"name": "api-server", "namespace": "prod"}
        result = coerce_k8s_readonly_args("k8s_verify_rollout", args)
        assert result["deployment"] == "api-server"

    def test_list_workload_pods_renames_name(self):
        from workers.analyst_agentic_loop import coerce_k8s_readonly_args
        args = {"name": "worker", "namespace": "multi-agent"}
        result = coerce_k8s_readonly_args("k8s_list_workload_pods", args)
        assert result["deployment"] == "worker"

    def test_deployment_state_with_existing_deployment_no_rename(self):
        from workers.analyst_agentic_loop import coerce_k8s_readonly_args
        args = {"deployment": "nginx", "namespace": "default"}
        result = coerce_k8s_readonly_args("k8s_get_deployment_state", args)
        assert result["deployment"] == "nginx"

    def test_describe_resource_with_resource_type_preserved(self):
        from workers.analyst_agentic_loop import coerce_k8s_readonly_args
        args = {"resource_type": "ConfigMap", "name": "my-cm", "namespace": "default"}
        result = coerce_k8s_readonly_args("k8s_describe_resource", args)
        assert result["resource_type"] == "ConfigMap"

    def test_describe_resource_normalizes_kind_field(self):
        from workers.analyst_agentic_loop import coerce_k8s_readonly_args
        args = {"kind": "deployment", "name": "api", "namespace": "prod"}
        result = coerce_k8s_readonly_args("k8s_describe_resource", args)
        assert result.get("resource_type") == "Deployment"
        assert "kind" not in result

    def test_describe_resource_with_resource_kind_field(self):
        from workers.analyst_agentic_loop import coerce_k8s_readonly_args
        args = {"resource_kind": "service", "name": "my-svc", "namespace": "prod"}
        result = coerce_k8s_readonly_args("k8s_describe_resource", args)
        assert result.get("resource_type") == "Service"

    def test_non_k8s_tool_passthrough(self):
        from workers.analyst_agentic_loop import coerce_k8s_readonly_args
        args = {"query": "up", "label": "pod"}
        result = coerce_k8s_readonly_args("promql_instant", args)
        assert result == args

    def test_describe_resource_with_invalid_resource_type_fallback(self):
        from workers.analyst_agentic_loop import coerce_k8s_readonly_args
        args = {"resource_type": "UnknownKind", "name": "x", "namespace": "ns"}
        result = coerce_k8s_readonly_args("k8s_describe_resource", args)
        # No valid normalization, resource_type stays as-is (original value)
        assert "name" in result

    def test_describe_resource_api_kind_field(self):
        from workers.analyst_agentic_loop import coerce_k8s_readonly_args
        args = {"api_kind": "pod", "name": "my-pod", "namespace": "default"}
        result = coerce_k8s_readonly_args("k8s_describe_resource", args)
        assert result.get("resource_type") == "Pod"


# ---------------------------------------------------------------------------
# _readonly_tool_router
# ---------------------------------------------------------------------------

class TestReadonlyToolRouter:
    def test_readonly_tool_returns_true(self):
        from workers.analyst_agentic_loop import _readonly_tool_router
        # k8s_get_deployment_state is in READONLY_TOOL_ALLOWLIST
        assert _readonly_tool_router("k8s_get_deployment_state") is True

    def test_empty_string_returns_false(self):
        from workers.analyst_agentic_loop import _readonly_tool_router
        assert _readonly_tool_router("") is False

    def test_mutate_tool_returns_false(self):
        from workers.analyst_agentic_loop import _readonly_tool_router
        assert _readonly_tool_router("k8s_rollout_restart") is False

    def test_whitespace_returns_false(self):
        from workers.analyst_agentic_loop import _readonly_tool_router
        assert _readonly_tool_router("   ") is False


# ---------------------------------------------------------------------------
# _discovery_repeat_dedupe_key
# ---------------------------------------------------------------------------

class TestDiscoveryRepeatDedupeKey:
    def test_same_args_same_key(self):
        from workers.analyst_agentic_loop import _discovery_repeat_dedupe_key
        k1 = _discovery_repeat_dedupe_key("k8s_get_deployment_state", {"namespace": "default"})
        k2 = _discovery_repeat_dedupe_key("k8s_get_deployment_state", {"namespace": "default"})
        assert k1 == k2

    def test_different_args_different_key(self):
        from workers.analyst_agentic_loop import _discovery_repeat_dedupe_key
        k1 = _discovery_repeat_dedupe_key("k8s_get_deployment_state", {"namespace": "default"})
        k2 = _discovery_repeat_dedupe_key("k8s_get_deployment_state", {"namespace": "production"})
        assert k1 != k2

    def test_different_tools_different_key(self):
        from workers.analyst_agentic_loop import _discovery_repeat_dedupe_key
        k1 = _discovery_repeat_dedupe_key("tool_a", {"arg": "val"})
        k2 = _discovery_repeat_dedupe_key("tool_b", {"arg": "val"})
        assert k1 != k2

    def test_empty_args_stable(self):
        from workers.analyst_agentic_loop import _discovery_repeat_dedupe_key
        k1 = _discovery_repeat_dedupe_key("some_tool", {})
        k2 = _discovery_repeat_dedupe_key("some_tool", {})
        assert k1 == k2


# ---------------------------------------------------------------------------
# _is_pod_scoped_readonly_request
# ---------------------------------------------------------------------------

class TestIsPodScopedReadonlyRequest:
    def test_k8s_get_logs_is_pod_scoped(self):
        from workers.analyst_agentic_loop import _is_pod_scoped_readonly_request
        assert _is_pod_scoped_readonly_request("k8s_get_logs", {}) is True

    def test_k8s_get_pod_log_tail_is_pod_scoped(self):
        from workers.analyst_agentic_loop import _is_pod_scoped_readonly_request
        assert _is_pod_scoped_readonly_request("k8s_get_pod_log_tail", {}) is True

    def test_k8s_get_pod_log_previous_is_pod_scoped(self):
        from workers.analyst_agentic_loop import _is_pod_scoped_readonly_request
        assert _is_pod_scoped_readonly_request("k8s_get_pod_log_previous", {}) is True

    def test_k8s_get_pod_secret_refs_is_pod_scoped(self):
        from workers.analyst_agentic_loop import _is_pod_scoped_readonly_request
        assert _is_pod_scoped_readonly_request("k8s_get_pod_secret_refs", {}) is True

    def test_k8s_get_events_with_pod_involved_name_is_pod_scoped(self):
        from workers.analyst_agentic_loop import _is_pod_scoped_readonly_request
        args = {"involved_name": "my-pod-abc", "involved_kind": "pod"}
        assert _is_pod_scoped_readonly_request("k8s_get_events", args) is True

    def test_qwen_hallucinated_pod_log_aliases_are_pod_scoped(self):
        # qwen2.5-coder emits k8s_get_pod_logs etc. — must redirect to workload probe,
        # not dead-cycle ERR_REA_HALLUCINATION_DETECTED on an unknown tool name.
        from workers.analyst_agentic_loop import _is_pod_scoped_readonly_request
        for alias in ("k8s_get_pod_logs", "k8s_get_pod_log", "k8s_logs", "k8s_pod_logs"):
            assert _is_pod_scoped_readonly_request(alias, {"pod_name": "p", "namespace": "n"}) is True


class TestNormalizePlannerDialectNestedKind:
    def test_nested_kind_readonly_executed_maps_to_discovery(self):
        from workers.analyst_agentic_loop import _normalize_planner_dialect
        o = {
            "thoughts": "investigate",
            "next_action": {"kind": "readonly_executed", "tool": "k8s_get_pod_logs",
                            "args": {"pod_name": "p", "namespace": "n"}},
        }
        out = _normalize_planner_dialect(o)
        assert out["tool_name"] == "k8s_get_pod_logs"
        assert out["decision"] == "discovery"

    def test_nested_kind_mutate_maps_to_mutate(self):
        from workers.analyst_agentic_loop import _normalize_planner_dialect
        o = {"next_action": {"kind": "mutate_executed", "tool": "k8s_patch_resource", "args": {}}}
        out = _normalize_planner_dialect(o)
        assert out["decision"] == "mutate"

    def test_explicit_top_level_decision_not_overridden(self):
        from workers.analyst_agentic_loop import _normalize_planner_dialect
        o = {"decision": "mutate", "next_action": {"kind": "readonly_executed", "tool": "k8s_get_logs"}}
        out = _normalize_planner_dialect(o)
        assert out["decision"] == "mutate"  # model's explicit top-level intent wins

    def test_k8s_get_events_without_involved_name_is_not_pod_scoped(self):
        from workers.analyst_agentic_loop import _is_pod_scoped_readonly_request
        assert _is_pod_scoped_readonly_request("k8s_get_events", {}) is False

    def test_k8s_describe_resource_pod_is_pod_scoped(self):
        from workers.analyst_agentic_loop import _is_pod_scoped_readonly_request
        args = {"resource_type": "Pod", "name": "my-pod", "namespace": "default"}
        assert _is_pod_scoped_readonly_request("k8s_describe_resource", args) is True

    def test_k8s_describe_resource_deployment_is_not_pod_scoped(self):
        from workers.analyst_agentic_loop import _is_pod_scoped_readonly_request
        args = {"resource_type": "Deployment", "name": "my-dep", "namespace": "default"}
        assert _is_pod_scoped_readonly_request("k8s_describe_resource", args) is False

    def test_workload_scoped_tool_is_not_pod_scoped(self):
        from workers.analyst_agentic_loop import _is_pod_scoped_readonly_request
        assert _is_pod_scoped_readonly_request("k8s_get_deployment_state", {"namespace": "default"}) is False


# ---------------------------------------------------------------------------
# _rewrite_pod_scoped_to_workload_probe
# ---------------------------------------------------------------------------

class TestRewritePodScopedToWorkloadProbe:
    def test_with_ns_and_dep_returns_deployment_state(self):
        from workers.analyst_agentic_loop import _rewrite_pod_scoped_to_workload_probe
        tn, args, reason = _rewrite_pod_scoped_to_workload_probe(
            "k8s_get_logs", {}, namespace="multi-agent", deployment="nginx"
        )
        assert tn == "k8s_get_deployment_state"
        assert args["namespace"] == "multi-agent"
        assert args["deployment"] == "nginx"
        assert "pod_scoped_blocked" in reason

    def test_pod_secret_refs_redirected_to_describe_deployment(self):
        from workers.analyst_agentic_loop import _rewrite_pod_scoped_to_workload_probe
        tn, args, reason = _rewrite_pod_scoped_to_workload_probe(
            "k8s_get_pod_secret_refs", {}, namespace="prod", deployment="api"
        )
        assert tn == "k8s_describe_resource"
        assert args["resource_type"] == "Deployment"
        assert args["name"] == "api"

    def test_with_ns_only_returns_list_resources(self):
        from workers.analyst_agentic_loop import _rewrite_pod_scoped_to_workload_probe
        tn, args, reason = _rewrite_pod_scoped_to_workload_probe(
            "k8s_get_logs", {}, namespace="staging", deployment=""
        )
        assert tn == "k8s_list_resources"
        assert args["namespace"] == "staging"

    def test_with_neither_ns_nor_dep_returns_default_namespace(self):
        from workers.analyst_agentic_loop import _rewrite_pod_scoped_to_workload_probe
        tn, args, reason = _rewrite_pod_scoped_to_workload_probe(
            "k8s_get_logs", {}, namespace="", deployment=""
        )
        assert tn == "k8s_list_resources"
        assert args["namespace"] == "multi-agent"

    def test_reason_contains_original_tool(self):
        from workers.analyst_agentic_loop import _rewrite_pod_scoped_to_workload_probe
        _, _, reason = _rewrite_pod_scoped_to_workload_probe(
            "k8s_get_pod_log_tail", {}, namespace="ns", deployment="dep"
        )
        assert "k8s_get_pod_log_tail" in reason


# ---------------------------------------------------------------------------
# _planner_model_candidates
# ---------------------------------------------------------------------------

class TestPlannerModelCandidates:
    def test_returns_non_empty_models(self):
        from workers.analyst_agentic_loop import _planner_model_candidates
        ws = _make_settings(
            diag_evidence_llm_model="model-a",
            model_reasoning_engine="model-b",
            model_helper="model-c",
            chat_model="model-d",
        )
        result = _planner_model_candidates(ws)
        assert "model-a" in result
        assert len(result) == 4

    def test_deduplication(self):
        from workers.analyst_agentic_loop import _planner_model_candidates
        ws = _make_settings(
            diag_evidence_llm_model="model-x",
            model_reasoning_engine="model-x",
            model_helper="model-y",
            chat_model="model-y",
        )
        result = _planner_model_candidates(ws)
        assert result.count("model-x") == 1
        assert result.count("model-y") == 1

    def test_empty_string_excluded(self):
        from workers.analyst_agentic_loop import _planner_model_candidates
        ws = _make_settings(
            diag_evidence_llm_model="",
            model_reasoning_engine="model-z",
            model_helper="",
            chat_model="",
        )
        result = _planner_model_candidates(ws)
        assert result == ["model-z"]

    def test_maintains_priority_order(self):
        from workers.analyst_agentic_loop import _planner_model_candidates
        ws = _make_settings(
            diag_evidence_llm_model="first",
            model_reasoning_engine="second",
            model_helper="third",
            chat_model="fourth",
        )
        result = _planner_model_candidates(ws)
        assert result[0] == "first"


# ---------------------------------------------------------------------------
# build_fact_table_prompt
# ---------------------------------------------------------------------------

class TestBuildFactTablePrompt:
    def test_empty_batch(self):
        from workers.analyst_agentic_loop import build_fact_table_prompt
        result = build_fact_table_prompt([], "")
        assert "Fact table" in result

    def test_includes_probe_info(self):
        from workers.analyst_agentic_loop import build_fact_table_prompt
        batch = [{"probe": "check_cpu", "extracted_fact": {"cpu": 95}, "alert_rule": "HighCPU", "alert_hint": "CPU >90%"}]
        result = build_fact_table_prompt(batch, "CPU spike detected")
        assert "check_cpu" in result
        assert "CPU spike detected" in result

    def test_string_extracted_fact(self):
        from workers.analyst_agentic_loop import build_fact_table_prompt
        batch = [{"probe": "logs", "extracted_fact": "error: pod OOMKilled"}]
        result = build_fact_table_prompt(batch, "OOM killed")
        assert "logs" in result

    def test_dict_extracted_fact_serialized(self):
        from workers.analyst_agentic_loop import build_fact_table_prompt
        batch = [{"probe": "k8s_state", "extracted_fact": {"status": "CrashLoopBackOff"}}]
        result = build_fact_table_prompt(batch, "crash loop")
        assert "CrashLoopBackOff" in result

    def test_max_16_batch_items(self):
        from workers.analyst_agentic_loop import build_fact_table_prompt
        batch = [{"probe": f"probe_{i}", "extracted_fact": f"fact_{i}"} for i in range(20)]
        result = build_fact_table_prompt(batch, "test")
        # Only first 16 should appear (probe_0..probe_15)
        assert "probe_15" in result
        assert "probe_16" not in result

    def test_sanitized_text_included(self):
        from workers.analyst_agentic_loop import build_fact_table_prompt
        result = build_fact_table_prompt([], "This is the sanitized narrative text")
        assert "This is the sanitized narrative text" in result


# ---------------------------------------------------------------------------
# _parse_agentic_json
# ---------------------------------------------------------------------------

class TestParseAgenticJson:
    def test_valid_json_object(self):
        from workers.analyst_agentic_loop import _parse_agentic_json
        result = _parse_agentic_json('{"tool_name": "k8s_get_logs", "args": {}}')
        assert result == {"tool_name": "k8s_get_logs", "args": {}}

    def test_empty_string_returns_none(self):
        from workers.analyst_agentic_loop import _parse_agentic_json
        assert _parse_agentic_json("") is None

    def test_whitespace_only_returns_none(self):
        from workers.analyst_agentic_loop import _parse_agentic_json
        assert _parse_agentic_json("   ") is None

    def test_no_braces_returns_none(self):
        from workers.analyst_agentic_loop import _parse_agentic_json
        assert _parse_agentic_json("no json here") is None

    def test_invalid_json_returns_none(self):
        from workers.analyst_agentic_loop import _parse_agentic_json
        assert _parse_agentic_json("{broken json") is None

    def test_json_array_returns_none(self):
        from workers.analyst_agentic_loop import _parse_agentic_json
        assert _parse_agentic_json('["a", "b"]') is None

    def test_extracts_from_surrounding_text(self):
        from workers.analyst_agentic_loop import _parse_agentic_json
        result = _parse_agentic_json('Some text {"key": "val"} more text')
        assert result == {"key": "val"}

    def test_nested_object(self):
        from workers.analyst_agentic_loop import _parse_agentic_json
        raw = json.dumps({"tool_name": "tool", "args": {"namespace": "ns", "opts": {"limit": 10}}})
        result = _parse_agentic_json(raw)
        assert result["args"]["opts"]["limit"] == 10


class TestNormalizePlannerDialect:
    """qwen2.5-coder emits several dialects instead of the canonical schema;
    the normalizer maps each so the agentic loop stops reject-spinning
    ERR_REA_SCHEMA_VIOLATION (live: trace gw-prom-201e877d7c8b)."""

    def test_flat_action_args_dialect(self):
        from workers.analyst_agentic_loop import _parse_agentic_json
        result = _parse_agentic_json('{"action": "k8s_get_events", "args": {"namespace": "multi-agent"}}')
        assert result["tool_name"] == "k8s_get_events"
        assert result["args"] == {"namespace": "multi-agent"}

    def test_nested_next_action_dialect(self):
        from workers.analyst_agentic_loop import _parse_agentic_json
        raw = (
            '{"thoughts": "crash loop", "next_action": {"kind": "readonly_executed", '
            '"tool": "k8s_get_pod_logs", "args": {"pod_name": "p", "namespace": "ns"}}, '
            '"rationale": "logs help"}'
        )
        result = _parse_agentic_json(raw)
        assert result["tool_name"] == "k8s_get_pod_logs"
        assert result["args"] == {"pod_name": "p", "namespace": "ns"}
        assert result["thought"] == "crash loop"

    def test_flat_resource_type_folds_into_args(self):
        from workers.analyst_agentic_loop import _parse_agentic_json
        result = _parse_agentic_json(
            '{"action": "k8s_describe_resource", "resource_type": "Pod", "name": "x", "namespace": "ns"}'
        )
        assert result["tool_name"] == "k8s_describe_resource"
        assert result["args"] == {"kind": "Pod", "namespace": "ns", "name": "x"}

    def test_canonical_schema_untouched(self):
        from workers.analyst_agentic_loop import _parse_agentic_json
        raw = (
            '{"decision": "mutate", "tool_name": "k8s_patch_resource", '
            '"args": {"namespace": "n", "name": "d", "patch_json": "{}"}, "evidence_refs": ["e"]}'
        )
        result = _parse_agentic_json(raw)
        assert result["tool_name"] == "k8s_patch_resource"
        assert result["args"] == {"namespace": "n", "name": "d", "patch_json": "{}"}

    def test_non_tool_json_untouched(self):
        from workers.analyst_agentic_loop import _parse_agentic_json
        # "key" is a flat-arg alias but must NOT be folded when there is no tool call.
        assert _parse_agentic_json('text {"key": "val"} x') == {"key": "val"}


# ---------------------------------------------------------------------------
# _json_fingerprint
# ---------------------------------------------------------------------------

class TestJsonFingerprint:
    def test_same_value_same_hash(self):
        from workers.analyst_agentic_loop import _json_fingerprint
        assert _json_fingerprint({"a": 1}) == _json_fingerprint({"a": 1})

    def test_different_value_different_hash(self):
        from workers.analyst_agentic_loop import _json_fingerprint
        assert _json_fingerprint({"a": 1}) != _json_fingerprint({"a": 2})

    def test_12_char_length(self):
        from workers.analyst_agentic_loop import _json_fingerprint
        result = _json_fingerprint("test")
        assert len(result) == 12

    def test_handles_non_serializable(self):
        from workers.analyst_agentic_loop import _json_fingerprint
        obj = object()
        result = _json_fingerprint(obj)
        assert isinstance(result, str)
        assert len(result) == 12


# ---------------------------------------------------------------------------
# _infer_readonly_error
# ---------------------------------------------------------------------------

class TestInferReadonlyError:
    def test_data_error_prefix(self):
        from workers.analyst_agentic_loop import _infer_readonly_error
        assert _infer_readonly_error("[DATA] error\n[DIAGNOSIS] something") is True

    def test_unknown_readonly_tool_in_obs(self):
        from workers.analyst_agentic_loop import _infer_readonly_error
        assert _infer_readonly_error("unknown_readonly_tool name='bad'") is True

    def test_normal_output_returns_false(self):
        from workers.analyst_agentic_loop import _infer_readonly_error
        assert _infer_readonly_error("Pods: 3/3 Running") is False

    def test_empty_returns_false(self):
        from workers.analyst_agentic_loop import _infer_readonly_error
        assert _infer_readonly_error("") is False


# ---------------------------------------------------------------------------
# _compact_batch_hint
# ---------------------------------------------------------------------------

class TestCompactBatchHint:
    def test_empty_batch_returns_empty_json(self):
        from workers.analyst_agentic_loop import _compact_batch_hint
        result = _compact_batch_hint([])
        assert result == "[]"

    def test_includes_probe_and_result(self):
        from workers.analyst_agentic_loop import _compact_batch_hint
        batch = [{"probe": "check_cpu", "result": "cpu=90%"}]
        result = _compact_batch_hint(batch)
        data = json.loads(result)
        assert data[0]["probe"] == "check_cpu"

    def test_caps_at_10_items(self):
        from workers.analyst_agentic_loop import _compact_batch_hint
        batch = [{"probe": f"probe_{i}", "result": f"val_{i}"} for i in range(15)]
        result = _compact_batch_hint(batch)
        data = json.loads(result)
        assert len(data) == 10


# ---------------------------------------------------------------------------
# _initial_symptoms_for_memory
# ---------------------------------------------------------------------------

class TestInitialSymptomsForMemory:
    def test_non_empty_sanitized_text_returned(self):
        from workers.analyst_agentic_loop import _initial_symptoms_for_memory
        result = _initial_symptoms_for_memory("Pod OOMKilled", [])
        assert result == "Pod OOMKilled"

    def test_empty_text_falls_back_to_compact_batch(self):
        from workers.analyst_agentic_loop import _initial_symptoms_for_memory
        batch = [{"probe": "check", "result": "ok"}]
        result = _initial_symptoms_for_memory("", batch)
        assert "check" in result

    def test_long_text_truncated_to_2000(self):
        from workers.analyst_agentic_loop import _initial_symptoms_for_memory
        long_text = "x" * 5000
        result = _initial_symptoms_for_memory(long_text, [])
        assert len(result) <= 2000


# ---------------------------------------------------------------------------
# _reject_reason
# ---------------------------------------------------------------------------

class TestRejectReason:
    def test_none_parsed_returns_schema_violation(self):
        from workers.analyst_agentic_loop import _reject_reason
        from pkg.reasoning.reason_codes import ERR_REA_SCHEMA_VIOLATION
        assert _reject_reason(None) == ERR_REA_SCHEMA_VIOLATION

    def test_empty_tool_name_returns_schema_violation(self):
        from workers.analyst_agentic_loop import _reject_reason
        from pkg.reasoning.reason_codes import ERR_REA_SCHEMA_VIOLATION
        result = _reject_reason({"tool_name": "", "args": {}, "evidence_refs": ["fact-1"]})
        assert result == ERR_REA_SCHEMA_VIOLATION

    def test_readonly_tool_returns_sem_channel_mismatch(self):
        from workers.analyst_agentic_loop import _reject_reason
        from pkg.reasoning.reason_codes import ERR_SEM_CHANNEL_MISMATCH
        result = _reject_reason({
            "tool_name": "k8s_get_deployment_state",
            "args": {"namespace": "default"},
            "evidence_refs": ["fact-1"],
        })
        assert result == ERR_SEM_CHANNEL_MISMATCH

    def test_hallucinated_tool_returns_hallucination_detected(self):
        from workers.analyst_agentic_loop import _reject_reason
        from pkg.reasoning.reason_codes import ERR_REA_HALLUCINATION_DETECTED
        result = _reject_reason({
            "tool_name": "completely_fake_nonexistent_tool",
            "args": {},
            "evidence_refs": ["fact-1"],
        })
        assert result == ERR_REA_HALLUCINATION_DETECTED

    def test_valid_rollout_restart_passes(self):
        from workers.analyst_agentic_loop import _reject_reason
        result = _reject_reason({
            "tool_name": "k8s_rollout_restart",
            "args": {"namespace": "default", "deployment": "nginx"},
            "evidence_refs": ["fact-1"],
        })
        assert result == ""

    def test_rollout_restart_missing_namespace(self):
        from workers.analyst_agentic_loop import _reject_reason
        from pkg.reasoning.reason_codes import ERR_REA_SCHEMA_VIOLATION
        result = _reject_reason({
            "tool_name": "k8s_rollout_restart",
            "args": {"deployment": "nginx"},
            "evidence_refs": ["fact-1"],
        })
        assert result == ERR_REA_SCHEMA_VIOLATION

    def test_rollout_restart_missing_deployment(self):
        from workers.analyst_agentic_loop import _reject_reason
        from pkg.reasoning.reason_codes import ERR_REA_SCHEMA_VIOLATION
        result = _reject_reason({
            "tool_name": "k8s_rollout_restart",
            "args": {"namespace": "default"},
            "evidence_refs": ["fact-1"],
        })
        assert result == ERR_REA_SCHEMA_VIOLATION

    def test_no_evidence_refs_returns_schema_violation(self):
        from workers.analyst_agentic_loop import _reject_reason
        from pkg.reasoning.reason_codes import ERR_REA_SCHEMA_VIOLATION
        result = _reject_reason({
            "tool_name": "k8s_rollout_restart",
            "args": {"namespace": "default", "deployment": "nginx"},
            "evidence_refs": [],
        })
        assert result == ERR_REA_SCHEMA_VIOLATION

    def test_empty_evidence_refs_returns_schema_violation(self):
        from workers.analyst_agentic_loop import _reject_reason
        from pkg.reasoning.reason_codes import ERR_REA_SCHEMA_VIOLATION
        result = _reject_reason({
            "tool_name": "k8s_rollout_restart",
            "args": {"namespace": "default", "deployment": "nginx"},
            "evidence_refs": ["   "],  # whitespace only
        })
        assert result == ERR_REA_SCHEMA_VIOLATION

    def test_patch_configmap_valid(self):
        from workers.analyst_agentic_loop import _reject_reason
        result = _reject_reason({
            "tool_name": "k8s_patch_configmap",
            "args": {"namespace": "default", "name": "my-cm", "key": "k", "value": "v"},
            "evidence_refs": ["fact-1"],
        })
        assert result == ""

    def test_patch_configmap_missing_key(self):
        from workers.analyst_agentic_loop import _reject_reason
        from pkg.reasoning.reason_codes import ERR_REA_SCHEMA_VIOLATION
        result = _reject_reason({
            "tool_name": "k8s_patch_configmap",
            "args": {"namespace": "default", "name": "my-cm", "value": "v"},
            "evidence_refs": ["fact-1"],
        })
        assert result == ERR_REA_SCHEMA_VIOLATION

    def test_patch_configmap_missing_value_key(self):
        from workers.analyst_agentic_loop import _reject_reason
        from pkg.reasoning.reason_codes import ERR_REA_SCHEMA_VIOLATION
        result = _reject_reason({
            "tool_name": "k8s_patch_configmap",
            "args": {"namespace": "default", "name": "my-cm", "key": "k"},
            "evidence_refs": ["fact-1"],
        })
        assert result == ERR_REA_SCHEMA_VIOLATION

    def test_patch_secret_valid(self):
        from workers.analyst_agentic_loop import _reject_reason
        result = _reject_reason({
            "tool_name": "k8s_patch_secret",
            "args": {"namespace": "default", "name": "my-secret", "key": "PASSWORD", "value": "newpass"},
            "evidence_refs": ["fact-1"],
        })
        assert result == ""

    def test_non_dict_args_fallback_to_tool_args(self):
        from workers.analyst_agentic_loop import _reject_reason
        result = _reject_reason({
            "tool_name": "k8s_rollout_restart",
            "args": None,
            "tool_args": {"namespace": "default", "deployment": "nginx"},
            "evidence_refs": ["fact-1"],
        })
        assert result == ""

    def test_patch_resource_valid(self):
        from workers.analyst_agentic_loop import _reject_reason
        result = _reject_reason({
            "tool_name": "k8s_patch_resource",
            "args": {"namespace": "default", "name": "my-dep", "patch_json": '{"spec":{}}'},
            "evidence_refs": ["fact-1"],
        })
        assert result == ""

    def test_patch_resource_missing_name(self):
        from workers.analyst_agentic_loop import _reject_reason
        from pkg.reasoning.reason_codes import ERR_REA_SCHEMA_VIOLATION
        result = _reject_reason({
            "tool_name": "k8s_patch_resource",
            "args": {"namespace": "default", "patch_json": '{}'},
            "evidence_refs": ["fact-1"],
        })
        assert result == ERR_REA_SCHEMA_VIOLATION


# ---------------------------------------------------------------------------
# _post_verify_namespace_bound_ok
# ---------------------------------------------------------------------------

class TestPostVerifyNamespaceBoundOk:
    def test_matching_namespace_returns_true(self):
        from workers.analyst_agentic_loop import _post_verify_namespace_bound_ok
        parsed = {"tool_name": "k8s_patch_configmap", "args": {"namespace": "ns-a"}}
        assert _post_verify_namespace_bound_ok(parsed, bound_ns="ns-a", bound_dep="dep") is True

    def test_mismatching_namespace_returns_false(self):
        from workers.analyst_agentic_loop import _post_verify_namespace_bound_ok
        parsed = {"tool_name": "k8s_patch_configmap", "args": {"namespace": "wrong-ns"}}
        assert _post_verify_namespace_bound_ok(parsed, bound_ns="ns-a", bound_dep="dep") is False

    def test_rollout_restart_matching_deployment(self):
        from workers.analyst_agentic_loop import _post_verify_namespace_bound_ok
        parsed = {"tool_name": "k8s_rollout_restart", "args": {"namespace": "ns", "deployment": "nginx"}}
        assert _post_verify_namespace_bound_ok(parsed, bound_ns="ns", bound_dep="nginx") is True

    def test_rollout_restart_mismatching_deployment(self):
        from workers.analyst_agentic_loop import _post_verify_namespace_bound_ok
        parsed = {"tool_name": "k8s_rollout_restart", "args": {"namespace": "ns", "deployment": "other-dep"}}
        assert _post_verify_namespace_bound_ok(parsed, bound_ns="ns", bound_dep="nginx") is False

    def test_tool_args_fallback(self):
        from workers.analyst_agentic_loop import _post_verify_namespace_bound_ok
        parsed = {"tool_name": "k8s_patch_configmap", "tool_args": {"namespace": "ns-a"}}
        assert _post_verify_namespace_bound_ok(parsed, bound_ns="ns-a", bound_dep="dep") is True


# ---------------------------------------------------------------------------
# _truncate_obs
# ---------------------------------------------------------------------------

class TestTruncateObs:
    def test_short_text_returned_as_is(self):
        from workers.analyst_agentic_loop import _truncate_obs
        assert _truncate_obs("short text", 100) == "short text"

    def test_long_text_truncated(self):
        from workers.analyst_agentic_loop import _truncate_obs
        result = _truncate_obs("x" * 1000, 200)
        assert len(result) <= 200 + 50  # truncation note adds chars
        assert "TRUNCATED" in result

    def test_empty_string(self):
        from workers.analyst_agentic_loop import _truncate_obs
        assert _truncate_obs("", 100) == ""


# ---------------------------------------------------------------------------
# _planner_readonly_output_cap
# ---------------------------------------------------------------------------

class TestPlannerReadonlyOutputCap:
    def test_returns_max_of_both_caps(self):
        from workers.analyst_agentic_loop import _planner_readonly_output_cap
        ws = _make_settings(tool_output_max_chars=1500, omni_trace_memory_tool_output_max_chars=4000)
        assert _planner_readonly_output_cap(ws) == 4000

    def test_returns_max_when_tool_cap_larger(self):
        from workers.analyst_agentic_loop import _planner_readonly_output_cap
        ws = _make_settings(tool_output_max_chars=5000, omni_trace_memory_tool_output_max_chars=1000)
        assert _planner_readonly_output_cap(ws) == 5000

    def test_none_ws_returns_default(self):
        from workers.analyst_agentic_loop import _planner_readonly_output_cap
        result = _planner_readonly_output_cap(None)
        assert result >= 400

    def test_min_400(self):
        from workers.analyst_agentic_loop import _planner_readonly_output_cap
        # Both caps are 0 but the function falls back to default of 4000 for memory cap
        # because getattr with default 4000 is used. The min(400) floor applies after max.
        # With tool_output_max_chars=100 and trace_memory cap=100, result should be max(400, max(100,100))=400
        ws = _make_settings(tool_output_max_chars=100, omni_trace_memory_tool_output_max_chars=100)
        result = _planner_readonly_output_cap(ws)
        assert result == 400


# ---------------------------------------------------------------------------
# _react_system_content
# ---------------------------------------------------------------------------

class TestReactSystemContent:
    def test_base_content_included(self):
        from workers.analyst_agentic_loop import _react_system_content
        ws = _make_settings()
        result = _react_system_content(ws)
        assert "Omni SRE" in result

    def test_post_mutate_verify_appends_block(self):
        from workers.analyst_agentic_loop import _react_system_content
        ws = _make_settings()
        result = _react_system_content(ws, post_mutate_verify=True)
        assert "POST-MUTATE STATE VERIFICATION" in result

    def test_sole_evaluator_mode_appended(self):
        from workers.analyst_agentic_loop import _react_system_content
        ws = _make_settings(omni_planner_llm_sole_evaluator=True)
        result = _react_system_content(ws)
        assert "SOLE EVALUATOR MODE" in result

    def test_shadow_os_mode_appended(self):
        from workers.analyst_agentic_loop import _react_system_content
        ws = _make_settings(omni_shadow_os_mode=True)
        result = _react_system_content(ws)
        assert "SHADOW OS MODE" in result

    def test_post_mutate_appended_block_only_with_flag(self):
        from workers.analyst_agentic_loop import _react_system_content, _POST_MUTATE_STATE_VERIFY_APPEND
        ws = _make_settings()
        result_no_flag = _react_system_content(ws, post_mutate_verify=False)
        result_with_flag = _react_system_content(ws, post_mutate_verify=True)
        # The append block adds extra content beyond the base
        assert len(result_with_flag) > len(result_no_flag)
        # The append-specific phrasing is only in the flag variant
        assert "The executor has already applied a mutation" in result_with_flag
        assert "The executor has already applied a mutation" not in result_no_flag


# ---------------------------------------------------------------------------
# _format_playbook_block
# ---------------------------------------------------------------------------

class TestFormatPlaybookBlock:
    def test_basic_playbook_formatting(self):
        from workers.analyst_agentic_loop import _format_playbook_block
        from services.playbook.models import Playbook, PlaybookStep
        step = PlaybookStep(
            step_order=1,
            action_type="k8s_rollout_restart",
            target="nginx",
            params={"namespace": "default"},
            timeout_sec=60,
            requires_hitl=False,
        )
        playbook = Playbook(
            playbook_id="pb-001",
            version="1.0",
            name="Restart Nginx",
            severity_filter="critical",
            approved_by="ops-team",
            steps=(step,),
        )
        result = _format_playbook_block(playbook)
        assert "Restart Nginx" in result
        assert "k8s_rollout_restart" in result
        assert "pb-001" in result

    def test_hitl_required_step_shows_hitl(self):
        from workers.analyst_agentic_loop import _format_playbook_block
        from services.playbook.models import Playbook, PlaybookStep
        step = PlaybookStep(
            step_order=1,
            action_type="k8s_patch_secret",
            target="prod-secret",
            params={},
            timeout_sec=30,
            requires_hitl=True,
        )
        playbook = Playbook(
            playbook_id="pb-002",
            version="1.0",
            name="Rotate Secret",
            severity_filter="critical",
            approved_by="security-team",
            steps=(step,),
        )
        result = _format_playbook_block(playbook)
        assert "HITL_REQUIRED" in result

    def test_auto_step_shows_auto(self):
        from workers.analyst_agentic_loop import _format_playbook_block
        from services.playbook.models import Playbook, PlaybookStep
        step = PlaybookStep(
            step_order=1,
            action_type="k8s_rollout_restart",
            target="api",
            params={},
            timeout_sec=60,
            requires_hitl=False,
        )
        playbook = Playbook(
            playbook_id="pb-003",
            version="1.0",
            name="Auto Restart",
            severity_filter="",
            approved_by="",
            steps=(step,),
        )
        result = _format_playbook_block(playbook)
        assert "auto" in result


# ---------------------------------------------------------------------------
# _format_post_mutate_verify_user_block
# ---------------------------------------------------------------------------

class TestFormatPostMutateVerifyUserBlock:
    def test_contains_executor_feedback(self):
        from workers.analyst_agentic_loop import _format_post_mutate_verify_user_block
        pm = {
            "tool_name": "k8s_rollout_restart",
            "stdout": "deployment.apps/nginx restarted",
            "verify_summary": "Pod is Running",
            "sdk_all_passed": True,
            "exit_code": 0,
        }
        result = _format_post_mutate_verify_user_block(pm)
        assert "EXECUTOR_FEEDBACK" in result
        assert "k8s_rollout_restart" in result
        assert "deployment.apps/nginx restarted" in result

    def test_verify_summary_included(self):
        from workers.analyst_agentic_loop import _format_post_mutate_verify_user_block
        pm = {"verify_summary": "All pods healthy", "exit_code": 0, "tool_name": "", "stdout": "", "sdk_all_passed": True}
        result = _format_post_mutate_verify_user_block(pm)
        assert "All pods healthy" in result

    def test_sdk_all_passed_hint_shown(self):
        from workers.analyst_agentic_loop import _format_post_mutate_verify_user_block
        pm = {"exit_code": 0, "tool_name": "k8s_rollout_restart", "stdout": "", "verify_summary": "", "sdk_all_passed": False}
        result = _format_post_mutate_verify_user_block(pm)
        assert "False" in result


# ---------------------------------------------------------------------------
# _infer_workload_identity
# ---------------------------------------------------------------------------

class TestInferWorkloadIdentity:
    def test_from_sanitized_text_regex(self):
        from workers.analyst_agentic_loop import _infer_workload_identity
        with patch("workers.analyst_agentic_loop.rollout_args_from_evidence_batch", return_value=None):
            ns, dep = _infer_workload_identity([], "namespace=multi-agent deployment=nginx-app alert=true")
        assert ns == "multi-agent"
        assert dep == "nginx-app"

    def test_empty_batch_and_empty_text(self):
        from workers.analyst_agentic_loop import _infer_workload_identity
        with patch("workers.analyst_agentic_loop.rollout_args_from_evidence_batch", return_value=None):
            ns, dep = _infer_workload_identity([], "")
        assert ns == ""
        assert dep == ""

    def test_from_rollout_args(self):
        from workers.analyst_agentic_loop import _infer_workload_identity
        with patch("workers.analyst_agentic_loop.rollout_args_from_evidence_batch", return_value={"namespace": "prod", "deployment": "api-server"}):
            ns, dep = _infer_workload_identity([{}], "")
        assert ns == "prod"
        assert dep == "api-server"


# ---------------------------------------------------------------------------
# _batch_text_for_planner_hints
# ---------------------------------------------------------------------------

class TestBatchTextForPlannerHints:
    def test_empty_batch_returns_empty(self):
        from workers.analyst_agentic_loop import _batch_text_for_planner_hints
        result = _batch_text_for_planner_hints([])
        assert result == ""

    def test_raw_field_included(self):
        from workers.analyst_agentic_loop import _batch_text_for_planner_hints
        batch = [{"raw": "pod error logs here", "extracted_fact": "fact"}]
        result = _batch_text_for_planner_hints(batch)
        assert "pod error logs here" in result

    def test_dict_extracted_fact_serialized(self):
        from workers.analyst_agentic_loop import _batch_text_for_planner_hints
        batch = [{"raw": "", "extracted_fact": {"status": "CrashLoop"}}]
        result = _batch_text_for_planner_hints(batch)
        assert "CrashLoop" in result

    def test_string_extracted_fact_included(self):
        from workers.analyst_agentic_loop import _batch_text_for_planner_hints
        batch = [{"raw": "", "extracted_fact": "OOM killed"}]
        result = _batch_text_for_planner_hints(batch)
        assert "OOM killed" in result


# ---------------------------------------------------------------------------
# _broken_spec_first_round_instruction
# ---------------------------------------------------------------------------

class TestBrokenSpecFirstRoundInstruction:
    def test_no_broken_spec_returns_empty(self):
        from workers.analyst_agentic_loop import _broken_spec_first_round_instruction
        with patch("workers.analyst_agentic_loop.evidence_suggests_broken_spec", return_value=False):
            result = _broken_spec_first_round_instruction([])
        assert result == ""

    def test_broken_spec_with_configmap_not_found(self):
        from workers.analyst_agentic_loop import _broken_spec_first_round_instruction
        batch = [{"raw": 'configmap "nginx-config" not found', "extracted_fact": ""}]
        with patch("workers.analyst_agentic_loop.evidence_suggests_broken_spec", return_value=True):
            result = _broken_spec_first_round_instruction(batch)
        assert "nginx-config" in result
        assert "k8s_describe_resource" in result

    def test_broken_spec_with_secret_not_found(self):
        from workers.analyst_agentic_loop import _broken_spec_first_round_instruction
        batch = [{"raw": 'secret "my-db-secret" not found', "extracted_fact": ""}]
        with patch("workers.analyst_agentic_loop.evidence_suggests_broken_spec", return_value=True):
            result = _broken_spec_first_round_instruction(batch)
        assert "my-db-secret" in result

    def test_broken_spec_without_specific_resource(self):
        from workers.analyst_agentic_loop import _broken_spec_first_round_instruction
        batch = [{"raw": "FailedMount: error", "extracted_fact": ""}]
        with patch("workers.analyst_agentic_loop.evidence_suggests_broken_spec", return_value=True):
            result = _broken_spec_first_round_instruction(batch)
        assert "BROKEN-SPEC PRIORITY" in result


# ---------------------------------------------------------------------------
# _general_credential_failure_hint
# ---------------------------------------------------------------------------

class TestGeneralCredentialFailureHint:
    def test_no_credential_failure_returns_empty(self):
        from workers.analyst_agentic_loop import _general_credential_failure_hint
        with patch("workers.analyst_agentic_loop._evidence_suggests_credential_failure", return_value=False):
            result = _general_credential_failure_hint([])
        assert result == ""

    def test_credential_failure_with_namespace(self):
        from workers.analyst_agentic_loop import _general_credential_failure_hint
        batch = [{"extracted_fact": {"namespace": "prod"}}]
        with patch("workers.analyst_agentic_loop._evidence_suggests_credential_failure", return_value=True):
            result = _general_credential_failure_hint(batch)
        assert "CREDENTIAL FAILURE DETECTED" in result
        assert "k8s_patch_secret" in result
        assert "prod" in result

    def test_credential_failure_extracts_ns_from_raw_fields(self):
        from workers.analyst_agentic_loop import _general_credential_failure_hint
        batch = [{"extracted_fact": {}, "namespace": "staging"}]
        with patch("workers.analyst_agentic_loop._evidence_suggests_credential_failure", return_value=True):
            result = _general_credential_failure_hint(batch)
        assert "CREDENTIAL FAILURE DETECTED" in result


# ---------------------------------------------------------------------------
# infer_blind_proof_lane_hint (async)
# ---------------------------------------------------------------------------

class TestInferBlindProofLaneHint:
    @pytest.mark.asyncio
    async def test_returns_none_when_matrix_matches(self):
        from workers.analyst_agentic_loop import infer_blind_proof_lane_hint
        ctx = _make_ctx()
        with patch("workers.analyst_agentic_loop.pick_matrix_row_for_batch", return_value={"lane": "resource"}):
            result = await infer_blind_proof_lane_hint(ctx, [], sanitized_text="test", rag_match_text=None)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_settings(self):
        from workers.analyst_agentic_loop import infer_blind_proof_lane_hint
        ctx = SimpleNamespace()  # no settings attribute
        with patch("workers.analyst_agentic_loop.pick_matrix_row_for_batch", return_value=None):
            result = await infer_blind_proof_lane_hint(ctx, [], sanitized_text="test", rag_match_text=None)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_blind_lane_disabled(self):
        from workers.analyst_agentic_loop import infer_blind_proof_lane_hint
        ctx = _make_ctx(settings=_make_settings(omni_blind_lane_llm_enabled=False))
        with patch("workers.analyst_agentic_loop.pick_matrix_row_for_batch", return_value=None):
            result = await infer_blind_proof_lane_hint(ctx, [], sanitized_text="test", rag_match_text=None)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_valid_lane_from_llm(self):
        from workers.analyst_agentic_loop import infer_blind_proof_lane_hint
        mock_llm = CompatLLM()
        mock_llm.chat = AsyncMock(return_value={"message": {"content": "resource"}})
        ctx = _make_ctx(
            settings=_make_settings(omni_blind_lane_llm_enabled=True),
            llm=mock_llm,
        )
        with patch("workers.analyst_agentic_loop.pick_matrix_row_for_batch", return_value=None):
            result = await infer_blind_proof_lane_hint(ctx, [], sanitized_text="CPU spike", rag_match_text=None)
        assert result == "resource"

    @pytest.mark.asyncio
    async def test_returns_none_when_llm_returns_invalid_lane(self):
        from workers.analyst_agentic_loop import infer_blind_proof_lane_hint
        mock_llm = CompatLLM()
        mock_llm.chat = AsyncMock(return_value={"message": {"content": "invalid_lane"}})
        ctx = _make_ctx(
            settings=_make_settings(omni_blind_lane_llm_enabled=True),
            llm=mock_llm,
        )
        with patch("workers.analyst_agentic_loop.pick_matrix_row_for_batch", return_value=None):
            result = await infer_blind_proof_lane_hint(ctx, [], sanitized_text="test", rag_match_text=None)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_model_candidates(self):
        from workers.analyst_agentic_loop import infer_blind_proof_lane_hint
        ctx = _make_ctx(
            settings=_make_settings(
                omni_blind_lane_llm_enabled=True,
                diag_evidence_llm_model="",
                model_reasoning_engine="",
                model_helper="",
                chat_model="",
            ),
            llm=CompatLLM(),
        )
        with patch("workers.analyst_agentic_loop.pick_matrix_row_for_batch", return_value=None):
            result = await infer_blind_proof_lane_hint(ctx, [], sanitized_text="test", rag_match_text=None)
        assert result is None

    @pytest.mark.asyncio
    async def test_handles_llm_exception(self):
        from workers.analyst_agentic_loop import infer_blind_proof_lane_hint
        mock_llm = CompatLLM()
        mock_llm.chat = AsyncMock(side_effect=RuntimeError("connection refused"))
        ctx = _make_ctx(
            settings=_make_settings(omni_blind_lane_llm_enabled=True),
            llm=mock_llm,
        )
        with patch("workers.analyst_agentic_loop.pick_matrix_row_for_batch", return_value=None):
            result = await infer_blind_proof_lane_hint(ctx, [], sanitized_text="test", rag_match_text=None)
        assert result is None


# ---------------------------------------------------------------------------
# _execute_readonly_tool (async)
# ---------------------------------------------------------------------------

class TestExecuteReadonlyTool:
    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error(self):
        from workers.analyst_agentic_loop import _execute_readonly_tool
        ctx = _make_ctx()
        # Patch the TOOL_REGISTRY in workers.tools (where the function imports it from)
        import workers.tools as w_tools
        with patch.object(w_tools, "TOOL_REGISTRY", {}):
            result = await _execute_readonly_tool(ctx, "nonexistent_tool", {})
        assert "unknown_readonly_tool" in result

    @pytest.mark.asyncio
    async def test_known_tool_returns_output(self):
        from workers.analyst_agentic_loop import _execute_readonly_tool

        async def _fake_tool(ctx, args):
            return "Pods: nginx-1 Running"

        ctx = _make_ctx()
        from workers import tools as w_tools
        with patch.object(w_tools, "TOOL_REGISTRY", {"k8s_list_pods": _fake_tool}):
            result = await _execute_readonly_tool(ctx, "k8s_list_pods", {})
        assert "nginx-1" in result

    @pytest.mark.asyncio
    async def test_tool_exception_returns_error_message(self):
        from workers.analyst_agentic_loop import _execute_readonly_tool

        async def _fail_tool(ctx, args):
            raise ValueError("connection timeout")

        ctx = _make_ctx()
        from workers import tools as w_tools
        with patch.object(w_tools, "TOOL_REGISTRY", {"k8s_get_logs": _fail_tool}):
            result = await _execute_readonly_tool(ctx, "k8s_get_logs", {})
        assert "[DATA] error" in result
        assert "connection timeout" in result


# ---------------------------------------------------------------------------
# run_agentic_mutate_plan (async) — simplified flow tests
# ---------------------------------------------------------------------------

class TestRunAgenticMutatePlan:
    def _make_trace_memory(self):
        from workers.memory.trace_memory import OmniTraceMemory
        return OmniTraceMemory(
            trace_id="test-trace",
            attempt_count=0,
            action_history=[],
            working_hypothesis="",
        )

    @pytest.mark.asyncio
    async def test_returns_none_when_no_settings(self):
        from workers.analyst_agentic_loop import run_agentic_mutate_plan
        ctx = SimpleNamespace()  # no settings
        result = await run_agentic_mutate_plan(
            ctx, trace="t", sanitized_text="test", batch=[], max_steps=1
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_llm(self):
        from workers.analyst_agentic_loop import run_agentic_mutate_plan
        ctx = _make_ctx(settings=_make_settings(), llm=None)
        result = await run_agentic_mutate_plan(
            ctx, trace="t", sanitized_text="test", batch=[], max_steps=1
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_model_candidates(self):
        from workers.analyst_agentic_loop import run_agentic_mutate_plan
        ctx = _make_ctx(
            settings=_make_settings(
                diag_evidence_llm_model="",
                model_reasoning_engine="",
                model_helper="",
                chat_model="",
            ),
            llm=CompatLLM(),
        )
        result = await run_agentic_mutate_plan(
            ctx, trace="t", sanitized_text="test", batch=[], max_steps=1
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_done_on_phase_done_response(self):
        from workers.analyst_agentic_loop import run_agentic_mutate_plan
        from pkg.reasoning.reason_codes import PLANNER_PHASE_DONE

        mem = self._make_trace_memory()
        mock_llm = CompatLLM()
        mock_llm.chat = AsyncMock(return_value={
            "message": {"content": json.dumps({
                "decision": "done",
                "phase": "done",
                "tool_name": "",
                "args": {},
                "step": "readonly",
                "thought": "Issue is resolved",
                "resolution_summary": "Deployment is healthy",
            })}
        })
        ctx = _make_ctx(llm=mock_llm)

        with (
            patch("workers.analyst_agentic_loop.load_trace_memory", return_value=mem),
            patch("workers.analyst_agentic_loop.save_trace_memory", new_callable=AsyncMock),
            patch("workers.analyst_agentic_loop.log_llm_trace"),
            patch("workers.analyst_agentic_loop.get_tool_registry") as mock_reg,
            patch("workers.analyst_agentic_loop._planner_tool_catalog_prompt", return_value="catalog"),
            patch("workers.analyst_agentic_loop.format_trace_memory_block", return_value="<TRACE_MEMORY></TRACE_MEMORY>"),
        ):
            mock_reg.return_value.tool_catalog_json_for_prompt.return_value = "catalog"
            result = await run_agentic_mutate_plan(
                ctx, trace="test-trace", sanitized_text="Pod is healthy", batch=[], max_steps=2
            )

        assert result is not None
        assert result["reason_code"] == PLANNER_PHASE_DONE
        assert result["phase"] == "done"

    @pytest.mark.asyncio
    async def test_returns_escalate_on_escalate_decision(self):
        from workers.analyst_agentic_loop import run_agentic_mutate_plan

        mem = self._make_trace_memory()
        mock_llm = CompatLLM()
        mock_llm.chat = AsyncMock(return_value={
            "message": {"content": json.dumps({
                "decision": "escalate",
                "phase": "observe",
                "tool_name": "",
                "args": {},
                "step": "readonly",
                "thought": "Need human",
                "escalation_reason": "Cannot determine root cause without access",
            })}
        })
        ctx = _make_ctx(llm=mock_llm)

        with (
            patch("workers.analyst_agentic_loop.load_trace_memory", return_value=mem),
            patch("workers.analyst_agentic_loop.save_trace_memory", new_callable=AsyncMock),
            patch("workers.analyst_agentic_loop.log_llm_trace"),
            patch("workers.analyst_agentic_loop.get_tool_registry") as mock_reg,
            patch("workers.analyst_agentic_loop._planner_tool_catalog_prompt", return_value="catalog"),
            patch("workers.analyst_agentic_loop.format_trace_memory_block", return_value="<TRACE_MEMORY></TRACE_MEMORY>"),
        ):
            mock_reg.return_value.tool_catalog_json_for_prompt.return_value = "catalog"
            result = await run_agentic_mutate_plan(
                ctx, trace="test-trace", sanitized_text="test", batch=[], max_steps=2
            )

        assert result is not None
        assert result["decision"] == "escalate"
        assert result["phase"] == "escalate"

    @pytest.mark.asyncio
    async def test_returns_none_when_llm_unparseable(self):
        from workers.analyst_agentic_loop import run_agentic_mutate_plan

        mem = self._make_trace_memory()
        mock_llm = CompatLLM()
        mock_llm.chat = AsyncMock(return_value={"message": {"content": "not json"}})
        ctx = _make_ctx(llm=mock_llm)

        with (
            patch("workers.analyst_agentic_loop.load_trace_memory", return_value=mem),
            patch("workers.analyst_agentic_loop.save_trace_memory", new_callable=AsyncMock),
            patch("workers.analyst_agentic_loop.log_llm_trace"),
            patch("workers.analyst_agentic_loop.get_tool_registry") as mock_reg,
            patch("workers.analyst_agentic_loop._planner_tool_catalog_prompt", return_value="catalog"),
            patch("workers.analyst_agentic_loop.format_trace_memory_block", return_value="<TRACE_MEMORY></TRACE_MEMORY>"),
        ):
            mock_reg.return_value.tool_catalog_json_for_prompt.return_value = "catalog"
            result = await run_agentic_mutate_plan(
                ctx, trace="test-trace", sanitized_text="test", batch=[], max_steps=2
            )

        assert result is None


# ---------------------------------------------------------------------------
# _persist_post_verify_memory and _clear_post_verify_memory (async)
# ---------------------------------------------------------------------------

class TestPostVerifyMemoryHelpers:
    @pytest.mark.asyncio
    async def test_persist_does_nothing_without_redis(self):
        from workers.analyst_agentic_loop import _persist_post_verify_memory
        ctx = _make_ctx(redis=None)
        # Should not raise
        await _persist_post_verify_memory(ctx, "trace-1", thought_process=["t"], observations=["o"], step_idx=1)

    @pytest.mark.asyncio
    async def test_persist_with_redis_calls_setex(self):
        from workers.analyst_agentic_loop import _persist_post_verify_memory
        mock_redis = AsyncMock()
        mock_redis.setex = AsyncMock()
        ctx = _make_ctx(redis=mock_redis)
        await _persist_post_verify_memory(ctx, "trace-1", thought_process=["t"], observations=["o"], step_idx=2)
        mock_redis.setex.assert_called_once()

    @pytest.mark.asyncio
    async def test_clear_does_nothing_without_redis(self):
        from workers.analyst_agentic_loop import _clear_post_verify_memory
        ctx = _make_ctx(redis=None)
        # Should not raise
        await _clear_post_verify_memory(ctx, "trace-1")

    @pytest.mark.asyncio
    async def test_clear_with_redis_calls_delete(self):
        from workers.analyst_agentic_loop import _clear_post_verify_memory
        mock_redis = AsyncMock()
        mock_redis.delete = AsyncMock()
        ctx = _make_ctx(redis=mock_redis)
        await _clear_post_verify_memory(ctx, "trace-1")
        mock_redis.delete.assert_called_once()


# ---------------------------------------------------------------------------
# run_post_verify_react_loop (async)
# ---------------------------------------------------------------------------

class TestRunPostVerifyReactLoop:
    @pytest.mark.asyncio
    async def test_returns_none_when_no_settings(self):
        from workers.analyst_agentic_loop import run_post_verify_react_loop
        ctx = SimpleNamespace()
        result = await run_post_verify_react_loop(
            ctx, trace="t", namespace="ns", deployment="dep",
            verify_summary="", dep_detail="", executor_stdout="",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_disabled(self):
        from workers.analyst_agentic_loop import run_post_verify_react_loop
        ctx = _make_ctx(settings=_make_settings(omni_post_verify_state_llm_enabled=False))
        result = await run_post_verify_react_loop(
            ctx, trace="t", namespace="ns", deployment="dep",
            verify_summary="", dep_detail="", executor_stdout="",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_empty_ns(self):
        from workers.analyst_agentic_loop import run_post_verify_react_loop
        ctx = _make_ctx(llm=CompatLLM())
        result = await run_post_verify_react_loop(
            ctx, trace="t", namespace="", deployment="dep",
            verify_summary="", dep_detail="", executor_stdout="",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_empty_dep(self):
        from workers.analyst_agentic_loop import run_post_verify_react_loop
        ctx = _make_ctx(llm=CompatLLM())
        result = await run_post_verify_react_loop(
            ctx, trace="t", namespace="ns", deployment="",
            verify_summary="", dep_detail="", executor_stdout="",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_phase_done(self):
        from workers.analyst_agentic_loop import run_post_verify_react_loop
        mock_llm = CompatLLM()
        mock_llm.chat = AsyncMock(return_value={
            "message": {"content": json.dumps({
                "phase": "done",
                "tool_name": "",
                "args": {},
                "thought": "All healthy",
            })}
        })
        ctx = _make_ctx(llm=mock_llm)

        with (
            patch("workers.analyst_agentic_loop.log_llm_trace"),
            patch("workers.analyst_agentic_loop.sanitize_probe_text_for_llm", return_value="sanitized"),
            patch("workers.analyst_agentic_loop._clear_post_verify_memory", new_callable=AsyncMock),
            patch("workers.analyst_agentic_loop._persist_post_verify_memory", new_callable=AsyncMock),
            patch("workers.analyst_agentic_loop.get_tool_registry") as mock_reg,
            patch("workers.analyst_agentic_loop._planner_tool_catalog_prompt", return_value="catalog"),
        ):
            mock_reg.return_value.tool_catalog_json_for_prompt.return_value = "catalog"
            result = await run_post_verify_react_loop(
                ctx, trace="t", namespace="ns", deployment="dep",
                verify_summary="ok", dep_detail="healthy", executor_stdout="done",
            )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_model_candidates(self):
        from workers.analyst_agentic_loop import run_post_verify_react_loop
        ctx = _make_ctx(
            settings=_make_settings(
                diag_evidence_llm_model="",
                model_reasoning_engine="",
                model_helper="",
                chat_model="",
            ),
            llm=CompatLLM(),
        )
        result = await run_post_verify_react_loop(
            ctx, trace="t", namespace="ns", deployment="dep",
            verify_summary="", dep_detail="", executor_stdout="",
        )
        assert result is None


# ---------------------------------------------------------------------------
# run_post_mutate_state_verify_planner (async)
# ---------------------------------------------------------------------------

class TestRunPostMutateStateVerifyPlanner:
    @pytest.mark.asyncio
    async def test_returns_none_when_no_settings(self):
        from workers.analyst_agentic_loop import run_post_mutate_state_verify_planner
        ctx = SimpleNamespace()
        result = await run_post_mutate_state_verify_planner(
            ctx, trace="t", batch=[], sanitized_text="test",
            stdout="", tool_name="k8s_rollout_restart",
            verify_summary="ok", sdk_all_passed=True,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_delegates_to_run_agentic_mutate_plan(self):
        from workers.analyst_agentic_loop import run_post_mutate_state_verify_planner
        ctx = _make_ctx(llm=CompatLLM())
        expected = {"reason_code": "PLANNER_PHASE_DONE", "phase": "done"}

        with patch("workers.analyst_agentic_loop.run_agentic_mutate_plan", new_callable=AsyncMock, return_value=expected):
            result = await run_post_mutate_state_verify_planner(
                ctx, trace="t", batch=[], sanitized_text="test",
                stdout="restarted", tool_name="k8s_rollout_restart",
                verify_summary="Pod Running", sdk_all_passed=True,
            )
        assert result == expected


# ---------------------------------------------------------------------------
# discovery_tool_registry_names_for_spec
# ---------------------------------------------------------------------------

class TestDiscoveryToolRegistryNamesForSpec:
    def test_returns_tuple_for_known_spec(self):
        from workers.analyst_agentic_loop import discovery_tool_registry_names_for_spec
        from pkg.reasoning.diagnostic_policy import DISCOVERY_TOOL_ALIASES
        if DISCOVERY_TOOL_ALIASES:
            spec_name = next(iter(DISCOVERY_TOOL_ALIASES))
            result = discovery_tool_registry_names_for_spec(spec_name)
            assert isinstance(result, tuple)
        else:
            # No aliases defined — just verify the function runs
            result = discovery_tool_registry_names_for_spec("nonexistent")
            assert result == ()

    def test_returns_empty_tuple_for_unknown_spec(self):
        from workers.analyst_agentic_loop import discovery_tool_registry_names_for_spec
        result = discovery_tool_registry_names_for_spec("nonexistent_spec_name_xyz")
        assert result == ()
