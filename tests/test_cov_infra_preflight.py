"""Tests for src/workers/infra_preflight.py — coverage of uncovered paths."""

from __future__ import annotations

import os

os.environ.setdefault("OMNI_KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
os.environ.setdefault("OMNI_REDIS_URL", "redis://localhost:6379")
os.environ.setdefault("OMNI_ENV_MODE", "dev")
os.environ.setdefault("OMNI_OLLAMA_BASE_URL", "http://localhost:11434")

import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from workers.infra_preflight import (
    LearnedContext,
    _apply_bypass_heuristic,
    _embedding_from_response,
    _merge_hints,
    preflight_infra_kb,
)


# ---------------------------------------------------------------------------
# _embedding_from_response
# ---------------------------------------------------------------------------


def test_embedding_from_response_embedding_key_list():
    resp = {"embedding": [0.1, 0.2, 0.3]}
    result = _embedding_from_response(resp)
    assert result == [0.1, 0.2, 0.3]


def test_embedding_from_response_embedding_key_tuple():
    resp = {"embedding": (0.1, 0.2)}
    result = _embedding_from_response(resp)
    assert result == [0.1, 0.2]


def test_embedding_from_response_embeddings_key():
    resp = {"embeddings": [[0.5, 0.6]]}
    result = _embedding_from_response(resp)
    assert result == [0.5, 0.6]


def test_embedding_from_response_missing_raises():
    with pytest.raises(ValueError, match="missing embedding"):
        _embedding_from_response({})


def test_embedding_from_response_empty_embeddings_raises():
    with pytest.raises(ValueError):
        _embedding_from_response({"embeddings": []})


# ---------------------------------------------------------------------------
# _apply_bypass_heuristic
# ---------------------------------------------------------------------------


def test_bypass_namespace_match():
    learned = LearnedContext(namespace="multi-agent")
    _apply_bypass_heuristic("alert in multi-agent namespace", learned)
    assert learned.clarification_bypass is True


def test_bypass_namespace_underscore_normalized():
    learned = LearnedContext(namespace="multi-agent")
    _apply_bypass_heuristic("multi_agent incident", learned)
    assert learned.clarification_bypass is True


def test_bypass_matched_token():
    learned = LearnedContext(matched_token="redis")
    _apply_bypass_heuristic("redis is down", learned)
    assert learned.clarification_bypass is True


def test_bypass_pod_name():
    learned = LearnedContext(pod_name="my-pod-abc")
    _apply_bypass_heuristic("pod my-pod-abc is crashing", learned)
    assert learned.clarification_bypass is True


def test_bypass_service_name():
    learned = LearnedContext(service_name="frontend-svc")
    _apply_bypass_heuristic("frontend-svc latency spike", learned)
    assert learned.clarification_bypass is True


def test_bypass_no_match():
    learned = LearnedContext(namespace="prod")
    _apply_bypass_heuristic("unrelated text", learned)
    assert learned.clarification_bypass is False


def test_bypass_empty_learned():
    learned = LearnedContext()
    _apply_bypass_heuristic("some text", learned)
    assert learned.clarification_bypass is False


# ---------------------------------------------------------------------------
# _merge_hints
# ---------------------------------------------------------------------------


def test_merge_hints_sets_namespace():
    learned = LearnedContext()
    _merge_hints(learned, {"namespace": "staging"})
    assert learned.namespace == "staging"
    assert learned.matched_token == "staging"


def test_merge_hints_sets_pod_via_pod_name():
    learned = LearnedContext()
    _merge_hints(learned, {"pod_name": "my-pod"})
    assert learned.pod_name == "my-pod"


def test_merge_hints_sets_pod_via_pod_key():
    learned = LearnedContext()
    _merge_hints(learned, {"pod": "my-pod-2"})
    assert learned.pod_name == "my-pod-2"


def test_merge_hints_sets_service():
    learned = LearnedContext()
    _merge_hints(learned, {"service_name": "api-svc"})
    assert learned.service_name == "api-svc"


def test_merge_hints_does_not_overwrite_existing():
    learned = LearnedContext(namespace="existing-ns")
    _merge_hints(learned, {"namespace": "new-ns"})
    assert learned.namespace == "existing-ns"


def test_merge_hints_none_is_noop():
    learned = LearnedContext()
    _merge_hints(learned, None)
    assert learned.namespace is None


def test_merge_hints_empty_values_ignored():
    learned = LearnedContext()
    _merge_hints(learned, {"namespace": "  ", "pod_name": ""})
    assert learned.namespace is None
    assert learned.pod_name is None


# ---------------------------------------------------------------------------
# preflight_infra_kb — async paths
# ---------------------------------------------------------------------------


def _make_ctx(embed_return=None, vector_store_points=None, extra_settings=None):
    settings = SimpleNamespace(
        embed_model="nomic-embed-text:latest",
        pgvector_collection_k8s_expert=None,
    )
    if extra_settings:
        for k, v in extra_settings.items():
            setattr(settings, k, v)

    llm = AsyncMock()
    llm.embed.return_value = embed_return or {"embedding": [0.1] * 8}

    vector_store = AsyncMock()
    points_obj = SimpleNamespace(points=vector_store_points or [])
    vector_store.query_points.return_value = points_obj

    ctx = SimpleNamespace(settings=settings, llm=llm, vector_store=vector_store)
    return ctx


async def test_preflight_empty_text_returns_empty():
    ctx = _make_ctx()
    result = await preflight_infra_kb(ctx, "")
    assert isinstance(result, LearnedContext)
    assert result.namespace is None
    assert result.had_vector_search is False


async def test_preflight_with_hints_returns_early():
    ctx = _make_ctx()
    result = await preflight_infra_kb(ctx, "some text", hints={"namespace": "prod", "pod_name": "app-pod"})
    assert result.namespace == "prod"
    assert result.pod_name == "app-pod"
    # namespace was found, returns early without vector search
    assert result.had_vector_search is False


async def test_preflight_short_text_returns_early():
    ctx = _make_ctx()
    # < 8 chars, no namespace
    result = await preflight_infra_kb(ctx, "abc")
    assert result.had_vector_search is False


async def test_preflight_ambiguous_cpu_ram_no_namespace_returns_early():
    ctx = _make_ctx()
    with patch("workers.infra_preflight.is_scope_ambiguous_cpu_ram", return_value=True):
        result = await preflight_infra_kb(ctx, "cpu usage is high everywhere")
    assert result.had_vector_search is False


async def test_preflight_vector_search_success():
    point = SimpleNamespace(
        score=0.70,
        payload={"text": "some topology chunk", "namespace": "ns-from-vector"},
    )
    ctx = _make_ctx(vector_store_points=[point])

    with patch("workers.infra_preflight.is_scope_ambiguous_cpu_ram", return_value=False):
        result = await preflight_infra_kb(ctx, "redis OOM in production cluster")

    assert result.had_vector_search is True
    assert len(result.infra_blocks) > 0


async def test_preflight_vector_search_extracts_namespace_from_infra_topology():
    from rag.pgvector_store import COLLECTION_INFRA_TOPOLOGY

    # First query (k8s_expert) returns empty, second (infra_topology) returns hit
    point = SimpleNamespace(
        score=0.65,  # >= 0.58
        payload={"text": "infra block text", "namespace": "my-ns", "pod_name": "my-pod", "service_name": "my-svc"},
    )

    call_count = 0

    async def fake_query_points(collection_name, query, limit, score_threshold, with_payload):
        nonlocal call_count
        call_count += 1
        if collection_name == COLLECTION_INFRA_TOPOLOGY:
            return SimpleNamespace(points=[point])
        return SimpleNamespace(points=[])

    ctx = _make_ctx()
    ctx.vector_store.query_points.side_effect = fake_query_points

    with patch("workers.infra_preflight.is_scope_ambiguous_cpu_ram", return_value=False):
        result = await preflight_infra_kb(ctx, "something crashed in the cluster")

    assert result.had_vector_search is True
    assert result.namespace == "my-ns"
    assert result.pod_name == "my-pod"
    assert result.service_name == "my-svc"


async def test_preflight_vector_search_low_score_skips_namespace_extraction():
    from rag.pgvector_store import COLLECTION_INFRA_TOPOLOGY

    point = SimpleNamespace(
        score=0.55,  # < 0.58, should NOT extract namespace
        payload={"text": "some block", "namespace": "should-not-be-set"},
    )

    async def fake_query_points(collection_name, query, limit, score_threshold, with_payload):
        if collection_name == COLLECTION_INFRA_TOPOLOGY:
            return SimpleNamespace(points=[point])
        return SimpleNamespace(points=[])

    ctx = _make_ctx()
    ctx.vector_store.query_points.side_effect = fake_query_points

    with patch("workers.infra_preflight.is_scope_ambiguous_cpu_ram", return_value=False):
        result = await preflight_infra_kb(ctx, "something happened in production")

    assert result.namespace is None  # not extracted because score < 0.58


async def test_preflight_embed_failure_returns_empty_context():
    ctx = _make_ctx()
    ctx.llm.embed.side_effect = Exception("LLM unavailable")

    with patch("workers.infra_preflight.is_scope_ambiguous_cpu_ram", return_value=False):
        result = await preflight_infra_kb(ctx, "some long enough text to trigger embed")

    assert result.had_vector_search is False


async def test_preflight_vector_query_exception_continues():
    ctx = _make_ctx()
    ctx.vector_store.query_points.side_effect = Exception("vector DB error")

    with patch("workers.infra_preflight.is_scope_ambiguous_cpu_ram", return_value=False):
        result = await preflight_infra_kb(ctx, "something failed in the cluster")

    # Embed succeeded, but vector query failed — should still mark had_vector_search
    assert result.had_vector_search is True
    assert result.infra_blocks == []


async def test_preflight_custom_expert_collection():
    ctx = _make_ctx(extra_settings={"pgvector_collection_k8s_expert": "custom_expert_coll"})
    ctx.vector_store.query_points.return_value = SimpleNamespace(points=[])

    with patch("workers.infra_preflight.is_scope_ambiguous_cpu_ram", return_value=False):
        await preflight_infra_kb(ctx, "some query about crash loop")

    called_collections = [call.kwargs["collection_name"] for call in ctx.vector_store.query_points.call_args_list]
    assert "custom_expert_coll" in called_collections


async def test_preflight_point_missing_text_payload_skipped():
    point = SimpleNamespace(score=0.75, payload={"namespace": "ns"})  # no text/summary

    ctx = _make_ctx(vector_store_points=[point])

    with patch("workers.infra_preflight.is_scope_ambiguous_cpu_ram", return_value=False):
        result = await preflight_infra_kb(ctx, "some cluster crash event")

    # block should NOT be appended since chunk is empty
    assert all("CONTEXT:" not in b or "alert_or_hints" in b for b in result.infra_blocks)


async def test_preflight_hint_adds_context_block():
    ctx = _make_ctx()
    result = await preflight_infra_kb(
        ctx,
        "some alert text",
        hints={"namespace": "prod", "pod_name": "api-xyz"},
    )
    assert any("alert_or_hints" in b for b in result.infra_blocks)
