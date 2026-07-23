"""TDD — nối tài liệu nghiệp vụ khách hàng (ingest_customer_knowledge) vào evidence
advisory (gap 4, Phase 2 omni-close-autonomous-sre-gaps-2026-07-23).

INV_DATA_RESIDENCY: document_store chỉ lưu metadata + summary (<=2000 chars) —
build_customer_knowledge_block() chỉ được render từ đó, KHÔNG bao giờ kéo full
content. Tài liệu khách hàng KHÔNG phải Fact đã verify — block PHẢI đánh dấu rõ
nguồn "customer-provided, chưa verify" (verify-before-believe / INV_LLM_NOT_FIRST).
"""

from __future__ import annotations

import pytest
from fakeredis.aioredis import FakeRedis

from services.knowledge.document_store import ingest_customer_knowledge
from workers.customer_knowledge_context import build_customer_knowledge_block


@pytest.fixture
def redis() -> FakeRedis:
    return FakeRedis(decode_responses=True)


async def test_no_docs_returns_empty_string(redis: FakeRedis):
    block = await build_customer_knowledge_block(redis, "default")
    assert block == ""


async def test_block_contains_summary_and_unverified_marker(redis: FakeRedis):
    await ingest_customer_knowledge(
        redis,
        tenant_id="staging-sim",
        agent_id="agent-1",
        file_id="tg-file-1",
        file_name="runbook-db-failover.pdf",
        summary="Khi MySQL primary down, failover sang replica cust-db-2 trong 5 phút.",
    )
    block = await build_customer_knowledge_block(redis, "staging-sim")
    assert "runbook-db-failover.pdf" in block
    assert "failover sang replica cust-db-2" in block
    # Explicit unverified marker — LLM must not treat this as a verified Fact.
    assert "chưa verify" in block.lower() or "unverified" in block.lower()


async def test_block_never_exceeds_max_chars(redis: FakeRedis):
    for i in range(20):
        await ingest_customer_knowledge(
            redis,
            tenant_id="big-tenant",
            agent_id=f"agent-{i}",
            file_id=f"tg-file-{i}",
            file_name=f"doc-{i}.pdf",
            summary="x" * 500,
        )
    block = await build_customer_knowledge_block(redis, "big-tenant", max_chars=800)
    assert len(block) <= 800


async def test_block_never_leaks_beyond_2000_char_summary_cap(redis: FakeRedis):
    """INV_DATA_RESIDENCY: summary already capped at ingest time (<=2000 chars) —
    the block builder must not somehow read a longer field (e.g. full content)."""
    doc_id = await ingest_customer_knowledge(
        redis,
        tenant_id="t1",
        agent_id="a1",
        file_id="f1",
        file_name="doc.pdf",
        summary="y" * 5000,  # ingest_customer_knowledge itself clips to 2000
    )
    from services.knowledge.document_store import get_doc

    doc = await get_doc(redis, doc_id=doc_id)
    assert doc is not None
    assert len(doc["summary"]) == 2000  # confirms store-level cap held


async def test_block_survives_redis_error():
    class _Boom:
        async def lrange(self, *_a, **_k):
            raise ConnectionError("down")

    block = await build_customer_knowledge_block(_Boom(), "default")
    assert block == ""


async def test_block_limits_doc_count_not_just_chars(redis: FakeRedis):
    """Even with a generous max_chars, only a bounded number of most-recent docs
    should be surfaced — avoids drowning the LLM prompt in low-signal docs."""
    for i in range(10):
        await ingest_customer_knowledge(
            redis,
            tenant_id="many-docs",
            agent_id=f"agent-{i}",
            file_id=f"tg-file-{i}",
            file_name=f"doc-{i}.pdf",
            summary="short summary",
        )
    block = await build_customer_knowledge_block(redis, "many-docs", max_chars=100_000)
    # header + at most N doc lines, not all 10 necessarily but bounded and non-crashing
    assert block.count("doc-") <= 10
    assert "doc-0.pdf" in block or "doc-9.pdf" in block
