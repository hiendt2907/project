"""Coverage tests for rag.redis_vector_store — async paths, class methods, log_error_to_ledger."""
from __future__ import annotations

import json
import os
import struct
import math
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("OMNI_ENV_MODE", "dev")
os.environ.setdefault("OMNI_KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
os.environ.setdefault("OMNI_REDIS_URL", "redis://localhost:6379/0")

from rag import redis_vector_store as rvs
from rag.redis_vector_store import (
    EMBED_DIM,
    PointStruct,
    QueryResponse,
    RedisVectorStore,
    RedisRAGSettings,
    PostgresRAGSettings,
    PGVectorStore,
    log_error_to_ledger,
    _embed_query_robust,
    _docs_to_points,
    _ft_escape,
    _stable_vec_from_text,
    _is_embed_bad_request,
    _embedding_vector_from_response,
    _make_index_fields,
    _before_retry_log,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_redis_mock() -> MagicMock:
    """Return a mock that mimics redis.asyncio.Redis."""
    r = MagicMock()
    ft_mock = MagicMock()
    r.ft.return_value = ft_mock
    ft_mock.info = AsyncMock(return_value={})
    ft_mock.create_index = AsyncMock()
    ft_mock.search = AsyncMock(return_value=SimpleNamespace(docs=[]))
    r.pipeline = MagicMock(return_value=AsyncMock())
    r.scan = AsyncMock(return_value=(0, []))
    r.hget = AsyncMock(return_value=None)
    return r


def _make_store(r=None) -> RedisVectorStore:
    return RedisVectorStore(r or _make_redis_mock())


def _valid_vec() -> list[float]:
    return [0.0] * EMBED_DIM


# ---------------------------------------------------------------------------
# Pure function tests (new paths)
# ---------------------------------------------------------------------------

def test_collections_constants() -> None:
    assert rvs.COLLECTION_SOP == "itops_sop_ledger"
    assert rvs.COLLECTION_ACTION_EXPERIENCE == "action_experience"
    assert rvs.PGVectorStore is RedisVectorStore
    assert rvs.PostgresRAGSettings is RedisRAGSettings


def test_embedding_vector_from_response_list_of_non_list() -> None:
    # embeddings key with a non-list element that is iterable
    result = _embedding_vector_from_response({"embeddings": [(0.1, 0.2)]})
    assert result == [0.1, 0.2]


def test_embedding_vector_from_response_embedding_is_tuple() -> None:
    result = _embedding_vector_from_response({"embedding": (0.5, 0.6)})
    assert result == [0.5, 0.6]


def test_docs_to_points_below_threshold_excluded() -> None:
    doc = SimpleNamespace(__score=0.9, omni_payload='{"k":1}', id="doc:col:abc")
    pts = _docs_to_points([doc], score_threshold=0.5)
    # score = 1 - 0.9 = 0.1 < 0.5 → excluded
    assert len(pts) == 0


def test_docs_to_points_threshold_none_includes_all() -> None:
    doc = SimpleNamespace(__score=0.95, omni_payload='{"k":2}', id="doc:col:xyz")
    pts = _docs_to_points([doc], score_threshold=None)
    assert len(pts) == 1
    assert pts[0].id == "xyz"
    assert abs(pts[0].score - 0.05) < 1e-6


def test_docs_to_points_payload_json_error() -> None:
    doc = SimpleNamespace(__score=0.0, omni_payload="bad{{json", id="a:b:c")
    pts = _docs_to_points([doc], None)
    assert pts[0].payload == {}


def test_make_index_fields_returns_four_fields() -> None:
    fields = _make_index_fields()
    assert len(fields) == 4


def test_redis_rag_settings_defaults() -> None:
    s = RedisRAGSettings()
    assert "redis" in s.redis_url or "localhost" in s.redis_url or "6379" in s.redis_url


# ---------------------------------------------------------------------------
# _embed_query_robust tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_embed_query_robust_success_first_tier() -> None:
    llm = MagicMock()
    llm.embed = AsyncMock(return_value={"embedding": [0.1] * EMBED_DIM})
    with patch("pkg.rag.embed_utils.truncate_for_embedding", side_effect=lambda t, max_tokens: t):
        result = await _embed_query_robust(
            llm,
            "test query",
            embed_model="nomic-embed-text",
            embed_model_fallback=None,
            keep_alive="5m",
            query_max_chars=8000,
        )
    assert len(result) == EMBED_DIM


@pytest.mark.asyncio
async def test_embed_query_robust_400_retries_then_raises() -> None:
    llm = MagicMock()
    llm.embed = AsyncMock(side_effect=ValueError("status code 400"))
    with patch("pkg.rag.embed_utils.truncate_for_embedding", side_effect=lambda t, max_tokens: t):
        with pytest.raises(RuntimeError, match="rag_llm_embed_failed"):
            await _embed_query_robust(
                llm,
                "x" * 100,
                embed_model="nomic-embed-text",
                embed_model_fallback=None,
                keep_alive="5m",
                query_max_chars=8000,
            )


@pytest.mark.asyncio
async def test_embed_query_robust_non400_raises_immediately() -> None:
    llm = MagicMock()
    llm.embed = AsyncMock(side_effect=ConnectionError("timeout"))
    with patch("pkg.rag.embed_utils.truncate_for_embedding", side_effect=lambda t, max_tokens: t):
        with pytest.raises(RuntimeError, match="rag_llm_embed_failed"):
            await _embed_query_robust(
                llm,
                "query",
                embed_model="nomic-embed-text",
                embed_model_fallback=None,
                keep_alive="5m",
                query_max_chars=8000,
            )


@pytest.mark.asyncio
async def test_embed_query_robust_fallback_model_used() -> None:
    call_count = {"n": 0}

    async def _embed_side(model, input, keep_alive):
        call_count["n"] += 1
        if model == "primary":
            raise ValueError("status code 400")
        return {"embedding": [0.2] * EMBED_DIM}

    llm = MagicMock()
    llm.embed = _embed_side
    with patch("pkg.rag.embed_utils.truncate_for_embedding", side_effect=lambda t, max_tokens: t):
        result = await _embed_query_robust(
            llm,
            "query",
            embed_model="primary",
            embed_model_fallback="fallback",
            keep_alive="5m",
            query_max_chars=8000,
        )
    assert len(result) == EMBED_DIM


# ---------------------------------------------------------------------------
# RedisVectorStore._ensure_index tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ensure_index_skips_if_already_cached() -> None:
    r = _make_redis_mock()
    store = _make_store(r)
    store._initialized_indexes.add("mycol")
    await store._ensure_index("mycol")
    r.ft.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_index_calls_create_when_info_raises() -> None:
    r = _make_redis_mock()
    ft_mock = r.ft.return_value
    ft_mock.info = AsyncMock(side_effect=Exception("no index"))
    ft_mock.create_index = AsyncMock()
    store = _make_store(r)
    await store._ensure_index("newcol")
    ft_mock.create_index.assert_called_once()
    assert "newcol" in store._initialized_indexes


@pytest.mark.asyncio
async def test_ensure_index_handles_already_exists_race() -> None:
    r = _make_redis_mock()
    ft_mock = r.ft.return_value
    ft_mock.info = AsyncMock(side_effect=Exception("no index"))
    ft_mock.create_index = AsyncMock(side_effect=Exception("index already exists"))
    store = _make_store(r)
    await store._ensure_index("racecol")
    assert "racecol" in store._initialized_indexes


@pytest.mark.asyncio
async def test_ensure_index_handles_create_warning() -> None:
    r = _make_redis_mock()
    ft_mock = r.ft.return_value
    ft_mock.info = AsyncMock(side_effect=Exception("no index"))
    ft_mock.create_index = AsyncMock(side_effect=Exception("some other error"))
    store = _make_store(r)
    await store._ensure_index("warncol")
    assert "warncol" in store._initialized_indexes


@pytest.mark.asyncio
async def test_ensure_ready_initializes_all_collections() -> None:
    r = _make_redis_mock()
    ft_mock = r.ft.return_value
    ft_mock.info = AsyncMock(return_value={})
    store = _make_store(r)
    await store.ensure_ready()
    assert store._initialized is True
    # calling again is no-op
    r.ft.reset_mock()
    await store.ensure_ready()
    r.ft.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_partition_valid_name() -> None:
    r = _make_redis_mock()
    ft_mock = r.ft.return_value
    ft_mock.info = AsyncMock(return_value={})
    store = _make_store(r)
    await store.ensure_partition_for_collection("valid_col")
    assert "valid_col" in store._initialized_indexes


@pytest.mark.asyncio
async def test_ensure_partition_invalid_name_raises() -> None:
    store = _make_store()
    with pytest.raises(ValueError, match="invalid collection_name"):
        await store.ensure_partition_for_collection("9invalid")


@pytest.mark.asyncio
async def test_ensure_partition_invalid_name_empty() -> None:
    store = _make_store()
    with pytest.raises(ValueError, match="invalid collection_name"):
        await store.ensure_partition_for_collection("")


# ---------------------------------------------------------------------------
# upsert tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upsert_normal_pipeline() -> None:
    r = _make_redis_mock()
    pipe = MagicMock()
    pipe.hset = MagicMock()
    pipe.execute = AsyncMock(return_value=[])
    r.pipeline.return_value = pipe
    store = _make_store(r)
    store._initialized_indexes.add("testcol")

    vec = [0.1] * EMBED_DIM
    pts = [PointStruct(id="p1", vector=vec, payload={"text": "hello", "source": "s", "type": "t"})]
    await store.upsert("testcol", pts)
    pipe.hset.assert_called_once()
    pipe.execute.assert_called_once()


@pytest.mark.asyncio
async def test_upsert_skips_point_without_vector() -> None:
    r = _make_redis_mock()
    pipe = MagicMock()
    pipe.hset = MagicMock()
    pipe.execute = AsyncMock(return_value=[])
    r.pipeline.return_value = pipe
    store = _make_store(r)
    store._initialized_indexes.add("testcol")

    pts = [PointStruct(id="novec", vector=None, payload={})]
    await store.upsert("testcol", pts)
    pipe.hset.assert_not_called()


@pytest.mark.asyncio
async def test_upsert_pipeline_exception_reraises() -> None:
    r = _make_redis_mock()
    pipe = MagicMock()
    pipe.hset = MagicMock()
    pipe.execute = AsyncMock(side_effect=Exception("pipe failed"))
    r.pipeline.return_value = pipe
    store = _make_store(r)
    store._initialized_indexes.add("testcol")

    vec = [0.0] * EMBED_DIM
    pts = [PointStruct(id="p2", vector=vec, payload={})]
    with pytest.raises(Exception, match="pipe failed"):
        await store.upsert("testcol", pts)


@pytest.mark.asyncio
async def test_upsert_dict_point() -> None:
    r = _make_redis_mock()
    pipe = MagicMock()
    pipe.hset = MagicMock()
    pipe.execute = AsyncMock(return_value=[])
    r.pipeline.return_value = pipe
    store = _make_store(r)
    store._initialized_indexes.add("testcol")

    vec = [0.5] * EMBED_DIM
    pts = [{"id": "p3", "vector": vec, "payload": {"text": "x"}}]
    await store.upsert("testcol", pts)
    pipe.hset.assert_called_once()


# ---------------------------------------------------------------------------
# query_points tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_query_points_returns_filtered_results() -> None:
    r = _make_redis_mock()
    doc = SimpleNamespace(
        id="doc:testcol:abc",
        omni_payload=json.dumps({"k": "v"}),
        **{"__score": 0.1},  # score attr
    )
    setattr(doc, "__score", 0.1)
    r.ft.return_value.info = AsyncMock(return_value={})
    r.ft.return_value.search = AsyncMock(return_value=SimpleNamespace(docs=[doc]))
    store = _make_store(r)
    store._initialized_indexes.add("testcol")

    vec = [0.0] * EMBED_DIM
    result = await store.query_points("testcol", vec, limit=5, score_threshold=0.5)
    assert isinstance(result, QueryResponse)
    assert len(result.points) == 1
    assert result.points[0].id == "abc"
    assert result.points[0].payload == {"k": "v"}


@pytest.mark.asyncio
async def test_query_points_exception_reraises() -> None:
    r = _make_redis_mock()
    r.ft.return_value.info = AsyncMock(return_value={})
    r.ft.return_value.search = AsyncMock(side_effect=Exception("search failed"))
    store = _make_store(r)
    store._initialized_indexes.add("testcol")

    vec = [0.0] * EMBED_DIM
    with pytest.raises(Exception, match="search failed"):
        await store.query_points("testcol", vec)


@pytest.mark.asyncio
async def test_query_points_with_payload_filters() -> None:
    r = _make_redis_mock()
    doc = SimpleNamespace(id="doc:c:id1", omni_payload=json.dumps({"env": "prod", "x": 1}))
    setattr(doc, "__score", 0.05)
    r.ft.return_value.search = AsyncMock(return_value=SimpleNamespace(docs=[doc]))
    store = _make_store(r)
    store._initialized_indexes.add("c")

    vec = [0.0] * EMBED_DIM
    # Filter that matches
    result = await store.query_points("c", vec, payload_filters={"env": "prod"})
    assert len(result.points) == 1

    # Filter that doesn't match
    r.ft.return_value.search = AsyncMock(return_value=SimpleNamespace(docs=[doc]))
    result2 = await store.query_points("c", vec, payload_filters={"env": "staging"})
    assert len(result2.points) == 0


@pytest.mark.asyncio
async def test_query_points_bad_payload_json() -> None:
    r = _make_redis_mock()
    doc = SimpleNamespace(id="doc:c:id2", omni_payload="bad-json")
    setattr(doc, "__score", 0.0)
    r.ft.return_value.search = AsyncMock(return_value=SimpleNamespace(docs=[doc]))
    store = _make_store(r)
    store._initialized_indexes.add("c")

    vec = [0.0] * EMBED_DIM
    result = await store.query_points("c", vec, with_payload=True)
    assert result.points[0].payload == {}


@pytest.mark.asyncio
async def test_query_points_with_payload_false() -> None:
    r = _make_redis_mock()
    doc = SimpleNamespace(id="doc:c:id3", omni_payload='{"k":1}')
    setattr(doc, "__score", 0.0)
    r.ft.return_value.search = AsyncMock(return_value=SimpleNamespace(docs=[doc]))
    store = _make_store(r)
    store._initialized_indexes.add("c")

    vec = [0.0] * EMBED_DIM
    result = await store.query_points("c", vec, with_payload=False)
    # with_payload=False means we skip JSON parse — payload stays {}
    assert result.points[0].payload == {}


# ---------------------------------------------------------------------------
# fulltext_search_points tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fulltext_search_short_query_returns_empty() -> None:
    store = _make_store()
    result = await store.fulltext_search_points("col", "x", limit=5)
    assert result.points == []


@pytest.mark.asyncio
async def test_fulltext_search_normal() -> None:
    r = _make_redis_mock()
    doc = SimpleNamespace(id="doc:col:ft1", omni_payload='{"text":"hello"}')
    setattr(doc, "score", 1.5)
    r.ft.return_value.search = AsyncMock(return_value=SimpleNamespace(docs=[doc]))
    store = _make_store(r)
    store._initialized_indexes.add("col")

    result = await store.fulltext_search_points("col", "hello world", limit=5)
    assert len(result.points) == 1
    assert result.points[0].score == pytest.approx(1.5)


@pytest.mark.asyncio
async def test_fulltext_search_exception_returns_empty() -> None:
    r = _make_redis_mock()
    r.ft.return_value.search = AsyncMock(side_effect=Exception("ft failed"))
    store = _make_store(r)
    store._initialized_indexes.add("col")

    result = await store.fulltext_search_points("col", "hello world", limit=5)
    assert result.points == []


@pytest.mark.asyncio
async def test_fulltext_search_all_special_chars_safe_too_short() -> None:
    store = _make_store()
    # query is just special chars that after escaping become >=2 but original text < 2
    result = await store.fulltext_search_points("col", "!", limit=5)
    assert result.points == []


# ---------------------------------------------------------------------------
# similarity_search tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_similarity_search_no_llm_raises() -> None:
    store = _make_store()
    with pytest.raises(TypeError, match="requires llm client"):
        await store.similarity_search("q", "col", llm=None, embed_model="m")


@pytest.mark.asyncio
async def test_similarity_search_propagates_exception() -> None:
    r = _make_redis_mock()
    r.ft.return_value.search = AsyncMock(side_effect=Exception("knn fail"))
    store = _make_store(r)
    store._initialized_indexes.add("col")

    llm = MagicMock()
    llm.embed = AsyncMock(return_value={"embedding": [0.1] * EMBED_DIM})
    with patch("pkg.rag.embed_utils.truncate_for_embedding", side_effect=lambda t, max_tokens: t):
        with pytest.raises(RuntimeError, match="rag_redis_query_failed"):
            await store.similarity_search("query", "col", llm=llm, embed_model="model")


@pytest.mark.asyncio
async def test_similarity_search_returns_results() -> None:
    r = _make_redis_mock()
    doc = SimpleNamespace(id="doc:col:r1", omni_payload='{"x":1}')
    setattr(doc, "__score", 0.1)
    r.ft.return_value.search = AsyncMock(return_value=SimpleNamespace(docs=[doc]))
    store = _make_store(r)
    store._initialized_indexes.add("col")

    llm = MagicMock()
    llm.embed = AsyncMock(return_value={"embedding": [0.1] * EMBED_DIM})
    with patch("pkg.rag.embed_utils.truncate_for_embedding", side_effect=lambda t, max_tokens: t):
        result = await store.similarity_search("query", "col", llm=llm, embed_model="model")
    assert isinstance(result, QueryResponse)


# ---------------------------------------------------------------------------
# similarity_search_hybrid tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_similarity_search_hybrid_no_llm_raises() -> None:
    store = _make_store()
    with pytest.raises(TypeError, match="requires llm client"):
        await store.similarity_search_hybrid("q", "col", llm=None, embed_model="m")


@pytest.mark.asyncio
async def test_similarity_search_hybrid_native_path_with_results() -> None:
    r = _make_redis_mock()
    doc = SimpleNamespace(id="doc:col:h1", omni_payload='{"a":1}')
    setattr(doc, "__score", 0.1)
    # native hybrid search returns docs
    r.ft.return_value.search = AsyncMock(return_value=SimpleNamespace(docs=[doc]))
    store = _make_store(r)
    store._initialized_indexes.add("col")

    llm = MagicMock()
    llm.embed = AsyncMock(return_value={"embedding": [0.1] * EMBED_DIM})
    with patch("pkg.rag.embed_utils.truncate_for_embedding", side_effect=lambda t, max_tokens: t):
        result = await store.similarity_search_hybrid(
            "long enough query", "col", llm=llm, embed_model="m"
        )
    assert isinstance(result, QueryResponse)


@pytest.mark.asyncio
async def test_similarity_search_hybrid_fallback_rrf() -> None:
    r = _make_redis_mock()
    doc = SimpleNamespace(id="doc:col:h2", omni_payload='{"b":2}')
    setattr(doc, "__score", 0.1)
    call_count = {"n": 0}

    async def search_side(*a, **kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise Exception("native hybrid fail")
        return SimpleNamespace(docs=[doc])

    r.ft.return_value.search = search_side
    store = _make_store(r)
    store._initialized_indexes.add("col")

    llm = MagicMock()
    llm.embed = AsyncMock(return_value={"embedding": [0.1] * EMBED_DIM})
    with patch("pkg.rag.embed_utils.truncate_for_embedding", side_effect=lambda t, max_tokens: t):
        result = await store.similarity_search_hybrid(
            "long query text here", "col", llm=llm, embed_model="m"
        )
    assert isinstance(result, QueryResponse)


@pytest.mark.asyncio
async def test_similarity_search_hybrid_dense_query_error_raises() -> None:
    r = _make_redis_mock()

    async def search_side(*a, **kw):
        raise Exception("always fail")

    r.ft.return_value.search = search_side
    store = _make_store(r)
    store._initialized_indexes.add("col")

    llm = MagicMock()
    llm.embed = AsyncMock(return_value={"embedding": [0.1] * EMBED_DIM})
    with patch("pkg.rag.embed_utils.truncate_for_embedding", side_effect=lambda t, max_tokens: t):
        with pytest.raises(RuntimeError, match="rag_redis_query_failed"):
            await store.similarity_search_hybrid(
                "long query text", "col", llm=llm, embed_model="m"
            )


# ---------------------------------------------------------------------------
# action_experience_unique_counts tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_action_experience_unique_counts_empty() -> None:
    r = _make_redis_mock()
    r.scan = AsyncMock(return_value=(0, []))
    store = _make_store(r)
    result = await store.action_experience_unique_counts()
    assert result == {"unique_success_patterns": 0, "unique_fail_patterns": 0}


@pytest.mark.asyncio
async def test_action_experience_unique_counts_with_keys() -> None:
    r = _make_redis_mock()
    key1 = f"doc:action_experience:id1"
    key2 = f"doc:action_experience:id2"
    r.scan = AsyncMock(side_effect=[(1, [key1]), (0, [key2])])
    r.hget = AsyncMock(side_effect=[
        json.dumps({"pattern_key": "pk1", "exec_outcome": "success"}),
        json.dumps({"pattern_key": "pk2", "exec_outcome": "fail"}),
    ])
    store = _make_store(r)
    result = await store.action_experience_unique_counts()
    assert result["unique_success_patterns"] == 1
    assert result["unique_fail_patterns"] == 1


@pytest.mark.asyncio
async def test_action_experience_unique_counts_scan_exception() -> None:
    r = _make_redis_mock()
    r.scan = AsyncMock(side_effect=Exception("scan failed"))
    store = _make_store(r)
    result = await store.action_experience_unique_counts()
    assert "unique_success_patterns" in result


@pytest.mark.asyncio
async def test_action_experience_unique_counts_bad_payload() -> None:
    r = _make_redis_mock()
    r.scan = AsyncMock(return_value=(0, ["doc:action_experience:bad"]))
    r.hget = AsyncMock(return_value="bad-json")
    store = _make_store(r)
    result = await store.action_experience_unique_counts()
    # bad payload is skipped
    assert result["unique_success_patterns"] == 0


@pytest.mark.asyncio
async def test_action_experience_unique_counts_missing_payload() -> None:
    r = _make_redis_mock()
    r.scan = AsyncMock(return_value=(0, ["doc:action_experience:empty"]))
    r.hget = AsyncMock(return_value=None)
    store = _make_store(r)
    result = await store.action_experience_unique_counts()
    assert result["unique_success_patterns"] == 0


# ---------------------------------------------------------------------------
# close test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_close_is_noop() -> None:
    store = _make_store()
    await store.close()  # should not raise


# ---------------------------------------------------------------------------
# log_error_to_ledger tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_log_error_to_ledger_with_vector_store() -> None:
    r = _make_redis_mock()
    pipe = MagicMock()
    pipe.hset = MagicMock()
    pipe.execute = AsyncMock(return_value=[])
    r.pipeline.return_value = pipe
    store = RedisVectorStore(r)
    store._initialized_indexes.add("itops_error_ledger")

    point_id = await log_error_to_ledger(
        store,
        title="Test Error",
        detail="some detail",
        phase="init",
        component="tests",
    )
    assert isinstance(point_id, str)
    pipe.hset.assert_called_once()


@pytest.mark.asyncio
async def test_log_error_to_ledger_with_raw_redis() -> None:
    r = _make_redis_mock()
    pipe = MagicMock()
    pipe.hset = MagicMock()
    pipe.execute = AsyncMock(return_value=[])
    r.pipeline.return_value = pipe

    point_id = await log_error_to_ledger(
        r,
        title="err",
        detail="d",
        phase="p",
        extra={"k": "v"},
    )
    assert isinstance(point_id, str)


@pytest.mark.asyncio
async def test_log_error_to_ledger_with_extra() -> None:
    r = _make_redis_mock()
    pipe = MagicMock()
    pipe.hset = MagicMock()
    pipe.execute = AsyncMock(return_value=[])
    r.pipeline.return_value = pipe
    store = RedisVectorStore(r)
    store._initialized_indexes.add("itops_error_ledger")

    point_id = await log_error_to_ledger(
        store,
        title="T",
        detail="D",
        phase="ph",
        extra={"context": "test"},
    )
    assert isinstance(point_id, str)


# ---------------------------------------------------------------------------
# init_pg_pool deprecation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_init_pg_pool_raises_deprecation() -> None:
    from rag.redis_vector_store import init_pg_pool
    with pytest.raises(DeprecationWarning):
        await init_pg_pool()
