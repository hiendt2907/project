"""Regression: proactive SLO hooks, PromQL grounding, placeholder guard."""

from __future__ import annotations

from observability.normalize import (
    canonical_query_from_rule_name,
    infer_error_hint_from_promql,
)
from workers.sdk_service_tools import is_placeholder_promql


def test_infer_error_hint_crashloop_promql() -> None:
    pq = 'sum(kube_pod_container_status_waiting_reason{reason="CrashLoopBackOff"})'
    h = infer_error_hint_from_promql(pq)
    assert "crash" in h or "backoff" in h


def test_canonical_proactive_threshold_with_promql_context() -> None:
    pq = 'sum(kube_pod_container_status_waiting_reason{reason="CrashLoopBackOff"})'
    hint = infer_error_hint_from_promql(pq)
    q = canonical_query_from_rule_name(
        "PrometheusProactiveThreshold",
        target="cluster",
        error_hint=hint,
        promql_context=pq,
    )
    assert "POD" in q
    assert "CRASH_LOOP" in q


def test_is_placeholder_promql_rejects_llm_garbage() -> None:
    assert is_placeholder_promql("metric_value > threshold") is True
    assert is_placeholder_promql("  metric_value  >  threshold  ") is True
    assert is_placeholder_promql("sum(up)") is False
    assert is_placeholder_promql("") is True
