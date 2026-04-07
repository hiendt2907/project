"""Kubernetes-flavored adapter implementations backed by current worker primitives."""

from __future__ import annotations

from typing import Any

from workers.adapters.contracts import (
    ActuatorAdapter,
    AdapterEvent,
    AdapterExecutionResult,
    AdapterPlan,
    PlannerAdapter,
    ProbeAdapter,
    VerifierAdapter,
)
from workers.autonomous_execute import run_execute_mutate_tool
from workers.diagnostic_dispatcher import run_diagnostic_pipeline
from workers.proactive_models import AnomalyEvent


class K8sProbeAdapter(ProbeAdapter):
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    async def diagnose(self, ev: AdapterEvent) -> dict[str, Any]:
        model = AnomalyEvent.model_validate(ev.payload)
        await run_diagnostic_pipeline(self.ctx, model)
        return {"status": "diagnostic_dispatched", "trace_id": model.trace_id}


class K8sPlannerAdapter(PlannerAdapter):
    async def plan(self, ev: AdapterEvent, diagnosis: dict[str, Any]) -> AdapterPlan | None:
        tool_name = str((ev.payload.get("plan") or {}).get("tool_name") or "").strip()
        args = (ev.payload.get("plan") or {}).get("args")
        if not tool_name or not isinstance(args, dict):
            return None
        return AdapterPlan(trace_id=ev.trace_id, tool_name=tool_name, args=args, confidence=0.5)


class K8sActuatorAdapter(ActuatorAdapter):
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    async def execute(self, plan: AdapterPlan) -> AdapterExecutionResult:
        out, code = await run_execute_mutate_tool(
            self.ctx,
            tool_name=plan.tool_name,
            args=plan.args,
            trace_id=plan.trace_id,
        )
        return AdapterExecutionResult(
            trace_id=plan.trace_id,
            status="ok" if code == 0 else "error",
            exit_code=code,
            stdout=out,
            stderr="",
        )


class K8sVerifierAdapter(VerifierAdapter):
    async def verify(self, ev: AdapterEvent, result: AdapterExecutionResult) -> dict[str, Any]:
        return {
            "trace_id": ev.trace_id,
            "verified": result.exit_code == 0,
            "status": result.status,
        }
