"""Mock external adapter for portability/contract tests."""

from __future__ import annotations

from typing import Any

from workers.adapters.contracts import (
    ActuatorAdapter,
    AdapterEvent,
    AdapterExecutionResult,
    AdapterPlan,
    IngressAdapter,
    PlannerAdapter,
    ProbeAdapter,
    VerifierAdapter,
)


class MockIngressAdapter(IngressAdapter):
    async def ingest(self, raw: dict[str, Any]) -> AdapterEvent:
        return AdapterEvent(
            trace_id=str(raw.get("trace_id") or "mock-trace"),
            source=str(raw.get("source") or "mock"),
            payload=dict(raw),
        )


class MockProbeAdapter(ProbeAdapter):
    async def diagnose(self, ev: AdapterEvent) -> dict[str, Any]:
        return {"trace_id": ev.trace_id, "diagnosis": "mock_diagnosis"}


class MockPlannerAdapter(PlannerAdapter):
    async def plan(self, ev: AdapterEvent, diagnosis: dict[str, Any]) -> AdapterPlan | None:
        _ = diagnosis
        return AdapterPlan(
            trace_id=ev.trace_id,
            tool_name="mock_noop",
            args={"resource": ev.payload.get("resource", "unknown")},
            confidence=0.9,
        )


class MockActuatorAdapter(ActuatorAdapter):
    async def execute(self, plan: AdapterPlan) -> AdapterExecutionResult:
        return AdapterExecutionResult(
            trace_id=plan.trace_id,
            status="ok",
            exit_code=0,
            stdout=f"mock_executed:{plan.tool_name}",
            stderr="",
        )


class MockVerifierAdapter(VerifierAdapter):
    async def verify(self, ev: AdapterEvent, result: AdapterExecutionResult) -> dict[str, Any]:
        return {
            "trace_id": ev.trace_id,
            "verified": result.exit_code == 0,
            "status": result.status,
            "stdout_preview": result.stdout[:120],
        }
