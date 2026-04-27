"""Advisory Mode output schema — Level 2 Autonomy (read-only analysis, structured forecasts)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class VerificationStep(BaseModel):
    """Read-only command or query for human verification."""

    order: int = Field(gt=0, description="Step number")
    command: str = Field(description="Exact kubectl, prometheus, or shell command (read-only)")
    expected_output: str = Field(default="", description="What healthy output looks like")
    rationale: str = Field(description="Why this step proves/disproves the root cause")


class ProposedRemediationStep(BaseModel):
    """Suggested action for human execution (never auto-executed)."""

    order: int = Field(gt=0, description="Step number")
    action: str = Field(description="The remediation action (e.g., 'kubectl rollout restart ...')")
    args: dict[str, Any] = Field(
        default_factory=dict,
        description="Structured args for the action (namespace, name, key, value, etc.)",
    )
    preconditions: list[str] = Field(
        default_factory=list,
        description="Conditions that must be verified before execution",
    )
    approval_required: bool = Field(
        default=False,
        description="If True, escalate to HITL for human approval before execution",
    )
    rollback_plan: str = Field(
        default="",
        description="How to undo this action if it causes harm",
    )


class ImpactForecast(BaseModel):
    """Time-based degradation forecast if the issue is left unaddressed."""

    timeframe: Literal["1h", "3h", "6h", "12h", "24h"]
    severity: Literal["healthy", "degraded", "critical", "catastrophic"]
    prediction: str = Field(
        description="What will happen (e.g., 'CPU utilization will exceed 95%', 'Data loss begins')",
    )
    confidence: Literal["high", "medium", "low"]


class ForecastTimeline(BaseModel):
    """Complete time-series degradation model."""

    method: Literal["linear_extrapolation", "kill_chain", "heuristic"]
    basis: str = Field(
        default="",
        description="What evidence basis this forecast uses (e.g., 'prometheus predict_linear(rate[5m])')",
    )
    forecasts: list[ImpactForecast] = Field(description="Severity at each timeframe")
    note: str = Field(
        default="",
        description="If forecast is degraded, explain why (e.g., 'missing rate data')",
    )


class AnalystAdvisory(BaseModel):
    """The complete structured output of the Advisory-Mode Analyst."""

    trace_id: str = Field(description="Trace ID for correlation")
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    verdict: Literal["NORMAL", "INVESTIGATE", "URGENT", "CRITICAL"]
    root_cause: str = Field(
        description="Concise technical explanation of what is broken (one sentence, no speculation)",
    )
    confidence: Literal["high", "medium", "low"]
    affected_workload: str = Field(
        default="",
        description="namespace/deployment or 'unknown' if not identifiable",
    )
    verification_steps: list[VerificationStep] = Field(
        description="Read-only commands/queries the human should run to verify"
    )
    proposed_remediation: list[ProposedRemediationStep] = Field(
        description="Suggested actions (NEVER auto-executed); human decides approval"
    )
    forecast: ForecastTimeline = Field(
        description="Predicted system state degradation if unaddressed"
    )
    escalation_reason: str = Field(
        default="",
        description="Why this is being escalated (security, unknown-cause, out-of-scope, etc.)",
    )


class AnalystAdvisoryAggregated(BaseModel):
    """Multiple advisory outputs aggregated for batch incidents."""

    advisories: list[AnalystAdvisory] = Field(description="One advisory per distinct incident")
    batch_summary: str = Field(
        default="",
        description="Prose summary of the batch (e.g., '3 pod crashes, 1 config issue')",
    )
