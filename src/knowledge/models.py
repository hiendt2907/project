from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


Backend = Literal["local", "firecrawl", "jina"]


class SourceEntry(BaseModel):
    id: str
    backend: Backend = "local"
    url: str | None = None
    local_dir: str | None = None
    layer: str = Field(default="misc", min_length=1, max_length=64)
    version: str = "0"


class RawDocument(BaseModel):
    source_id: str
    source_key: str
    raw_text: str
    content_type: Literal["markdown", "html", "plain"] = "markdown"


class TextChunk(BaseModel):
    heading_path: str
    body: str
    chunk_index: int
