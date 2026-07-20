"""Alert input schema / classification (SRE-autonomous plan step 6, part A).

Not every alert has a remediable cluster target. Self-monitoring / KPI alerts
(``OmniAdvisoryAcceptanceRateLow``, ``OmniWorkerStalled`` …) describe Omni's own
health — there is nothing in the customer cluster to mutate, so they must never
reach the mutate-planner. They route to a self-monitoring runbook instead.

Workload alerts are mutate-eligible only when they carry the minimum identity the
proof-of-fault gate needs to anchor a mutation: a namespace plus a pod or workload.
Missing identity → mutate-ineligible (diagnosis/visibility only), surfaced via
``missing_fields`` rather than silently inferred into a mutation.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

ALERT_KIND_META_SELF = "meta_self"
ALERT_KIND_WORKLOAD = "workload"
ALERT_KIND_INFRA = "infra"
ALERT_KIND_UNKNOWN = "unknown"

# Self-monitoring / KPI alert families emitted by Omni about itself. These have no
# customer-cluster remediation target.
# OmniBaseline* (recording rules omni:mem:z / omni:cpu:z) thiếu ở đây từng để
# OmniBaselineMemZHigh rơi vào RAG+LLM và bị parrot thành advisory bịa
# (trace gw-prom-84cd18edddb2, 2026-07-15).
_META_SELF_RE = re.compile(
    r"^(Omni(Worker|Redis|LLM|Advisory|FalsePositive|Health|Kafka|Pipeline|Baseline)"
    r"|OmniAdvisoryAcceptanceRateLow|OmniFalsePositiveRateHigh)",
    re.I,
)

# Infra-layer alert hints (platform components, not a tenant workload).
_INFRA_RE = re.compile(
    r"(kafka|zookeeper|etcd|kubelet|node|prometheus|alertmanager|coredns|ingress)",
    re.I,
)


@dataclass(frozen=True)
class AlertClass:
    """Classification of an inbound alert for routing + mutate eligibility."""

    kind: str
    mutate_eligible: bool
    alertname: str = ""
    namespace: str = ""
    pod: str = ""
    workload: str = ""
    severity: str = ""
    source: str = ""
    missing_fields: list[str] = field(default_factory=list)


def _first_alert_labels(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract labels of the first alert; tolerate JSON-string ``data`` bodies."""
    try:
        body = payload.get("data") or {}
        if isinstance(body, str):
            body = json.loads(body)
        alerts = body.get("alerts") or []
        if alerts and isinstance(alerts[0], dict):
            labels = alerts[0].get("labels") or {}
            if isinstance(labels, dict):
                return labels
    except Exception:
        pass
    return {}


def classify_alert(payload: dict[str, Any]) -> AlertClass:
    """Classify an inbound alert payload and decide mutate eligibility.

    Pure / side-effect free so it can gate at ingestion and be unit-tested without
    Redis or Kafka.
    """
    labels = _first_alert_labels(payload)
    alertname = str(labels.get("alertname") or "").strip()
    namespace = str(labels.get("namespace") or "").strip()
    pod = str(labels.get("pod") or "").strip()
    workload = str(
        labels.get("deployment")
        or labels.get("workload")
        or labels.get("statefulset")
        or labels.get("daemonset")
        or ""
    ).strip()
    severity = str(labels.get("severity") or "").strip()
    source = str(payload.get("source") or labels.get("source") or "").strip()

    # Meta / self-KPI: no remediable cluster target → never mutate-eligible.
    if _META_SELF_RE.match(alertname):
        return AlertClass(
            kind=ALERT_KIND_META_SELF,
            mutate_eligible=False,
            alertname=alertname,
            namespace=namespace,
            pod=pod,
            workload=workload,
            severity=severity,
            source=source,
            missing_fields=["self_monitoring_alert_no_cluster_target"],
        )

    if not alertname:
        return AlertClass(
            kind=ALERT_KIND_UNKNOWN,
            mutate_eligible=False,
            severity=severity,
            source=source,
            missing_fields=["alertname"],
        )

    kind = ALERT_KIND_INFRA if _INFRA_RE.search(alertname) else ALERT_KIND_WORKLOAD

    missing: list[str] = []
    if not namespace:
        missing.append("namespace")
    if not pod and not workload:
        missing.append("pod_or_workload")

    return AlertClass(
        kind=kind,
        mutate_eligible=not missing,
        alertname=alertname,
        namespace=namespace,
        pod=pod,
        workload=workload,
        severity=severity,
        source=source,
        missing_fields=missing,
    )
