"""Embed (Ollama) + bulk upsert SOP vào Postgres `itops_sop_ledger`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from rag.pgvector_store import (
    EMBED_DIM, 
    PGVectorStore, 
    PointStruct, 
    PostgresRAGSettings, 
    init_pg_pool
)
from rag.sop_ledger import SOP_COLLECTION, sop_payload_for_fast_path
from llm.ollama_client import OllamaClient
from training.sop_expand import expand_entries, load_seed_path
from workers.settings import WorkerSettings

logger = logging.getLogger(__name__)


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


async def _embed_batch(
    ollama: OllamaClient,
    *,
    model: str,
    texts: list[str],
    keep_alive: str | None,
) -> list[list[float]]:
    resp = await ollama.embed(
        model=model,
        input=texts,
        keep_alive=keep_alive,
    )
    vecs = _vecs_from_embed_response(resp)
    if len(vecs) != len(texts):
        raise RuntimeError(f"embed batch size mismatch: want {len(texts)} got {len(vecs)}")
    return [_pad_vec(v) for v in vecs]


async def run_ingest(
    *,
    settings: WorkerSettings,
    seed_path: Path,
    max_points: int | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> int:
    cap = min(
        max_points if max_points is not None else settings.max_sop_contexts,
        settings.max_sop_contexts,
    )
    seed = load_seed_path(seed_path)
    entries = expand_entries(
        seed,
        max_total=cap,
        shuffle_seed=settings.sop_expand_seed,
        god_mode=bool(settings.god_mode or settings.lab_unchained),
    )
    if limit is not None:
        entries = entries[: max(0, limit)]

    ollama = OllamaClient(base_url=settings.ollama_base_url, timeout_s=120.0)
    pg_settings = PostgresRAGSettings()
    pg_pool = await init_pg_pool(pg_settings)
    vector_store = PGVectorStore(pg_pool)
    sem = asyncio.Semaphore(settings.training_ollama_concurrency)
    upsert_batch = settings.sop_ingest_upsert_batch
    embed_batch = settings.sop_ingest_embed_batch
    log_every = settings.sop_ingest_log_every

    try:
        # Index creation is handled by schema.sql
        buf: list[PointStruct] = []
        done = 0

        async def flush() -> None:
            nonlocal buf, done
            if not buf:
                return
            if dry_run:
                buf = []
                return
            await vector_store.upsert(collection_name=SOP_COLLECTION, points=buf)
            done += len(buf)
            if done == len(entries) or (log_every > 0 and done % log_every == 0):
                logger.info("sop_ingest upserted total=%s / %s", done, len(entries))
            buf = []

        for i in range(0, len(entries), embed_batch):
            chunk = entries[i : i + embed_batch]
            texts = [e.match_text[:8000] for e in chunk]

            async with sem:
                vecs = await _embed_batch(
                    ollama,
                    model=settings.embed_model,
                    texts=texts,
                    keep_alive=settings.ollama_keep_alive,
                )

            for e, vec in zip(chunk, vecs, strict=True):
                payload = sop_payload_for_fast_path(
                    match_text=e.match_text,
                    tool=e.tool,
                    args=e.args,
                    auto_execute=e.auto_execute,
                    template_id=e.template_id,
                    variant_key=e.variant_key,
                )
                buf.append(PointStruct(id=e.point_id, vector=vec, payload=payload))
                if len(buf) >= upsert_batch:
                    await flush()

        await flush()

        if dry_run:
            logger.info("sop_ingest dry_run: would upsert %s points", len(entries))
            return len(entries)

        logger.info("sop_ingest complete: %s points", done)
        return done
    finally:
        await ollama.aclose()
        await vector_store.close()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Ingest SOP seed → Postgres itops_sop_ledger")
    p.add_argument("--path", type=str, default=None, help="YAML seed path (default: OMNI_SOP_SEED_PATH)")
    p.add_argument("--limit", type=int, default=None, help="Chỉ ingest N điểm đầu (test)")
    p.add_argument("--max", type=int, default=None, help="Override cap (≤ OMNI_MAX_SOP_CONTEXTS)")
    p.add_argument("--dry-run", action="store_true", help="Không ghi Postgres RAG")
    p.add_argument("-v", action="store_true", dest="verbose", help="DEBUG log")
    return p.parse_args(argv)


async def _amain(argv: list[str] | None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s %(message)s",
    )
    settings = WorkerSettings()
    path = Path(args.path or settings.sop_seed_path)
    if not path.is_file():
        logger.error("seed file missing: %s", path.resolve())
        return 2
    await run_ingest(
        settings=settings,
        seed_path=path,
        max_points=args.max,
        limit=args.limit,
        dry_run=args.dry_run,
    )
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_amain(sys.argv[1:])))


if __name__ == "__main__":
    main()
