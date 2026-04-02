from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from knowledge.models import SourceEntry


class KnowledgeSourcesFile(BaseModel):
    version: int = 1
    sources: list[SourceEntry] = Field(default_factory=list)


def load_knowledge_sources(path: str | Path) -> KnowledgeSourcesFile:
    p = Path(path)
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return KnowledgeSourcesFile.model_validate(raw)
