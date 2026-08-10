"""Unit tests for evidence_mutate_emit, autonomous_feedback_loop helpers, and extra evidence_consumer branches."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from workers.handler_context import WorkerHandlerContext


def _ws(**kwargs):
    base = dict(
        omni_auto_execute_enabled=False,
        omni_hitl_routing_enabled=False,
        omni_siem_suggest_only=True,
        kafka_topic_actions="omni-actions",
        kafka_topic_hitl_pending="omni-hitl-pending",
        kafka_topic_audit_chain="omni-audit-chain",
        kafka_topic_audit_agent="omni-audit-agent",
        kafka_bootstrap_servers="localhost:9092",
        consumer_group_analyst_feedback="cg-fb",
        consumer_name_analyst="analyst-test",
        rag_hot_cache_ttl_sec=3600,
        embed_model="nomic-embed-text:latest",
        chat_model="qwen2.5:7b",
        diag_evidence_llm_model="qwen2.5:7b",
        autonomous_execute_max_attempts=3,
        autonomous_verify_max_rounds=5,
        omni_post_mutate_sdk_verify_enabled=False,
        omni_post_mutate_verify_planner_enabled=False,
        omni_experience_requires_sdk_verify=False,
        omni_post_verify_state_llm_enabled=False,
        omni_post_verify_deployment_state_enabled=False,
        omni_telegram_suppress_when_deployment_healthy=False,
        lab_chaos_credential_autofix_enabled=False,
        omni_feedback_full_agentic_planner_enabled=False,
        omni_llm_first_autonomy_enabled=False,
        omni_legacy_deterministic_fallback=True,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def _handler_ctx(**ws_kwargs):
    ws = _ws(**ws_kwargs)
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


def _fakeredis():
    import fakeredis.aioredis

    return fakeredis.aioredis.FakeRedis(decode_responses=True)


# --- evidence_mutate_emit ---


def test_verify_probe_ids_from_batch_and_symptom():
    from workers import evidence_mutate_emit as eme
    from workers.diagnostic_dispatcher import probe_ids_for_alertname

    assert eme._verify_probe_ids_from_batch([{"probe": "a"}, {"probe": "a"}, {"probe": "b"}]) == ["a", "b"]
    with patch("workers.evidence_mutate_emit.alertname_from_batch", return_value="KubePodCrashLooping"):
        got = eme._verify_probe_ids_from_batch([])
        assert got == probe_ids_for_alertname("KubePodCrashLooping")
    assert eme._symptom_group_from_batch([{"symptom_group": "  cpu "}, {"symptom_group": "y"}]) == "cpu"
    assert eme._symptom_group_from_batch([{"x": 1}]) == ""


def test_deployment_name_and_rollout_args():
    from workers import evidence_mutate_emit as eme
    from pkg.reasoning.rollout_eligibility import _deployment_name_from_alert_labels

    assert _deployment_name_from_alert_labels({"deployment": " d1 "}) == "d1"
    assert _deployment_name_from_alert_labels({"workload": "w1"}) == "w1"
    assert _deployment_name_from_alert_labels({}) == ""

    batch_bad = [{"canonical_query_snippet": "not-json"}]
    assert eme.rollout_args_from_evidence_batch(batch_bad) is None
    batch_invalid = [{"canonical_query_snippet": '{"labels": "nope"}'}]
    assert eme.rollout_args_from_evidence_batch(batch_invalid) is None
    good = [
        {
            "canonical_query_snippet": json.dumps(
                {"labels": {"namespace": "ns1", "workload": "api", "alertname": "HighCPU"}}
            )
        }
    ]
    assert eme.rollout_args_from_evidence_batch(good) == {"namespace": "ns1", "deployment": "api"}


def test_should_try_rollout_and_rollout_flags():
    from workers import evidence_mutate_emit as eme

    assert eme.should_try_rollout_from_rag("kubectl_rollout_restart", "") is True
    assert eme.should_try_rollout_from_rag("x", "please rollout restart") is True
    assert eme.should_try_rollout_from_rag("describe", "ok") is False

    cpu_batch = [{"alert_hint": "HighCPU utilization spike"}]
    assert eme.workload_cpu_incident_rollout_eligible(cpu_batch) is True
    fault_batch = [{"alert_hint": "CrashLoopBackOff detected"}]
    assert eme.workload_fault_incident_rollout_eligible(fault_batch) is True

    rr = {"namespace": "a", "deployment": "b"}
    assert (
        eme.should_emit_rollout_after_rag(
            suggested_tool="",
            diag_snippet="",
            batch=cpu_batch,
            rr=rr,
            autonomous_rollout_on_cpu_incident=True,
        )
        is True
    )
    assert (
        eme.should_emit_rollout_after_rag(
            suggested_tool="",
            diag_snippet="",
            batch=[{"alert_hint": "nothing"}],
            rr=None,
            autonomous_rollout_on_cpu_incident=True,
        )
        is False
    )


@pytest.mark.asyncio
async def test_emit_execute_mutate_early_exit_and_redis_merge(monkeypatch):
    from workers import evidence_mutate_emit as eme

    async def audit_ok(**kwargs):
        return {"seq": 1}

    monkeypatch.setattr(eme, "write_audit_block", audit_ok)
    monkeypatch.setattr(
        "workers.telegram_escalation.emit_telegram_escalation",
        AsyncMock(),
    )

    ctx = _handler_ctx()
    ok = await eme.emit_execute_mutate(ctx, trace="t", tool_name="k8s_rollout_restart", args={"ns": "n"})
    assert ok is False

    kafka = AsyncMock()
    # Kill-switch producer 2026-07-31: emit chỉ ghi omni-actions khi switch BẬT.
    ws = _ws(omni_auto_execute_enabled=True)
    r_empty = _fakeredis()
    ctx2 = SimpleNamespace(settings=ws, kafka=kafka, redis=r_empty)
    ok2 = await eme.emit_execute_mutate(
        ctx2, trace="t2", tool_name="k8s_rollout_restart", args={"namespace": "x"}
    )
    assert ok2 is True
    kafka.send_dict.assert_awaited()

    r = _fakeredis()
    prev = json.dumps(
        {"feedback_failures": 2, "sdk_verify_round": 1, "state_verify_attempt": 3},
        ensure_ascii=False,
    )
    await r.set("omni:autonomous:state:t3", prev)
    ctx3 = SimpleNamespace(settings=ws, kafka=kafka, redis=r)
    ok3 = await eme.emit_execute_mutate(ctx3, trace="t3", tool_name="x", args={}, attempt_count=2)
    assert ok3 is True
    raw = await r.get("omni:autonomous:state:t3")
    saved = json.loads(raw)
    assert saved["last_attempt_count"] == 2
    assert saved["feedback_failures"] == 2

    kafka_exc = AsyncMock()
    kafka_exc.send_dict = AsyncMock(side_effect=RuntimeError("broker down"))
    r4 = _fakeredis()
    ctx4 = SimpleNamespace(settings=ws, kafka=kafka_exc, redis=r4)
    ok4 = await eme.emit_execute_mutate(ctx4, trace="t4", tool_name="x", args={})
    assert ok4 is False


@pytest.mark.asyncio
async def test_emit_hitl_pending_when_auto_execute_enabled():
    from workers import evidence_mutate_emit as eme

    kafka = AsyncMock()
    r = _fakeredis()
    ws = _ws(omni_auto_execute_enabled=True)
    ws.kafka_topic_hitl_pending = "omni-hitl-pending"
    ctx = SimpleNamespace(settings=ws, kafka=kafka, redis=r)
    batch = [
        {
            "canonical_query_snippet": json.dumps(
                {
                    "labels": {
                        "siem_source": "finguard",
                        "siem_incident_id": "INC1",
                        "siem_tenant": "t1",
                        "siem_category": "ddos",
                    }
                }
            )
        }
    ]
    await eme.emit_hitl_pending(
        ctx,
        trace="tr-hitl",
        tool_name="k8s_patch_resource",
        args={"namespace": "n"},
        batch=batch,
        explain="because",
        advise="careful",
    )
    kafka.send_dict.assert_awaited()
    st = await r.get("omni:hitl:state:tr-hitl")
    assert st and "PENDING_APPROVAL" in st


@pytest.mark.asyncio
async def test_emit_hitl_pending_writes_postgres_pending_row():
    """Đ49 B1 — tái hiện sống trên UAT: emit_hitl_pending gửi Kafka/Redis nhưng
    KHÔNG ghi omni_admin.hitl_decision (bảng vĩnh viễn 0 dòng dù CRAT ghi
    MUTATION_ENQUEUED thật), vì consumer duy nhất của omni-hitl-pending
    (hitl_dispatcher.py) không được đăng ký trong worker loop nào. Cùng lớp bug
    #27 đã vá ở hitl_telegram.py::open_hitl_pending_for_mutate — vá bằng cách
    ghi trực tiếp tại nguồn thay vì phụ thuộc consumer chết."""
    from workers import evidence_mutate_emit as eme

    kafka = AsyncMock()
    r = _fakeredis()
    ws = _ws(omni_auto_execute_enabled=True)
    ws.kafka_topic_hitl_pending = "omni-hitl-pending"

    class _FakeAdminRepo:
        def __init__(self) -> None:
            self.created: list[dict] = []

        async def create_hitl_pending(self, **kwargs):
            self.created.append(kwargs)

    admin_repo = _FakeAdminRepo()
    ctx = SimpleNamespace(settings=ws, kafka=kafka, redis=r, admin_repo=admin_repo)
    batch = [
        {
            "canonical_query_snippet": json.dumps(
                {"labels": {"siem_source": "finguard", "siem_tenant": "t1"}}
            )
        }
    ]
    await eme.emit_hitl_pending(
        ctx,
        trace="tr-hitl-pg",
        tool_name="human_escalation",
        args={},
        batch=batch,
        hitl_reason="siem_critical_action_requires_approval",
    )
    assert admin_repo.created == [{
        "pending_id": "mut-tr-hitl-pg",
        "tenant_id": "t1",
        "tool_name": "human_escalation",
        "risk_class": "HIGH",
        "tier_at_time": "siem_critical_action_requires_approval",
    }]


def test_siem_hitl_required_and_labels():
    from workers import evidence_mutate_emit as eme

    b = [
        {
            "canonical_query_snippet": json.dumps(
                {"labels": {"siem_hitl_required": "true", "siem_source": "finguard"}}
            )
        }
    ]
    assert eme._siem_hitl_required(b) is True
    assert eme._siem_alert_labels(b).get("siem_hitl_required") == "true"
    assert eme._siem_alert_labels([{"canonical_query_snippet": '{"labels": {"siem_source": "other"}}'}]) == {}


@pytest.mark.asyncio
async def test_store_autonomous_trace_context_minimal_and_rollout():
    from workers import evidence_mutate_emit as eme

    r = _fakeredis()
    await eme.store_autonomous_trace_context(r, "trace-a", batch=None, sanitized_text="hello")
    blob = json.loads(await r.get("omni:autonomous:ctx:trace-a"))
    assert blob["sanitized_text"] == "hello"

    batch = [
        {
            "probe": "prometheus_instant",
            "canonical_query_snippet": json.dumps(
                {
                    "labels": {
                        "namespace": "nsx",
                        "deployment": "depy",
                        "siem_source": "finguard",
                        "omni.io/layer": "workload",
                        "omni.io/symptom-group": "cpu",
                        "omni_verify_required": "false",
                    }
                }
            ),
            "symptom_group": "cpu",
        }
    ]
    with (
        patch("workers.evidence_mutate_emit.anomaly_event_dict_from_evidence_batch", return_value={"x": 1}),
        patch("workers.evidence_mutate_emit.alertname_from_batch", return_value="HighCPU"),
    ):
        await eme.store_autonomous_trace_context(r, "trace-b", batch=batch, sanitized_text="s")
    blob2 = json.loads(await r.get("omni:autonomous:ctx:trace-b"))
    assert blob2.get("rollout_ns_dep") == {"namespace": "nsx", "deployment": "depy"}
    assert blob2.get("omni.io/layer") == "workload"


# --- evidence_consumer extra branches ---


def test_build_sdk_fact_only_prompt_malformed_json_string():
    from workers.evidence_consumer import build_sdk_fact_only_prompt

    batch = [{"probe": "p", "extracted_fact": '{"broken": '}]
    out = build_sdk_fact_only_prompt(batch)
    assert "p" in out and "extracted_fact=" in out


def test_siem_diagnosis_string_extracted_fact_invalid_json():
    from workers.evidence_consumer import _siem_diagnosis_from_batch

    batch = [{"probe": "siem_incident_context", "extracted_fact": "not-json"}]
    text = _siem_diagnosis_from_batch(batch, {"siem_incident_id": "L1", "siem_category": "ddos"}, "")
    assert "L1" in text and "WHAT:" in text


@pytest.mark.asyncio
async def test_notify_siem_telegram_send_error():
    from workers import evidence_consumer as ec

    tg = AsyncMock()
    tg.send_message = AsyncMock(side_effect=RuntimeError("telegram down"))
    ctx = _handler_ctx()
    ctx.telegram = tg
    ctx.settings.telegram_admin_chat_id = 1
    await ec._notify_siem_telegram(ctx, trace="t", batch=[], diagnosis="hello")


@pytest.mark.asyncio
async def test_proof_of_fault_gate_app_log_log_surge_bypass():
    from workers import evidence_consumer as ec
    from workers.log_surge_probe import LogSurgeResult

    ctx = _handler_ctx(omni_proof_lane_enabled=True, omni_sigma_log_bypass_enabled=True)
    r = _fakeredis()
    ctx.redis = r
    batch = [{"alert_hint": "CrashLoopBackOff", "probe": "k8s"}]
    res = LogSurgeResult(
        ok=True,
        reason="5xx",
        escalate_log_unavailable=False,
        meta={},
        dominant_error_class="5xx",
    )
    with (
        patch("workers.evidence_consumer.resolve_proof_lane", return_value=("app_log", "m")),
        patch("workers.evidence_consumer._f64", return_value=None),
        patch("workers.evidence_consumer.evaluate_log_surge_sigma_bypass", new=AsyncMock(return_value=res)),
        patch("workers.evidence_consumer.namespace_pod_from_batch", return_value=("ns", "pod1")),
        patch("workers.evidence_consumer.namespace_allowed", return_value=True),
        patch("pkg.reasoning.incident_matrix_profile.is_api_web_workload", return_value=True),
    ):
        ctx.settings.omni_loki_base_url = "http://loki"
        ok, _, meta = await ec._proof_of_fault_gate(ctx, trace="t-app", batch=batch, rag_match_text=None)
    assert ok is True
    assert meta.get("sigma_bypass_via_log_surge") is True


@pytest.mark.asyncio
async def test_planner_missing_preconditions_unknown_tool():
    from workers.evidence_consumer import _planner_missing_preconditions

    ctx = _handler_ctx(omni_planner_precondition_gate_enabled=True)
    ctx.redis = _fakeredis()
    with patch("workers.evidence_consumer.get_tool_registry") as gr:
        reg = MagicMock()
        reg.has = MagicMock(return_value=False)
        gr.return_value = reg
        out = await _planner_missing_preconditions(
            ctx, trace="t", tool_name="not_a_real_tool", args={}, discovery_steps=[], planner_missing=None
        )
    assert any("unknown_tool" in x for x in out)


@pytest.mark.asyncio
async def test_emit_suggest_os_runbook_validation_fail():
    from workers import evidence_consumer as ec

    ctx = _handler_ctx(trace_correlation_ping_enabled=True)
    ctx.kafka = AsyncMock()
    with patch("workers.evidence_consumer.validate_suggest_os_runbook_data", side_effect=ValueError("bad")):
        ok = await ec._emit_suggest_os_runbook(
            ctx,
            trace="tid",
            diagnosis="d",
            confidence=0.5,
            source="s",
            runbook_title="t",
            commands=[{"x": 1}],
        )
    assert ok is False
    ctx.kafka.send_dict.assert_not_awaited()


@pytest.mark.asyncio
async def test_emit_suggest_os_runbook_kafka_error():
    from workers import evidence_consumer as ec

    ctx = _handler_ctx(trace_correlation_ping_enabled=True)
    k = AsyncMock()
    k.send_dict = AsyncMock(side_effect=RuntimeError("kafka"))
    ctx.kafka = k
    with patch("workers.evidence_consumer.validate_suggest_os_runbook_data", return_value=None):
        ok = await ec._emit_suggest_os_runbook(
            ctx,
            trace="tid2",
            diagnosis="d",
            confidence=0.5,
            source="s",
            runbook_title="t",
            commands=[
                {
                    "purpose": "p",
                    "dry_run_command": "d",
                    "command": "c",
                    "target": "t",
                    "risk_level": "low",
                    "expected_output": "e",
                    "rollback_command": "r",
                    "timeout_sec": 1,
                    "evidence_refs": ["e"],
                }
            ],
        )
    assert ok is False


# --- autonomous_feedback_loop ---


@pytest.mark.asyncio
async def test_load_state_variants():
    from workers.autonomous_feedback_loop import _load_state

    r = _fakeredis()
    assert (await _load_state(r, "nope"))["feedback_failures"] == 0
    await r.set("omni:autonomous:state:x", "not-json{{{")
    st = await _load_state(r, "x")
    assert st["last_attempt_count"] == 0
    await r.set(
        "omni:autonomous:state:y",
        json.dumps({"last_attempt_count": 2, "feedback_failures": 1}, ensure_ascii=False),
    )
    st2 = await _load_state(r, "y")
    assert st2["last_attempt_count"] == 2 and st2.get("sdk_verify_round") == 0


@pytest.mark.asyncio
async def test_write_success_hot_cache_and_load_ctx_text():
    from workers.autonomous_feedback_loop import _load_autonomous_ctx_text, _write_success_hot_cache

    ctx = _handler_ctx()
    ctx.redis = _fakeredis()
    await _write_success_hot_cache(ctx, "ht", "stdout" * 10)
    raw = await ctx.redis.get("omni:autonomous:hot:ht")
    assert raw and "closed" in raw

    await ctx.redis.set(
        "omni:autonomous:ctx:zz",
        json.dumps({"sanitized_text": "body"}, ensure_ascii=False),
    )
    assert await _load_autonomous_ctx_text(ctx.redis, "zz") == "body"
    assert await _load_autonomous_ctx_text(ctx.redis, "missing") == ""


def test_args_hash_and_embedding_from_response():
    from workers.autonomous_feedback_loop import _args_hash, _embedding_from_response

    h = _args_hash({"b": 1, "a": 2})
    assert len(h) == 24
    assert _embedding_from_response({"embedding": [0.1, 0.2]}) == [0.1, 0.2]
    assert _embedding_from_response({"embeddings": [[1.0, 2.0]]}) == [1.0, 2.0]
    assert _embedding_from_response({}) == []


def test_anomaly_event_from_redis_ctx_and_initial_symptom():
    from workers.autonomous_feedback_loop import _anomaly_event_from_redis_ctx, _initial_symptom_from_ctx
    from workers.memory.initial_symptom import InitialSymptom

    assert _anomaly_event_from_redis_ctx("tr", None) is None
    assert _anomaly_event_from_redis_ctx("tr", {"anomaly_event_min": "bad"}) is None

    ev = _anomaly_event_from_redis_ctx(
        "trace-1234",
        {
            "anomaly_event_min": {
                "trace_id": "ignored",
                "canonical_query": "up",
                "namespace": "n",
            }
        },
    )
    assert ev is not None and ev.namespace == "n"

    sym = InitialSymptom(alertname="KubeOOM", namespace="ns1", summary="oom")
    got = _initial_symptom_from_ctx({"initial_symptom": sym.model_dump()})
    assert got is not None and got.alertname == "KubeOOM"
    assert _initial_symptom_from_ctx({"initial_symptom": {"alertname": []}}) is None


@pytest.mark.asyncio
async def test_upsert_action_experience_embed_dim_pad():
    from workers.autonomous_feedback_loop import _upsert_action_experience_on_success
    from rag.pgvector_store import EMBED_DIM

    class _StubLLM:
        async def embed(self, **kwargs):
            return {"embedding": [0.1] * 3}

    ctx = _handler_ctx()
    ctx.llm = _StubLLM()
    ctx.redis = _fakeredis()
    await ctx.redis.set(
        "omni:autonomous:ctx:uu",
        json.dumps({"sanitized_text": "symptom here"}, ensure_ascii=False),
    )
    ctx.vector_store.upsert = AsyncMock()
    await _upsert_action_experience_on_success(
        ctx,
        trace="uu",
        tool_name="k8s_rollout_restart",
        mutate_args={"namespace": "n"},
        stdout="out",
        sdk_verify_summary="",
        ctx_obj={"alertname": "X", "drift_type": "d"},
    )
    ctx.vector_store.upsert.assert_awaited()
    pts = ctx.vector_store.upsert.await_args.kwargs["points"]
    assert len(pts[0].vector) == EMBED_DIM


@pytest.mark.asyncio
async def test_llm_replan_branches():
    from workers.autonomous_feedback_loop import _llm_replan_after_feedback

    ctx = _handler_ctx()
    ctx.redis = _fakeredis()
    await ctx.redis.set(
        "omni:autonomous:ctx:rp",
        json.dumps({"sanitized_text": "ctx"}, ensure_ascii=False),
    )
    ctx.llm.chat = AsyncMock(return_value={"message": {"content": "not json"}})
    assert await _llm_replan_after_feedback(ctx, "rp", "o", "e", 1) is None

    ctx.llm.chat = AsyncMock(
        return_value={"message": {"content": '{"tool_name": "", "args": {}}'}}
    )
    with patch("workers.autonomous_feedback_loop._parse_tool_json", return_value={"tool_name": "", "args": {}}):
        plan = await _llm_replan_after_feedback(ctx, "rp", "o", "e", 1)
    assert plan == {"tool_name": "no_op", "args": {}}

    ctx.llm.chat = AsyncMock(
        side_effect=RuntimeError("llm down"),
    )
    assert await _llm_replan_after_feedback(ctx, "rp", "o", "e", 1) is None


@pytest.mark.asyncio
async def test_llm_post_verify_state_react_branches():
    from workers.autonomous_feedback_loop import _llm_post_verify_state_react

    ctx = _handler_ctx(omni_post_verify_state_llm_enabled=False)
    assert await _llm_post_verify_state_react(ctx, trace="t", namespace="n", deployment="d", verify_summary="", dep_detail="", stdout="", last_attempt=0) is False

    ctx2 = _handler_ctx(omni_post_verify_state_llm_enabled=True, autonomous_execute_max_attempts=1)
    ctx2.redis = _fakeredis()
    assert (
        await _llm_post_verify_state_react(
            ctx2, trace="t", namespace="n", deployment="d", verify_summary="", dep_detail="", stdout="", last_attempt=99
        )
        is False
    )


@pytest.mark.asyncio
async def test_finalize_if_deployment_rollout_healthy_flags():
    from workers.autonomous_feedback_loop import _finalize_if_deployment_rollout_healthy
    from workers.proactive_models import AnomalyEvent

    ctx = _handler_ctx(omni_post_verify_deployment_state_enabled=False)
    ev = AnomalyEvent(
        trace_id="abcd",
        canonical_query="up",
        namespace="n",
        deployment="d",
    )
    assert await _finalize_if_deployment_rollout_healthy(ctx, "t", body={}, mutate_args={}, ctx_obj={}, verify_summary="", stdout="", ev=ev, reason_tag="x") is False

    ctx2 = _handler_ctx(
        omni_post_verify_deployment_state_enabled=True,
        omni_telegram_suppress_when_deployment_healthy=True,
    )
    with patch(
        "workers.autonomous_feedback_loop.check_deployment_rollout_healthy",
        new=AsyncMock(return_value=(False, "not ready")),
    ):
        assert (
            await _finalize_if_deployment_rollout_healthy(
                ctx2, "t", body={"tool_name": "k8s_rollout_restart"}, mutate_args={}, ctx_obj={}, verify_summary="", stdout="", ev=ev, reason_tag="x"
            )
            is False
        )


@pytest.mark.asyncio
async def test_handle_action_feedback_envelope_skips_and_success_legacy():
    from workers.autonomous_feedback_loop import handle_action_feedback_envelope
    from rag.pgvector_store import EMBED_DIM

    class _StubLLM:
        async def embed(self, **kwargs):
            return {"embedding": [0.01] * EMBED_DIM}

    ctx = _handler_ctx()
    ctx.llm = _StubLLM()
    ctx.redis = _fakeredis()
    ctx.kafka = AsyncMock()
    await handle_action_feedback_envelope(ctx, {"data": "not-json"})
    await handle_action_feedback_envelope(ctx, {"data": "{}"})
    await handle_action_feedback_envelope(ctx, {"data": '{"trace_id":""}'})

    with (
        patch("workers.autonomous_feedback_loop.emit_transition", new=AsyncMock()),
        patch("workers.autonomous_feedback_loop.emit_terminal_tombstone", new=AsyncMock()),
        patch("workers.autonomous_feedback_loop.emit_telegram_escalation", new=AsyncMock()),
        patch("workers.autonomous_feedback_loop.write_incident_postmortem"),
        patch(
            "workers.autonomous_feedback_loop._verify_state_machine_gate",
            new=AsyncMock(return_value=(True, "ok")),
        ),
    ):
        body = {
            "trace_id": "trace-legacy-99",
            "exit_code": 0,
            "stdout": "done",
            "tool_name": "k8s_rollout_restart",
            "mutate_args": {},
        }
        await handle_action_feedback_envelope(ctx, {"data": json.dumps(body)})
    assert await ctx.redis.get("omni:autonomous:state:trace-legacy-99") is None

    skip_body = {
        "trace_id": "trace-skip-1",
        "exit_code": 0,
        "skipped_reason": "AUTO_EXECUTE disabled for lab",
        "stdout": "",
    }
    with patch("workers.autonomous_feedback_loop.emit_transition", new=AsyncMock()) as et:
        await handle_action_feedback_envelope(ctx, {"data": json.dumps(skip_body)})
    assert any(
        (getattr(c, "kwargs", None) or {}).get("transition") == "EXECUTED"
        for c in et.call_args_list
    )
