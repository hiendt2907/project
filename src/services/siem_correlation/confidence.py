"""Chain confidence — 3 independent signals, weighted aggregate.

Port 1-1 của brain-go ``internal/correlate/confidence.go`` (kể cả round3
half-up để giữ parity số học với Go).
"""

from __future__ import annotations

from typing import Iterable

from services.siem_correlation.models import Entity, IncidentMeta


def round3(f: float) -> float:
    """Parity với Go ``float64(int(f*1000+0.5))/1000`` (half-up, truncation)."""
    return int(f * 1000 + 0.5) / 1000


def entity_score(entities: Iterable[Entity]) -> float:
    """Reward spanning multiple distinct entity TYPES: 1→⅓, 2→⅔, 3+→1.0."""
    n = len({e.type for e in entities})
    if n <= 0:
        return 0.0
    if n >= 3:
        return 1.0
    return n / 3.0


def sequence_score(members: Iterable[IncidentMeta]) -> float:
    """Monotonic kill-chain progression across members ordered by timestamp:
    advances / transitions, ignoring unknown (order 0) stages."""
    ordered = sorted(members, key=lambda m: m.ts)
    stages = [m.stage.order for m in ordered if m.stage.order > 0]
    if len(stages) < 2:
        return 0.0
    transitions = len(stages) - 1
    advances = sum(1 for i in range(1, len(stages)) if stages[i] > stages[i - 1])
    return advances / transitions


def volume_score(count: int, threshold: int) -> float:
    """Saturates at 2× threshold so a single extra event past threshold does
    not dominate the score."""
    if threshold <= 0:
        threshold = 1
    cap = 2 * threshold
    if count >= cap:
        return 1.0
    return count / cap


def score_chain(
    members: list[IncidentMeta],
    entities: list[Entity],
    *,
    threshold: int,
    w_entity: float,
    w_sequence: float,
    w_volume: float,
) -> dict[str, float]:
    """ChainSignals dict {entity, sequence, volume, confidence} — weights are
    normalized internally when they do not sum to 1 (parity: scoreChain)."""
    entity_sig = entity_score(entities)
    seq_sig = sequence_score(members)
    vol_sig = volume_score(len(members), threshold)

    total = w_entity + w_sequence + w_volume
    if total <= 0:
        total = 1.0
    confidence = (entity_sig * w_entity + seq_sig * w_sequence + vol_sig * w_volume) / total

    return {
        "entity": round3(entity_sig),
        "sequence": round3(seq_sig),
        "volume": round3(vol_sig),
        "confidence": round3(confidence),
    }
