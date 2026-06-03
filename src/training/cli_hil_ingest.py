"""Ingest 100k cli_hil_context: tiếng Việt + suggested_commands → Ollama embed → Redis RAG."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis

from llm.factory import build_llm_client
from rag.redis_vector_store import RedisVectorStore, PostgresRAGSettings
from rag.pgvector_store import (
    COLLECTION_CLI_HIL_CONTEXT,
    PointStruct,
)
from training.cli_hil_pools import GENERATOR_VERSION, MAX_COMBINATIONS, cli_hil_payload, generate_cli_hil_entry
from training import sop_ingest as sop_ingest_mod
from workers.settings import WorkerSettings

logger = logging.getLogger(__name__)

DEFAULT_COUNT = 100_000


def _read_checkpoint(path: Path) -> int:
    if not path.is_file():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        last = int(data.get("last_completed_index", -1))
        return max(0, last + 1)
    except (json.JSONDecodeError, OSError, TypeError, ValueError) as e:
        logger.warning("checkpoint read failed %s: %s — starting from 0", path, e)
        return 0


def _write_checkpoint(path: Path, last_completed_index: int) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    body = json.dumps(
        {"version": 1, "last_completed_index": last_completed_index},
        ensure_ascii=False,
        indent=0,
    )
    tmp.write_text(body + "\n", encoding="utf-8")
    tmp.replace(path)


def build_run_commands_json(
    *,
    settings: WorkerSettings,
    collection: str,
    count: int,
    dry_run: bool,
    checkpoint: str | None,
) -> dict[str, Any]:
    """Lệnh mẫu để chạy ingest (local + gợi ý Job K8s)."""
    local = (
        f"PYTHONPATH=src OMNI_VLLM_BASE_URL={settings.vllm_base_url} "
        f"OMNI_REDIS_URL=${{OMNI_REDIS_URL:-redis://redis:6379/0}} "
        f"CLI_HIL_COLLECTION={collection} "
        f".venv/bin/python -m training.cli_hil_ingest --count {count}"
        + (" --dry-run" if dry_run else "")
        + (f" --checkpoint {checkpoint}" if checkpoint else "")
        + (" --json-stdout" if not dry_run else "")
    )
    cmds: list[str] = [
        local,
        "kubectl -n multi-agent delete job/cli-hil-ingest --ignore-not-found=true",
        (
            "# Gợi ý: tạo Job một shot tương tự sop-ingest-job.yaml "
            f"(image multi-agent-system:latest, command python -m training.cli_hil_ingest --count {count})"
        ),
    ]
    return {
        "ingest_summary": {
            "collection": collection,
            "points_target": count,
            "dry_run": dry_run,
        },
        "commands": cmds,
    }


async def run_cli_hil_ingest(
    *,
    settings: WorkerSettings,
    count: int,
    dry_run: bool,
    checkpoint_path: Path | None,
    start_index: int | None,
    collection: str,
    embed_batch: int,
    upsert_batch: int,
    log_every: int,
) -> int:
    if count < 1 or count > MAX_COMBINATIONS:
        raise ValueError(f"count must be 1..{MAX_COMBINATIONS}, got {count}")

    begin = start_index if start_index is not None else 0
    if checkpoint_path is not None and start_index is None:
        begin = _read_checkpoint(checkpoint_path)

    end = min(begin + count, MAX_COMBINATIONS)
    total_to_process = end - begin

    if dry_run:
        for i in range(begin, end):
            generate_cli_hil_entry(i)
        logger.info(
            "cli_hil_ingest dry_run: would upsert %s points (indices %s..%s)",
            total_to_process,
            begin,
            end - 1,
        )
        return total_to_process

    llm = build_llm_client(base_url=settings.vllm_base_url, embed_url=settings.vllm_embed_url, timeout_s=120.0)
    redis_url = os.environ.get("OMNI_REDIS_URL", "redis://redis:6379/0")
    r = aioredis.from_url(redis_url, decode_responses=False)
    vector_store = RedisVectorStore(r)
    await vector_store.ensure_ready()
    sem = asyncio.Semaphore(settings.training_llm_concurrency)
    done = 0

    try:
        # Schema is pre-initialized via schema.sql
        buf: list[PointStruct] = []

        async def flush_batch(last_index: int) -> None:
            nonlocal buf, done
            if not buf:
                return
            await vector_store.upsert(collection_name=collection, points=buf)
            done += len(buf)
            if checkpoint_path is not None:
                _write_checkpoint(checkpoint_path, last_index)
            if log_every > 0 and done % log_every == 0:
                logger.info("cli_hil_ingest upserted total=%s / %s", done, total_to_process)
            buf = []

        idx = begin
        while idx < end:
            chunk_end = min(idx + embed_batch, end)
            chunk = list(range(idx, chunk_end))
            entries = [generate_cli_hil_entry(i) for i in chunk]
            texts = [e.embed_text[:8000] for e in entries]

            async with sem:
                vecs = await sop_ingest_mod._embed_batch(
                    llm,
                    model=settings.embed_model,
                    texts=texts,
                    
                )

            for e, vec in zip(entries, vecs, strict=True):
                buf.append(
                    PointStruct(
                        id=e.point_id,
                        vector=vec,
                        payload=cli_hil_payload(e),
                    )
                )
                if len(buf) >= upsert_batch:
                    await flush_batch(e.index)

            idx = chunk_end

        if buf:
            await flush_batch(end - 1)

        logger.info("cli_hil_ingest complete: %s points", done)
        return done
    finally:
        await llm.aclose()
        await vector_store.close()
        await r.aclose()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Ingest cli_hil_context (VN + CLI hints) → Redis RAG")
    p.add_argument("--count", type=int, default=DEFAULT_COUNT, help=f"Số điểm (max {MAX_COMBINATIONS})")
    p.add_argument("--start-index", type=int, default=None, help="Bắt đầu từ index (không dùng checkpoint)")
    p.add_argument("--dry-run", action="store_true", help="Không ghi Postgres RAG")
    p.add_argument(
        "--checkpoint",
        type=str,
        default=None,
        help="File JSON: last_completed_index để resume",
    )
    p.add_argument(
        "--collection",
        type=str,
        default=None,
        help="Override collection name (default: cli_hil_context hoặc $CLI_HIL_COLLECTION)",
    )
    p.add_argument("--embed-batch", type=int, default=None, help="Override embed batch size")
    p.add_argument("--upsert-batch", type=int, default=None, help="Override upsert batch size")
    p.add_argument("--write-json", type=str, default=None, help="Ghi JSON kết quả + lệnh ra file")
    p.add_argument("--json-stdout", action="store_true", help="In JSON cuối ra stdout")
    p.add_argument("-v", action="store_true", dest="verbose", help="DEBUG log")
    return p.parse_args(argv)


async def _amain(argv: list[str] | None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s %(message)s",
    )
    settings = WorkerSettings()
    collection = args.collection or os.environ.get("CLI_HIL_COLLECTION") or COLLECTION_CLI_HIL_CONTEXT
    embed_batch = args.embed_batch or settings.sop_ingest_embed_batch
    upsert_batch = args.upsert_batch or settings.sop_ingest_upsert_batch
    log_every = settings.sop_ingest_log_every

    cp = Path(args.checkpoint).resolve() if args.checkpoint else None

    n = await run_cli_hil_ingest(
        settings=settings,
        count=args.count,
        dry_run=args.dry_run,
        checkpoint_path=cp,
        start_index=args.start_index,
        collection=collection,
        embed_batch=embed_batch,
        upsert_batch=upsert_batch,
        log_every=log_every,
    )

    out = build_run_commands_json(
        settings=settings,
        collection=collection,
        count=args.count,
        dry_run=args.dry_run,
        checkpoint=args.checkpoint,
    )
    out["ingest_summary"]["points_upserted_or_simulated"] = n
    out["ingest_summary"]["generator_version"] = GENERATOR_VERSION

    if args.json_stdout or args.write_json:
        text = json.dumps(out, ensure_ascii=False, indent=2)
        if args.json_stdout:
            print(text)
        if args.write_json:
            Path(args.write_json).write_text(text + "\n", encoding="utf-8")

    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_amain(sys.argv[1:])))


if __name__ == "__main__":
    main()
