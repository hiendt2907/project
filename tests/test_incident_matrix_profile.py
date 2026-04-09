"""incident_matrix_profile: alertname + api_web lookup."""

from __future__ import annotations

import json

from pkg.reasoning import incident_matrix_profile as imp


def test_alertname_from_batch() -> None:
    batch = [
        {
            "canonical_query_snippet": json.dumps(
                {"labels": {"alertname": "PodCpuUtilizationVsLimitHigh", "namespace": "multi-agent"}}
            ),
        }
    ]
    assert imp.alertname_from_batch(batch) == "PodCpuUtilizationVsLimitHigh"


def test_workload_profile_for_alert_silent_5xx() -> None:
    imp.invalidate_matrix_cache()
    assert imp.workload_profile_for_alert("PodCpuUtilizationVsLimitHigh") == "api_web"


def test_is_api_web_from_matrix() -> None:
    imp.invalidate_matrix_cache()
    batch = [
        {
            "canonical_query_snippet": json.dumps(
                {"labels": {"alertname": "PodCpuUtilizationVsLimitHigh", "namespace": "ns"}}
            ),
        }
    ]
    assert imp.is_api_web_workload(batch, rag_match_text=None) is True


def test_rag_implies_api_web() -> None:
    assert imp.rag_match_text_implies_api_web("nginx ingress returns 503 for REST API") is True
    assert imp.rag_match_text_implies_api_web("short") is False


def test_is_api_web_from_rag_only() -> None:
    imp.invalidate_matrix_cache()
    batch = [{"canonical_query_snippet": "{}"}]
    assert imp.is_api_web_workload(batch, rag_match_text="Envoy gateway 503 errors") is True


def test_resolve_proof_lane_annotation_overrides_matrix() -> None:
    imp.invalidate_matrix_cache()
    batch = [
        {
            "canonical_query_snippet": json.dumps(
                {
                    "labels": {
                        "alertname": "PodCpuUtilizationVsLimitHigh",
                        "omni_proof_lane": "state",
                    }
                }
            ),
        }
    ]
    lane, src = imp.resolve_proof_lane(batch, rag_match_text=None)
    assert lane == "state" and src == "annotation"


def test_pick_matrix_row_disambiguates_api_web() -> None:
    imp.invalidate_matrix_cache()
    batch = [
        {
            "canonical_query_snippet": json.dumps(
                {"labels": {"alertname": "PodCpuUtilizationVsLimitHigh", "namespace": "multi-agent"}}
            ),
        }
    ]
    row = imp.pick_matrix_row_for_batch(batch, rag_match_text="nginx 503 REST API")
    assert row is not None
    assert row.get("id") == "silent_5xx_bypass_sigma"


def test_pick_matrix_row_imagepull_reason() -> None:
    imp.invalidate_matrix_cache()
    batch = [
        {
            "canonical_query_snippet": json.dumps(
                {
                    "labels": {
                        "alertname": "NginxTestContainerWaitingFaultLab",
                        "namespace": "multi-agent",
                        "reason": "ImagePullBackOff",
                    }
                }
            ),
        }
    ]
    row = imp.pick_matrix_row_for_batch(batch, rag_match_text=None)
    assert row is not None
    assert row.get("id") == "image_pull_backoff_expired_secret"
