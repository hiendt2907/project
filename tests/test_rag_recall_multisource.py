"""Lô D (2026-07-31): RAG thật sự hoạt động — fallback tenant rỗng, thêm SOP,
nhét kiến thức vào prompt chẩn đoán."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from rag.pgvector_store import COLLECTION_ACTION_EXPERIENCE, COLLECTION_SOP
from rag.redis_vector_store import DEFAULT_TENANT_ID


class _FakeVS:
    """Ghi lại (collection, tenant_id) mỗi lần search; trả rỗng cho index tenant khách."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def similarity_search(self, query, collection, *, llm, embed_model,
                               limit, score_threshold, tenant_id):
        self.calls.append((collection, tenant_id))
        # index tenant khách LUÔN rỗng (bug gốc); default có dữ liệu
        pts = []
        if tenant_id == DEFAULT_TENANT_ID:
            pts = [SimpleNamespace(
                id=f"{collection}-1", score=0.6,
                payload={"text_content": f"kb from {collection}", "tool": "advisory_only"},
            )]
        return SimpleNamespace(points=pts)


def _ctx(vs):
    return SimpleNamespace(
        vector_store=vs, llm=object(),
        settings=SimpleNamespace(embed_model="nomic-embed-text"),
        redis=None,
    )


@pytest.mark.asyncio
async def test_recall_falls_back_to_default_and_queries_sop():
    """Tenant khách 'staging-sim' index rỗng ⇒ phải fallback default + tra SOP."""
    from workers.archivist import recall_playbook_advisory

    vs = _FakeVS()
    out = await recall_playbook_advisory(
        _ctx(vs), query_text="nginx port lost", trace="t", tenant_id="staging-sim",
    )
    # đã thử: (experience, staging-sim) → (experience, default) → (sop, default)
    assert (COLLECTION_ACTION_EXPERIENCE, "staging-sim") in vs.calls
    assert (COLLECTION_ACTION_EXPERIENCE, DEFAULT_TENANT_ID) in vs.calls
    assert (COLLECTION_SOP, DEFAULT_TENANT_ID) in vs.calls
    # có hit từ default (không còn None dù index tenant rỗng)
    assert out is not None


@pytest.mark.asyncio
async def test_knowledge_context_digest_for_prompt():
    """recall_knowledge_context trả digest text để nhét prompt, gồm cả SOP."""
    from workers.archivist import recall_knowledge_context

    vs = _FakeVS()
    text = await recall_knowledge_context(
        _ctx(vs), query_text="nginx port lost", tenant_id="staging-sim",
    )
    assert "kb from" in text
    assert (COLLECTION_SOP, DEFAULT_TENANT_ID) in vs.calls


@pytest.mark.asyncio
async def test_diagnosis_prompt_includes_knowledge_when_present():
    from services.analyst.diagnosis_loop import _build_initial_context

    p = _build_initial_context({}, {"probe": "x"}, "KB: check sites-enabled")
    assert "KIẾN THỨC LIÊN QUAN" in p and "check sites-enabled" in p
    assert "KIẾN THỨC" not in _build_initial_context({}, {"probe": "x"}, "")
