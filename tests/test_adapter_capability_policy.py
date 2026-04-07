from __future__ import annotations

from workers.adapters.contracts import AdapterCapabilityPolicy, policy_allows_execute


def test_adapter_policy_prod_requires_approval() -> None:
    pol = AdapterCapabilityPolicy(
        adapter_name="k8s",
        allowed_mutators={"k8s_rollout_restart"},
        allowed_namespaces={"multi-agent"},
        require_approval_in_prod=True,
    )
    ok, reason = policy_allows_execute(
        pol,
        env_mode="prod",
        tool_name="k8s_rollout_restart",
        namespace="multi-agent",
    )
    assert ok is False
    assert reason == "approval_required_in_prod"


def test_adapter_policy_dev_allows_when_in_scope() -> None:
    pol = AdapterCapabilityPolicy(
        adapter_name="k8s",
        allowed_mutators={"k8s_rollout_restart"},
        allowed_namespaces={"multi-agent"},
        require_approval_in_prod=True,
    )
    ok, reason = policy_allows_execute(
        pol,
        env_mode="dev",
        tool_name="k8s_rollout_restart",
        namespace="multi-agent",
    )
    assert ok is True
    assert reason == ""
