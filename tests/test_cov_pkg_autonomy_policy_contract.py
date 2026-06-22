"""Coverage: autonomy policy store, llm_contract, preflight secret_refs, two_channel."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from pkg.autonomy import llm_contract, policy
from pkg.autonomy.llm_contract import HighLevelRemediationPlan, RemediationContext
from pkg.reasoning import preflight_deployment_secret_refs as preflight
from pkg.reasoning import two_channel_sdk


class _FakeRedis:
    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}

    async def get(self, key: str) -> str | None:
        return self.kv.get(key)

    async def set(self, key: str, value: str) -> None:
        self.kv[key] = value

    async def lpush(self, key: str, value: str) -> None:
        self.lists.setdefault(key, []).insert(0, value)

    async def ltrim(self, key: str, start: int, end: int) -> None:
        lst = self.lists.get(key, [])
        self.lists[key] = lst[start : end + 1] if end >= 0 else lst[start:]

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        lst = self.lists.get(key, [])
        return lst[start : end + 1] if end >= 0 else lst[start:]


@pytest.mark.asyncio
async def test_autonomy_policy_store_roundtrip() -> None:
    r = _FakeRedis()
    store = policy.AutonomyPolicyStore()
    rules = await store.get_policy(r)
    assert rules and rules[0].lane == "*"

    new_rule = policy.PolicyRule(
        lane="SYS_RESOURCE",
        severity="high",
        action_type="restart_pod",
        level=policy.AutonomyLevel.HITL,
        reason="test",
    )
    await store.set_rule(r, new_rule)
    loaded = await store.get_policy(r)
    assert loaded[0].lane == "SYS_RESOURCE"

    hist = await store.get_history(r, limit=5)
    assert hist and hist[0].get("action") == "set_rule"

    await store.reset_to_defaults(r)
    final = await store.get_policy(r)
    assert any(x.action_type == "*" for x in final)


@pytest.mark.parametrize(
    "rules,lane,sev,act,expect_action",
    [
        (list(policy.AutonomyPolicyStore.DEFAULT_POLICY), "SIEM_SECURITY", "critical", "x", "HITL"),
        (list(policy.AutonomyPolicyStore.DEFAULT_POLICY), "X", "low", "restart_pod", "FULL_AUTO"),
    ],
)
def test_find_matching_rule(
    rules: list[policy.PolicyRule],
    lane: str,
    sev: str,
    act: str,
    expect_action: str,
) -> None:
    m = policy.find_matching_rule(rules, lane, sev, act)
    assert m is not None and m.level.value == expect_action


@pytest.mark.parametrize(
    "raw,action",
    [
        ('{"action":"noop","target_ref":"","namespace":"ns"}', "noop"),
        ("```json\n{\"action\":\"noop\",\"target_ref\":\"\",\"namespace\":\"n\"}\n```", "noop"),
    ],
)
def test_parse_high_level_plan_json(raw: str, action: str) -> None:
    p = llm_contract.parse_high_level_plan_json(raw)
    assert p is not None and p.action == action


def test_map_high_level_plan_to_mutate() -> None:
    p = HighLevelRemediationPlan.model_validate(
        {
            "action": "rollout_restart",
            "target_ref": "dep",
            "namespace": "ns",
        }
    )
    m = llm_contract.map_high_level_plan_to_mutate(p)
    assert m and m["tool_name"] == "k8s_rollout_restart"


def test_remediation_context_prompt_block() -> None:
    ctx = RemediationContext(trace_id="trace-t", alertname="a", namespace="n", iterations=0)
    assert ctx.to_prompt_block() == ""
    ctx.iterations = 1
    ctx.observations.append(
        llm_contract.ObservationRecord(iteration=1, summary="obs"),
    )
    ctx.actions_taken.append(
        llm_contract.ActionRecord(
            iteration=1, action="noop", target_ref="", namespace="", reasoning="r"
        )
    )
    ctx.outcomes.append(llm_contract.OutcomeRecord(iteration=1, healthy=True, summary="ok"))
    block = ctx.to_prompt_block()
    assert "REMEDIATION HISTORY" in block and "HEALTHY" in block


@pytest.mark.parametrize(
    "text,expect_machine",
    [
        ("", None),
        ("MACHINE_JSON: {\"a\":1}\nHUMAN_SUMMARY: hi", {"a": 1}),
    ],
)
def test_parse_two_channel_sdk_only(text: str, expect_machine: dict | None) -> None:
    out = two_channel_sdk.parse_two_channel_sdk_only(text)
    assert out["machine"] == expect_machine


def test_parse_two_channel_plain_human_only() -> None:
    out = two_channel_sdk.parse_two_channel_sdk_only("just human text without markers")
    assert out["machine"] is None and out["human"] == "just human text without markers"


def test_parse_two_channel_machine_json_with_prefix_before_brace() -> None:
    raw = 'MACHINE_JSON: noise before {"a": 1, "b": 2}\nHUMAN_SUMMARY: tail'
    out = two_channel_sdk.parse_two_channel_sdk_only(raw)
    assert out["machine"] == {"a": 1, "b": 2}
    assert out["human"] == "tail"


def test_secret_refs_from_deployment_minimal() -> None:
    sk = SimpleNamespace(name="sec1", key="k1")
    vf = SimpleNamespace(secret_key_ref=sk)
    ev = SimpleNamespace(name="E1", value_from=vf)
    container = SimpleNamespace(env=[ev], env_from=[])
    pod_spec = SimpleNamespace(containers=[container])
    pod_tpl = SimpleNamespace(spec=pod_spec)
    dep_spec = SimpleNamespace(template=pod_tpl)
    dep = SimpleNamespace(spec=dep_spec)
    refs = preflight.secret_refs_from_deployment(dep)
    assert refs and refs[0]["secret_name"] == "sec1"


def test_secret_refs_malformed_spec() -> None:
    dep = SimpleNamespace(spec=None)
    assert preflight.secret_refs_from_deployment(dep) == []
