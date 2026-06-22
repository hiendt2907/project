"""Unit tests for trace orchestrator state + candidate merge."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from pkg.trace_orchestrator.candidates import (
    enqueue_rag_candidate,
    merge_ranked_candidate_rows,
    pop_next_untried_candidate,
)
from pkg.trace_orchestrator.state import (
    TraceOrchestratorPhase,
    TraceOrchestratorState,
    redis_key_trace_orchestrator,
)


def test_redis_key_format() -> None:
    assert redis_key_trace_orchestrator("t1") == "omni:trace_orchestrator:t1"


def test_state_roundtrip_dict() -> None:
    s = TraceOrchestratorState(
        trace_id="tr",
        phase=TraceOrchestratorPhase.LLM_TOOLS,
        rag_candidate_ids=["a:1", "b:2"],
        attempted_rag_ids=["a:1"],
        last_verify_ok=False,
        last_error="x",
    )
    d = s.to_dict()
    assert d["phase"] == "llm_tools"
    s2 = TraceOrchestratorState.from_dict(d)
    assert s2.trace_id == "tr"
    assert s2.phase == TraceOrchestratorPhase.LLM_TOOLS
    assert s2.rag_candidate_ids == ["a:1", "b:2"]
    assert s2.attempted_rag_ids == ["a:1"]
    assert s2.last_verify_ok is False
    assert s2.last_error == "x"


def test_merge_ranked_candidate_rows_orders_by_score() -> None:
    rows = [
        {"id": "low", "score": 0.1, "source": "sop"},
        {"id": "high", "score": 0.9, "source": "sop"},
        {"id": "high", "score": 0.9, "source": "sop"},
    ]
    out = merge_ranked_candidate_rows(rows)
    assert out[0] == "sop:high"
    assert out[1] == "sop:low"


def test_pop_next_untried() -> None:
    st = TraceOrchestratorState(trace_id="t")
    enqueue_rag_candidate(st, "p:a")
    enqueue_rag_candidate(st, "p:b")
    assert pop_next_untried_candidate(st) == "p:a"
    st.attempted_rag_ids.append("p:a")
    assert pop_next_untried_candidate(st) == "p:b"
    st.attempted_rag_ids.append("p:b")
    assert pop_next_untried_candidate(st) is None


@pytest.mark.asyncio
async def test_record_verify_failure_marks_attempted() -> None:
    from pkg.trace_orchestrator.candidates import record_verify_failure_for_candidate

    class FakeRedis:
        def __init__(self) -> None:
            self.last: tuple[str, int, str] | None = None

        async def setex(self, key: str, ttl: int, val: str) -> None:
            self.last = (key, ttl, val)

    r = FakeRedis()
    st = TraceOrchestratorState(trace_id="t1", rag_candidate_ids=["p:a"])
    await record_verify_failure_for_candidate(r, st, "p:a", detail="verify failed")
    assert "p:a" in st.attempted_rag_ids
    assert st.last_verify_ok is False
    assert r.last is not None


@pytest.mark.asyncio
async def test_record_verify_success_sets_resolved_phase() -> None:
    from pkg.trace_orchestrator.candidates import record_verify_success_for_candidate

    class FakeRedis:
        async def setex(self, key: str, ttl: int, val: str) -> None:
            pass

    st = TraceOrchestratorState(trace_id="t2", rag_candidate_ids=["p:b"])
    await record_verify_success_for_candidate(FakeRedis(), st, "p:b")
    assert st.phase == TraceOrchestratorPhase.RESOLVED
    assert st.last_verify_ok is True


@pytest.mark.asyncio
async def test_on_verified_resolve_hook_logs() -> None:
    from pkg.trace_orchestrator.learning import on_verified_resolve_hook

    class Ws:
        action_experience_enabled = True

    ctx = SimpleNamespace(settings=Ws())
    await on_verified_resolve_hook(ctx, trace_id="tr", tool_name="t", summary="ok")
    from pkg.trace_orchestrator.state import load_trace_orchestrator_state, save_trace_orchestrator_state

    class FakeRedis:
        def __init__(self) -> None:
            self._data: dict[str, str] = {}

        async def setex(self, key: str, ttl: int, val: str) -> None:
            self._data[key] = val

        async def get(self, key: str) -> str | None:
            return self._data.get(key)

    r = FakeRedis()
    s = TraceOrchestratorState(trace_id="abc", rag_candidate_ids=["playbook:1"])
    await save_trace_orchestrator_state(r, s, ttl_sec=120)
    loaded = await load_trace_orchestrator_state(r, "abc")
    assert loaded is not None
    assert loaded.trace_id == "abc"
    assert loaded.rag_candidate_ids == ["playbook:1"]
