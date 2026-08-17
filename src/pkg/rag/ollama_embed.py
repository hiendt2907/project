"""Gateway-safe embedding helper (stdlib urllib only — no httpx/openai, no workers import).

Used by the gateway KB route and the KB seed script to embed text into the same
vector space that the RedisVectorStore HNSW index expects. Provider is env-driven
(OMNI_LLM_PROVIDER): "ollama" (default, nomic-embed-text, 768-dim, native /api/embed)
or "nim" (NVIDIA NIM nv-embedqa-e5-v5, 1024-dim, OpenAI-compat /v1/embeddings +
Bearer auth). EMBED_DIM must match OMNI_EMBED_DIM used by redis_vector_store.py —
switching provider requires recreating the HNSW index and re-embedding existing
entries (dimension is fixed at index-creation time).
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from typing import Any

DEFAULT_EMBED_MODEL = "nomic-embed-text"
NIM_DEFAULT_EMBED_MODEL = "nvidia/nv-embedqa-e5-v5"
NIM_BASE_URL = "https://integrate.api.nvidia.com/v1"
EMBED_DIM = int(os.environ.get("OMNI_EMBED_DIM", "768"))


def _provider() -> str:
    return (os.environ.get("OMNI_LLM_PROVIDER") or "ollama").strip().lower()


def _nim_api_key() -> str:
    return (os.environ.get("OMNI_NIM_API_KEY") or "").strip()


def _ollama_base_url() -> str:
    # Default host.orb.internal chỉ đúng cho OrbStack lab; GCP production luôn set
    # OMNI_OLLAMA_BASE_URL (NVIDIA NIM) qua ConfigMap. Nếu key đó từng biến mất khỏi ConfigMap,
    # module này sẽ ÂM THẦM rơi về host lab không tồn tại trên GCP (audit 2026-08-17, chưa có
    # fail-closed — thử thêm model_validator ở settings.py nhưng phá vỡ các test construct
    # WorkerSettings tối giản nên đã rút lại; chỉ còn cảnh báo bằng comment).
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


def _vector_from_openai_response(doc: dict[str, Any]) -> list[float]:
    data = doc.get("data")
    if isinstance(data, list) and data:
        emb = data[0].get("embedding")
        if isinstance(emb, list) and emb:
            return [float(x) for x in emb]
    raise ValueError("nim embed response missing embedding")


def _embed_sync_nim(text: str, *, model: str, timeout: float, input_type: str) -> list[float]:
    key = _nim_api_key()
    if not key:
        raise RuntimeError("OMNI_NIM_API_KEY missing (required when OMNI_LLM_PROVIDER=nim)")
    payload = json.dumps(
        {"input": [text], "model": model, "input_type": input_type, "truncate": "END"}
    ).encode()
    req = urllib.request.Request(
        f"{NIM_BASE_URL}/embeddings",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — pinned NIM host
        return _vector_from_openai_response(json.loads(resp.read().decode()))


async def embed_text(
    text: str,
    *,
    base_url: str | None = None,
    model: str | None = None,
    timeout: float = 30.0,
    input_type: str = "passage",
) -> list[float]:
    """Embed *text*; returns an EMBED_DIM-length float vector. Raises on failure."""
    if not text or not text.strip():
        raise ValueError("cannot embed empty text")
    if _provider() == "nim":
        mdl = model or os.environ.get("OMNI_EMBED_MODEL") or NIM_DEFAULT_EMBED_MODEL
        vec = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _embed_sync_nim(text, model=mdl, timeout=timeout, input_type=input_type)
        )
    else:
        url = (base_url or _ollama_base_url()).rstrip("/")
        mdl = model or DEFAULT_EMBED_MODEL
        vec = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _embed_sync(text, base_url=url, model=mdl, timeout=timeout)
        )
    if len(vec) != EMBED_DIM:
        raise ValueError(f"embed dim mismatch: got {len(vec)}, expected {EMBED_DIM}")
    return vec
