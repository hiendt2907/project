"""Fault rollout deterministic plan (no LLM) when labels + fault signal present."""

from __future__ import annotations

import json
from types import SimpleNamespace

from pkg.reasoning.deterministic_mutate_from_evidence import (
    chaos_credential_lab_autofix_plan_from_batch,
    deterministic_mutate_plan_from_batch,
    fault_rollout_deterministic_plan_from_batch,
    parse_probe_driven_mutate_tools_csv,
)


def test_fault_rollout_deterministic_without_probe_csv_allowlist():
    """Probe CSV default omits k8s_rollout_restart; fault rollout must still be deterministic."""
    snip = json.dumps(
        {
            "labels": {
                "namespace": "multi-agent",
                "deployment": "lab-nginx",
                "alertname": "OmniPodCrashLoop",
                "reason": "CrashLoopBackOff",
            }
        }
    )
    batch = [
        {
            "probe": "k8s_clinical_pod_status",
            "canonical_query_snippet": snip,
            "alert_hint": "crashloop",
        }
    ]
    allowed = parse_probe_driven_mutate_tools_csv("")
    assert "k8s_rollout_restart" not in allowed

    plan = deterministic_mutate_plan_from_batch(
        batch,
        default_ns="multi-agent",
        allowed_tools=allowed,
        ws=SimpleNamespace(omni_autonomous_rollout_on_fault_incident=True),
    )
    assert plan is not None
    assert plan.get("tool_name") == "k8s_rollout_restart"
    args = plan.get("args") or {}
    assert args.get("namespace") == "multi-agent"
    assert args.get("deployment") == "lab-nginx"


def test_rollout_args_from_workload_label_chaos_alert():
    """Chaos rule sets workload=chaos-victim without deployment=."""
    from workers.evidence_mutate_emit import rollout_args_from_evidence_batch

    snip = json.dumps(
        {
            "labels": {
                "namespace": "multi-agent",
                "workload": "chaos-victim",
                "alertname": "KubePodCrashLoopVictim",
                "reason": "CrashLoopBackOff",
            }
        }
    )
    batch = [{"canonical_query_snippet": snip}]
    assert rollout_args_from_evidence_batch(batch) == {
        "namespace": "multi-agent",
        "deployment": "chaos-victim",
    }




def test_fault_rollout_blocked_for_broken_spec_secret():
    snip = json.dumps(
        {
            "labels": {
                "namespace": "multi-agent",
                "deployment": "chaos-victim",
                "alertname": "KubePodCrashLoopVictim",
            }
        }
    )
    batch = [
        {
            "canonical_query_snippet": snip,
            "alert_hint": "crashloop",
            "raw": 'MountVolume.SetUp failed for volume "x": secret "chaos-pg-secret" not found',
        }
    ]
    plan = deterministic_mutate_plan_from_batch(
        batch,
        default_ns="multi-agent",
        allowed_tools=parse_probe_driven_mutate_tools_csv(""),
        ws=SimpleNamespace(omni_autonomous_rollout_on_fault_incident=True),
    )
    assert plan is None

def test_chaos_credential_lab_autofix_patch_secret():
    allowed = parse_probe_driven_mutate_tools_csv("k8s_patch_secret,k8s_rollout_restart")
    snip = json.dumps(
        {
            "labels": {
                "namespace": "multi-agent",
                "deployment": "chaos-victim",
                "alertname": "KubePodCrashLoopVictim",
            }
        }
    )
    batch = [
        {
            "canonical_query_snippet": snip,
            "raw": "FATAL: password authentication failed for user chaos_app",
        }
    ]
    ws = SimpleNamespace(
        lab_chaos_credential_autofix_enabled=True,
        chaos_pg_app_password="chaos-app-pass-2025",
        chaos_pg_secret_name="chaos-pg-secret",
        chaos_pg_password_key="APP_PASSWORD",
    )
    plan = deterministic_mutate_plan_from_batch(
        batch,
        default_ns="multi-agent",
        allowed_tools=allowed,
        ws=ws,
    )
    assert plan is not None
    assert plan.get("tool_name") == "k8s_patch_secret"
    args = plan.get("args") or {}
    assert args.get("namespace") == "multi-agent"
    assert args.get("name") == "chaos-pg-secret"
    assert args.get("key") == "APP_PASSWORD"
    assert args.get("value") == "chaos-app-pass-2025"

    assert (
        chaos_credential_lab_autofix_plan_from_batch(
            batch,
            default_ns="multi-agent",
            allowed_tools=allowed,
            ws=SimpleNamespace(lab_chaos_credential_autofix_enabled=False),
        )
        is None
    )

    # Default CSV allowlist omits k8s_patch_secret — lab autofix must still merge it in.
    plan2 = deterministic_mutate_plan_from_batch(
        batch,
        default_ns="multi-agent",
        allowed_tools=parse_probe_driven_mutate_tools_csv(""),
        ws=ws,
    )
    assert plan2 is not None
    assert plan2.get("tool_name") == "k8s_patch_secret"


def test_fault_rollout_disabled_when_setting_off():
    snip = json.dumps(
        {
            "labels": {
                "namespace": "multi-agent",
                "deployment": "lab-nginx",
                "alertname": "OmniPodCrashLoop",
            }
        }
    )
    batch = [{"canonical_query_snippet": snip, "alert_hint": "crashloop"}]
    assert (
        fault_rollout_deterministic_plan_from_batch(
            batch, ws=SimpleNamespace(omni_autonomous_rollout_on_fault_incident=False)
        )
        is None
    )
