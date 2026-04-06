"""proactive fail-safe: tombstone k8s_state + EVENT_TIMEOUT."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from workers.proactive_observer import (
    AnomalyEvent,
    _fail_safe_after_tool_error,
    _process_proactive_message,
)
from workers.tools import ToolCallPayload


@pytest.mark.asyncio
async def test_fail_safe_audit_includes_k8s_state(monkeypatch: pytest.MonkeyPatch) -> None:
    audit_calls: list[dict] = []

    async def capture_audit(ctx, **kwargs: object) -> None:
        audit_calls.append(kwargs)

    monkeypatch.setattr("workers.proactive_observer._append_audit", capture_audit)
    monkeypatch.setattr("workers.proactive_observer._append_dlq_proactive", AsyncMock(return_value="dlq-m1"))
    monkeypatch.setattr("workers.proactive_observer._save_proactive_learning_record", AsyncMock())
    monkeypatch.setattr(
        "workers.proactive_observer.fetch_last_known_state",
        AsyncMock(return_value={"resourceVersion": "rv-99", "metadata": {"name": "web"}}),
    )
    monkeypatch.setattr("workers.proactive_observer.set_resource_freeze", AsyncMock(return_value="freeze-k"))

    ctx = MagicMock()
    ctx.settings.proactive_resource_freeze_enabled = True
    ctx.settings.proactive_freeze_key_prefix = "omni:proactive:freeze:res"
    ctx.settings.proactive_resource_freeze_ttl_sec = 120
    ctx.settings.proactive_freeze_namespace_fallback_allowed = False
    ctx.settings.proactive_k8s_snapshot_timeout_sec = 5.0
    ctx.telegram = None

    ev = AnomalyEvent(
        trace_id="trace-fs-1",
        rule_name="R1",
        canonical_query="cpu high",
        metric_value=9.0,
        threshold=1.0,
    )
    call = ToolCallPayload(tool="k8s_rollout_restart", args={"namespace": "ns1", "deployment": "web"})
    await _fail_safe_after_tool_error(
        ctx,
        ev,
        "trace-fs-1",
        "pk1",
        call,
        RuntimeError("boom"),
        reason_code="TOOL_EXCEPTION",
        stream_msg_id="stream-abc",
    )

    human = [c for c in audit_calls if c.get("outcome") == "REQUIRES_HUMAN_INTERVENTION"]
    assert len(human) == 1
    meta = human[0].get("meta") or {}
    assert meta.get("k8s_state", {}).get("resourceVersion") == "rv-99"
    assert meta.get("freeze_key") == "freeze-k"


@pytest.mark.asyncio
async def test_process_proactive_message_event_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    audit_calls: list[dict] = []

    async def capture_audit(ctx, **kwargs: object) -> None:
        audit_calls.append(kwargs)

    async def slow_pipeline(*_a: object, **_k: object) -> None:
        await asyncio.sleep(2.0)

    monkeypatch.setattr("workers.proactive_observer._append_audit", capture_audit)
    monkeypatch.setattr("workers.proactive_observer._proactive_event_pipeline", slow_pipeline)
    monkeypatch.setattr("workers.proactive_observer.proactive_kill_switch_engaged", AsyncMock(return_value=False))

    observed: list[float] = []

    def capture_duration(sec: float) -> None:
        observed.append(float(sec))

    monkeypatch.setattr("workers.proactive_observer.observe_proactive_incident_duration", capture_duration)

    ctx = MagicMock()
    ctx.settings.proactive_event_timeout_sec = 0.15
    ctx.settings.proactive_tool_timeout_sec = 0.05
    ctx.settings.proactive_kill_switch_key = "omni:proactive:kill_switch"
    ctx.settings.proactive_gigo_require_cluster_identity = True
    ctx.redis = AsyncMock()
    sem = MagicMock()
    sem.acquire_proactive = AsyncMock(return_value="tok")
    sem.release = AsyncMock()
    ctx.semaphore = sem

    ev = AnomalyEvent(
        trace_id="trace-et-1",
        rule_name="R1",
        canonical_query="cpu high",
        metric_value=9.0,
        threshold=1.0,
        namespace="lab",
        trigger_promql="up",
    )
    raw = json.dumps(ev.model_dump())
    await _process_proactive_message(ctx, "redis-msg-1", raw)

    timeouts = [c for c in audit_calls if c.get("outcome") == "EVENT_TIMEOUT"]
    assert len(timeouts) == 1
    assert timeouts[0].get("meta", {}).get("msg_id") == "redis-msg-1"
    sem.release.assert_awaited()
    assert len(observed) == 1
    assert observed[0] >= 0
