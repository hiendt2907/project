"""Crawl kubernetes.io (in-memory) → chunk → Ollama embed → UPSERT ``rag_documents`` (collection từ env).

Không tạo file trung gian; không tạo bảng mới — dùng ``PGVectorStore`` + partition ``k8s_expert`` (tên override ``OMNI_PGVECTOR_COLLECTION_K8S_EXPERT``).

Chạy:
  PYTHONPATH=src .venv/bin/python -m training.k8s_official_ingest --dry-run
  PYTHONPATH=src .venv/bin/python -m training.k8s_official_ingest
  PYTHONPATH=src .venv/bin/python -m training.k8s_official_ingest --max-pages 200

Env: ``OMNI_K8S_OFFICIAL_DOCS_*``, ``OMNI_K8S_OFFICIAL_SITEMAP_URL``, ``OMNI_K8S_OFFICIAL_SITEMAP_MAX_URLS``.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import logging
import os
import re
import uuid
from collections import deque
from typing import Any
from urllib.parse import urldefrag, urljoin, urlparse

import httpx
import redis.asyncio as aioredis

from llm.factory import build_llm_client
from rag.redis_vector_store import RedisVectorStore, PostgresRAGSettings
from rag.pgvector_store import (
    COLLECTION_K8S_EXPERT,
    EMBED_DIM,
    PointStruct,
)
from workers.settings import WorkerSettings

logger = logging.getLogger(__name__)

_HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.I)
_SCRIPT_RE = re.compile(r"<script[\s\S]*?</script>", re.I)
_STYLE_RE = re.compile(r"<style[\s\S]*?</style>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def _html_to_text(raw: str) -> str:
    s = _SCRIPT_RE.sub(" ", raw)
    s = _STYLE_RE.sub(" ", s)
    s = _TAG_RE.sub(" ", s)
    return html.unescape(re.sub(r"\s+", " ", s)).strip()


def _normalize_url(base: str, href: str) -> str | None:
    joined = urljoin(base, href)
    joined, _frag = urldefrag(joined)
    if joined.startswith("http://"):
        joined = "https://" + joined[len("http://") :]
    return joined or None


def _infer_doc_type(path: str) -> str:
    p = path.lower()
    if "troubleshoot" in p or "/debug" in p or "debug-" in p:
        return "troubleshooting"
    if "/tasks/" in p:
        return "troubleshooting"
    return "reference"


def _infer_knowledge_level(path: str) -> str:
    """Phase C: L1 symptom / L2 investigation / L3 resolution (metadata.level)."""
    p = (path or "").lower()
    if "troubleshoot" in p or "debug" in p:
        return "symptom"
    if "/tasks/" in p:
        return "resolution"
    return "investigation"


def _chunk_text(text: str, *, size: int, overlap: int) -> list[str]:
    t = (text or "").strip()
    if not t:
        return []
    if len(t) <= size:
        return [t]
    out: list[str] = []
    step = max(1, size - overlap)
    i = 0
    while i < len(t):
        out.append(t[i : i + size])
        i += step
    return out


def _vecs_from_embed_response(resp: dict[str, Any]) -> list[list[float]]:
    if "embeddings" in resp and resp["embeddings"]:
        out: list[list[float]] = []
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


_SITEMAP_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)


async def _fetch_sitemap_seed_urls(
    client: httpx.AsyncClient,
    *,
    sitemap_url: str,
    allowed_host: str,
    path_prefix: str,
    max_urls: int,
) -> list[str]:
    """Parse URLs from sitemap XML (best-effort; robots/sitemap index)."""
    if max_urls <= 0 or not (sitemap_url or "").strip():
        return []
    try:
        r = await client.get(sitemap_url.strip())
        if r.status_code != 200:
            logger.warning("sitemap HTTP %s status=%s", sitemap_url, r.status_code)
            return []
        body = r.text or ""
    except Exception as e:
        logger.warning("sitemap fetch fail %s: %s", sitemap_url, e)
        return []
    locs = _SITEMAP_LOC_RE.findall(body)
    if not locs and "<sitemapindex" in body.lower():
        logger.info(
            "sitemap appears to be an index; set OMNI_K8S_OFFICIAL_SITEMAP_URL to a concrete sitemap or rely on seeds"
        )
    out: list[str] = []
    seen: set[str] = set()
    ah = allowed_host.lower()
    for raw in locs:
        u = (raw or "").strip()
        if not u:
            continue
        nu = _normalize_url(sitemap_url, u) or u
        pu = urlparse(nu)
        if (pu.hostname or "").lower() != ah:
            continue
        if not (pu.path or "/").startswith(path_prefix):
            continue
        if nu in seen:
            continue
        seen.add(nu)
        out.append(nu)
        if len(out) >= max_urls:
            break
    logger.info("sitemap seed urls=%s (cap=%s)", len(out), max_urls)
    return out


async def crawl_k8s_docs(ws: WorkerSettings) -> list[tuple[str, str]]:
    """Trả về [(url, plain_text), ...] — chỉ HTML, không ghi đĩa."""
    base_root = ws.k8s_official_docs_base_url.strip().rstrip("/")
    parsed_root = urlparse(base_root)
    allowed_host = (parsed_root.hostname or "kubernetes.io").lower()
    path_prefix = ws.k8s_official_docs_path_prefix
    if not path_prefix.startswith("/"):
        path_prefix = "/" + path_prefix

    seeds = [u.strip() for u in ws.k8s_official_docs_seed_urls.split(",") if u.strip()]
    if not seeds:
        seeds = [f"{base_root}/docs/home/"]

    max_pages = int(ws.k8s_official_crawl_max_pages)
    max_depth = int(ws.k8s_official_crawl_max_depth)
    delay = float(ws.k8s_official_request_delay_sec)
    timeout = float(ws.k8s_official_crawl_timeout_sec)

    seen: set[str] = set()
    queue: deque[tuple[str, int]] = deque()
    for s in seeds:
        nu = _normalize_url(base_root + "/", s)
        if nu:
            queue.append((nu, 0))

    pages: list[tuple[str, str]] = []
    headers = {"User-Agent": ws.k8s_official_user_agent, "Accept": "text/html,application/xhtml+xml"}

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as client:
        sm_url = (getattr(ws, "k8s_official_sitemap_url", None) or "").strip()
        sm_cap = int(getattr(ws, "k8s_official_sitemap_max_urls", 0) or 0)
        if sm_url and sm_cap > 0:
            extra = await _fetch_sitemap_seed_urls(
                client,
                sitemap_url=sm_url,
                allowed_host=allowed_host,
                path_prefix=path_prefix,
                max_urls=sm_cap,
            )
            for u in extra:
                queue.append((u, 0))

        while queue and len(pages) < max_pages:
            url, depth = queue.popleft()
            if url in seen:
                continue
            pu = urlparse(url)
            if (pu.hostname or "").lower() != allowed_host:
                continue
            path = pu.path or "/"
            if not path.startswith(path_prefix):
                continue
            seen.add(url)

            try:
                r = await client.get(url)
                if delay > 0:
                    await asyncio.sleep(delay)
            except Exception as e:
                logger.warning("fetch fail %s: %s", url, e)
                continue

            ctype = (r.headers.get("content-type") or "").lower()
            if r.status_code != 200 or "text/html" not in ctype:
                continue

            body = r.text or ""
            text = _html_to_text(body)
            if len(text) < 80:
                continue
            pages.append((url, text))
            logger.info("crawled page %s/%s: %s (chars=%s)", len(pages), max_pages, url, len(text))

            if depth < max_depth:
                for m in _HREF_RE.findall(body):
                    if m.startswith("#") or m.startswith("mailto:") or m.startswith("javascript:"):
                        continue
                    nxt = _normalize_url(url, m)
                    if not nxt or nxt in seen:
                        continue
                    pn = urlparse(nxt)
                    if (pn.hostname or "").lower() != allowed_host:
                        continue
                    if not (pn.path or "/").startswith(path_prefix):
                        continue
                    queue.append((nxt, depth + 1))

    return pages


async def run_k8s_official_ingest(*, dry_run: bool = False, max_pages_override: int | None = None) -> int:
    ws = WorkerSettings()
    if max_pages_override is not None:
        mp = max(1, min(5000, int(max_pages_override)))
        ws = ws.model_copy(update={"k8s_official_crawl_max_pages": mp})
    collection = ws.pgvector_collection_k8s_expert.strip()
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9_]{0,62}$", collection):
        logger.error("invalid pgvector_collection_k8s_expert: %r", collection)
        return 2

    pages = await crawl_k8s_docs(ws)
    if not pages:
        logger.warning("no pages collected")
        return 1

    chunk_size = int(ws.k8s_official_chunk_chars)
    overlap = int(ws.k8s_official_chunk_overlap)
    all_chunks: list[tuple[str, str, str]] = []
    for url, text in pages:
        path = urlparse(url).path or ""
        dtype = _infer_doc_type(path)
        for ch in _chunk_text(text, size=chunk_size, overlap=overlap):
            if ch.strip():
                all_chunks.append((url, dtype, ch))

    logger.info("total chunks=%s (collection=%s)", len(all_chunks), collection)
    if dry_run:
        return 0

    redis_url = os.environ.get("OMNI_REDIS_URL", "redis://redis:6379/0")
    r = aioredis.from_url(redis_url, decode_responses=False)
    store = RedisVectorStore(r)
    await store.ensure_ready()
    llm = build_llm_client(base_url=ws.vllm_base_url, embed_url=ws.vllm_embed_url, timeout_s=120.0)
    try:
        batch = 16
        n = 0
        for i in range(0, len(all_chunks), batch):
            slice_ = all_chunks[i : i + batch]
            texts = [c for _, _, c in slice_]
            resp = await llm.embed(
                model=ws.embed_model,
                input=texts,
            )
            vecs = _vecs_from_embed_response(resp)
            if len(vecs) != len(texts):
                raise RuntimeError(f"embed batch mismatch want {len(texts)} got {len(vecs)}")
            points: list[PointStruct] = []
            for (url, dtype, ch), vec in zip(slice_, vecs, strict=True):
                path = urlparse(url).path or ""
                pid = str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"k8s_official:{collection}:{url}:{hash(ch[:400])}",
                    )
                )
                meta = {
                    "source": "official_k8s",
                    "url": url,
                    "type": dtype,
                    "level": _infer_knowledge_level(path),
                    "version": ws.k8s_official_metadata_version,
                }
                payload: dict[str, Any] = {
                    "text": ch[:8000],
                    "summary": ch[:500],
                    "metadata": meta,
                }
                points.append(PointStruct(id=pid, vector=_pad_vec(vec), payload=payload))
            await store.upsert(collection, points)
            n += len(points)
            logger.info("upserted %s / %s", n, len(all_chunks))
    finally:
        await llm.aclose()
        await r.aclose()
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Ingest kubernetes.io → pgvector (collection from OMNI_PGVECTOR_COLLECTION_K8S_EXPERT)")
    ap.add_argument("--dry-run", action="store_true", help="Crawl + chunk only, no DB / embed")
    ap.add_argument(
        "--max-pages",
        type=int,
        default=None,
        metavar="N",
        help="Override OMNI_K8S_OFFICIAL_CRAWL_MAX_PAGES for this run (1–5000)",
    )
    args = ap.parse_args()
    rc = asyncio.run(run_k8s_official_ingest(dry_run=args.dry_run, max_pages_override=args.max_pages))
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
