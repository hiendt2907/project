"""Coverage: autonomy policy store, llm_contract, preflight secret_refs, two_channel."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from pkg.autonomy import llm_contract
from pkg.autonomy.llm_contract import HighLevelRemediationPlan, RemediationContext
from pkg.reasoning import preflight_deployment_secret_refs as preflight
from pkg.reasoning import two_channel_sdk


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
