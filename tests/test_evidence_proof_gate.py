from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from pkg.reasoning import incident_matrix_profile as imp
from pkg.reasoning.reason_codes import ERR_REA_NO_PHYSICAL_PROOF, ERR_REA_SIGMA_GATE_BLOCKED
from workers.evidence_consumer import _proof_of_fault_gate


def _settings(**kwargs: object) -> SimpleNamespace:
    base = {
        "baseline_dr_z_threshold": 3.0,
        "autonomous_sigma_observation_window": 1,
        "omni_proof_lane_enabled": True,
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


class _RedisStub:
    def __init__(self, snapshot: dict[str, object]) -> None:
        self._data: dict[str, object] = {
            "omni:baseline:snapshot": json.dumps(snapshot),
        }

    async def get(self, key: str):
        return self._data.get(key)

    async def incr(self, key: str) -> int:
        cur = int(self._data.get(key, 0)) + 1
        self._data[key] = cur
        return cur

    async def expire(self, key: str, _ttl: int) -> bool:
        return True

    async def delete(self, key: str) -> int:
        self._data.pop(key, None)
        return 1


def _resource_batch(*, hint: str) -> list[dict[str, object]]:
    return [
        {
            "alert_hint": hint,
            "canonical_query_snippet": json.dumps(
                {
                    "labels": {
                        "alertname": "PodCpuUtilizationVsLimitHigh",
                        "namespace": "multi-agent",
                        "omni_proof_lane": "resource",
                    }
                }
            ),
        }
    ]


@pytest.mark.asyncio
async def test_proof_gate_blocks_without_critical_evidence() -> None:
    imp.invalidate_matrix_cache()
    ctx = SimpleNamespace(
        redis=_RedisStub({"dr": True, "z_cpu": 4.2, "z_mem": 0.1}),
        settings=_settings(),
    )
    ok, reason, meta = await _proof_of_fault_gate(
        ctx,
        trace="t-proof-1",
        batch=[{"alert_hint": "cpu high but no crash proof"}],
    )
    assert ok is False
    assert reason == ERR_REA_NO_PHYSICAL_PROOF
    assert meta["critical_evidence"] is False


@pytest.mark.asyncio
async def test_proof_gate_blocks_when_sigma_not_met_resource_lane() -> None:
    imp.invalidate_matrix_cache()
    ctx = SimpleNamespace(
        redis=_RedisStub({"dr": False, "z_cpu": 1.2, "z_mem": 0.8}),
        settings=_settings(autonomous_sigma_observation_window=1),
    )
    ok, reason, meta = await _proof_of_fault_gate(
        ctx,
        trace="t-proof-2",
        batch=_resource_batch(hint="CrashLoopBackOff waiting"),
    )
    assert ok is False
    assert reason == ERR_REA_SIGMA_GATE_BLOCKED
    assert meta.get("proof_lane") == "resource"


@pytest.mark.asyncio
async def test_proof_gate_requires_observation_window_resource_lane() -> None:
    imp.invalidate_matrix_cache()
    ctx = SimpleNamespace(
        redis=_RedisStub({"dr": True, "z_cpu": 3.4, "z_mem": 0.2}),
        settings=_settings(autonomous_sigma_observation_window=2),
    )
    batch = _resource_batch(hint="readiness probe fail for workload")
    ok1, reason1, _ = await _proof_of_fault_gate(ctx, trace="t-proof-3", batch=batch)
    ok2, reason2, _ = await _proof_of_fault_gate(ctx, trace="t-proof-3", batch=batch)
    assert ok1 is False and reason1 == ERR_REA_SIGMA_GATE_BLOCKED
    assert ok2 is True and reason2 == ""


@pytest.mark.asyncio
async def test_proof_gate_state_lane_heuristic_passes_without_sigma() -> None:
    imp.invalidate_matrix_cache()
    ctx = SimpleNamespace(
        redis=_RedisStub({"dr": False, "z_cpu": 0.1, "z_mem": 0.1}),
        settings=_settings(autonomous_sigma_observation_window=1),
    )
    ok, reason, meta = await _proof_of_fault_gate(
        ctx,
        trace="t-proof-state",
        batch=[{"alert_hint": "CrashLoopBackOff detected"}],
    )
    assert ok is True and reason == ""
    assert meta.get("proof_lane") == "state"


@pytest.mark.asyncio
async def test_proof_gate_legacy_sigma_block_without_annotation() -> None:
    """OMNI_PROOF_LANE_ENABLED=false: old path — log bypass off, sigma fail blocks."""
    imp.invalidate_matrix_cache()
    ctx = SimpleNamespace(
        redis=_RedisStub({"dr": False, "z_cpu": 1.2, "z_mem": 0.8}),
        settings=_settings(omni_proof_lane_enabled=False, autonomous_sigma_observation_window=1),
    )
    ok, reason, _ = await _proof_of_fault_gate(
        ctx,
        trace="t-proof-legacy",
        batch=[{"alert_hint": "CrashLoopBackOff waiting"}],
    )
    assert ok is False
    assert reason == ERR_REA_SIGMA_GATE_BLOCKED
