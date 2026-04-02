from __future__ import annotations

from knowledge.models import TextChunk


async def enrich_optional(_ollama, _model: str, chunk: TextChunk) -> list[TextChunk]:
    """Optional LLM enrich — MVP returns chunk unchanged."""
    return [chunk]
