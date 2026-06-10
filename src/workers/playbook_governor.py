"""PlaybookGovernor — graduation state + tenant circuit breaker + blast-radius lock.

Redis là hot-path authoritative trên lab (write-through Postgres omni_admin.playbook_graduation
do Admin UI/gateway đảm nhiệm sau). MỌI đường lỗi Redis = DENY (fail-closed, không fail-open).

Keys:
  omni:playbook:grad:{tenant}:{domain}:{playbook_id}   HASH {state, success_count, fail_count}
  omni:actuator:rollbacks:{tenant}                     ZSET member=trace score=ts (rolling 1h)
  omni:actuator:freeze:{tenant}                        STRING reason (no TTL — admin reset)
  omni:actuator:lock:{tenant}                          STRING trace (SETNX + TTL) — 1 workload/lần
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from workers.schemas.playbook import (
    GRAD_CANDIDATE,
    GRAD_FROZEN,
    GRAD_GRADUATED,
    GRADUATION_STATES,
)

logger = logging.getLogger(__name__)

_GRAD_KEY = "omni:playbook:grad:{tenant}:{domain}:{playbook_id}"
_ROLLBACK_ZSET = "omni:actuator:rollbacks:{tenant}"
_FREEZE_KEY = "omni:actuator:freeze:{tenant}"
_LOCK_KEY = "omni:actuator:lock:{tenant}"

_BREAKER_WINDOW_SEC = 3600
_BREAKER_MAX_ROLLBACKS = 2  # ≥2 rollback/h → freeze tenant
_DEFAULT_LOCK_TTL_SEC = 300


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    reason: str
    graduation_state: str = ""


class PlaybookGovernor:
    def __init__(self, redis: Any) -> None:
        self._r = redis

    # ---------------- graduation ----------------

    async def get_state(self, tenant: str, domain: str, playbook_id: str) -> str:
        key = _GRAD_KEY.format(tenant=tenant, domain=domain, playbook_id=playbook_id)
        try:
            v = await self._r.hget(key, "state")
        except Exception as exc:
            logger.warning("event=grad_read_failed key=%s err=%s (DENY)", key, exc)
            return ""  # caller treats unknown as deny
        if isinstance(v, bytes):
            v = v.decode()
        return str(v or "")

    async def ensure_seeded(self, tenant: str, domain: str, playbook_id: str, initial: str) -> str:
        """Seed graduation state nếu chưa có; trả về state hiện hành."""
        if initial not in GRADUATION_STATES:
            initial = GRAD_CANDIDATE
        key = _GRAD_KEY.format(tenant=tenant, domain=domain, playbook_id=playbook_id)
        try:
            added = await self._r.hsetnx(key, "state", initial)
            if added:
                await self._r.hsetnx(key, "success_count", 0)
                await self._r.hsetnx(key, "fail_count", 0)
        except Exception as exc:
            logger.warning("event=grad_seed_failed key=%s err=%s", key, exc)
            return ""
        return await self.get_state(tenant, domain, playbook_id)

    async def record_outcome(
        self, tenant: str, domain: str, playbook_id: str, *, success: bool,
        promote_min_success: int = 3,
    ) -> tuple[str, str]:
        """Ghi outcome; demote 1 bậc khi fail, promote CANDIDATE→GRADUATED khi đủ
        success liên tiếp. Trả về (from_state, to_state)."""
        key = _GRAD_KEY.format(tenant=tenant, domain=domain, playbook_id=playbook_id)
        cur = await self.get_state(tenant, domain, playbook_id)
        if not cur:
            return ("", "")
        new = cur
        try:
            if success:
                n = int(await self._r.hincrby(key, "success_count", 1))
                if cur == GRAD_CANDIDATE and n >= promote_min_success:
                    new = GRAD_GRADUATED
            else:
                await self._r.hincrby(key, "fail_count", 1)
                await self._r.hset(key, "success_count", 0)  # success phải LIÊN TIẾP
                if cur == GRAD_GRADUATED:
                    new = GRAD_CANDIDATE
                elif cur == GRAD_CANDIDATE:
                    new = GRAD_FROZEN
            if new != cur:
                await self._r.hset(key, "state", new)
        except Exception as exc:
            logger.warning("event=grad_outcome_failed key=%s err=%s", key, exc)
            return (cur, cur)
        return (cur, new)

    async def freeze(self, tenant: str, domain: str, playbook_id: str) -> None:
        key = _GRAD_KEY.format(tenant=tenant, domain=domain, playbook_id=playbook_id)
        try:
            await self._r.hset(key, "state", GRAD_FROZEN)
        except Exception as exc:
            logger.warning("event=grad_freeze_failed key=%s err=%s", key, exc)

    # ---------------- circuit breaker ----------------

    async def record_rollback(self, tenant: str, trace: str) -> bool:
        """Ghi 1 rollback; trả về True nếu breaker TRIP (≥2/h) — caller ghi CRAT + freeze."""
        zkey = _ROLLBACK_ZSET.format(tenant=tenant)
        now = time.time()
        try:
            await self._r.zadd(zkey, {f"{trace}:{now}": now})
            await self._r.zremrangebyscore(zkey, 0, now - _BREAKER_WINDOW_SEC)
            n = int(await self._r.zcard(zkey))
            await self._r.expire(zkey, _BREAKER_WINDOW_SEC * 2)
        except Exception as exc:
            logger.warning("event=breaker_record_failed tenant=%s err=%s (treat as TRIP)", tenant, exc)
            return True
        if n >= _BREAKER_MAX_ROLLBACKS:
            try:
                await self._r.set(_FREEZE_KEY.format(tenant=tenant),
                                  f"rollbacks={n}/1h trace={trace}")
            except Exception:
                pass
            logger.error("event=circuit_breaker_tripped tenant=%s rollbacks_1h=%d", tenant, n)
            return True
        return False

    async def is_frozen(self, tenant: str) -> bool:
        """Tenant frozen? Redis lỗi = frozen (DENY)."""
        try:
            v = await self._r.get(_FREEZE_KEY.format(tenant=tenant))
        except Exception as exc:
            logger.warning("event=breaker_read_failed tenant=%s err=%s (DENY)", tenant, exc)
            return True
        return bool(v)

    async def admin_reset_breaker(self, tenant: str) -> None:
        try:
            await self._r.delete(_FREEZE_KEY.format(tenant=tenant))
            await self._r.delete(_ROLLBACK_ZSET.format(tenant=tenant))
        except Exception as exc:
            logger.warning("event=breaker_reset_failed tenant=%s err=%s", tenant, exc)

    # ---------------- blast-radius lock ----------------

    async def acquire_blast_lock(self, tenant: str, trace: str, *, ttl_sec: int = _DEFAULT_LOCK_TTL_SEC) -> bool:
        """1 mutation in-flight / tenant. SETNX + TTL chống deadlock. Redis lỗi = DENY."""
        try:
            ok = await self._r.set(_LOCK_KEY.format(tenant=tenant), trace, nx=True, ex=ttl_sec)
        except Exception as exc:
            logger.warning("event=blast_lock_failed tenant=%s err=%s (DENY)", tenant, exc)
            return False
        return bool(ok)

    async def release_blast_lock(self, tenant: str, trace: str) -> None:
        """Chỉ release nếu lock thuộc trace này (không đạp lock của trace khác)."""
        key = _LOCK_KEY.format(tenant=tenant)
        try:
            holder = await self._r.get(key)
            if isinstance(holder, bytes):
                holder = holder.decode()
            if holder == trace:
                await self._r.delete(key)
        except Exception as exc:
            logger.warning("event=blast_unlock_failed tenant=%s err=%s", tenant, exc)

    # ---------------- gate tổng hợp ----------------

    async def gate(self, tenant: str, domain: str, playbook_id: str, *, initial: str) -> GateDecision:
        """Gate fail-closed: frozen-tenant → deny; graduation phải GRADUATED để auto.
        CANDIDATE → allowed=False reason=candidate_hitl (caller route SUGGEST/HITL)."""
        if await self.is_frozen(tenant):
            return GateDecision(False, "tenant_frozen_circuit_breaker")
        state = await self.ensure_seeded(tenant, domain, playbook_id, initial)
        if not state:
            return GateDecision(False, "graduation_state_unreadable")
        if state == GRAD_GRADUATED:
            return GateDecision(True, "graduated", state)
        if state == GRAD_CANDIDATE:
            return GateDecision(False, "candidate_requires_hitl_or_suggest", state)
        return GateDecision(False, f"graduation_state_{state.lower()}", state)
