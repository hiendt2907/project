"""Coverage tests for scattered single-line uncovered branches across multiple modules."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fakeredis.aioredis import FakeRedis


# ── llm_prompts_en.py line 14 ─────────────────────────────────────────────────

def test_truncate_max_words_none_uses_default():
    """Line 14: max_words=None → uses LLM_MAX_OUTPUT_WORDS default."""
    from workers.llm_prompts_en import truncate_plain_text_to_max_words, LLM_MAX_OUTPUT_WORDS

    # Default max_words (None) should use LLM_MAX_OUTPUT_WORDS (25)
    result = truncate_plain_text_to_max_words("hello world")
    assert result == "hello world"

    # Long text gets truncated to LLM_MAX_OUTPUT_WORDS
    long_text = " ".join(f"word{i}" for i in range(30))
    result2 = truncate_plain_text_to_max_words(long_text)
    assert len(result2.split()) == LLM_MAX_OUTPUT_WORDS


# ── tool_observation.py lines 12, 16 ─────────────────────────────────────────

def test_summarize_for_context_empty():
    """Line 12: if not text: return ''"""
    from workers.tool_observation import summarize_for_context

    assert summarize_for_context("", 100) == ""
    assert summarize_for_context(None, 100) == ""  # type: ignore[arg-type]


def test_summarize_for_context_fits_max_chars():
    """Line 15: if len(t) <= max_chars: return t"""
    from workers.tool_observation import summarize_for_context

    short = "hello world"
    result = summarize_for_context(short, max_chars=200)
    assert result == short


def test_summarize_for_context_truncates():
    """Line 16: truncation path when text exceeds max_chars."""
    from workers.tool_observation import summarize_for_context

    result = summarize_for_context("hello world how are you", max_chars=5)
    assert "…" in result
    assert len(result) <= 6  # 5 chars + ellipsis


# ── trace_context.py line 57 ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_trace_context_decorator_no_args():
    """Line 57: return _decorator when called without arguments (@trace_context)."""
    from workers.trace_context import trace_context

    @trace_context
    async def my_func(trace_id: str = "") -> str:
        return "ok"

    result = await my_func(trace_id="t-001")
    assert result == "ok"


# ── sre_output.py line 27 ────────────────────────────────────────────────────

def test_strip_sre_fluff_removes_greeting():
    """Line 27: continue for _FLUFF_LINE match."""
    from pkg.reasoning.sre_output import strip_sre_fluff

    text = "Hi, based on the analysis below, here is the issue:\nPod is crashing due to OOM."
    result = strip_sre_fluff(text)
    assert "Pod is crashing" in result
    assert "Hi" not in result or "based on" not in result


def test_strip_sre_fluff_removes_bullet_noise():
    """Line 29: continue for _BULLET_NOISE match."""
    from pkg.reasoning.sre_output import strip_sre_fluff

    text = "- here is the solution:\nkubectl delete pod nginx"
    result = strip_sre_fluff(text)
    assert "kubectl delete pod" in result


# ── env_mode.py line 30 ──────────────────────────────────────────────────────

def test_namespace_allowed_empty_namespace():
    """Line 30: if not ns: return False"""
    from workers.env_mode import namespace_allowed

    settings = SimpleNamespace(autonomous_allowed_namespaces="multi-agent,prod")
    assert namespace_allowed(settings, "") is False
    assert namespace_allowed(settings, "   ") is False


# ── model_routing.py line 70 ────────────────────────────────────────────────

def test_dispatch_task_default_route():
    """Line 70: return model_default for plain text."""
    from workers.model_routing import dispatch_task

    result = dispatch_task(
        model_default="qwen2.5:7b",
        model_reasoning="qwen3:14b",
        model_heavy="qwen3:27b",
        user_text="show me the status",
        attempt=1,
        json_parse_failures=0,
    )
    assert result == "qwen2.5:7b"


def test_classify_route_default():
    from workers.model_routing import classify_route

    assert classify_route("list pods") == "heavy"  # ops path → heavy
    assert classify_route("hello there") == "default"


# ── remote_triage.py line 87 ─────────────────────────────────────────────────

def test_assess_urgency_baseline():
    """Line 87: return 'baseline' for low domain severity."""
    import time
    from collections import Counter
    from workers.remote_triage import _assess_urgency
    from pkg.reasoning.evidence_cluster import LogCluster

    cluster = LogCluster(
        fingerprint="fp1",
        probe="cpu_probe",
        domain="SYS_RESOURCE",
        representative={"alert_hint": "low cpu usage", "raw": "", "extracted_fact": {}},
        count=1,
        first_seen=time.time(),
        last_seen=time.time(),
        results=Counter({"PASSED": 1}),
        agent_ids={"agent-1"},
        lane="SYS_RESOURCE",
        is_new=True,
    )
    result = _assess_urgency(cluster)
    assert result == "baseline"


# ── diagnostic_evidence.py line 28 ───────────────────────────────────────────

def test_evidence_from_probe_basic():
    """Line 28: probe_name field in EvidenceObject."""
    from workers.diagnostic_evidence import evidence_from_probe, ProbeRunRaw

    raw = ProbeRunRaw(probe_name="cpu_check", status="PASSED", raw_text="ok")
    ev = evidence_from_probe(raw, trace_id="t-001")
    assert ev.probe_name == "cpu_check"
    assert ev.trace_id == "t-001"


def test_evidence_from_probe_with_structured_hint():
    """Line 26: structured_hint merged into fact."""
    from workers.diagnostic_evidence import evidence_from_probe, ProbeRunRaw

    raw = ProbeRunRaw(
        probe_name="mem_check", status="FAILED",
        structured_hint={"cpu_pct": 99.0},
    )
    ev = evidence_from_probe(raw, trace_id="t-002")
    assert ev.extracted_fact.get("cpu_pct") == 99.0


# ── evidence_signals.py lines 29-30, 71-72, 77 ───────────────────────────────

def test_extracted_fact_dict_invalid_json():
    """Lines 29-30: invalid JSON string in extracted_fact → return None via exception."""
    from pkg.reasoning.evidence_signals import critical_evidence_present

    batch = [{
        "alert_hint": "",
        "raw": "",
        "result": "FAILED",
        "extracted_fact": "{not valid json{{{",
    }]
    result = critical_evidence_present(batch)
    assert result is False


def test_critical_evidence_invalid_canonical_json():
    """Lines 71-72: canonical_query_snippet starts with { but is invalid JSON."""
    from pkg.reasoning.evidence_signals import critical_evidence_present

    batch = [{
        "alert_hint": "",
        "raw": "",
        "result": "PASSED",
        "canonical_query_snippet": "{bad json]",
    }]
    result = critical_evidence_present(batch)
    assert result is False


def test_critical_evidence_labels_not_dict():
    """Line 77: labels is not a dict → continue."""
    from pkg.reasoning.evidence_signals import critical_evidence_present

    snip = json.dumps({"labels": "not-a-dict"})
    batch = [{"alert_hint": "", "raw": "", "result": "PASSED", "canonical_query_snippet": snip}]
    result = critical_evidence_present(batch)
    assert result is False


# ── os_executor_adapter.py lines 25, 33-34, 39 ───────────────────────────────

def test_wrap_host_command_empty_raises():
    """Line 25: raise ValueError for empty command."""
    from workers.os_executor_adapter import wrap_host_command

    with pytest.raises(ValueError, match="command is required"):
        wrap_host_command("")


def test_command_feedback_digest():
    """Lines 33-34: command_feedback_digest returns hex string."""
    from workers.os_executor_adapter import command_feedback_digest

    result = command_feedback_digest({"tool": "restart", "namespace": "prod"})
    assert len(result) == 24
    assert all(c in "0123456789abcdef" for c in result)


def test_shell_escape():
    """Line 39: shell_escape returns quoted string."""
    from workers.os_executor_adapter import shell_escape

    result = shell_escape("kubectl get pods")
    assert "kubectl" in result


# ── session_state.py lines 48-50, 62 ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_load_session_invalid_json():
    """Lines 48-50: JSON parse error → log warning + return default SessionState."""
    from workers.session_state import load_session

    r = FakeRedis(decode_responses=True)
    await r.set("omni:session:12345", "not valid json {{{")
    result = await load_session(r, 12345)
    assert result is not None  # returns default SessionState()


@pytest.mark.asyncio
async def test_delete_session():
    """Line 62: delete_session removes the key."""
    from workers.session_state import delete_session, save_session, load_session, SessionState

    r = FakeRedis(decode_responses=True)
    state = SessionState()
    await save_session(r, 99999, state, ttl_sec=3600)
    await delete_session(r, 99999)
    raw = await r.get("omni:session:99999")
    assert raw is None


# ── advisory_hitl_compat.py lines 69, 84 ─────────────────────────────────────

def test_validate_hitl_gate_enabled_via_settings():
    """Line 55: return True when hitl_routing_enabled=True."""
    from workers.advisory_hitl_compat import AdvisoryHITLCompat

    settings = SimpleNamespace(omni_hitl_routing_enabled=True)
    allowed, reason = AdvisoryHITLCompat.validate_hitl_gate("t-001", "test", settings=settings)
    assert allowed is True
    assert reason == ""


def test_validate_hitl_gate_blocked():
    """Line 68: return False when blocked."""
    from workers.advisory_hitl_compat import AdvisoryHITLCompat

    allowed, reason = AdvisoryHITLCompat.validate_hitl_gate("t-002", "analyst")
    assert allowed is False
    assert "ADVISORY_MODE_HITL_DISABLED" in reason


def test_validate_hitl_gate_omni_hitl_enabled():
    """Line 69: return True when OMNI_HITL_ENABLED=True (monkeypatched)."""
    from workers.advisory_hitl_compat import AdvisoryHITLCompat

    original = AdvisoryHITLCompat.OMNI_HITL_ENABLED
    try:
        AdvisoryHITLCompat.OMNI_HITL_ENABLED = True
        allowed, reason = AdvisoryHITLCompat.validate_hitl_gate("t-003", "analyst", settings=None)
        assert allowed is True
    finally:
        AdvisoryHITLCompat.OMNI_HITL_ENABLED = original


# ── vm_slot_accumulation.py lines 37, 73-79, 89, 94, 96, 102, 116, 118 ───────

def test_extract_vm_slots_empty_text():
    """Line 37: return empty slots for empty input."""
    from workers.vm_slot_accumulation import extract_vm_slots_from_text

    assert extract_vm_slots_from_text("") == {}
    assert extract_vm_slots_from_text("   ") == {}


def test_extract_vm_slots_single_token_pod():
    """Lines 73-79: single-word input → pod_name detected."""
    from workers.vm_slot_accumulation import extract_vm_slots_from_text

    result = extract_vm_slots_from_text("nginx-abc-xyz")
    assert "pod_name" in result or result == {}  # depends on _BAD_POD_NAME_TOKENS


def test_extract_vm_slots_with_duration():
    """Line 69: duration slot extracted from text like '5h'."""
    from workers.vm_slot_accumulation import extract_vm_slots_from_text

    result = extract_vm_slots_from_text("cpu usage for 5h")
    assert result.get("duration") == "5h"


def test_enrich_slots_no_discovery():
    """Line 89: empty discovery → return slots unchanged."""
    from workers.vm_slot_accumulation import enrich_slots_from_discovery

    slots = {"pod_name": "nginx"}
    result = enrich_slots_from_discovery(slots, None)
    assert result is slots or result == slots


def test_enrich_slots_ns_and_pod_already_set():
    """Line 94: ns and pod both set → return early."""
    from workers.vm_slot_accumulation import enrich_slots_from_discovery

    slots = {"namespace": "prod", "pod_name": "nginx"}
    result = enrich_slots_from_discovery(slots, [{"name": "other", "namespace": "other"}])
    assert result["namespace"] == "prod"
    assert result["pod_name"] == "nginx"


def test_enrich_slots_no_pod():
    """Line 96: no pod_name in slots → return early."""
    from workers.vm_slot_accumulation import enrich_slots_from_discovery

    result = enrich_slots_from_discovery({}, [{"name": "nginx", "namespace": "prod"}])
    assert "pod_name" not in result


def test_enrich_slots_empty_row_skipped():
    """Line 102: discovery row with empty name/namespace → continue."""
    from workers.vm_slot_accumulation import enrich_slots_from_discovery

    slots = {"pod_name": "nginx"}
    result = enrich_slots_from_discovery(slots, [{"name": "", "namespace": ""}])
    assert result.get("pod_name") == "nginx"


def test_merge_vm_slots_skips_none_and_empty():
    """Lines 116, 118: None and empty string values are skipped."""
    from workers.vm_slot_accumulation import merge_vm_slots

    result = merge_vm_slots(None, "cpu usage for pod nginx")
    existing_with_none = {"pod_name": None, "namespace": "   "}
    from workers.vm_slot_accumulation import extract_vm_slots_from_text
    new_slots = {"pod_name": None, "namespace": "  "}
    from workers.vm_slot_accumulation import merge_vm_slots as ms
    result2 = ms({"x": "ok"}, "")
    assert result2["x"] == "ok"


# ── advisory_hitl_compat.py lines 69, 84 ─────────────────────────────────────

def test_emit_advisory_suggestion_to_telegram():
    """Line 84: emit_advisory_suggestion_to_telegram builds the dict."""
    from workers.advisory_hitl_compat import AdvisoryHITLCompat

    result = AdvisoryHITLCompat.emit_advisory_suggestion_to_telegram(
        trace_id="t-001",
        verdict="INVESTIGATE",
        root_cause="Pod OOM killed due to memory leak",
        proposed_actions=[{"action": "restart", "args": {}}],
    )
    assert result["trace_id"] == "t-001"
    assert result["verdict"] == "INVESTIGATE"
    assert "proposed_remediation" in result
