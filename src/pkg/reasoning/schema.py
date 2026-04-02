"""Pure types for diagnostic evidence → reasoning (no K8s / executor imports)."""

from __future__ import annotations

from typing import Any, TypedDict


class DiagnosticEvidenceDict(TypedDict, total=False):
    """Payload shape produced by Prober onto ``omni-diagnostic-evidence``."""

    kind: str
    trace_id: str
    symptom_group: str
    layer: str
    probe: str
    result: str
    extracted_fact: str
    raw: str
    ts: str


def coerce_evidence_dict(obj: Any) -> DiagnosticEvidenceDict:
    """Best-effort dict for inbound reasoning (GIGO-safe)."""
    if not isinstance(obj, dict):
        return {"kind": "invalid", "trace_id": "evidence-unknown", "raw": str(obj)[:4000]}
    out: DiagnosticEvidenceDict = {}
    for k in ("kind", "trace_id", "symptom_group", "layer", "probe", "result", "extracted_fact", "raw", "ts"):
        v = obj.get(k)
        if v is not None:
            out[k] = str(v)  # type: ignore[assignment]
    return out
