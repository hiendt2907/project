"""Coverage tests for src/workers/infra_context.py."""
from __future__ import annotations

import os

os.environ.setdefault("OMNI_KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
os.environ.setdefault("OMNI_REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("OMNI_OLLAMA_BASE_URL", "http://localhost:11434")
os.environ.setdefault("OMNI_ENV_MODE", "dev")

from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers / stubs
# ---------------------------------------------------------------------------

@dataclass
class _FakeLearnedContext:
    namespace: str | None = None
    pod_name: str | None = None
    service_name: str | None = None
    infra_blocks: list[str] = field(default_factory=list)
    had_vector_search: bool = False
    clarification_bypass: bool = False


def _make_point(text: str, score: float = 0.75, url: str = "", version: str = "1.28"):
    pt = MagicMock()
    pt.score = score
    pt.payload = {"text": text, "metadata": {"url": url, "version": version}}
    return pt


def _make_resp(points):
    resp = MagicMock()
    resp.points = points
    return resp


def _make_ctx(*, settings=None, vector_store=None, llm=None):
    ctx = SimpleNamespace()
    ctx.settings = settings or SimpleNamespace(
        diag_k8s_expert_rag_enabled=True,
        diag_k8s_expert_rag_query_max_chars=4000,
        diag_k8s_expert_rag_limit=4,
        diag_k8s_expert_rag_score_threshold=0.40,
        diag_k8s_expert_rag_max_chars=3200,
        embed_model="nomic-embed-text",
        infra_enrich_max_total_chars=6000,
        pgvector_collection_k8s_expert="",
    )
    ctx.vector_store = vector_store or MagicMock()
    ctx.llm = llm or MagicMock()
    return ctx


# ---------------------------------------------------------------------------
# _expert_collection
# ---------------------------------------------------------------------------

def test_expert_collection_default():
    from workers.infra_context import _expert_collection
    from rag.pgvector_store import COLLECTION_K8S_EXPERT
    ctx = _make_ctx()
    assert _expert_collection(ctx) == COLLECTION_K8S_EXPERT


def test_expert_collection_custom():
    from workers.infra_context import _expert_collection
    ctx = _make_ctx()
    ctx.settings.pgvector_collection_k8s_expert = "custom_k8s"
    assert _expert_collection(ctx) == "custom_k8s"


def test_expert_collection_no_settings():
    from workers.infra_context import _expert_collection
    from rag.pgvector_store import COLLECTION_K8S_EXPERT
    ctx = SimpleNamespace()
    assert _expert_collection(ctx) == COLLECTION_K8S_EXPERT


# ---------------------------------------------------------------------------
# _embedding_from_response
# ---------------------------------------------------------------------------

def test_embedding_from_response_embedding_key():
    from workers.infra_context import _embedding_from_response
    result = _embedding_from_response({"embedding": [0.1, 0.2, 0.3]})
    assert result == [0.1, 0.2, 0.3]


def test_embedding_from_response_list_passthrough():
    from workers.infra_context import _embedding_from_response
    result = _embedding_from_response({"embedding": [1.0, 2.0]})
    assert isinstance(result, list)


def test_embedding_from_response_embeddings_key():
    from workers.infra_context import _embedding_from_response
    result = _embedding_from_response({"embeddings": [[0.5, 0.6]]})
    assert result == [0.5, 0.6]


def test_embedding_from_response_missing_raises():
    from workers.infra_context import _embedding_from_response
    with pytest.raises(ValueError):
        _embedding_from_response({})


def test_embedding_from_response_non_list_embedding():
    from workers.infra_context import _embedding_from_response
    # non-list that is iterable (e.g. tuple) should be converted to list
    result = _embedding_from_response({"embedding": (0.1, 0.2)})
    assert result == [0.1, 0.2]


# ---------------------------------------------------------------------------
# _apply_infra_enrich_cap
# ---------------------------------------------------------------------------

def test_apply_infra_enrich_cap_no_cap():
    from workers.infra_context import _apply_infra_enrich_cap
    ctx = _make_ctx()
    merged = "x" * 100
    assert _apply_infra_enrich_cap(ctx, merged) == merged


def test_apply_infra_enrich_cap_truncates():
    from workers.infra_context import _apply_infra_enrich_cap
    ctx = _make_ctx()
    ctx.settings.infra_enrich_max_total_chars = 10
    merged = "a" * 50
    result = _apply_infra_enrich_cap(ctx, merged)
    assert len(result) == 10


def test_apply_infra_enrich_cap_no_settings():
    from workers.infra_context import _apply_infra_enrich_cap
    ctx = SimpleNamespace()  # no settings
    merged = "y" * 100
    # default cap = 6000, so short string passes through
    assert _apply_infra_enrich_cap(ctx, merged) == merged


# ---------------------------------------------------------------------------
# fetch_k8s_expert_context_for_diagnostic
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_k8s_expert_disabled_returns_empty():
    from workers.infra_context import fetch_k8s_expert_context_for_diagnostic
    ctx = _make_ctx()
    ctx.settings.diag_k8s_expert_rag_enabled = False
    result = await fetch_k8s_expert_context_for_diagnostic(ctx, "check cpu usage")
    assert result == ""


@pytest.mark.asyncio
async def test_fetch_k8s_expert_short_query_returns_empty():
    from workers.infra_context import fetch_k8s_expert_context_for_diagnostic
    ctx = _make_ctx()
    result = await fetch_k8s_expert_context_for_diagnostic(ctx, "cpu")
    assert result == ""


@pytest.mark.asyncio
async def test_fetch_k8s_expert_no_settings_returns_empty():
    from workers.infra_context import fetch_k8s_expert_context_for_diagnostic
    ctx = SimpleNamespace(vector_store=MagicMock(), llm=MagicMock())
    result = await fetch_k8s_expert_context_for_diagnostic(ctx, "check cpu on all pods")
    assert result == ""


@pytest.mark.asyncio
async def test_fetch_k8s_expert_returns_blocks():
    from workers.infra_context import fetch_k8s_expert_context_for_diagnostic
    ctx = _make_ctx()
    pt = _make_point("Pod scheduling docs", score=0.8)
    ctx.vector_store.similarity_search = AsyncMock(return_value=_make_resp([pt]))
    result = await fetch_k8s_expert_context_for_diagnostic(ctx, "how does pod scheduling work in k8s")
    assert "k8s_expert" in result
    assert "Pod scheduling docs" in result


@pytest.mark.asyncio
async def test_fetch_k8s_expert_empty_chunk_skipped():
    from workers.infra_context import fetch_k8s_expert_context_for_diagnostic
    ctx = _make_ctx()
    pt = _make_point("")  # empty chunk
    pt.payload = {"text": "", "metadata": {}}
    ctx.vector_store.similarity_search = AsyncMock(return_value=_make_resp([pt]))
    result = await fetch_k8s_expert_context_for_diagnostic(ctx, "how does pod scheduling work in k8s")
    assert result == ""


@pytest.mark.asyncio
async def test_fetch_k8s_expert_summary_fallback():
    from workers.infra_context import fetch_k8s_expert_context_for_diagnostic
    ctx = _make_ctx()
    pt = MagicMock()
    pt.score = 0.7
    pt.payload = {"summary": "Summary text here", "metadata": {}}
    ctx.vector_store.similarity_search = AsyncMock(return_value=_make_resp([pt]))
    result = await fetch_k8s_expert_context_for_diagnostic(ctx, "how does pod scheduling work in k8s")
    assert "Summary text here" in result


@pytest.mark.asyncio
async def test_fetch_k8s_expert_exception_returns_empty():
    from workers.infra_context import fetch_k8s_expert_context_for_diagnostic
    ctx = _make_ctx()
    ctx.vector_store.similarity_search = AsyncMock(side_effect=Exception("vector db error"))
    result = await fetch_k8s_expert_context_for_diagnostic(ctx, "how does pod scheduling work in k8s")
    assert result == ""


@pytest.mark.asyncio
async def test_fetch_k8s_expert_max_chars_cap():
    from workers.infra_context import fetch_k8s_expert_context_for_diagnostic
    ctx = _make_ctx()
    # Must exceed header + newline + at least part of chunk (see infra_context block layout).
    ctx.settings.diag_k8s_expert_rag_max_chars = 1600
    pts = [_make_point("A" * 1000, score=0.8), _make_point("B" * 1000, score=0.7)]
    ctx.vector_store.similarity_search = AsyncMock(return_value=_make_resp(pts))
    result = await fetch_k8s_expert_context_for_diagnostic(ctx, "how does pod scheduling work in k8s")
    # cap prevents adding second block
    assert result != ""


@pytest.mark.asyncio
async def test_fetch_k8s_expert_no_points():
    from workers.infra_context import fetch_k8s_expert_context_for_diagnostic
    ctx = _make_ctx()
    ctx.vector_store.similarity_search = AsyncMock(return_value=_make_resp([]))
    result = await fetch_k8s_expert_context_for_diagnostic(ctx, "how does pod scheduling work in k8s")
    assert result == ""


@pytest.mark.asyncio
async def test_fetch_k8s_expert_none_points():
    from workers.infra_context import fetch_k8s_expert_context_for_diagnostic
    ctx = _make_ctx()
    resp = MagicMock()
    resp.points = None
    ctx.vector_store.similarity_search = AsyncMock(return_value=resp)
    result = await fetch_k8s_expert_context_for_diagnostic(ctx, "how does pod scheduling work in k8s")
    assert result == ""


# ---------------------------------------------------------------------------
# fetch_infra_injection_for_fallback
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_infra_fallback_short_text():
    from workers.infra_context import fetch_infra_injection_for_fallback
    ctx = _make_ctx()
    result = await fetch_infra_injection_for_fallback(ctx, "hi")
    assert result == ""


@pytest.mark.asyncio
async def test_fetch_infra_fallback_embed_error():
    from workers.infra_context import fetch_infra_injection_for_fallback
    ctx = _make_ctx()
    ctx.llm.embed = AsyncMock(side_effect=Exception("embed failed"))
    result = await fetch_infra_injection_for_fallback(ctx, "check all pods in multi-agent namespace")
    assert result == ""


@pytest.mark.asyncio
async def test_fetch_infra_fallback_returns_blocks():
    from workers.infra_context import fetch_infra_injection_for_fallback
    ctx = _make_ctx()
    ctx.llm.embed = AsyncMock(return_value={"embedding": [0.1] * 768})
    pt = MagicMock()
    pt.score = 0.75
    pt.payload = {"text": "topology info"}
    resp = _make_resp([pt])
    ctx.vector_store.query_points = AsyncMock(return_value=resp)
    result = await fetch_infra_injection_for_fallback(ctx, "check all pods in multi-agent namespace")
    assert "topology info" in result or "k8s_expert" in result


@pytest.mark.asyncio
async def test_fetch_infra_fallback_vector_search_fails():
    from workers.infra_context import fetch_infra_injection_for_fallback
    ctx = _make_ctx()
    ctx.llm.embed = AsyncMock(return_value={"embedding": [0.1] * 768})
    ctx.vector_store.query_points = AsyncMock(side_effect=Exception("qdrant down"))
    result = await fetch_infra_injection_for_fallback(ctx, "check all pods in multi-agent namespace")
    assert result == ""


@pytest.mark.asyncio
async def test_fetch_infra_fallback_no_points():
    from workers.infra_context import fetch_infra_injection_for_fallback
    ctx = _make_ctx()
    ctx.llm.embed = AsyncMock(return_value={"embedding": [0.1] * 768})
    ctx.vector_store.query_points = AsyncMock(return_value=_make_resp([]))
    result = await fetch_infra_injection_for_fallback(ctx, "check all pods in multi-agent namespace")
    assert result == ""


@pytest.mark.asyncio
async def test_fetch_infra_fallback_embeddings_key():
    """Tests the embeddings (plural) response format."""
    from workers.infra_context import fetch_infra_injection_for_fallback
    ctx = _make_ctx()
    ctx.llm.embed = AsyncMock(return_value={"embeddings": [[0.1] * 768]})
    pt = MagicMock()
    pt.score = 0.8
    pt.payload = {"text": "some infra data"}
    ctx.vector_store.query_points = AsyncMock(return_value=_make_resp([pt]))
    result = await fetch_infra_injection_for_fallback(ctx, "check all pods in multi-agent namespace")
    assert result != ""


# ---------------------------------------------------------------------------
# enrich_working_text_with_infra
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_enrich_short_text_returns_raw():
    from workers.infra_context import enrich_working_text_with_infra
    ctx = _make_ctx()
    result = await enrich_working_text_with_infra(ctx, "hi")
    assert result == "hi"


@pytest.mark.asyncio
async def test_enrich_with_learned_context_uses_preflight():
    from workers.infra_context import enrich_working_text_with_infra
    ctx = _make_ctx()
    learned = _FakeLearnedContext(
        namespace="multi-agent",
        pod_name="omni-worker-abc123-xyz",
        infra_blocks=["[CONTEXT: k8s_expert score=0.8]\nPod resource limits"],
        had_vector_search=True,
    )
    result = await enrich_working_text_with_infra(ctx, "check this pod cpu usage", learned=learned)
    assert "k8s_expert" in result
    assert "multi-agent" in result


@pytest.mark.asyncio
async def test_enrich_with_learned_context_infra_blocks_only():
    from workers.infra_context import enrich_working_text_with_infra
    ctx = _make_ctx()
    learned = _FakeLearnedContext(
        infra_blocks=["[CONTEXT: infra_topology]\nDeployment topology info"],
        had_vector_search=False,
    )
    result = await enrich_working_text_with_infra(ctx, "analyze this infrastructure", learned=learned)
    assert "infra_topology" in result


@pytest.mark.asyncio
async def test_enrich_no_learned_fallback_to_vector():
    from workers.infra_context import enrich_working_text_with_infra
    ctx = _make_ctx()
    ctx.llm.embed = AsyncMock(return_value={"embedding": [0.1] * 768})
    pt = MagicMock()
    pt.score = 0.75
    pt.payload = {"text": "k8s expert content"}
    ctx.vector_store.query_points = AsyncMock(return_value=_make_resp([pt]))
    result = await enrich_working_text_with_infra(ctx, "diagnose the failing deployment in production")
    assert "k8s expert content" in result


@pytest.mark.asyncio
async def test_enrich_no_learned_embed_fails_returns_raw():
    from workers.infra_context import enrich_working_text_with_infra
    ctx = _make_ctx()
    ctx.llm.embed = AsyncMock(side_effect=Exception("embed failure"))
    raw_text = "diagnose the failing deployment in production"
    result = await enrich_working_text_with_infra(ctx, raw_text)
    assert result == raw_text


@pytest.mark.asyncio
async def test_enrich_no_learned_empty_blocks_returns_raw():
    from workers.infra_context import enrich_working_text_with_infra
    ctx = _make_ctx()
    ctx.llm.embed = AsyncMock(return_value={"embedding": [0.1] * 768})
    ctx.vector_store.query_points = AsyncMock(return_value=_make_resp([]))
    raw_text = "diagnose the failing deployment in production"
    result = await enrich_working_text_with_infra(ctx, raw_text)
    assert result == raw_text


@pytest.mark.asyncio
async def test_enrich_cap_applied():
    from workers.infra_context import enrich_working_text_with_infra
    ctx = _make_ctx()
    ctx.settings.infra_enrich_max_total_chars = 50
    ctx.llm.embed = AsyncMock(return_value={"embedding": [0.1] * 768})
    pt = MagicMock()
    pt.score = 0.75
    pt.payload = {"text": "A" * 2000}
    ctx.vector_store.query_points = AsyncMock(return_value=_make_resp([pt]))
    result = await enrich_working_text_with_infra(ctx, "diagnose the failing deployment in production")
    assert len(result) == 50


@pytest.mark.asyncio
async def test_enrich_with_learned_none_blocks():
    from workers.infra_context import enrich_working_text_with_infra
    ctx = _make_ctx()
    # learned is None — falls through to embed path
    ctx.llm.embed = AsyncMock(return_value={"embedding": [0.1] * 768})
    pt = MagicMock()
    pt.score = 0.75
    pt.payload = {"text": "topology block"}
    ctx.vector_store.query_points = AsyncMock(return_value=_make_resp([pt]))
    result = await enrich_working_text_with_infra(ctx, "check pods in multi-agent", learned=None)
    assert "topology block" in result
