"""Phân loại follow-up theo ngữ cảnh (LLM schema)."""

from __future__ import annotations

from workers.clarification_context import (
    MonitoringFollowupLLM,
    followup_llm_to_merge_params,
    format_session_snippet_for_llm,
)


def test_followup_llm_to_merge_params_host() -> None:
    m = MonitoringFollowupLLM(target="host", pod_name=None, namespace=None)
    assert followup_llm_to_merge_params(m) == ("host", None, None)


def test_followup_llm_to_merge_params_pod_with_ns() -> None:
    m = MonitoringFollowupLLM(target="pod", pod_name="omni-worker", namespace="multi-agent")
    assert followup_llm_to_merge_params(m) == ("pod", "omni-worker", "multi-agent")


def test_followup_llm_to_merge_params_unclear() -> None:
    m = MonitoringFollowupLLM(target="unclear")
    assert followup_llm_to_merge_params(m) is None


def test_format_session_snippet() -> None:
    s = format_session_snippet_for_llm(
        last_summary="User hỏi CPU",
        recent_messages=[{"role": "user", "content": "check"}],
    )
    assert "summary" in s.lower() or "[summary]" in s
    assert "user" in s
