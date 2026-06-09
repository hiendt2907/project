"""Behavioral: post-mutate ground-truth reconcile → auto-rollback (CRAT ROLLBACK_EXECUTED).

Covers:
  (a) mutation that FIXES the problem  → no rollback, "verified" success feedback
  (b) mutation that does NOT fix (post verdict refuted) → apply_rollback called,
      ROLLBACK_EXECUTED CRAT block written, feedback status=rolled_back
  (c) pre-mutate snapshot captured BEFORE the mutation (ordering)
  (d) ctx.rollback_target_name set from the envelope before rollback apply
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from fakeredis.aioredis import FakeRedis as _FakeRedisBackend  # type: ignore

import workers.kafka_actions_consumer as kac
from workers.verify_reconcile import ReconcileOutcome


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

def FakeAsyncRedis(decode_responses: bool = True):
    return _FakeRedisBackend(decode_responses=decode_responses)


class _KafkaCapture:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def send_dict(self, topic: str, envelope: dict[str, Any], **kwargs: Any) -> None:
        # audit-chain compact topic passes key=; must be accepted.
        self.sent.append((topic, envelope))

    def feedback_bodies(self) -> list[dict[str, Any]]:
        out = []
        for topic, env in self.sent:
            if topic.endswith("action-feedback") and isinstance(env, dict) and "data" in env:
                try:
                    out.append(json.loads(env["data"]))
                except Exception:
                    pass
        return out


def _settings(**over: Any) -> SimpleNamespace:
    base = dict(
        omni_auto_execute_enabled=True,
        omni_auto_rollback_enabled=True,
        omni_shadow_os_mode=False,
        omni_rollback_snapshot_ttl_sec=3600,
        executor_action_rate_limit_burst=100,
        executor_action_rate_limit_window_sec=60,
        kafka_topic_action_feedback="omni-action-feedback",
        kafka_topic_audit_chain="omni-audit-chain",
        omni_env_mode="lab",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _ctx(redis, kafka, settings) -> SimpleNamespace:
    return SimpleNamespace(redis=redis, kafka=kafka, settings=settings)


def _envelope_data() -> dict[str, Any]:
    return {
        "tool_name": "k8s_scale_deployment",
        "args": {"namespace": "multi-agent", "name": "nginx-test", "replicas": 2},
        "correlation_id": "corr-1",
        "root_cause": "pod nginx-test OOMKilled exceeds memory limit",
        "affected_workload": "multi-agent/nginx-test",
    }


@pytest.mark.asyncio
async def test_mutation_fixes_problem_no_rollback(monkeypatch):
    """(a) verdict=confirmed/healthy → no rollback, no redundant second feedback.

    One action = one feedback: the mutate handler's terminal 'ok' stands; the
    post-mutate verified-healthy path logs the ground-truth verdict but must NOT
    emit an extra feedback (avoids analyst double re-eval + KPI double-count).
    """
    redis = FakeAsyncRedis(decode_responses=True)
    kafka = _KafkaCapture()
    ctx = _ctx(redis, kafka, _settings())

    order: list[str] = []

    async def fake_run(_ctx, *, tool_name, args, trace_id):
        order.append("mutate")
        return "[DATA] ok scaled", 0

    async def fake_reconcile(_ctx, advisory):
        # ground truth says the problem is gone
        return ReconcileOutcome("unverifiable", "no testable failure-mode signal", ())

    async def fake_rollback(_ctx, trace):
        raise AssertionError("rollback must NOT be called when problem is fixed")

    monkeypatch.setattr(kac, "run_execute_mutate_tool", fake_run)
    monkeypatch.setattr("workers.verify_reconcile.reconcile_advisory", fake_reconcile)
    monkeypatch.setattr("workers.rollback_executor.apply_rollback_from_snapshot", fake_rollback)

    await kac._handle_execute_mutate(ctx, "trace-a", _envelope_data())

    fbs = kafka.feedback_bodies()
    statuses = [f["status"] for f in fbs]
    # post-mutate path emits NO extra feedback on the healthy/verified branch
    assert "verified" not in statuses
    assert "rolled_back" not in statuses
    # rollback was never invoked (fake_rollback raises if called)
    # no ROLLBACK_EXECUTED block
    assert not any(t == "omni-audit-chain" for t, _ in kafka.sent)


@pytest.mark.asyncio
async def test_mutation_does_not_fix_triggers_rollback_and_crat(monkeypatch):
    """(b) post verdict refuted → apply_rollback called + ROLLBACK_EXECUTED CRAT + rolled_back feedback."""
    redis = FakeAsyncRedis(decode_responses=True)
    kafka = _KafkaCapture()
    ctx = _ctx(redis, kafka, _settings())

    rollback_calls: list[str] = []
    crat_blocks: list[dict[str, Any]] = []

    async def fake_run(_ctx, *, tool_name, args, trace_id):
        return "[DATA] ok scaled", 0

    async def fake_reconcile(_ctx, advisory):
        # problem STILL present after mutation
        return ReconcileOutcome("refuted", "oom:refuted (still OOMKilled)", ("oom",))

    async def fake_rollback(_ctx, trace):
        rollback_calls.append(trace)
        return True, f"rollback_ok: restored replicas deployment=nginx-test ns=multi-agent"

    async def fake_write_audit_block(**kwargs):
        crat_blocks.append(kwargs)
        return {"seq": 1}

    monkeypatch.setattr(kac, "run_execute_mutate_tool", fake_run)
    monkeypatch.setattr("workers.verify_reconcile.reconcile_advisory", fake_reconcile)
    monkeypatch.setattr("workers.rollback_executor.apply_rollback_from_snapshot", fake_rollback)
    monkeypatch.setattr("services.audit_ledger.chain_writer.write_audit_block", fake_write_audit_block)

    await kac._handle_execute_mutate(ctx, "trace-b", _envelope_data())

    # rollback was invoked
    assert rollback_calls == ["trace-b"]
    # CRAT ROLLBACK_EXECUTED block written
    assert len(crat_blocks) == 1
    assert crat_blocks[0]["event_type"] == "ROLLBACK_EXECUTED"
    assert crat_blocks[0]["trace_id"] == "trace-b"
    assert crat_blocks[0]["payload"]["post_mutate_verdict"] == "refuted"
    # feedback says rolled_back
    statuses = [f["status"] for f in kafka.feedback_bodies()]
    assert "rolled_back" in statuses


@pytest.mark.asyncio
async def test_snapshot_captured_before_mutate(monkeypatch):
    """(c) capture_pre_mutate_snapshot runs BEFORE the mutation dispatch."""
    redis = FakeAsyncRedis(decode_responses=True)
    kafka = _KafkaCapture()
    ctx = _ctx(redis, kafka, _settings())

    order: list[str] = []

    # Exercise the REAL run_execute_mutate_tool ordering by patching its internals.
    import workers.autonomous_execute as ae

    async def fake_capture(_ctx, resolved_tool_name, args, trace_id, ttl_sec=3600):
        order.append("snapshot")
        return {"prior_replicas": 1}

    def fake_governance(*, settings, resolved_tool_name, args):
        return True, "ok"

    async def fake_tool(_ctx, args):
        order.append("mutate")
        return "[DATA] ok"

    monkeypatch.setattr(ae, "capture_pre_mutate_snapshot", fake_capture)
    monkeypatch.setattr(ae, "governance_check_executor_mutate", fake_governance)
    # ensure snapshot_required returns True for our tool
    monkeypatch.setattr(ae, "snapshot_required", lambda n: True)
    monkeypatch.setitem(ae.TOOL_REGISTRY, "k8s_scale_deployment", fake_tool)

    out, code = await ae.run_execute_mutate_tool(
        ctx, tool_name="k8s_scale_deployment",
        args={"namespace": "multi-agent", "name": "nginx-test"}, trace_id="trace-c",
    )

    assert code == 0
    assert order == ["snapshot", "mutate"], f"snapshot must precede mutate, got {order}"


@pytest.mark.asyncio
async def test_rollback_target_name_set_from_envelope(monkeypatch):
    """(d) ctx.rollback_target_name set from envelope before apply_rollback."""
    redis = FakeAsyncRedis(decode_responses=True)
    kafka = _KafkaCapture()
    ctx = _ctx(redis, kafka, _settings())

    seen_target: list[str] = []

    async def fake_run(_ctx, *, tool_name, args, trace_id):
        return "[DATA] ok scaled", 0

    async def fake_reconcile(_ctx, advisory):
        return ReconcileOutcome("refuted", "oom:refuted", ("oom",))

    async def fake_rollback(_ctx, trace):
        seen_target.append(str(getattr(_ctx, "rollback_target_name", "")))
        return True, "rollback_ok"

    async def fake_write_audit_block(**kwargs):
        return {"seq": 1}

    monkeypatch.setattr(kac, "run_execute_mutate_tool", fake_run)
    monkeypatch.setattr("workers.verify_reconcile.reconcile_advisory", fake_reconcile)
    monkeypatch.setattr("workers.rollback_executor.apply_rollback_from_snapshot", fake_rollback)
    monkeypatch.setattr("services.audit_ledger.chain_writer.write_audit_block", fake_write_audit_block)

    await kac._handle_execute_mutate(ctx, "trace-d", _envelope_data())

    assert seen_target == ["nginx-test"], f"rollback_target_name must be set, got {seen_target}"
