"""Pure types for diagnostic evidence → reasoning (no K8s / executor imports)."""

from __future__ import annotations

import hashlib
import json
from typing import Any, NotRequired, TypedDict

SCHEMA_VERSION = "1.0"
EXTRACTED_FACT_BUDGET = 2000


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
    # Compaction provenance — always present when extracted_fact was compacted
    # so downstream consumers can detect/audit lossy evidence instead of
    # silently getting a truncated (possibly invalid) blob.
    schema_version: str
    content_hash: str
    original_size: int
    truncated: bool


class OmniActionKafkaBody(TypedDict):
    """JSON inside ``KafkaBus.send_dict(topic, {"data": json.dumps(...)})`` for ``omni-actions``."""

    action: str
    trace_id: str
    data: dict[str, Any]


class OmniActionEnvelope(TypedDict):
    """Kafka value envelope for ``omni-actions`` (same pattern as other topics)."""

    data: str
    trace_id: NotRequired[str]


def _compact_value(value: Any, field_budget: int) -> Any:
    """Best-effort shrink of a JSON-serializable value toward ``field_budget``.

    Never mutates ``value``. Strings are truncated with a marker; lists cap
    both element count and per-element size; dicts recurse over their
    values. Used only once the full serialization already exceeds the
    overall budget, so this never runs on the common (small) case.
    """
    if isinstance(value, str):
        return value if len(value) <= field_budget else value[:field_budget] + "…"
    if isinstance(value, list):
        max_items = max(1, field_budget // 40)
        return [_compact_value(item, field_budget) for item in value[:max_items]]
    if isinstance(value, dict):
        return {k: _compact_value(v, field_budget) for k, v in value.items()}
    return value


def _compact_extracted_fact(ef: dict[str, Any] | list[Any], budget: int) -> tuple[str, bool, int]:
    """Serialize ``ef`` to JSON, guaranteed to parse and fit within ``budget``.

    Returns ``(serialized, truncated, original_size)``. Unlike naive string
    slicing of an already-serialized blob (which can cut mid-token and
    produce invalid JSON — see post-mortem tr-leg-no-upsert), this shrinks
    nested string/list values first so the output always round-trips
    through ``json.loads``.
    """
    original = json.dumps(ef, ensure_ascii=False)
    if len(original) <= budget:
        return original, False, len(original)

    for field_budget in (500, 200, 80, 20):
        compacted = _compact_value(ef, field_budget)
        serialized = json.dumps(compacted, ensure_ascii=False)
        if len(serialized) <= budget:
            return serialized, True, len(original)

    # Pathological case (extremely wide dict even at minimum field budget):
    # drop nested collections rather than emit a payload over budget.
    if isinstance(ef, dict):
        minimal = {k: v for k, v in ef.items() if not isinstance(v, (dict, list))}
        serialized = json.dumps(minimal, ensure_ascii=False)
        if len(serialized) <= budget:
            return serialized, True, len(original)
    return "{}", True, len(original)


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
            serialized, truncated, original_size = _compact_extracted_fact(ef, EXTRACTED_FACT_BUDGET)
            out["extracted_fact"] = serialized  # type: ignore[assignment]
            out["schema_version"] = SCHEMA_VERSION
            out["truncated"] = truncated
            if truncated:
                out["original_size"] = original_size
                out["content_hash"] = hashlib.sha256(
                    json.dumps(ef, ensure_ascii=False, sort_keys=True).encode("utf-8")
                ).hexdigest()
        else:
            out["extracted_fact"] = str(ef)  # type: ignore[assignment]
    tid = str(out.get("trace_id") or obj.get("trace_id") or "").strip()
    if not tid:
        tid = "evidence-unknown"
    out["trace_id"] = tid
    return out
