"""INV_NAMESPACE_ISOLATION guard for the lab auto-execute path.

A mutate targeting a namespace outside `autonomous_allowed_namespaces` MUST be
rejected with ERR_GOV_NS_OUT_OF_BOUNDS in EVERY env mode (lab included),
because auto-execute fires in lab. A mutate against the in-scope target
multi-agent/nginx-test MUST pass governance when auto-execute is on.
"""

from __future__ import annotations

from types import SimpleNamespace

from pkg.executor.mutate_governance import governance_check_executor_mutate
from pkg.reasoning.reason_codes import (
    ERR_GOV_NS_OUT_OF_BOUNDS,
    ERR_GOV_UNAUTHORIZED_MUTATION,
)


def _lab_settings(*, auto_execute: bool = True, allowed: str = "multi-agent") -> SimpleNamespace:
    """Lab settings with auto-execute on and the default namespace allowlist."""
    return SimpleNamespace(
        env_mode="dev",  # lab posture (non-prod)
        omni_auto_execute_enabled=auto_execute,
        autonomous_allowed_namespaces=allowed,
        omni_high_risk_mutate_allowed=False,
        omni_kubectl_cluster_mutate_allowed=False,
    )


# ── Rejected: out-of-scope namespace ─────────────────────────────────────────
def test_mutate_other_namespace_rejected_in_lab():
    # Arrange — lab, auto-execute on, target kube-system (out of scope)
    settings = _lab_settings()

    # Act
    ok, msg = governance_check_executor_mutate(
        settings=settings,
        resolved_tool_name="k8s_rollout_restart",
        args={"namespace": "kube-system", "deployment": "coredns"},
    )

    # Assert — fail-closed with the governance reason code
    assert ok is False
    assert ERR_GOV_NS_OUT_OF_BOUNDS in msg
    assert "INV_NAMESPACE_ISOLATION" in msg


def test_mutate_default_namespace_rejected_in_lab():
    settings = _lab_settings()

    ok, msg = governance_check_executor_mutate(
        settings=settings,
        resolved_tool_name="k8s_patch_configmap",
        args={"namespace": "default", "name": "some-config"},
    )

    assert ok is False
    assert ERR_GOV_NS_OUT_OF_BOUNDS in msg


def test_finguard_namespace_rejected_when_not_allowlisted():
    settings = _lab_settings(allowed="multi-agent")

    ok, msg = governance_check_executor_mutate(
        settings=settings,
        resolved_tool_name="k8s_scale_deployment",
        args={"namespace": "finguard-customer", "name": "hitl-api"},
    )

    assert ok is False
    assert ERR_GOV_NS_OUT_OF_BOUNDS in msg


# ── Allowed: in-scope multi-agent/nginx-test ─────────────────────────────────
def test_mutate_multi_agent_nginx_test_allowed_with_auto_execute():
    # Arrange — lab, auto-execute on, scoped target
    settings = _lab_settings()

    # Act
    ok, msg = governance_check_executor_mutate(
        settings=settings,
        resolved_tool_name="k8s_rollout_restart",
        args={"namespace": "multi-agent", "deployment": "nginx-test"},
    )

    # Assert — governance passes (downstream tier/kill-switch still apply)
    assert ok is True
    assert msg == ""


def test_mutate_multi_agent_patch_configmap_allowed():
    settings = _lab_settings()

    ok, msg = governance_check_executor_mutate(
        settings=settings,
        resolved_tool_name="k8s_patch_configmap",
        args={"namespace": "multi-agent", "name": "omni-worker-config"},
    )

    assert ok is True
    assert msg == ""


# ── Defense-in-depth: even with a widened allowlist, DNS-label guard holds ────
def test_invalid_namespace_label_rejected():
    settings = _lab_settings(allowed="multi-agent,Bad_NS")

    ok, msg = governance_check_executor_mutate(
        settings=settings,
        resolved_tool_name="k8s_rollout_restart",
        args={"namespace": "Bad_NS", "deployment": "x"},
    )

    assert ok is False
    # invalid DNS-label is a distinct rejection from the allowlist check
    assert ERR_GOV_UNAUTHORIZED_MUTATION in msg
