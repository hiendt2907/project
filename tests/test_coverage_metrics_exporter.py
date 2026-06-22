"""Coverage tests for src/workers/metrics_exporter.py — all setter/counter functions."""
from __future__ import annotations

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch


# Force fresh module import for each test by using direct imports
import workers.metrics_exporter as me


# ── Basic counter/gauge functions ─────────────────────────────────────────────

def test_inc_messages_processed_stream():
    me.inc_messages_processed("stream")


def test_inc_messages_processed_telegram():
    me.inc_messages_processed("telegram")


def test_inc_messages_processed_http():
    me.inc_messages_processed("http")


def test_inc_messages_processed_unknown_normalised():
    me.inc_messages_processed("some_unknown_source")


def test_set_last_scout_timestamp_default():
    me.set_last_scout_timestamp()


def test_set_last_scout_timestamp_explicit():
    me.set_last_scout_timestamp(1_700_000_000.0)


def test_inc_slow_path_exhausted():
    me.inc_slow_path_exhausted("max_attempts", "error_signature_abc")
    me.inc_slow_path_exhausted("stale_signature", "mixed")
    me.inc_slow_path_exhausted("loop_exit", "")
    me.inc_slow_path_exhausted("", "")


def test_set_proactive_kill_switch():
    me.set_proactive_kill_switch(0.0)
    me.set_proactive_kill_switch(1.0)


def test_llm_semaphore_inc_dec():
    me.llm_semaphore_inc("proactive")
    me.llm_semaphore_dec("proactive")
    me.llm_semaphore_inc("reactive")
    me.llm_semaphore_dec("reactive")
    me.llm_semaphore_inc("unknown_lane")
    me.llm_semaphore_dec("unknown_lane")


def test_ollama_semaphore_aliases():
    me.ollama_semaphore_inc("proactive")
    me.ollama_semaphore_dec("proactive")


def test_inc_anomaly_events():
    me.inc_anomaly_events()


def test_set_baseline_snapshot_gauges_all_none():
    me.set_baseline_snapshot_gauges(
        z_cpu=None,
        z_mem=None,
        dr=False,
    )


def test_set_baseline_snapshot_gauges_full():
    me.set_baseline_snapshot_gauges(
        z_cpu=2.5,
        z_mem=1.1,
        z_disk=0.3,
        z_iops=0.7,
        dr=True,
        chs=0.85,
        remediation_silent=True,
    )


def test_set_circuit_breaker_active():
    me.set_circuit_breaker_active(0)
    me.set_circuit_breaker_active(1)


def test_set_lag_size():
    me.set_lag_size(0)
    me.set_lag_size(1000)


def test_inc_error_rate():
    me.inc_error_rate("kafka", "timeout")
    me.inc_error_rate("redis", "connection_refused")
    me.inc_error_rate("", "")


def test_observe_latency():
    me.observe_latency(0.5)
    me.observe_latency(10.0)


def test_observe_crat_write_seconds():
    me.observe_crat_write_seconds(0.002)
    me.observe_crat_write_seconds(-1.0)  # should be clamped to 0


def test_inc_learning_upsert():
    me.inc_learning_upsert("proactive_sop", "success")
    me.inc_learning_upsert("proactive_fallback", "fail")
    me.inc_learning_upsert("", "")


def test_set_learning_unique_patterns():
    me.set_learning_unique_patterns("proactive_sop", "success", 42.0)
    me.set_learning_unique_patterns("", "", 0.0)


def test_inc_proactive_fallback():
    me.inc_proactive_fallback("sop_miss")
    me.inc_proactive_fallback("")


def test_inc_proactive_verify():
    me.inc_proactive_verify("success")
    me.inc_proactive_verify("fail")
    me.inc_proactive_verify("")


def test_inc_learning_governance():
    me.inc_learning_governance("allow")
    me.inc_learning_governance("deny")
    me.inc_learning_governance("")


def test_inc_proactive_events():
    me.inc_proactive_events()


def test_inc_llm_requests():
    me.inc_llm_requests()


def test_inc_evidence_llm_contradiction():
    me.inc_evidence_llm_contradiction()


def test_inc_rag_empty_result():
    me.inc_rag_empty_result("playbook_store", "semantic")
    me.inc_rag_empty_result("", "")


def test_inc_fastpath_hits():
    me.inc_fastpath_hits()


def test_inc_experience_saved():
    me.inc_experience_saved()


def test_inc_agent_sessions_total():
    me.inc_agent_sessions_total()


def test_inc_agent_premature_escalate_blocked():
    me.inc_agent_premature_escalate_blocked()


def test_inc_proactive_requires_human():
    me.inc_proactive_requires_human("no_k8s")
    me.inc_proactive_requires_human("lease_conflict")
    me.inc_proactive_requires_human("")


def test_inc_proactive_freeze():
    me.inc_proactive_freeze("resource")
    me.inc_proactive_freeze("namespace")
    me.inc_proactive_freeze("")


def test_inc_proactive_event_timeout():
    me.inc_proactive_event_timeout()


def test_inc_proactive_tombstone_no_k8s():
    me.inc_proactive_tombstone_no_k8s()


def test_inc_proactive_lease_conflict():
    me.inc_proactive_lease_conflict()


def test_inc_proactive_skip_frozen():
    me.inc_proactive_skip_frozen("resource")
    me.inc_proactive_skip_frozen("namespace")
    me.inc_proactive_skip_frozen("")


def test_set_wilson_confidence_score():
    me.set_wilson_confidence_score(0.75)
    me.set_wilson_confidence_score(0.0)
    me.set_wilson_confidence_score(1.0)


def test_set_redis_stream_backlog():
    me.set_redis_stream_backlog("stream:incidents", 42.0)
    me.set_redis_stream_backlog("", 0.0)


def test_inc_proactive_outcome():
    me.inc_proactive_outcome("sop_success")
    me.inc_proactive_outcome("learning_resolved")
    me.inc_proactive_outcome("governance_deny")
    me.inc_proactive_outcome("")


def test_observe_proactive_incident_duration():
    me.observe_proactive_incident_duration(5.5)
    me.observe_proactive_incident_duration(-1.0)  # clamped to 0


def test_inc_promql_placeholder_rejected():
    me.inc_promql_placeholder_rejected()


def test_inc_telegram_timeout():
    me.inc_telegram_timeout("advisory")
    me.inc_telegram_timeout("escalation")
    me.inc_telegram_timeout("")


def test_set_kafka_consumer_lag():
    me.set_kafka_consumer_lag("omni-diagnostic-evidence", "omni-analyst", 500)
    me.set_kafka_consumer_lag("", "", 0)


def test_inc_dlq_published():
    me.inc_dlq_published("omni-dlq")
    me.inc_dlq_published()
    me.inc_dlq_published("")


# ── Self-monitoring setters ────────────────────────────────────────────────────

def test_set_worker_last_message_age():
    me.set_worker_last_message_age(0.0)
    me.set_worker_last_message_age(120.5)
    me.set_worker_last_message_age(-1.0)  # clamped


def test_set_health_check_status():
    me.set_health_check_status("redis_ping", 1.0)
    me.set_health_check_status("kafka_lag", 0.5)
    me.set_health_check_status("llm_up", 0.0)
    me.set_health_check_status("", 0.0)


# ── KPI setters ────────────────────────────────────────────────────────────────

def test_observe_kpi_mttd():
    me.observe_kpi_mttd("SYS_RESOURCE", 45.0)
    me.observe_kpi_mttd("SIEM_SECURITY", 120.0)
    me.observe_kpi_mttd("", 0.0)
    me.observe_kpi_mttd("APP_HTTP", -1.0)  # clamped


def test_observe_kpi_mttr():
    me.observe_kpi_mttr("SYS_HARD_FAIL", 300.0)
    me.observe_kpi_mttr("", 0.0)


def test_set_kpi_advisory_acceptance_rate():
    me.set_kpi_advisory_acceptance_rate(0.92)
    me.set_kpi_advisory_acceptance_rate(0.0)
    me.set_kpi_advisory_acceptance_rate(1.0)
    me.set_kpi_advisory_acceptance_rate(-0.5)  # clamped to 0
    me.set_kpi_advisory_acceptance_rate(1.5)   # clamped to 1


def test_set_kpi_false_positive_rate():
    me.set_kpi_false_positive_rate(0.05)
    me.set_kpi_false_positive_rate(0.0)
    me.set_kpi_false_positive_rate(1.0)


def test_inc_kpi_incident():
    me.inc_kpi_incident("SYS_RESOURCE", "accepted")
    me.inc_kpi_incident("SIEM_SECURITY", "rejected")
    me.inc_kpi_incident("APP_HTTP", "false_positive")
    me.inc_kpi_incident("", "")


# ── Advisory benchmark setters ─────────────────────────────────────────────────

def test_set_advisory_benchmark_score():
    me.set_advisory_benchmark_score("case_001", "qwen2.5:7b", 85.0)
    me.set_advisory_benchmark_score("case_010", "qwen2.5:7b", 0.0)
    me.set_advisory_benchmark_score("", "", 50.0)
    me.set_advisory_benchmark_score("case_001", "model", -10.0)   # clamped to 0
    me.set_advisory_benchmark_score("case_001", "model", 110.0)   # clamped to 100


def test_set_advisory_benchmark_pass_rate():
    me.set_advisory_benchmark_pass_rate(0.8)
    me.set_advisory_benchmark_pass_rate(0.0)
    me.set_advisory_benchmark_pass_rate(1.0)


# ── Async probe_llm_up ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_probe_llm_up_success():
    import httpx
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client
        await me.probe_llm_up("http://localhost:11434")


@pytest.mark.asyncio
async def test_probe_llm_up_failure():
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=Exception("connection refused"))
        mock_client_cls.return_value = mock_client
        await me.probe_llm_up("http://localhost:11434")


@pytest.mark.asyncio
async def test_probe_llm_up_500_response():
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client
        await me.probe_llm_up("http://localhost:11434")


# ── sync_proactive_kill_switch_metric ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_sync_kill_switch_redis_returns_1():
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value="1")
    await me.sync_proactive_kill_switch_metric(mock_redis, "some:key")


@pytest.mark.asyncio
async def test_sync_kill_switch_redis_returns_none():
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=None)
    await me.sync_proactive_kill_switch_metric(mock_redis, "some:key")


@pytest.mark.asyncio
async def test_sync_kill_switch_redis_exception():
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(side_effect=Exception("redis down"))
    await me.sync_proactive_kill_switch_metric(mock_redis, "some:key")
