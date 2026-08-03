"""AutonomyPolicy — fine-grained control over system autonomy per incident type.

Policy is stored as an ordered list in Redis. Evaluation: first matching rule wins.
"""

from __future__ import annotations

import json
import logging
import time
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel

from pkg.domain.taxonomy import UNKNOWN, lane_to_domain, normalize_domain

logger = logging.getLogger(__name__)

_POLICY_KEY = "omni:autonomy:policy"
_HISTORY_KEY = "omni:autonomy:policy:history"
_HISTORY_MAX = 100


class AutonomyLevel(str, Enum):
    FULL_AUTO = "FULL_AUTO"      # execute immediately, no human needed
    SUGGEST_ONLY = "SUGGEST_ONLY"  # Telegram advisory, wait for next cycle
    HITL = "HITL"                # block until human approves
    ALERT_ONLY = "ALERT_ONLY"    # notify only, no action


class PolicyRule(BaseModel):
    # Lĩnh vực áp dụng. Nhận CẢ HAI từ vựng: domain canonical (`security`,
    # `database`, ... — nên dùng cho rule mới) và lane trục A cũ (`SIEM_SECURITY`,
    # ...). Xem `_scope_matches`: một rule khai `security` vẫn khớp một sự cố còn
    # mang `SIEM_SECURITY`, và ngược lại. `"*"` = mọi lĩnh vực.
    #
    # Không đổi TÊN field: giá trị của nó nằm trong Redis (`omni:autonomy:policy`) và
    # trong history đã ghi — đổi tên là làm policy đang chạy im lặng không parse được.
    lane: str
    severity: str     # "critical" | "high" | "medium" | "low" | "*"
    action_type: str  # "restart_pod" | "scale_replicas" | "block_ip" | "*"
    level: AutonomyLevel
    reason: Optional[str] = None
    updated_at: Optional[float] = None
    updated_by: Optional[str] = None


def _as_domain(value: str) -> str:
    """Domain canonical của một giá trị scope, dù nó là domain hay lane trục A."""
    d = normalize_domain(value)
    return d if d != UNKNOWN else lane_to_domain(value)


def _scope_matches(rule_scope: str, incoming: str) -> bool:
    """Khớp scope của rule với sự cố, bắc cầu qua hai từ vựng domain ↔ lane trục A.

    Vì sao cần bắc cầu: rule nằm trong Redis do người vận hành đã ghi (lane cũ), còn
    caller dần chuyển sang gửi domain. So chuỗi thô sẽ làm rule `SIEM_SECURITY` lặng
    lẽ KHÔNG khớp sự cố `security` nữa — và rule đó là cái buộc HITL cho security
    critical. Fail hở, không có lỗi nào bật ra.

    Hai giá trị không nhận ra (`unknown`) KHÔNG khớp nhau: nếu không thì mọi nhãn rác
    sẽ khớp mọi nhãn rác khác.
    """
    if rule_scope == "*" or rule_scope == incoming:
        return True
    rd = _as_domain(rule_scope)
    return rd != UNKNOWN and rd == _as_domain(incoming)


def _rule_matches(rule: PolicyRule, lane: str, severity: str, action_type: str) -> bool:
    """Return True when the rule is applicable to the given (lane, severity, action_type) triple.

    ``lane`` nhận domain canonical HOẶC lane trục A — xem `_scope_matches`.
    """
    lane_ok = _scope_matches(rule.lane, lane)
    sev_ok = rule.severity == "*" or rule.severity == severity
    act_ok = rule.action_type == "*" or rule.action_type == action_type
    return lane_ok and sev_ok and act_ok


class AutonomyPolicyStore:
    """Async Redis-backed store for autonomy policy rules and history."""

    DEFAULT_POLICY: list[PolicyRule] = [
        # Safe restarts can be auto-executed
        PolicyRule(
            lane="*",
            severity="*",
            action_type="restart_pod",
            level=AutonomyLevel.FULL_AUTO,
            reason="safe: pod restart is idempotent",
        ),
        # Scale up is safe
        PolicyRule(
            lane="*",
            severity="*",
            action_type="scale_replicas",
            level=AutonomyLevel.FULL_AUTO,
            reason="safe: scaling is reversible",
        ),
        # SIEM critical always needs HITL
        PolicyRule(
            lane="SIEM_SECURITY",
            severity="critical",
            action_type="*",
            level=AutonomyLevel.HITL,
            reason="security critical requires human",
        ),
        # Default: suggest only
        PolicyRule(
            lane="*",
            severity="*",
            action_type="*",
            level=AutonomyLevel.SUGGEST_ONLY,
            reason="default conservative policy",
        ),
    ]

    async def get_policy(self, redis: Any) -> list[PolicyRule]:
        """Load current policy from Redis. Falls back to DEFAULT_POLICY if not set."""
        raw = await redis.get(_POLICY_KEY)
        if not raw:
            return list(self.DEFAULT_POLICY)
        try:
            data = json.loads(raw)
            if not isinstance(data, list):
                return list(self.DEFAULT_POLICY)
            rules: list[PolicyRule] = []
            for item in data:
                try:
                    rules.append(PolicyRule.model_validate(item))
                except Exception as exc:
                    logger.warning("autonomy_policy: skipping invalid rule: %s — %s", item, exc)
            return rules if rules else list(self.DEFAULT_POLICY)
        except Exception as exc:
            logger.error("autonomy_policy: failed to parse policy from Redis: %s", exc)
            return list(self.DEFAULT_POLICY)

    async def set_rule(self, redis: Any, rule: PolicyRule) -> None:
        """Prepend a rule to the policy list and log to history."""
        stamped = rule.model_copy(update={"updated_at": time.time()})
        current = await self.get_policy(redis)
        new_policy = [stamped, *current]
        await redis.set(_POLICY_KEY, json.dumps([r.model_dump() for r in new_policy], default=str))

        change = {
            "timestamp": time.time(),
            "action": "set_rule",
            "rule": stamped.model_dump(),
        }
        await redis.lpush(_HISTORY_KEY, json.dumps(change, default=str))
        await redis.ltrim(_HISTORY_KEY, 0, _HISTORY_MAX - 1)
        logger.info(
            "autonomy_policy: rule set lane=%s severity=%s action_type=%s level=%s",
            stamped.lane,
            stamped.severity,
            stamped.action_type,
            stamped.level,
        )

    async def get_history(self, redis: Any, limit: int = 50) -> list[dict]:
        """Return the most recent policy change history entries."""
        raw_items = await redis.lrange(_HISTORY_KEY, 0, limit - 1)
        result: list[dict] = []
        for raw in raw_items:
            try:
                result.append(json.loads(raw))
            except Exception:
                pass
        return result

    async def reset_to_defaults(self, redis: Any) -> None:
        """Reset policy to defaults and log the reset to history."""
        defaults = [r.model_dump() for r in self.DEFAULT_POLICY]
        await redis.set(_POLICY_KEY, json.dumps(defaults, default=str))

        change = {
            "timestamp": time.time(),
            "action": "reset_to_defaults",
            "rule": None,
        }
        await redis.lpush(_HISTORY_KEY, json.dumps(change, default=str))
        await redis.ltrim(_HISTORY_KEY, 0, _HISTORY_MAX - 1)
        logger.info("autonomy_policy: reset to defaults")


def find_matching_rule(
    rules: list[PolicyRule],
    lane: str,
    severity: str,
    action_type: str,
) -> PolicyRule | None:
    """Return the first matching rule or None."""
    for rule in rules:
        if _rule_matches(rule, lane, severity, action_type):
            return rule
    return None
