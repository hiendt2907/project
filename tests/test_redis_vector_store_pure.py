"""Pure-function tests for rag.redis_vector_store without Redis Stack (W3)."""

from __future__ import annotations

import math
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from rag import redis_vector_store as rvs


def test_ft_escape_escapes_special_chars():
    assert "\\-" in rvs._ft_escape("a-b")
    assert "\\@" in rvs._ft_escape("@")


def test_stable_vec_from_text_unit_norm():
    v = rvs._stable_vec_from_text("hello", dim=16)
    assert len(v) == 16
    n = math.sqrt(sum(x * x for x in v))
    assert abs(n - 1.0) < 1e-6
    v2 = rvs._stable_vec_from_text("hello", dim=16)
    assert v == v2
    v3 = rvs._stable_vec_from_text("other", dim=16)
    assert v3 != v


def test_embedding_vector_from_response():
    assert rvs._embedding_vector_from_response({"embedding": [0.1, 0.2]}) == [0.1, 0.2]
    assert rvs._embedding_vector_from_response({"embeddings": [[1.0, 2.0]]}) == [1.0, 2.0]
    with pytest.raises(ValueError, match="missing"):
        rvs._embedding_vector_from_response({})


def test_docs_to_points_score_threshold():
    doc = SimpleNamespace(__score=0.2, omni_payload='{"a":1}', id="doc:col:pid1")
    pts = rvs._docs_to_points([doc], score_threshold=0.9)
    assert len(pts) == 0
    pts2 = rvs._docs_to_points([doc], score_threshold=None)
    assert len(pts2) == 1
    assert pts2[0].id == "pid1"
    assert pts2[0].score == pytest.approx(0.8)


def test_docs_to_points_bad_payload():
    doc = SimpleNamespace(__score=0.0, omni_payload="not-json", id="x:y:z")
    pts = rvs._docs_to_points([doc], None)
    assert pts[0].payload == {}


def test_is_embed_bad_request_string_heuristic():
    assert rvs._is_embed_bad_request(Exception("status code 400")) is True
    assert rvs._is_embed_bad_request(Exception("ok")) is False


def test_is_embed_bad_request_httpx():
    httpx = pytest.importorskip("httpx")
    req = httpx.Request("GET", "http://example.test/")
    resp = httpx.Response(400, request=req)
    exc = httpx.HTTPStatusError("bad", request=req, response=resp)
    assert rvs._is_embed_bad_request(exc) is True


def test_before_retry_log(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    rs = SimpleNamespace(attempt_number=2, outcome=SimpleNamespace(exception=lambda: None))
    caplog.set_level(logging.WARNING)
    rvs._before_retry_log(rs)
    assert "Attempt: 2" in caplog.text


def test_make_index_fields():
    fields = rvs._make_index_fields()
    assert len(fields) == 4


def test_rrf_merge_points():
    store = rvs.RedisVectorStore(MagicMock())
    dense = [
        rvs.PointStruct(id="a", payload={"x": 1}, score=0.9),
        rvs.PointStruct(id="b", payload={}, score=0.5),
    ]
    sparse = [rvs.PointStruct(id="b", payload={"y": 2}, score=0.1)]
    merged = store._rrf_merge_points(dense, sparse, k=60, dense_weight=0.65)
    ids = [p.id for p in merged]
    assert "a" in ids and "b" in ids


def test_ensure_partition_invalid_name():
    import asyncio

    async def _run() -> None:
        store = rvs.RedisVectorStore(MagicMock())
        with pytest.raises(ValueError, match="invalid collection_name"):
            await store.ensure_partition_for_collection("9bad")

    asyncio.run(_run())


def test_document_payload_field_conflict():
    """Document(id, payload=None, **fields) raises TypeError when 'payload' is in fields.

    redis-py passes our stored HASH field name as a keyword arg to Document.__init__,
    which already takes payload as its second parameter — causing 'got multiple values'.
    Fix: store as 'omni_payload' so there is no name clash.
    """
    from redis.commands.search.document import Document

    # Reproduce: redis-py does Document(id, None, **returned_fields) internally.
    # If returned_fields contains "payload" (old field name), this raises TypeError.
    with pytest.raises(TypeError, match="multiple values"):
        Document("doc:col:abc", None, **{"payload": '{"a":1}', "__score": "0.1"})

    # After fix: "omni_payload" does not clash — Document construction succeeds.
    doc = Document("doc:col:abc", None, **{"omni_payload": '{"a":1}', "__score": "0.2"})
    assert getattr(doc, "omni_payload") == '{"a":1}'


def test_docs_to_points_reads_omni_payload():
    """_docs_to_points must read 'omni_payload', not 'payload', from Document objects."""
    from redis.commands.search.document import Document

    doc = Document("doc:col:pid2", None, **{"omni_payload": '{"lane":"SYS_RESOURCE"}', "__score": "0.15"})
    pts = rvs._docs_to_points([doc], score_threshold=None)
    assert len(pts) == 1
    assert pts[0].payload == {"lane": "SYS_RESOURCE"}
    assert pts[0].id == "pid2"


async def _upsert_uses_omni_payload():
    from unittest.mock import AsyncMock, MagicMock, call
    import json

    pipe = AsyncMock()
    pipe.hset = MagicMock()
    pipe.execute = AsyncMock()

    r = MagicMock()
    r.ft.return_value.info = AsyncMock(return_value={})
    r.pipeline.return_value = pipe

    store = rvs.RedisVectorStore(r)
    store._initialized_indexes.add("action_experience")

    vec = [0.0] * rvs.EMBED_DIM
    await store.upsert(
        "action_experience",
        [rvs.PointStruct(id="p1", vector=vec, payload={"text": "hello"})],
    )

    # hset must use "omni_payload", NOT "payload"
    hset_call = pipe.hset.call_args
    mapping = hset_call.kwargs.get("mapping") or hset_call[1].get("mapping") or hset_call[0][1]
    assert "omni_payload" in mapping, "upsert must store field as 'omni_payload'"
    assert "payload" not in mapping, "upsert must NOT store field as 'payload'"


def test_upsert_uses_omni_payload():
    import asyncio
    asyncio.run(_upsert_uses_omni_payload())
