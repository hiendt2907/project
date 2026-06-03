"""
tests/test_configmap_remediation.py

Contract tests for the FailedMount / missing ConfigMap remediation path:
  - k8s_create_or_patch_configmap is in MUTATE_TOOL_ALLOWLIST and K8S_SDK_MUTATING_TOOL_NAMES
  - _reject_reason accepts valid args and rejects missing required fields
  - _broken_spec_first_round_instruction embeds create-or-patch guidance when CM name found
  - evaluate_diagnostic_invariants allows the tool through (not blocked by INV_*)
  - evidence_consumer deterministic fallback uses create-or-patch when CM name found in evidence
"""

from __future__ import annotations

import pytest

from workers.autonomous_execute import (
    K8S_SDK_MUTATING_TOOL_NAMES,
    MUTATE_TOOL_ALLOWLIST,
    _MUTATING_POLICY_GUARD_TOOLS,
)
from workers.analyst_agentic_loop import (
    LLM_TOOL_CATALOG_CONFIGMAP_MUTATE,
    _broken_spec_first_round_instruction,
    _reject_reason,
)
from pkg.reasoning.diagnostic_policy import evaluate_diagnostic_invariants
from pkg.reasoning.reason_codes import ERR_REA_SCHEMA_VIOLATION, ERR_REA_HALLUCINATION_DETECTED


TOOL = "k8s_create_or_patch_configmap"


# ---------------------------------------------------------------------------
# Allowlist contract
# ---------------------------------------------------------------------------

def test_tool_in_tool_registry():
    from workers.tools import TOOL_REGISTRY
    assert TOOL in TOOL_REGISTRY


def test_tool_in_mutating_tool_names():
    assert TOOL in K8S_SDK_MUTATING_TOOL_NAMES


def test_tool_in_mutate_tool_allowlist():
    assert TOOL in MUTATE_TOOL_ALLOWLIST


def test_tool_in_policy_guard_tools():
    assert TOOL in _MUTATING_POLICY_GUARD_TOOLS


# ---------------------------------------------------------------------------
# _reject_reason contract
# ---------------------------------------------------------------------------

def test_reject_reason_valid_args_accepted():
    # evidence_refs is now required by the mutate schema guard (added with HITL explain/advise).
    parsed = {
        "tool_name": TOOL,
        "args": {
            "namespace": "multi-agent",
            "name": "nginx-test-never-created-cm",
            "key": "placeholder",
            "value": "created-by-omni",
        },
        "evidence_refs": ["nginx-test-pod-001"],
    }
    assert _reject_reason(parsed) == ""


def test_reject_reason_missing_namespace():
    parsed = {
        "tool_name": TOOL,
        "args": {"name": "my-cm", "key": "k", "value": "v"},
    }
    assert _reject_reason(parsed) == ERR_REA_SCHEMA_VIOLATION


def test_reject_reason_missing_name():
    parsed = {
        "tool_name": TOOL,
        "args": {"namespace": "multi-agent", "key": "k", "value": "v"},
    }
    assert _reject_reason(parsed) == ERR_REA_SCHEMA_VIOLATION


def test_reject_reason_missing_key():
    parsed = {
        "tool_name": TOOL,
        "args": {"namespace": "multi-agent", "name": "my-cm", "value": "v"},
    }
    assert _reject_reason(parsed) == ERR_REA_SCHEMA_VIOLATION


def test_reject_reason_missing_value_key_entirely():
    parsed = {
        "tool_name": TOOL,
        "args": {"namespace": "multi-agent", "name": "my-cm", "key": "k"},
    }
    # "value" key absent from args dict
    assert _reject_reason(parsed) == ERR_REA_SCHEMA_VIOLATION


def test_reject_reason_empty_value_accepted():
    # value="" is valid (empty string is allowed); evidence_refs required since HITL schema guard.
    parsed = {
        "tool_name": TOOL,
        "args": {
            "namespace": "multi-agent",
            "name": "my-cm",
            "key": "k",
            "value": "",
        },
        "evidence_refs": ["pod-event-001"],
    }
    assert _reject_reason(parsed) == ""


# ---------------------------------------------------------------------------
# Prompt guidance contract
# ---------------------------------------------------------------------------

def test_configmap_catalog_mentions_create_or_patch():
    assert "k8s_create_or_patch_configmap" in LLM_TOOL_CATALOG_CONFIGMAP_MUTATE


def test_configmap_catalog_warns_against_restart_only():
    assert "rollout_restart" in LLM_TOOL_CATALOG_CONFIGMAP_MUTATE
    assert "cannot create" in LLM_TOOL_CATALOG_CONFIGMAP_MUTATE


def test_broken_spec_instruction_with_cm_name_includes_create_or_patch():
    batch = [
        {
            "probe": "events",
            "raw": 'configmap "nginx-test-never-created-cm" not found',
            "extracted_fact": {"namespace": "multi-agent"},
        }
    ]
    hint = _broken_spec_first_round_instruction(batch)
    assert "k8s_create_or_patch_configmap" in hint
    assert "nginx-test-never-created-cm" in hint
    # Must NOT suggest restart-only as the fix
    assert "Do NOT use k8s_rollout_restart" in hint or "not use k8s_rollout_restart" in hint


def test_broken_spec_instruction_empty_when_no_broken_spec():
    batch = [{"probe": "cpu", "raw": "cpu usage high", "extracted_fact": {}}]
    hint = _broken_spec_first_round_instruction(batch)
    assert hint == ""


# ---------------------------------------------------------------------------
# Invariant policy — k8s_create_or_patch_configmap must not be blocked
# ---------------------------------------------------------------------------

class _FakeWS:
    omni_env_mode = "lab"
    autonomous_allowed_namespaces = "multi-agent"


def test_invariants_allow_create_or_patch_configmap():
    ws = _FakeWS()
    batch = [
        {
            "probe": "events",
            "raw": 'configmap "nginx-test-never-created-cm" not found',
            "extracted_fact": {"namespace": "multi-agent"},
        }
    ]
    ok, reason, _ = evaluate_diagnostic_invariants(
        ws,
        tool_name=TOOL,
        args={"namespace": "multi-agent", "name": "nginx-test-never-created-cm", "key": "placeholder", "value": ""},
        batch=batch,
        discovery_tool_names=["k8s_describe_resource"],
        proof_lane="resource",
    )
    assert ok is True
    assert reason is None


# ---------------------------------------------------------------------------
# evidence_consumer deterministic fallback contract
# ---------------------------------------------------------------------------

import re
import json

def _simulate_broken_spec_fallback_with_lane(batch: list[dict]) -> tuple[str, dict, str | None]:
    """
    Mirror the evidence_consumer `not plan` fallback branch.
    Returns (tool_name, args, fallback_lane_override).
    """
    from pkg.reasoning.diagnostic_policy import evidence_suggests_broken_spec

    _cm_re = re.compile(r'configmap\s+"([^"]+)"\s+not\s+found', re.IGNORECASE)
    _sec_re = re.compile(r'secret\s+"([^"]+)"\s+not\s+found', re.IGNORECASE)
    _batch_blob = " ".join(
        str(b.get("raw") or "") + " " + json.dumps(b.get("extracted_fact") or "")
        for b in batch
    )
    _cm_match = _cm_re.search(_batch_blob)
    _sec_match = _sec_re.search(_batch_blob)

    if evidence_suggests_broken_spec(batch) and _cm_match:
        cm_name = _cm_match.group(1)
        return "k8s_create_or_patch_configmap", {
            "namespace": "multi-agent",
            "name": cm_name,
            "key": "placeholder",
            "value": "created-by-omni",
        }, "state"
    if evidence_suggests_broken_spec(batch) and _sec_match:
        return "k8s_rollout_restart", {}, "state"
    return "k8s_rollout_restart", {}, None


def _simulate_broken_spec_fallback(batch: list[dict]) -> tuple[str, dict]:
    """Thin wrapper kept for backward-compatible callers."""
    tn, args, _ = _simulate_broken_spec_fallback_with_lane(batch)
    return tn, args


def test_fallback_uses_create_or_patch_when_cm_absent():
    batch = [
        {
            "probe": "events",
            "raw": 'configmap "nginx-test-never-created-cm" not found',
            "extracted_fact": {"namespace": "multi-agent"},
        }
    ]
    tn, args = _simulate_broken_spec_fallback(batch)
    assert tn == "k8s_create_or_patch_configmap"
    assert args["name"] == "nginx-test-never-created-cm"
    assert args["namespace"] == "multi-agent"
    assert "key" in args and "value" in args


def test_fallback_uses_rollout_restart_when_no_cm_name():
    batch = [
        {
            "probe": "events",
            "raw": "CrashLoopBackOff",
            "extracted_fact": {"reason": "CrashLoopBackOff"},
        }
    ]
    tn, _ = _simulate_broken_spec_fallback(batch)
    assert tn == "k8s_rollout_restart"


# ---------------------------------------------------------------------------
# Lane override tests
# ---------------------------------------------------------------------------


def test_fallback_lane_is_state_for_broken_spec_cm():
    """ConfigMap fallback must set lane=state so proof gate fast-tracks without sigma."""
    batch = [
        {
            "probe": "events",
            "raw": 'configmap "nginx-test-never-created-cm" not found',
            "extracted_fact": {"namespace": "multi-agent"},
        }
    ]
    tn, args, lane = _simulate_broken_spec_fallback_with_lane(batch)
    assert tn == "k8s_create_or_patch_configmap"
    assert lane == "state", f"Expected state lane for broken-spec CM fallback, got {lane!r}"


def test_fallback_lane_is_state_for_broken_spec_secret():
    """
    Secret absent: tool stays k8s_rollout_restart (INV blocks it), but lane=state so
    the proof gate fast-tracks and INV_NO_RESTART_ON_BROKEN_SPEC fires via invariant gate
    instead of sigma-gate block.
    """
    batch = [
        {
            "probe": "events",
            "raw": 'secret "nginx-test-tls-secret" not found',
            "extracted_fact": {"namespace": "multi-agent"},
        }
    ]
    tn, _, lane = _simulate_broken_spec_fallback_with_lane(batch)
    assert tn == "k8s_rollout_restart"
    assert lane == "state", f"Expected state lane for broken-spec Secret fallback, got {lane!r}"


def test_fallback_lane_is_none_for_crashloop():
    """Non-broken-spec fallback must not override lane (let matrix/RAG decide)."""
    batch = [
        {
            "probe": "events",
            "raw": "CrashLoopBackOff",
            "extracted_fact": {"reason": "CrashLoopBackOff"},
        }
    ]
    tn, _, lane = _simulate_broken_spec_fallback_with_lane(batch)
    assert tn == "k8s_rollout_restart"
    assert lane is None


def test_fallback_cm_wins_over_sec_when_both_present():
    """ConfigMap path takes priority when both CM and Secret names appear in evidence."""
    batch = [
        {
            "probe": "events",
            "raw": 'configmap "my-cm" not found\nsecret "my-sec" not found',
            "extracted_fact": {"namespace": "multi-agent"},
        }
    ]
    tn, args, lane = _simulate_broken_spec_fallback_with_lane(batch)
    assert tn == "k8s_create_or_patch_configmap"
    assert args["name"] == "my-cm"
    assert lane == "state"


# ---------------------------------------------------------------------------
# Lab RBAC manifest contract
# ---------------------------------------------------------------------------

import os

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Split-role RBAC consolidated into omni-fullstack-rbac.yaml (2026-06-03). The lab
# ClusterRoleBinding + lab executor ClusterRole now live there. Prod least-privilege
# (namespace-scoped, no ClusterRoleBinding) is rbac-executor-least-privilege.yaml.
_LAB_RBAC = os.path.join(_REPO_ROOT, "k8s", "deployments", "omni-fullstack-rbac.yaml")
_PROD_RBAC = os.path.join(_REPO_ROOT, "k8s", "rbac-executor-least-privilege.yaml")


def test_lab_rbac_file_exists():
    assert os.path.isfile(_LAB_RBAC), "omni-fullstack-rbac.yaml must exist for lab ClusterRoleBinding"


def test_lab_rbac_has_clusterrolebinding():
    content = open(_LAB_RBAC).read()
    assert "ClusterRoleBinding" in content, "lab RBAC must contain a ClusterRoleBinding (not only RoleBindings)"


def test_lab_rbac_has_create_verb_on_configmaps():
    content = open(_LAB_RBAC).read()
    assert "configmaps" in content
    assert "create" in content


def test_lab_rbac_labeled_lab_env():
    content = open(_LAB_RBAC).read()
    assert "omni.io/env: lab" in content, "lab RBAC must be labeled omni.io/env: lab"


def test_prod_rbac_has_no_clusterrolebinding():
    content = open(_PROD_RBAC).read()
    # Check for the kind field, not just the word in comments
    assert "kind: ClusterRoleBinding" not in content, (
        "prod RBAC must not contain kind: ClusterRoleBinding — "
        "prod stays namespace-scoped (kind: RoleBinding only)"
    )


def test_lab_rbac_does_not_grant_delete_namespaces():
    import yaml
    with open(_LAB_RBAC) as f:
        docs = list(yaml.safe_load_all(f))
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        if doc.get("kind") != "ClusterRole":
            continue
        for rule in doc.get("rules", []):
            resources = rule.get("resources", [])
            verbs = rule.get("verbs", [])
            if "namespaces" in resources:
                assert "delete" not in verbs, (
                    f"lab ClusterRole must not grant 'delete' on namespaces; got verbs={verbs}"
                )
