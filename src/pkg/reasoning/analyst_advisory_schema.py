"""Advisory Mode output schema — Level 2 Autonomy (read-only analysis, structured forecasts)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

_LAYER_PATTERNS: list[tuple[str, list[str]]] = [
    ("prometheus", ["rate(", "predict_linear(", "irate(", "increase(", "avg_over_time("]),
    ("kubernetes", ["kubectl ", "helm ", "k8s.", "kube-"]),
    ("network", ["ss -", "ip route", "ip link", "mtr ", "dig ", "tcpdump", "nmap ", "curl ", "nslookup"]),
    ("os_baremetal", ["df ", "iostat", "dmesg", "journalctl", "lsblk", "top ", "top\n", "vmstat", "free ", "lscpu", "uptime", "du ", "sar "]),
]

_LAYER_TYPE = Literal["os_baremetal", "network", "kubernetes", "prometheus"]


def _infer_layer(command: str) -> _LAYER_TYPE:
    """Infer diagnostic layer from command text when the LLM omits the field."""
    cmd_lower = command.lower()
    for layer, patterns in _LAYER_PATTERNS:
        if any(p in cmd_lower for p in patterns):
            return layer  # type: ignore[return-value]
    return "kubernetes"


class VerificationStep(BaseModel):
    """Read-only command or query for human verification."""

    order: int = Field(gt=0, description="Step number")
    layer: _LAYER_TYPE = Field(
        default="kubernetes",
        description="Diagnostic layer: os_baremetal → network → kubernetes → prometheus",
    )
    command: str = Field(description="Exact shell, network, kubectl, or prometheus command (read-only)")
    expected_output: str = Field(default="", description="What healthy output looks like")
    rationale: str = Field(description="Why this step proves/disproves the root cause")

    @model_validator(mode="after")
    def infer_layer_from_command(self) -> "VerificationStep":
        # If the LLM omitted layer or left it at the default and the command
        # clearly belongs to a different layer, auto-correct silently.
        if self.layer == "kubernetes" and self.command:
            inferred = _infer_layer(self.command)
            if inferred != "kubernetes":
                object.__setattr__(self, "layer", inferred)
        return self


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

    @field_validator("severity", mode="before")
    @classmethod
    def coerce_severity(cls, v: object) -> object:
        # LLMs occasionally output "normal" — map to "healthy" before enum validation.
        if v == "normal":
            return "healthy"
        return v


class ForecastTimeline(BaseModel):
    """Complete time-series degradation model."""

    method: Literal["linear_extrapolation", "kill_chain", "heuristic"]
    basis: str = Field(
        default="",
        description="What evidence basis this forecast uses (e.g., 'prometheus predict_linear(rate[5m])')",
    )
    forecasts: list[ImpactForecast] = Field(default_factory=list, description="Severity at each timeframe")
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
