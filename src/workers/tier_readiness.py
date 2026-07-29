"""Tier readiness — CHỈ tính & hiển thị, KHÔNG tự nhảy tier (MASTER_PLAN §5).

Đọc data thật từ KPI ZSET per-tenant + ngày kể từ lúc vào tier hiện tại. Trả
struct cho UI + metric ``omni_tier_promotion_ready{from,to}``. Không có code path
nào set tier từ kết quả này — chuyển pha CHỈ qua Admin UI (operator bấm).
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass
from typing import Any

from workers.proactive_policy_gate import wilson_lower_bound

logger = logging.getLogger(__name__)

_DAY_SEC = 86400


@dataclass(frozen=True)
class TierReadiness:
    current_tier: str
    next_tier: str | None
    ready: bool
    elapsed_days: int
    accepted: int
    rejected: int
    false_positive: int
    total: int
    wilson_lb: float
    false_positive_rate: float
    reasons: tuple[str, ...]  # điều kiện chưa đạt (rỗng = ready)
    graduated_playbooks: int = 0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["reasons"] = list(self.reasons)
        return d


async def _zcard(redis: Any, key: str) -> int:
    try:
        return int(await redis.zcard(key))
    except Exception:  # noqa: BLE001
        return 0


async def compute_tier_readiness(
    *,
    redis: Any,
    settings: Any,
    current_tier: str,
    tenant_id: str = "default",
    tier_entered_at: float | None = None,
    graduated_playbooks: int = 0,
) -> TierReadiness:
    """Tính readiness cho lần nâng tier kế tiếp. ``tier_entered_at`` epoch giây.

    ``graduated_playbooks`` = số playbook đã tốt nghiệp (`omni_admin.playbook_graduation`,
    state GRADUATED). Acceptance cao chứng minh Omni chẩn đoán ĐÚNG; nó không chứng minh
    Omni đã rút ra được QUY TRÌNH lặp lại được. Mặc định 0 nên caller cũ bị chặn
    (fail-closed) thay vì vô tình được nâng tier.
    """
    accepted = await _zcard(redis, f"omni:kpi:z:{tenant_id}:accepted")
    rejected = await _zcard(redis, f"omni:kpi:z:{tenant_id}:rejected")
    false_positive = await _zcard(redis, f"omni:kpi:z:{tenant_id}:false_positive")
    total = accepted + rejected + false_positive
    wilson = wilson_lower_bound(accepted, total)
    fp_rate = (false_positive / total) if total > 0 else 0.0
    elapsed_days = 0
    if tier_entered_at:
        elapsed_days = int((time.time() - tier_entered_at) / _DAY_SEC)

    next_tier, min_days, min_wilson = _next_tier_thresholds(settings, current_tier)
    if next_tier is None:
        return TierReadiness(
            current_tier=current_tier, next_tier=None, ready=False,
            elapsed_days=elapsed_days, accepted=accepted, rejected=rejected,
            false_positive=false_positive, total=total, wilson_lb=wilson,
            false_positive_rate=fp_rate, reasons=("đã ở tier cao nhất",),
            graduated_playbooks=graduated_playbooks,
        )

    min_adv = int(getattr(settings, "omni_tier_min_advisories", 50))
    max_fp = float(getattr(settings, "omni_tier_max_false_positive_rate", 0.10))
    reasons: list[str] = []
    if elapsed_days < min_days:
        reasons.append(f"elapsed_days {elapsed_days} < {min_days}")
    if total < min_adv:
        reasons.append(f"total {total} < {min_adv}")
    if wilson < min_wilson:
        reasons.append(f"wilson_lb {wilson:.3f} < {min_wilson}")
    if fp_rate >= max_fp:
        reasons.append(f"false_positive_rate {fp_rate:.3f} >= {max_fp}")
    min_grad = int(getattr(settings, "omni_tier_min_graduated_playbooks", 1))
    if graduated_playbooks < min_grad:
        reasons.append(
            f"graduated_playbooks {graduated_playbooks} < {min_grad} "
            "(chưa có quy trình nào lặp lại được)"
        )

    return TierReadiness(
        current_tier=current_tier, next_tier=next_tier, ready=not reasons,
        elapsed_days=elapsed_days, accepted=accepted, rejected=rejected,
        false_positive=false_positive, total=total, wilson_lb=wilson,
        false_positive_rate=fp_rate, reasons=tuple(reasons),
        graduated_playbooks=graduated_playbooks,
    )


def _next_tier_thresholds(settings: Any, current_tier: str) -> tuple[str | None, int, float]:
    """(next_tier, min_days, min_wilson) cho bước nâng kế tiếp."""
    if current_tier == "shadow":
        return (
            "assist",
            int(getattr(settings, "omni_tier_min_days_shadow", 90)),
            float(getattr(settings, "omni_tier_shadow_assist_wilson", 0.80)),
        )
    if current_tier == "assist":
        return (
            "auto",
            int(getattr(settings, "omni_tier_min_days_assist", 270)),
            float(getattr(settings, "omni_tier_assist_auto_wilson", 0.85)),
        )
    return (None, 0, 0.0)
