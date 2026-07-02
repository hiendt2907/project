"""Pure types for diagnostic evidence → reasoning (no K8s / executor imports)."""

from __future__ import annotations

import json
from typing import Any, NotRequired, TypedDict


class DiagnosticEvidenceDict(TypedDict, total=False):
    """Payload shape produced by Prober onto ``omni-diagnostic-evidence``."""

    kind: str
    trace_id: str
    symptom_group: str
    layer: str
    lane: str          # diagnostic lane: SYS_RESOURCE / SYS_HARD_FAIL / APP_LOG / APP_HTTP / SIEM_SECURITY
    probe: str
    result: str
    extracted_fact: str
    raw: str
    ts: str
    namespace: str
    # Từ AnomalyEvent lúc alert vào prober — để analyst không suy diễn từ probe một mình
    alert_rule: str
    alert_hint: str
    canonical_query_snippet: str
    evidence_source: str
    clinical_priority_note: str
    tenant_id: str
    # Promoted out of a (possibly-truncated) nested extracted_fact — see
    # coerce_evidence_dict. Only reliably populated for onboarding discovery
    # evidence today; other evidence sources may leave these unset.
    agent_id: str
    hostname: str


class OmniActionKafkaBody(TypedDict):
    """JSON inside ``KafkaBus.send_dict(topic, {"data": json.dumps(...)})`` for ``omni-actions``."""

    action: str
    trace_id: str
    data: dict[str, Any]


class OmniActionEnvelope(TypedDict):
    """Kafka value envelope for ``omni-actions`` (same pattern as other topics)."""

    data: str
    trace_id: NotRequired[str]


def coerce_evidence_dict(obj: Any) -> DiagnosticEvidenceDict:
    """Best-effort dict for inbound reasoning (GIGO-safe). Always includes ``trace_id`` when dict-like."""
    if not isinstance(obj, dict):
        return {"kind": "invalid", "trace_id": "evidence-unknown", "raw": str(obj)[:4000]}
    out: DiagnosticEvidenceDict = {}
    for k in (
        "kind",
        "trace_id",
        "symptom_group",
        "layer",
        "lane",        # Bug fix: lane was missing — caused empty lane badge in Telegram
        "namespace",
        "probe",
        "result",
        "raw",
        "ts",
        "alert_rule",
        "alert_hint",
        "canonical_query_snippet",
        "evidence_source",
        "clinical_priority_note",
        "tenant_id",
    ):
        v = obj.get(k)
        if v is not None:
            out[k] = str(v)  # type: ignore[assignment]
    ef = obj.get("extracted_fact")
    if ef is not None:
        # Promote agent_id/hostname to dedicated top-level fields BEFORE
        # truncating extracted_fact below — the gateway (agent_webhook.py)
        # nests them inside extracted_fact via dict-spread, appended AFTER
        # discovery_data, so a large discovery_data payload (e.g. a long
        # process_list) silently truncates them out of the 2000-char cap and
        # every downstream Fact provenance falls back to "agent:unknown".
        if isinstance(ef, dict):
            for identity_key in ("agent_id", "hostname"):
                v = ef.get(identity_key)
                if v is not None and identity_key not in out:
                    out[identity_key] = str(v)  # type: ignore[literal-required]
        if isinstance(ef, (dict, list)):
            out["extracted_fact"] = json.dumps(ef, ensure_ascii=False)[:2000]  # type: ignore[assignment]
        else:
            out["extracted_fact"] = str(ef)  # type: ignore[assignment]
    tid = str(out.get("trace_id") or obj.get("trace_id") or "").strip()
    if not tid:
        tid = "evidence-unknown"
    out["trace_id"] = tid
    return out
