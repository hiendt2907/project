"""Coverage: pkg.autonomous_actions Kafka body builders."""

from __future__ import annotations

import pytest

from pkg import autonomous_actions as aa


def test_build_execute_mutate_body_correlation_and_chain() -> None:
    body = aa.build_execute_mutate_body(
        "t1",
        tool_name="k8s_rollout_restart",
        args={"namespace": "ns", "deployment": "d"},
        attempt_count=0,
        correlation_id="",
        reasoning_chain={"verdict": "x"},
        tenant_id="acme",
    )
    assert body["action"] == aa.ACTION_EXECUTE_MUTATE
    assert body["data"]["attempt_count"] == 1
    assert "correlation_id" in body["data"] and len(body["data"]["correlation_id"]) > 8
    assert body["data"]["reasoning_chain"]["verdict"] == "x"
    assert body["data"]["tenant_id"] == "acme"


def test_build_action_feedback_body() -> None:
    fb = aa.build_action_feedback_body(
        trace_id="trace-t",
        tool_name="x",
        correlation_id="c",
        stdout="o",
        stderr="e",
        exit_code=2,
        status="fail",
        skipped_reason="r",
        mutate_args={"a": 1},
    )
    assert fb["exit_code"] == 2 and fb["mutate_args"] == {"a": 1}


def test_build_suggest_os_runbook_body() -> None:
    rb = aa.build_suggest_os_runbook_body(
        "t2",
        diagnosis="d",
        confidence=1.5,
        source="src",
        runbook_title="title",
        commands=[{"step": 1}],
        reasoning_chain={"lane": "state"},
    )
    assert rb["action"] == aa.ACTION_SUGGEST_OS_RUNBOOK
    assert rb["data"]["confidence"] == 1.0


@pytest.mark.parametrize(
    "text,code",
    [
        ("[data] error happened", 1),
        ("ok diagnosis looks fine", 0),
        ("error in first line\n" + "x" * 250 + "diagnosis later", 1),
    ],
)
def test_infer_exit_code_from_tool_output(text: str, code: int) -> None:
    assert aa.infer_exit_code_from_tool_output(text) == code
