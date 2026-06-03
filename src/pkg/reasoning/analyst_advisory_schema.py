"""Advisory Mode output schema — Level 2 Autonomy (read-only analysis, structured forecasts)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

_MAX_VERIFICATION_STEPS = 5
_MAX_REMEDIATION_STEPS = 4
_ROOT_CAUSE_MAX_WORDS = 48
_AFFECTED_WORKLOAD_MAX_CHARS = 200
_VERIFICATION_RATIONALE_MAX_WORDS = 45
_VERIFICATION_EXPECTED_OUTPUT_MAX_WORDS = 48
_FORECAST_PREDICTION_MAX_WORDS = 40
_FORECAST_NOTE_MAX_WORDS = 80
_FORECAST_BASIS_MAX_CHARS = 320
_ESCALATION_MAX_WORDS = 48
_REMEDIATION_ACTION_MAX_WORDS = 72
_REMEDIATION_ROLLBACK_MAX_WORDS = 48
_PRECONDITION_MAX_WORDS = 28

_LAYER_PATTERNS: list[tuple[str, list[str]]] = [
    ("prometheus", ["rate(", "predict_linear(", "irate(", "increase(", "avg_over_time("]),
    ("kubernetes", ["kubectl ", "helm ", "k8s.", "kube-"]),
    ("network", ["ss -", "ip route", "ip link", "mtr ", "dig ", "tcpdump", "nmap ", "curl ", "nslookup"]),
    ("os_baremetal", ["df ", "iostat", "dmesg", "journalctl", "lsblk", "top ", "top\n", "vmstat", "free ", "lscpu", "uptime", "du ", "sar "]),
    ("database", ["mysql ", "proxysql", "mysqlcheck", "SHOW STATUS", "SHOW REPLICA", "SHOW SLAVE", "SELECT @@", "SELECT variable_name"]),
    ("storage", ["df -h", "df -T", "stat --file", "stat -f ", "mount -t", "nfsstat", "showmount"]),
    ("services", ["systemctl ", "journalctl -u", "systemctl show", "socat", "haproxy", "show stat", "show info"]),
]

_LAYER_TYPE = Literal["os_baremetal", "network", "kubernetes", "prometheus", "database", "storage", "services"]

VALID_LAYERS: frozenset[str] = frozenset(get_args(_LAYER_TYPE))


def normalize_layer(raw: str) -> str:
    """Map any string the LLM emits to a valid _LAYER_TYPE value."""
    if raw in VALID_LAYERS:
        return raw
    s = raw.lower()
    if any(x in s for x in ("os", "bare", "metal", "vm", "infra", "host", "layer 1", "layer_1", "l1 ")):
        return "os_baremetal"
    if any(x in s for x in ("network", "dns", "net ", "layer 2", "layer_2", "l2 ")):
        return "network"
    if any(x in s for x in ("kube", "k8s", "workload", "container", "application", "layer 3", "layer_3", "l3 ")):
        return "kubernetes"
    if any(x in s for x in ("prom", "metric", "monitor", "layer 4", "layer_4", "l4 ")):
        return "prometheus"
    if any(x in s for x in ("database", "db ", "mysql", "postgres", "proxysql", "mongo", "replication", "sql ")):
        return "database"
    if any(x in s for x in ("storage", "disk", "nfs", "mount", "filesystem", "inode", "raid")):
        return "storage"
    if any(x in s for x in ("service", "systemd", "daemon", "haproxy", "unit ")):
        return "services"
    return "kubernetes"


def _infer_layer(command: str) -> _LAYER_TYPE:
    """Infer diagnostic layer from command text when the LLM omits the field."""
    cmd_lower = command.lower()
    for layer, patterns in _LAYER_PATTERNS:
        if any(p in cmd_lower for p in patterns):
            return layer  # type: ignore[return-value]
    return "kubernetes"


def _truncate_words(text: str, max_words: int) -> tuple[str, bool]:
    if max_words <= 0:
        return "", bool((text or "").strip())
    words = (text or "").split()
    if len(words) <= max_words:
        return (text or "").strip(), False
    return " ".join(words[:max_words]).strip(), True


def _truncate_chars(text: str, max_chars: int) -> tuple[str, bool]:
    t = text or ""
    if max_chars <= 0:
        return "", bool(t.strip())
    if len(t) <= max_chars:
        return t, False
    return t[:max_chars].rstrip(), True


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

    @model_validator(mode="after")
    def clamp_verbose_fields(self) -> "VerificationStep":
        rationale, rc = _truncate_words(self.rationale, _VERIFICATION_RATIONALE_MAX_WORDS)
        expected_output, ec = _truncate_words(self.expected_output, _VERIFICATION_EXPECTED_OUTPUT_MAX_WORDS)
        if rc or ec:
            logger.warning(
                "event=advisory_field_clamped component=verification_step order=%s "
                "rationale_clamped=%s expected_output_clamped=%s",
                self.order,
                rc,
                ec,
            )
        object.__setattr__(self, "rationale", rationale)
        object.__setattr__(self, "expected_output", expected_output)
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

    @model_validator(mode="after")
    def clamp_remediation_text(self) -> "ProposedRemediationStep":
        action, ca = _truncate_words(self.action, _REMEDIATION_ACTION_MAX_WORDS)
        rollback, cr = _truncate_words(self.rollback_plan, _REMEDIATION_ROLLBACK_MAX_WORDS)
        new_pre: list[str] = []
        pre_changed = False
        for line in self.preconditions:
            pl, pcl = _truncate_words(line, _PRECONDITION_MAX_WORDS)
            new_pre.append(pl)
            pre_changed = pre_changed or pcl
        if ca or cr or pre_changed:
            logger.warning(
                "event=advisory_field_clamped component=proposed_remediation order=%s",
                self.order,
            )
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "rollback_plan", rollback)
        object.__setattr__(self, "preconditions", new_pre)
        return self


class ImpactForecast(BaseModel):
    """Time-based degradation forecast if the issue is left unaddressed."""

    timeframe: Literal["1h", "3h", "6h", "12h", "24h"]
    severity: Literal["healthy", "degraded", "critical", "catastrophic"]
    prediction: str = Field(
        description="What will happen (e.g., 'CPU utilization will exceed 95%', 'Data loss begins')",
    )
    confidence: Literal["high", "medium", "low"] = "medium"

    @field_validator("severity", mode="before")
    @classmethod
    def coerce_severity(cls, v: object) -> object:
        # LLMs occasionally output "normal" — map to "healthy" before enum validation.
        if v == "normal":
            return "healthy"
        return v

    @model_validator(mode="after")
    def clamp_prediction(self) -> "ImpactForecast":
        pred, clipped = _truncate_words(self.prediction, _FORECAST_PREDICTION_MAX_WORDS)
        if clipped:
            logger.warning(
                "event=advisory_field_clamped component=impact_forecast timeframe=%s",
                self.timeframe,
            )
        object.__setattr__(self, "prediction", pred)
        return self


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

    @model_validator(mode="after")
    def clamp_timeline_text(self) -> "ForecastTimeline":
        note, nc = _truncate_words(self.note, _FORECAST_NOTE_MAX_WORDS)
        basis, bc = _truncate_chars(self.basis, _FORECAST_BASIS_MAX_CHARS)
        if nc or bc:
            logger.warning("event=advisory_field_clamped component=forecast_timeline")
        object.__setattr__(self, "note", note)
        object.__setattr__(self, "basis", basis)
        return self


class AnalystAdvisory(BaseModel):
    """The complete structured output of the Advisory-Mode Analyst."""

    model_config = ConfigDict(extra="ignore")

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
    escalation_tier: Literal["L1_AUTO", "L2_SUGGEST", "L3_HITL"] = Field(
        default="L2_SUGGEST",
        description=(
            "L1_AUTO: high confidence, known SOP, no approval needed — execute when auto_execute=True. "
            "L2_SUGGEST: suggest to human for review. "
            "L3_HITL: low confidence or novel/escalated — human must decide."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _drop_placeholder_timestamp(cls, data: Any) -> Any:
        """LLMs often emit literal \"ISO8601\" instead of a real instant; drop so default_factory applies."""
        if not isinstance(data, dict):
            return data
        ts = data.get("timestamp")
        if not isinstance(ts, str):
            return data
        s = ts.strip()
        if not s:
            return {k: v for k, v in data.items() if k != "timestamp"}
        sl = s.lower()
        if sl in {"iso8601", "utc", "now", "datetime", "timestamp", "rfc3339"}:
            return {k: v for k, v in data.items() if k != "timestamp"}
        if "iso8601" in sl and len(s) < 20:
            return {k: v for k, v in data.items() if k != "timestamp"}
        return data

    @model_validator(mode="before")
    @classmethod
    def _cap_step_counts(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out = dict(data)
        vs = out.get("verification_steps")
        if isinstance(vs, list) and len(vs) > _MAX_VERIFICATION_STEPS:
            logger.warning(
                "event=advisory_step_list_truncated kind=verification_steps before=%s after=%s",
                len(vs),
                _MAX_VERIFICATION_STEPS,
            )
            out["verification_steps"] = vs[:_MAX_VERIFICATION_STEPS]
        pr = out.get("proposed_remediation")
        if isinstance(pr, list) and len(pr) > _MAX_REMEDIATION_STEPS:
            logger.warning(
                "event=advisory_step_list_truncated kind=proposed_remediation before=%s after=%s",
                len(pr),
                _MAX_REMEDIATION_STEPS,
            )
            out["proposed_remediation"] = pr[:_MAX_REMEDIATION_STEPS]
        return out

    @model_validator(mode="after")
    def clamp_top_level_prose(self) -> "AnalystAdvisory":
        root, rcl = _truncate_words(self.root_cause, _ROOT_CAUSE_MAX_WORDS)
        esc, ecl = _truncate_words(self.escalation_reason, _ESCALATION_MAX_WORDS)
        aff, acl = _truncate_chars(self.affected_workload, _AFFECTED_WORKLOAD_MAX_CHARS)
        if rcl or ecl or acl:
            logger.warning(
                "event=advisory_field_clamped component=analyst_advisory "
                "root_cause_clamped=%s escalation_clamped=%s affected_workload_clamped=%s",
                rcl,
                ecl,
                acl,
            )
        object.__setattr__(self, "root_cause", root)
        object.__setattr__(self, "escalation_reason", esc)
        object.__setattr__(self, "affected_workload", aff)
        return self


# Derived from AnalystAdvisory field Literal types — single source of truth.
_verdict_args = get_args(AnalystAdvisory.model_fields["verdict"].annotation)
_confidence_args = get_args(AnalystAdvisory.model_fields["confidence"].annotation)

VALID_VERDICTS: frozenset[str] = frozenset(_verdict_args)
VALID_CONFIDENCE_LEVELS: frozenset[str] = frozenset(_confidence_args)

# Safe repair defaults: least-alarming valid values used when LLM emits invalid literals.
DEFAULT_VERDICT: str = "INVESTIGATE"
DEFAULT_CONFIDENCE: str = "medium"

# Derived from sub-model field Literal types.
DEFAULT_LAYER: str = VerificationStep.model_fields["layer"].default
DEFAULT_FORECAST_METHOD: str = get_args(ForecastTimeline.model_fields["method"].annotation)[2]  # "heuristic"

# Repair fallbacks for missing required string fields — no Literal in schema, define here.
DEFAULT_COMMAND_FALLBACK: str = "echo 'no command provided'"
DEFAULT_ACTION_FALLBACK: str = "manual review required"


class AnalystAdvisoryAggregated(BaseModel):
    """Multiple advisory outputs aggregated for batch incidents."""

    model_config = ConfigDict(extra="ignore")

    advisories: list[AnalystAdvisory] = Field(description="One advisory per distinct incident")
    batch_summary: str = Field(
        default="",
        description="Prose summary of the batch (e.g., '3 pod crashes, 1 config issue')",
    )
