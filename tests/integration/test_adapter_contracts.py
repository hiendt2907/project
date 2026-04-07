from __future__ import annotations

import pytest

from workers.adapters.mock_external_adapter import (
    MockActuatorAdapter,
    MockIngressAdapter,
    MockPlannerAdapter,
    MockProbeAdapter,
    MockVerifierAdapter,
)


@pytest.mark.asyncio
async def test_mock_adapter_chain_preserves_core_contract() -> None:
    ingress = MockIngressAdapter()
    probe = MockProbeAdapter()
    planner = MockPlannerAdapter()
    actuator = MockActuatorAdapter()
    verifier = MockVerifierAdapter()

    ev = await ingress.ingest({"trace_id": "adp-trace-1", "source": "mock", "resource": "svc-a"})
    diag = await probe.diagnose(ev)
    plan = await planner.plan(ev, diag)
    assert plan is not None
    result = await actuator.execute(plan)
    verdict = await verifier.verify(ev, result)

    assert verdict["trace_id"] == "adp-trace-1"
    assert verdict["verified"] is True
