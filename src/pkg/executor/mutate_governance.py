"""Risk tier & prod posture gates for EXECUTE_MUTATE (executor boundary).

Runs before SDK invocation — complements planner-side proof/invariant gates.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from workers.env_mode import is_prod_mode, namespace_allowed

_K8S_DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


class MutateRiskTier(StrEnum):
    READ_ONLY = "read_only"
    LOW = "low_mutate"
    MEDIUM = "medium_mutate"
    HIGH = "high_mutate"
    BREAK_GLASS = "break_glass"


_TOOL_RISK: dict[str, MutateRiskTier] = {
    "k8s_rollout_restart": MutateRiskTier.LOW,
    "k8s_scale_deployment": MutateRiskTier.MEDIUM,
    "k8s_patch_resource": MutateRiskTier.MEDIUM,
    "k8s_patch_configmap": MutateRiskTier.MEDIUM,
    "k8s_patch_secret": MutateRiskTier.HIGH,
    "k8s_create_or_patch_configmap": MutateRiskTier.MEDIUM,
    "k8s_apply_rbac_least_privilege": MutateRiskTier.HIGH,
    "k8s_delete_pod": MutateRiskTier.HIGH,
    "kubectl_cluster": MutateRiskTier.BREAK_GLASS,
}

_TOOLS_REQUIRING_NAMESPACE = frozenset(
    {
        "k8s_rollout_restart",
        "k8s_scale_deployment",
        "k8s_patch_resource",
        "k8s_patch_configmap",
        "k8s_patch_secret",
        "k8s_create_or_patch_configmap",
        "k8s_delete_pod",
        "k8s_apply_rbac_least_privilege",
    }
)

_HIGH_RISK_TOOLS = frozenset(
    {"k8s_delete_pod", "k8s_patch_secret", "k8s_apply_rbac_least_privilege"}
)

# Tools that require namespace + prod allowlist (exported for contract tests).
MUTATING_POLICY_GUARD_TOOLS: frozenset[str] = _TOOLS_REQUIRING_NAMESPACE | frozenset(
    {"kubectl_cluster"}
)


def mutate_risk_tier(resolved_tool_name: str) -> MutateRiskTier | None:
    return _TOOL_RISK.get(resolved_tool_name)


def validate_k8s_dns_label(value: str, *, max_len: int = 63) -> bool:
    v = (value or "").strip()
    if not v or len(v) > max_len:
        return False
    return bool(_K8S_DNS_LABEL.match(v))


def governance_check_executor_mutate(
    *,
    settings: Any | None,
    resolved_tool_name: str,
    args: dict[str, Any] | None,
) -> tuple[bool, str]:
    """Returns (allow, stderr_style_message). Deny before TOOL_REGISTRY invoke."""
    raw_args = dict(args or {})
    tn = str(resolved_tool_name or "").strip()
    if tn == "kubectl_cluster":
        if settings is not None and is_prod_mode(settings):
            if not bool(getattr(settings, "omni_kubectl_cluster_mutate_allowed", False)):
                return False, (
                    "[DATA] error\n[DIAGNOSIS] reason_code=ERR_GOV_UNAUTHORIZED_MUTATION "
                    "kubectl_cluster blocked in OMNI_ENV_MODE=prod unless "
                    "OMNI_KUBECTL_CLUSTER_MUTATE_ALLOWED=true"
                )

    if tn in _HIGH_RISK_TOOLS and settings is not None:
        if is_prod_mode(settings) and not bool(getattr(settings, "omni_high_risk_mutate_allowed", False)):
            return False, (
                "[DATA] error\n[DIAGNOSIS] reason_code=ERR_GOV_UNAUTHORIZED_MUTATION "
                f"high_risk_tool_blocked={tn!r} in prod unless OMNI_HIGH_RISK_MUTATE_ALLOWED=true"
            )

    if tn in _TOOLS_REQUIRING_NAMESPACE:
        ns = str(raw_args.get("namespace") or "").strip()
        if not validate_k8s_dns_label(ns):
            return False, (
                "[DATA] error\n[DIAGNOSIS] reason_code=ERR_GOV_UNAUTHORIZED_MUTATION "
                f"mutate tool {tn!r} requires valid Kubernetes DNS-label namespace"
            )
        if settings is not None and is_prod_mode(settings) and not namespace_allowed(settings, ns):
            return False, (
                f"[DATA] error\n[DIAGNOSIS] reason_code=ERR_GOV_NS_OUT_OF_BOUNDS "
                f"namespace={ns!r} not in autonomous_allowed_namespaces"
            )

    if tn == "k8s_rollout_restart":
        dep = str(
            raw_args.get("deployment") or raw_args.get("name") or ""
        ).strip()
        if dep and not validate_k8s_dns_label(dep):
            return False, (
                "[DATA] error\n[DIAGNOSIS] reason_code=ERR_GOV_UNAUTHORIZED_MUTATION "
                "invalid deployment/workload DNS label"
            )

    return True, ""
