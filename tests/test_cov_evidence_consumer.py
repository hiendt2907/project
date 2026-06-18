"""Coverage tests for src/workers/evidence_consumer.py — additional branches.

Targets uncovered lines/branches not covered by test_cov_evidence_consumer_gaps.py:
  - _notify_siem_telegram: HOW-TO parsing variants, fallback advise, tenant handling
  - _try_log_surge_sigma_bypass: loki call paths, ok/fail/escalate returns
  - _proof_of_fault_gate: resource lane with sigma, app_log without sigma (bypass)
  - _emit_agentic_mutate_if_any: PLANNER_PHASE_DONE path
  - _planner_missing_preconditions: credential_source_of_truth, patch_target_confirmed,
      rbac_drift_signal, readonly_before_mutate gate, planner_missing dedup edge cases
  - _hints_from_evidence_batch: oom note, matrix row injection
  - build_sdk_fact_only_prompt: non-JSON string ef, missing keys
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis.aioredis
import pytest

os.environ.setdefault("OMNI_KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
os.environ.setdefault("OMNI_REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("OMNI_OLLAMA_BASE_URL", "http://localhost:11434")


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


def _siem_batch(category: str = "ddos", severity: str = "critical") -> list[dict]:
    labels = {
        "alertname": f"SIEM{category.title()}",
        "siem_source": "finguard",
        "siem_category": category,
        "siem_incident_id": "inc-001",
        "severity": severity,
        "namespace": "multi-agent",
    }
    return [
        {
            "probe": "siem_incident_context",
            "canonical_query_snippet": json.dumps({"labels": labels}),
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
# _notify_siem_telegram — additional branches
# ---------------------------------------------------------------------------


class TestNotifySiemTelegramAdditional:
    """Cover extra branches in _notify_siem_telegram that are not yet covered."""

    async def test_tenant_unknown_is_excluded(self):
        """extracted_fact with tenant='unknown' — tenant must not appear in problem."""
        from workers.evidence_consumer import _notify_siem_telegram

        tg = AsyncMock()
        tg.send_message = AsyncMock(return_value=None)
        ctx = _make_ctx(settings=_make_settings(telegram_admin_chat_id=1))
        ctx.telegram = tg
        # tenant is 'unknown' → should not be shown
        batch = [
            {
                "probe": "siem_incident_context",
                "canonical_query_snippet": json.dumps({"labels": {
                    "siem_source": "finguard", "siem_category": "ddos",
                    "siem_incident_id": "inc-1", "severity": "critical",
                    "namespace": "ns1", "alertname": "SIEMDdos",
                }}),
                "extracted_fact": {"tenant": "unknown", "affected_ip": "", "description": "ddos"},
            }
        ]
        await _notify_siem_telegram(ctx, trace="t1", batch=batch, diagnosis="WHY: test\n")
        tg.send_message.assert_called_once()
        msg = tg.send_message.call_args[0][1]
        assert "unknown" not in msg or "tenant=unknown" not in msg

    async def test_no_affected_ip_skips_ip_in_problem(self):
        """No affected_ip in extracted_fact → ip= not in problem string."""
        from workers.evidence_consumer import _notify_siem_telegram

        tg = AsyncMock()
        tg.send_message = AsyncMock(return_value=None)
        ctx = _make_ctx(settings=_make_settings(telegram_admin_chat_id=1))
        ctx.telegram = tg
        batch = [
            {
                "probe": "siem_incident_context",
                "canonical_query_snippet": json.dumps({"labels": {
                    "siem_source": "finguard", "siem_category": "ddos",
                    "siem_incident_id": "inc-2", "severity": "critical",
                    "namespace": "ns1", "alertname": "SIEMDdos",
                }}),
                "extracted_fact": {"affected_ip": "", "description": "no ip test", "tenant": ""},
            }
        ]
        await _notify_siem_telegram(ctx, trace="t2", batch=batch, diagnosis="plain diag without WHY")
        tg.send_message.assert_called_once()

    async def test_fallback_advise_from_raw_numbered_lines(self):
        """No HOW-TO section but numbered lines exist → fallback parse."""
        from workers.evidence_consumer import _notify_siem_telegram

        tg = AsyncMock()
        tg.send_message = AsyncMock(return_value=None)
        ctx = _make_ctx(settings=_make_settings(telegram_admin_chat_id=1))
        ctx.telegram = tg
        # No HOW-TO section but has numbered lines starting at top level
        diagnosis = "1. kubectl get pods -n ns1\n2. kubectl describe pod x\n"
        await _notify_siem_telegram(ctx, trace="t3", batch=_siem_batch(), diagnosis=diagnosis)
        tg.send_message.assert_called_once()
        msg = tg.send_message.call_args[0][1]
        assert "[SIEM]" in msg

    async def test_suggested_action_appended_to_advise(self):
        """siem labels with suggested_action → appended to advise list."""
        from workers.evidence_consumer import _notify_siem_telegram

        tg = AsyncMock()
        tg.send_message = AsyncMock(return_value=None)
        ctx = _make_ctx(settings=_make_settings(telegram_admin_chat_id=1))
        ctx.telegram = tg
        labels = {
            "siem_source": "finguard", "siem_category": "ddos",
            "siem_incident_id": "inc-3", "severity": "critical",
            "namespace": "ns1", "alertname": "SIEMDdos",
            "suggested_action": "block malicious ip",
        }
        batch = [{"probe": "siem_incident_context",
                  "canonical_query_snippet": json.dumps({"labels": labels}),
                  "extracted_fact": {"affected_ip": "1.2.3.4"}}]
        await _notify_siem_telegram(ctx, trace="t4", batch=batch,
                                     diagnosis="HOW-TO:\n1. kubectl check\nForecast:\n+1h bad")
        tg.send_message.assert_called_once()

    async def test_why_text_from_structured_diagnosis(self):
        """WHY: line extracted as reason for Telegram card."""
        from workers.evidence_consumer import _notify_siem_telegram

        tg = AsyncMock()
        tg.send_message = AsyncMock(return_value=None)
        ctx = _make_ctx(settings=_make_settings(telegram_admin_chat_id=1))
        ctx.telegram = tg
        diagnosis = "WHAT: ddos\nWHO: ns=ns1\nWHY: traffic exceeded threshold\n\nHOW-TO:\n1. kubectl get pods"
        await _notify_siem_telegram(ctx, trace="t5", batch=_siem_batch(), diagnosis=diagnosis)
        tg.send_message.assert_called_once()

    async def test_probe_chain_items_built(self):
        """Multiple batch items with probe/lane/hint → chain items built."""
        from workers.evidence_consumer import _notify_siem_telegram

        tg = AsyncMock()
        tg.send_message = AsyncMock(return_value=None)
        ctx = _make_ctx(settings=_make_settings(telegram_admin_chat_id=1))
        ctx.telegram = tg
        batch = _siem_batch()
        # Add extra items for chain building
        batch.extend([
            {"probe": "k8s_clinical", "lane": "state", "alert_hint": "pod fail", "ts": "2026-01-01T00:00:00"},
            {"probe": "metric_probe", "evidence_source": "prometheus", "result": "high cpu"},
        ])
        await _notify_siem_telegram(ctx, trace="t6", batch=batch, diagnosis="WHY: test\n")
        tg.send_message.assert_called_once()


# ---------------------------------------------------------------------------
# _try_log_surge_sigma_bypass — Loki call paths
# ---------------------------------------------------------------------------


class TestTryLogSurgeSigmaBypassLoki:
    """Cover branches when Loki is reachable (ok / fail / escalate)."""

    async def test_loki_ok_returns_bypass(self):
        """Loki returns ok=True → (True, meta, False)."""
        from workers.evidence_consumer import _try_log_surge_sigma_bypass

        ctx = _make_ctx(settings=_make_settings(
            omni_sigma_log_bypass_enabled=True,
            omni_loki_base_url="http://loki:3100",
        ))
        batch = [{"probe": "k8s_clinical", "lane": "app_log", "extracted_fact": {}}]
        loki_result = SimpleNamespace(
            ok=True,
            reason="5xx_surge",
            dominant_error_class="server_error",
            escalate_log_unavailable=False,
            meta={"lines_fetched": 100},
        )
        with patch("workers.evidence_consumer.namespace_pod_from_batch", return_value=("ns1", "pod1")), \
             patch("workers.evidence_consumer.namespace_allowed", return_value=True), \
             patch("workers.evidence_consumer.evaluate_log_surge_sigma_bypass", new=AsyncMock(return_value=loki_result)):
            from pkg.reasoning.incident_matrix_profile import is_api_web_workload
            with patch("pkg.reasoning.incident_matrix_profile.is_api_web_workload", return_value=True):
                ok, extra, esc = await _try_log_surge_sigma_bypass(ctx, "t1", batch, None)
        assert ok is True
        assert esc is False
        assert extra.get("log_surge_bypass") is True

    async def test_loki_fail_escalate_log_unavailable(self):
        """Loki returns escalate_log_unavailable=True → (False, extra, True)."""
        from workers.evidence_consumer import _try_log_surge_sigma_bypass

        ctx = _make_ctx(settings=_make_settings(
            omni_sigma_log_bypass_enabled=True,
            omni_loki_base_url="http://loki:3100",
        ))
        batch = [{"probe": "k8s_clinical", "lane": "app_log"}]
        loki_result = SimpleNamespace(
            ok=False,
            reason="loki_unavailable",
            dominant_error_class="",
            escalate_log_unavailable=True,
            meta={"lines_fetched": 0},
        )
        with patch("workers.evidence_consumer.namespace_pod_from_batch", return_value=("ns1", "pod1")), \
             patch("workers.evidence_consumer.namespace_allowed", return_value=True), \
             patch("workers.evidence_consumer.evaluate_log_surge_sigma_bypass", new=AsyncMock(return_value=loki_result)):
            from pkg.reasoning.incident_matrix_profile import is_api_web_workload
            with patch("pkg.reasoning.incident_matrix_profile.is_api_web_workload", return_value=True):
                ok, extra, esc = await _try_log_surge_sigma_bypass(ctx, "t1", batch, None)
        assert ok is False
        assert esc is True

    async def test_loki_fail_client_abort_informational(self):
        """client_abort class is informational — ok=False, esc=False."""
        from workers.evidence_consumer import _try_log_surge_sigma_bypass

        ctx = _make_ctx(settings=_make_settings(
            omni_sigma_log_bypass_enabled=True,
            omni_loki_base_url="http://loki:3100",
        ))
        batch = [{"probe": "metric", "lane": "app_log"}]
        loki_result = SimpleNamespace(
            ok=False,
            reason="client_abort",
            dominant_error_class="client_abort",
            escalate_log_unavailable=False,
            meta={"lines_fetched": 5},
        )
        with patch("workers.evidence_consumer.namespace_pod_from_batch", return_value=("ns1", "pod1")), \
             patch("workers.evidence_consumer.namespace_allowed", return_value=True), \
             patch("workers.evidence_consumer.evaluate_log_surge_sigma_bypass", new=AsyncMock(return_value=loki_result)):
            with patch("pkg.reasoning.incident_matrix_profile.is_api_web_workload", return_value=True):
                ok, extra, esc = await _try_log_surge_sigma_bypass(ctx, "t1", batch, None)
        assert ok is False
        assert esc is False

    async def test_not_api_web_workload_returns_false(self):
        """is_api_web_workload=False → early return False."""
        from workers.evidence_consumer import _try_log_surge_sigma_bypass

        ctx = _make_ctx(settings=_make_settings(
            omni_sigma_log_bypass_enabled=True,
            omni_loki_base_url="http://loki:3100",
        ))
        batch = [{"probe": "metric", "lane": "resource"}]
        with patch("workers.evidence_consumer.namespace_pod_from_batch", return_value=("ns1", "pod1")), \
             patch("workers.evidence_consumer.namespace_allowed", return_value=True), \
             patch("pkg.reasoning.incident_matrix_profile.is_api_web_workload", return_value=False):
            ok, extra, esc = await _try_log_surge_sigma_bypass(ctx, "t1", batch, None)
        assert ok is False
        assert esc is False


# ---------------------------------------------------------------------------
# _proof_of_fault_gate — resource lane with sigma
# ---------------------------------------------------------------------------


class TestProofOfFaultGateResourceLane:
    """Cover resource lane with sigma (window counter increments)."""

    async def _base_batch(self) -> list[dict]:
        return [
            {
                "probe": "k8s_clinical_pod_status",
                "lane": "resource",
                "extracted_fact": {"phase": "Failed", "cpu_throttle": 90},
                "alert_rule": "HighCPU",
                "alert_hint": "cpu spike",
            }
        ]

    async def test_meta_self_alert_hard_closes_gate(self):
        """Plan step 6: trace flagged meta_self → gate returns ERR_META_SELF_NO_TARGET."""
        from workers.evidence_consumer import _proof_of_fault_gate

        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        await redis.setex(
            "omni:trace:t-meta:alert_class",
            3600,
            json.dumps({"kind": "meta_self", "mutate_eligible": False}),
        )
        ctx = _make_ctx(redis=redis, settings=_make_settings(omni_proof_lane_enabled=True))
        # Even with critical evidence present, meta_self short-circuits to blocked.
        with patch("workers.evidence_consumer.critical_evidence_present", return_value=True):
            ok, code, meta = await _proof_of_fault_gate(
                ctx, trace="t-meta", batch=await self._base_batch()
            )
        assert ok is False
        assert code == "ERR_META_SELF_NO_TARGET"
        assert meta.get("alert_class") == "meta_self"

    async def test_resource_lane_with_sigma_passes_after_window(self):
        """resource lane + sigma_ok=True + window>=needed → True."""
        from workers.evidence_consumer import _proof_of_fault_gate
        from workers.baseline_snapshot import REDIS_KEY_SNAPSHOT

        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        # Seed sigma snapshot
        await redis.set(REDIS_KEY_SNAPSHOT, json.dumps({"dr": False, "z_cpu": 4.0, "z_mem": 0.0}))
        ctx = _make_ctx(redis=redis, settings=_make_settings(
            omni_proof_lane_enabled=True,
            autonomous_sigma_observation_window=1,
        ))
        with patch("workers.evidence_consumer.critical_evidence_present", return_value=True), \
             patch("workers.evidence_consumer.resolve_proof_lane", return_value=("resource", "batch")):
            ok, code, meta = await _proof_of_fault_gate(
                ctx, trace="t-res-ok", batch=await self._base_batch()
            )
        assert ok is True
        assert code == ""

    async def test_app_log_lane_no_sigma_calls_bypass_not_ok(self):
        """app_log lane + sigma_ok=False + bypass not ok → SIGMA_GATE_BLOCKED."""
        from workers.evidence_consumer import _proof_of_fault_gate

        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        # No snapshot → sigma_ok=False
        ctx = _make_ctx(redis=redis, settings=_make_settings(
            omni_proof_lane_enabled=True,
            autonomous_sigma_observation_window=1,
            omni_sigma_log_bypass_enabled=False,
        ))
        with patch("workers.evidence_consumer.critical_evidence_present", return_value=True), \
             patch("workers.evidence_consumer.resolve_proof_lane", return_value=("app_log", "batch")), \
             patch("workers.evidence_consumer._try_log_surge_sigma_bypass",
                   new=AsyncMock(return_value=(False, {}, False))):
            ok, code, meta = await _proof_of_fault_gate(
                ctx, trace="t-applog-nok", batch=await self._base_batch()
            )
        assert ok is False
        assert "SIGMA" in code

    async def test_app_log_lane_no_sigma_with_bypass_escalate(self):
        """app_log + sigma_ok=False + bypass esc=True → LOG_SOURCE_UNAVAILABLE."""
        from workers.evidence_consumer import _proof_of_fault_gate
        from pkg.reasoning.reason_codes import ERR_REA_LOG_SOURCE_UNAVAILABLE

        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis=redis, settings=_make_settings(
            omni_proof_lane_enabled=True,
            autonomous_sigma_observation_window=1,
            omni_sigma_log_bypass_enabled=False,
        ))
        with patch("workers.evidence_consumer.critical_evidence_present", return_value=True), \
             patch("workers.evidence_consumer.resolve_proof_lane", return_value=("app_log", "batch")), \
             patch("workers.evidence_consumer._try_log_surge_sigma_bypass",
                   new=AsyncMock(return_value=(False, {"reason": "unavail"}, True))):
            ok, code, meta = await _proof_of_fault_gate(
                ctx, trace="t-applog-esc", batch=await self._base_batch()
            )
        assert ok is False
        assert code == ERR_REA_LOG_SOURCE_UNAVAILABLE

    async def test_resource_lane_window_not_met(self):
        """resource lane + sigma_ok=True but window_needed=2 and first tick → SIGMA_GATE_BLOCKED."""
        from workers.evidence_consumer import _proof_of_fault_gate
        from workers.baseline_snapshot import REDIS_KEY_SNAPSHOT

        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        await redis.set(REDIS_KEY_SNAPSHOT, json.dumps({"z_cpu": 5.0, "z_mem": 0.0}))
        ctx = _make_ctx(redis=redis, settings=_make_settings(
            omni_proof_lane_enabled=True,
            autonomous_sigma_observation_window=2,  # need 2 ticks
        ))
        with patch("workers.evidence_consumer.critical_evidence_present", return_value=True), \
             patch("workers.evidence_consumer.resolve_proof_lane", return_value=("resource", "batch")):
            ok, code, meta = await _proof_of_fault_gate(
                ctx, trace="t-res-window", batch=await self._base_batch()
            )
        assert ok is False
        assert "SIGMA" in code

    async def test_legacy_mode_sigma_ok_with_window(self):
        """Legacy mode (proof_lane disabled) + sigma_ok=True → window increments → pass."""
        from workers.evidence_consumer import _proof_of_fault_gate
        from workers.baseline_snapshot import REDIS_KEY_SNAPSHOT

        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        await redis.set(REDIS_KEY_SNAPSHOT, json.dumps({"z_cpu": 4.0}))
        ctx = _make_ctx(redis=redis, settings=_make_settings(
            omni_proof_lane_enabled=False,
            autonomous_sigma_observation_window=1,
        ))
        with patch("workers.evidence_consumer.critical_evidence_present", return_value=True), \
             patch("workers.evidence_consumer.resolve_proof_lane", return_value=("resource", "batch")):
            ok, code, meta = await _proof_of_fault_gate(
                ctx, trace="t-legacy-sigma", batch=await self._base_batch()
            )
        assert ok is True

    async def test_legacy_mode_sigma_ok_window_not_met_clears_key(self):
        """Legacy + sigma_ok=True but needed=5 → first tick fails → SIGMA_GATE_BLOCKED."""
        from workers.evidence_consumer import _proof_of_fault_gate
        from workers.baseline_snapshot import REDIS_KEY_SNAPSHOT

        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        await redis.set(REDIS_KEY_SNAPSHOT, json.dumps({"z_cpu": 4.0}))
        ctx = _make_ctx(redis=redis, settings=_make_settings(
            omni_proof_lane_enabled=False,
            autonomous_sigma_observation_window=5,
        ))
        with patch("workers.evidence_consumer.critical_evidence_present", return_value=True), \
             patch("workers.evidence_consumer.resolve_proof_lane", return_value=("resource", "batch")):
            ok, code, meta = await _proof_of_fault_gate(
                ctx, trace="t-legacy-win5", batch=await self._base_batch()
            )
        assert ok is False
        assert "SIGMA" in code

    async def test_legacy_not_sigma_not_bypass_returns_sigma_blocked(self):
        """Legacy + sigma_ok=False + bypass returns False, esc=False → SIGMA_GATE_BLOCKED."""
        from workers.evidence_consumer import _proof_of_fault_gate

        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis=redis, settings=_make_settings(
            omni_proof_lane_enabled=False,
            omni_sigma_log_bypass_enabled=False,
        ))
        with patch("workers.evidence_consumer.critical_evidence_present", return_value=True), \
             patch("workers.evidence_consumer.resolve_proof_lane", return_value=("app_log", "batch")), \
             patch("workers.evidence_consumer._try_log_surge_sigma_bypass",
                   new=AsyncMock(return_value=(False, {}, False))):
            ok, code, meta = await _proof_of_fault_gate(
                ctx, trace="t-legacy-nosigma", batch=await self._base_batch()
            )
        assert ok is False


# ---------------------------------------------------------------------------
# _emit_agentic_mutate_if_any — PLANNER_PHASE_DONE path
# ---------------------------------------------------------------------------


class TestEmitAgenticMutateIfAnyPlannerDone:
    """Cover PLANNER_PHASE_DONE branch — emits SUGGEST_REMEDIATION, returns False."""

    async def test_planner_phase_done_emits_suggest_returns_false(self):
        from workers.evidence_consumer import _emit_agentic_mutate_if_any
        from pkg.reasoning.reason_codes import PLANNER_PHASE_DONE

        kafka = _KafkaCapture()
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        settings = _make_settings(
            omni_siem_suggest_only=False,
            omni_unrestricted_tool_execution=True,
            omni_llm_first_autonomy_enabled=False,
            omni_legacy_deterministic_fallback=False,
        )
        ctx = SimpleNamespace(
            kafka=kafka, redis=redis, settings=settings, telegram=None,
            vector_store=None, inbound_trace_id=None,
        )
        batch = [{"probe": "k8s_clinical", "lane": "state",
                  "extracted_fact": {"phase": "Failed"}, "alert_rule": "OOMKilled"}]
        done_plan = {
            "reason_code": PLANNER_PHASE_DONE,
            "final_analysis": "System recovered",
            "resolution_summary": "Memory limit increased",
            "reasoning_chain": {"verdict": "SUGGEST_FIX", "lane": "state", "thought_process": ["memory issue"]},
            "discovery_steps": [],
        }
        with patch("workers.evidence_consumer.infer_blind_proof_lane_hint", new=AsyncMock(return_value=None)), \
             patch("workers.evidence_consumer.initial_symptom_from_evidence_batch", return_value=None), \
             patch("workers.evidence_consumer.run_agentic_mutate_plan", new=AsyncMock(return_value=done_plan)), \
             patch("workers.evidence_consumer.recall_playbook_advisory", new=AsyncMock(return_value=None)), \
             patch("workers.evidence_consumer.emit_transition", new=AsyncMock()):
            result = await _emit_agentic_mutate_if_any(
                ctx, "trace-done", batch,
                sanitized_text="oom memory issue", rag_match_text=None,
            )
        assert result is False
        # SUGGEST_REMEDIATION should have been emitted
        topics = [t for t, _ in kafka.sent]
        assert "omni-actions" in topics


# ---------------------------------------------------------------------------
# _planner_missing_preconditions — additional evidence tags
# ---------------------------------------------------------------------------


class TestPlannerMissingPreconditionsExtra:
    async def _make_ctx_for_gate(self, **settings_kw):
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        ctx = _make_ctx(redis=redis, settings=_make_settings(
            omni_planner_precondition_gate_enabled=True, **settings_kw
        ))
        return ctx

    async def test_credential_source_of_truth_satisfied(self):
        """credential_source_of_truth evidence satisfied when value_source + value_source_ref set."""
        from workers.evidence_consumer import _planner_missing_preconditions

        ctx = await self._make_ctx_for_gate()
        with patch("workers.evidence_consumer.get_tool_registry") as mock_reg, \
             patch("workers.evidence_consumer.load_trace_memory", new=AsyncMock(
                 return_value=SimpleNamespace(action_history=[]))):
            mock_reg.return_value.has.return_value = True
            mock_reg.return_value.metadata_for.return_value = {
                "required_fields": [],
                "required_evidence": ["credential_source_of_truth"],
                "requires_readonly_before_mutate": False,
            }
            mock_reg.return_value.json_schema_for.return_value = {}
            missing = await _planner_missing_preconditions(
                ctx, trace="t1", tool_name="k8s_patch_secret",
                args={"namespace": "ns1", "value_source": "vault", "value_source_ref": "secret/db"},
                discovery_steps=[], planner_missing=None,
            )
        assert "evidence:credential_source_of_truth" not in missing

    async def test_credential_source_of_truth_missing(self):
        """credential_source_of_truth evidence not satisfied when value_source missing."""
        from workers.evidence_consumer import _planner_missing_preconditions

        ctx = await self._make_ctx_for_gate()
        with patch("workers.evidence_consumer.get_tool_registry") as mock_reg, \
             patch("workers.evidence_consumer.load_trace_memory", new=AsyncMock(
                 return_value=SimpleNamespace(action_history=[]))):
            mock_reg.return_value.has.return_value = True
            mock_reg.return_value.metadata_for.return_value = {
                "required_fields": [],
                "required_evidence": ["credential_source_of_truth"],
                "requires_readonly_before_mutate": False,
            }
            mock_reg.return_value.json_schema_for.return_value = {}
            missing = await _planner_missing_preconditions(
                ctx, trace="t2", tool_name="k8s_patch_secret",
                args={"namespace": "ns1"},  # no value_source
                discovery_steps=[], planner_missing=None,
            )
        assert "evidence:credential_source_of_truth" in missing

    async def test_patch_target_confirmed_satisfied_via_discovery(self):
        """patch_target_confirmed satisfied when discovery_steps provided."""
        from workers.evidence_consumer import _planner_missing_preconditions

        ctx = await self._make_ctx_for_gate()
        with patch("workers.evidence_consumer.get_tool_registry") as mock_reg, \
             patch("workers.evidence_consumer.load_trace_memory", new=AsyncMock(
                 return_value=SimpleNamespace(action_history=[]))):
            mock_reg.return_value.has.return_value = True
            mock_reg.return_value.metadata_for.return_value = {
                "required_fields": [],
                "required_evidence": ["patch_target_confirmed"],
                "requires_readonly_before_mutate": False,
            }
            mock_reg.return_value.json_schema_for.return_value = {}
            missing = await _planner_missing_preconditions(
                ctx, trace="t3", tool_name="k8s_patch_resource",
                args={"namespace": "ns1"},
                discovery_steps=["k8s_describe_deployment"],  # has discovery
                planner_missing=None,
            )
        assert "evidence:patch_target_confirmed" not in missing

    async def test_rbac_drift_signal_satisfied_via_readonly_actions(self):
        """rbac_drift_signal satisfied via readonly_ok_actions in memory."""
        from workers.evidence_consumer import _planner_missing_preconditions

        ctx = await self._make_ctx_for_gate()
        readonly_action = SimpleNamespace(kind="readonly_executed", is_error=False,
                                          tool_name="k8s_get_rbac")
        with patch("workers.evidence_consumer.get_tool_registry") as mock_reg, \
             patch("workers.evidence_consumer.load_trace_memory", new=AsyncMock(
                 return_value=SimpleNamespace(action_history=[readonly_action]))):
            mock_reg.return_value.has.return_value = True
            mock_reg.return_value.metadata_for.return_value = {
                "required_fields": [],
                "required_evidence": ["rbac_drift_signal"],
                "requires_readonly_before_mutate": False,
            }
            mock_reg.return_value.json_schema_for.return_value = {}
            missing = await _planner_missing_preconditions(
                ctx, trace="t4", tool_name="k8s_delete_clusterrolebinding",
                args={"name": "bad-crb"},
                discovery_steps=[],
                planner_missing=None,
            )
        assert "evidence:rbac_drift_signal" not in missing

    async def test_requires_readonly_before_mutate_missing(self):
        """requires_readonly_before_mutate=True and no discovery → readonly_discovery_evidence."""
        from workers.evidence_consumer import _planner_missing_preconditions

        ctx = await self._make_ctx_for_gate()
        with patch("workers.evidence_consumer.get_tool_registry") as mock_reg, \
             patch("workers.evidence_consumer.load_trace_memory", new=AsyncMock(
                 return_value=SimpleNamespace(action_history=[]))):
            mock_reg.return_value.has.return_value = True
            mock_reg.return_value.metadata_for.return_value = {
                "required_fields": [],
                "required_evidence": [],
                "requires_readonly_before_mutate": True,
            }
            mock_reg.return_value.json_schema_for.return_value = {}
            missing = await _planner_missing_preconditions(
                ctx, trace="t5", tool_name="k8s_rollout_restart",
                args={"namespace": "ns1", "deployment": "dep1"},
                discovery_steps=[],  # no discovery
                planner_missing=None,
            )
        assert "readonly_discovery_evidence" in missing

    async def test_requires_readonly_before_mutate_satisfied(self):
        """requires_readonly_before_mutate=True but has discovery_steps → not missing."""
        from workers.evidence_consumer import _planner_missing_preconditions

        ctx = await self._make_ctx_for_gate()
        with patch("workers.evidence_consumer.get_tool_registry") as mock_reg, \
             patch("workers.evidence_consumer.load_trace_memory", new=AsyncMock(
                 return_value=SimpleNamespace(action_history=[]))):
            mock_reg.return_value.has.return_value = True
            mock_reg.return_value.metadata_for.return_value = {
                "required_fields": [],
                "required_evidence": [],
                "requires_readonly_before_mutate": True,
            }
            mock_reg.return_value.json_schema_for.return_value = {}
            missing = await _planner_missing_preconditions(
                ctx, trace="t6", tool_name="k8s_rollout_restart",
                args={"namespace": "ns1", "deployment": "dep1"},
                discovery_steps=["k8s_describe_pod"],  # has discovery
                planner_missing=None,
            )
        assert "readonly_discovery_evidence" not in missing

    async def test_planner_missing_arg_satisfied_in_args(self):
        """planner_missing arg:namespace but args has namespace → not added again."""
        from workers.evidence_consumer import _planner_missing_preconditions

        ctx = await self._make_ctx_for_gate()
        with patch("workers.evidence_consumer.get_tool_registry") as mock_reg, \
             patch("workers.evidence_consumer.load_trace_memory", new=AsyncMock(
                 return_value=SimpleNamespace(action_history=[]))):
            mock_reg.return_value.has.return_value = True
            mock_reg.return_value.metadata_for.return_value = {
                "required_fields": [],
                "required_evidence": [],
                "requires_readonly_before_mutate": False,
            }
            mock_reg.return_value.json_schema_for.return_value = {}
            missing = await _planner_missing_preconditions(
                ctx, trace="t7", tool_name="k8s_rollout_restart",
                args={"namespace": "ns1"},  # namespace IS present
                discovery_steps=[],
                planner_missing=["arg:namespace"],  # planner says missing
            )
        # arg:namespace is satisfied by args → should not be in missing
        assert "arg:namespace" not in missing

    async def test_planner_missing_readonly_discovery_satisfied(self):
        """planner_missing readonly_discovery_evidence but has discovery → skipped."""
        from workers.evidence_consumer import _planner_missing_preconditions

        ctx = await self._make_ctx_for_gate()
        with patch("workers.evidence_consumer.get_tool_registry") as mock_reg, \
             patch("workers.evidence_consumer.load_trace_memory", new=AsyncMock(
                 return_value=SimpleNamespace(action_history=[]))):
            mock_reg.return_value.has.return_value = True
            mock_reg.return_value.metadata_for.return_value = {
                "required_fields": [],
                "required_evidence": [],
                "requires_readonly_before_mutate": False,
            }
            mock_reg.return_value.json_schema_for.return_value = {}
            missing = await _planner_missing_preconditions(
                ctx, trace="t8", tool_name="k8s_rollout_restart",
                args={},
                discovery_steps=["k8s_describe"],
                planner_missing=["readonly_discovery_evidence"],  # planner says missing
            )
        assert "readonly_discovery_evidence" not in missing

    async def test_planner_missing_evidence_tag_satisfied(self):
        """planner_missing evidence:patch_target_confirmed but has discovery → skipped."""
        from workers.evidence_consumer import _planner_missing_preconditions

        ctx = await self._make_ctx_for_gate()
        with patch("workers.evidence_consumer.get_tool_registry") as mock_reg, \
             patch("workers.evidence_consumer.load_trace_memory", new=AsyncMock(
                 return_value=SimpleNamespace(action_history=[]))):
            mock_reg.return_value.has.return_value = True
            mock_reg.return_value.metadata_for.return_value = {
                "required_fields": [],
                "required_evidence": [],
                "requires_readonly_before_mutate": False,
            }
            mock_reg.return_value.json_schema_for.return_value = {}
            missing = await _planner_missing_preconditions(
                ctx, trace="t9", tool_name="k8s_patch_resource",
                args={},
                discovery_steps=["k8s_describe"],  # has discovery
                planner_missing=["evidence:patch_target_confirmed"],
            )
        assert "evidence:patch_target_confirmed" not in missing

    async def test_target_workload_identity_by_pod_name(self):
        """target_workload_identity satisfied via pod_name arg."""
        from workers.evidence_consumer import _planner_missing_preconditions

        ctx = await self._make_ctx_for_gate()
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
                ctx, trace="t10", tool_name="k8s_exec_command",
                args={"pod_name": "my-pod-abc"},
                discovery_steps=[], planner_missing=None,
            )
        assert "evidence:target_workload_identity" not in missing

    async def test_target_workload_identity_by_name(self):
        """target_workload_identity satisfied via 'name' arg."""
        from workers.evidence_consumer import _planner_missing_preconditions

        ctx = await self._make_ctx_for_gate()
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
                ctx, trace="t11", tool_name="k8s_scale",
                args={"name": "my-deployment"},
                discovery_steps=[], planner_missing=None,
            )
        assert "evidence:target_workload_identity" not in missing

    async def test_value_field_present_returns_true(self):
        """_has_field_value for 'value' key just checks presence."""
        from workers.evidence_consumer import _planner_missing_preconditions

        ctx = await self._make_ctx_for_gate()
        with patch("workers.evidence_consumer.get_tool_registry") as mock_reg, \
             patch("workers.evidence_consumer.load_trace_memory", new=AsyncMock(
                 return_value=SimpleNamespace(action_history=[]))):
            mock_reg.return_value.has.return_value = True
            mock_reg.return_value.metadata_for.return_value = {
                "required_fields": ["value"],
                "required_evidence": [],
                "requires_readonly_before_mutate": False,
            }
            mock_reg.return_value.json_schema_for.return_value = {}
            missing = await _planner_missing_preconditions(
                ctx, trace="t12", tool_name="k8s_patch_secret",
                args={"value": ""},  # even empty string: key is present
                discovery_steps=[], planner_missing=None,
            )
        assert "arg:value" not in missing

    async def test_schema_default_satisfies_missing_arg(self):
        """Field missing from args but has non-empty default in schema → satisfied."""
        from workers.evidence_consumer import _planner_missing_preconditions

        ctx = await self._make_ctx_for_gate()
        with patch("workers.evidence_consumer.get_tool_registry") as mock_reg, \
             patch("workers.evidence_consumer.load_trace_memory", new=AsyncMock(
                 return_value=SimpleNamespace(action_history=[]))):
            mock_reg.return_value.has.return_value = True
            mock_reg.return_value.metadata_for.return_value = {
                "required_fields": ["restart_policy"],
                "required_evidence": [],
                "requires_readonly_before_mutate": False,
            }
            mock_reg.return_value.json_schema_for.return_value = {
                "properties": {
                    "restart_policy": {"default": "Always"}
                }
            }
            missing = await _planner_missing_preconditions(
                ctx, trace="t13", tool_name="k8s_rollout_restart",
                args={},  # restart_policy not in args, but has schema default
                discovery_steps=[], planner_missing=None,
            )
        assert "arg:restart_policy" not in missing


# ---------------------------------------------------------------------------
# _oom_memory_planner_note_from_batch — PodMetricsSpecFallback kind
# ---------------------------------------------------------------------------


class TestOomMemoryPlannerNoteSpecFallback:
    def test_spec_fallback_kind_also_matches(self):
        """PodMetricsSpecFallback kind + oom_killed → returns note."""
        from workers.evidence_consumer import _oom_memory_planner_note_from_batch

        batch = [
            {"probe": "k8s_clinical_pod_status", "extracted_fact": {"has_oom_killed": True}},
            {
                "probe": "k8s_clinical_pod_metrics",
                "extracted_fact": {
                    "kind": "PodMetricsSpecFallback",
                    "containers": [{"name": "app", "memory": "256Mi"}],
                },
            },
        ]
        note = _oom_memory_planner_note_from_batch(batch)
        assert note is not None
        assert "256Mi" in note

    def test_no_containers_in_metrics(self):
        """containers is not a list → no memory line → None."""
        from workers.evidence_consumer import _oom_memory_planner_note_from_batch

        batch = [
            {"probe": "k8s_clinical_pod_status", "extracted_fact": {"has_oom_killed": True}},
            {
                "probe": "k8s_clinical_pod_metrics",
                "extracted_fact": {"kind": "PodMetrics", "containers": None},
            },
        ]
        assert _oom_memory_planner_note_from_batch(batch) is None

    def test_container_without_memory_field(self):
        """Container dict without 'memory' key → no note."""
        from workers.evidence_consumer import _oom_memory_planner_note_from_batch

        batch = [
            {"probe": "k8s_clinical_pod_status", "extracted_fact": {"has_oom_killed": True}},
            {
                "probe": "k8s_clinical_pod_metrics",
                "extracted_fact": {"kind": "PodMetrics", "containers": [{"name": "app"}]},
            },
        ]
        assert _oom_memory_planner_note_from_batch(batch) is None


# ---------------------------------------------------------------------------
# build_sdk_fact_only_prompt — additional paths
# ---------------------------------------------------------------------------


class TestBuildSdkFactOnlyPromptExtra:
    def test_multiple_items_all_represented(self):
        """Multiple batch items → each probe appears in output."""
        from workers.evidence_consumer import build_sdk_fact_only_prompt

        batch = [
            {"probe": "probe_a", "alert_rule": "A", "alert_hint": "hint_a", "extracted_fact": {"k": "v"}},
            {"probe": "probe_b", "alert_rule": "B", "alert_hint": "hint_b", "extracted_fact": {"x": 1}},
        ]
        result = build_sdk_fact_only_prompt(batch)
        assert "probe_a" in result
        assert "probe_b" in result

    def test_extracted_fact_json_string_starting_with_brace(self):
        """extracted_fact JSON string starting with '{' is re-parsed."""
        from workers.evidence_consumer import build_sdk_fact_only_prompt

        batch = [{"probe": "test", "alert_rule": "R", "alert_hint": "H",
                  "extracted_fact": '{"status": "Failed"}'}]
        result = build_sdk_fact_only_prompt(batch)
        assert "test" in result
        assert "Failed" in result

    def test_extracted_fact_non_json_string(self):
        """extracted_fact non-JSON string → used as-is."""
        from workers.evidence_consumer import build_sdk_fact_only_prompt

        batch = [{"probe": "raw_probe", "alert_rule": "RAW", "alert_hint": "hint",
                  "extracted_fact": "plain text output"}]
        result = build_sdk_fact_only_prompt(batch)
        assert "raw_probe" in result
        assert "plain text output" in result

    def test_extracted_fact_none(self):
        """extracted_fact=None → empty string used."""
        from workers.evidence_consumer import build_sdk_fact_only_prompt

        batch = [{"probe": "no_ef", "alert_rule": "R", "alert_hint": "H", "extracted_fact": None}]
        result = build_sdk_fact_only_prompt(batch)
        assert "no_ef" in result


# ---------------------------------------------------------------------------
# _hints_from_evidence_batch — matrix row and oom note injection
# ---------------------------------------------------------------------------


class TestHintsFromEvidenceBatch:
    def test_batch_alert_rule_added_as_alertname(self):
        """batch[0].alert_rule → h['alertname'] if not already set."""
        from workers.evidence_consumer import _hints_from_evidence_batch

        batch = [{"alert_rule": "HighCPU", "symptom_group": "", "extracted_fact": {}}]
        with patch("pkg.reasoning.incident_matrix_profile.pick_matrix_row_for_batch", return_value=None):
            result = _hints_from_evidence_batch(batch, "some text")
        assert result is not None
        assert result.get("alertname") == "HighCPU"

    def test_batch_symptom_group_added(self):
        """batch[0].symptom_group → h['symptom_group']."""
        from workers.evidence_consumer import _hints_from_evidence_batch

        batch = [{"alert_rule": "", "symptom_group": "resource_pressure", "extracted_fact": {}}]
        with patch("pkg.reasoning.incident_matrix_profile.pick_matrix_row_for_batch", return_value=None):
            result = _hints_from_evidence_batch(batch, "")
        assert result is not None
        assert result.get("symptom_group") == "resource_pressure"

    def test_diagnostic_pattern_from_matrix_row(self):
        """pick_matrix_row_for_batch returns row with diagnostic_pattern → injected."""
        from workers.evidence_consumer import _hints_from_evidence_batch

        batch = [{"alert_rule": "", "symptom_group": "", "extracted_fact": {}}]
        matrix_row = {"diagnostic_pattern": "cpu_spike_then_oom"}
        with patch("pkg.reasoning.incident_matrix_profile.pick_matrix_row_for_batch",
                   return_value=matrix_row):
            result = _hints_from_evidence_batch(batch, "")
        assert result is not None
        assert result.get("diagnostic_pattern") == "cpu_spike_then_oom"

    def test_oom_note_injected(self):
        """_oom_memory_planner_note_from_batch result → h['oom_memory_planner_note']."""
        from workers.evidence_consumer import _hints_from_evidence_batch

        batch = [
            {"probe": "k8s_clinical_pod_status", "alert_rule": "", "symptom_group": "",
             "extracted_fact": {"has_oom_killed": True}},
            {"probe": "k8s_clinical_pod_metrics", "alert_rule": "", "symptom_group": "",
             "extracted_fact": {"kind": "PodMetrics", "containers": [{"name": "app", "memory": "128Mi"}]}},
        ]
        with patch("pkg.reasoning.incident_matrix_profile.pick_matrix_row_for_batch", return_value=None):
            result = _hints_from_evidence_batch(batch, "")
        assert result is not None
        assert "oom_memory_planner_note" in result
        assert "128Mi" in result["oom_memory_planner_note"]

    def test_empty_batch_with_empty_text_returns_none(self):
        """Empty batch and empty text → no hints → None."""
        from workers.evidence_consumer import _hints_from_evidence_batch

        result = _hints_from_evidence_batch([], "")
        assert result is None
