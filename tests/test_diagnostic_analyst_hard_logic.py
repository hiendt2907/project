"""Hard logic: SDK ~0% CPU vs HighCPU alert → FALSE_ALARM."""

from __future__ import annotations

import json

from workers.diagnostic_analyst_hard_logic import apply_sdk_truth_hard_logic


def test_false_alarm_when_sdk_cpu_zero() -> None:
    by_probe = {
        "k8s_clinical_pod_status": {
            "probe": "k8s_clinical_pod_status",
            "alert_hint": "HighCPUUsage cpu 90%",
            "result": "PASSED",
            "extracted_fact": json.dumps({"kind": "PodStatus", "phase": "Running"}),
        },
        "k8s_clinical_pod_metrics": {
            "probe": "k8s_clinical_pod_metrics",
            "alert_hint": "HighCPUUsage cpu 90%",
            "result": "PASSED",
            "extracted_fact": json.dumps(
                {
                    "containers": [
                        {"name": "nginx", "cpu": "0", "memory": "4528Ki"},
                    ]
                }
            ),
            "raw": "nginx: cpu=0 memory=4528Ki",
        },
    }
    out = apply_sdk_truth_hard_logic(by_probe)
    assert out is not None
    assert "FALSE_ALARM" in out
    assert "STALE_METRIC" in out


def test_no_conclusion_when_sdk_cpu_nonzero() -> None:
    by_probe = {
        "k8s_clinical_pod_metrics": {
            "probe": "k8s_clinical_pod_metrics",
            "alert_hint": "HighCPUUsage",
            "result": "PASSED",
            "extracted_fact": json.dumps(
                {"containers": [{"name": "x", "cpu": "100m", "memory": "10Mi"}]}
            ),
        },
    }
    assert apply_sdk_truth_hard_logic(by_probe) is None
