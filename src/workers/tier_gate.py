"""Autonomy tier gate — ma trận tier × risk_class (MASTER_PLAN §3).

3 quyết định: ALLOW (chạy), SUGGEST (advisory-only, hành vi shadow hiện tại),
HITL (cần người duyệt qua Telegram/UI). Bất biến:
- READONLY luôn ALLOW.
- HIGH luôn HITL ở MỌI tier (dangerous_tools không bao giờ tự chạy).
- shadow → mọi mutate SUGGEST (≡ production hiện tại).
- tier lạ → SUGGEST (fail-closed).

Tier runtime resolve: Redis cache → Postgres (omni_admin) → env derive. Default
`shadow`; dẫn xuất tương thích ngược từ `omni_auto_execute_enabled` khi tier rỗng.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from services.admin_config.cache import read_tier_cached, write_through_cache, cache_key_tier
from workers.risk_class import HIGH, LOW, MEDIUM, READONLY, risk_class_of

logger = logging.getLogger(__name__)

ALLOW: Final = "ALLOW"
SUGGEST: Final = "SUGGEST"
HITL: Final = "HITL"

SHADOW: Final = "shadow"
ASSIST: Final = "assist"
AUTO: Final = "auto"
VALID_TIERS: Final = (SHADOW, ASSIST, AUTO)


def derive_tier_from_legacy(auto_execute_enabled: bool) -> str:
    """Dẫn xuất tier khi DB/env tier chưa set: False→shadow, True→auto."""
    return AUTO if auto_execute_enabled else SHADOW


def evaluate_tier_gate(tier: str, risk_class: str) -> str:
    """Ma trận tier×risk → ALLOW|SUGGEST|HITL. Pure, deterministic."""
    if risk_class == READONLY:
        return ALLOW
    if tier == SHADOW:
        return SUGGEST
    if risk_class == HIGH:
        return HITL
    if tier == ASSIST:
        if risk_class == LOW:
            return ALLOW
        return HITL  # MEDIUM → HITL
    if tier == AUTO:
        if risk_class in (LOW, MEDIUM):
            return ALLOW
        return HITL
    return SUGGEST  # tier lạ → fail-closed


def gate_decision_for_tool(
    tool_name: str,
    *,
    tier: str,
    override: str | None = None,
) -> tuple[str, str]:
    """Trả (decision, risk_class) cho 1 tool. Dùng bảng tĩnh + override (đã resolve)."""
    risk = risk_class_of(tool_name, override=override)
    return evaluate_tier_gate(tier, risk), risk


async def resolve_tier(
    *,
    settings: Any,
    repo: Any = None,
    redis: Any = None,
    tenant_id: str = "default",
) -> str:
    """Tier hiệu lực: cache → Postgres → env derive. Luôn trả 1 tier hợp lệ.

    - Redis cache hit → dùng ngay (hot path).
    - miss → repo.get_tier (đã write-through cache trong repo).
    - repo None / chưa có hàng → env: omni_autonomy_tier nếu set, else derive từ
      omni_auto_execute_enabled (tương thích ngược).
    """
    cached = await read_tier_cached(redis, tenant_id)
    if cached in VALID_TIERS:
        return cached
    if repo is not None:
        db_tier = await repo.get_tier(tenant_id)
        if db_tier in VALID_TIERS:
            return db_tier
    # env fallback
    env_tier = (getattr(settings, "omni_autonomy_tier", "") or "").strip().lower()
    if env_tier in VALID_TIERS:
        tier = env_tier
    else:
        tier = derive_tier_from_legacy(bool(getattr(settings, "omni_auto_execute_enabled", False)))
    # nạp cache để hot path lần sau khỏi tính lại
    await write_through_cache(redis, cache_key_tier(tenant_id), tier)
    return tier
