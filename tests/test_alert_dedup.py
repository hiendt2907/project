"""Tests for S1.1 — Cross-Incident Alert Deduplication."""

from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from workers.omni_worker import _alert_fingerprint


def _make_prometheus_payload(alertname: str, namespace: str, deployment: str) -> dict:
    return {
        "source": "prometheus",
        "trace_id": "test-trace-001",
        "data": {
            "alerts": [
                {
                    "labels": {
                        "alertname": alertname,
                        "namespace": namespace,
                        "deployment": deployment,
                    },
                    "annotations": {"description": "test alert"},
                }
            ]
        },
    }


def _make_pod_alert(alertname: str, namespace: str, pod: str) -> dict:
    """Pod-scoped alert with ONLY a pod label (no deployment) — the OOMKilled shape."""
    return {
        "source": "prometheus",
        "trace_id": "test-trace-pod",
        "data": {
            "alerts": [
                {
                    "labels": {
                        "alertname": alertname,
                        "namespace": namespace,
                        "pod": pod,
                    },
                    "annotations": {"description": "pod oom"},
                }
            ]
        },
    }


class TestPodScopedFingerprint:
    """Pod-scoped alerts with no deployment label must NOT collapse into one."""

    def test_distinct_pods_distinct_fingerprint(self) -> None:
        p1 = _make_pod_alert("PodMemoryWorkingSetVsLimitHigh", "multi-agent", "nginx-test")
        p2 = _make_pod_alert("PodMemoryWorkingSetVsLimitHigh", "multi-agent", "ghost-pod-404")
        assert _alert_fingerprint(p1) != _alert_fingerprint(p2)

    def test_same_pod_same_fingerprint(self) -> None:
        p1 = _make_pod_alert("PodMemoryWorkingSetVsLimitHigh", "multi-agent", "nginx-test")
        p2 = _make_pod_alert("PodMemoryWorkingSetVsLimitHigh", "multi-agent", "nginx-test")
        assert _alert_fingerprint(p1) == _alert_fingerprint(p2)


def _make_siem_payload(alertname: str, namespace: str) -> dict:
    return {
        "source": "siem",
        "trace_id": "siem-trace-001",
        "data": {
            "alerts": [
                {
                    "labels": {
                        "alertname": alertname,
                        "namespace": namespace,
                    },
                    "annotations": {},
                }
            ]
        },
    }


class TestAlertFingerprint:
    def test_prometheus_returns_fingerprint(self):
        payload = _make_prometheus_payload("HighCPU", "multi-agent", "nginx")
        fp = _alert_fingerprint(payload)
        assert fp is not None
        assert len(fp) == 20

    def test_same_alert_same_fingerprint(self):
        p1 = _make_prometheus_payload("HighCPU", "multi-agent", "nginx")
        p2 = _make_prometheus_payload("HighCPU", "multi-agent", "nginx")
        assert _alert_fingerprint(p1) == _alert_fingerprint(p2)

    def test_different_alertname_different_fingerprint(self):
        p1 = _make_prometheus_payload("HighCPU", "multi-agent", "nginx")
        p2 = _make_prometheus_payload("OOMKilled", "multi-agent", "nginx")
        assert _alert_fingerprint(p1) != _alert_fingerprint(p2)

    def test_different_namespace_different_fingerprint(self):
        p1 = _make_prometheus_payload("HighCPU", "ns-a", "nginx")
        p2 = _make_prometheus_payload("HighCPU", "ns-b", "nginx")
        assert _alert_fingerprint(p1) != _alert_fingerprint(p2)

    def test_different_deployment_different_fingerprint(self):
        p1 = _make_prometheus_payload("HighCPU", "multi-agent", "nginx")
        p2 = _make_prometheus_payload("HighCPU", "multi-agent", "api-server")
        assert _alert_fingerprint(p1) != _alert_fingerprint(p2)

    def test_siem_source_returns_fingerprint(self):
        payload = _make_siem_payload("DDoSDetected", "finguard-customer")
        fp = _alert_fingerprint(payload)
        assert fp is not None

    def test_telegram_source_returns_none(self):
        payload = {"source": "telegram", "trace_id": "tg-001", "text": "check pods"}
        assert _alert_fingerprint(payload) is None

    def test_telegram_callback_returns_none(self):
        payload = {"source": "telegram_callback", "trace_id": "cb-001"}
        assert _alert_fingerprint(payload) is None

    def test_unknown_source_returns_none(self):
        payload = {"source": "unknown", "trace_id": "x-001"}
        assert _alert_fingerprint(payload) is None

    def test_empty_payload_returns_none(self):
        assert _alert_fingerprint({}) is None

    def test_no_alerts_returns_none(self):
        payload = {"source": "prometheus", "data": {"alerts": []}}
        assert _alert_fingerprint(payload) is None

    def test_malformed_alerts_returns_none(self):
        payload = {"source": "prometheus", "data": {"alerts": ["not-a-dict"]}}
        assert _alert_fingerprint(payload) is None

    def test_fingerprint_is_stable_string(self):
        payload = _make_prometheus_payload("KafkaLag", "multi-agent", "omni-worker")
        fp = _alert_fingerprint(payload)
        assert isinstance(fp, str)
        assert fp == _alert_fingerprint(payload)  # deterministic

    def test_statefulset_label_used_as_deployment(self):
        payload = {
            "source": "prometheus",
            "data": {
                "alerts": [
                    {
                        "labels": {
                            "alertname": "HighCPU",
                            "namespace": "multi-agent",
                            "statefulset": "kafka",
                        }
                    }
                ]
            },
        }
        fp = _alert_fingerprint(payload)
        assert fp is not None

    def test_100_identical_alerts_same_fingerprint(self):
        """100 identical alerts must produce identical fingerprint → 1 pipeline call."""
        fps = set()
        for _ in range(100):
            payload = _make_prometheus_payload("HighCPU", "multi-agent", "nginx")
            fps.add(_alert_fingerprint(payload))
        assert len(fps) == 1

    def test_100_different_namespace_alerts_unique_fingerprints(self):
        """100 alerts on different namespaces must produce unique fingerprints."""
        fps = set()
        for i in range(100):
            payload = _make_prometheus_payload("HighCPU", f"ns-{i}", "nginx")
            fps.add(_alert_fingerprint(payload))
        assert len(fps) == 100
