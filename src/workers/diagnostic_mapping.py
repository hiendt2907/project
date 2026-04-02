from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from workers.proactive_models import AnomalyEvent

logger = logging.getLogger(__name__)


class MatrixRow(BaseModel):
    symptom_group: str
    layer: str
    error_hint_pattern: str | None = None
    canonical_query_pattern: str | None = None
    probe_ids: list[str] = Field(default_factory=list)
    stop_on_first_failure: bool = False


class DiagnosticMatrixFile(BaseModel):
    version: int = 1
    rows: list[MatrixRow] = Field(default_factory=list)


def load_diagnostic_matrix(path: str | Path) -> DiagnosticMatrixFile:
    p = Path(path)
    if not p.is_file():
        logger.warning("diagnostic matrix missing: %s", p)
        return DiagnosticMatrixFile()
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return DiagnosticMatrixFile.model_validate(raw)


def _row_matches(row: MatrixRow, ev: AnomalyEvent) -> bool:
    eh = ev.error_hint or ""
    cq = ev.canonical_query or ""
    if row.error_hint_pattern:
        try:
            ok_eh = bool(re.search(row.error_hint_pattern, eh))
        except re.error:
            ok_eh = False
    else:
        ok_eh = False
    if row.canonical_query_pattern:
        try:
            ok_cq = bool(re.search(row.canonical_query_pattern, cq))
        except re.error:
            ok_cq = False
    else:
        ok_cq = False
    if row.error_hint_pattern and row.canonical_query_pattern:
        return ok_eh or ok_cq
    if row.error_hint_pattern:
        return ok_eh
    if row.canonical_query_pattern:
        return ok_cq
    return False


def classify_event(ev: AnomalyEvent, matrix: DiagnosticMatrixFile) -> MatrixRow | None:
    for row in matrix.rows:
        if _row_matches(row, ev):
            return row
    return None
