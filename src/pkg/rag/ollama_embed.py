"""Gateway-safe Ollama embedding helper (stdlib urllib only — no httpx/openai, no workers import).

Used by the gateway KB route and the KB seed script to embed text into the same
768-dim space (nomic-embed-text) that the RedisVectorStore HNSW index expects.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from typing import Any

DEFAULT_EMBED_MODEL = "nomic-embed-text"
EMBED_DIM = 768


def _ollama_base_url() -> str:
    return (
        os.environ.get("OMNI_OLLAMA_BASE_URL")
        or os.environ.get("OLLAMA_BASE_URL")
        or "http://host.orb.internal:11434"
    ).rstrip("/")


def _vector_from_response(doc: dict[str, Any]) -> list[float]:
    """Accept both /api/embed ({embeddings:[[...]]}) and /api/embeddings ({embedding:[...]})."""
    embs = doc.get("embeddings")
    if isinstance(embs, list) and embs and isinstance(embs[0], list):
        return [float(x) for x in embs[0]]
    emb = doc.get("embedding")
    if isinstance(emb, list) and emb:
        return [float(x) for x in emb]
    raise ValueError("ollama embed response missing embedding(s)")


def _embed_sync(text: str, *, base_url: str, model: str, timeout: float) -> list[float]:
    payload = json.dumps({"model": model, "input": text}).encode()
    req = urllib.request.Request(
        f"{base_url}/api/embed",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — internal Ollama
            return _vector_from_response(json.loads(resp.read().decode()))
    except urllib.error.HTTPError as e:
        # Fallback to the legacy single-input endpoint on 404/400.
        body = json.dumps({"model": model, "prompt": text}).encode()
        req2 = urllib.request.Request(
            f"{base_url}/api/embeddings",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req2, timeout=timeout) as resp2:  # noqa: S310
            return _vector_from_response(json.loads(resp2.read().decode()))
        raise RuntimeError(f"ollama embed failed: {e}") from e


async def embed_text(
    text: str,
    *,
    base_url: str | None = None,
    model: str = DEFAULT_EMBED_MODEL,
    timeout: float = 30.0,
) -> list[float]:
    """Embed *text* via Ollama; returns a 768-dim float vector. Raises on failure."""
    if not text or not text.strip():
        raise ValueError("cannot embed empty text")
    url = (base_url or _ollama_base_url()).rstrip("/")
    vec = await asyncio.get_event_loop().run_in_executor(
        None, lambda: _embed_sync(text, base_url=url, model=model, timeout=timeout)
    )
    if len(vec) != EMBED_DIM:
        raise ValueError(f"embed dim mismatch: got {len(vec)}, expected {EMBED_DIM}")
    return vec
