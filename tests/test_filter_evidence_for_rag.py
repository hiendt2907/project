"""RAG embed payload: minimal fields, no HTTP junk."""

from __future__ import annotations

import json

from pkg.reasoning.sanitize import filter_evidence_for_rag


def test_filter_evidence_for_rag_strips_http_noise() -> None:
    batch = [
        {
            "probe": "k8s_clinical_pod_status",
            "alert_rule": "PodDown",
            "extracted_fact": json.dumps(
                {
                    "phase": "Pending",
                    "waiting_reasons": ["ImagePullBackOff"],
                    "container_signals": ["app:waiting=ImagePullBackOff"],
                }
            ),
            "raw": "",
        },
        {
            "probe": "k8s_clinical_pod_events",
            "alert_rule": "PodDown",
            "extracted_fact": "{}",
            "raw": "HTTP/1.1 400 Bad Request\nContent-Type: application/json\n\n"
            "Normal Warning Failed: pull access denied",
        },
    ]
    out = filter_evidence_for_rag(batch, max_tokens=512)
    assert "HTTP/1.1" not in out
    assert "400 Bad Request" not in out
    assert "Content-Type" not in out
    assert "probes=" in out
    assert "k8s_clinical_pod_events" in out
    assert "PodDown" in out
    assert "ImagePullBackOff" in out
    assert "pull access denied" in out
    assert len(out) <= 2100


def test_filter_evidence_for_rag_length_cap() -> None:
    long_ev = "E " * 5000
    batch = [
        {
            "probe": "k8s_clinical_pod_status",
            "alert_rule": "X",
            "extracted_fact": '{"phase":"Running"}',
            "raw": "",
        },
        {
            "probe": "k8s_clinical_pod_events",
            "alert_rule": "X",
            "extracted_fact": "{}",
            "raw": long_ev,
        },
    ]
    out = filter_evidence_for_rag(batch, max_tokens=512)
    assert len(out) <= 2100
