"""Unit chaos tests — evidence_mutate_emit helper functions.

Covers rollout eligibility helpers, SIEM label extraction, and hitl detection.
These are pure-function tests (no I/O) that exercise uncovered utility code.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import fakeredis.aioredis
import pytest

from workers.evidence_mutate_emit import (
    rollout_args_from_evidence_batch,
    should_try_rollout_from_rag,
    should_emit_rollout_after_rag,
    workload_cpu_incident_rollout_eligible,
    workload_fault_incident_rollout_eligible,
    _siem_hitl_required,
    _siem_alert_labels,
    _symptom_group_from_batch,
    _deployment_name_from_alert_labels,
)


# ── rollout_args_from_evidence_batch ──────────────────────────────────────────


def test_rollout_args_extracted_from_canonical_snippet() -> None:
    """rollout_args_from_evidence_batch extracts namespace+deployment from alert labels."""
    batch = [
        {
            "canonical_query_snippet": json.dumps({
                "labels": {
                    "namespace": "multi-agent",
                    "deployment": "nginx-lab",
                }
            })
        }
    ]
    result = rollout_args_from_evidence_batch(batch)
    assert result == {"namespace": "multi-agent", "deployment": "nginx-lab"}


def test_rollout_args_none_on_empty_batch() -> None:
    """rollout_args_from_evidence_batch returns None for empty batch."""
    assert rollout_args_from_evidence_batch([]) is None


def test_rollout_args_none_when_no_valid_snippet() -> None:
    """rollout_args_from_evidence_batch returns None when snippet has no namespace/deployment."""
    batch = [{"canonical_query_snippet": "not-json"}, {"alert_hint": "HighCPU"}]
    assert rollout_args_from_evidence_batch(batch) is None


def test_rollout_args_uses_workload_label_fallback() -> None:
    """rollout_args_from_evidence_batch uses 'workload' if 'deployment' is absent."""
    batch = [
        {
            "canonical_query_snippet": json.dumps({
                "labels": {
                    "namespace": "multi-agent",
                    "workload": "omni-analyst",
                }
            })
        }
    ]
    result = rollout_args_from_evidence_batch(batch)
    assert result == {"namespace": "multi-agent", "deployment": "omni-analyst"}


# ── should_try_rollout_from_rag ───────────────────────────────────────────────


def test_should_try_rollout_from_rag_tool_name() -> None:
    """should_try_rollout_from_rag returns True when suggested_tool contains 'rollout'."""
    assert should_try_rollout_from_rag("k8s_rollout_restart", "") is True


def test_should_try_rollout_from_rag_diag_snippet() -> None:
    """should_try_rollout_from_rag returns True when diag_snippet mentions 'restart'."""
    assert should_try_rollout_from_rag("kubectl_get_events", "you should restart the pod") is True


def test_should_try_rollout_from_rag_no_match() -> None:
    """should_try_rollout_from_rag returns False for unrelated tool/snippet."""
    assert should_try_rollout_from_rag("kubectl_get_logs", "check events for errors") is False


# ── workload_cpu_incident_rollout_eligible ────────────────────────────────────


def test_workload_cpu_eligible_from_alert_hint() -> None:
    """workload_cpu_incident_rollout_eligible returns True when alert_hint mentions CPU."""
    batch = [{"alert_hint": "HighCPU on nginx-lab millicore limit"}]
    assert workload_cpu_incident_rollout_eligible(batch) is True


def test_workload_cpu_eligible_from_labels() -> None:
    """workload_cpu_incident_rollout_eligible returns True when alertname contains 'cpu'."""
    batch = [
        {
            "canonical_query_snippet": json.dumps({
                "labels": {"alertname": "KubeContainerHighCPUUsage", "namespace": "multi-agent"}
            })
        }
    ]
    assert workload_cpu_incident_rollout_eligible(batch) is True


def test_workload_cpu_not_eligible_unrelated() -> None:
    """workload_cpu_incident_rollout_eligible returns False for non-CPU alerts."""
    batch = [{"alert_hint": "Redis OOM condition detected"}]
    assert workload_cpu_incident_rollout_eligible(batch) is False


# ── workload_fault_incident_rollout_eligible ──────────────────────────────────


def test_workload_fault_eligible_crashloop() -> None:
    """workload_fault_incident_rollout_eligible returns True for CrashLoopBackOff hint."""
    batch = [{"alert_hint": "CrashLoopBackOff: nginx-lab restart count=5"}]
    assert workload_fault_incident_rollout_eligible(batch) is True


def test_workload_fault_eligible_imagepull() -> None:
    """workload_fault_incident_rollout_eligible returns True for ImagePullBackOff."""
    batch = [{"alert_hint": "ImagePullBackOff: cannot pull docker.io/nginx:bad-tag"}]
    assert workload_fault_incident_rollout_eligible(batch) is True


def test_workload_fault_eligible_from_labels_alertname() -> None:
    """workload_fault_incident_rollout_eligible returns True from alertname in labels."""
    batch = [
        {
            "canonical_query_snippet": json.dumps({
                "labels": {"alertname": "KubePodCrashLoopVictim", "namespace": "multi-agent"}
            })
        }
    ]
    assert workload_fault_incident_rollout_eligible(batch) is True


def test_workload_fault_not_eligible_unrelated() -> None:
    """workload_fault_incident_rollout_eligible returns False for non-fault alerts."""
    batch = [{"alert_hint": "Kafka consumer lag elevated"}]
    assert workload_fault_incident_rollout_eligible(batch) is False


# ── should_emit_rollout_after_rag ─────────────────────────────────────────────


def test_should_emit_rollout_via_rag_suggestion() -> None:
    """should_emit_rollout_after_rag returns True when tool is rollout."""
    result = should_emit_rollout_after_rag(
        suggested_tool="k8s_rollout_restart",
        diag_snippet="",
        batch=[],
        rr={},
        autonomous_rollout_on_cpu_incident=False,
        autonomous_rollout_on_fault_incident=False,
    )
    assert result is True


def test_should_emit_rollout_cpu_incident_eligible() -> None:
    """should_emit_rollout_after_rag returns True for CPU incident when flag is enabled."""
    batch = [{"alert_hint": "HighCPU millicore limit"}]
    result = should_emit_rollout_after_rag(
        suggested_tool="kubectl_get_logs",
        diag_snippet="check the metrics",
        batch=batch,
        rr={"tool": "kubectl_get_logs"},
        autonomous_rollout_on_cpu_incident=True,
    )
    assert result is True


def test_should_emit_rollout_false_when_no_rr_and_no_rollout_hint() -> None:
    """should_emit_rollout_after_rag returns False when rr=None and tool is read-only."""
    result = should_emit_rollout_after_rag(
        suggested_tool="kubectl_get_events",
        diag_snippet="check events",
        batch=[],
        rr=None,
        autonomous_rollout_on_cpu_incident=True,
    )
    assert result is False


# ── _siem_hitl_required ───────────────────────────────────────────────────────


def test_siem_hitl_required_true() -> None:
    """_siem_hitl_required returns True when siem_hitl_required=true in labels."""
    batch = [
        {
            "canonical_query_snippet": json.dumps({
                "labels": {
                    "siem_hitl_required": "true",
                    "siem_source": "finguard",
                }
            })
        }
    ]
    assert _siem_hitl_required(batch) is True


def test_siem_hitl_required_false_no_label() -> None:
    """_siem_hitl_required returns False when label is absent."""
    batch = [{"canonical_query_snippet": json.dumps({"labels": {"siem_source": "finguard"}})}]
    assert _siem_hitl_required(batch) is False


def test_siem_hitl_required_false_empty_batch() -> None:
    """_siem_hitl_required returns False on empty batch."""
    assert _siem_hitl_required([]) is False


# ── _siem_alert_labels ────────────────────────────────────────────────────────


def test_siem_alert_labels_extracted() -> None:
    """_siem_alert_labels returns dict of SIEM labels for finguard-sourced batch item."""
    batch = [
        {
            "canonical_query_snippet": json.dumps({
                "labels": {
                    "siem_source": "finguard",
                    "siem_incident_id": "inc-999",
                    "siem_tenant": "default",
                }
            })
        }
    ]
    labels = _siem_alert_labels(batch)
    assert labels["siem_incident_id"] == "inc-999"
    assert labels["siem_tenant"] == "default"


def test_siem_alert_labels_empty_for_non_finguard() -> None:
    """_siem_alert_labels returns empty dict when siem_source is not finguard."""
    batch = [
        {
            "canonical_query_snippet": json.dumps({
                "labels": {"siem_source": "other", "siem_incident_id": "inc-888"}
            })
        }
    ]
    assert _siem_alert_labels(batch) == {}


# ── _symptom_group_from_batch / _deployment_name_from_alert_labels ───────────


def test_symptom_group_extracted_from_batch() -> None:
    """_symptom_group_from_batch returns the first non-empty symptom_group."""
    batch = [
        {"symptom_group": ""},
        {"symptom_group": "RESOURCE_EXHAUSTION"},
    ]
    from workers.evidence_mutate_emit import _symptom_group_from_batch
    assert _symptom_group_from_batch(batch) == "RESOURCE_EXHAUSTION"


def test_symptom_group_empty_batch() -> None:
    """_symptom_group_from_batch returns empty string for batch with no symptom_group."""
    from workers.evidence_mutate_emit import _symptom_group_from_batch
    assert _symptom_group_from_batch([{"alert_hint": "HighCPU"}]) == ""


def test_deployment_name_from_alert_labels_deployment_key() -> None:
    """_deployment_name_from_alert_labels prefers 'deployment' key."""
    labels = {"deployment": "nginx-lab", "workload": "something-else"}
    assert _deployment_name_from_alert_labels(labels) == "nginx-lab"


def test_deployment_name_from_alert_labels_workload_fallback() -> None:
    """_deployment_name_from_alert_labels falls back to 'workload' when 'deployment' absent."""
    labels = {"workload": "omni-analyst"}
    assert _deployment_name_from_alert_labels(labels) == "omni-analyst"


def test_deployment_name_from_alert_labels_empty() -> None:
    """_deployment_name_from_alert_labels returns '' when no recognized key."""
    assert _deployment_name_from_alert_labels({}) == ""
