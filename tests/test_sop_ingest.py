"""SOP ingest — mock Ollama + PGVectorStore."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from training.sop_ingest import run_ingest
from workers.settings import WorkerSettings

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sop_mini.yaml"


@pytest.mark.asyncio
async def test_run_ingest_dry_run_calls_no_upsert() -> None:
    settings = WorkerSettings(
        sop_seed_path=str(_FIXTURE),
        max_sop_contexts=500,
        sop_ingest_embed_batch=8,
        sop_ingest_upsert_batch=16,
    )
    mock_pool = AsyncMock()
    mock_store = MagicMock()
    mock_store.upsert = AsyncMock()
    mock_store.close = AsyncMock()

    with patch("training.sop_ingest.init_pg_pool", new_callable=AsyncMock, return_value=mock_pool):
        with patch("training.sop_ingest.PGVectorStore", return_value=mock_store):

            async def ollama_embed(*, model: str, input: list | str, **kw):  # noqa: A002
                n = len(input) if isinstance(input, list) else 1
                return {"embeddings": [[0.1] * 768 for _ in range(n)]}

            ollama_close = AsyncMock()
            with patch("training.sop_ingest.OllamaClient") as Oc:
                o = MagicMock()
                o.embed = AsyncMock(side_effect=ollama_embed)
                o.aclose = ollama_close
                Oc.return_value = o
                n = await run_ingest(
                    settings=settings,
                    seed_path=_FIXTURE,
                    dry_run=True,
                )
        assert n == 9
        mock_store.upsert.assert_not_called()


@pytest.mark.asyncio
async def test_run_ingest_upsert_batches() -> None:
    settings = WorkerSettings(
        sop_seed_path=str(_FIXTURE),
        max_sop_contexts=500,
        sop_ingest_embed_batch=4,
        sop_ingest_upsert_batch=8,
        training_ollama_concurrency=2,
    )
    batches: list[int] = []

    async def upsert_side_effect(*, collection_name: str, points: list) -> None:
        batches.append(len(points))

    mock_pool = AsyncMock()
    mock_store = MagicMock()
    mock_store.upsert = AsyncMock(side_effect=upsert_side_effect)
    mock_store.close = AsyncMock()

    with patch("training.sop_ingest.init_pg_pool", new_callable=AsyncMock, return_value=mock_pool):
        with patch("training.sop_ingest.PGVectorStore", return_value=mock_store):

            async def embed_side_effect(*, model: str, input: list, **kw):  # noqa: A002
                n = len(input) if isinstance(input, list) else 1
                return {"embeddings": [[0.02 * (i % 7)] * 768 for i in range(n)]}

            with patch("training.sop_ingest.OllamaClient") as Oc:
                o = MagicMock()
                o.embed = AsyncMock(side_effect=embed_side_effect)
                o.aclose = AsyncMock()
                Oc.return_value = o
                n = await run_ingest(settings=settings, seed_path=_FIXTURE, dry_run=False)
            assert n == 9
            assert sum(batches) == 9
            assert max(batches) <= 8
