"""Unit tests for pkg.rag.gate pure helpers and evaluate_rag_gate (mocked vector store / redis)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pkg.rag import gate as rag_gate
from pkg.rag.embed_utils import truncate_for_embedding


def test_truncate_for_embedding_truncates_long_input() -> None:
    long = "w" * 5000
    out = truncate_for_embedding(long, max_tokens=256)
    assert len(out) < len(long)
    assert "truncated" in out.lower() or "…" in out


def test_clean_and_truncate_context_strips_noise_and_hints() -> None:
    raw = (
        "Status: 500\n"
        + "x" * 450
        + "\nTraceback (most recent call last):\n"
        + "  File \"/app/x.py\", line 1\n"
        + " normal  warning  error  something happened\n"
        + "Warning: disk full\n"
    )
    out = rag_gate.clean_and_truncate_context(
        raw,
        {"alertname": " HighCPU "},
        max_tokens=512,
    )
    assert "alert_name=HighCPU" in out
    assert "Status: 500" not in out
    assert "Traceback" not in out
    assert "…" in out or "truncated" in out.lower() or len(out) < len(raw)


def test_normalize_rag_query_merges_string_hints() -> None:
    hints = {
        "namespace": "ns1",
        "pod_name": "p1",
        "service_name": "svc",
        "alertname": "AlertX",
        "symptom_group": " cpu ",
        "diagnostic_pattern": " pat ",
    }
    q = rag_gate.normalize_rag_query("body line", hints)
    assert "namespace=ns1" in q
    assert "pod=p1" in q
    assert "service=svc" in q
    assert "alertname=AlertX" in q
    assert "symptom_group=cpu" in q
    assert "diagnostic_pattern=pat" in q
    assert "body line" in q


def test_normalize_rag_query_skips_non_string_namespace() -> None:
    hints: dict = {"namespace": 123, "pod": "pod-a", "alert_name": "A"}
    q = rag_gate.normalize_rag_query("x" * 20, hints)
    assert "namespace=" not in q
    assert "pod=pod-a" in q


@pytest.mark.parametrize(
    "dtype,expected_tool",
    [
        ("troubleshoot_guide", "kubectl_describe_pod"),
        ("reference_doc", "kubectl_get_events"),
        ("other", "kubectl_describe_pod"),
    ],
)
def test_primary_match_excerpt_tool(dtype: str, expected_tool: str) -> None:
    pt = SimpleNamespace(
        payload={
            "text": "  . Hello excerpt",
            "metadata": {"type": dtype},
        },
    )
    text, tool = rag_gate._primary_match_excerpt(pt)
    assert text.startswith("Hello excerpt")
    assert tool == expected_tool


def test_post_filter_drops_reference_on_incident_query() -> None:
    class Pt:
        def __init__(self, meta_type: str) -> None:
            self.payload = {"metadata": {"type": meta_type}}
            self.score = 0.9

    pts = [Pt("reference_only"), Pt("troubleshoot_task")]
    out = rag_gate._post_filter_points_for_incident(
        pts,
        "pod oom kill",
        enabled=True,
    )
    assert len(out) == 1
    assert out[0].payload["metadata"]["type"] == "troubleshoot_task"


def test_post_filter_returns_original_when_all_filtered() -> None:
    class Pt:
        payload = {"metadata": {"type": "reference_glossary"}}
        score = 0.9

    pts = [Pt(), Pt()]
    out = rag_gate._post_filter_points_for_incident(pts, "crash loop error", enabled=True)
    assert out is pts


def test_resolve_search_collection_routes_incident_to_troubleshoot() -> None:
    ws = SimpleNamespace(
        pgvector_collection_k8s_expert="expert",
        pgvector_collection_k8s_troubleshoot="ts",
    )
    assert rag_gate._resolve_search_collection(ws, "OOM error here") == "ts"
    assert rag_gate._resolve_search_collection(ws, "what is a deployment") == "expert"


def test_format_hits_builds_blocks() -> None:
    class Pt:
        id = "chunk-1"
        score = 0.88
        payload = {
            "text": "remediation text",
            "metadata": {"url": "https://k8s.io", "version": "1.29"},
        }

    text, cids = rag_gate._format_hits(
        [Pt()],
        max_words=500,
        max_block_chars=200,
    )
    assert "RAG_CHUNK_chunk-1" in text
    assert cids == ["chunk-1"]


@pytest.mark.asyncio
async def test_evaluate_rag_gate_disabled() -> None:
    ctx = SimpleNamespace(settings=SimpleNamespace(rag_gate_enabled=False))
    out = await rag_gate.evaluate_rag_gate(ctx, "x" * 20)
    assert out.hit is False
    assert out.detail.get("reason") == "disabled"


@pytest.mark.asyncio
async def test_evaluate_rag_gate_query_too_short() -> None:
    ctx = SimpleNamespace(settings=SimpleNamespace(rag_gate_enabled=True))
    out = await rag_gate.evaluate_rag_gate(ctx, "short")
    assert out.detail.get("reason") == "query_too_short"


@pytest.mark.asyncio
async def test_evaluate_rag_gate_search_error_phase() -> None:
    vs = SimpleNamespace(
        similarity_search=AsyncMock(side_effect=RuntimeError("rag_llm_embed_failed: boom")),
    )
    ctx = SimpleNamespace(
        settings=SimpleNamespace(
            rag_gate_enabled=True,
            rag_gate_query_max_chars=8000,
            rag_embed_max_tokens=512,
            embed_model="m",
            embed_model_fallback=None,
            rag_gate_limit=4,
            rag_gate_score_threshold=0.42,
            rag_hybrid_search_enabled=False,
            rag_hot_cache_enabled=False,
            rag_hot_cache_ttl_sec=3600,
            rag_tier_knowledge_uncertain_threshold=0.7,
            rag_tier_uncertain_gate_enabled=False,
            pgvector_collection_k8s_expert="",
            pgvector_collection_k8s_troubleshoot="",
        ),
        redis=None,
        llm=object(),
        vector_store=vs,
    )
    out = await rag_gate.evaluate_rag_gate(ctx, "y" * 20)
    assert out.hit is False
    assert out.detail.get("phase") == "llm_embed"


@pytest.mark.asyncio
async def test_evaluate_rag_gate_no_points() -> None:
    vs = SimpleNamespace(
        similarity_search=AsyncMock(return_value=SimpleNamespace(points=[])),
    )
    ctx = SimpleNamespace(
        settings=SimpleNamespace(
            rag_gate_enabled=True,
            rag_gate_query_max_chars=8000,
            rag_embed_max_tokens=512,
            embed_model="m",
            embed_model_fallback=None,
            rag_gate_limit=4,
            rag_gate_score_threshold=0.42,
            rag_hybrid_search_enabled=False,
            rag_hot_cache_enabled=False,
            rag_hot_cache_ttl_sec=3600,
            rag_tier_knowledge_uncertain_threshold=0.7,
            rag_tier_uncertain_gate_enabled=False,
            pgvector_collection_k8s_expert="",
            pgvector_collection_k8s_troubleshoot="",
        ),
        redis=None,
        llm=object(),
        vector_store=vs,
    )
    out = await rag_gate.evaluate_rag_gate(ctx, "query text long enough")
    assert out.hit is False
    assert out.detail.get("reason") == "no_points"


@pytest.mark.asyncio
async def test_evaluate_rag_gate_below_threshold() -> None:
    pt = SimpleNamespace(score=0.1, payload={"text": "a"}, id="1")
    vs = SimpleNamespace(
        similarity_search=AsyncMock(return_value=SimpleNamespace(points=[pt])),
    )
    ctx = SimpleNamespace(
        settings=SimpleNamespace(
            rag_gate_enabled=True,
            rag_gate_query_max_chars=8000,
            rag_embed_max_tokens=512,
            embed_model="m",
            embed_model_fallback=None,
            rag_gate_limit=4,
            rag_gate_score_threshold=0.99,
            rag_hybrid_search_enabled=False,
            rag_hot_cache_enabled=False,
            rag_hot_cache_ttl_sec=3600,
            rag_tier_knowledge_uncertain_threshold=0.7,
            rag_tier_uncertain_gate_enabled=False,
            pgvector_collection_k8s_expert="",
            pgvector_collection_k8s_troubleshoot="",
        ),
        redis=None,
        llm=object(),
        vector_store=vs,
    )
    out = await rag_gate.evaluate_rag_gate(ctx, "query text long enough for gate")
    assert out.hit is False
    assert out.detail.get("reason") == "below_threshold"


@pytest.mark.asyncio
async def test_evaluate_rag_gate_knowledge_uncertain() -> None:
    pt = SimpleNamespace(score=0.5, payload={"text": "helpful chunk"}, id="1")
    vs = SimpleNamespace(
        similarity_search=AsyncMock(return_value=SimpleNamespace(points=[pt])),
    )
    ctx = SimpleNamespace(
        settings=SimpleNamespace(
            rag_gate_enabled=True,
            rag_gate_query_max_chars=8000,
            rag_embed_max_tokens=512,
            embed_model="m",
            embed_model_fallback=None,
            rag_gate_limit=4,
            rag_gate_score_threshold=0.42,
            rag_hybrid_search_enabled=False,
            rag_hot_cache_enabled=False,
            rag_hot_cache_ttl_sec=3600,
            rag_tier_knowledge_uncertain_threshold=0.7,
            rag_tier_uncertain_gate_enabled=True,
            rag_post_filter_metadata_enabled=False,
            pgvector_collection_k8s_expert="",
            pgvector_collection_k8s_troubleshoot="",
        ),
        redis=None,
        llm=object(),
        vector_store=vs,
    )
    out = await rag_gate.evaluate_rag_gate(ctx, "query text long enough for gate")
    assert out.hit is False
    assert out.detail.get("reason") == "knowledge_uncertain"


@pytest.mark.asyncio
async def test_evaluate_rag_gate_hit_and_cache_set() -> None:
    pt = SimpleNamespace(
        score=0.9,
        payload={"text": "fix the pod", "metadata": {"type": "task"}},
        id="c1",
    )
    vs = SimpleNamespace(
        similarity_search=AsyncMock(return_value=SimpleNamespace(points=[pt])),
    )
    redis = MagicMock()
    redis.get = AsyncMock(return_value=None)
    redis.setex = AsyncMock()
    ctx = SimpleNamespace(
        settings=SimpleNamespace(
            rag_gate_enabled=True,
            rag_gate_query_max_chars=8000,
            rag_embed_max_tokens=512,
            embed_model="m",
            embed_model_fallback=None,
            rag_gate_limit=4,
            rag_gate_score_threshold=0.42,
            rag_hybrid_search_enabled=False,
            rag_hot_cache_enabled=True,
            rag_hot_cache_ttl_sec=120,
            rag_tier_knowledge_uncertain_threshold=0.7,
            rag_tier_uncertain_gate_enabled=False,
            rag_post_filter_metadata_enabled=False,
            rag_gate_chunk_max_chars=800,
            pgvector_collection_k8s_expert="",
            pgvector_collection_k8s_troubleshoot="",
        ),
        redis=redis,
        llm=object(),
        vector_store=vs,
    )
    with patch("pkg.rag.gate.effective_reply_max_words", return_value=200):
        out = await rag_gate.evaluate_rag_gate(ctx, "query text long enough for gate")
    assert out.hit is True
    assert out.best_score == 0.9
    redis.setex.assert_called_once()
    payload = json.loads(redis.setex.call_args[0][2])
    assert payload["hit"] is True


@pytest.mark.asyncio
async def test_evaluate_rag_gate_cache_hit() -> None:
    cached = json.dumps(
        {
            "hit": True,
            "formatted": "cached body",
            "best_score": 0.91,
            "collection": "coll",
            "match_text_en": "en",
            "suggested_tool": "kubectl_get_events",
            "chunk_ids": ["a"],
        }
    )
    redis = MagicMock()
    redis.get = AsyncMock(return_value=cached.encode())
    vs = MagicMock()
    vs.similarity_search = AsyncMock()
    ctx = SimpleNamespace(
        settings=SimpleNamespace(
            rag_gate_enabled=True,
            rag_hot_cache_enabled=True,
            rag_gate_query_max_chars=8000,
            rag_embed_max_tokens=512,
            embed_model="m",
            embed_model_fallback=None,
            rag_gate_limit=4,
            rag_gate_score_threshold=0.42,
            rag_hybrid_search_enabled=False,
            rag_hot_cache_ttl_sec=3600,
            rag_tier_knowledge_uncertain_threshold=0.7,
            rag_tier_uncertain_gate_enabled=False,
            pgvector_collection_k8s_expert="",
            pgvector_collection_k8s_troubleshoot="",
        ),
        redis=redis,
        llm=object(),
        vector_store=vs,
    )
    with patch("pkg.rag.gate.effective_reply_max_words", return_value=200):
        out = await rag_gate.evaluate_rag_gate(ctx, "query text long enough for gate")
    assert out.hit is True
    assert out.detail.get("reason") == "cache_hit"
    vs.similarity_search.assert_not_called()


@pytest.mark.asyncio
async def test_evaluate_rag_gate_hybrid_search_path() -> None:
    pt = SimpleNamespace(score=0.95, payload={"text": "hybrid ok", "metadata": {}}, id="h1")
    vs = MagicMock()
    vs.similarity_search_hybrid = AsyncMock(return_value=SimpleNamespace(points=[pt]))
    ctx = SimpleNamespace(
        settings=SimpleNamespace(
            rag_gate_enabled=True,
            rag_gate_query_max_chars=8000,
            rag_embed_max_tokens=512,
            embed_model="m",
            embed_model_fallback="  fb  ",
            rag_gate_limit=4,
            rag_gate_score_threshold=0.42,
            rag_hybrid_search_enabled=True,
            rag_hybrid_vector_weight=0.5,
            rag_hot_cache_enabled=False,
            rag_hot_cache_ttl_sec=3600,
            rag_tier_knowledge_uncertain_threshold=0.7,
            rag_tier_uncertain_gate_enabled=False,
            rag_post_filter_metadata_enabled=False,
            rag_gate_chunk_max_chars=800,
            pgvector_collection_k8s_expert="",
            pgvector_collection_k8s_troubleshoot="",
        ),
        redis=None,
        llm=object(),
        vector_store=vs,
    )
    with patch("pkg.rag.gate.effective_reply_max_words", return_value=200):
        out = await rag_gate.evaluate_rag_gate(ctx, "query text long enough for gate")
    assert out.hit is True
    vs.similarity_search_hybrid.assert_called_once()
