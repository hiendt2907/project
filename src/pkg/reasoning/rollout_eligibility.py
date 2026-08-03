"""Rollout-eligibility parsing from evidence batches.

Moved from workers/evidence_mutate_emit.py (WS1, dependency-direction fix):
these 2 functions + their helpers are pure evidence-batch parsing with no
dependency on the rest of that file (audit_ledger/alert_to_event/etc), so they
can live in pkg/ where pkg/reasoning/deterministic_mutate_from_evidence.py
needs them without importing workers/.  workers/evidence_mutate_emit.py
re-exports these unchanged so existing worker callers are unaffected.
"""

from __future__ import annotations

import json
import re
from typing import Any

_RE_FAULT_INCIDENT = re.compile(
    r"(createcontainer|crashloop|imagepull|probefail|readiness|liveness|backoff|oom|oomkilled|failedmount|unschedul"
    # Workload-availability faults: a Deployment/StatefulSet with 0 ready replicas (pods stuck
    # NotReady) where the spec is valid is the textbook case for `kubectl rollout restart`.
    # Canonical Prometheus/kube-state alertnames: KubePodNotReady, KubeDeploymentReplicasMismatch.
    # Downstream proof-of-fault + INV_NO_RESTART_ON_BROKEN_SPEC still gate the actual mutate.
    r"|notready|not[\s_-]?ready|podnotready|replicas?[\s_-]?mismatch|noavailablereplicas|workload[\s_-]?unavailable)",
    re.IGNORECASE,
)


def _deployment_name_from_alert_labels(labels: dict[str, Any]) -> str:
    """Prometheus rules may use `deployment` or `workload` (chaos KubePodCrashLoopVictim uses workload)."""
    for k in ("deployment", "deployment_name", "workload"):
        v = str(labels.get(k) or "").strip()
        if v:
            return v
    return ""


def rollout_args_from_evidence_batch(batch: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Best-effort: namespace + deployment from canonical_query_snippet JSON (alert labels)."""
    for b in batch:
        snip = str(b.get("canonical_query_snippet") or "").strip()
        if not snip.startswith("{"):
            continue
        try:
            j = json.loads(snip)
            labels = j.get("labels") if isinstance(j, dict) else None
            if not isinstance(labels, dict):
                continue
            ns = str(labels.get("namespace") or "").strip()
            dep = _deployment_name_from_alert_labels(labels)
            if ns and dep:
                return {"namespace": ns, "deployment": dep}
        except Exception:
            continue
    return None


def workload_fault_incident_rollout_eligible(batch: list[dict[str, Any]]) -> bool:
    """True when evidence points to a concrete workload fault where restart is a safe first mutate."""
    for b in batch:
        hint = str(b.get("alert_hint") or "")
        if _RE_FAULT_INCIDENT.search(hint):
            return True
        snip = str(b.get("canonical_query_snippet") or "").strip()
        if not snip.startswith("{"):
            continue
        try:
            j = json.loads(snip)
        except Exception:
            continue
        if not isinstance(j, dict):
            continue
        labels = j.get("labels")
        if not isinstance(labels, dict):
            continue
        alertname = str(labels.get("alertname") or "")
        reason = str(labels.get("reason") or "")
        if _RE_FAULT_INCIDENT.search(alertname) or _RE_FAULT_INCIDENT.search(reason):
            return True
    return False
