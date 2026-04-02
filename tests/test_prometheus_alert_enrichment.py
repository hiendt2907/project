"""Prometheus alert trigger inference + Ollama anchor (English)."""

from __future__ import annotations

from workers.prometheus_alert_enrichment import (
    build_ollama_anchor_en,
    infer_alert_trigger_dimension,
)


def test_infer_trigger_probe_from_readiness() -> None:
    t = infer_alert_trigger_dimension(
        {},
        {"summary": "readiness probe failed"},
        "NginxTestProbe",
        "",
    )
    assert t == "probe"


def test_infer_trigger_cpu_from_summary() -> None:
    t = infer_alert_trigger_dimension({}, {}, "HighCPU", "CPU usage over 90%")
    assert t == "cpu"


def test_infer_unknown_when_no_signal() -> None:
    t = infer_alert_trigger_dimension({}, {}, "WeirdAlert", "ok")
    assert t == "unknown"


def test_anchor_unspecified_ids_mandates_discovery_not_escalate_hint() -> None:
    a = build_ollama_anchor_en(namespace="", pod="", deployment="", trigger="unknown")
    assert "identifiers=unspecified" in a
    assert "list_all_pods_sdk" in a
    assert "observation tool" in a.lower()
