"""Workload-scoped PromQL helpers — no exact pod= label on ephemeral pod names."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from workers.proactive_models import AnomalyEvent
from workers.diagnostic_resource import (
    deployment_workload_from_event,
    promql_workload_pod_regex_selector,
    workload_pod_prefix_for_promql,
)
from workers.promql_presets import build_dynamic_promql
from workers.promql_workload_helpers import workload_prefix_from_tool_args
from workers.sdk_service_tools import resolve_promql_for_args


def _ev(labels: dict, **kwargs: object) -> AnomalyEvent:
    base = {
        "trace_id": "t-workload-promql",
        "canonical_query": json.dumps({"labels": labels}),
    }
    base.update(kwargs)
    return AnomalyEvent.model_validate(base)


def test_workload_from_deployment_label():
    ev = _ev(
        {
            "namespace": "multi-agent",
            "pod": "chaos-victim-OLD-deadbeef",
            "workload": "chaos-victim",
        }
    )
    ns, w = workload_pod_prefix_for_promql(ev)
    assert ns == "multi-agent"
    assert w == "chaos-victim"
    assert "chaos-victim-OLD" not in promql_workload_pod_regex_selector(w)


def test_derive_prefix_from_replicaset_pod_name():
    ev = _ev({"namespace": "multi-agent", "pod": "chaos-victim-6cd57c7847-cft4v"})
    ns, w = workload_pod_prefix_for_promql(ev)
    assert ns == "multi-agent"
    assert w == "chaos-victim"


def test_statefulset_label():
    ev = _ev({"namespace": "ns1", "pod": "ignored", "statefulset": "data"})
    assert deployment_workload_from_event(ev) == ("ns1", "data")


def test_promql_selector_escaped_hyphen():
    sel = promql_workload_pod_regex_selector("my-app")
    assert 'pod=~"^my-app-.*"' in sel


@pytest.mark.parametrize(
    "pod,expected",
    [
        ("sts-0", "sts"),
        ("sts-12", "sts"),
    ],
)
def test_statefulset_ordinal_pod(pod: str, expected: str):
    ev = _ev({"namespace": "ns", "pod": pod})
    _, w = workload_pod_prefix_for_promql(ev)
    assert w == expected


def test_build_dynamic_promql_workload_prefix_regex_not_exact_pod():
    q, note, meta = build_dynamic_promql(
        "pod",
        "cpu",
        namespace="multi-agent",
        workload_prefix="chaos-victim",
        pod_name=None,
    )
    assert 'pod=~"^chaos-victim-.*"' in q
    assert 'pod="chaos-victim-6cd57c7847-cft4v"' not in q
    assert meta.get("used_profile") == "cAdvisor_workload_regex"
    assert "workload" in note.lower() or "chaos-victim" in note


def test_workload_prefix_from_tool_args_explicit_deployment():
    assert (
        workload_prefix_from_tool_args(
            {"deployment": "my-dep", "pod_name": "my-dep-old-abc-xyz12"}
        )
        == "my-dep"
    )


def test_resolve_promql_prefers_deployment_over_stale_pod():
    ctx = MagicMock()
    ctx.settings = MagicMock(k8s_default_namespace="multi-agent")
    args = {
        "namespace": "multi-agent",
        "pod_name": "chaos-victim-OLD-deadbeef",
        "deployment": "chaos-victim",
        "intent": "cpu",
    }
    q, src = resolve_promql_for_args(args, ctx)
    assert 'pod=~"^chaos-victim-.*"' in q
    assert "workload-regex" in src


def test_resolve_promql_derives_prefix_from_rs_pod_name():
    ctx = MagicMock()
    ctx.settings = MagicMock(k8s_default_namespace="multi-agent")
    args = {
        "namespace": "multi-agent",
        "pod_name": "chaos-victim-6cd57c7847-cft4v",
        "intent": "ram",
    }
    q, src = resolve_promql_for_args(args, ctx)
    assert 'pod=~"^chaos-victim-.*"' in q
    assert "container_memory_working_set_bytes" in q


# ── promql_workload_helpers uncovered branches ────────────────────────────────

def test_workload_prefix_empty_pod_name_returns_none():
    """Line 29: empty pod_name string → returns None."""
    from workers.promql_workload_helpers import workload_prefix_from_tool_args
    assert workload_prefix_from_tool_args({"pod_name": "   "}) is None


def test_workload_prefix_statefulset_pod_returns_prefix():
    """Line 35: StatefulSet ordinal pattern pod-0 → returns 'pod'."""
    from workers.promql_workload_helpers import workload_prefix_from_tool_args
    result = workload_prefix_from_tool_args({"pod_name": "mysql-0"})
    assert result == "mysql"


def test_workload_pod_label_empty_prefix_raises():
    """Line 43: empty workload_prefix → raises ValueError."""
    from workers.promql_workload_helpers import workload_pod_label_for_cadvisor
    import pytest
    with pytest.raises(ValueError, match="rỗng"):
        workload_pod_label_for_cadvisor("")
