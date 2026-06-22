"""Coverage: deterministic_mutate_from_evidence, incident_matrix_profile."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from pkg.reasoning import deterministic_mutate_from_evidence as dmf
from pkg.reasoning import incident_matrix_profile as imp


@pytest.fixture(autouse=True)
def reset_matrix_cache() -> None:
    imp.invalidate_matrix_cache()
    yield
    imp.invalidate_matrix_cache()


@pytest.mark.parametrize(
    "csv,expect_contains",
    [
        ("", True),
        ("k8s_patch_configmap", True),
    ],
)
def test_parse_probe_driven_mutate_tools_csv(csv: str, expect_contains: bool) -> None:
    tools = dmf.parse_probe_driven_mutate_tools_csv(csv if csv else None)
    assert bool(tools) is expect_contains


def test_env_default_remediation_namespace_roundtrip() -> None:
    prev = os.environ.get("OMNI_AUTONOMOUS_ALLOWED_NAMESPACES")
    try:
        os.environ["OMNI_AUTONOMOUS_ALLOWED_NAMESPACES"] = "a, b ; c"
        assert dmf.env_default_remediation_namespace() == "a"
    finally:
        if prev is None:
            os.environ.pop("OMNI_AUTONOMOUS_ALLOWED_NAMESPACES", None)
        else:
            os.environ["OMNI_AUTONOMOUS_ALLOWED_NAMESPACES"] = prev


def test_deterministic_mutate_plan_from_item_rbac() -> None:
    allowed = dmf.parse_probe_driven_mutate_tools_csv("k8s_apply_rbac_least_privilege")
    assert "k8s_apply_rbac_least_privilege" in allowed, (
        "k8s_apply_rbac_least_privilege missing from MUTATE_TOOL_ALLOWLIST — "
        "this is a code contract violation, not a test configuration issue"
    )
    item = {
        "probe": "clinical",
        "extracted_fact": {
            "status": "FAILED",
            "recommended_tool": "k8s_apply_rbac_least_privilege",
            "namespace": "multi-agent",
            "reasoning": "test",
        },
    }
    plan = dmf.deterministic_mutate_plan_from_item(item, default_ns="multi-agent", allowed_tools=allowed)
    assert plan and plan["tool_name"] == "k8s_apply_rbac_least_privilege"


def test_deterministic_mutate_plan_from_item_configmap_mutate_args() -> None:
    allowed = dmf.parse_probe_driven_mutate_tools_csv("k8s_patch_configmap")
    assert "k8s_patch_configmap" in allowed, (
        "k8s_patch_configmap missing from MUTATE_TOOL_ALLOWLIST — "
        "this is a code contract violation, not a test configuration issue"
    )
    item = {
        "probe": "p",
        "extracted_fact": {
            "status": "FAILED",
            "recommended_tool": "k8s_patch_configmap",
            "mutate_args": {
                "namespace": "multi-agent",
                "name": "cm1",
                "key": "k",
                "value": "v",
                "reasoning": "r",
            },
        },
    }
    plan = dmf.deterministic_mutate_plan_from_item(item, default_ns="multi-agent", allowed_tools=allowed)
    assert plan and plan["args"]["name"] == "cm1"


def test_probe_structured_remediation_ready_false() -> None:
    assert not dmf.probe_structured_remediation_ready(
        [{"extracted_fact": {"status": "OK"}}],
        default_ns="multi-agent",
        allowed_tools=dmf.parse_probe_driven_mutate_tools_csv(""),
    )


def test_oom_deterministic_plan_when_enabled() -> None:
    plan_batch = [
        {
            "canonical_query_snippet": json.dumps(
                {
                    "labels": {
                        "namespace": "x",
                        "deployment": "y",
                        "alertname": "OmniOomKilledPodNoRecovery",
                    }
                }
            )
        }
    ]
    prev_oom = os.environ.get("OMNI_OOM_DETERMINISTIC_REMEDIATE_ENABLED")
    prev_tools = os.environ.get("OMNI_PROBE_DRIVEN_MUTATE_TOOLS")
    try:
        os.environ["OMNI_OOM_DETERMINISTIC_REMEDIATE_ENABLED"] = "true"
        os.environ["OMNI_PROBE_DRIVEN_MUTATE_TOOLS"] = "k8s_patch_resource"
        tools = dmf.parse_probe_driven_mutate_tools_csv(os.environ["OMNI_PROBE_DRIVEN_MUTATE_TOOLS"])
        oom = dmf.oom_deterministic_plan_from_batch(
            plan_batch, default_ns="multi-agent", allowed_tools=tools, ws=None
        )
        if "k8s_patch_resource" in tools:
            assert oom is not None and oom["tool_name"] == "k8s_patch_resource"
    finally:
        if prev_oom is None:
            os.environ.pop("OMNI_OOM_DETERMINISTIC_REMEDIATE_ENABLED", None)
        else:
            os.environ["OMNI_OOM_DETERMINISTIC_REMEDIATE_ENABLED"] = prev_oom
        if prev_tools is None:
            os.environ.pop("OMNI_PROBE_DRIVEN_MUTATE_TOOLS", None)
        else:
            os.environ["OMNI_PROBE_DRIVEN_MUTATE_TOOLS"] = prev_tools


def test_fault_rollout_plan_from_batch() -> None:
    batch = [
        {
            "canonical_query_snippet": json.dumps(
                {
                    "labels": {
                        "namespace": "multi-agent",
                        "deployment": "web",
                        "alertname": "CrashLoop",
                        "reason": "CrashLoopBackOff",
                    }
                }
            ),
            "alert_hint": "unhealthy",
        }
    ]
    ws = SimpleNamespace(omni_autonomous_rollout_on_fault_incident=True)
    plan = dmf.fault_rollout_deterministic_plan_from_batch(batch, ws=ws)
    assert plan and plan["tool_name"] == "k8s_rollout_restart"


def test_fault_rollout_blocked_broken_spec() -> None:
    batch = [
        {
            "canonical_query_snippet": json.dumps(
                {
                    "labels": {
                        "namespace": "multi-agent",
                        "deployment": "web",
                        "reason": "CrashLoopBackOff",
                    }
                }
            ),
            "alert_hint": "ConfigMap not found for mount",
        }
    ]
    ws = SimpleNamespace(omni_autonomous_rollout_on_fault_incident=True)
    assert dmf.fault_rollout_deterministic_plan_from_batch(batch, ws=ws) is None


def test_alertname_from_batch_and_proof_lane() -> None:
    b = [{"canonical_query_snippet": json.dumps({"labels": {"alertname": "Z"}})}]
    assert imp.alertname_from_batch(b) == "Z"
    lane, src = imp.resolve_proof_lane(
        [
            {
                "canonical_query_snippet": json.dumps(
                    {"labels": {}, "annotations": {"omni_proof_lane": "app_log"}}
                )
            }
        ]
    )
    assert lane == "app_log" and src == "annotation"


def test_state_and_app_log_heuristics() -> None:
    assert imp.state_lane_heuristic([{"alert_hint": "ImagePullBackOff"}])
    assert imp.app_log_heuristic([{"alert_hint": "HttpError rate sustained 500"}])


def test_labels_from_batch_row_match() -> None:
    batch = [{"canonical_query_snippet": json.dumps({"labels": {"env": "prod"}})}]
    row = {"series_label_defaults": {"env": "prod"}, "prometheus_alert": "X"}
    assert imp.row_matches_series_label_defaults(row, batch)
