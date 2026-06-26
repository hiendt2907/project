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
    evidence_source: str = "RemoteAgent",
    signal_type: str = "ANOMALY",
) -> dict[str, Any]:
    """Build a DiagnosticEvidence envelope ready to POST to /webhook/agent/evidence.

    Lane authority: the remote agent is a *sensor*. The ``lane`` it stamps is a
    NON-AUTHORITATIVE hint (mirrored as ``lane_hint``). Omni re-derives the
    authoritative proof lane on ingest via ``resolve_proof_lane()`` +
    ``os_state_validator``. ``lane``/``stream_tags`` are kept for Kafka topic
    routing and the gateway envelope schema, not for lane decisions.
    """
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
        "lane_hint": lane,
        "lane_authoritative": False,
        "stream_tags": [lane],
        "namespace": namespace,
        "ts": str(int(time.time())),
        "evidence_source": evidence_source,
        "signal_type": signal_type,
    }
