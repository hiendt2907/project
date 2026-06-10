#!/usr/bin/env python3
"""[STALE — pgvector đã gỡ khỏi RAG path 2026-06; dùng src/training/advisory_ingest.py + Redis HNSW thay thế.]

Chunk + embed (Ollama) + upsert Markdown local vào ``rag_documents`` (collection = ``OMNI_PGVECTOR_COLLECTION_K8S_EXPERT``).

Cùng bảng/partition với ingest kubernetes.io; phân biệt bằng ``payload.metadata.source`` (local_docs).

Example:
  .venv/bin/python scripts/ingest_sre_knowledge.py --dir docs/vendor/sre_seed --dry-run
"""

from __future__ import annotations

import os as _os

if not _os.getenv("OMNI_PGVECTOR_DSN"):
    raise SystemExit(
        "ingest_sre_knowledge.py is STALE: RAG moved to Redis HNSW (use src/training/advisory_ingest.py). "
        "Set OMNI_PGVECTOR_DSN explicitly if you really have a pgvector instance."
    )

import argparse
import asyncio
import hashlib
import logging
import sys
import uuid
from pathlib import Path

from rag.pgvector_store import (
    COLLECTION_K8S_EXPERT,
    EMBED_DIM,
    PGVectorStore,
    PointStruct,
    PostgresRAGSettings,
    init_pg_pool,
)
from llm.vllm_client import VLLMClient
from workers.settings import WorkerSettings

logger = logging.getLogger(__name__)

_DEFAULT_CHUNK = 1200
_DEFAULT_OVERLAP = 120


def _vecs_from_embed_response(resp: dict) -> list[list[float]]:
    if "embeddings" in resp and resp["embeddings"]:
        out = []
        for emb in resp["embeddings"]:
            out.append(list(emb) if isinstance(emb, list) else list(emb or []))
        return out
    if "embedding" in resp:
        e = resp["embedding"]
        return [list(e) if isinstance(e, list) else list(e or [])]
    raise ValueError("embed response missing embedding(s)")


def _pad_vec(v: list[float]) -> list[float]:
    if len(v) == EMBED_DIM:
        return v
    if len(v) > EMBED_DIM:
        return v[:EMBED_DIM]
    return v + [0.0] * (EMBED_DIM - len(v))


def _chunk_text(text: str, *, size: int, overlap: int) -> list[str]:
    t = (text or "").strip()
    if not t:
        return []
    if len(t) <= size:
        return [t]
    out: list[str] = []
    i = 0
    step = max(1, size - overlap)
    while i < len(t):
        out.append(t[i : i + size])
        i += step
    return out


def _collect_markdown_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.md") if p.is_file())


async def _run(
    *,
    dir_path: Path,
    dry_run: bool,
    chunk_size: int,
    overlap: int,
) -> int:
    ws = WorkerSettings()
    collection = ws.pgvector_collection_k8s_expert.strip()
    files = _collect_markdown_files(dir_path)
    if not files:
        logger.warning("no .md under %s", dir_path)
        return 1

    chunks_meta: list[tuple[Path, int, str]] = []
    for fp in files:
        raw = fp.read_text(encoding="utf-8", errors="replace")
        for idx, ch in enumerate(_chunk_text(raw, size=chunk_size, overlap=overlap)):
            if ch.strip():
                chunks_meta.append((fp, idx, ch))

    if dry_run:
        logger.info("dry-run: %s files → %s chunks", len(files), len(chunks_meta))
        return 0

    pool = await init_pg_pool(PostgresRAGSettings())
    store = PGVectorStore(pool)
    await store.ensure_ready()
    if collection != COLLECTION_K8S_EXPERT:
        await store.ensure_partition_for_collection(collection)
    llm = VLLMClient(base_url=ws.vllm_base_url, embed_url=ws.vllm_embed_url)
    try:
        model = ws.embed_model
        batch = 16
        n_ok = 0
        for i in range(0, len(chunks_meta), batch):
            batch_slice = chunks_meta[i : i + batch]
            texts = [c for _, _, c in batch_slice]
            resp = await llm.embed(model=model, input=texts)
            vecs = _vecs_from_embed_response(resp)
            if len(vecs) != len(texts):
                raise RuntimeError(f"embed batch mismatch want {len(texts)} got {len(vecs)}")
            points: list[PointStruct] = []
            for (fp, idx, ch), vec in zip(batch_slice, vecs, strict=True):
                h = hashlib.sha256(f"{fp}:{idx}:{ch[:200]}".encode()).hexdigest()[:32]
                pid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"sre_knowledge:{h}"))
                rel = str(fp.relative_to(dir_path)) if fp.is_relative_to(dir_path) else str(fp)
                payload = {
                    "text": ch[:8000],
                    "summary": ch[:2000],
                    "source_file": rel,
                    "chunk_index": idx,
                    "ingest": "ingest_sre_knowledge",
                    "metadata": {
                        "source": "local_docs",
                        "url": f"file://{rel}",
                        "type": "documentation",
                        "version": "local",
                    },
                }
                points.append(PointStruct(id=pid, vector=_pad_vec(vec), payload=payload))
            await store.upsert(collection, points)
            n_ok += len(points)
            logger.info("upserted %s / %s chunks", n_ok, len(chunks_meta))
    finally:
        await llm.aclose()
        await pool.close()
    logger.info("done: %s chunks → %s", len(chunks_meta), collection)
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Ingest markdown → rag_documents (expert collection from env)")
    ap.add_argument("--dir", type=Path, required=True, help="Root folder of .md files")
    ap.add_argument("--chunk-size", type=int, default=_DEFAULT_CHUNK)
    ap.add_argument("--overlap", type=int, default=_DEFAULT_OVERLAP)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not args.dir.is_dir():
        logger.error("not a directory: %s", args.dir)
        sys.exit(2)
    rc = asyncio.run(
        _run(
            dir_path=args.dir.resolve(),
            dry_run=args.dry_run,
            chunk_size=max(256, args.chunk_size),
            overlap=max(0, args.overlap),
        )
    )
    sys.exit(rc)


if __name__ == "__main__":
    main()
