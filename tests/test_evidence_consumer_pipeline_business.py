"""Deep unit tests for workers/evidence_consumer.py business helpers (no Kafka loop)."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from workers.evidence_mutate_emit import _siem_alert_labels
from workers.handler_context import WorkerHandlerContext


def _ctx(**kwargs):
    defaults = dict(
        omni_shadow_os_mode=True,
        omni_sigma_log_bypass_enabled=False,
        omni_proof_lane_enabled=True,
        baseline_dr_z_threshold=3.0,
        autonomous_sigma_observation_window=1,
        omni_planner_precondition_gate_enabled=False,
        trace_correlation_ping_enabled=False,
        telegram_admin_chat_id=123,
        kafka_topic_alerts="omni-alerts",
    )
    defaults.update(kwargs)
    ws = SimpleNamespace(**defaults)
    return WorkerHandlerContext(
        settings=ws,
        redis=None,
        llm=AsyncMock(),
        vector_store=MagicMock(),
        ledger=MagicMock(),
        semaphore=AsyncMock(),
        telegram=None,
        kafka=None,
    )


def test_shadow_os_mode_and_derive_commands():
    from workers.evidence_consumer import _shadow_os_mode, _derive_shadow_os_commands

    ctx = _ctx(omni_shadow_os_mode=False)
    assert _shadow_os_mode(ctx) is False
    ctx2 = _ctx(omni_shadow_os_mode=True)
    assert _shadow_os_mode(ctx2) is True

    assert _derive_shadow_os_commands(tool_name="", args={}, evidence_refs=[], trace="t") == []
    steps = _derive_shadow_os_commands(
        tool_name="k8s_rollout_restart",
        args={"namespace": "ns"},
        evidence_refs=["e1"],
        trace="trace-very-long-id-1234567890",
    )
    assert len(steps) == 2
    assert steps[0]["risk_level"] == "low"
    assert "kubectl" in steps[0]["command"]


def test_symptom_and_siem_batch_flags():
    from workers.evidence_consumer import _symptom_group_from_batch, _is_siem_batch

    assert _symptom_group_from_batch([]) == ""
    assert _symptom_group_from_batch([{"symptom_group": "  cpu  "}]) == "cpu"
    batch = [{"probe": "siem_incident_context", "labels": {"siem_category": "ddos"}}]
    assert _is_siem_batch(batch) is bool(_siem_alert_labels(batch))


def test_siem_forecast_and_format_all_branches():
    from workers.evidence_consumer import _siem_forecast_timeline, _format_siem_forecast_text

    for cat in ("ddos", "malware", "data_exfil", "k8s_threat", "auth_failure", "lateral_movement", "network_anomaly", "unknown"):
        for sev in ("critical", "high", "medium", "low", "nope"):
            tl = _siem_forecast_timeline(cat, sev)
            assert len(tl) == 5
            txt = _format_siem_forecast_text(tl)
            assert "Dự báo" in txt


def test_siem_diagnosis_variants():
    from workers.evidence_consumer import _siem_diagnosis_from_batch

    labels = {"siem_category": "ddos", "severity": "critical", "siem_incident_id": "I1"}
    diag = _siem_diagnosis_from_batch([], labels, "fallback text")
    assert "WHAT" in diag and "HOW-TO" in diag

    batch = [
        {
            "probe": "siem_incident_context",
            "extracted_fact": json.dumps(
                {
                    "incident_id": "I2",
                    "category": "auth_failure",
                    "severity": "high",
                    "namespace": "ns1",
                    "description": "x",
                    "tenant": "t1",
                    "affected_ip": "1.2.3.4",
                }
            ),
        }
    ]
    d2 = _siem_diagnosis_from_batch(batch, {}, "")
    assert "AUTH" in d2.upper() or "auth" in d2.lower()


def test_rag_search_failed_and_f64_clamp():
    from workers.evidence_consumer import _rag_search_failed, _f64, _clamp01

    assert _rag_search_failed({"reason": "search_error"}) is True
    assert _rag_search_failed({"reason": "ok"}) is False
    assert _f64("3.5") == 3.5
    assert _f64("bad") is None
    assert _clamp01(-1) == 0.0
    assert _clamp01(2) == 1.0


def test_build_sdk_fact_only_prompt_edges():
    from workers.evidence_consumer import build_sdk_fact_only_prompt

    assert "no evidence" in build_sdk_fact_only_prompt([])
    batch = [
        {
            "alert_rule": "R",
            "alert_hint": "H",
            "probe": "p1",
            "extracted_fact": {"a": 1},
        },
        {
            "probe": "p2",
            "extracted_fact": '{"x": 2}',
        },
        {"probe": "p3", "extracted_fact": "plain"},
    ]
    out = build_sdk_fact_only_prompt(batch)
    assert "p1" in out and "p2" in out


def test_hints_from_evidence_text_and_batch():
    from workers.evidence_consumer import _hints_from_evidence_text, _hints_from_evidence_batch

    h = _hints_from_evidence_text("namespace=prod pod=my-pod-1\nrule: KubeOOM\nsymptom_group: mem")
    assert h and h.get("namespace") == "prod"
    assert h.get("pod_name") == "my-pod-1"

    batch = [{"alert_rule": "KubePodCrashLooping", "symptom_group": "crash"}]
    hb = _hints_from_evidence_batch(batch, "namespace=ns2")
    assert hb and hb.get("namespace") == "ns2"


def test_oom_memory_planner_note():
    from workers.evidence_consumer import _oom_memory_planner_note_from_batch

    batch = [
        {"probe": "k8s_clinical_pod_status", "extracted_fact": {"has_oom_killed": True}},
        {
            "probe": "k8s_clinical_pod_metrics",
            "extracted_fact": {
                "kind": "PodMetrics",
                "containers": [{"memory": "512Mi"}],
            },
        },
    ]
    note = _oom_memory_planner_note_from_batch(batch)
    assert note and "OOMKilled" in note


def test_planner_phase_done_diagnosis():
    from workers.evidence_consumer import _planner_phase_done_diagnosis

    assert "A" in _planner_phase_done_diagnosis("A", "B")
    assert _planner_phase_done_diagnosis("", "only_rs") == "only_rs"


@pytest.mark.asyncio
async def test_planner_missing_preconditions_respects_gate_off():
    from workers.evidence_consumer import _planner_missing_preconditions

    ctx = _ctx(omni_planner_precondition_gate_enabled=False)
    ctx.redis = fakeredis_module()
    out = await _planner_missing_preconditions(
        ctx, trace="t", tool_name="x", args={}, discovery_steps=[], planner_missing=None
    )
    assert out == []


def fakeredis_module():
    import fakeredis.aioredis

    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.mark.asyncio
async def test_proof_of_fault_gate_no_critical():
    from workers import evidence_consumer as ec

    ctx = _ctx()
    ctx.redis = fakeredis_module()
    ok, reason, meta = await ec._proof_of_fault_gate(
        ctx, trace="tr", batch=[{"probe": "redis_ping"}], rag_match_text=None
    )
    assert ok is False
    assert meta["critical_evidence"] is False


@pytest.mark.asyncio
async def test_proof_of_fault_gate_state_lane():
    from workers import evidence_consumer as ec

    ctx = _ctx(omni_proof_lane_enabled=True)
    r = fakeredis_module()
    ctx.redis = r
    batch = [{"alert_hint": "CrashLoopBackOff", "probe": "k8s"}]
    with patch("workers.evidence_consumer.resolve_proof_lane", return_value=("state", "test")):
        ok, _, meta = await ec._proof_of_fault_gate(ctx, trace="t2", batch=batch)
    assert meta["proof_lane"] == "state"
    assert ok is True


@pytest.mark.asyncio
async def test_proof_of_fault_gate_resource_lane_sigma():
    from workers import evidence_consumer as ec
    from workers.baseline_snapshot import REDIS_KEY_SNAPSHOT

    ctx = _ctx(omni_proof_lane_enabled=True)
    r = fakeredis_module()
    await r.set(REDIS_KEY_SNAPSHOT, json.dumps({"dr": False, "z_cpu": 4.0, "z_mem": 0.1}))
    ctx.redis = r
    batch = [{"alert_hint": "CrashLoopBackOff", "probe": "prom"}]
    with patch("workers.evidence_consumer.resolve_proof_lane", return_value=("resource", "m")):
        ok, _, meta = await ec._proof_of_fault_gate(ctx, trace="t3", batch=batch)
    assert meta["sigma_ok"] is True
    assert ok is True


@pytest.mark.asyncio
async def test_proof_of_fault_gate_legacy_log_bypass():
    from workers import evidence_consumer as ec
    from workers.log_surge_probe import LogSurgeResult

    ctx = _ctx(omni_proof_lane_enabled=False, omni_sigma_log_bypass_enabled=True)
    r = fakeredis_module()
    ctx.redis = r
    batch = [{"alert_hint": "CrashLoopBackOff", "probe": "k8s"}]
    res = LogSurgeResult(
        ok=True,
        reason="5xx",
        escalate_log_unavailable=False,
        meta={"lines_fetched": 10},
        dominant_error_class="5xx",
    )
    with (
        patch("workers.evidence_consumer.resolve_proof_lane", return_value=("app_log", "x")),
        patch("workers.evidence_consumer.evaluate_log_surge_sigma_bypass", new=AsyncMock(return_value=res)),
        patch("workers.evidence_consumer.namespace_pod_from_batch", return_value=("ns", "pod1")),
        patch("workers.evidence_consumer.namespace_allowed", return_value=True),
        patch("pkg.reasoning.incident_matrix_profile.is_api_web_workload", return_value=True),
    ):
        ctx.settings.omni_loki_base_url = "http://loki"
        ok, _, meta = await ec._proof_of_fault_gate(ctx, trace="t4", batch=batch)
    assert ok is True
    assert meta.get("sigma_bypass_via_log_surge") is True


@pytest.mark.asyncio
async def test_try_log_surge_early_exits():
    from workers import evidence_consumer as ec

    ctx = _ctx(omni_sigma_log_bypass_enabled=False)
    ok, extra, esc = await ec._try_log_surge_sigma_bypass(ctx, "t", [], None)
    assert ok is False and esc is False

    ctx2 = _ctx(omni_sigma_log_bypass_enabled=True)
    ctx2.redis = fakeredis_module()
    with patch("workers.evidence_consumer.namespace_pod_from_batch", return_value=(None, None)):
        ok2, _, _ = await ec._try_log_surge_sigma_bypass(ctx2, "t", [{"x": 1}], None)
    assert ok2 is False


@pytest.mark.asyncio
async def test_notify_siem_telegram_paths():
    from workers import evidence_consumer as ec

    ctx = _ctx(telegram_admin_chat_id=None)
    await ec._notify_siem_telegram(ctx, trace="t", batch=[], diagnosis="WHY: siem reason\nHOW-TO\n1 kubectl get pods")
    tg = AsyncMock()
    ctx.telegram = tg
    await ec._notify_siem_telegram(ctx, trace="t", batch=[], diagnosis="x")
    tg.send_message.assert_not_called()

    ctx.settings.telegram_admin_chat_id = 999
    batch = [
        {
            "probe": "siem_incident_context",
            "extracted_fact": {
                "incident_id": "I9",
                "category": "ddos",
                "severity": "critical",
                "namespace": "ns",
                "description": "flood",
                "tenant": "bank",
                "affected_ip": "9.9.9.9",
            },
            "labels": {
                "siem_incident_id": "I9",
                "siem_category": "ddos",
                "severity": "critical",
                "namespace": "ns",
                "alertname": "SIEMDdos",
            },
        }
    ]
    from workers.evidence_consumer import _siem_diagnosis_from_batch

    diag = _siem_diagnosis_from_batch(batch, _siem_alert_labels(batch) or {}, "")
    await ec._notify_siem_telegram(ctx, trace="t", batch=batch, diagnosis=diag)
    tg.send_message.assert_awaited()


@pytest.mark.asyncio
async def test_emit_suggest_remediation_skips_when_disabled():
    from workers import evidence_consumer as ec

    ctx = _ctx(trace_correlation_ping_enabled=False)
    await ec._emit_suggest_remediation(
        ctx,
        trace="t",
        diagnosis="d",
        confidence=0.9,
        source="s",
        suggested_tool="k8s_describe_resource",
    )


@pytest.mark.asyncio
async def test_emit_suggest_os_runbook_skips_when_disabled():
    from workers import evidence_consumer as ec

    ctx = _ctx(trace_correlation_ping_enabled=False)
    ok = await ec._emit_suggest_os_runbook(
        ctx,
        trace="t",
        diagnosis="d",
        confidence=0.5,
        source="s",
        runbook_title="t",
        commands=[],
    )
    assert ok is False
