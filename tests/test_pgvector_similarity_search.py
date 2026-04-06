"""PGVectorStore.similarity_search + embed parsing."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from rag.pgvector_store import PGVectorStore, _embedding_vector_from_ollama_response


def test_embedding_vector_from_ollama_single() -> None:
    v = _embedding_vector_from_ollama_response({"embedding": [0.1, 0.2, 0.3]})
    assert v == [0.1, 0.2, 0.3]


def test_embedding_vector_from_ollama_batch_first() -> None:
    v = _embedding_vector_from_ollama_response({"embeddings": [[1.0, 2.0]]})
    assert v == [1.0, 2.0]


@pytest.mark.asyncio
async def test_similarity_search_delegates_to_query_points() -> None:
    pool = MagicMock()
    store = PGVectorStore(pool)
    store.query_points = AsyncMock(
        return_value=MagicMock(points=[]),
    )
    ollama = AsyncMock()
    ollama.embed = AsyncMock(return_value={"embedding": [0.5] * 768})
    await store.similarity_search(
        "pod crash",
        "k8s_expert",
        ollama=ollama,
        embed_model="nomic-embed-text:latest",
        keep_alive="5m",
        limit=3,
        score_threshold=0.4,
    )
    ollama.embed.assert_awaited_once()
    store.query_points.assert_awaited_once()
    call_kw = store.query_points.call_args.kwargs
    assert call_kw["collection_name"] == "k8s_expert"
    assert call_kw["limit"] == 3
    assert len(call_kw["query"]) == 768


@pytest.mark.asyncio
async def test_similarity_search_requires_ollama() -> None:
    pool = MagicMock()
    store = PGVectorStore(pool)
    with pytest.raises(TypeError):
        await store.similarity_search("x", "k8s_expert", ollama=None, embed_model="m")  # type: ignore[arg-type]
