from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from pkg.reasoning.reason_codes import ERR_REA_NO_PHYSICAL_PROOF, ERR_REA_SIGMA_GATE_BLOCKED
from workers.evidence_consumer import _proof_of_fault_gate


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


@pytest.mark.asyncio
async def test_proof_gate_blocks_without_critical_evidence() -> None:
    ctx = SimpleNamespace(
        redis=_RedisStub({"dr": True, "z_cpu": 4.2, "z_mem": 0.1}),
        settings=SimpleNamespace(baseline_dr_z_threshold=3.0, autonomous_sigma_observation_window=1),
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
async def test_proof_gate_blocks_when_sigma_not_met() -> None:
    ctx = SimpleNamespace(
        redis=_RedisStub({"dr": False, "z_cpu": 1.2, "z_mem": 0.8}),
        settings=SimpleNamespace(baseline_dr_z_threshold=3.0, autonomous_sigma_observation_window=1),
    )
    ok, reason, _meta = await _proof_of_fault_gate(
        ctx,
        trace="t-proof-2",
        batch=[{"alert_hint": "CrashLoopBackOff waiting"}],
    )
    assert ok is False
    assert reason == ERR_REA_SIGMA_GATE_BLOCKED


@pytest.mark.asyncio
async def test_proof_gate_requires_observation_window() -> None:
    ctx = SimpleNamespace(
        redis=_RedisStub({"dr": True, "z_cpu": 3.4, "z_mem": 0.2}),
        settings=SimpleNamespace(baseline_dr_z_threshold=3.0, autonomous_sigma_observation_window=2),
    )
    batch = [{"alert_hint": "CreateContainerError waiting"}]
    ok1, reason1, _ = await _proof_of_fault_gate(ctx, trace="t-proof-3", batch=batch)
    ok2, reason2, _ = await _proof_of_fault_gate(ctx, trace="t-proof-3", batch=batch)
    assert ok1 is False and reason1 == ERR_REA_SIGMA_GATE_BLOCKED
    assert ok2 is True and reason2 == ""
