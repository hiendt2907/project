"""CLI: ``python -m knowledge.ingest_main`` — vendor knowledge → PGVector."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from knowledge.config import load_knowledge_sources
from knowledge.pipeline import run_pipeline_for_entry
from llm.vllm_client import VLLMClient
from rag.pgvector_store import COLLECTION_VENDOR_KNOWLEDGE, PGVectorStore, PostgresRAGSettings, init_pg_pool
from training.sop_ingest import _embed_batch  # noqa: PLC2701 — shared embed helper
from workers.settings import WorkerSettings

logger = logging.getLogger(__name__)


async def _run(sources_path: str, *, dry_run: bool, limit_sources: int | None) -> int:
    logging.basicConfig(level=logging.INFO)
    cfg = load_knowledge_sources(sources_path)
    ws = WorkerSettings()
    llm = VLLMClient(base_url=ws.vllm_base_url, embed_url=ws.vllm_embed_url, timeout_s=120.0)

    async def embed_fn(texts: list[str]) -> list[list[float]]:
        return await _embed_batch(
            ollama,
            model=ws.embed_model,
            texts=texts,
        )

    entries = cfg.sources
    if limit_sources is not None:
        entries = entries[:limit_sources]

    all_points = []
    for ent in entries:
        pts = await run_pipeline_for_entry(ent, embed_fn)
        logger.info("source=%s points=%d", ent.id, len(pts))
        all_points.extend(pts)

    if dry_run:
        logger.info("dry-run: would upsert %d points", len(all_points))
        return 0

    if not all_points:
        logger.warning("no points to upsert")
        return 0

    pg_pool = await init_pg_pool(PostgresRAGSettings())
    store = PGVectorStore(pg_pool)
    await store.ensure_ready()
    batch = ws.knowledge_ingest_embed_batch
    for i in range(0, len(all_points), batch):
        chunk = all_points[i : i + batch]
        await store.upsert(COLLECTION_VENDOR_KNOWLEDGE, chunk)
    await store.close()
    logger.info("upserted %d points into %s", len(all_points), COLLECTION_VENDOR_KNOWLEDGE)
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Ingest vendor knowledge (clean → chunk → embed).")
    p.add_argument(
        "--sources",
        default=None,
        help="Path to knowledge_sources.yaml (default: OMNI_KNOWLEDGE_SOURCES or /app/config/knowledge_sources.yaml)",
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int, default=None, help="Max number of source entries")
    args = p.parse_args()
    ws = WorkerSettings()
    path = args.sources or ws.knowledge_sources_path
    rc = asyncio.run(_run(path, dry_run=args.dry_run, limit_sources=args.limit))
    sys.exit(rc)


if __name__ == "__main__":
    main()
