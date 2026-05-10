from __future__ import annotations

import logging
import re
import json
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from workers.proactive_models import AnomalyEvent

logger = logging.getLogger(__name__)


class MatrixRow(BaseModel):
    symptom_group: str
    layer: str
    priority: int = 100
    labels_alertname: str | None = None
    labels_domain: str | None = None
    labels_reason_pattern: str | None = None
    labels_workload: str | None = None
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
    """Match a matrix row against an event.

    Label predicates apply only when the event carries that label key (Prometheus JSON).
    If the key is absent, the predicate is waived so ``error_hint_pattern`` / ``canonical_query_pattern``
    can still classify sparse alerts — avoids broad rows winning only because specific rows required labels.
    """
    eh = ev.error_hint or ""
    cq = ev.canonical_query or ""
    labels: dict[str, str] = {}
    if cq.strip().startswith("{"):
        try:
            obj = json.loads(cq)
            raw_labels = obj.get("labels") if isinstance(obj, dict) else None
            if isinstance(raw_labels, dict):
                labels = {str(k): str(v) for k, v in raw_labels.items()}
        except Exception:
            labels = {}

    label_predicates = 0
    label_matches = 0
    if row.labels_alertname:
        if "alertname" in labels:
            label_predicates += 1
            if str(labels.get("alertname") or "").strip().lower() == row.labels_alertname.strip().lower():
                label_matches += 1
    if row.labels_domain:
        if "domain" in labels:
            label_predicates += 1
            if str(labels.get("domain") or "").strip().lower() == row.labels_domain.strip().lower():
                label_matches += 1
    if row.labels_workload:
        if "workload" in labels or "deployment" in labels:
            label_predicates += 1
            wl = str(labels.get("workload") or labels.get("deployment") or "").strip().lower()
            if wl == row.labels_workload.strip().lower():
                label_matches += 1
    if row.labels_reason_pattern:
        if "reason" in labels:
            label_predicates += 1
            try:
                ok_reason = bool(re.search(row.labels_reason_pattern, str(labels.get("reason") or "")))
            except re.error:
                ok_reason = False
            if ok_reason:
                label_matches += 1
    if label_predicates and label_matches == label_predicates:
        return True
    if label_predicates and label_matches < label_predicates:
        return False

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


def alertname_from_anomaly_event(ev: AnomalyEvent) -> str:
    """Return ``labels.alertname`` from JSON ``canonical_query`` when present."""
    cq = ev.canonical_query or ""
    if not cq.strip().startswith("{"):
        return ""
    try:
        obj = json.loads(cq)
        if not isinstance(obj, dict):
            return ""
        labels = obj.get("labels")
        if isinstance(labels, dict):
            an = str(labels.get("alertname") or "").strip()
            return an
    except Exception:
        return ""
    return ""


def classify_event(ev: AnomalyEvent, matrix: DiagnosticMatrixFile) -> MatrixRow | None:
    """Return the first matching matrix row.

    Rows are tried in ascending **priority** (lower number = more specific; try first).
    See ``config/diagnostic_matrix.yaml`` header for ordering rules.
    """
    rows = sorted(matrix.rows, key=lambda r: int(getattr(r, "priority", 100) or 100))
    for row in rows:
        if _row_matches(row, ev):
            return row
    return None
