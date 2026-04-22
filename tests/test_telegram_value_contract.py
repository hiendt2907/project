"""Tests for Telegram Value Contract: format_operator_action_card + RAG_MISS_SDK_ESCALATE body."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import pytest

from workers.telegram_escalation import (
    format_operator_action_card,
    format_operator_triage_card,
)


# ---------------------------------------------------------------------------
# A) format_operator_triage_card unit tests (Problem / Reason / Chain / Advise)
# ---------------------------------------------------------------------------

def test_card_has_all_four_sections():
    body = format_operator_triage_card(
        problem="CrashLoop on prod/api",
        reason="container exits immediately",
        chain=["t1 state: pod=Pending", "t2 app_log: ERR connection refused"],
        advise=["kubectl get pods -n prod"],
    )
    assert "Problem:" in body
    assert "Reason:" in body
    assert "Chain:" in body
    assert "Advise:" in body


def test_card_problem_and_reason_rendered():
    body = format_operator_triage_card(
        problem="HighMemoryUsage on myns/api",
        reason="OOMKilled observed on last restart",
        chain=None,
        advise=None,
    )
    assert "HighMemoryUsage on myns/api" in body
    assert "OOMKilled observed on last restart" in body


def test_card_empty_problem_has_fallback():
    body = format_operator_triage_card(
        problem="",
        reason="",
        chain=None,
        advise=None,
    )
    assert "no problem description extracted" in body
    assert "reason unknown" in body


def test_card_back_compat_action_card_maps_to_triage():
    body = format_operator_action_card(
        known_facts={"alert": "CrashLoop", "namespace": "prod", "deployment": "api"},
        missing_facts=["pod name"],
        suggested_steps=["kubectl get pods -n prod"],
    )
    assert "Problem:" in body
    assert "CrashLoop on prod/api" in body
    assert "kubectl get pods -n prod" in body


def test_card_empty_steps_has_fallback():
    body = format_operator_action_card(
        known_facts={"namespace": "ns"},
        missing_facts=[],
        suggested_steps=[],
    )
    assert "escalate to on-call" in body


def test_card_missing_facts_rendered():
    body = format_operator_action_card(
        known_facts={"namespace": "ns"},
        missing_facts=["pod name", "RAG knowledge: miss"],
        suggested_steps=["kubectl get pods -n ns"],
    )
    assert "pod name" in body
    assert "RAG knowledge: miss" in body


def test_card_suggested_steps_rendered():
    body = format_operator_action_card(
        known_facts={"namespace": "ns"},
        missing_facts=["pod name"],
        suggested_steps=["kubectl get pods -n ns --show-labels", "escalate to on-call"],
    )
    assert "kubectl get pods -n ns --show-labels" in body
    assert "escalate to on-call" in body


# ---------------------------------------------------------------------------
# B) RAG_MISS_SDK_ESCALATE integration: Telegram body shape
# ---------------------------------------------------------------------------

def _make_ctx(telegram_captures: list, **settings_overrides):
    settings_defaults = {
        "rag_truth_law_enforced": True,
        "trace_correlation_ping_enabled": True,
        "kafka_topic_actions": "omni-actions",
        "kafka_topic_hitl_pending": "omni-hitl-pending",
        "model_reasoning_engine": "llama3",
        "diag_evidence_llm_model": "",
        "omni_siem_suggest_only": True,
        "omni_llm_first_autonomy_enabled": False,
        "omni_unrestricted_tool_execution": True,
        "omni_legacy_deterministic_fallback": False,
        "omni_planner_precondition_gate_enabled": False,
        "omni_auto_execute_enabled": False,
        "diag_k8s_expert_rag_enabled": False,
        "baseline_snapshot_enabled": False,
        "autonomous_agentic_max_steps": 3,
        "autonomous_sigma_observation_window": 1,
        "baseline_dr_z_threshold": 3.0,
        "omni_proof_lane_enabled": True,
        "rag_evidence_contradiction_check_enabled": False,
        "lab_chaos_credential_autofix_enabled": False,
        "omni_shadow_os_mode": False,
        "omni_sigma_log_bypass_enabled": False,
        "telegram_admin_chat_id": "12345",
        "kafka_topic_diagnostic_evidence": "omni-diagnostic-evidence",
    }
    settings_defaults.update(settings_overrides)
    settings = SimpleNamespace(**settings_defaults)

    kafka = MagicMock()
    kafka.send_dict = AsyncMock()

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    scout_ready = MagicMock()
    scout_ready.is_set.return_value = True

    llm = MagicMock()

    tg = MagicMock()

    async def _send_message(chat_id, text):
        telegram_captures.append({"chat_id": chat_id, "text": text})

    tg.send_message = AsyncMock(side_effect=_send_message)

    ctx = SimpleNamespace(
        settings=settings,
        kafka=kafka,
        redis=redis,
        scout_ready=scout_ready,
        llm=llm,
        telegram=tg,
        vector_store=None,
        inbound_trace_id="",
    )
    return ctx


@pytest.mark.asyncio
async def test_rag_miss_sdk_escalate_telegram_has_structure():
    """RAG_MISS_SDK_ESCALATE Telegram message must contain trace=, KNOWN:, MISSING:, NEXT:."""
    from workers.evidence_consumer import reason_from_diagnostic_evidence

    tg_captures: list = []
    ctx = _make_ctx(tg_captures)

    ev_doc = {
        "kind": "diagnostic_evidence",
        "trace_id": "tg-contract-trace-001",
        "probe": "k8s_clinical_pod_status",
        "alert_rule": "KubePodCrashLoopBacking",
        "alert_hint": "CrashLoopBackOff namespace=blue-ns deployment=web-api",
        "canonical_query_snippet": json.dumps({
            "labels": {"namespace": "blue-ns", "deployment": "web-api"}
        }),
        "extracted_fact": {"phase": "CrashLoopBackOff"},
        "namespace": "blue-ns",
        "deployment": "web-api",
        "pod": "",
        "result": "CrashLoopBackOff",
        "raw": "",
        "ts": "1700000000",
        "evidence_source": "K8s_SDK",
        "symptom_group": "pod_container_state",
        "layer": "workload",
        "clinical_priority_note": "",
    }

    escalate_llm = (
        'MACHINE_JSON: {"verdict":"ESCALATE","hypothesis":"Cannot determine root cause",'
        '"action":{"tool":"","args":{}}}\n'
        "HUMAN_SUMMARY: Insufficient context to diagnose."
    )
    ctx.llm.chat = AsyncMock(return_value={"message": {"content": escalate_llm}})

    import unittest.mock as mock
    with (
        mock.patch("workers.evidence_consumer.append_evidence_and_take_flush_batch", new_callable=AsyncMock) as mock_batch,
        mock.patch("workers.evidence_consumer.merge_preflight_deployment_secret_refs", new_callable=AsyncMock) as mock_merge,
        mock.patch("workers.evidence_consumer.evaluate_rag_gate", new_callable=AsyncMock) as mock_gate,
        mock.patch("workers.evidence_consumer.compare_alert_claim_to_sdk_state", return_value=None),
        mock.patch("workers.evidence_consumer.emit_transition", new_callable=AsyncMock),
        mock.patch("workers.evidence_consumer.emit_terminal_tombstone", new_callable=AsyncMock),
        mock.patch("workers.evidence_consumer.run_shadow_selflearning", new_callable=AsyncMock),
        mock.patch("workers.evidence_consumer._emit_agentic_mutate_if_any", new_callable=AsyncMock, return_value=False),
        mock.patch("workers.evidence_consumer.send_telegram_out_for_inbound", new_callable=AsyncMock),
        mock.patch("workers.evidence_consumer.store_autonomous_trace_context", new_callable=AsyncMock),
    ):
        mock_batch.return_value = [ev_doc]
        mock_merge.return_value = [ev_doc]
        gate_out = SimpleNamespace(
            hit=False, formatted="", best_score=None,
            match_text_en="", suggested_tool="", detail=None, chunk_ids=[],
        )
        mock_gate.return_value = gate_out
        await reason_from_diagnostic_evidence(ctx, {"data": json.dumps(ev_doc)})

    escalate_msgs = [m for m in tg_captures if "RAG_MISS_SDK_ESCALATE" in m["text"]]
    assert escalate_msgs, f"Expected RAG_MISS_SDK_ESCALATE Telegram message; got: {tg_captures}"
    body = escalate_msgs[0]["text"]

    assert "trace=" in body, f"Missing trace= in: {body!r}"
    assert "Problem:" in body, f"Missing Problem: section in: {body!r}"
    assert "Reason:" in body, f"Missing Reason: section in: {body!r}"
    assert "Chain:" in body, f"Missing Chain: section in: {body!r}"
    assert "Advise:" in body, f"Missing Advise: section in: {body!r}"


@pytest.mark.asyncio
async def test_rag_miss_sdk_escalate_body_not_empty_context_only():
    """Telegram body must not be reason=RAG_MISS_SDK_ESCALATE with no other context."""
    from workers.evidence_consumer import reason_from_diagnostic_evidence

    tg_captures: list = []
    ctx = _make_ctx(tg_captures)

    ev_doc = {
        "kind": "diagnostic_evidence",
        "trace_id": "tg-nonempty-trace-002",
        "probe": "k8s_clinical_pod_status",
        "alert_rule": "KubePodCrashLoopBacking",
        "alert_hint": "CrashLoopBackOff namespace=red-ns deployment=svc-b",
        "canonical_query_snippet": json.dumps({
            "labels": {"namespace": "red-ns", "deployment": "svc-b"}
        }),
        "extracted_fact": {},
        "namespace": "red-ns",
        "deployment": "svc-b",
        "pod": "",
        "result": "",
        "raw": "",
        "ts": "1700000002",
        "evidence_source": "K8s_SDK",
        "symptom_group": "",
        "layer": "",
        "clinical_priority_note": "",
    }

    escalate_llm = (
        'MACHINE_JSON: {"verdict":"ESCALATE","hypothesis":"No data",'
        '"action":{"tool":"","args":{}}}\n'
        "HUMAN_SUMMARY: No usable data."
    )
    ctx.llm.chat = AsyncMock(return_value={"message": {"content": escalate_llm}})

    import unittest.mock as mock
    with (
        mock.patch("workers.evidence_consumer.append_evidence_and_take_flush_batch", new_callable=AsyncMock) as mock_batch,
        mock.patch("workers.evidence_consumer.merge_preflight_deployment_secret_refs", new_callable=AsyncMock) as mock_merge,
        mock.patch("workers.evidence_consumer.evaluate_rag_gate", new_callable=AsyncMock) as mock_gate,
        mock.patch("workers.evidence_consumer.compare_alert_claim_to_sdk_state", return_value=None),
        mock.patch("workers.evidence_consumer.emit_transition", new_callable=AsyncMock),
        mock.patch("workers.evidence_consumer.emit_terminal_tombstone", new_callable=AsyncMock),
        mock.patch("workers.evidence_consumer.run_shadow_selflearning", new_callable=AsyncMock),
        mock.patch("workers.evidence_consumer._emit_agentic_mutate_if_any", new_callable=AsyncMock, return_value=False),
        mock.patch("workers.evidence_consumer.send_telegram_out_for_inbound", new_callable=AsyncMock),
        mock.patch("workers.evidence_consumer.store_autonomous_trace_context", new_callable=AsyncMock),
    ):
        mock_batch.return_value = [ev_doc]
        mock_merge.return_value = [ev_doc]
        gate_out = SimpleNamespace(
            hit=False, formatted="", best_score=None,
            match_text_en="", suggested_tool="", detail=None, chunk_ids=[],
        )
        mock_gate.return_value = gate_out
        await reason_from_diagnostic_evidence(ctx, {"data": json.dumps(ev_doc)})

    escalate_msgs = [m for m in tg_captures if "RAG_MISS_SDK_ESCALATE" in m["text"]]
    assert escalate_msgs, f"Expected RAG_MISS_SDK_ESCALATE message; got: {tg_captures}"
    body = escalate_msgs[0]["text"]

    lines = [l.strip() for l in body.strip().splitlines() if l.strip()]
    non_header = [l for l in lines if not l.startswith("[RED_ESCALATION]") and l != "reason=RAG_MISS_SDK_ESCALATE"]
    assert non_header, f"Body has no content beyond header lines: {body!r}"


@pytest.mark.asyncio
async def test_rag_miss_sdk_escalate_known_includes_alert_and_namespace():
    """KNOWN: section must include alert name and namespace when available."""
    from workers.evidence_consumer import reason_from_diagnostic_evidence

    tg_captures: list = []
    ctx = _make_ctx(tg_captures)

    ev_doc = {
        "kind": "diagnostic_evidence",
        "trace_id": "tg-known-fields-003",
        "probe": "k8s_clinical_pod_status",
        "alert_rule": "HighMemoryUsage",
        "alert_hint": "OOMKilled namespace=staging deployment=cache",
        "canonical_query_snippet": json.dumps({
            "labels": {"namespace": "staging", "deployment": "cache"}
        }),
        "extracted_fact": {},
        "namespace": "staging",
        "deployment": "cache",
        "pod": "",
        "result": "OOMKilled",
        "raw": "",
        "ts": "1700000003",
        "evidence_source": "K8s_SDK",
        "symptom_group": "",
        "layer": "",
        "clinical_priority_note": "",
    }

    ctx.llm.chat = AsyncMock(return_value={"message": {"content": (
        'MACHINE_JSON: {"verdict":"ESCALATE","hypothesis":"OOM",'
        '"action":{"tool":"","args":{}}}\n'
        "HUMAN_SUMMARY: OOMKilled detected."
    )}})

    import unittest.mock as mock
    with (
        mock.patch("workers.evidence_consumer.append_evidence_and_take_flush_batch", new_callable=AsyncMock) as mock_batch,
        mock.patch("workers.evidence_consumer.merge_preflight_deployment_secret_refs", new_callable=AsyncMock) as mock_merge,
        mock.patch("workers.evidence_consumer.evaluate_rag_gate", new_callable=AsyncMock) as mock_gate,
        mock.patch("workers.evidence_consumer.compare_alert_claim_to_sdk_state", return_value=None),
        mock.patch("workers.evidence_consumer.emit_transition", new_callable=AsyncMock),
        mock.patch("workers.evidence_consumer.emit_terminal_tombstone", new_callable=AsyncMock),
        mock.patch("workers.evidence_consumer.run_shadow_selflearning", new_callable=AsyncMock),
        mock.patch("workers.evidence_consumer._emit_agentic_mutate_if_any", new_callable=AsyncMock, return_value=False),
        mock.patch("workers.evidence_consumer.send_telegram_out_for_inbound", new_callable=AsyncMock),
        mock.patch("workers.evidence_consumer.store_autonomous_trace_context", new_callable=AsyncMock),
    ):
        mock_batch.return_value = [ev_doc]
        mock_merge.return_value = [ev_doc]
        gate_out = SimpleNamespace(
            hit=False, formatted="", best_score=None,
            match_text_en="", suggested_tool="", detail=None, chunk_ids=[],
        )
        mock_gate.return_value = gate_out
        await reason_from_diagnostic_evidence(ctx, {"data": json.dumps(ev_doc)})

    escalate_msgs = [m for m in tg_captures if "RAG_MISS_SDK_ESCALATE" in m["text"]]
    assert escalate_msgs
    body = escalate_msgs[0]["text"]
    assert "staging" in body, f"Expected namespace 'staging' in body: {body!r}"
    assert "HighMemoryUsage" in body, f"Expected alert name in body: {body!r}"
