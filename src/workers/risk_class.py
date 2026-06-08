"""Risk-class taxonomy — RE-EXPORT của :mod:`pkg.risk_taxonomy` (canonical).

Dữ liệu/logic đã chuyển sang ``src/pkg/risk_taxonomy.py`` để gateway/admin_config
dùng chung mà KHÔNG vi phạm bất biến "gateway KHÔNG import workers". Module này giữ
nguyên public API cũ (`from workers.risk_class import ...`) cho worker/test.
"""

from __future__ import annotations

from pkg.risk_taxonomy import (  # noqa: F401
    DANGEROUS_TOOLS,
    HIGH,
    LOW,
    MEDIUM,
    READONLY,
    STATIC_RISK_CLASS,
    VALID_RISK_CLASSES,
    _ORDER,
    _READONLY_TOOLS,
    _STATIC_MUTATE,
    _clamp_dangerous,
    is_dangerous,
    rank,
    risk_class_of,
)

__all__ = [
    "DANGEROUS_TOOLS",
    "HIGH",
    "LOW",
    "MEDIUM",
    "READONLY",
    "STATIC_RISK_CLASS",
    "VALID_RISK_CLASSES",
    "is_dangerous",
    "rank",
    "risk_class_of",
]
