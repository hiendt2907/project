"""CorrelationChainAdvisory — analyst output for a graph-correlated attack chain.

Consumes the ``omni-siem-chains`` contract emitted by brain-go (see
``smart-siem/omni/siem/contracts/correlation_chain.json``) and produces a
read-only advisory in the same WHAT/WHO/WHY/HOW-TO spirit as AnalystAdvisory,
specialised for multi-event attack narratives.

SECURITY: every field is metadata/derived only — no raw VM log content. The
member references carry incident ids and parsed fields exactly as received.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ChainMemberRef(BaseModel):
    """Metadata-only reference to one incident in the chain."""

    incident_id: str
    category: str = ""
    severity: str = ""
    source_ip: str = ""
    kill_chain_stage: str = ""
    kill_chain_order: int = 0
    timestamp_unix: int = 0
    # Set by semantic-cohesion analysis: a member whose embedding diverges from
    # the chain centroid (likely coincidental, not part of the same campaign).
    weak_member: bool = False


class ChainCommonDimension(BaseModel):
    """A shared entity tying members together."""

    type: Literal["ip", "user", "session_id", "host", "pod", "process"]
    value: str


class ChainSignals(BaseModel):
    entity: float = 0.0
    sequence: float = 0.0
    volume: float = 0.0
    confidence: float = 0.0


class CorrelationChainAdvisory(BaseModel):
    """Read-only advisory describing a correlated attack chain."""

    chain_id: str
    tenant_id: str
    # WHAT: concise attack narrative (one sentence).
    narrative: str = Field(..., description="One-sentence attack-chain summary.")
    # WHO: the shared dimensions that link the events.
    common_dimensions: list[ChainCommonDimension] = Field(default_factory=list)
    # WHY: rationale for correlation, plus per-signal breakdown.
    why_correlated: str = ""
    signals: ChainSignals = Field(default_factory=ChainSignals)
    # Classification.
    attack_category: str = ""
    kill_chain_stage: str = ""
    kill_chain_ordered: bool = False
    confidence: float = 0.0
    # Cohesion: fraction of members semantically consistent with the centroid.
    cohesion: float = 1.0
    # HOW-TO: safe, read-first investigation steps (no auto-mutation).
    recommended_actions: list[str] = Field(default_factory=list)
    member_events: list[ChainMemberRef] = Field(default_factory=list)
    # Provenance of the classification: recalled SOP vs fresh LLM call.
    source: Literal["recall", "llm", "heuristic"] = "heuristic"
    schema_version: str = "1.0.0"
