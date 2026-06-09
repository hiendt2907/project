"""Tests for RAG-miss SDK identity gate: label propagation, identity prefix, and post-parse guardrail."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import pytest

from pkg.reasoning.sanitize import format_sanitized_analyst_user_text
from workers.reasoning_evidence_inbound import (
    _build_identity_prefix,
    _identity_from_batch,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _batch_with_labels(
    namespace: str = "production",
    deployment: str = "api-service",
    pod: str = "",
) -> list[dict]:
    labels: dict = {"alertname": "KubePodCrashLoopBacking", "namespace": namespace, "deployment": deployment}
    if pod:
        labels["pod"] = pod
    return [
        {
            "probe": "k8s_clinical_pod_status",
            "alert_rule": "KubePodCrashLoopBacking",
            "alert_hint": f"CrashLoopBackOff namespace={namespace} deployment={deployment}",
            "canonical_query_snippet": json.dumps({"labels": labels}),
            "extracted_fact": {"phase": "CrashLoopBackOff"},
            "namespace": namespace,
            "deployment": deployment,
            "pod": pod,
        }
    ]


def _batch_no_labels() -> list[dict]:
    return [
        {
            "probe": "k8s_clinical_pod_status",
            "alert_rule": "UnknownAlert",
            "alert_hint": "Some alert with no identity",
            "canonical_query_snippet": "",
            "extracted_fact": {},
            "namespace": "",
            "deployment": "",
            "pod": "",
        }
    ]


def _batch_namespace_only(namespace: str = "staging") -> list[dict]:
    return [
        {
            "probe": "k8s_clinical_pod_status",
            "alert_rule": "HighCPU",
            "alert_hint": f"CPU high namespace={namespace}",
            "canonical_query_snippet": json.dumps({"labels": {"namespace": namespace, "alertname": "HighCPU"}}),
            "extracted_fact": {},
            "namespace": namespace,
            "deployment": "",
            "pod": "",
        }
    ]


# ---------------------------------------------------------------------------
# A) Evidence enrichment: _identity_from_batch extracts from top-level fields
# ---------------------------------------------------------------------------

def test_identity_from_batch_top_level_fields():
    batch = _batch_with_labels("prod", "svc-alpha")
    identity = _identity_from_batch(batch)
    assert identity["namespace"] == "prod"
    assert identity["deployment"] == "svc-alpha"


def test_identity_from_batch_canonical_query_snippet():
    """Falls back to canonical_query_snippet when top-level fields are empty."""
    batch = [
        {
            "probe": "k8s_clinical_pod_status",
            "canonical_query_snippet": json.dumps(
                {"labels": {"namespace": "ns-from-snippet", "deployment": "dep-from-snippet"}}
            ),
            "namespace": "",
            "deployment": "",
            "pod": "",
        }
    ]
    identity = _identity_from_batch(batch)
    assert identity["namespace"] == "ns-from-snippet"
    assert identity["deployment"] == "dep-from-snippet"


def test_identity_from_batch_no_labels():
    identity = _identity_from_batch(_batch_no_labels())
    assert identity == {}


def test_identity_from_batch_pod_in_snippet():
    batch = [
        {
            "probe": "k8s_clinical_pod_status",
            "canonical_query_snippet": json.dumps(
                {"labels": {"namespace": "ns-x", "pod": "my-pod-abc123"}}
            ),
            "namespace": "",
            "deployment": "",
            "pod": "",
        }
    ]
    identity = _identity_from_batch(batch)
    assert identity["namespace"] == "ns-x"
    assert identity["pod"] == "my-pod-abc123"
    assert "deployment" not in identity


# ---------------------------------------------------------------------------
# B) format_sanitized_analyst_user_text includes identity from top-level fields
# ---------------------------------------------------------------------------

def test_format_sanitized_text_has_namespace_from_labels():
    """A minimal alert label set with namespace results in non-empty identity in sanitized_text."""
    ev = {
        "alert_rule": "KubePodCrashLoopBacking",
        "alert_hint": "CrashLoopBackOff namespace=myns deployment=myapp",
        "canonical_query_snippet": json.dumps(
            {"labels": {"namespace": "myns", "deployment": "myapp"}}
        ),
        "extracted_fact": {"phase": "CrashLoopBackOff"},
        "result": "CrashLoopBackOff",
        "probe": "k8s_clinical_pod_status",
    }
    text = format_sanitized_analyst_user_text(ev)
    assert "myns" in text
    assert "labels_or_query_hint" in text


# ---------------------------------------------------------------------------
# C) _build_identity_prefix formats the block correctly
# ---------------------------------------------------------------------------

def test_build_identity_prefix_full():
    prefix = _build_identity_prefix({"namespace": "ns1", "deployment": "dep1", "pod": "pod-x"})
    assert "[AVAILABLE_IDENTITY]" in prefix
    assert "namespace: ns1" in prefix
    assert "deployment: dep1" in prefix
    assert "pod: pod-x" in prefix
    assert "[END_AVAILABLE_IDENTITY]" in prefix


def test_build_identity_prefix_empty():
    assert _build_identity_prefix({}) == ""


def test_build_identity_prefix_namespace_only():
    prefix = _build_identity_prefix({"namespace": "ns-only"})
    assert "namespace: ns-only" in prefix
    assert "deployment" not in prefix
    assert "pod" not in prefix


# ---------------------------------------------------------------------------
# D) Post-parse guardrail: ESCALATE+empty tool+known namespace → scoped suggest
# ---------------------------------------------------------------------------

def _make_ctx(kafka_captures: list, **settings_overrides):
    settings_defaults = {
        "rag_truth_law_enforced": True,
        "trace_correlation_ping_enabled": True,
        "kafka_topic_actions": "omni-actions",
        "kafka_topic_audit_chain": "omni-audit-chain",
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
        "telegram_admin_chat_id": None,
        "kafka_topic_diagnostic_evidence": "omni-diagnostic-evidence",
    }
    settings_defaults.update(settings_overrides)
    settings = SimpleNamespace(**settings_defaults)

    async def _send_dict(topic, msg, **kwargs):
        kafka_captures.append({"topic": topic, "msg": msg})

    kafka = MagicMock()
    kafka.send_dict = AsyncMock(side_effect=_send_dict)

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    scout_ready = MagicMock()
    scout_ready.is_set.return_value = True

    llm = MagicMock()

    ctx = SimpleNamespace(
        settings=settings,
        kafka=kafka,
        redis=redis,
        scout_ready=scout_ready,
        llm=llm,
        telegram=None,
        vector_store=None,
        inbound_trace_id="",
    )
    return ctx


@pytest.mark.asyncio
async def test_escalate_with_namespace_emits_scoped_suggest():
    """When SDK-only LLM returns ESCALATE+empty tool but namespace is known, emit scoped suggest."""
    from workers.evidence_consumer import reason_from_diagnostic_evidence

    kafka_captures: list = []
    # Disable advisory mode: this test targets the traditional planner path (SDK_PARTIAL_IDENTITY_SUGGEST).
    # Advisory mode would short-circuit and never reach the planner.
    ctx = _make_ctx(kafka_captures, omni_siem_suggest_only=False)

    # Build a batch with known namespace
    batch_ns = "known-ns"
    batch_dep = "api-deploy"
    ev_doc = {
        "kind": "diagnostic_evidence",
        "trace_id": "test-escalate-ns-trace",
        "probe": "k8s_clinical_pod_status",
        "alert_rule": "KubePodCrashLoopBacking",
        "alert_hint": f"CrashLoopBackOff namespace={batch_ns} deployment={batch_dep}",
        "canonical_query_snippet": json.dumps({"labels": {"namespace": batch_ns, "deployment": batch_dep}}),
        "extracted_fact": {"phase": "CrashLoopBackOff"},
        "namespace": batch_ns,
        "deployment": batch_dep,
        "pod": "",
        "result": "CrashLoopBackOff",
        "raw": "",
        "ts": "1700000000",
        "evidence_source": "K8s_SDK",
        "symptom_group": "pod_container_state",
        "layer": "workload",
        "clinical_priority_note": "",
    }

    # LLM returns ESCALATE with empty tool
    escalate_llm = (
        'MACHINE_JSON: {"verdict":"ESCALATE","hypothesis":"Missing pod details for diagnosis",'
        '"action":{"tool":"","args":{}}}\n'
        "HUMAN_SUMMARY: Missing pod details; cannot determine root cause."
    )
    ctx.llm.chat = AsyncMock(return_value={"message": {"content": escalate_llm}})

    # Mock out heavy dependencies
    import unittest.mock as mock
    with (
        mock.patch("workers.evidence_consumer.append_evidence_and_take_flush_batch", new_callable=AsyncMock) as mock_batch,
        mock.patch("workers.evidence_consumer.merge_preflight_deployment_secret_refs", new_callable=AsyncMock) as mock_merge,
        mock.patch("workers.evidence_consumer.evaluate_rag_gate", new_callable=AsyncMock) as mock_gate,
        mock.patch("workers.evidence_consumer.compare_alert_claim_to_sdk_state", return_value=None),
        mock.patch("workers.evidence_consumer.emit_transition", new_callable=AsyncMock),
        mock.patch("workers.evidence_consumer.emit_terminal_tombstone", new_callable=AsyncMock),
        mock.patch("workers.evidence_consumer.emit_telegram_escalation", new_callable=AsyncMock),
        mock.patch("workers.evidence_consumer.run_shadow_selflearning", new_callable=AsyncMock),
        mock.patch("workers.evidence_consumer._emit_agentic_mutate_if_any", new_callable=AsyncMock, return_value=False),
        mock.patch("workers.evidence_consumer.send_telegram_out_for_inbound", new_callable=AsyncMock),
        mock.patch("workers.evidence_consumer.store_autonomous_trace_context", new_callable=AsyncMock),
    ):
        mock_batch.return_value = [ev_doc]
        mock_merge.return_value = [ev_doc]
        # Simulate RAG miss
        gate_out = SimpleNamespace(hit=False, formatted="", best_score=None, match_text_en="", suggested_tool="", detail=None, chunk_ids=[])
        mock_gate.return_value = gate_out

        await reason_from_diagnostic_evidence(ctx, {"data": json.dumps(ev_doc)})

    # Must have emitted SDK_PARTIAL_IDENTITY_SUGGEST to omni-actions
    partial_suggest = [
        m for m in kafka_captures
        if m["topic"] == "omni-actions" and "SDK_PARTIAL_IDENTITY_SUGGEST" in json.dumps(m["msg"])
    ]
    assert partial_suggest, (
        "Expected SDK_PARTIAL_IDENTITY_SUGGEST on omni-actions for ESCALATE+namespace, got: "
        + str([m["msg"] for m in kafka_captures])
    )
    body = json.loads(partial_suggest[0]["msg"]["data"])
    diag = body.get("data", {}).get("diagnosis", "") or json.dumps(body)
    assert batch_ns in diag, f"Expected namespace {batch_ns!r} in diagnosis: {diag!r}"


@pytest.mark.asyncio
async def test_escalate_no_identity_no_scoped_suggest():
    """When no identity known, no scoped suggest is emitted (pure escalate stays)."""
    from workers.evidence_consumer import reason_from_diagnostic_evidence

    kafka_captures: list = []
    ctx = _make_ctx(kafka_captures)

    ev_doc = {
        "kind": "diagnostic_evidence",
        "trace_id": "test-escalate-noid-trace",
        "probe": "k8s_clinical_pod_status",
        "alert_rule": "UnknownAlert",
        "alert_hint": "Unknown issue",
        "canonical_query_snippet": "",
        "extracted_fact": {},
        "namespace": "",
        "deployment": "",
        "pod": "",
        "result": "unknown",
        "raw": "",
        "ts": "1700000001",
        "evidence_source": "K8s_SDK",
        "symptom_group": "",
        "layer": "",
        "clinical_priority_note": "",
    }

    escalate_llm = (
        'MACHINE_JSON: {"verdict":"ESCALATE","hypothesis":"No identity available",'
        '"action":{"tool":"","args":{}}}\n'
        "HUMAN_SUMMARY: No identity; cannot diagnose."
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
        mock.patch("workers.evidence_consumer.emit_telegram_escalation", new_callable=AsyncMock),
        mock.patch("workers.evidence_consumer.run_shadow_selflearning", new_callable=AsyncMock),
        mock.patch("workers.evidence_consumer._emit_agentic_mutate_if_any", new_callable=AsyncMock, return_value=False),
        mock.patch("workers.evidence_consumer.send_telegram_out_for_inbound", new_callable=AsyncMock),
        mock.patch("workers.evidence_consumer.store_autonomous_trace_context", new_callable=AsyncMock),
    ):
        mock_batch.return_value = [ev_doc]
        mock_merge.return_value = [ev_doc]
        gate_out = SimpleNamespace(hit=False, formatted="", best_score=None, match_text_en="", suggested_tool="", detail=None, chunk_ids=[])
        mock_gate.return_value = gate_out

        await reason_from_diagnostic_evidence(ctx, {"data": json.dumps(ev_doc)})

    partial_suggest = [
        m for m in kafka_captures
        if m["topic"] == "omni-actions" and "SDK_PARTIAL_IDENTITY_SUGGEST" in json.dumps(m["msg"])
    ]
    assert not partial_suggest, "Should NOT emit partial suggest when no namespace is known"
