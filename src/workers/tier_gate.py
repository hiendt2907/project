"""Autonomy tier gate — RE-EXPORT của :mod:`pkg.autonomy.tier_gate` (canonical).

Logic đã chuyển sang ``src/pkg/autonomy/tier_gate.py`` (Phase 2, 0-6 roadmap) để
gateway dùng chung mà KHÔNG vi phạm bất biến "gateway KHÔNG import workers" — chính
pattern đã dùng cho ``pkg.risk_taxonomy`` (xem ``workers/risk_class.py``). Module này
giữ nguyên public API cũ (`from workers.tier_gate import ...`) cho worker/test.
"""

from __future__ import annotations

from pkg.autonomy.tier_gate import (  # noqa: F401
    ALLOW,
    ASSIST,
    AUTO,
    HITL,
    SHADOW,
    SUGGEST,
    VALID_TIERS,
    confidence_ceiling,
    derive_tier_from_legacy,
    effective_tier,
    evaluate_tier_gate,
    gate_decision_for_tool,
    is_trusted_origin,
    normalize_tier,
    resolve_tier,
)

__all__ = [
    "ALLOW",
    "ASSIST",
    "AUTO",
    "HITL",
    "SHADOW",
    "SUGGEST",
    "VALID_TIERS",
    "confidence_ceiling",
    "derive_tier_from_legacy",
    "effective_tier",
    "evaluate_tier_gate",
    "gate_decision_for_tool",
    "is_trusted_origin",
    "normalize_tier",
    "resolve_tier",
]
