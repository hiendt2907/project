"""CorrelationChain build + validate — wire shape cho ``omni-siem-chains``.

Port 1-1 của brain-go ``internal/correlate/chain.go`` (buildChain /
explainChain / isMonotonic) và ``internal/publisher/chain_publisher.go``
(ValidateChain / chainEnvelope). Envelope build thẳng ra dict để hợp đồng
auditable, khớp từng key với ``ChainConsumer`` downstream.
"""

from __future__ import annotations

import uuid
from typing import Any

from services.siem_correlation.models import CHAIN_SCHEMA_VERSION, Entity, IncidentMeta

# Cap chain description to stay within LLM token budget (parity: maxChainDescLen).
_MAX_CHAIN_DESC_LEN = 2000
_TRUNCATED_SUFFIX = "...[TRUNCATED]"


def is_monotonic(orders: list[int]) -> bool:
    """True when stage orders never decrease AND actually advance overall."""
    if len(orders) < 2:
        return False
    for i in range(1, len(orders)):
        if orders[i] < orders[i - 1]:
            return False
    return orders[-1] > orders[0]


def explain_chain(
    dims: list[dict[str, Any]],
    refs: list[dict[str, Any]],
    signals: dict[str, float],
) -> str:
    """One-line, metadata-only rationale for the correlation."""
    dim_parts = [f"{d['type']}={d['value']}" for d in dims]
    stage_parts = [
        r["kill_chain_stage"]
        for r in refs
        if r.get("kill_chain_stage") and r["kill_chain_stage"] != "unknown"
    ]
    msg = f"{len(refs)} events share [{', '.join(dim_parts)}]"
    if stage_parts:
        msg += "; kill-chain: " + " → ".join(stage_parts)
    msg += (
        f"; confidence={signals['confidence']:.2f}"
        f" (entity={signals['entity']:.2f}"
        f" seq={signals['sequence']:.2f}"
        f" vol={signals['volume']:.2f})"
    )
    if len(msg) > _MAX_CHAIN_DESC_LEN:
        msg = msg[: _MAX_CHAIN_DESC_LEN - len(_TRUNCATED_SUFFIX) - 1] + _TRUNCATED_SUFFIX
    return msg


def build_chain(
    tenant: str,
    members: list[IncidentMeta],
    entities: list[Entity],
    signals: dict[str, float],
    *,
    window_seconds: int,
    now: int,
    chain_id: str | None = None,
) -> dict[str, Any]:
    """Assemble the CorrelationChain envelope from component members/entities.

    Parity notes (Go buildChain):
    - members ordered ascending by ts; top stage uses ``>=`` so later members
      win ties — a chain of all-unknown stages therefore gets
      attack_category="unknown", NOT the "correlated_activity" fallback.
    """
    ordered = sorted(members, key=lambda m: m.ts)

    refs: list[dict[str, Any]] = []
    top_name, top_order = "", 0
    stage_orders: list[int] = []
    for m in ordered:
        refs.append({
            "incident_id": m.id,
            "category": m.category,
            "severity": m.severity,
            "source_ip": m.source_ip,
            "kill_chain_stage": m.stage.name,
            "kill_chain_order": m.stage.order,
            "timestamp_unix": m.ts,
        })
        if m.stage.order >= top_order:
            top_order = m.stage.order
            top_name = m.stage.name
        if m.stage.order > 0:
            stage_orders.append(m.stage.order)

    dims = [{"type": e.type, "value": e.value} for e in entities]
    attack_category = top_name or "correlated_activity"

    return {
        "chain_id": chain_id or str(uuid.uuid4()),
        "tenant_id": tenant,
        "attack_category": attack_category,
        "kill_chain_stage": top_name,
        "kill_chain_ordered": is_monotonic(stage_orders),
        "confidence": signals["confidence"],
        "signals": dict(signals),
        "common_dimensions": dims,
        "member_events": refs,
        "why_correlated": explain_chain(dims, refs, signals),
        "window_seconds": window_seconds,
        "timestamp_unix": now,
        "schema_version": CHAIN_SCHEMA_VERSION,
    }


def validate_chain(chain: dict[str, Any]) -> None:
    """Fail-closed contract check before the chain leaves the engine
    (parity: publisher.ValidateChain). Raises ValueError on violation."""
    if not str(chain.get("chain_id") or "").strip():
        raise ValueError("chain_id required")
    if not str(chain.get("tenant_id") or "").strip():
        raise ValueError("tenant_id required")
    if not str(chain.get("schema_version") or "").strip():
        raise ValueError("schema_version required")
    members = chain.get("member_events") or []
    if len(members) < 2:
        raise ValueError(f"chain must reference >=2 member events, got {len(members)}")
    if len(chain.get("common_dimensions") or []) < 1:
        raise ValueError("chain must have >=1 common dimension")
    confidence = float(chain.get("confidence") or 0.0)
    if confidence < 0 or confidence > 1:
        raise ValueError(f"confidence out of range: {confidence}")
