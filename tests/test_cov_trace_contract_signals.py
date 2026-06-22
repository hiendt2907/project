"""Coverage tests for:
  - workers/autonomy_contract.py
  - pkg/trace_orchestrator/state.py, learning.py, candidates.py
  - pkg/reasoning/evidence_signals.py
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fakeredis.aioredis import FakeRedis


# ── autonomy_contract ──────────────────────────────────────────────────────────

class TestAutonomyContract:
    @pytest.mark.asyncio
    async def test_emit_transition_no_trace_id(self):
        from workers.autonomy_contract import emit_transition
        ctx = SimpleNamespace(redis=None, kafka=None)
        await emit_transition(ctx, trace_id="", transition="INGESTED")  # no error

    @pytest.mark.asyncio
    async def test_emit_transition_with_redis(self):
        from workers.autonomy_contract import emit_transition
        r = FakeRedis(decode_responses=True)
        ctx = SimpleNamespace(redis=r, kafka=None)
        await emit_transition(ctx, trace_id="t-001", transition="INGESTED", status="ok",
                              component="analyst", detail="started")

    @pytest.mark.asyncio
    async def test_emit_transition_with_kafka(self):
        from workers.autonomy_contract import emit_transition
        r = FakeRedis(decode_responses=True)
        mock_kafka = AsyncMock()
        mock_kafka.send_dict = AsyncMock()
        ws = SimpleNamespace(kafka_topic_audit_agent="omni-audit", kafka_topic_dlq="omni-dlq")
        ctx = SimpleNamespace(redis=r, kafka=mock_kafka, settings=ws)
        await emit_transition(ctx, trace_id="t-002", transition="DIAGNOSED")
        mock_kafka.send_dict.assert_awaited()

    @pytest.mark.asyncio
    async def test_emit_transition_with_meta(self):
        from workers.autonomy_contract import emit_transition
        r = FakeRedis(decode_responses=True)
        ctx = SimpleNamespace(redis=r, kafka=None)
        await emit_transition(ctx, trace_id="t-003", transition="EXECUTED",
                              meta={"tool": "kubectl_restart"})

    @pytest.mark.asyncio
    async def test_emit_terminal_tombstone_no_trace_id(self):
        from workers.autonomy_contract import emit_terminal_tombstone
        ctx = SimpleNamespace(redis=None, kafka=None)
        await emit_terminal_tombstone(ctx, trace_id="", reason_code="NO_PROOF", component="analyst")

    @pytest.mark.asyncio
    async def test_emit_terminal_tombstone_with_redis(self):
        from workers.autonomy_contract import emit_terminal_tombstone
        r = FakeRedis(decode_responses=True)
        ctx = SimpleNamespace(redis=r, kafka=None)
        await emit_terminal_tombstone(ctx, trace_id="t-004", reason_code="TIMEOUT",
                                      component="executor", detail="timed out", meta={"seq": 5})
        val = await r.get("omni:autonomous:terminal:t-004")
        assert val is not None
        obj = json.loads(val)
        assert obj["reason_code"] == "TIMEOUT"

    @pytest.mark.asyncio
    async def test_emit_terminal_tombstone_with_kafka(self):
        from workers.autonomy_contract import emit_terminal_tombstone
        r = FakeRedis(decode_responses=True)
        mock_kafka = AsyncMock()
        mock_kafka.send_dict = AsyncMock()
        ws = SimpleNamespace(kafka_topic_audit_agent="omni-audit", kafka_topic_dlq="omni-dlq")
        ctx = SimpleNamespace(redis=r, kafka=mock_kafka, settings=ws)
        await emit_terminal_tombstone(ctx, trace_id="t-005", reason_code="NO_PROOF",
                                      component="analyst")
        mock_kafka.send_dict.assert_awaited()


# ── trace_orchestrator/state.py ────────────────────────────────────────────────

class TestTraceOrchestratorState:
    @pytest.mark.asyncio
    async def test_load_returns_none_when_missing(self):
        from pkg.trace_orchestrator.state import load_trace_orchestrator_state
        r = FakeRedis(decode_responses=True)
        result = await load_trace_orchestrator_state(r, "no-such-trace")
        assert result is None

    @pytest.mark.asyncio
    async def test_save_and_load_roundtrip(self):
        from pkg.trace_orchestrator.state import (
            TraceOrchestratorState, save_trace_orchestrator_state, load_trace_orchestrator_state
        )
        r = FakeRedis(decode_responses=True)
        state = TraceOrchestratorState(trace_id="t-abc", rag_candidate_ids=["c1", "c2"])
        ok = await save_trace_orchestrator_state(r, state)
        assert ok is True
        loaded = await load_trace_orchestrator_state(r, "t-abc")
        assert loaded is not None
        assert loaded.trace_id == "t-abc"
        assert loaded.rag_candidate_ids == ["c1", "c2"]

    @pytest.mark.asyncio
    async def test_load_corrupt_json_returns_none(self):
        from pkg.trace_orchestrator.state import (
            load_trace_orchestrator_state, redis_key_trace_orchestrator
        )
        r = FakeRedis(decode_responses=True)
        key = redis_key_trace_orchestrator("t-bad")
        await r.setex(key, 3600, "not-valid-json{")
        result = await load_trace_orchestrator_state(r, "t-bad")
        assert result is None

    @pytest.mark.asyncio
    async def test_load_non_dict_json_returns_none(self):
        from pkg.trace_orchestrator.state import (
            load_trace_orchestrator_state, redis_key_trace_orchestrator
        )
        r = FakeRedis(decode_responses=True)
        key = redis_key_trace_orchestrator("t-list")
        await r.setex(key, 3600, json.dumps(["not", "a", "dict"]))
        result = await load_trace_orchestrator_state(r, "t-list")
        assert result is None

    @pytest.mark.asyncio
    async def test_from_dict_unknown_phase_defaults(self):
        from pkg.trace_orchestrator.state import TraceOrchestratorState, TraceOrchestratorPhase
        state = TraceOrchestratorState.from_dict({
            "trace_id": "t-x",
            "phase": "INVALID_PHASE",
        })
        assert state.phase == TraceOrchestratorPhase.RAG_TRIALS

    @pytest.mark.asyncio
    async def test_mark_resolved_no_existing_state(self):
        from pkg.trace_orchestrator.state import mark_trace_orchestrator_resolved_verified
        r = FakeRedis(decode_responses=True)
        result = await mark_trace_orchestrator_resolved_verified(r, "t-new")
        assert result is True

    @pytest.mark.asyncio
    async def test_mark_resolved_existing_state(self):
        from pkg.trace_orchestrator.state import (
            TraceOrchestratorState, TraceOrchestratorPhase,
            save_trace_orchestrator_state, mark_trace_orchestrator_resolved_verified,
            load_trace_orchestrator_state
        )
        r = FakeRedis(decode_responses=True)
        st = TraceOrchestratorState(trace_id="t-res")
        await save_trace_orchestrator_state(r, st)
        ok = await mark_trace_orchestrator_resolved_verified(r, "t-res")
        assert ok is True
        loaded = await load_trace_orchestrator_state(r, "t-res")
        assert loaded.phase == TraceOrchestratorPhase.RESOLVED
        assert loaded.last_verify_ok is True

    @pytest.mark.asyncio
    async def test_mark_resolved_empty_trace_id(self):
        from pkg.trace_orchestrator.state import mark_trace_orchestrator_resolved_verified
        r = FakeRedis(decode_responses=True)
        result = await mark_trace_orchestrator_resolved_verified(r, "")
        assert result is False


# ── trace_orchestrator/learning.py ────────────────────────────────────────────

class TestTraceOrchestratorLearning:
    @pytest.mark.asyncio
    async def test_hook_no_settings(self):
        from pkg.trace_orchestrator.learning import on_verified_resolve_hook
        ctx = SimpleNamespace()  # no settings attribute
        await on_verified_resolve_hook(ctx, trace_id="t", tool_name="kubectl")  # no error

    @pytest.mark.asyncio
    async def test_hook_disabled(self):
        from pkg.trace_orchestrator.learning import on_verified_resolve_hook
        ws = SimpleNamespace(action_experience_enabled=False)
        ctx = SimpleNamespace(settings=ws)
        await on_verified_resolve_hook(ctx, trace_id="t", tool_name="kubectl")  # returns early

    @pytest.mark.asyncio
    async def test_hook_enabled(self):
        from pkg.trace_orchestrator.learning import on_verified_resolve_hook
        ws = SimpleNamespace(action_experience_enabled=True)
        ctx = SimpleNamespace(settings=ws)
        await on_verified_resolve_hook(ctx, trace_id="t-abc", tool_name="kubectl",
                                       summary="restart succeeded")


# ── trace_orchestrator/candidates.py ──────────────────────────────────────────

class TestCandidates:
    def test_merge_skips_none_id(self):
        from pkg.trace_orchestrator.candidates import merge_ranked_candidate_rows
        rows = [{"score": 0.9}, {"id": "c1", "score": 0.8}]
        result = merge_ranked_candidate_rows(rows)
        assert len(result) == 1
        assert "c1" in result[0]

    def test_merge_uses_payload_id_fallback(self):
        from pkg.trace_orchestrator.candidates import merge_ranked_candidate_rows
        rows = [{"payload_id": "p1", "score": 0.7}]
        result = merge_ranked_candidate_rows(rows)
        assert len(result) == 1

    def test_merge_invalid_score_defaults_zero(self):
        from pkg.trace_orchestrator.candidates import merge_ranked_candidate_rows
        rows = [{"id": "c1", "score": "not-a-float"}, {"id": "c2", "score": 0.5}]
        result = merge_ranked_candidate_rows(rows)
        assert len(result) == 2
        assert result[0].endswith("c2")  # higher score first

    @pytest.mark.asyncio
    async def test_enqueue_and_dequeue(self):
        from pkg.trace_orchestrator.candidates import enqueue_rag_candidate
        from pkg.trace_orchestrator.state import TraceOrchestratorState
        st = TraceOrchestratorState(trace_id="t")
        enqueue_rag_candidate(st, "cid-1")
        enqueue_rag_candidate(st, "cid-1")  # duplicate ignored
        enqueue_rag_candidate(st, "")  # empty ignored
        assert st.rag_candidate_ids == ["cid-1"]


# ── evidence_signals.py ────────────────────────────────────────────────────────

class TestEvidenceSignals:
    def test_no_critical_signals(self):
        from pkg.reasoning.evidence_signals import critical_evidence_present
        batch = [{"alert_hint": "CPU usage normal", "result": "PASSED", "raw": ""}]
        assert not critical_evidence_present(batch)

    def test_crash_loop_in_hint(self):
        from pkg.reasoning.evidence_signals import critical_evidence_present
        batch = [{"alert_hint": "CrashLoopBackOff detected", "result": "FAILED", "raw": ""}]
        assert critical_evidence_present(batch)

    def test_oom_killed_in_raw(self):
        from pkg.reasoning.evidence_signals import critical_evidence_present
        batch = [{"alert_hint": "", "result": "FAILED", "raw": "OOMKilled process nginx"}]
        assert critical_evidence_present(batch)

    def test_extracted_fact_has_crash_loop(self):
        from pkg.reasoning.evidence_signals import critical_evidence_present
        batch = [{
            "alert_hint": "",
            "result": "FAILED",
            "raw": "",
            "extracted_fact": {"has_crash_loop": True},
        }]
        assert critical_evidence_present(batch)

    def test_extracted_fact_string_json(self):
        from pkg.reasoning.evidence_signals import critical_evidence_present
        batch = [{
            "alert_hint": "",
            "result": "FAILED",
            "raw": "",
            "extracted_fact": json.dumps({"has_oom_killed": True}),
        }]
        assert critical_evidence_present(batch)

    def test_pod_in_extracted_fact(self):
        from pkg.reasoning.evidence_signals import critical_evidence_present
        batch = [{
            "alert_hint": "",
            "result": "FAILED",
            "raw": "",
            "extracted_fact": {"pods": [{"has_crash_loop": True}]},
        }]
        assert critical_evidence_present(batch)

    def test_canonical_query_snippet_with_alertname(self):
        from pkg.reasoning.evidence_signals import critical_evidence_present
        snip = json.dumps({"labels": {"alertname": "CrashLoopBackOff", "reason": ""}})
        batch = [{"alert_hint": "", "result": "PASSED", "raw": "", "canonical_query_snippet": snip}]
        assert critical_evidence_present(batch)

    def test_extracted_fact_phase_not_running_ready_false(self):
        from pkg.reasoning.evidence_signals import critical_evidence_present
        batch = [{
            "alert_hint": "",
            "result": "FAILED",
            "raw": "",
            "extracted_fact": {"phase": "Pending", "ready_false": True},
        }]
        assert critical_evidence_present(batch)
