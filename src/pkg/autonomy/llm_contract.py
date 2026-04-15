"""Strict JSON contract for high-level remediation intent → mapped to mutate tools by executor/analyst."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ALLOWED_HIGH_LEVEL_ACTIONS: frozenset[str] = frozenset(
    {
        "noop",
        "rollout_restart",
        "patch_deployment_resource",
        "patch_configmap_key",
        "apply_rbac_least_privilege",
    }
)

# JSON Schema for prompts / evals (Ollama must emit one JSON object only).
STRICT_REMEDIATION_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["action", "target_ref", "namespace"],
    "additionalProperties": False,
    "properties": {
        "action": {
            "type": "string",
            "enum": sorted(ALLOWED_HIGH_LEVEL_ACTIONS),
            "description": "High-level verb; executor maps to k8s_* tools.",
        },
        "target_ref": {
            "type": "string",
            "description": "Workload name or binding/config name depending on action.",
        },
        "namespace": {"type": "string"},
        "reasoning": {"type": "string", "default": ""},
        "patch_json": {
            "type": "string",
            "description": "JSON merge patch for Deployment when action=patch_deployment_resource.",
        },
        "configmap_key": {"type": "string"},
        "configmap_value": {"type": "string"},
    },
}


class HighLevelRemediationPlan(BaseModel):
    """Validated shape for LLM output before tool routing (lab: full authority → still validate JSON)."""

    model_config = ConfigDict(extra="forbid")

    action: str = Field(
        ...,
        description="One of rollout_restart | patch_deployment_resource | patch_configmap_key | apply_rbac_least_privilege | noop",
    )
    target_ref: str = Field(default="", description="Deployment, ConfigMap, or ClusterRoleBinding name as applicable.")
    namespace: str = Field(default="", description="Kubernetes namespace for namespaced actions.")
    reasoning: str = Field(default="", max_length=2000)
    patch_json: str = Field(
        default="",
        max_length=12000,
        description="Strategic-merge JSON string for k8s_patch_resource (Deployment).",
    )
    configmap_key: str = Field(default="", max_length=256)
    configmap_value: str = Field(default="", max_length=4096)

    @field_validator("action")
    @classmethod
    def _action_ok(cls, v: str) -> str:
        s = str(v or "").strip()
        if s not in ALLOWED_HIGH_LEVEL_ACTIONS:
            raise ValueError(f"action must be one of {sorted(ALLOWED_HIGH_LEVEL_ACTIONS)}")
        return s

    @model_validator(mode="after")
    def _action_specific_fields(self) -> HighLevelRemediationPlan:
        a = self.action
        if a == "patch_deployment_resource":
            if len(self.patch_json.strip()) < 2:
                raise ValueError("patch_deployment_resource requires non-empty patch_json")
            if not self.namespace.strip() or not self.target_ref.strip():
                raise ValueError("patch_deployment_resource requires namespace and target_ref")
        if a == "patch_configmap_key":
            if not self.namespace.strip() or not self.target_ref.strip():
                raise ValueError("patch_configmap_key requires namespace and target_ref (configmap name)")
            if not self.configmap_key.strip():
                raise ValueError("patch_configmap_key requires configmap_key (value may be empty string)")
        if a == "rollout_restart":
            if not self.namespace.strip() or not self.target_ref.strip():
                raise ValueError("rollout_restart requires namespace and target_ref (deployment name)")
        if a == "apply_rbac_least_privilege":
            if not self.namespace.strip():
                raise ValueError("apply_rbac_least_privilege requires namespace")
            if not self.target_ref.strip():
                raise ValueError("apply_rbac_least_privilege requires target_ref (ClusterRoleBinding to remove)")
        return self


_JSON_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


# ---------------------------------------------------------------------------
# RemediationContext — stateful closed-loop memory across iterations.
# ---------------------------------------------------------------------------

from dataclasses import dataclass, field as _field


@dataclass
class ObservationRecord:
    """Snapshot of what the probe saw at a given iteration."""

    iteration: int
    summary: str  # e.g. "Initial alert: OOMKilled in prod/api-server-xyz. message=…"


@dataclass
class ActionRecord:
    """LLM-decided action taken at a given iteration."""

    iteration: int
    action: str
    target_ref: str
    namespace: str
    reasoning: str


@dataclass
class OutcomeRecord:
    """SDK verification result after executing an action at a given iteration."""

    iteration: int
    healthy: bool
    summary: str  # e.g. "UNHEALTHY: desired=1 available=0 ready=0. Pod in CreateContainerConfigError"


@dataclass
class RemediationContext:
    """
    Stateful memory for one closed-loop remediation trace.

    Passed into the LLM system prompt on iterations > 1 so the model can
    reason about what has already been tried and what the current state is,
    instead of repeating the same action.
    """

    trace_id: str
    alertname: str
    namespace: str
    iterations: int = 0
    converged: bool = False
    resolution_state: str = "incomplete"
    observations: list[ObservationRecord] = _field(default_factory=list)
    actions_taken: list[ActionRecord] = _field(default_factory=list)
    outcomes: list[OutcomeRecord] = _field(default_factory=list)

    def to_prompt_block(self) -> str:
        """
        Serialize the remediation history into a structured block injected
        into the LLM system prompt on re-runs.

        The model receives what was observed, what was tried, and whether
        the workload recovered — enabling it to reason about next steps
        without hardcoded if/else logic.
        """
        if not self.iterations:
            return ""

        lines: list[str] = [f"=== REMEDIATION HISTORY (trace={self.trace_id}) ==="]
        for i in range(1, self.iterations + 1):
            obs = next((o for o in self.observations if o.iteration == i), None)
            act = next((a for a in self.actions_taken if a.iteration == i), None)
            out = next((o for o in self.outcomes if o.iteration == i), None)
            lines.append(f"\n--- Iteration {i} ---")
            if obs:
                lines.append(f"  Probe:   {obs.summary}")
            if act:
                lines.append(f"  Action:  {act.action} | target={act.target_ref} | ns={act.namespace}")
                lines.append(f"  Reason:  {act.reasoning[:300]}")
            if out:
                status = "HEALTHY" if out.healthy else "UNHEALTHY"
                lines.append(f"  Outcome: {status} — {out.summary}")

        lines += [
            "",
            "=== END HISTORY ===",
            "Based on the above history, determine the NEXT remediation action.",
            "Guidelines:",
            "  - If the workload is HEALTHY in the last outcome, use noop.",
            "  - If a dependency resource was created or patched but the workload",
            "    is still UNHEALTHY, consider rollout_restart to force pods to reload.",
            "  - Do not repeat an action that produced an UNHEALTHY outcome without",
            "    a clear reason to believe the second attempt will differ.",
            "  - Reason from observed state, not from assumptions about the fault type.",
        ]
        return "\n".join(lines)


def map_high_level_plan_to_mutate(plan: HighLevelRemediationPlan) -> dict[str, Any] | None:
    """
    Bridge strict LLM JSON → ``run_execute_mutate_tool`` shape (tool_name + args).

    Returns None for ``noop``. Callers must still enforce allowlist / env_mode gates.
    """
    ns = plan.namespace.strip()
    tgt = plan.target_ref.strip()
    if plan.action == "noop":
        return None
    if plan.action == "rollout_restart":
        if not ns or not tgt:
            return None
        return {"tool_name": "k8s_rollout_restart", "args": {"namespace": ns, "deployment": tgt}}
    if plan.action == "patch_deployment_resource":
        return {
            "tool_name": "k8s_patch_resource",
            "args": {
                "resource_type": "Deployment",
                "namespace": ns,
                "name": tgt,
                "patch_json": plan.patch_json.strip(),
            },
        }
    if plan.action == "patch_configmap_key":
        return {
            "tool_name": "k8s_create_or_patch_configmap",
            "args": {
                "namespace": ns,
                "name": tgt,
                "key": plan.configmap_key.strip(),
                "value": plan.configmap_value.strip(),
            },
        }
    if plan.action == "apply_rbac_least_privilege":
        return {
            "tool_name": "k8s_apply_rbac_least_privilege",
            "args": {
                "namespace": ns or "multi-agent",
                "remove_cluster_admin_binding": tgt or "omni-worker-cluster-admin",
            },
        }
    return None


def parse_high_level_plan_json(raw: str) -> HighLevelRemediationPlan | None:
    """
    Parse model output: strip optional ```json fences, then json.loads + Pydantic.
    Returns None if not parseable (caller may retry / escalate).
    """
    t = (raw or "").strip()
    if not t:
        return None
    m = _JSON_FENCE.search(t)
    if m:
        t = m.group(1).strip()
    try:
        obj = json.loads(t)
    except json.JSONDecodeError:
        # Single JSON object somewhere in the buffer
        start = t.find("{")
        end = t.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            obj = json.loads(t[start : end + 1])
        except json.JSONDecodeError:
            return None
    if not isinstance(obj, dict):
        return None
    try:
        return HighLevelRemediationPlan.model_validate(obj)
    except Exception:
        return None
