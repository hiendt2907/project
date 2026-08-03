"""Deterministic Diagnostic Policy invariants (INV_*) — LLM cannot override."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterable, Sequence

from pkg.reasoning.evidence_signals import critical_evidence_present
from pkg.reasoning.reason_codes import (
    INV_DISCOVERY_MANDATORY,
    INV_NAMESPACE_ISOLATION,
    INV_NO_RESTART_ON_BROKEN_SPEC,
    INV_READ_BEFORE_MUTATE,
)

logger = logging.getLogger(__name__)

# Spec-facing names -> registry tool ids (read-only). Used for docs / future routing.
DISCOVERY_TOOL_ALIASES: dict[str, tuple[str, ...]] = {
    "k8s_inspect_resource": (
        "k8s_describe_resource",
        "inspect_pod_deep",
        "inspect_pod_details",
        "k8s_list_pods",
        "list_namespace_pods",
    ),
    "loki_pattern_analysis": ("k8s_tail_logs",),  # phased: log correlation via tail
    "prom_vector_context": ("query_prometheus_metrics", "k8s_check_endpoints"),
}

# Read-only registry tools used for metrics/logs/redis/service observability (not in K8s SDK readonly set).
OBSERVABILITY_READONLY_DISCOVERY_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "query_prometheus_metrics",
        "query_victoria_metrics",
        "query_vm_timeseries",
        "query_historical_metrics",
        "promql_instant",
        "promql_range",
        "vm_promql_instant",
        "vm_promql_range",
        "metrics_promql_hints",
        "timeseries_analyze",
        "redis_expert_check",
        "redis_health",
        "redis_info",
        "audit_observability_stack",
        "forecast_metric_prophet",
        "forecast_memory_risk_vm",
        "predict_resource_exhaustion",
    }
)

_DISCOVERY_PROBE_MARKERS: tuple[str, ...] = (
    "loki",
    "logql",
    "promql",
    "victoriametrics",
    "query_prometheus",
    "redis_expert",
    "redis_health",
    "audit_observability",
    "topology",
    "service_mesh",
    "endpoints",
)

_RE_BROKEN_SPEC = re.compile(
    r"(createcontainerconfigerror|createcontainererror|failedmount|"
    r"configmap.*not\s+found|secret.*not\s+found|references\s+non-existent|"
    r"no\s+such\s+configmap|could\s+not\s+find\s+configmap)",
    re.IGNORECASE,
)


def _batch_text_blob(batch: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for b in batch:
        parts.append(str(b.get("alert_hint") or ""))
        ef = b.get("extracted_fact")
        if isinstance(ef, dict):
            parts.append(json.dumps(ef, ensure_ascii=False))
        elif isinstance(ef, str):
            parts.append(ef)
        parts.append(str(b.get("canonical_query_snippet") or ""))
        # Pod events / probe raw (FailedMount, missing ConfigMap) often live only here.
        parts.append(str(b.get("raw") or ""))
    return "\n".join(parts)


def evidence_suggests_broken_spec(batch: list[dict[str, Any]]) -> bool:
    """True when evidence points to missing ConfigMap/Secret/mount (restart would not fix root cause)."""
    t = _batch_text_blob(batch)
    if _RE_BROKEN_SPEC.search(t):
        return True
    # Nested reason fields in JSON
    for b in batch:
        ef = b.get("extracted_fact")
        if not isinstance(ef, dict):
            continue
        for key in ("message", "reason", "detail", "error"):
            s = str(ef.get(key) or "")
            if _RE_BROKEN_SPEC.search(s):
                return True
    return False


def _discovery_alias_flat_names() -> frozenset[str]:
    out: set[str] = set()
    for _spec, names in DISCOVERY_TOOL_ALIASES.items():
        out.update(str(n).strip() for n in names if str(n).strip())
    return frozenset(out)


def discovery_satisfying_tool_names() -> frozenset[str]:
    """Union of DISCOVERY_TOOL_ALIASES registry ids, K8s readonly allowlist, and observability readonly tools."""
    try:
        from pkg.risk_taxonomy import READONLY_TOOL_ALLOWLIST
    except Exception:
        READONLY_TOOL_ALLOWLIST = frozenset()  # type: ignore[misc,assignment]
    return frozenset(READONLY_TOOL_ALLOWLIST) | OBSERVABILITY_READONLY_DISCOVERY_TOOL_NAMES | _discovery_alias_flat_names()


def batch_has_prior_readonly_evidence(batch: list[dict[str, Any]]) -> bool:
    """
    True when the batch already contains read-style probe output (satisfies INV_READ_BEFORE_MUTATE
    without an extra ReAct tool round).
    """
    sat_tools = discovery_satisfying_tool_names()
    blob_lower = _batch_text_blob(batch).lower()
    if any(m in blob_lower for m in _DISCOVERY_PROBE_MARKERS):
        return True
    for b in batch:
        pr = str(b.get("probe") or "").lower()
        if any(t in pr for t in sat_tools):
            return True
        if any(
            x in pr
            for x in (
                "k8s_clinical",
                "kubectl",
                "describe",
                "inspect",
                "events",
                "tail_logs",
                "list_pods",
                "prometheus",
            )
        ):
            return True
        ef = b.get("extracted_fact")
        if isinstance(ef, dict) and (ef.get("items") or ef.get("reason") or ef.get("message")):
            return True
        if isinstance(ef, str) and len(ef) > 40 and ("Warning" in ef or "reason" in ef.lower()):
            return True
    return False


def discovery_mandatory_satisfied(
    batch: list[dict[str, Any]],
    *,
    discovery_steps: Iterable[str] | None,
    readonly_executed: Sequence[str] | None = None,
) -> tuple[bool, dict[str, Any]]:
    """
    When OMNI_DISCOVERY_MANDATORY is enabled, EXECUTE_MUTATE must not proceed unless at least one
    discovery-equivalent signal exists: planner discovery_steps, successful readonly tools from trace
    memory, or batch probe/output consistent with DISCOVERY_TOOL_ALIASES / observability readonly paths.
    """
    meta: dict[str, Any] = {}
    tools = discovery_satisfying_tool_names()
    names: list[str] = []
    for src in (discovery_steps or (), readonly_executed or ()):
        for x in src:
            s = str(x).strip()
            if s:
                names.append(s)
    for nm in names:
        if nm in tools:
            meta["discovery_via_tool"] = nm
            return True, meta
    if batch_has_prior_readonly_evidence(batch):
        meta["discovery_via_batch"] = True
        return True, meta
    meta["discovery_missing"] = True
    return False, meta


def _inv_no_restart_broken_spec_blocks(
    proof_lane: str | None,
    batch: list[dict[str, Any]],
    *,
    meta: dict[str, Any],
) -> bool:
    """
    INV_NO_RESTART_ON_BROKEN_SPEC — final safety layer: safety over lane classification.

    Policy narrative: concern when ``proof_lane == "state"`` **or** physical broken-spec signals;
    **block restart** only when ``evidence_suggests_broken_spec(batch)`` (events/SDK text).
    Misclassified lane cannot hide missing ConfigMap/Secret evidence.
    """
    es = evidence_suggests_broken_spec(batch)
    meta["inv_no_restart_concern_zone"] = (proof_lane == "state") or es
    meta["proof_lane"] = proof_lane
    meta["evidence_suggests_broken_spec"] = es
    if es:
        meta["suggest_verdict"] = "SUGGEST_FIX_SOURCE"
        return True
    return False


def _rollout_read_before_defer_applies(proof_lane: str | None) -> bool:
    """
    INV_READ_BEFORE_MUTATE for rollout is prioritized for proof_lane state; unknown lane is conservative.
    resource/app_log lanes use other proof paths — do not defer rollout for missing ReAct discovery alone.
    """
    if proof_lane in ("resource", "app_log"):
        return False
    return True  # state, None, or other → require read-before when critical and no discovery


def evaluate_diagnostic_invariants(
    ws: Any,
    *,
    tool_name: str,
    args: dict[str, Any] | None,
    batch: list[dict[str, Any]],
    discovery_tool_names: list[str] | None,
    proof_lane: str | None = None,
    readonly_discovery_executed: list[str] | None = None,
) -> tuple[bool, str | None, dict[str, Any]]:
    """
    Returns (ok, invariant_reason_code_or_none, meta).

    Meta may include ``security_signal`` for INV_NAMESPACE_ISOLATION.
    """
    tn = str(tool_name or "").strip()
    ag = dict(args or {})
    meta: dict[str, Any] = {}
    disc = [str(x).strip() for x in (discovery_tool_names or []) if str(x).strip()]
    ro_exec = [str(x).strip() for x in (readonly_discovery_executed or []) if str(x).strip()]

    # INV_NAMESPACE_ISOLATION (mutating tools with namespace)
    ns = str(ag.get("namespace") or "").strip()
    if ns and (tn.startswith("k8s_") or tn == "kubectl_cluster"):
        try:
            from pkg.env_mode import namespace_allowed
        except Exception:
            namespace_allowed = None  # type: ignore[assignment]
        if namespace_allowed is not None and not namespace_allowed(ws, ns):
            meta["security_signal"] = True
            logger.error(
                "event=inv_namespace_isolation_block tool=%s namespace=%s",
                tn,
                ns,
            )
            return False, INV_NAMESPACE_ISOLATION, meta

    if bool(getattr(ws, "omni_discovery_mandatory", False)):
        try:
            from pkg.risk_taxonomy import MUTATE_TOOL_ALLOWLIST
        except Exception:
            MUTATE_TOOL_ALLOWLIST = frozenset()  # type: ignore[misc,assignment]
        if tn in MUTATE_TOOL_ALLOWLIST:
            ok_dm, dm_meta = discovery_mandatory_satisfied(
                batch,
                discovery_steps=disc,
                readonly_executed=ro_exec,
            )
            meta["discovery_mandatory"] = dm_meta
            if not ok_dm:
                return False, INV_DISCOVERY_MANDATORY, meta

    if tn != "k8s_rollout_restart":
        # Other mutates: namespace already checked; optional read-before for critical faults
        if tn in ("k8s_scale_deployment", "k8s_patch_resource", "kubectl_cluster"):
            if critical_evidence_present(batch) and not (
                disc or batch_has_prior_readonly_evidence(batch)
            ):
                return False, INV_READ_BEFORE_MUTATE, meta
        return True, None, meta

    # k8s_rollout_restart — INV_NO_RESTART_ON_BROKEN_SPEC (physical evidence; lane in meta for audit)
    if _inv_no_restart_broken_spec_blocks(proof_lane, batch, meta=meta):
        return False, INV_NO_RESTART_ON_BROKEN_SPEC, meta

    if (
        _rollout_read_before_defer_applies(proof_lane)
        and critical_evidence_present(batch)
        and not (disc or batch_has_prior_readonly_evidence(batch))
    ):
        meta["defer"] = True
        meta["proof_lane"] = proof_lane
        return False, INV_READ_BEFORE_MUTATE, meta

    return True, None, meta


def build_reasoning_chain_payload(
    *,
    verdict: str,
    lane: str,
    thought_process: list[str],
    invariant_id: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "verdict": str(verdict)[:64],
        "lane": str(lane)[:32],
        "thought_process": [str(x)[:2000] for x in thought_process][:32],
    }
    if invariant_id:
        out["invariant_id"] = str(invariant_id)[:128]
    return out
