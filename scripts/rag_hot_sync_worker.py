#!/usr/bin/env python3
"""Optional lab hook: Redis hot RAG cache is populated by ``evaluate_rag_gate``; bulk embed via ``training.k8s_official_ingest``."""

from __future__ import annotations

import asyncio


async def main() -> None:
    print(
        "rag_hot_sync_worker: no batch job required — "
        "set OMNI_RAG_HOT_CACHE_ENABLED=true and run k8s_official_ingest for corpus."
    )


if __name__ == "__main__":
    asyncio.run(main())
