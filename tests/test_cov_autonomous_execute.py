"""Coverage tests for src/workers/autonomous_execute.py."""
from __future__ import annotations

import os

os.environ.setdefault("OMNI_KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
os.environ.setdefault("OMNI_REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("OMNI_OLLAMA_BASE_URL", "http://localhost:11434")
os.environ.setdefault("OMNI_ENV_MODE", "dev")

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_settings(
    *,
    env_mode: str = "dev",
    force_nsenter: bool = False,
    unrestricted: bool = True,
    allowed_namespaces: str = "multi-agent",
):
    return SimpleNamespace(
        env_mode=env_mode,
        omni_executor_force_nsenter=force_nsenter,
        omni_unrestricted_tool_execution=unrestricted,
        autonomous_allowed_namespaces=allowed_namespaces,
        kafka_topic_action_feedback="omni-action-feedback",
    )


def _make_ctx(settings=None, kafka=None):
    ctx = SimpleNamespace()
    ctx.settings = settings or _make_settings()
    ctx.kafka = kafka
    return ctx


# ---------------------------------------------------------------------------
# _trunc_feedback_text
# ---------------------------------------------------------------------------

def test_trunc_feedback_text_no_truncation():
    from workers.autonomous_execute import _trunc_feedback_text
    assert _trunc_feedback_text("hello") == "hello"


def test_trunc_feedback_text_truncates():
    from workers.autonomous_execute import _trunc_feedback_text, _FEEDBACK_TRUNC
    long_s = "x" * (_FEEDBACK_TRUNC + 100)
    result = _trunc_feedback_text(long_s)
    assert result.endswith("[...truncated]")
    assert len(result) <= _FEEDBACK_TRUNC + 20


def test_trunc_feedback_text_none():
    from workers.autonomous_execute import _trunc_feedback_text
    assert _trunc_feedback_text(None) == ""


def test_trunc_feedback_text_custom_max():
    from workers.autonomous_execute import _trunc_feedback_text
    result = _trunc_feedback_text("hello world", max_len=5)
    assert result.startswith("hello")
    assert "[...truncated]" in result


# ---------------------------------------------------------------------------
# _normalize_mutate_args_for_registry
# ---------------------------------------------------------------------------

def test_normalize_not_describe_resource():
    from workers.autonomous_execute import _normalize_mutate_args_for_registry
    args = {"namespace": "n", "deployment": "d"}
    result = _normalize_mutate_args_for_registry("k8s_rollout_restart", args)
    assert result == args


def test_normalize_describe_resource_adds_resource_type():
    from workers.autonomous_execute import _normalize_mutate_args_for_registry
    args = {"kind": "pod", "name": "my-pod"}
    result = _normalize_mutate_args_for_registry("k8s_describe_resource", args)
    assert result["resource_type"] == "Pod"


def test_normalize_describe_resource_deployment():
    from workers.autonomous_execute import _normalize_mutate_args_for_registry
    args = {"kind": "deploy"}
    result = _normalize_mutate_args_for_registry("k8s_describe_resource", args)
    assert result["resource_type"] == "Deployment"


def test_normalize_describe_resource_service():
    from workers.autonomous_execute import _normalize_mutate_args_for_registry
    args = {"kind": "svc"}
    result = _normalize_mutate_args_for_registry("k8s_describe_resource", args)
    assert result["resource_type"] == "Service"


def test_normalize_describe_resource_unknown_kind():
    from workers.autonomous_execute import _normalize_mutate_args_for_registry
    args = {"kind": "unknown_type"}
    result = _normalize_mutate_args_for_registry("k8s_describe_resource", args)
    assert "resource_type" not in result


def test_normalize_describe_resource_fills_name_from_pod():
    from workers.autonomous_execute import _normalize_mutate_args_for_registry
    args = {"kind": "pod", "pod": "my-pod-123"}
    result = _normalize_mutate_args_for_registry("k8s_describe_resource", args)
    assert result["name"] == "my-pod-123"


def test_normalize_describe_resource_existing_name_preserved():
    from workers.autonomous_execute import _normalize_mutate_args_for_registry
    args = {"kind": "pod", "name": "existing-pod"}
    result = _normalize_mutate_args_for_registry("k8s_describe_resource", args)
    assert result["name"] == "existing-pod"


# ---------------------------------------------------------------------------
# Allowlists / sets
# ---------------------------------------------------------------------------

def test_mutate_allowlist_contains_known_tools():
    from workers.autonomous_execute import MUTATE_TOOL_ALLOWLIST
    assert "k8s_rollout_restart" in MUTATE_TOOL_ALLOWLIST
    assert "k8s_scale_deployment" in MUTATE_TOOL_ALLOWLIST
    assert "k8s_patch_deployment" in MUTATE_TOOL_ALLOWLIST  # alias


def test_readonly_allowlist_contains_known_tools():
    from workers.autonomous_execute import READONLY_TOOL_ALLOWLIST
    assert "k8s_tail_logs" in READONLY_TOOL_ALLOWLIST
    assert "list_namespace_pods" in READONLY_TOOL_ALLOWLIST


# ---------------------------------------------------------------------------
# run_execute_mutate_tool — unrestricted mode
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_mutate_unrestricted_known_tool():
    # Unrestricted mode still requires tools to be in MUTATE_TOOL_ALLOWLIST.
    from workers.autonomous_execute import run_execute_mutate_tool
    from workers.tools import TOOL_REGISTRY
    ctx = _make_ctx(settings=_make_settings(unrestricted=True))
    mock_fn = AsyncMock(return_value="tool ok output")
    TOOL_REGISTRY["k8s_rollout_restart"] = mock_fn
    try:
        out, code = await run_execute_mutate_tool(
            ctx, tool_name="k8s_rollout_restart",
            args={"namespace": "multi-agent", "deployment": "test-dep"},
            trace_id="trace-one",
        )
        assert "tool ok output" in out
        assert code == 0
    finally:
        TOOL_REGISTRY.pop("k8s_rollout_restart", None)


@pytest.mark.asyncio
async def test_run_mutate_unrestricted_unknown_tool_returns_error():
    # Tool not in MUTATE_TOOL_ALLOWLIST is rejected even in unrestricted mode.
    from workers.autonomous_execute import run_execute_mutate_tool
    ctx = _make_ctx(settings=_make_settings(unrestricted=True))
    out, code = await run_execute_mutate_tool(
        ctx, tool_name="nonexistent_tool_abc123", args={}, trace_id="trace-one"
    )
    assert "not in mutate allowlist" in out
    assert code == 1


@pytest.mark.asyncio
async def test_run_mutate_unrestricted_tool_exception():
    from workers.autonomous_execute import run_execute_mutate_tool
    from workers.tools import TOOL_REGISTRY
    ctx = _make_ctx(settings=_make_settings(unrestricted=True))
    TOOL_REGISTRY["failing_tool_xyz"] = AsyncMock(side_effect=RuntimeError("boom"))
    try:
        out, code = await run_execute_mutate_tool(
            ctx, tool_name="failing_tool_xyz", args={}, trace_id="trace-one"
        )
        assert "[DATA] error" in out
        assert code == 1
    finally:
        TOOL_REGISTRY.pop("failing_tool_xyz", None)


# ---------------------------------------------------------------------------
# run_execute_mutate_tool — force_nsenter mode
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_mutate_force_nsenter_blocks_non_kubectl():
    from workers.autonomous_execute import run_execute_mutate_tool
    from pkg.reasoning.reason_codes import ERR_GOV_UNAUTHORIZED_MUTATION
    ctx = _make_ctx(settings=_make_settings(force_nsenter=True, unrestricted=False))
    out, code = await run_execute_mutate_tool(
        ctx, tool_name="k8s_rollout_restart", args={}, trace_id="trace-one"
    )
    assert ERR_GOV_UNAUTHORIZED_MUTATION in out
    assert code == 1


@pytest.mark.asyncio
async def test_run_mutate_force_nsenter_allows_kubectl_cluster():
    """kubectl_cluster should pass through even with force_nsenter=True."""
    from workers.autonomous_execute import run_execute_mutate_tool
    from workers.tools import TOOL_REGISTRY
    ctx = _make_ctx(settings=_make_settings(force_nsenter=True, unrestricted=False))
    mock_fn = AsyncMock(return_value="kubectl cluster output")
    TOOL_REGISTRY["kubectl_cluster"] = mock_fn
    try:
        out, code = await run_execute_mutate_tool(
            ctx, tool_name="kubectl_cluster",
            args={"namespace": "multi-agent"},
            trace_id="trace-one"
        )
        # Should NOT be blocked (no nsenter error)
        from pkg.reasoning.reason_codes import ERR_GOV_UNAUTHORIZED_MUTATION

        assert ERR_GOV_UNAUTHORIZED_MUTATION not in out or code == 0
    finally:
        TOOL_REGISTRY.pop("kubectl_cluster", None)


# ---------------------------------------------------------------------------
# run_execute_mutate_tool — restricted mode (unrestricted=False)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_mutate_restricted_readonly_tool_blocked():
    from workers.autonomous_execute import run_execute_mutate_tool
    from pkg.reasoning.reason_codes import ERR_GOV_UNAUTHORIZED_MUTATION
    ctx = _make_ctx(settings=_make_settings(unrestricted=False))
    out, code = await run_execute_mutate_tool(
        ctx, tool_name="k8s_tail_logs", args={}, trace_id="trace-one"
    )
    assert ERR_GOV_UNAUTHORIZED_MUTATION in out
    assert code == 1


@pytest.mark.asyncio
async def test_run_mutate_restricted_not_in_allowlist():
    from workers.autonomous_execute import run_execute_mutate_tool
    from pkg.reasoning.reason_codes import ERR_GOV_UNAUTHORIZED_MUTATION
    ctx = _make_ctx(settings=_make_settings(unrestricted=False))
    out, code = await run_execute_mutate_tool(
        ctx, tool_name="some_random_tool", args={}, trace_id="trace-one"
    )
    assert ERR_GOV_UNAUTHORIZED_MUTATION in out
    assert code == 1


@pytest.mark.asyncio
async def test_run_mutate_restricted_mutate_tool_not_in_registry():
    """Tool is in mutate allowlist but not registered in TOOL_REGISTRY."""
    from workers.autonomous_execute import run_execute_mutate_tool
    from workers.tools import TOOL_REGISTRY
    from pkg.reasoning.reason_codes import ERR_GOV_UNAUTHORIZED_MUTATION
    ctx = _make_ctx(settings=_make_settings(unrestricted=False))
    # Temporarily remove k8s_scale_deployment if present
    saved = TOOL_REGISTRY.pop("k8s_scale_deployment", None)
    try:
        out, code = await run_execute_mutate_tool(
            ctx, tool_name="k8s_scale_deployment", args={"namespace": "multi-agent"}, trace_id="trace-one"
        )
        assert code == 1
    finally:
        if saved:
            TOOL_REGISTRY["k8s_scale_deployment"] = saved


@pytest.mark.asyncio
async def test_run_mutate_prod_mode_no_namespace_blocked():
    """Prod mode: mutating tool without namespace in args → blocked."""
    from workers.autonomous_execute import run_execute_mutate_tool
    from workers.tools import TOOL_REGISTRY
    from pkg.reasoning.reason_codes import ERR_GOV_UNAUTHORIZED_MUTATION
    ctx = _make_ctx(settings=_make_settings(env_mode="prod", unrestricted=False))
    mock_fn = AsyncMock(return_value="ok")
    TOOL_REGISTRY["k8s_rollout_restart"] = mock_fn
    try:
        out, code = await run_execute_mutate_tool(
            ctx, tool_name="k8s_rollout_restart", args={}, trace_id="trace-one"
        )
        assert ERR_GOV_UNAUTHORIZED_MUTATION in out or "requires valid Kubernetes DNS-label namespace" in out
        assert code == 1
    finally:
        TOOL_REGISTRY.pop("k8s_rollout_restart", None)


@pytest.mark.asyncio
async def test_run_mutate_prod_mode_ns_not_allowed():
    from workers.autonomous_execute import run_execute_mutate_tool
    from workers.tools import TOOL_REGISTRY
    from pkg.reasoning.reason_codes import ERR_GOV_NS_OUT_OF_BOUNDS
    ctx = _make_ctx(settings=_make_settings(
        env_mode="prod",
        unrestricted=False,
        allowed_namespaces="multi-agent",
    ))
    mock_fn = AsyncMock(return_value="ok")
    TOOL_REGISTRY["k8s_rollout_restart"] = mock_fn
    try:
        out, code = await run_execute_mutate_tool(
            ctx, tool_name="k8s_rollout_restart",
            args={"namespace": "forbidden-ns", "deployment": "my-app"},
            trace_id="trace-one"
        )
        assert ERR_GOV_NS_OUT_OF_BOUNDS in out
        assert code == 1
    finally:
        TOOL_REGISTRY.pop("k8s_rollout_restart", None)


@pytest.mark.asyncio
async def test_run_mutate_restricted_tool_success():
    """Happy path: mutating tool in prod mode, namespace allowed."""
    from workers.autonomous_execute import run_execute_mutate_tool
    from workers.tools import TOOL_REGISTRY
    ctx = _make_ctx(settings=_make_settings(
        env_mode="prod",
        unrestricted=False,
        allowed_namespaces="multi-agent",
    ))
    mock_fn = AsyncMock(return_value="scale ok")
    TOOL_REGISTRY["k8s_scale_deployment"] = mock_fn
    try:
        out, code = await run_execute_mutate_tool(
            ctx, tool_name="k8s_scale_deployment",
            args={"namespace": "multi-agent", "deployment": "my-app", "replicas": 3},
            trace_id="trace-one"
        )
        assert "scale ok" in out
        assert code == 0
    finally:
        TOOL_REGISTRY.pop("k8s_scale_deployment", None)


@pytest.mark.asyncio
async def test_run_mutate_restricted_tool_raises_exception():
    from workers.autonomous_execute import run_execute_mutate_tool
    from workers.tools import TOOL_REGISTRY
    ctx = _make_ctx(settings=_make_settings(env_mode="dev", unrestricted=False))
    TOOL_REGISTRY["k8s_scale_deployment"] = AsyncMock(side_effect=RuntimeError("k8s error"))
    try:
        out, code = await run_execute_mutate_tool(
            ctx, tool_name="k8s_scale_deployment",
            args={"namespace": "multi-agent", "deployment": "x"},
            trace_id="trace-one"
        )
        assert "[DATA] error" in out
        assert code == 1
    finally:
        TOOL_REGISTRY.pop("k8s_scale_deployment", None)


# ---------------------------------------------------------------------------
# run_execute_mutate_tool — k8s_rollout_restart special path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_mutate_rollout_restart_missing_ns_or_dep():
    from workers.autonomous_execute import run_execute_mutate_tool
    from workers.tools import TOOL_REGISTRY
    ctx = _make_ctx(settings=_make_settings(unrestricted=False))
    TOOL_REGISTRY["k8s_rollout_restart"] = AsyncMock(return_value="ok")
    try:
        out, code = await run_execute_mutate_tool(
            ctx, tool_name="k8s_rollout_restart",
            args={"namespace": "multi-agent"},  # missing deployment
            trace_id="trace-one"
        )
        assert "requires namespace and deployment" in out
        assert code == 1
    finally:
        TOOL_REGISTRY.pop("k8s_rollout_restart", None)


@pytest.mark.asyncio
async def test_run_mutate_rollout_restart_success():
    from workers.autonomous_execute import run_execute_mutate_tool
    from workers.tools import TOOL_REGISTRY
    ctx = _make_ctx(settings=_make_settings(unrestricted=False, env_mode="dev"))
    TOOL_REGISTRY["k8s_rollout_restart"] = AsyncMock(return_value="restart ok")
    snap = {"deployment_generation": 5}
    with patch("workers.autonomous_execute.execute_rollout_restart_from_pending", new=AsyncMock(return_value="restart completed")) as mock_restart, \
         patch("workers.autonomous_execute.deployment_evidence_snapshot", new=AsyncMock(return_value=snap)):
        try:
            out, code = await run_execute_mutate_tool(
                ctx, tool_name="k8s_rollout_restart",
                args={"namespace": "multi-agent", "deployment": "my-app"},
                trace_id="trace-one"
            )
            assert "restart" in out
        finally:
            TOOL_REGISTRY.pop("k8s_rollout_restart", None)


@pytest.mark.asyncio
async def test_run_mutate_rollout_restart_with_existing_snapshot():
    from workers.autonomous_execute import run_execute_mutate_tool
    from workers.tools import TOOL_REGISTRY
    ctx = _make_ctx(settings=_make_settings(unrestricted=False, env_mode="dev"))
    TOOL_REGISTRY["k8s_rollout_restart"] = AsyncMock(return_value="restart ok")
    snap = {"deployment_generation": 3}
    with patch("workers.autonomous_execute.execute_rollout_restart_from_pending", new=AsyncMock(return_value="done")):
        try:
            out, code = await run_execute_mutate_tool(
                ctx, tool_name="k8s_rollout_restart",
                args={"namespace": "multi-agent", "deployment": "my-app", "evidence_snapshot": snap},
                trace_id="trace-one"
            )
            assert code == 0
        finally:
            TOOL_REGISTRY.pop("k8s_rollout_restart", None)


# ---------------------------------------------------------------------------
# run_execute_mutate_tool — alias resolution
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_mutate_alias_k8s_patch_deployment():
    """k8s_patch_deployment → k8s_patch_resource alias."""
    from workers.autonomous_execute import run_execute_mutate_tool
    from workers.tools import TOOL_REGISTRY
    ctx = _make_ctx(settings=_make_settings(unrestricted=True))
    mock_fn = AsyncMock(return_value="patched ok")
    TOOL_REGISTRY["k8s_patch_resource"] = mock_fn
    try:
        out, code = await run_execute_mutate_tool(
            ctx, tool_name="k8s_patch_deployment",
            args={"namespace": "multi-agent"},
            trace_id="trace-one"
        )
        assert "patched ok" in out
        assert code == 0
    finally:
        TOOL_REGISTRY.pop("k8s_patch_resource", None)


# ---------------------------------------------------------------------------
# run_execute_mutate_tool — blast-radius reader unavailable (circuit breaker open)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_mutate_blast_radius_reader_unavailable_fails_closed():
    """Regression: when K8sBlastReader() raises (e.g. circuit breaker open), the
    destructive tool must still be hard-blocked via assess_blast_radius(None, ...),
    not silently allowed through by skipping the blast-radius check entirely."""
    from workers.autonomous_execute import run_execute_mutate_tool
    from workers.tools import TOOL_REGISTRY

    settings = _make_settings(unrestricted=True)
    settings.omni_blast_radius_enabled = True
    ctx = _make_ctx(settings=settings)
    mock_fn = AsyncMock(return_value="scale ok")
    TOOL_REGISTRY["k8s_scale_deployment"] = mock_fn
    try:
        with patch(
            "pkg.executor.blast_radius.K8sBlastReader",
            side_effect=RuntimeError("circuit breaker open"),
        ):
            out, code = await run_execute_mutate_tool(
                ctx, tool_name="k8s_scale_deployment",
                args={"namespace": "multi-agent", "deployment": "my-app", "replicas": 3},
                trace_id="trace-one",
            )
        assert code != 0
        assert "cannot bound blast radius" in out or "fail-closed" in out
        mock_fn.assert_not_called()
    finally:
        TOOL_REGISTRY.pop("k8s_scale_deployment", None)


# ---------------------------------------------------------------------------
# publish_action_feedback
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_publish_action_feedback_no_kafka():
    from workers.autonomous_execute import publish_action_feedback
    ctx = _make_ctx(kafka=None)
    # Should return without error when kafka is None
    await publish_action_feedback(
        ctx, trace_id="trace-one", tool_name="k8s_rollout_restart",
        correlation_id="c1", stdout="ok", stderr="", exit_code=0
    )


@pytest.mark.asyncio
async def test_publish_action_feedback_no_settings():
    from workers.autonomous_execute import publish_action_feedback
    ctx = SimpleNamespace(kafka=AsyncMock(), settings=None)
    await publish_action_feedback(
        ctx, trace_id="trace-one", tool_name="k8s_rollout_restart",
        correlation_id="c1", stdout="ok", stderr="", exit_code=0
    )


@pytest.mark.asyncio
async def test_publish_action_feedback_success():
    from workers.autonomous_execute import publish_action_feedback
    kafka = AsyncMock()
    kafka.send_dict = AsyncMock()
    ctx = _make_ctx(kafka=kafka)
    await publish_action_feedback(
        ctx, trace_id="trace-one", tool_name="k8s_rollout_restart",
        correlation_id="c1", stdout="done", stderr="", exit_code=0,
        status="ok"
    )
    kafka.send_dict.assert_called_once()
    call_args = kafka.send_dict.call_args
    assert call_args[0][0] == "omni-action-feedback"


@pytest.mark.asyncio
async def test_publish_action_feedback_failure_truncates():
    from workers.autonomous_execute import publish_action_feedback
    kafka = AsyncMock()
    kafka.send_dict = AsyncMock()
    ctx = _make_ctx(kafka=kafka)
    long_stdout = "error output " * 1000
    await publish_action_feedback(
        ctx, trace_id="trace-one", tool_name="k8s_scale_deployment",
        correlation_id="c1", stdout=long_stdout, stderr="some error",
        exit_code=1, status="fail"
    )
    kafka.send_dict.assert_called_once()


@pytest.mark.asyncio
async def test_publish_action_feedback_kafka_error_logged():
    from workers.autonomous_execute import publish_action_feedback
    kafka = AsyncMock()
    kafka.send_dict = AsyncMock(side_effect=Exception("kafka unavailable"))
    ctx = _make_ctx(kafka=kafka)
    # Should not raise — logs warning instead
    await publish_action_feedback(
        ctx, trace_id="trace-one", tool_name="k8s_rollout_restart",
        correlation_id="c1", stdout="ok", stderr="", exit_code=0
    )


@pytest.mark.asyncio
async def test_publish_action_feedback_with_skipped_reason():
    from workers.autonomous_execute import publish_action_feedback
    kafka = AsyncMock()
    kafka.send_dict = AsyncMock()
    ctx = _make_ctx(kafka=kafka)
    await publish_action_feedback(
        ctx, trace_id="trace-one", tool_name="k8s_rollout_restart",
        correlation_id="c1", stdout="", stderr="",
        exit_code=0, status="skipped",
        skipped_reason="dry run mode active"
    )
    kafka.send_dict.assert_called_once()


@pytest.mark.asyncio
async def test_publish_action_feedback_with_mutate_args():
    from workers.autonomous_execute import publish_action_feedback
    kafka = AsyncMock()
    kafka.send_dict = AsyncMock()
    ctx = _make_ctx(kafka=kafka)
    await publish_action_feedback(
        ctx, trace_id="trace-one", tool_name="k8s_scale_deployment",
        correlation_id="c1", stdout="scaled", stderr="",
        exit_code=0, mutate_args={"namespace": "multi-agent", "replicas": 3}
    )
    kafka.send_dict.assert_called_once()


# ---------------------------------------------------------------------------
# WS2 Decision Transparency Layer — CRAT DECISION_RENDERED (task #3)
# ---------------------------------------------------------------------------

def _make_ctx_with_redis(settings=None, kafka=None):
    import fakeredis.aioredis

    ctx = _make_ctx(settings=settings, kafka=kafka)
    ctx.redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    return ctx


@pytest.mark.asyncio
async def test_decision_rendered_written_on_deny():
    from workers.autonomous_execute import run_execute_mutate_tool
    from pkg.reasoning.reason_codes import ERR_GOV_UNAUTHORIZED_MUTATION

    kafka = AsyncMock()
    kafka.send_dict = AsyncMock()
    ctx = _make_ctx_with_redis(
        settings=_make_settings(force_nsenter=True, unrestricted=False), kafka=kafka,
    )
    out, code = await run_execute_mutate_tool(
        ctx, tool_name="k8s_rollout_restart", args={}, trace_id="trace-deny",
    )
    assert ERR_GOV_UNAUTHORIZED_MUTATION in out
    assert code == 1

    blocks = await ctx.redis.lrange("audit_chain:blocks", 0, -1)
    assert blocks, "DECISION_RENDERED must be written even when denied"
    block = json.loads(blocks[-1])
    assert block["event_type"] == "DECISION_RENDERED"
    assert block["payload"]["tool_name"] == "k8s_rollout_restart"
    assert block["payload"]["allow"] is False
    assert block["payload"]["reason_code"] == ERR_GOV_UNAUTHORIZED_MUTATION
    await ctx.redis.aclose()


@pytest.mark.asyncio
async def test_decision_rendered_written_on_allow():
    from workers.autonomous_execute import run_execute_mutate_tool
    from workers.tools import TOOL_REGISTRY

    kafka = AsyncMock()
    kafka.send_dict = AsyncMock()
    ctx = _make_ctx_with_redis(
        settings=_make_settings(force_nsenter=True, unrestricted=False), kafka=kafka,
    )
    mock_fn = AsyncMock(return_value="kubectl cluster output")
    TOOL_REGISTRY["kubectl_cluster"] = mock_fn
    try:
        out, code = await run_execute_mutate_tool(
            ctx, tool_name="kubectl_cluster",
            args={"namespace": "multi-agent"}, trace_id="trace-allow",
        )
    finally:
        del TOOL_REGISTRY["kubectl_cluster"]
    assert out == "kubectl cluster output"
    assert code == 0

    blocks = await ctx.redis.lrange("audit_chain:blocks", 0, -1)
    assert blocks, "DECISION_RENDERED must be written on allow too"
    block = json.loads(blocks[-1])
    assert block["event_type"] == "DECISION_RENDERED"
    assert block["payload"]["allow"] is True
    assert block["payload"]["reason_code"] is None
    await ctx.redis.aclose()
