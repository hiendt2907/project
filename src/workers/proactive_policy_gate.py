"""Learning governance gate for proactive fallback (Wilson LB on pattern stats)."""

from __future__ import annotations

import math

from workers.handlers import WorkerHandlerContext
from workers.metrics_exporter import set_wilson_confidence_score


def wilson_lower_bound(success: int, total: int, z: float = 1.96) -> float:
    if total <= 0:
        return 0.0
    phat = success / total
    denom = 1.0 + (z * z / total)
    num = phat + (z * z / (2 * total)) - z * math.sqrt((phat * (1 - phat) + (z * z / (4 * total))) / total)
    return max(0.0, num / denom)


async def learning_governance_decision(ctx: WorkerHandlerContext, pattern_key: str) -> tuple[str, float]:
    ws = ctx.settings
    key = f"omni:learning:pattern:{pattern_key}"
    try:
        raw = await ctx.redis.hgetall(key)
    except Exception:
        raw = {}
    total = int(raw.get("total") or 0)
    success = int(raw.get("success") or 0)
    if total < ws.learning_governance_min_samples:
        set_wilson_confidence_score(0.0)
        return "hold", 0.0
    lb = wilson_lower_bound(success, total)
    set_wilson_confidence_score(lb)
    if lb >= ws.learning_governance_exec_lb95_min:
        return "allow", lb
    return "deny", lb
