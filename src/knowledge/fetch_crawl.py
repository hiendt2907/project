from __future__ import annotations

import os

import httpx

from knowledge.models import RawDocument


async def fetch_url_jina(url: str, source_id: str, *, timeout: float = 60.0) -> RawDocument:
    """Jina Reader API — returns markdown; still must pass clean+chunk in pipeline."""
    key = os.getenv("JINA_API_KEY", "").strip()
    if not key:
        raise RuntimeError("JINA_API_KEY unset")
    endpoint = f"https://r.jina.ai/{url}"
    headers = {"Authorization": f"Bearer {key}"}
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(endpoint, headers=headers)
        r.raise_for_status()
        text = r.text
    return RawDocument(
        source_id=source_id,
        source_key=url,
        raw_text=text,
        content_type="markdown",
    )
