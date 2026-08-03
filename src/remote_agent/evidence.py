from __future__ import annotations

import time
import uuid
from typing import Any

from pkg.domain.taxonomy import require_domain


def build_envelope(
    *,
    probe: str,
    lane: str,
    domain: str,
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

    ``domain`` is the NEW source of truth for "what technical field is this
    evidence about" (9 canonical values, ``pkg.domain.taxonomy``). It is
    REQUIRED and validated with ``require_domain``: this is a WRITE path, so an
    unrecognised name raises ``ValueError`` instead of silently degrading to
    ``unknown`` — a garbage domain in the source of truth skews every report
    built on it without any error surfacing.

    ``lane`` (axis A: ``SYS_RESOURCE``/``SYS_HARD_FAIL``/...) is DEPRECATED and
    being phased out — it collapses four real domains into ``SYS_HARD_FAIL``.
    It is still emitted because Kafka topic routing, ``stream_tags`` and the
    gateway envelope schema read it. Do not add new branching on it.

    Lane authority: the remote agent is a *sensor*. The ``lane`` it stamps is a
    NON-AUTHORITATIVE hint (mirrored as ``lane_hint``). Omni re-derives the
    authoritative proof lane on ingest via ``resolve_proof_lane()`` +
    ``os_state_validator``. ``lane``/``stream_tags`` are kept for Kafka topic
    routing and the gateway envelope schema, not for lane decisions.
    """
    canonical_domain = require_domain(domain)
    return {
        "trace_id": trace_id or f"ra-{uuid.uuid4().hex[:12]}",
        "probe": probe,
        "alert_rule": alert_rule or probe,
        "alert_hint": alert_hint[:2000],
        "result": result,
        "extracted_fact": extracted_fact,
        "raw": raw[:4000],
        "symptom_group": symptom_group or lane.lower(),
        "domain": canonical_domain,
        "lane": lane,
        "lane_hint": lane,
        "lane_authoritative": False,
        "stream_tags": [lane],
        "namespace": namespace,
        "ts": str(int(time.time())),
        "evidence_source": evidence_source,
        "signal_type": signal_type,
    }
