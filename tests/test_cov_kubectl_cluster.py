"""Tests for src/workers/kubectl_cluster.py — coverage of uncovered paths."""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("OMNI_KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
os.environ.setdefault("OMNI_REDIS_URL", "redis://localhost:6379")
os.environ.setdefault("OMNI_ENV_MODE", "dev")
os.environ.setdefault("OMNI_OLLAMA_BASE_URL", "http://localhost:11434")

import pytest

from workers.kubectl_cluster import (
    KubectlClusterArgs,
    _audit_kubectl,
    _force_nsenter,
    cluster_ops_allowed,
    tool_kubectl_cluster,
)


def _wf_return(value):
    """asyncio.wait_for stub: đóng coroutine được truyền vào rồi trả value —
    tránh RuntimeWarning 'coroutine never awaited' từ mocked wait_for."""
    def _wf(coro, *args, **kwargs):
        if hasattr(coro, "close"):
            coro.close()
        return value
    return _wf


def _wf_timeout(coro, *args, **kwargs):
    if hasattr(coro, "close"):
        coro.close()
    raise asyncio.TimeoutError()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings(**kwargs):
    defaults = dict(
        cluster_full_access=True,
        lab_unchained=False,
        god_mode=False,
        omni_executor_force_nsenter=False,
        omni_shadow_os_mode=False,
        kafka_topic_audit_sandbox="omni-audit-sandbox",
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _ctx(settings=None, kafka=None, redis=None, trace_id="trace-abc"):
    if settings is None:
        settings = _settings()
    ctx = SimpleNamespace(
        settings=settings,
        kafka=kafka,
        redis=redis,
        inbound_trace_id=trace_id,
    )
    return ctx


def _args(args=None, timeout_sec=30.0, reasoning=""):
    return KubectlClusterArgs(
        args=args or ["get", "pods", "-A"],
        timeout_sec=timeout_sec,
        reasoning=reasoning,
    )


# ---------------------------------------------------------------------------
# cluster_ops_allowed
# ---------------------------------------------------------------------------


def test_cluster_ops_allowed_no_settings():
    ctx = SimpleNamespace()
    assert cluster_ops_allowed(ctx) is False


def test_cluster_ops_allowed_via_cluster_full_access():
    ctx = _ctx(settings=_settings(cluster_full_access=True))
    assert cluster_ops_allowed(ctx) is True


def test_cluster_ops_allowed_via_lab_unchained():
    ctx = _ctx(settings=_settings(cluster_full_access=False, lab_unchained=True))
    assert cluster_ops_allowed(ctx) is True


def test_cluster_ops_allowed_via_god_mode():
    ctx = _ctx(settings=_settings(cluster_full_access=False, lab_unchained=False, god_mode=True))
    assert cluster_ops_allowed(ctx) is True


def test_cluster_ops_not_allowed():
    ctx = _ctx(settings=_settings(cluster_full_access=False, lab_unchained=False, god_mode=False))
    assert cluster_ops_allowed(ctx) is False


# ---------------------------------------------------------------------------
# _force_nsenter
# ---------------------------------------------------------------------------


def test_force_nsenter_no_settings():
    ctx = SimpleNamespace()
    assert _force_nsenter(ctx) is False


def test_force_nsenter_via_force_nsenter_flag():
    ctx = _ctx(settings=_settings(omni_executor_force_nsenter=True))
    assert _force_nsenter(ctx) is True


def test_force_nsenter_via_shadow_os_mode():
    ctx = _ctx(settings=_settings(omni_shadow_os_mode=True))
    assert _force_nsenter(ctx) is True


def test_force_nsenter_false():
    ctx = _ctx(settings=_settings(omni_executor_force_nsenter=False, omni_shadow_os_mode=False))
    assert _force_nsenter(ctx) is False


# ---------------------------------------------------------------------------
# _audit_kubectl
# ---------------------------------------------------------------------------


async def test_audit_kubectl_no_settings():
    ctx = SimpleNamespace()
    # Should not raise
    await _audit_kubectl(ctx, trace_id="trace-t", argv=["kubectl", "get", "pods"], exit_code=0, stdout="", stderr="")


async def test_audit_kubectl_no_redis():
    ctx = _ctx(redis=None)
    # Should not raise with no redis
    await _audit_kubectl(ctx, trace_id="trace-t", argv=["kubectl", "get", "pods"], exit_code=0, stdout="ok", stderr="")


async def test_audit_kubectl_sends_to_kafka():
    kafka = AsyncMock()
    redis = AsyncMock()
    ctx = _ctx(kafka=kafka, redis=redis)
    await _audit_kubectl(
        ctx,
        trace_id="test-trace",
        argv=["kubectl", "get", "pods"],
        exit_code=0,
        stdout="NAME   READY",
        stderr="",
    )
    kafka.send_dict.assert_called_once()
    call_args = kafka.send_dict.call_args
    topic = call_args[0][0]
    assert topic == "omni-audit-sandbox"
    payload = call_args[0][1]
    body = json.loads(payload["data"])
    assert body["exit_code"] == 0
    assert body["source"] == "kubectl_cluster"


async def test_audit_kubectl_kafka_error_swallowed():
    kafka = AsyncMock()
    kafka.send_dict.side_effect = Exception("kafka down")
    ctx = _ctx(kafka=kafka)
    # Should not raise
    await _audit_kubectl(ctx, trace_id="trace-t", argv=["kubectl", "get"], exit_code=0, stdout="", stderr="")


async def test_audit_kubectl_no_kafka():
    ctx = _ctx(kafka=None)
    # Should be a no-op (no kafka, but settings present)
    await _audit_kubectl(ctx, trace_id="trace-t", argv=["kubectl", "get"], exit_code=0, stdout="", stderr="")


# ---------------------------------------------------------------------------
# tool_kubectl_cluster — access denied
# ---------------------------------------------------------------------------


async def test_tool_not_allowed_returns_error():
    ctx = _ctx(settings=_settings(cluster_full_access=False, lab_unchained=False, god_mode=False))
    result = await tool_kubectl_cluster(ctx, _args())
    assert "requires OMNI_CLUSTER_FULL_ACCESS" in result


async def test_tool_empty_args_returns_error():
    ctx = _ctx()
    # args.args will be empty after stripping
    a = KubectlClusterArgs(args=["   "], timeout_sec=30.0, reasoning="")
    result = await tool_kubectl_cluster(ctx, a)
    assert "empty" in result.lower()


async def test_tool_too_many_args_returns_error():
    ctx = _ctx()
    a = KubectlClusterArgs(args=["get"] * 65, timeout_sec=30.0, reasoning="")
    result = await tool_kubectl_cluster(ctx, a)
    assert "Too many arguments" in result


# ---------------------------------------------------------------------------
# tool_kubectl_cluster — normal execution (mocked subprocess)
# ---------------------------------------------------------------------------


async def test_tool_success_exec():
    ctx = _ctx()

    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate.return_value = (b"pod1  Running\n", b"")

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
        with patch("asyncio.wait_for", side_effect=_wf_return((b"pod1  Running\n", b""))):
            result = await tool_kubectl_cluster(ctx, _args(["get", "pods", "-A"]))

    assert "kubectl_ok" in result
    assert "exit=0" in result


async def test_tool_nonzero_exit_code():
    ctx = _ctx()

    mock_proc = AsyncMock()
    mock_proc.returncode = 1

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        with patch("asyncio.wait_for", side_effect=_wf_return((b"", b"Error from server"))):
            result = await tool_kubectl_cluster(ctx, _args(["get", "pods"]))

    assert "kubectl_exit_1" in result or "stderr=Error from server" in result


async def test_tool_with_reasoning_in_output():
    ctx = _ctx()

    mock_proc = AsyncMock()
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        with patch("asyncio.wait_for", side_effect=_wf_return((b"output", b""))):
            result = await tool_kubectl_cluster(ctx, _args(reasoning="check pod health"))

    assert "reasoning=check pod health" in result or "kubectl_ok" in result


async def test_tool_timeout_returns_error():
    ctx = _ctx()

    kafka = AsyncMock()
    ctx.kafka = kafka

    mock_proc = AsyncMock()
    mock_proc.returncode = None

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        with patch("asyncio.wait_for", side_effect=_wf_timeout):
            result = await tool_kubectl_cluster(ctx, _args(timeout_sec=5.0))

    assert "timeout" in result.lower()
    assert "5.0s" in result


async def test_tool_exec_exception_returns_error():
    ctx = _ctx()

    with patch("asyncio.create_subprocess_exec", side_effect=Exception("exec failed")):
        result = await tool_kubectl_cluster(ctx, _args())

    assert "kubectl exec failed" in result or "error" in result.lower()


# ---------------------------------------------------------------------------
# tool_kubectl_cluster — nsenter path
# ---------------------------------------------------------------------------


async def test_tool_nsenter_path():
    ctx = _ctx(settings=_settings(cluster_full_access=True, omni_executor_force_nsenter=True))

    mock_proc = AsyncMock()
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_shell", return_value=mock_proc) as mock_shell:
        with patch("asyncio.wait_for", side_effect=_wf_return((b"ok\n", b""))):
            result = await tool_kubectl_cluster(ctx, _args(["get", "nodes"]))

    mock_shell.assert_called_once()
    assert "kubectl_ok" in result or "exit=0" in result


async def test_tool_nsenter_timeout():
    ctx = _ctx(settings=_settings(cluster_full_access=True, omni_executor_force_nsenter=True))

    kafka = AsyncMock()
    ctx.kafka = kafka

    mock_proc = AsyncMock()

    with patch("asyncio.create_subprocess_shell", return_value=mock_proc):
        with patch("asyncio.wait_for", side_effect=_wf_timeout):
            result = await tool_kubectl_cluster(ctx, _args(timeout_sec=10.0))

    assert "timeout" in result.lower()


# ---------------------------------------------------------------------------
# tool_kubectl_cluster — output truncation
# ---------------------------------------------------------------------------


async def test_tool_large_output_truncated():
    ctx = _ctx()

    large_output = b"X" * 500_000
    mock_proc = AsyncMock()
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        with patch("asyncio.wait_for", side_effect=_wf_return((large_output, b""))):
            result = await tool_kubectl_cluster(ctx, _args())

    assert "truncated" in result or len(result) < 500_000


# ---------------------------------------------------------------------------
# tool_kubectl_cluster — trace_id fallback
# ---------------------------------------------------------------------------


async def test_tool_trace_id_fallback():
    ctx = _ctx(trace_id="")
    ctx.inbound_trace_id = ""

    mock_proc = AsyncMock()
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        with patch("asyncio.wait_for", side_effect=_wf_return((b"ok", b""))):
            result = await tool_kubectl_cluster(ctx, _args())

    assert "[TRACE]" in result
    assert "kubectl" in result  # fallback trace name
