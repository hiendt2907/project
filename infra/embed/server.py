"""OpenAI-compatible /v1/embeddings for Omni VLLMClient._embed_client (CPU, ARM64-safe)."""
from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sentence_transformers import SentenceTransformer

MODEL_ID = os.environ.get("MODEL_ID", "nomic-ai/nomic-embed-text-v1.5")

app = FastAPI(title="omni-embed-cpu")
_encoder: SentenceTransformer | None = None


@app.on_event("startup")
def _load() -> None:
    global _encoder
    _encoder = SentenceTransformer(MODEL_ID, device="cpu")


class EmbeddingsRequest(BaseModel):
    model: str = ""
    input: str | list[str]


@app.get("/health")
def health() -> dict[str, str]:
    if _encoder is None:
        raise HTTPException(status_code=503, detail="model not loaded")
    return {"status": "ok"}


@app.post("/v1/embeddings")
def embeddings(body: EmbeddingsRequest) -> dict:
    if _encoder is None:
        raise HTTPException(status_code=503, detail="model not loaded")
    texts = body.input if isinstance(body.input, list) else [body.input]
    if not texts:
        raise HTTPException(status_code=400, detail="empty input")
    vecs = _encoder.encode(texts, convert_to_numpy=True)
    data = []
    for i, row in enumerate(vecs):
        data.append(
            {
                "object": "embedding",
                "embedding": row.tolist(),
                "index": i,
            }
        )
    return {
        "object": "list",
        "data": data,
        "model": body.model or MODEL_ID,
    }
