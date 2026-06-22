"""Cross-tenant-leak tests for RAG vector store + semantic cache (onboarding-ops-agent plan, step 1)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from rag import redis_vector_store as rvs
from rag import semantic_cache as sc


def test_scoped_collection_name_default_is_legacy_unscoped():
    assert rvs.scoped_collection_name("k8s_expert") == "k8s_expert"
    assert rvs.scoped_collection_name("k8s_expert", "default") == "k8s_expert"


def test_scoped_collection_name_isolates_distinct_tenants():
    a = rvs.scoped_collection_name("itops_sop_ledger", "tenantA")
    b = rvs.scoped_collection_name("itops_sop_ledger", "tenantB")
    assert a != b
    assert a == "itops_sop_ledger:tenantA"
    assert b == "itops_sop_ledger:tenantB"


def test_scoped_collection_name_invalid_tenant_id_rejected():
    with pytest.raises(ValueError, match="invalid tenant_id"):
        rvs.scoped_collection_name("k8s_expert", "bad id with spaces")
    with pytest.raises(ValueError, match="invalid tenant_id"):
        rvs.scoped_collection_name("k8s_expert", "")


def test_validate_tenant_id_accepts_safe_charset():
    assert rvs.validate_tenant_id("tenant-A_1") == "tenant-A_1"


async def _upsert_routes_to_tenant_scoped_index():
    pipe = AsyncMock()
    pipe.hset = MagicMock()
    pipe.execute = AsyncMock()

    r = MagicMock()
    r.ft.return_value.info = AsyncMock(return_value={})
    r.pipeline.return_value = pipe

    store = rvs.RedisVectorStore(r)
    # Pretend the tenant-scoped index already exists so upsert doesn't try to create it.
    store._initialized_indexes.add("action_experience:tenantA")

    vec = [0.0] * rvs.EMBED_DIM
    await store.upsert(
        "action_experience",
        [rvs.PointStruct(id="p1", vector=vec, payload={"text": "hello"})],
        tenant_id="tenantA",
    )

    hset_call = pipe.hset.call_args
    key = hset_call[0][0] if hset_call[0] else hset_call.kwargs.get("name")
    assert "action_experience:tenantA" in key
    assert "action_experience:tenantB" not in key


def test_upsert_routes_to_tenant_scoped_index():
    asyncio.run(_upsert_routes_to_tenant_scoped_index())


async def _ensure_partition_uses_tenant_scoped_name():
    r = MagicMock()
    r.ft.return_value.info = AsyncMock(side_effect=Exception("no such index"))
    r.ft.return_value.create_index = AsyncMock()

    store = rvs.RedisVectorStore(r)
    await store.ensure_partition_for_collection("k8s_expert", tenant_id="tenantA")

    create_call = r.ft.call_args
    assert create_call[0][0] == "idx:k8s_expert:tenantA"


def test_ensure_partition_uses_tenant_scoped_name():
    asyncio.run(_ensure_partition_uses_tenant_scoped_name())


async def _set_writes_tenant_id_field():
    r = MagicMock()
    r.json.return_value.set = AsyncMock()
    r.expire = AsyncMock()

    cache = sc.SemanticCache(r)
    cache._ready = True  # skip ensure_ready() index bootstrap

    result = rvs.QueryResponse(points=[])
    vec = [0.0] * rvs.EMBED_DIM
    await cache.set(vec, result, tenant_id="tenantA")

    set_call = r.json.return_value.set.call_args
    doc = set_call[0][2]
    assert doc["tenant_id"] == "tenantA"


def test_set_writes_tenant_id_field():
    asyncio.run(_set_writes_tenant_id_field())


async def _get_filters_query_by_tenant_id():
    r = MagicMock()
    fake_results = MagicMock()
    fake_results.docs = []
    r.ft.return_value.search = AsyncMock(return_value=fake_results)

    cache = sc.SemanticCache(r)
    cache._ready = True  # skip ensure_ready() index bootstrap

    vec = [0.0] * rvs.EMBED_DIM
    await cache.get(vec, tenant_id="tenantA")

    search_call = r.ft.return_value.search.call_args
    query_obj = search_call[0][0]
    query_str = query_obj.query_string()
    assert "@tenant_id:{tenantA}" in query_str


def test_get_filters_query_by_tenant_id():
    asyncio.run(_get_filters_query_by_tenant_id())
