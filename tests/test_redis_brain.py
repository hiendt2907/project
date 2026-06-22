"""Tests for the Redis second-brain multi-turn RAG loop."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fakeredis.aioredis import FakeRedis

import rag.redis_brain as _brain_mod
from rag.redis_brain import run_redis_brain, SESSION_KEY


def _pt(score: float, pid: str, payload: dict):
    return SimpleNamespace(score=score, id=pid, payload=payload)


def _resp(points):
    return SimpleNamespace(points=points)


# The brain embeds each turn query ONCE then reuses the vector across collections
# (embed-1x optimization). Tests stub the embed so they run without a live Ollama,
# and capture the per-turn query text here for the query-refinement assertions.
_EMBED_QUERIES: list[str] = []


@pytest.fixture(autouse=True)
def _stub_embed(monkeypatch):
    _EMBED_QUERIES.clear()

    async def _fake_embed(llm, query, embed_model):
        _EMBED_QUERIES.append(query)
        return [0.11, 0.22, 0.33]

    monkeypatch.setattr(_brain_mod, "_embed_query_once", _fake_embed)
    yield


class _FakeVS:
    """Scripted vector store: each search call pops the next scripted batch.

    Implements ``similarity_search_by_vector`` (the brain's actual call path after the
    embed-1x refactor) and keeps ``similarity_search`` for backward compatibility.
    """
    def __init__(self, batches: list[list]):
        self._batches = list(batches)
        self.calls: list[str] = []

    def _pop(self):
        if self._batches:
            return _resp(self._batches.pop(0))
        return _resp([])

    async def similarity_search_by_vector(self, vector, collection, **kw):
        return self._pop()

    async def similarity_search(self, query, collection_id, **kw):
        self.calls.append(query)
        return self._pop()


def _ctx(vs):
    return SimpleNamespace(
        vector_store=vs,
        llm=AsyncMock(),
        settings=SimpleNamespace(embed_model="nomic-embed-text"),
        redis=FakeRedis(decode_responses=True),
    )


@pytest.mark.asyncio
async def test_no_vector_store_returns_empty():
    ctx = SimpleNamespace(vector_store=None, llm=AsyncMock(), settings=SimpleNamespace(), redis=None)
    res = await run_redis_brain(ctx, trace="t1", initial_query="cpu high")
    assert res.confident is False
    assert res.turn_count == 0
    assert res.accumulated_context == ""


@pytest.mark.asyncio
async def test_confident_hit_short_circuits_first_turn():
    vs = _FakeVS([[_pt(0.91, "p1", {"advisory": "restart mysql; check OOM"})]])
    ctx = _ctx(vs)
    res = await run_redis_brain(ctx, trace="t-conf", initial_query="mysql failed OOM")
    assert res.confident is True
    assert res.turn_count == 1  # stopped immediately on strong hit
    assert "restart mysql" in res.answer
    assert res.answer_point_id == "p1"
    assert res.top_score == 0.91


@pytest.mark.asyncio
async def test_multi_turn_accumulates_context():
    # Turn 1: weak hits (one collection returns p1); subsequent collection calls empty.
    # Then turn 2 finds a new, slightly stronger hit. None reach confidence (0.85).
    vs = _FakeVS([
        [_pt(0.60, "p1", {"summary": "disk /var filling"})], [], [],   # turn 1 (3 collections)
        [_pt(0.70, "p2", {"summary": "wal cannot fsync"})], [], [],     # turn 2
        [], [], [],                                                     # turn 3 (nothing new)
    ])
    ctx = _ctx(vs)
    res = await run_redis_brain(ctx, trace="t-multi", initial_query="disk full db down")
    assert res.confident is False
    assert res.turn_count >= 2
    assert "disk /var filling" in res.accumulated_context
    assert "wal cannot fsync" in res.accumulated_context
    assert res.top_score == 0.70


@pytest.mark.asyncio
async def test_dedup_across_turns():
    # Same point_id returned again on turn 2 must not be double-counted.
    vs = _FakeVS([
        [_pt(0.60, "dup", {"summary": "same hit"})], [], [],
        [_pt(0.60, "dup", {"summary": "same hit"})], [], [],
        [], [], [],
    ])
    ctx = _ctx(vs)
    res = await run_redis_brain(ctx, trace="t-dup", initial_query="x")
    occurrences = res.accumulated_context.count("same hit")
    assert occurrences == 1


@pytest.mark.asyncio
async def test_session_persisted_to_redis():
    vs = _FakeVS([[_pt(0.92, "p1", {"advisory": "do the thing"})]])
    ctx = _ctx(vs)
    res = await run_redis_brain(ctx, trace="t-persist", initial_query="q")
    raw = await ctx.redis.get(SESSION_KEY.format(trace="t-persist"))
    assert raw is not None
    doc = json.loads(raw)
    assert doc["confident"] is True
    assert doc["turn_count"] == 1
    assert doc["turns"][0]["hits"][0]["point_id"] == "p1"


@pytest.mark.asyncio
async def test_query_refines_across_turns():
    vs = _FakeVS([
        [_pt(0.60, "p1", {"summary": "first clue"})], [], [],
        [_pt(0.62, "p2", {"summary": "second clue"})], [], [],
        [], [], [],
    ])
    ctx = _ctx(vs)
    await run_redis_brain(ctx, trace="t-refine", initial_query="ALERT XYZ")
    # turn-2 query must carry the original anchor AND the learned snippet.
    turn2_queries = [q for q in _EMBED_QUERIES if "KNOWN SO FAR" in q]
    assert turn2_queries
    assert "ALERT XYZ" in turn2_queries[0]
    assert "first clue" in turn2_queries[0]


@pytest.mark.asyncio
async def test_proactive_fallback_noise_is_filtered():
    # A proactive-fallback 'restart pod' reflex entry must NOT be recalled as knowledge,
    # even when it scores above MIN_HIT_SCORE. A real entry alongside it is kept.
    noise = {"routing_source": "proactive_fallback",
             "symptom_text": "[proactive learning hit] tool=k8s_rollout_restart",
             "args": {"namespace": "<namespace>", "deployment": "<valid_deployment>"}}
    real = {"summary": "systemd unit failed; read journalctl -u for Result=oom-kill"}
    vs = _FakeVS([[_pt(0.71, "noise", noise), _pt(0.66, "real", real)], [], [], [], [], [], [], [], []])
    ctx = _ctx(vs)
    res = await run_redis_brain(ctx, trace="t-noise", initial_query="systemd unit failed")
    assert "proactive learning hit" not in res.accumulated_context
    assert "k8s_rollout_restart" not in res.accumulated_context
    assert "journalctl" in res.accumulated_context  # the real entry survives


@pytest.mark.asyncio
async def test_brain_writes_per_phase_logs():
    from rag.redis_brain import run_redis_brain
    vs = _FakeVS([[_pt(0.60, "p1", {"summary": "clue one"})], [], [], [], [], [], [], [], []])
    ctx = _ctx(vs)
    await run_redis_brain(ctx, trace="t-logs", initial_query="q")
    raw = await ctx.redis.lrange("omni:trace:logs:t-logs", 0, -1)
    assert any("2nd-brain turn 1" in r for r in raw)
