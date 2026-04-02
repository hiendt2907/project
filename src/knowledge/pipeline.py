from __future__ import annotations

import hashlib
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from knowledge.chunk import chunk_by_markdown_headings
from knowledge.clean import assert_no_embed_raw_html, clean_html, clean_plain
from knowledge.fetch_crawl import fetch_url_jina
from knowledge.fetch_local import fetch_local_markdown
from knowledge.models import RawDocument, SourceEntry
from rag.pgvector_store import COLLECTION_VENDOR_KNOWLEDGE, PointStruct


def _normalize_after_fetch(doc: RawDocument) -> str:
    """Mandatory clean step — HTML never reaches chunk/embed unchanged."""
    if doc.content_type == "html":
        text = clean_html(doc.raw_text)
    else:
        text = clean_plain(doc.raw_text)
    assert_no_embed_raw_html(text)
    return text


async def raw_documents_for_source(entry: SourceEntry) -> list[RawDocument]:
    if entry.backend == "local":
        if not entry.local_dir:
            return []
        return await fetch_local_markdown(entry.id, entry.local_dir)
    if entry.backend == "jina":
        if not entry.url:
            return []
        return [await fetch_url_jina(entry.url, entry.id)]
    raise ValueError(f"unsupported backend: {entry.backend}")


async def run_pipeline_for_entry(
    entry: SourceEntry,
    embed_fn: Callable[[list[str]], Awaitable[list[list[float]]]],
    *,
    collection_name: str = COLLECTION_VENDOR_KNOWLEDGE,
) -> list[PointStruct]:
    """Fetch → clean → chunk → embed. No raw HTML to embed. ``collection_name`` reserved for metrics/logging."""
    _ = collection_name
    docs = await raw_documents_for_source(entry)
    all_points: list[PointStruct] = []
    for doc in docs:
        raw_for_hash = doc.raw_text.encode("utf-8")
        content_hash = hashlib.sha256(raw_for_hash).hexdigest()
        normalized = _normalize_after_fetch(doc)
        chunks = chunk_by_markdown_headings(normalized)
        if not chunks:
            continue
        embed_texts: list[str] = []
        metas: list[dict[str, Any]] = []
        for ch in chunks:
            body = ch.body.strip()
            if not body:
                continue
            assert_no_embed_raw_html(body)
            embed_texts.append(body[:8000])
            metas.append(
                {
                    "embed_text": body[:8000],
                    "chunk_type": "raw_section",
                    "source_url": doc.source_key if doc.source_key.startswith("http") else "",
                    "source_path": doc.source_key if not doc.source_key.startswith("http") else "",
                    "layer": entry.layer,
                    "doc_version": entry.version,
                    "heading_path": ch.heading_path,
                    "content_hash": content_hash,
                    "ingested_at": datetime.now(UTC).isoformat(),
                    "citation_text": body[:2000],
                }
            )
        if not embed_texts:
            continue
        vectors = await embed_fn(embed_texts)
        for vec, meta in zip(vectors, metas, strict=True):
            pid = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"vendor:{entry.id}:{meta['content_hash']}:{meta['heading_path']}:{meta['embed_text'][:64]}",
                )
            )
            all_points.append(PointStruct(id=pid, vector=vec, payload=meta))
    return all_points
