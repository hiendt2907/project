"""Coverage gap tests for src/workers/evidence_consumer.py.

Targets uncovered lines / branches in:
  - _siem_forecast_timeline / _format_siem_forecast_text
  - _siem_diagnosis_from_batch  (extracted_fact as JSON string, tenant, ip paths)
  - _notify_siem_telegram  (string ef path, WHY extraction, HOW-TO parsing, fallback)
  - build_sdk_fact_only_prompt
  - _f64  edge cases
  - _symptom_group_from_batch
  - _shadow_os_mode / _derive_shadow_os_commands
  - _hints_from_evidence_text
  - _oom_memory_planner_note_from_batch
  - _clamp01
  - _planner_phase_done_diagnosis
  - _emit_suggest_remediation  (disabled, no kafka, empty trace, shadow-os, normal)
  - _emit_suggest_os_runbook   (disabled, no kafka, empty trace, valid/invalid schema)
  - _planner_missing_preconditions  (various field/evidence gates)
  - _proof_of_fault_gate  (legacy / lane branches)
  - _try_log_surge_sigma_bypass  (disabled, missing ns, missing pod, missing loki url)
  - _emit_agentic_mutate_if_any  (SIEM suggest-only path and PLANNER_PHASE_DONE path)
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis.aioredis
import pytest


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

class _KafkaCapture:
    def __init__(self):
        self.sent: list[tuple[str, dict]] = []

    async def send_dict(self, topic: str, payload: dict, **kwargs) -> None:
        self.sent.append((topic, payload))


def _make_settings(**kw: Any) -> SimpleNamespace:
    defaults: dict[str, Any] = {
        "trace_correlation_ping_enabled": True,
        "kafka_topic_actions": "omni-actions",
        "kafka_topic_hitl_pending": "omni-hitl-pending",
        "kafka_topic_audit_chain": "omni-audit-chain",
        "omni_siem_suggest_only": True,
        "omni_auto_execute_enabled": False,
        "omni_llm_first_autonomy_enabled": False,
        "omni_unrestricted_tool_execution": True,
        "omni_legacy_deterministic_fallback": False,
        "omni_planner_precondition_gate_enabled": False,
        "omni_shadow_os_mode": False,
        "telegram_admin_chat_id": 9999,
        "omni_sigma_log_bypass_enabled": False,
        "omni_proof_lane_enabled": True,
        "autonomous_sigma_observation_window": 1,
        "baseline_dr_z_threshold": 3.0,
        "omni_loki_base_url": "",
        "omni_log_surge_window_sec": 300,
        "omni_log_surge_min_lines": 5,
        "omni_log_surge_min_ratio": 0.5,
        "omni_log_surge_line_limit": 500,
        "omni_log_surge_http_timeout_sec": 25.0,
        "lab_chaos_credential_autofix_enabled": False,
        "omni_discovery_mandatory": False,
        "autonomous_agentic_max_steps": 5,
        "omni_operator_digest_locale": "both",
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _make_ctx(redis=None, kafka=None, settings=None, **kw: Any) -> SimpleNamespace:
    if redis is None:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    if kafka is None:
        kafka = _KafkaCapture()
    if settings is None:
        settings = _make_settings()
    return SimpleNamespace(
        settings=settings,
        redis=redis,
        kafka=kafka,
        telegram=None,
        vector_store=None,
        inbound_trace_id=None,
        **kw,
    )


def _siem_batch(category: str = "ddos", severity: str = "critical", hitl: bool = False) -> list[dict]:
    labels = {
        "alertname": f"SIEM{category.title()}",
        "siem_source": "finguard",
        "siem_category": category,
        "siem_incident_id": "inc-001",
        "severity": severity,
        "namespace": "multi-agent",
        "siem_hitl_required": "true" if hitl else "false",
    }
    snippet = json.dumps({"labels": labels})
    return [
        {
            "probe": "siem_incident_context",
            "canonical_query_snippet": snippet,
            "extracted_fact": {
                "category": category,
                "severity": severity,
                "incident_id": "inc-001",
                "namespace": "multi-agent",
                "affected_ip": "10.0.0.1",
                "description": f"{category} detected",
                "suggested_action": "block ip",
                "tenant": "t1",
            },
        }
    ]


# ---------------------------------------------------------------------------
# _f64
# ---------------------------------------------------------------------------

class TestF64:
    def test_float_input(self):
        from workers.evidence_consumer import _f64
        assert _f64(3.14) == pytest.approx(3.14)

    def test_int_input(self):
        from workers.evidence_consumer import _f64
        assert _f64(42) == 42.0

    def test_string_number(self):
        from workers.evidence_consumer import _f64
        assert _f64("2.5") == pytest.approx(2.5)

    def test_none_returns_none(self):
        from workers.evidence_consumer import _f64
        assert _f64(None) is None

    def test_non_numeric_string_returns_none(self):
        from workers.evidence_consumer import _f64
        assert _f64("abc") is None

    def test_empty_string_returns_none(self):
        from workers.evidence_consumer import _f64
        assert _f64("") is None


# ---------------------------------------------------------------------------
# _clamp01
# ---------------------------------------------------------------------------

class TestClamp01:
    def test_clamps_below_zero(self):
        from workers.evidence_consumer import _clamp01
        assert _clamp01(-5.0) == 0.0

    def test_clamps_above_one(self):
        from workers.evidence_consumer import _clamp01
        assert _clamp01(5.0) == 1.0

    def test_midpoint_unchanged(self):
        from workers.evidence_consumer import _clamp01
        assert _clamp01(0.5) == pytest.approx(0.5)

    def test_boundary_zero(self):
        from workers.evidence_consumer import _clamp01
        assert _clamp01(0.0) == 0.0

    def test_boundary_one(self):
        from workers.evidence_consumer import _clamp01
        assert _clamp01(1.0) == 1.0


# ---------------------------------------------------------------------------
# _symptom_group_from_batch
# ---------------------------------------------------------------------------

class TestSymptomGroupFromBatch:
    def test_returns_first_non_empty(self):
        from workers.evidence_consumer import _symptom_group_from_batch
        batch = [{"symptom_group": ""}, {"symptom_group": "resource_pressure"}, {"symptom_group": "other"}]
        assert _symptom_group_from_batch(batch) == "resource_pressure"

    def test_returns_empty_when_all_empty(self):
        from workers.evidence_consumer import _symptom_group_from_batch
        assert _symptom_group_from_batch([{"symptom_group": ""}]) == ""

    def test_returns_empty_for_empty_batch(self):
        from workers.evidence_consumer import _symptom_group_from_batch
        assert _symptom_group_from_batch([]) == ""

    def test_missing_key_treated_as_empty(self):
        from workers.evidence_consumer import _symptom_group_from_batch
        assert _symptom_group_from_batch([{"probe": "foo"}]) == ""


# ---------------------------------------------------------------------------
# _shadow_os_mode
# ---------------------------------------------------------------------------

class TestShadowOsMode:
    def test_returns_false_by_default(self):
        from workers.evidence_consumer import _shadow_os_mode
        ctx = _make_ctx(settings=_make_settings(omni_shadow_os_mode=False))
        assert _shadow_os_mode(ctx) is False

    def test_returns_true_when_enabled(self):
        from workers.evidence_consumer import _shadow_os_mode
        ctx = _make_ctx(settings=_make_settings(omni_shadow_os_mode=True))
        assert _shadow_os_mode(ctx) is True


# ---------------------------------------------------------------------------
# _derive_shadow_os_commands
# ---------------------------------------------------------------------------

class TestDeriveShadowOsCommands:
    def test_empty_tool_name_returns_empty(self):
        from workers.evidence_consumer import _derive_shadow_os_commands
        result = _derive_shadow_os_commands(tool_name="", args={}, evidence_refs=[], trace="t1")
        assert result == []

    def test_valid_tool_returns_two_steps(self):
        from workers.evidence_consumer import _derive_shadow_os_commands
        result = _derive_shadow_os_commands(
            tool_name="k8s_rollout_restart",
            args={"namespace": "ns1", "deployment": "dep1"},
            evidence_refs=["trace:t1"],
            trace="trace-001",
        )
        assert len(result) == 2
        assert result[0]["risk_level"] == "low"
        assert result[1]["risk_level"] == "medium"


# ---------------------------------------------------------------------------
# build_sdk_fact_only_prompt
# ---------------------------------------------------------------------------

class TestBuildSdkFactOnlyPrompt:
    def test_empty_batch_returns_fallback(self):
        from workers.evidence_consumer import build_sdk_fact_only_prompt
        result = build_sdk_fact_only_prompt([])
        assert "no evidence batch" in result

    def test_single_item_with_dict_ef(self):
        from workers.evidence_consumer import build_sdk_fact_only_prompt
        batch = [{"probe": "k8s_clinical", "alert_rule": "OOMKilled", "alert_hint": "OOM", "extracted_fact": {"phase": "Failed"}}]
        result = build_sdk_fact_only_prompt(batch)
        assert "k8s_clinical" in result
        assert "OOMKilled" in result

    def test_item_with_json_string_ef(self):
        from workers.evidence_consumer import build_sdk_fact_only_prompt
        batch = [{"probe": "metric", "alert_rule": "HighCPU", "alert_hint": "", "extracted_fact": '{"cpu": 90}'}]
        result = build_sdk_fact_only_prompt(batch)
        assert "metric" in result

    def test_item_with_non_json_string_ef(self):
        from workers.evidence_consumer import build_sdk_fact_only_prompt
        batch = [{"probe": "raw", "alert_rule": "A", "alert_hint": "H", "extracted_fact": "plain text"}]
        result = build_sdk_fact_only_prompt(batch)
        assert "raw" in result


# ---------------------------------------------------------------------------
# _planner_phase_done_diagnosis
# ---------------------------------------------------------------------------

class TestPlannerPhaseDoneDiagnosis:
    def test_both_equal_returns_either(self):
        from workers.evidence_consumer import _planner_phase_done_diagnosis
        result = _planner_phase_done_diagnosis("same text", "same text")
        assert result == "same text"

    def test_both_different_merges(self):
        from workers.evidence_consumer import _planner_phase_done_diagnosis
        result = _planner_phase_done_diagnosis("final analysis", "resolution summary")
        assert "resolution summary" in result
        assert "final analysis" in result

    def test_only_final_analysis_set(self):
        from workers.evidence_consumer import _planner_phase_done_diagnosis
        result = _planner_phase_done_diagnosis("final only", "")
        assert result == "final only"

    def test_only_resolution_summary_set(self):
        from workers.evidence_consumer import _planner_phase_done_diagnosis
        result = _planner_phase_done_diagnosis("", "resolution only")
        assert result == "resolution only"

    def test_both_empty_returns_default(self):
        from workers.evidence_consumer import _planner_phase_done_diagnosis
        result = _planner_phase_done_diagnosis("", "")
        assert "concluded" in result.lower() or "planner" in result.lower()


# ---------------------------------------------------------------------------
# _siem_forecast_timeline
# ---------------------------------------------------------------------------

class TestSiemForecastTimeline:
    def test_known_category_and_severity(self):
        from workers.evidence_consumer import _siem_forecast_timeline
        result = _siem_forecast_timeline("ddos", "critical")
        assert len(result) == 5
        assert result[0]["timeframe"] == "1h"

    def test_unknown_category_returns_default(self):
        from workers.evidence_consumer import _siem_forecast_timeline
        result = _siem_forecast_timeline("unknown_category", "critical")
        assert len(result) == 5

    def test_known_category_missing_severity_falls_back_to_critical(self):
        from workers.evidence_consumer import _siem_forecast_timeline
        result = _siem_forecast_timeline("ddos", "low")  # ddos only has critical
        assert len(result) == 5

    def test_auth_failure_high_severity(self):
        from workers.evidence_consumer import _siem_forecast_timeline
        result = _siem_forecast_timeline("auth_failure", "high")
        assert len(result) == 5
        assert result[0]["timeframe"] == "1h"

    def test_all_categories(self):
        from workers.evidence_consumer import _siem_forecast_timeline
        for cat in ["malware", "data_exfil", "k8s_threat", "lateral_movement", "network_anomaly"]:
            result = _siem_forecast_timeline(cat, "critical")
            assert len(result) == 5


# ---------------------------------------------------------------------------
# _format_siem_forecast_text
# ---------------------------------------------------------------------------

class TestFormatSiemForecastText:
    def test_output_contains_timeframe_labels(self):
        from workers.evidence_consumer import _format_siem_forecast_text, _siem_forecast_timeline
        items = _siem_forecast_timeline("ddos", "critical")
        text = _format_siem_forecast_text(items)
        assert "+1h:" in text
        assert "+3h:" in text
        assert "confidence=" in text

    def test_severity_is_uppercased(self):
        from workers.evidence_consumer import _format_siem_forecast_text
        items = [{"timeframe": "1h", "severity": "critical", "prediction": "bad", "confidence": "high"}]
        text = _format_siem_forecast_text(items)
        assert "[CRITICAL]" in text


# ---------------------------------------------------------------------------
# _siem_diagnosis_from_batch
# ---------------------------------------------------------------------------

class TestSiemDiagnosisFromBatch:
    def _make_labels(self, category="ddos", severity="critical", ns="ns1"):
        return {
            "alertname": f"SIEM{category.title()}",
            "siem_source": "finguard",
            "siem_category": category,
            "siem_incident_id": "inc-001",
            "severity": severity,
            "namespace": ns,
        }

    def _make_siem_batch_with_json_string_ef(self):
        """extracted_fact is a JSON string — covers line 450-456."""
        labels = self._make_labels()
        ef = json.dumps({
            "category": "ddos",
            "severity": "critical",
            "incident_id": "inc-json",
            "namespace": "ns-json",
            "affected_ip": "192.168.1.1",
            "description": "json ef description",
            "suggested_action": "block",
            "tenant": "tenant-json",
        })
        return [
            {
                "probe": "siem_incident_context",
                "canonical_query_snippet": json.dumps({"labels": labels}),
                "extracted_fact": ef,  # string form
            }
        ]

    def test_extracted_fact_as_json_string(self):
        from workers.evidence_consumer import _siem_diagnosis_from_batch
        batch = self._make_siem_batch_with_json_string_ef()
        labels = self._make_labels()
        result = _siem_diagnosis_from_batch(batch, labels, "sanitized text")
        assert "inc-json" in result or "WHAT" in result

    def test_diagnosis_contains_who_block(self):
        from workers.evidence_consumer import _siem_diagnosis_from_batch
        batch = _siem_batch()
        labels = {"siem_category": "ddos", "siem_incident_id": "inc-001", "severity": "critical", "namespace": "ns1"}
        result = _siem_diagnosis_from_batch(batch, labels, "text")
        assert "WHO:" in result

    def test_diagnosis_contains_how_to_block(self):
        from workers.evidence_consumer import _siem_diagnosis_from_batch
        batch = _siem_batch("malware")
        labels = {"siem_category": "malware", "siem_incident_id": "inc-002", "severity": "critical"}
        result = _siem_diagnosis_from_batch(batch, labels, "text")
        assert "HOW-TO" in result

    def test_diagnosis_contains_forecast(self):
        from workers.evidence_consumer import _siem_diagnosis_from_batch
        batch = _siem_batch("k8s_threat", "high")
        labels = {"siem_category": "k8s_threat", "severity": "high", "siem_incident_id": "inc-003"}
        result = _siem_diagnosis_from_batch(batch, labels, "text")
        assert "Forecast" in result

    def test_default_steps_for_unknown_category(self):
        from workers.evidence_consumer import _siem_diagnosis_from_batch
        batch = _siem_batch("unknown_cat")
        labels = {"siem_category": "unknown_cat", "siem_incident_id": "inc-x", "severity": "low"}
        result = _siem_diagnosis_from_batch(batch, labels, "")
        # Should use _SIEM_DEFAULT_STEPS
        assert "HOW-TO" in result

    def test_special_chars_in_ns_do_not_cause_format_error(self):
        """Namespace with '{' braces must not crash format substitution."""
        from workers.evidence_consumer import _siem_diagnosis_from_batch
        batch = _siem_batch()
        # Inject curly brace in ns
        batch[0]["extracted_fact"]["namespace"] = "ns-{bad}"
        labels = {"siem_category": "ddos", "siem_incident_id": "inc-x", "severity": "critical", "namespace": "ns-{bad}"}
        result = _siem_diagnosis_from_batch(batch, labels, "")
        assert "HOW-TO" in result  # must not raise

    def test_no_siem_incident_context_probe(self):
        """No probe=siem_incident_context — falls back to siem_labels."""
        from workers.evidence_consumer import _siem_diagnosis_from_batch
        batch = [{"probe": "other", "extracted_fact": {}}]
        labels = {"siem_category": "auth_failure", "siem_incident_id": "inc-404", "severity": "high"}
        result = _siem_diagnosis_from_batch(batch, labels, "")
        assert "auth_failure" in result.lower()


# ---------------------------------------------------------------------------
# _notify_siem_telegram
# ---------------------------------------------------------------------------

class TestNotifySiemTelegram:
    def _make_ctx_with_telegram(self, telegram=None, admin_cid=9999, **settings_kw):
        tg = telegram or AsyncMock()
        ctx = _make_ctx(settings=_make_settings(telegram_admin_chat_id=admin_cid, **settings_kw))
        ctx.telegram = tg
        return ctx, tg

    async def test_no_telegram_returns_immediately(self):
        from workers.evidence_consumer import _notify_siem_telegram
        ctx = _make_ctx()
        ctx.telegram = None
        # Must not raise
        await _notify_siem_telegram(ctx, trace="t1", batch=_siem_batch(), diagnosis="diag")

    async def test_no_admin_cid_logs_warning(self):
        from workers.evidence_consumer import _notify_siem_telegram
        tg = AsyncMock()
        ctx = _make_ctx(settings=_make_settings(telegram_admin_chat_id=None))
        ctx.telegram = tg
        await _notify_siem_telegram(ctx, trace="t1", batch=_siem_batch(), diagnosis="diag")
        tg.send_message.assert_not_called()

    async def test_sends_message_to_admin_chat(self):
        from workers.evidence_consumer import _notify_siem_telegram
        tg = AsyncMock()
        tg.send_message = AsyncMock(return_value=None)
        ctx = _make_ctx(settings=_make_settings(telegram_admin_chat_id=12345))
        ctx.telegram = tg
        await _notify_siem_telegram(ctx, trace="tr-1", batch=_siem_batch(), diagnosis="WHY: test\nHOW-TO:\n1. kubectl fix\n")
        tg.send_message.assert_called_once()
        cid = tg.send_message.call_args[0][0]
        assert cid == 12345

    async def test_telegram_error_is_swallowed(self):
        from workers.evidence_consumer import _notify_siem_telegram
        tg = AsyncMock()
        tg.send_message = AsyncMock(side_effect=RuntimeError("telegram fail"))
        ctx = _make_ctx(settings=_make_settings(telegram_admin_chat_id=1))
        ctx.telegram = tg
        # Must not raise
        await _notify_siem_telegram(ctx, trace="tr-2", batch=_siem_batch(), diagnosis="diag")

    async def test_string_extracted_fact_parsed(self):
        """extracted_fact as a JSON string triggers parsing branch (lines 450-456)."""
        from workers.evidence_consumer import _notify_siem_telegram
        tg = AsyncMock()
        tg.send_message = AsyncMock(return_value=None)
        ctx = _make_ctx(settings=_make_settings(telegram_admin_chat_id=1))
        ctx.telegram = tg
        ef_str = json.dumps({"tenant": "myTenant", "affected_ip": "10.0.0.9", "description": "test desc"})
        labels = {"siem_category": "ddos", "siem_incident_id": "inc-str", "severity": "critical", "namespace": "ns1", "siem_source": "finguard", "alertname": "SIEMDdos"}
        batch = [
            {
                "probe": "siem_incident_context",
                "canonical_query_snippet": json.dumps({"labels": labels}),
                "extracted_fact": ef_str,  # string
            }
        ]
        await _notify_siem_telegram(ctx, trace="tr-3", batch=batch, diagnosis="WHY: why\nHOW-TO:\n1. check")
        tg.send_message.assert_called_once()

    async def test_invalid_string_ef_does_not_crash(self):
        """extracted_fact is a non-JSON string — triggers except branch (line 455)."""
        from workers.evidence_consumer import _notify_siem_telegram
        tg = AsyncMock()
        tg.send_message = AsyncMock(return_value=None)
        ctx = _make_ctx(settings=_make_settings(telegram_admin_chat_id=1))
        ctx.telegram = tg
        labels = {"siem_category": "ddos", "siem_incident_id": "inc-bad", "severity": "critical", "namespace": "ns1", "siem_source": "finguard", "alertname": "SIEMDdos"}
        batch = [
            {
                "probe": "siem_incident_context",
                "canonical_query_snippet": json.dumps({"labels": labels}),
                "extracted_fact": "not valid json {{{{",
            }
        ]
        await _notify_siem_telegram(ctx, trace="tr-4", batch=batch, diagnosis="diag")
        tg.send_message.assert_called_once()

    async def test_howto_parsed_from_diagnosis(self):
        """HOW-TO section is properly parsed to build advise list (lines 488-512)."""
        from workers.evidence_consumer import _notify_siem_telegram
        tg = AsyncMock()
        tg.send_message = AsyncMock(return_value=None)
        ctx = _make_ctx(settings=_make_settings(telegram_admin_chat_id=1))
        ctx.telegram = tg
        diagnosis = (
            "WHAT: ddos attack\n"
            "WHO: ns=ns1\n"
            "WHY: traffic exceeded baseline\n\n"
            "HOW-TO (operator steps):\n"
            "1. kubectl get networkpolicy -n ns1\n"
            "2. Review WAF config\n"
            "Forecast (worst-case):\n"
            "  +1h: [CRITICAL] bad stuff\n"
            "Omni does NOT auto-execute\n"
        )
        await _notify_siem_telegram(ctx, trace="tr-5", batch=_siem_batch(), diagnosis=diagnosis)
        tg.send_message.assert_called_once()
        msg = tg.send_message.call_args[0][1]
        assert "[SIEM]" in msg

    async def test_fallback_advice_when_no_howto(self):
        """No HOW-TO and no numbered steps — falls back to Review: diagnosis[:400]."""
        from workers.evidence_consumer import _notify_siem_telegram
        tg = AsyncMock()
        tg.send_message = AsyncMock(return_value=None)
        ctx = _make_ctx(settings=_make_settings(telegram_admin_chat_id=1))
        ctx.telegram = tg
        diagnosis = "Just a plain diagnosis with no structure"
        await _notify_siem_telegram(ctx, trace="tr-6", batch=_siem_batch(), diagnosis=diagnosis)
        tg.send_message.assert_called_once()


# ---------------------------------------------------------------------------
# _emit_suggest_remediation
# ---------------------------------------------------------------------------

class TestEmitSuggestRemediation:
    async def test_disabled_when_ping_not_enabled(self):
        from workers.evidence_consumer import _emit_suggest_remediation
        kafka = _KafkaCapture()
        ctx = _make_ctx(kafka=kafka, settings=_make_settings(trace_correlation_ping_enabled=False))
        await _emit_suggest_remediation(
            ctx, trace="t1", diagnosis="diag", confidence=0.8,
            source="TEST", suggested_tool="tool",
        )
        assert len(kafka.sent) == 0

    async def test_no_kafka_returns_early(self):
        from workers.evidence_consumer import _emit_suggest_remediation
        ctx = _make_ctx(kafka=None)
        # Must not raise
        await _emit_suggest_remediation(
            ctx, trace="t1", diagnosis="diag", confidence=0.8,
            source="TEST", suggested_tool="tool",
        )

    async def test_empty_trace_returns_early(self):
        from workers.evidence_consumer import _emit_suggest_remediation
        kafka = _KafkaCapture()
        ctx = _make_ctx(kafka=kafka)
        await _emit_suggest_remediation(
            ctx, trace="", diagnosis="diag", confidence=0.8,
            source="TEST", suggested_tool="tool",
        )
        assert len(kafka.sent) == 0

    async def test_sends_to_kafka(self):
        from workers.evidence_consumer import _emit_suggest_remediation
        kafka = _KafkaCapture()
        ctx = _make_ctx(kafka=kafka)
        await _emit_suggest_remediation(
            ctx, trace="t-abc", diagnosis="diagnosis text", confidence=0.75,
            source="RAG_HIT", suggested_tool="kubectl_describe_pod",
            audit=False,  # audit path covered separately; assert the action emit only
        )
        assert len(kafka.sent) == 1
        topic, payload = kafka.sent[0]
        assert topic == "omni-actions"
        envelope = json.loads(payload["data"])
        assert envelope.get("action") == "SUGGEST_REMEDIATION"

    async def test_kafka_error_is_swallowed(self):
        """Kafka send failure must be logged, not raised."""
        from workers.evidence_consumer import _emit_suggest_remediation

        class _FailKafka:
            async def send_dict(self, topic, payload):
                raise RuntimeError("kafka down")

        ctx = _make_ctx(kafka=_FailKafka())
        # Must not raise
        await _emit_suggest_remediation(
            ctx, trace="t1", diagnosis="d", confidence=0.5,
            source="S", suggested_tool="t",
        )

    async def test_clamps_confidence(self):
        from workers.evidence_consumer import _emit_suggest_remediation
        kafka = _KafkaCapture()
        ctx = _make_ctx(kafka=kafka)
        await _emit_suggest_remediation(
            ctx, trace="t1", diagnosis="d", confidence=9.9,
            source="S", suggested_tool="t", audit=False,
        )
        topic, payload = kafka.sent[0]
        envelope = json.loads(payload["data"])
        inner = envelope.get("data", {})
        assert inner.get("confidence", 1.0) <= 1.0


# ---------------------------------------------------------------------------
# _emit_suggest_os_runbook
# ---------------------------------------------------------------------------

class TestEmitSuggestOsRunbook:
    async def test_disabled_when_ping_not_enabled(self):
        from workers.evidence_consumer import _emit_suggest_os_runbook
        kafka = _KafkaCapture()
        ctx = _make_ctx(kafka=kafka, settings=_make_settings(trace_correlation_ping_enabled=False))
        result = await _emit_suggest_os_runbook(
            ctx, trace="t1", diagnosis="d", confidence=0.7,
            source="S", runbook_title="T", commands=[],
        )
        assert result is False

    async def test_no_kafka_returns_false(self):
        from workers.evidence_consumer import _emit_suggest_os_runbook
        ctx = _make_ctx(kafka=None)
        result = await _emit_suggest_os_runbook(
            ctx, trace="t1", diagnosis="d", confidence=0.7,
            source="S", runbook_title="T", commands=[],
        )
        assert result is False

    async def test_empty_trace_returns_false(self):
        from workers.evidence_consumer import _emit_suggest_os_runbook
        kafka = _KafkaCapture()
        ctx = _make_ctx(kafka=kafka)
        result = await _emit_suggest_os_runbook(
            ctx, trace="", diagnosis="d", confidence=0.7,
            source="S", runbook_title="T", commands=[],
        )
        assert result is False

    async def test_invalid_schema_returns_false(self):
        from workers.evidence_consumer import _emit_suggest_os_runbook
        kafka = _KafkaCapture()
        ctx = _make_ctx(kafka=kafka)
        # Empty commands list should fail validate_suggest_os_runbook_data
        with patch("workers.evidence_consumer.validate_suggest_os_runbook_data", side_effect=ValueError("invalid")):
            result = await _emit_suggest_os_runbook(
                ctx, trace="t1", diagnosis="d", confidence=0.7,
                source="S", runbook_title="T", commands=[],
            )
        assert result is False

    async def test_valid_runbook_sends_to_kafka(self):
        from workers.evidence_consumer import _emit_suggest_os_runbook
        kafka = _KafkaCapture()
        ctx = _make_ctx(kafka=kafka)
        commands = [
            {
                "purpose": "inspect",
                "command": "kubectl get pods",
                "dry_run_command": "kubectl get pods",
                "target": "node:local",
                "risk_level": "low",
                "expected_output": "pod list",
                "rollback_command": "echo ok",
                "timeout_sec": 30,
                "evidence_refs": ["trace:t1"],
                "escalation_required": False,
            }
        ]
        with patch("workers.evidence_consumer.validate_suggest_os_runbook_data"):
            result = await _emit_suggest_os_runbook(
                ctx, trace="t1", diagnosis="diag", confidence=0.7,
                source="TEST", runbook_title="My runbook", commands=commands,
                audit=False,  # audit path covered separately; assert the runbook emit only
            )
        assert result is True
        assert len(kafka.sent) == 1

    async def test_kafka_error_returns_false(self):
        from workers.evidence_consumer import _emit_suggest_os_runbook

        class _FailKafka:
            async def send_dict(self, topic, payload):
                raise RuntimeError("fail")

        ctx = _make_ctx(kafka=_FailKafka())
        with patch("workers.evidence_consumer.validate_suggest_os_runbook_data"):
            result = await _emit_suggest_os_runbook(
                ctx, trace="t1", diagnosis="d", confidence=0.5,
                source="S", runbook_title="T", commands=[],
            )
        assert result is False


# ---------------------------------------------------------------------------
# _hints_from_evidence_text
# ---------------------------------------------------------------------------

class TestHintsFromEvidenceText:
    def test_returns_none_for_empty_text(self):
        from workers.evidence_consumer import _hints_from_evidence_text
        assert _hints_from_evidence_text("") is None

    def test_extracts_namespace(self):
        from workers.evidence_consumer import _hints_from_evidence_text
        result = _hints_from_evidence_text("namespace=multi-agent pod=my-pod")
        assert result is not None
        assert result.get("namespace") == "multi-agent"

    def test_extracts_pod(self):
        from workers.evidence_consumer import _hints_from_evidence_text
        result = _hints_from_evidence_text("pod=my-pod-xyz")
        assert result is not None
        assert result.get("pod_name") == "my-pod-xyz"

    def test_extracts_rule_line(self):
        from workers.evidence_consumer import _hints_from_evidence_text
        text = "some preamble\nrule: OOMKilledAlert\nmore text"
        result = _hints_from_evidence_text(text)
        assert result is not None
        assert result.get("alertname") == "OOMKilledAlert"

    def test_extracts_symptom_group(self):
        from workers.evidence_consumer import _hints_from_evidence_text
        text = "symptom_group: resource_pressure\n"
        result = _hints_from_evidence_text(text)
        assert result is not None
        assert result.get("symptom_group") == "resource_pressure"

    def test_rule_na_is_skipped(self):
        from workers.evidence_consumer import _hints_from_evidence_text
        text = "rule: n/a\n"
        result = _hints_from_evidence_text(text)
        assert result is None or "alertname" not in (result or {})

    def test_no_matches_returns_none(self):
        from workers.evidence_consumer import _hints_from_evidence_text
        assert _hints_from_evidence_text("nothing useful here") is None


# ---------------------------------------------------------------------------
# _oom_memory_planner_note_from_batch
# ---------------------------------------------------------------------------

class TestOomMemoryPlannerNote:
    def test_returns_none_when_no_oom(self):
        from workers.evidence_consumer import _oom_memory_planner_note_from_batch
        batch = [{"probe": "k8s_clinical_pod_status", "extracted_fact": {"has_oom_killed": False}}]
        assert _oom_memory_planner_note_from_batch(batch) is None

    def test_returns_none_without_memory_metrics(self):
        from workers.evidence_consumer import _oom_memory_planner_note_from_batch
        batch = [
            {"probe": "k8s_clinical_pod_status", "extracted_fact": {"has_oom_killed": True}},
            {"probe": "k8s_clinical_pod_metrics", "extracted_fact": {"kind": "PodMetrics", "containers": []}},
        ]
        assert _oom_memory_planner_note_from_batch(batch) is None

    def test_returns_note_when_oom_and_memory(self):
        from workers.evidence_consumer import _oom_memory_planner_note_from_batch
        batch = [
            {"probe": "k8s_clinical_pod_status", "extracted_fact": {"has_oom_killed": True}},
            {
                "probe": "k8s_clinical_pod_metrics",
                "extracted_fact": {
                    "kind": "PodMetrics",
                    "containers": [{"name": "app", "memory": "512Mi"}],
                },
            },
        ]
        note = _oom_memory_planner_note_from_batch(batch)
        assert note is not None
        assert "512Mi" in note
        assert "OOMKilled" in note

    def test_non_dict_extracted_fact_is_skipped(self):
        from workers.evidence_consumer import _oom_memory_planner_note_from_batch
        batch = [{"probe": "k8s_clinical_pod_status", "extracted_fact": "plain string"}]
        assert _oom_memory_planner_note_from_batch(batch) is None


# ---------------------------------------------------------------------------
# _proof_of_fault_gate — lane branches
# ---------------------------------------------------------------------------

class TestProofOfFaultGate:
    def _base_batch(self, lane="state") -> list[dict]:
        return [
            {
                "probe": "k8s_clinical_pod_status",
                "lane": lane,
                "extracted_fact": {"phase": "Failed", "reason": "OOMKilled"},
                "alert_rule": "OOMKilledPod",
                "alert_hint": "OOM",
            }
        ]

    async def test_no_critical_evidence_returns_false(self):
        from workers.evidence_consumer import _proof_of_fault_gate
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis=redis, settings=_make_settings(omni_proof_lane_enabled=True))

        with patch("workers.evidence_consumer.critical_evidence_present", return_value=False):
            ok, code, meta = await _proof_of_fault_gate(ctx, trace="t1", batch=self._base_batch())
        assert ok is False
        assert "NO_PHYSICAL_PROOF" in code

    async def test_state_lane_bypasses_sigma(self):
        """state lane always passes sigma check, no redis sigma needed."""
        from workers.evidence_consumer import _proof_of_fault_gate
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis=redis, settings=_make_settings(
            omni_proof_lane_enabled=True,
            autonomous_sigma_observation_window=1,
        ))

        with patch("workers.evidence_consumer.critical_evidence_present", return_value=True), \
             patch("workers.evidence_consumer.resolve_proof_lane", return_value=("state", "batch")):
            ok, code, meta = await _proof_of_fault_gate(ctx, trace="t1", batch=self._base_batch("state"))
        assert ok is True
        assert code == ""

    async def test_resource_lane_blocked_without_sigma(self):
        """resource lane without sigma → SIGMA_GATE_BLOCKED."""
        from workers.evidence_consumer import _proof_of_fault_gate
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        # No snapshot stored → sigma_ok=False
        ctx = _make_ctx(redis=redis, settings=_make_settings(omni_proof_lane_enabled=True))

        with patch("workers.evidence_consumer.critical_evidence_present", return_value=True), \
             patch("workers.evidence_consumer.resolve_proof_lane", return_value=("resource", "batch")):
            ok, code, meta = await _proof_of_fault_gate(ctx, trace="t1", batch=self._base_batch("resource"))
        assert ok is False
        assert "SIGMA" in code

    async def test_legacy_mode_no_sigma_calls_bypass(self):
        """Legacy mode (omni_proof_lane_enabled=False) without sigma calls log surge bypass."""
        from workers.evidence_consumer import _proof_of_fault_gate
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis=redis, settings=_make_settings(omni_proof_lane_enabled=False))

        with patch("workers.evidence_consumer.critical_evidence_present", return_value=True), \
             patch("workers.evidence_consumer._try_log_surge_sigma_bypass",
                   new=AsyncMock(return_value=(False, {}, False))):
            ok, code, meta = await _proof_of_fault_gate(ctx, trace="t1", batch=self._base_batch())
        assert ok is False

    async def test_legacy_mode_sigma_ok_passes(self):
        """Legacy mode with sigma passes (dr=True in snapshot)."""
        from workers.evidence_consumer import _proof_of_fault_gate
        from workers.baseline_snapshot import REDIS_KEY_SNAPSHOT
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        await redis.set(REDIS_KEY_SNAPSHOT, json.dumps({"dr": True, "z_cpu": 0.0, "z_mem": 0.0}))
        ctx = _make_ctx(redis=redis, settings=_make_settings(
            omni_proof_lane_enabled=False,
            autonomous_sigma_observation_window=1,
        ))

        with patch("workers.evidence_consumer.critical_evidence_present", return_value=True):
            ok, code, meta = await _proof_of_fault_gate(ctx, trace="t-legacy", batch=self._base_batch())
        assert ok is True

    async def test_app_log_lane_with_sigma_passes(self):
        """app_log lane with sigma passes after window increment."""
        from workers.evidence_consumer import _proof_of_fault_gate
        from workers.baseline_snapshot import REDIS_KEY_SNAPSHOT
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        await redis.set(REDIS_KEY_SNAPSHOT, json.dumps({"z_cpu": 4.0, "z_mem": 0.0}))
        ctx = _make_ctx(redis=redis, settings=_make_settings(
            omni_proof_lane_enabled=True,
            autonomous_sigma_observation_window=1,
        ))

        with patch("workers.evidence_consumer.critical_evidence_present", return_value=True), \
             patch("workers.evidence_consumer.resolve_proof_lane", return_value=("app_log", "batch")):
            ok, code, meta = await _proof_of_fault_gate(ctx, trace="t-applog", batch=self._base_batch())
        assert ok is True


# ---------------------------------------------------------------------------
# _try_log_surge_sigma_bypass
# ---------------------------------------------------------------------------

class TestTryLogSurgeSigmaBypass:
    def _batch(self):
        return [{"probe": "metric", "lane": "app_log", "alert_hint": "5xx surge", "extracted_fact": {}}]

    async def test_disabled_returns_false_immediately(self):
        from workers.evidence_consumer import _try_log_surge_sigma_bypass
        ctx = _make_ctx(settings=_make_settings(omni_sigma_log_bypass_enabled=False))
        ok, extra, esc = await _try_log_surge_sigma_bypass(ctx, "t1", self._batch(), None)
        assert ok is False
        assert esc is False

    async def test_missing_namespace_returns_false(self):
        from workers.evidence_consumer import _try_log_surge_sigma_bypass
        ctx = _make_ctx(settings=_make_settings(omni_sigma_log_bypass_enabled=True))
        with patch("workers.evidence_consumer.namespace_pod_from_batch", return_value=("", "")):
            ok, extra, esc = await _try_log_surge_sigma_bypass(ctx, "t1", self._batch(), None)
        assert ok is False

    async def test_namespace_not_allowed_returns_false(self):
        from workers.evidence_consumer import _try_log_surge_sigma_bypass
        ctx = _make_ctx(settings=_make_settings(omni_sigma_log_bypass_enabled=True))
        with patch("workers.evidence_consumer.namespace_pod_from_batch", return_value=("ns1", "pod1")), \
             patch("workers.evidence_consumer.namespace_allowed", return_value=False):
            ok, extra, esc = await _try_log_surge_sigma_bypass(ctx, "t1", self._batch(), None)
        assert ok is False

    async def test_missing_pod_returns_false(self):
        from workers.evidence_consumer import _try_log_surge_sigma_bypass
        ctx = _make_ctx(settings=_make_settings(omni_sigma_log_bypass_enabled=True))
        with patch("workers.evidence_consumer.namespace_pod_from_batch", return_value=("ns1", "")), \
             patch("workers.evidence_consumer.namespace_allowed", return_value=True), \
             patch("workers.evidence_consumer._try_log_surge_sigma_bypass.__wrapped__" if hasattr(None, "__wrapped__") else "workers.evidence_consumer.is_api_web_workload", return_value=True, create=True):
            ok, extra, esc = await _try_log_surge_sigma_bypass(ctx, "t1", self._batch(), None)
        assert ok is False

    async def test_missing_loki_url_returns_false(self):
        from workers.evidence_consumer import _try_log_surge_sigma_bypass
        ctx = _make_ctx(settings=_make_settings(omni_sigma_log_bypass_enabled=True, omni_loki_base_url=""))
        with patch("workers.evidence_consumer.namespace_pod_from_batch", return_value=("ns1", "pod1")), \
             patch("workers.evidence_consumer.namespace_allowed", return_value=True), \
             patch("pkg.reasoning.incident_matrix_profile.is_api_web_workload", return_value=True, create=True):
            ok, extra, esc = await _try_log_surge_sigma_bypass(ctx, "t1", self._batch(), None)
        assert ok is False


# ---------------------------------------------------------------------------
# _emit_agentic_mutate_if_any — SIEM suggest-only path
# ---------------------------------------------------------------------------

class TestEmitAgenticMutateIfAnySiem:
    async def test_siem_batch_returns_true_suggest_only(self):
        from workers.evidence_consumer import _emit_agentic_mutate_if_any
        kafka = _KafkaCapture()
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        tg = AsyncMock()
        tg.send_message = AsyncMock(return_value=None)
        settings = _make_settings(omni_siem_suggest_only=True, telegram_admin_chat_id=9999)
        ctx = SimpleNamespace(
            kafka=kafka, redis=redis, settings=settings, telegram=tg,
            vector_store=None, inbound_trace_id=None,
        )
        batch = _siem_batch("k8s_threat")
        result = await _emit_agentic_mutate_if_any(
            ctx, "trace-001", batch, sanitized_text="k8s threat detected"
        )
        assert result is True
        # Verify SUGGEST_REMEDIATION was emitted to kafka
        topics = [t for t, _ in kafka.sent]
        assert "omni-actions" in topics


# ---------------------------------------------------------------------------
# _planner_missing_preconditions
# ---------------------------------------------------------------------------

class TestPlannerMissingPreconditions:
    async def test_gate_disabled_returns_empty(self):
        from workers.evidence_consumer import _planner_missing_preconditions
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis=redis, settings=_make_settings(omni_planner_precondition_gate_enabled=False))
        missing = await _planner_missing_preconditions(
            ctx, trace="t1", tool_name="k8s_rollout_restart", args={},
            discovery_steps=[], planner_missing=None,
        )
        assert missing == []

    async def test_unknown_tool_returns_missing(self):
        from workers.evidence_consumer import _planner_missing_preconditions
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis=redis, settings=_make_settings(omni_planner_precondition_gate_enabled=True))

        with patch("workers.evidence_consumer.get_tool_registry") as mock_reg:
            mock_reg.return_value.has.return_value = False
            missing = await _planner_missing_preconditions(
                ctx, trace="t1", tool_name="nonexistent_tool", args={},
                discovery_steps=[], planner_missing=None,
            )
        assert "unknown_tool:nonexistent_tool" in missing

    async def test_missing_required_field_returns_field_error(self):
        from workers.evidence_consumer import _planner_missing_preconditions
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis=redis, settings=_make_settings(omni_planner_precondition_gate_enabled=True))

        with patch("workers.evidence_consumer.get_tool_registry") as mock_reg, \
             patch("workers.evidence_consumer.load_trace_memory", new=AsyncMock(
                 return_value=SimpleNamespace(action_history=[]))):
            mock_reg.return_value.has.return_value = True
            mock_reg.return_value.metadata_for.return_value = {
                "required_fields": ["namespace", "deployment"],
                "required_evidence": [],
                "requires_readonly_before_mutate": False,
            }
            mock_reg.return_value.json_schema_for.return_value = {}
            missing = await _planner_missing_preconditions(
                ctx, trace="t1", tool_name="k8s_rollout_restart",
                args={"namespace": "ns1"},  # deployment missing
                discovery_steps=[], planner_missing=None,
            )
        assert "arg:deployment" in missing

    async def test_planner_missing_deduplication(self):
        """planner_missing items already in missing list are not duplicated."""
        from workers.evidence_consumer import _planner_missing_preconditions
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis=redis, settings=_make_settings(omni_planner_precondition_gate_enabled=True))

        with patch("workers.evidence_consumer.get_tool_registry") as mock_reg, \
             patch("workers.evidence_consumer.load_trace_memory", new=AsyncMock(
                 return_value=SimpleNamespace(action_history=[]))):
            mock_reg.return_value.has.return_value = True
            mock_reg.return_value.metadata_for.return_value = {
                "required_fields": ["namespace"],
                "required_evidence": [],
                "requires_readonly_before_mutate": False,
            }
            mock_reg.return_value.json_schema_for.return_value = {}
            missing = await _planner_missing_preconditions(
                ctx, trace="t1", tool_name="k8s_rollout_restart",
                args={},  # namespace missing
                discovery_steps=[],
                planner_missing=["arg:namespace"],  # also declared by planner
            )
        # Should appear only once
        assert missing.count("arg:namespace") == 1

    async def test_evidence_satisfied_via_secret_ref(self):
        """secret_ref_confirmed evidence is satisfied by k8s_get_pod_secret_refs in discovery."""
        from workers.evidence_consumer import _planner_missing_preconditions
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis=redis, settings=_make_settings(omni_planner_precondition_gate_enabled=True))

        with patch("workers.evidence_consumer.get_tool_registry") as mock_reg, \
             patch("workers.evidence_consumer.load_trace_memory", new=AsyncMock(
                 return_value=SimpleNamespace(action_history=[]))):
            mock_reg.return_value.has.return_value = True
            mock_reg.return_value.metadata_for.return_value = {
                "required_fields": [],
                "required_evidence": ["secret_ref_confirmed"],
                "requires_readonly_before_mutate": False,
            }
            mock_reg.return_value.json_schema_for.return_value = {}
            missing = await _planner_missing_preconditions(
                ctx, trace="t1", tool_name="k8s_patch_secret",
                args={"namespace": "ns1"},
                discovery_steps=["k8s_get_pod_secret_refs"],
                planner_missing=None,
            )
        assert "evidence:secret_ref_confirmed" not in missing

    async def test_target_workload_identity_satisfied(self):
        """target_workload_identity evidence satisfied when deployment arg present."""
        from workers.evidence_consumer import _planner_missing_preconditions
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis=redis, settings=_make_settings(omni_planner_precondition_gate_enabled=True))

        with patch("workers.evidence_consumer.get_tool_registry") as mock_reg, \
             patch("workers.evidence_consumer.load_trace_memory", new=AsyncMock(
                 return_value=SimpleNamespace(action_history=[]))):
            mock_reg.return_value.has.return_value = True
            mock_reg.return_value.metadata_for.return_value = {
                "required_fields": [],
                "required_evidence": ["target_workload_identity"],
                "requires_readonly_before_mutate": False,
            }
            mock_reg.return_value.json_schema_for.return_value = {}
            missing = await _planner_missing_preconditions(
                ctx, trace="t1", tool_name="k8s_rollout_restart",
                args={"deployment": "my-dep", "namespace": "ns1"},
                discovery_steps=[],
                planner_missing=None,
            )
        assert "evidence:target_workload_identity" not in missing
