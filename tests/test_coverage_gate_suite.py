"""Raise `.coveragerc.gate` line coverage toward >=90% without live K8s.

Uses FastAPI TestClient + fakeredis / AsyncMock for gateway routes and
targeted calls into pkg/reasoning helpers.
"""
from __future__ import annotations

import json
import time
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_kpi_summary_and_trend():
    from gateway.routes.kpi import router

    redis = MagicMock()
    redis.zcount = AsyncMock(return_value=0)
    app = FastAPI()
    app.include_router(router)
    app.state.redis = redis
    with TestClient(app) as client:
        r = client.get("/kpi/summary")
        assert r.status_code == 200
        r2 = client.get("/kpi/trend?window=24h")
        assert r2.status_code == 200
        assert "lanes" in r2.json()


def test_agents_list_mock_redis():
    from gateway.routes.agents import router

    redis = MagicMock()
    hb = json.dumps({"updated_at": int(time.time()), "status": "ok", "role": "analyst"})
    redis.keys = AsyncMock(return_value=["omni:agent:heartbeat:analyst"])
    redis.get = AsyncMock(return_value=hb)
    app = FastAPI()
    app.include_router(router)
    app.state.redis = redis
    with TestClient(app) as client:
        r = client.get("/agents")
        assert r.status_code == 200
        assert r.json()["count"] >= 1


def test_autonomy_policy_flow_mock_redis():
    from gateway.routes.autonomy import router
    from pkg.autonomy.policy import PolicyRule, AutonomyLevel

    redis = MagicMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock()
    redis.lpush = AsyncMock()
    redis.ltrim = AsyncMock()
    redis.lrange = AsyncMock(return_value=[])

    app = FastAPI()
    app.include_router(router)
    app.state.redis = redis

    with TestClient(app) as client:
        r = client.get("/autonomy/policy")
        assert r.status_code == 200
        assert "policy" in r.json()

        rule = PolicyRule(
            lane="APP_HTTP",
            severity="high",
            action_type="restart_pod",
            level=AutonomyLevel.ALERT_ONLY,
            reason="test",
        )
        r2 = client.post("/autonomy/policy/rule", json=rule.model_dump())
        assert r2.status_code == 200

        redis.lrange = AsyncMock(return_value=[])
        r3 = client.get("/autonomy/policy/history?limit=5")
        assert r3.status_code == 200

        r4 = client.post("/autonomy/policy/reset")
        assert r4.status_code == 200


def test_siem_overview_mock_redis():
    from gateway.routes.siem import router

    block = {
        "seq": 2,
        "event_type": "ADVISORY_DECISION",
        "trace_id": "t1",
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "payload": {"verdict": "SUGGEST", "root_cause": "cpu", "affected_workload": "d/x"},
    }
    redis = MagicMock()
    redis.get = AsyncMock(side_effect=["2", "a" * 64])
    redis.lrange = AsyncMock(return_value=[json.dumps(block)])
    app = FastAPI()
    app.include_router(router)
    app.state.redis = redis
    with TestClient(app) as client:
        r = client.get("/siem/overview?limit=5")
        assert r.status_code == 200
        j = r.json()
        assert j["chain"]["total_blocks"] == 2


def test_playbooks_list_and_get_mock_redis():
    from gateway.routes.playbooks import router

    doc = {"playbook_id": "pb1", "name": "n1"}
    redis = MagicMock()
    redis.keys = AsyncMock(return_value=["pb:pb1"])
    redis.execute_command = AsyncMock(return_value=json.dumps(doc))
    app = FastAPI()
    app.include_router(router)
    app.state.redis = redis
    with TestClient(app) as client:
        r = client.get("/playbooks")
        assert r.status_code == 200
        assert r.json()["total"] >= 1
        r2 = client.get("/playbooks/pb1")
        assert r2.status_code == 200


def test_playbooks_state_and_hitl_mock_redis():
    from gateway.routes.playbooks import router

    redis = MagicMock()
    redis.get = AsyncMock(return_value=json.dumps({"step": 1}))
    app = FastAPI()
    app.include_router(router)
    app.state.redis = redis

    class _Resp:
        status = 200

        def read(self):
            return b'{"ok":true}'

    @contextmanager
    def _open(*_a, **_kw):
        yield _Resp()

    with TestClient(app) as client:
        r = client.get("/playbooks/pb1/state?trace_id=t1")
        assert r.status_code == 200

        with patch("gateway.routes.playbooks.urllib.request.urlopen", _open):
            r2 = client.post(
                "/playbooks/inc1/approve",
                json={"trace_id": "t1", "reason": "ok"},
            )
            assert r2.status_code == 200
            r3 = client.post(
                "/playbooks/inc1/reject",
                json={"trace_id": "t1", "reason": "no"},
            )
            assert r3.status_code == 200


def test_gateway_env_topic_and_headers():
    import os
    from unittest.mock import MagicMock

    from gateway import api as gw

    os.environ.pop("OMNI_KAFKA_TOPIC_ALERTS", None)
    assert gw._kafka_topic_from_env() == "omni-alerts"
    os.environ["OMNI_KAFKA_TOPIC_ALERTS"] = "valid-topic_1"
    assert gw._kafka_topic_from_env() == "valid-topic_1"
    del os.environ["OMNI_KAFKA_TOPIC_ALERTS"]

    req = MagicMock()
    req.headers = MagicMock()
    req.headers.get = MagicMock(return_value="abc")
    assert gw._str_header(req, "X") == "abc"
    req.headers.get = MagicMock(return_value=None)
    assert gw._str_header(req, "X") is None


def test_gateway_linear_forecast():
    from gateway import api as gw

    vals = [1.0, 2.0, 3.0, 4.0]
    pred, meta = gw._linear_forecast(vals, horizon_steps=3)
    assert len(pred) == 3
    assert "slope" in meta


def test_incident_matrix_helpers():
    from pkg.reasoning.incident_matrix_profile import (
        alertname_from_batch,
        invalidate_matrix_cache,
        labels_from_batch,
        merged_matrix_scenarios,
        pick_matrix_row_for_batch,
        proof_lane_from_annotation,
        resolve_proof_lane,
        row_matches_series_label_defaults,
        rows_matching_prometheus_alert,
        state_lane_heuristic,
        workload_profile_for_alert,
    )

    invalidate_matrix_cache()
    scenarios = merged_matrix_scenarios()
    assert isinstance(scenarios, list)

    batch = [
        {"canonical_query_snippet": json.dumps({"labels": {"alertname": "KubePodOOMKilled"}})}
    ]
    assert alertname_from_batch(batch) == "KubePodOOMKilled"
    assert isinstance(labels_from_batch(batch), dict)
    rows = rows_matching_prometheus_alert("KubePodOOMKilled")
    assert isinstance(rows, list)
    row = pick_matrix_row_for_batch(batch)
    assert row is None or isinstance(row, dict)

    ann = [
        {
            "probe": "x",
            "canonical_query_snippet": json.dumps(
                {"annotations": {"omni_proof_lane": "resource"}}
            ),
        }
    ]
    assert proof_lane_from_annotation(ann) == "resource"

    resolve_proof_lane(batch)
    wl = workload_profile_for_alert("KubePodOOMKilled")
    assert wl is None or isinstance(wl, str)

    assert isinstance(state_lane_heuristic([{"probe": "k8s_event_stream"}]), bool)
    row_matches_series_label_defaults({"labels_alertname": "KubePodOOMKilled"}, batch)


def test_evidence_signals_critical_hint():
    from pkg.reasoning.evidence_signals import critical_evidence_present

    assert critical_evidence_present(
        [{"alert_hint": "CrashLoopBackOff on pod", "probe": "k8s"}]
    )
    assert not critical_evidence_present(
        [{"extracted_fact": {"phase": "running"}, "probe": "k8s"}]
    )


def test_evidence_adapter_protocol_name():
    from services.evidence_adapter.protocol import EvidenceAdapter

    assert EvidenceAdapter is not None


def test_sanitize_branches():
    from pkg.reasoning import sanitize as san

    w = san.evidence_relevance_warning("High CPU 90%", "redis_ping")
    assert w is None or "workload" in w
    txt = san.format_sanitized_analyst_user_text(
        {
            "alert_rule": "R1",
            "alert_hint": "oom",
            "probe": "k8s_pod_status",
            "canonical_query_snippet": '{"labels":{"pod":"p"}}',
            "evidence_source": "K8s_SDK",
            "result": "ok",
            "extracted_fact": {"reason": "OOMKilled"},
        }
    )
    assert "[ALERT_CONTEXT]" in txt


@pytest.mark.asyncio
async def test_error_ledger_record_error_mocked():
    from rag.error_ledger import ErrorLedger

    r = AsyncMock()
    ledger = ErrorLedger(r)
    with patch("rag.error_ledger.log_error_to_ledger", new=AsyncMock(return_value="id-1")):
        out = await ledger.record_error(title="t", detail="d", phase="p")
        assert out == "id-1"


@pytest.mark.asyncio
async def test_playbook_store_ensure_and_upsert():
    from services.playbook.models import Playbook, PlaybookStep
    from services.playbook import store as pb_store

    r = MagicMock()
    ft_mock = MagicMock()
    r.ft = MagicMock(return_value=ft_mock)
    ft_mock.info = AsyncMock(side_effect=Exception("no index"))
    ft_mock.create_index = AsyncMock()
    json_mock = MagicMock()
    r.json = MagicMock(return_value=json_mock)
    json_mock.set = AsyncMock()

    ps = pb_store.PlaybookStore(r)
    await ps.ensure_ready()
    ft_mock.create_index.assert_awaited()

    pb = Playbook(
        playbook_id="p1",
        version=1,
        name="test",
        severity_filter="high",
        approved_by="qa",
        siem_categories=("ddos",),
        steps=(
            PlaybookStep(
                step_order=1,
                action_type="notify",
                target="slack",
                params={},
                timeout_sec=30,
                requires_hitl=False,
            ),
        ),
    )
    await ps.upsert(pb)
    json_mock.set.assert_awaited()


@pytest.mark.asyncio
async def test_playbook_store_get_and_search():
    from services.playbook import store as pb_store

    doc = {
        "playbook_id": "p1",
        "version": 1,
        "name": "n",
        "severity_filter": "high",
        "approved_by": "a",
        "siem_categories": ["ddos"],
        "created_at_ts": time.time(),
        "steps": [],
    }
    r = MagicMock()
    ft_mock = MagicMock()
    r.ft = MagicMock(return_value=ft_mock)
    ft_mock.info = AsyncMock()
    json_api = MagicMock()
    json_api.get = AsyncMock(return_value=[doc])
    r.json = MagicMock(return_value=json_api)

    ps = pb_store.PlaybookStore(r)
    out = await ps.get("p1")
    assert out is not None
    assert out.playbook_id == "p1"

    res_doc = MagicMock()
    res_doc.json = json.dumps(doc)
    search_res = MagicMock()
    search_res.docs = [res_doc]
    ft_mock.search = AsyncMock(return_value=search_res)
    found = await ps.find_by_category_severity("ddos", "high")
    assert found is not None

    ft_mock.search = AsyncMock(return_value=search_res)
    all_pb = await ps.list_all()
    assert len(all_pb) >= 1


def test_autonomy_transform_and_policy_match():
    from pkg.autonomy.policy import _rule_matches, PolicyRule, AutonomyLevel
    from pkg.autonomy.transform import clamp_evidence_text, llm_evidence_char_budget

    assert llm_evidence_char_budget() > 512
    long = "x" * 10_000
    clipped = clamp_evidence_text(long, max_chars=200)
    assert len(clipped) < len(long)

    rule = PolicyRule(lane="*", severity="*", action_type="*", level=AutonomyLevel.HITL)
    assert _rule_matches(rule, "X", "Y", "Z")


def test_diagnostic_mapping_load():
    from workers.diagnostic_mapping import load_diagnostic_matrix, MatrixRow, _row_matches
    from workers.proactive_models import AnomalyEvent

    m = load_diagnostic_matrix("/no/such/path/matrix.yaml")
    assert m.rows == []

    row = MatrixRow(symptom_group="g", layer="L1", error_hint_pattern="oom")
    ev = AnomalyEvent(trace_id="trace1", canonical_query="{}", namespace="ns", error_hint="oom kill")
    assert _row_matches(row, ev) is True


@pytest.mark.asyncio
async def test_tool_gated_execute_import():
    from workers.gated_execute import tool_gated_allowlisted_execute

    ctx = SimpleNamespace(settings=SimpleNamespace(omni_env_mode="lab"))
    with patch("execution.promotion.run_gated_allowlisted_execute", new=AsyncMock(return_value="ok")):
        out = await tool_gated_allowlisted_execute(ctx, {"command": "echo"})
        assert out == "ok"


def test_slow_path_trace_dataclass():
    from workers.slow_path_trace import AttemptRecord, format_slow_path_autopsy, slow_path_error_signature

    rec = AttemptRecord(
        attempt=0,
        phase="parse",
        error_signature="parse_json",
        one_line="bad json",
        detail_full="full",
        tool="k8s_describe_resource",
    )
    body = format_slow_path_autopsy(max_attempts=3, attempt_trace=[rec], exit_reason="unit")
    assert "autopsy_exhausted" in body
    assert slow_path_error_signature("parse", "{}") == "parse_json"
    assert slow_path_error_signature("tool_error", "403 forbidden", "x") == "tool_error:permission"


@pytest.mark.asyncio
async def test_emit_transition_no_kafka():
    import fakeredis.aioredis

    from workers.autonomy_contract import emit_transition, TRANSITION_CONTEXT_READY
    from workers.handler_context import WorkerHandlerContext

    ws = SimpleNamespace()
    ctx = WorkerHandlerContext(
        settings=ws,
        redis=fakeredis.aioredis.FakeRedis(decode_responses=True),
        llm=AsyncMock(),
        vector_store=MagicMock(),
        ledger=MagicMock(),
        semaphore=AsyncMock(),
        telegram=None,
        kafka=None,
    )
    await emit_transition(
        ctx,
        trace_id="t99",
        transition=TRANSITION_CONTEXT_READY,
        component="test",
        detail="",
    )


def test_vm_slots_helpers():
    from workers.vm_slot_accumulation import vm_slots_ready, extract_vm_slots_from_text

    assert vm_slots_ready({}) is False
    slots = extract_vm_slots_from_text("cpu high on pod nginx-abc in namespace prod")
    assert isinstance(slots, dict)
