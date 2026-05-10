"""P7.3.B — HITL E2E mock test: escalation_reason → omni-hitl-pending pipeline."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import fakeredis.aioredis
import pytest

from pkg.reasoning.analyst_advisory_schema import AnalystAdvisory
from workers.advisory_hitl_compat import AdvisoryHITLCompat


# ---------------------------------------------------------------------------
# Helper — minimal advisory with escalation_reason
# ---------------------------------------------------------------------------

def _escalating_advisory(trace: str = "hitl-test-001") -> AnalystAdvisory:
    return AnalystAdvisory(
        trace_id=trace,
        verdict="INVESTIGATE",
        root_cause="Redis replication lag detected — requires human decision to promote replica",
        confidence="high",
        escalation_reason="Replica promotion is irreversible without human sign-off",
        proposed_remediation=[],
        verification_steps=[],
        forecast={"method": "linear_extrapolation", "basis": "replication lag trend", "forecasts": []},
    )


def _non_escalating_advisory(trace: str = "hitl-test-002") -> AnalystAdvisory:
    return AnalystAdvisory(
        trace_id=trace,
        verdict="INVESTIGATE",
        root_cause="High CPU — recommend investigation",
        confidence="medium",
        escalation_reason="",
        proposed_remediation=[],
        verification_steps=[],
        forecast={"method": "heuristic", "basis": "cpu trend", "forecasts": []},
    )


# ---------------------------------------------------------------------------
# A) validate_hitl_gate respects omni_hitl_routing_enabled
# ---------------------------------------------------------------------------

def test_hitl_gate_blocked_by_default():
    """Default settings (omni_hitl_routing_enabled=False) → gate blocked."""
    settings = SimpleNamespace(omni_hitl_routing_enabled=False)
    ok, reason = AdvisoryHITLCompat.validate_hitl_gate("t1", settings=settings)
    assert not ok
    assert "ADVISORY_MODE_HITL_DISABLED" in reason


def test_hitl_gate_open_when_enabled():
    """omni_hitl_routing_enabled=True → gate open."""
    settings = SimpleNamespace(omni_hitl_routing_enabled=True)
    ok, reason = AdvisoryHITLCompat.validate_hitl_gate("t2", settings=settings)
    assert ok
    assert reason == ""


def test_hitl_gate_blocked_without_settings():
    """Called without settings → blocked (legacy advisory mode)."""
    ok, reason = AdvisoryHITLCompat.validate_hitl_gate("t3")
    assert not ok


# ---------------------------------------------------------------------------
# B) emit_hitl_pending passes settings through gate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_emit_hitl_pending_blocked_when_routing_disabled():
    """omni_hitl_routing_enabled=False → emit_hitl_pending returns without Kafka send."""
    from workers.evidence_mutate_emit import emit_hitl_pending

    kafka = MagicMock()
    kafka.send_dict = AsyncMock()
    ws = SimpleNamespace(
        omni_auto_execute_enabled=False,
        omni_siem_suggest_only=True,
        omni_hitl_routing_enabled=False,
        kafka_topic_hitl_pending="omni-hitl-pending",
        kafka_topic_audit_chain="omni-audit-chain",
    )
    ctx = SimpleNamespace(
        settings=ws,
        kafka=kafka,
        redis=fakeredis.aioredis.FakeRedis(decode_responses=True),
    )
    await emit_hitl_pending(ctx, trace="t-disabled", tool_name="human_escalation", args={})
    kafka.send_dict.assert_not_called()


@pytest.mark.asyncio
async def test_emit_hitl_pending_allowed_when_routing_enabled():
    """omni_hitl_routing_enabled=True → emit_hitl_pending sends to omni-hitl-pending."""
    from workers.evidence_mutate_emit import emit_hitl_pending

    sent: list[dict] = []

    async def _capture(topic, msg, **kwargs):
        sent.append({"topic": topic, "msg": msg})

    kafka = MagicMock()
    kafka.send_dict = AsyncMock(side_effect=_capture)
    ws = SimpleNamespace(
        omni_auto_execute_enabled=False,
        omni_siem_suggest_only=True,
        omni_hitl_routing_enabled=True,
        kafka_topic_hitl_pending="omni-hitl-pending",
        kafka_topic_audit_chain="omni-audit-chain",
    )
    ctx = SimpleNamespace(
        settings=ws,
        kafka=kafka,
        redis=fakeredis.aioredis.FakeRedis(decode_responses=True),
    )
    await emit_hitl_pending(ctx, trace="t-enabled", tool_name="human_escalation", args={})
    hitl_sends = [s for s in sent if s["topic"] == "omni-hitl-pending"]
    assert hitl_sends, f"Expected message on omni-hitl-pending, got: {sent}"


# ---------------------------------------------------------------------------
# C) CRAT block HITL_ESCALATION_EMITTED written BEFORE Kafka send
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_crat_written_before_hitl_kafka_send():
    """
    When HITL routing is enabled and advisory has escalation_reason:
    1. write_audit_block(event_type='HITL_ESCALATION_EMITTED') must be called first
    2. emit_hitl_pending Kafka send happens after
    """
    call_order: list[str] = []

    async def _mock_audit_write(*, event_type, trace_id, payload, redis, kafka, kafka_topic):
        call_order.append(f"audit:{event_type}")
        return {"seq": 1, "block_hash": "abc"}

    async def _mock_emit_hitl(ctx, *, trace, tool_name, args, **kwargs):
        call_order.append(f"hitl:{tool_name}")

    async def _mock_render_telegram(ctx, advisory, chat_id):
        call_order.append("telegram")

    ws = SimpleNamespace(
        omni_hitl_routing_enabled=True,
        omni_auto_execute_enabled=False,
        omni_siem_suggest_only=True,
        model_reasoning_engine="test-model",
        diag_evidence_llm_model="",
        kafka_topic_audit_chain="omni-audit-chain",
        kafka_topic_actions="omni-actions",
        kafka_topic_hitl_pending="omni-hitl-pending",
        telegram_admin_chat_id=12345,
        rag_truth_law_enforced=False,
        trace_correlation_ping_enabled=False,
        omni_llm_first_autonomy_enabled=False,
        omni_unrestricted_tool_execution=False,
        omni_legacy_deterministic_fallback=False,
        omni_planner_precondition_gate_enabled=False,
        omni_shadow_os_mode=False,
        omni_sigma_log_bypass_enabled=False,
        baseline_snapshot_enabled=False,
        autonomous_sigma_observation_window=1,
        baseline_dr_z_threshold=3.0,
        omni_proof_lane_enabled=False,
        rag_evidence_contradiction_check_enabled=False,
        lab_chaos_credential_autofix_enabled=False,
        diag_k8s_expert_rag_enabled=False,
        autonomous_agentic_max_steps=3,
        kafka_topic_diagnostic_evidence="omni-diagnostic-evidence",
    )

    kafka = MagicMock()
    kafka.send_dict = AsyncMock()
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    ctx = SimpleNamespace(
        settings=ws,
        kafka=kafka,
        redis=redis,
        telegram=None,
        llm=MagicMock(),
        vector_store=None,
        scout_ready=MagicMock(is_set=MagicMock(return_value=True)),
        inbound_trace_id="",
    )

    advisory = _escalating_advisory("hitl-crat-order-test")

    with (
        patch("workers.evidence_consumer.write_audit_block", side_effect=_mock_audit_write),
        patch("workers.evidence_consumer.emit_hitl_pending", side_effect=_mock_emit_hitl),
        patch(
            "workers.telegram_advisory_emitter.render_advisory_to_telegram",
            side_effect=_mock_render_telegram,
        ),
        patch("workers.advisory_mode_kill_switch.AdvisoryModeKillSwitch.validate_advisor_output", return_value=(True, "")),
        patch("workers.advisory_mode_kill_switch.AdvisoryModeKillSwitch.validate_execution_gate", return_value=(True, "")),
        patch("workers.advisory_analyst_handler.run_advisory_analyst", new_callable=AsyncMock, return_value=advisory),
        patch("workers.temporal_evidence_collector.fetch_temporal_evidence_for_batch", new_callable=AsyncMock, return_value=""),
        patch("workers.evidence_consumer.append_evidence_and_take_flush_batch", new_callable=AsyncMock) as mock_batch,
        patch("workers.evidence_consumer.merge_preflight_deployment_secret_refs", new_callable=AsyncMock) as mock_merge,
        patch("workers.evidence_consumer.evaluate_rag_gate", new_callable=AsyncMock) as mock_gate,
        patch("workers.evidence_consumer._emit_suggest_remediation", new_callable=AsyncMock),
        patch("workers.evidence_consumer.emit_transition", new_callable=AsyncMock),
        patch("workers.evidence_consumer.compare_alert_claim_to_sdk_state", return_value=None),
        patch("workers.evidence_consumer.send_telegram_out_for_inbound", new_callable=AsyncMock),
        patch("workers.evidence_consumer.store_autonomous_trace_context", new_callable=AsyncMock),
        patch("workers.evidence_consumer.run_shadow_selflearning", new_callable=AsyncMock),
        patch("workers.evidence_consumer._emit_agentic_mutate_if_any", new_callable=AsyncMock, return_value=False),
        patch("workers.evidence_consumer.emit_terminal_tombstone", new_callable=AsyncMock),
        patch("workers.evidence_consumer.emit_telegram_escalation", new_callable=AsyncMock),
    ):
        from workers.evidence_consumer import reason_from_diagnostic_evidence

        ev_doc = {
            "kind": "diagnostic_evidence",
            "trace_id": "hitl-crat-order-test",
            "probe": "k8s_clinical_pod_status",
            "alert_rule": "RedisReplicationLag",
            "alert_hint": "Redis replication lag namespace=prod",
            "canonical_query_snippet": json.dumps({"labels": {"namespace": "prod"}}),
            "extracted_fact": {"lag_ms": 5000},
            "namespace": "prod",
            "deployment": "redis-primary",
            "pod": "",
            "result": "lag_detected",
            "raw": "",
            "ts": "1700000000",
            "evidence_source": "K8s_SDK",
            "symptom_group": "redis_streams_stuck",
            "layer": "infrastructure",
            "clinical_priority_note": "",
        }
        mock_batch.return_value = [ev_doc]
        mock_merge.return_value = [ev_doc]
        gate_out = SimpleNamespace(
            hit=False, formatted="", best_score=None, match_text_en="",
            suggested_tool="", detail=None, chunk_ids=[],
        )
        mock_gate.return_value = gate_out

        await reason_from_diagnostic_evidence(ctx, {"data": json.dumps(ev_doc)})

    # CRAT for ADVISORY_DISPATCHED must come before HITL events
    audit_advisory_idx = next(
        (i for i, c in enumerate(call_order) if c == "audit:ADVISORY_DISPATCHED"), None
    )
    audit_hitl_idx = next(
        (i for i, c in enumerate(call_order) if c == "audit:HITL_ESCALATION_EMITTED"), None
    )
    hitl_idx = next(
        (i for i, c in enumerate(call_order) if c.startswith("hitl:")), None
    )

    assert audit_hitl_idx is not None, f"HITL_ESCALATION_EMITTED audit block not written. call_order={call_order}"
    assert hitl_idx is not None, f"emit_hitl_pending not called. call_order={call_order}"
    assert audit_hitl_idx < hitl_idx, (
        f"CRAT must be written BEFORE Kafka send. call_order={call_order}"
    )


# ---------------------------------------------------------------------------
# D) No escalation_reason → omni-hitl-pending NOT sent
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_escalation_reason_no_hitl():
    """Advisory without escalation_reason must not trigger HITL routing even when enabled."""
    hitl_calls: list = []

    async def _mock_emit_hitl(ctx, *, trace, tool_name, args, **kwargs):
        hitl_calls.append(tool_name)

    ws = SimpleNamespace(
        omni_hitl_routing_enabled=True,
        omni_auto_execute_enabled=False,
        omni_siem_suggest_only=True,
        kafka_topic_audit_chain="omni-audit-chain",
        kafka_topic_actions="omni-actions",
        kafka_topic_hitl_pending="omni-hitl-pending",
        model_reasoning_engine="test-model",
        diag_evidence_llm_model="",
        telegram_admin_chat_id=None,
        rag_truth_law_enforced=False,
        trace_correlation_ping_enabled=False,
        omni_llm_first_autonomy_enabled=False,
        omni_unrestricted_tool_execution=False,
        omni_legacy_deterministic_fallback=False,
        omni_planner_precondition_gate_enabled=False,
        omni_shadow_os_mode=False,
        omni_sigma_log_bypass_enabled=False,
        baseline_snapshot_enabled=False,
        autonomous_sigma_observation_window=1,
        baseline_dr_z_threshold=3.0,
        omni_proof_lane_enabled=False,
        rag_evidence_contradiction_check_enabled=False,
        lab_chaos_credential_autofix_enabled=False,
        diag_k8s_expert_rag_enabled=False,
        autonomous_agentic_max_steps=3,
        kafka_topic_diagnostic_evidence="omni-diagnostic-evidence",
    )
    kafka = MagicMock()
    kafka.send_dict = AsyncMock()
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    ctx = SimpleNamespace(
        settings=ws, kafka=kafka, redis=redis, telegram=None,
        llm=MagicMock(), vector_store=None,
        scout_ready=MagicMock(is_set=MagicMock(return_value=True)),
        inbound_trace_id="",
    )
    advisory = _non_escalating_advisory("hitl-no-reason-test")

    ev_doc = {
        "kind": "diagnostic_evidence", "trace_id": "hitl-no-reason-test",
        "probe": "k8s_clinical_pod_status", "alert_rule": "HighCPU",
        "alert_hint": "cpu high", "canonical_query_snippet": "",
        "extracted_fact": {}, "namespace": "prod", "deployment": "api", "pod": "",
        "result": "high_cpu", "raw": "", "ts": "1700000001",
        "evidence_source": "K8s_SDK", "symptom_group": "", "layer": "",
        "clinical_priority_note": "",
    }

    async def _mock_audit_write(*, event_type, **kwargs):
        return {"seq": 1, "block_hash": "abc"}

    with (
        patch("workers.evidence_consumer.write_audit_block", side_effect=_mock_audit_write),
        patch("workers.evidence_consumer.emit_hitl_pending", side_effect=_mock_emit_hitl),
        patch("workers.advisory_mode_kill_switch.AdvisoryModeKillSwitch.validate_advisor_output", return_value=(True, "")),
        patch("workers.advisory_mode_kill_switch.AdvisoryModeKillSwitch.validate_execution_gate", return_value=(True, "")),
        patch("workers.advisory_analyst_handler.run_advisory_analyst", new_callable=AsyncMock, return_value=advisory),
        patch("workers.temporal_evidence_collector.fetch_temporal_evidence_for_batch", new_callable=AsyncMock, return_value=""),
        patch("workers.evidence_consumer.append_evidence_and_take_flush_batch", new_callable=AsyncMock, return_value=[ev_doc]),
        patch("workers.evidence_consumer.merge_preflight_deployment_secret_refs", new_callable=AsyncMock, return_value=[ev_doc]),
        patch("workers.evidence_consumer.evaluate_rag_gate", new_callable=AsyncMock, return_value=SimpleNamespace(
            hit=False, formatted="", best_score=None, match_text_en="", suggested_tool="", detail=None, chunk_ids=[],
        )),
        patch("workers.evidence_consumer._emit_suggest_remediation", new_callable=AsyncMock),
        patch("workers.evidence_consumer.emit_transition", new_callable=AsyncMock),
        patch("workers.evidence_consumer.compare_alert_claim_to_sdk_state", return_value=None),
        patch("workers.evidence_consumer.send_telegram_out_for_inbound", new_callable=AsyncMock),
        patch("workers.evidence_consumer.store_autonomous_trace_context", new_callable=AsyncMock),
        patch("workers.evidence_consumer.run_shadow_selflearning", new_callable=AsyncMock),
        patch("workers.evidence_consumer._emit_agentic_mutate_if_any", new_callable=AsyncMock, return_value=False),
        patch("workers.evidence_consumer.emit_terminal_tombstone", new_callable=AsyncMock),
        patch("workers.evidence_consumer.emit_telegram_escalation", new_callable=AsyncMock),
        patch("workers.telegram_advisory_emitter.render_advisory_to_telegram", new_callable=AsyncMock),
    ):
        from workers.evidence_consumer import reason_from_diagnostic_evidence
        await reason_from_diagnostic_evidence(ctx, {"data": json.dumps(ev_doc)})

    assert not hitl_calls, (
        f"emit_hitl_pending should NOT be called when escalation_reason is empty, got: {hitl_calls}"
    )
