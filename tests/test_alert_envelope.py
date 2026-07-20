"""TDD for alert input schema / classification (plan step 6, part A).

Meta / self-KPI alerts (OmniAdvisoryAcceptanceRateLow, OmniWorkerStalled, …) have
no remediable cluster target. They must be classified mutate-ineligible so they
never reach the mutate-planner — they route to a self-monitoring runbook instead.
Workload alerts missing namespace+target are also mutate-ineligible (proof-of-fault
cannot anchor a mutation).
"""

from __future__ import annotations

from workers.alert_envelope import (
    ALERT_KIND_INFRA,
    ALERT_KIND_META_SELF,
    ALERT_KIND_WORKLOAD,
    classify_alert,
)


def _payload(alertname: str, labels: dict | None = None) -> dict:
    lbl = {"alertname": alertname}
    lbl.update(labels or {})
    return {"data": {"alerts": [{"labels": lbl}]}}


# --------------------------------------------------------------------------- #
# meta / self-KPI alerts                                                       #
# --------------------------------------------------------------------------- #


def test_advisory_acceptance_alert_is_meta_self_not_mutate_eligible():
    c = classify_alert(_payload("OmniAdvisoryAcceptanceRateLow"))
    assert c.kind == ALERT_KIND_META_SELF
    assert c.mutate_eligible is False


def test_worker_stalled_alert_is_meta_self():
    for name in ("OmniWorkerStalled", "OmniRedisConnectionLost", "OmniLLMSustainedDown"):
        c = classify_alert(_payload(name))
        assert c.kind == ALERT_KIND_META_SELF, name
        assert c.mutate_eligible is False, name


def test_meta_self_with_null_ns_pod_still_meta_self():
    c = classify_alert(_payload("OmniFalsePositiveRateHigh", {"namespace": "", "pod": ""}))
    assert c.kind == ALERT_KIND_META_SELF
    assert c.mutate_eligible is False


def test_baseline_z_alerts_are_meta_self():
    # Regression trace gw-prom-84cd18edddb2 (2026-07-15): OmniBaselineMemZHigh không khớp
    # _META_SELF_RE nên rơi vào RAG+LLM và LLM parrot ví dụ prompt thành advisory bịa.
    for name in ("OmniBaselineMemZHigh", "OmniBaselineCpuZHigh", "OmniBaselineDiskZHigh"):
        c = classify_alert(_payload(name))
        assert c.kind == ALERT_KIND_META_SELF, name
        assert c.mutate_eligible is False, name


# --------------------------------------------------------------------------- #
# workload alerts                                                              #
# --------------------------------------------------------------------------- #


def test_workload_alert_with_full_identity_is_mutate_eligible():
    c = classify_alert(
        _payload(
            "KubePodCrashLooping",
            {"namespace": "multi-agent", "pod": "nginx-abc", "deployment": "nginx"},
        )
    )
    assert c.kind == ALERT_KIND_WORKLOAD
    assert c.mutate_eligible is True
    assert c.namespace == "multi-agent"
    assert c.pod == "nginx-abc"
    assert c.missing_fields == []


def test_workload_alert_missing_namespace_not_mutate_eligible():
    c = classify_alert(_payload("KubePodCrashLooping", {"pod": "nginx-abc"}))
    assert c.kind == ALERT_KIND_WORKLOAD
    assert c.mutate_eligible is False
    assert "namespace" in c.missing_fields


def test_workload_alert_missing_target_not_mutate_eligible():
    c = classify_alert(_payload("KubePodCrashLooping", {"namespace": "multi-agent"}))
    assert c.mutate_eligible is False
    # neither pod nor workload present
    assert "pod_or_workload" in c.missing_fields


def test_workload_alert_with_workload_only_is_eligible():
    c = classify_alert(
        _payload("KubeDeploymentReplicasMismatch", {"namespace": "ma", "deployment": "api"})
    )
    assert c.mutate_eligible is True
    assert c.workload == "api"


# --------------------------------------------------------------------------- #
# robustness                                                                   #
# --------------------------------------------------------------------------- #


def test_empty_payload_is_unknown_not_eligible():
    c = classify_alert({})
    assert c.mutate_eligible is False


def test_json_string_data_is_parsed():
    import json

    raw = {"data": json.dumps({"alerts": [{"labels": {"alertname": "OmniWorkerStalled"}}]})}
    c = classify_alert(raw)
    assert c.kind == ALERT_KIND_META_SELF


def test_severity_and_source_extracted():
    c = classify_alert(
        _payload("KubePodCrashLooping", {"namespace": "ma", "pod": "p", "severity": "critical"})
    )
    assert c.severity == "critical"


def test_infra_alert_classified():
    c = classify_alert(_payload("KafkaConsumerLagHigh", {"namespace": "kafka"}))
    assert c.kind in (ALERT_KIND_INFRA, ALERT_KIND_WORKLOAD)
