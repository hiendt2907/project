"""llm_trace helpers."""

from types import SimpleNamespace

from workers.llm_trace import agentic_parse_failure_hint, llm_trace_enabled


def test_agentic_parse_failure_hint_empty():
    assert agentic_parse_failure_hint("") == "empty_content"


def test_agentic_parse_failure_hint_valid_json():
    raw = 'prefix {"tool_name":"k8s_rollout_restart","args":{"namespace":"a","deployment":"b"}} tail'
    assert agentic_parse_failure_hint(raw) == "parse_would_succeed"


def test_llm_trace_enabled_default_off():
    assert llm_trace_enabled(SimpleNamespace()) is False
    assert llm_trace_enabled(SimpleNamespace(omni_llm_trace_enabled=True)) is True
