from __future__ import annotations

import time
import uuid
from typing import Any


def build_envelope(
    *,
    probe: str,
    lane: str,
    result: str,
    extracted_fact: dict[str, Any],
    raw: str = "",
    alert_hint: str = "",
    alert_rule: str = "",
    symptom_group: str = "",
    namespace: str = "",
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Build a DiagnosticEvidence envelope ready to POST to /webhook/agent/evidence."""
    return {
        "trace_id": trace_id or f"ra-{uuid.uuid4().hex[:12]}",
        "probe": probe,
        "alert_rule": alert_rule or probe,
        "alert_hint": alert_hint[:2000],
        "result": result,
        "extracted_fact": extracted_fact,
        "raw": raw[:4000],
        "symptom_group": symptom_group or lane.lower(),
        "lane": lane,
        "stream_tags": [lane],
        "namespace": namespace,
        "ts": str(int(time.time())),
    }
