"""God-Mode V3: ToolRegistry, scale guardrail, approval Redis, observation sanitize."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest
from fakeredis import FakeAsyncRedis
from pydantic import BaseModel, ValidationError

import workers.k8s_cluster_tools  # noqa: F401 — side-effect @register_tool
from workers.k8s_cluster_tools import ScaleDeploymentArgs, TailLogsArgs
from workers.observation_sanitize import sanitize_for_llm
from workers.tool_approval import APPROVAL_KEY_PREFIX, approval_status, request_approval
from workers.react_logging import log_react_json
from workers.tool_observation import prepare_tool_return_for_llm
from workers.tool_registry import ToolRegistry, get_tool_registry


@pytest.mark.asyncio
async def test_duplicate_tool_registration_raises() -> None:
    class M(BaseModel):
        x: int = 1

    async def h(_ctx: object, _m: M) -> str:
        return "ok"

    reg = ToolRegistry()
    reg.register("dup_test_tool", M, h)
    with pytest.raises(ValueError, match="duplicate"):
        reg.register("dup_test_tool", M, h)


def test_scale_replicas_high_allowed() -> None:
    """Full cluster: không giới hạn trên replicas (RBAC + quota trên cluster)."""
    m = ScaleDeploymentArgs(name="x", namespace="ns", replicas=500)
    assert m.replicas == 500


@pytest.mark.asyncio
async def test_registry_invoke_k8s_scale_replicas_negative_raises_validation_error() -> None:
    ctx = SimpleNamespace(settings=SimpleNamespace(tool_output_max_chars=1500))
    reg = get_tool_registry()
    with pytest.raises(ValidationError):
        await reg.invoke(
            ctx,
            "k8s_scale_deployment",
            {"name": "dep", "namespace": "ns", "replicas": -1},
        )


def test_tail_logs_lines_over_max_raises() -> None:
    with pytest.raises(ValidationError):
        TailLogsArgs(pod_name="p", namespace="ns", lines=501)


def test_schema_export_non_empty() -> None:
    from workers.tool_registry import get_tool_registry

    reg = get_tool_registry()
    sch = reg.json_schema_for("k8s_scale_deployment")
    assert isinstance(sch, dict)
    assert sch.get("properties")


def test_all_schemas_json_string() -> None:
    from workers.tool_registry import get_tool_registry

    s = get_tool_registry().all_schemas_json()
    assert "k8s_scale_deployment" in s
    assert len(s) > 50


@pytest.mark.asyncio
async def test_request_approval_returns_false_no_redis_escalation_contract() -> None:
    redis = FakeAsyncRedis()
    ctx = SimpleNamespace(
        redis=redis,
        settings=SimpleNamespace(telegram_admin_chat_id=None),
        telegram=None,
        inbound_trace_id="trace-t",
    )
    ok = await request_approval(
        ctx,
        tool_name="k8s_patch_resource",
        args_summary='{"x":1}',
        fp="fp_test",
    )
    assert ok is False
    keys = [k for k in await redis.keys(f"{APPROVAL_KEY_PREFIX}*")]
    assert len(keys) == 0


@pytest.mark.asyncio
async def test_approval_status_deprecated_returns_none() -> None:
    redis = FakeAsyncRedis()
    ctx = SimpleNamespace(redis=redis)
    st = await approval_status(ctx, "any")
    assert st is None


def test_prepare_tool_return_truncates_and_sanitizes() -> None:
    class Ws:
        tool_output_max_chars = 80

    ctx = SimpleNamespace(settings=Ws())
    raw = "a" * 500 + ' password="x"'
    out = prepare_tool_return_for_llm(ctx, raw)
    assert len(out) <= 85
    assert "password=" not in out or "[REDACTED]" in out


def test_list_tool_schemas_and_tools_json_for_prompt() -> None:
    from workers.tool_registry import get_tool_registry

    reg = get_tool_registry()
    d = reg.list_tool_schemas()
    assert "k8s_scale_deployment" in d
    j = reg.tools_json_for_prompt(max_chars=500)
    assert len(j) <= 501
    assert "…" in j or len(j) < 500


def test_sanitize_for_llm_redacts_common_patterns() -> None:
    raw = 'password="secret123" token=abc bearer sk-test'
    out = sanitize_for_llm(raw)
    assert "secret123" not in out
    assert "[REDACTED]" in out or "REDACT" in out


def test_acceptance_sanitize_bearer_jwt_and_password_supersecret() -> None:
    """Nghiệm thu: JWT Bearer và password=SuperSecret123! → [REDACTED]."""
    raw = (
        "auth: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U "
        "and password=SuperSecret123! tail"
    )
    out = sanitize_for_llm(raw)
    assert "[REDACTED]" in out
    assert "eyJhbGciOiJIUzI1Ni" not in out
    assert "SuperSecret123!" not in out


def test_acceptance_log_react_json_emits_parseable_v3_react_thought(caplog: pytest.LogCaptureFixture) -> None:
    """Nghiệm thu: một dòng JSON chuẩn với reasoning_path v3_react_thought."""
    caplog.set_level(logging.INFO, logger="workers.react_logging")
    log_react_json("v3_react_thought", fp="fpacc", turn=1, thought="correlate")
    assert caplog.records, "expected log line from log_react_json"
    msg = caplog.records[-1].getMessage()
    data = json.loads(msg)
    assert data["reasoning_path"] == "v3_react_thought"
    assert data["fp"] == "fpacc"
    assert data["turn"] == 1
