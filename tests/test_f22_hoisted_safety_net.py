"""F22-residual: deterministic rollout safety-net resolves target via owner_reference
and is hoisted into the SDK-miss ESCALATE branch so the full alert→mutate chain closes
without the flaky LLM planner converging."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import workers.evidence_consumer as ec


def _batch(labels: dict) -> list[dict]:
    return [
        {
            "alert_hint": "Pod has been in a non-ready state (KubePodNotReady)",
            "canonical_query_snippet": json.dumps({"labels": labels}),
        }
    ]


@pytest.mark.asyncio
async def test_resolve_rollout_args_prefers_labels():
    ctx = SimpleNamespace()
    b = _batch({"namespace": "multi-agent", "deployment": "web-x", "alertname": "KubePodNotReady"})
    out = await ec._resolve_rollout_args(ctx, b)
    assert out == {"namespace": "multi-agent", "deployment": "web-x"}


@pytest.mark.asyncio
async def test_resolve_rollout_args_owner_reference_fallback(monkeypatch):
    # Alert carries only namespace+pod (no deployment label) — resolve via owner_reference.
    ctx = SimpleNamespace()
    b = _batch({"namespace": "multi-agent", "pod": "web-x-abc123-zzz", "alertname": "KubePodNotReady"})

    async def fake_resolve(ns, pod):
        assert ns == "multi-agent" and pod == "web-x-abc123-zzz"
        return ("Deployment", "web-x")

    monkeypatch.setattr(ec, "resolve_workload_from_pod", fake_resolve)
    out = await ec._resolve_rollout_args(ctx, b)
    assert out == {"namespace": "multi-agent", "deployment": "web-x"}


@pytest.mark.asyncio
async def test_resolve_rollout_args_unresolvable_returns_none(monkeypatch):
    ctx = SimpleNamespace()
    b = _batch({"namespace": "multi-agent", "pod": "orphan-pod", "alertname": "KubePodNotReady"})

    async def fake_resolve(ns, pod):
        return None

    monkeypatch.setattr(ec, "resolve_workload_from_pod", fake_resolve)
    assert await ec._resolve_rollout_args(ctx, b) is None


@pytest.mark.asyncio
async def test_synthesize_plan_for_notready_fault(monkeypatch):
    ctx = SimpleNamespace()
    b = _batch({"namespace": "multi-agent", "pod": "web-x-abc123-zzz", "alertname": "KubePodNotReady"})

    async def fake_resolve(ns, pod):
        return ("Deployment", "web-x")

    monkeypatch.setattr(ec, "resolve_workload_from_pod", fake_resolve)
    plan = await ec._synthesize_rollout_safety_net_plan(ctx, b)
    assert plan is not None
    assert plan["tool_name"] == "k8s_rollout_restart"
    assert plan["args"] == {"namespace": "multi-agent", "deployment": "web-x"}
    # Deterministic origin must bypass the LLM precondition re-ask gate.
    assert plan["planner_origin"].startswith("deterministic")


@pytest.mark.asyncio
async def test_synthesize_plan_none_for_non_eligible_fault():
    ctx = SimpleNamespace()
    # No fault keyword in hint/labels → not rollout-eligible → no synthesized plan.
    b = [{"alert_hint": "informational notice", "canonical_query_snippet": json.dumps({"labels": {"namespace": "x", "deployment": "y"}})}]
    assert await ec._synthesize_rollout_safety_net_plan(ctx, b) is None


# ── Proof-of-fault live ground-truth confirmation (closes the live E2E gap) ──

def _gate_ctx():
    from fakeredis.aioredis import FakeRedis

    return SimpleNamespace(
        redis=FakeRedis(decode_responses=True),
        settings=SimpleNamespace(
            baseline_dr_z_threshold=3.0,
            autonomous_sigma_observation_window=1,
            omni_proof_lane_enabled=True,
        ),
    )


@pytest.mark.asyncio
async def test_proof_gate_passes_when_live_workload_unavailable(monkeypatch):
    """KubePodNotReady has no sigma/critical evidence; a LIVE Ready=False confirmation
    is the physical proof that lets the rollout safety-net through the gate."""
    ctx = _gate_ctx()
    b = _batch({"namespace": "multi-agent", "pod": "web-x-abc-zzz", "alertname": "KubePodNotReady"})

    monkeypatch.setattr(ec, "critical_evidence_present", lambda batch: False)
    monkeypatch.setattr(ec, "_identity_from_batch", lambda batch: {"namespace": "multi-agent", "pod": "web-x-abc-zzz"})

    async def fake_confirm(ns, pod):
        return True

    monkeypatch.setattr(ec, "confirm_workload_unavailable", fake_confirm)
    ok, reason, meta = await ec._proof_of_fault_gate(ctx, trace="t-live-ok", batch=b)
    assert ok is True
    assert meta["physical_proof_source"] == "live_workload_unavailable_confirmed"
    assert meta["proof_lane"] == "state"


@pytest.mark.asyncio
async def test_proof_gate_blocks_when_live_workload_healthy(monkeypatch):
    """A false KubePodNotReady on a genuinely healthy pod must stay blocked
    (anti-hallucinate: the alert claim alone is never proof)."""
    ctx = _gate_ctx()
    b = _batch({"namespace": "multi-agent", "pod": "web-x-abc-zzz", "alertname": "KubePodNotReady"})

    monkeypatch.setattr(ec, "critical_evidence_present", lambda batch: False)
    monkeypatch.setattr(ec, "_identity_from_batch", lambda batch: {"namespace": "multi-agent", "pod": "web-x-abc-zzz"})

    async def fake_confirm(ns, pod):
        return False

    monkeypatch.setattr(ec, "confirm_workload_unavailable", fake_confirm)
    ok, reason, meta = await ec._proof_of_fault_gate(ctx, trace="t-live-block", batch=b)
    assert ok is False
    assert reason == ec.ERR_REA_NO_PHYSICAL_PROOF
