"""PlaybookGovernor — graduation state + tenant circuit breaker + blast-radius lock.

Redis là hot-path authoritative trên lab (write-through Postgres omni_admin.playbook_graduation
do Admin UI/gateway đảm nhiệm sau). MỌI đường lỗi Redis = DENY (fail-closed, không fail-open).

Keys:
  omni:playbook:grad:{tenant}:{track}:{domain}:{playbook_id}  HASH {state, success_count, fail_count}
      Hình dạng key có thêm {track} từ 2026-07-30 (xem migration 0013): trước đó `domain`
      chứa LẪN hai khái niệm — domain kỹ thuật (governor ghi) và nguồn học
      (advisory_promoter ghi qua PG). Đổi hình dạng key làm MẤT dữ liệu cũ, nên mọi
      đường đọc/seed đều thử key LEGACY trước và di chuyển một lần (_LEGACY_GRAD_KEY).
  omni:actuator:rollbacks:{tenant}                     ZSET member=trace score=ts (rolling 1h)
  omni:actuator:freeze:{tenant}                        STRING reason (no TTL — admin reset)
  omni:actuator:lock:{tenant}                          STRING trace (SETNX + TTL) — 1 workload/lần
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from pkg.domain.taxonomy import TRACK_PLAYBOOK, normalize_domain
from workers.schemas.playbook import (
    GRAD_CANDIDATE,
    GRAD_FROZEN,
    GRAD_GRADUATED,
    GRADUATION_STATES,
)

logger = logging.getLogger(__name__)

_GRAD_KEY = "omni:playbook:grad:{tenant}:{track}:{domain}:{playbook_id}"
# Hình dạng trước 2026-07-30 — chỉ ĐỌC, để migrate một lần rồi không dùng nữa.
_LEGACY_GRAD_KEY = "omni:playbook:grad:{tenant}:{domain}:{playbook_id}"
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

    async def _grad_key(
        self, tenant: str, domain: str, playbook_id: str, track: str = TRACK_PLAYBOOK
    ) -> str:
        """Key graduation canonical, tự di chuyển dữ liệu từ key hình dạng CŨ một lần.

        Đổi hình dạng key làm MẤT dữ liệu graduation đang sống (lab hiện có
        `omni:playbook:grad:default:k8s:PB-K8S-CPU-RESTART`). Mất state graduation là mất
        cả success_count đã tích luỹ — playbook đã GRADUATED tụt về DRAFT và ngừng được
        auto-execute, tức là một suy giảm năng lực im lặng.

        Vì vậy: key mới chưa tồn tại ⇒ quét key cũ của cùng playbook, chọn key mà domain
        (dạng cũ, ví dụ `k8s`) chuẩn hoá về CÙNG canonical, rồi copy sang key mới.
        CỐ Ý KHÔNG XOÁ key cũ: nếu phải rollback image về bản trước, bản cũ vẫn đọc được
        state của nó. Key cũ thành mồ côi vô hại, dọn tay sau khi cutover ổn định.
        """
        key = _GRAD_KEY.format(
            tenant=tenant, track=track, domain=normalize_domain(domain), playbook_id=playbook_id
        )
        try:
            if await self._r.exists(key):
                return key
        except Exception:  # noqa: BLE001 — lỗi Redis xử lý ở caller (fail-closed)
            return key
        if track != TRACK_PLAYBOOK:
            # Chỉ track playbook từng có key Redis; advisory chỉ sống ở Postgres.
            return key
        try:
            await self._migrate_legacy_grad(tenant, domain, playbook_id, key)
        except Exception as exc:  # noqa: BLE001 — không migrate được thì coi như chưa seed
            logger.warning("event=grad_legacy_migrate_failed key=%s err=%s", key, exc)
        return key

    async def _migrate_legacy_grad(
        self, tenant: str, domain: str, playbook_id: str, new_key: str
    ) -> None:
        canonical = normalize_domain(domain)
        prefix = f"omni:playbook:grad:{tenant}:"
        pattern = _LEGACY_GRAD_KEY.format(tenant=tenant, domain="*", playbook_id=playbook_id)
        async for raw in self._r.scan_iter(match=pattern):
            legacy = raw.decode() if isinstance(raw, bytes) else str(raw)
            middle = legacy[len(prefix):].rsplit(":", 1)[0]
            # Glob `*` khớp cả dấu ':' nên pattern cũng bắt key hình dạng MỚI —
            # loại chúng ra bằng cách đòi đúng một đoạn ở giữa.
            if ":" in middle or normalize_domain(middle) != canonical:
                continue
            fields = await self._r.hgetall(legacy)
            if not fields:
                continue
            decoded = {
                (k.decode() if isinstance(k, bytes) else k): (
                    v.decode() if isinstance(v, bytes) else v
                )
                for k, v in fields.items()
            }
            await self._r.hset(new_key, mapping=decoded)
            logger.info(
                "event=grad_legacy_migrated from=%s to=%s state=%s",
                legacy, new_key, decoded.get("state"),
            )
            return

    async def get_state(
        self, tenant: str, domain: str, playbook_id: str, track: str = TRACK_PLAYBOOK
    ) -> str:
        key = await self._grad_key(tenant, domain, playbook_id, track)
        try:
            v = await self._r.hget(key, "state")
        except Exception as exc:
            logger.warning("event=grad_read_failed key=%s err=%s (DENY)", key, exc)
            return ""  # caller treats unknown as deny
        if isinstance(v, bytes):
            v = v.decode()
        return str(v or "")

    async def ensure_seeded(
        self, tenant: str, domain: str, playbook_id: str, initial: str,
        track: str = TRACK_PLAYBOOK,
    ) -> str:
        """Seed graduation state nếu chưa có; trả về state hiện hành."""
        if initial not in GRADUATION_STATES:
            initial = GRAD_CANDIDATE
        key = await self._grad_key(tenant, domain, playbook_id, track)
        try:
            added = await self._r.hsetnx(key, "state", initial)
            if added:
                await self._r.hsetnx(key, "success_count", 0)
                await self._r.hsetnx(key, "fail_count", 0)
        except Exception as exc:
            logger.warning("event=grad_seed_failed key=%s err=%s", key, exc)
            return ""
        return await self.get_state(tenant, domain, playbook_id, track)

    async def record_outcome(
        self, tenant: str, domain: str, playbook_id: str, *, success: bool,
        promote_min_success: int = 3, track: str = TRACK_PLAYBOOK,
    ) -> tuple[str, str]:
        """Ghi outcome; demote 1 bậc khi fail, promote CANDIDATE→GRADUATED khi đủ
        success liên tiếp. Trả về (from_state, to_state)."""
        key = await self._grad_key(tenant, domain, playbook_id, track)
        cur = await self.get_state(tenant, domain, playbook_id, track)
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

    async def freeze(
        self, tenant: str, domain: str, playbook_id: str, track: str = TRACK_PLAYBOOK
    ) -> None:
        key = await self._grad_key(tenant, domain, playbook_id, track)
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

    async def gate(
        self, tenant: str, domain: str, playbook_id: str, *, initial: str,
        track: str = TRACK_PLAYBOOK,
    ) -> GateDecision:
        """Gate fail-closed: frozen-tenant → deny; graduation phải GRADUATED để auto.
        CANDIDATE → allowed=False reason=candidate_hitl (caller route SUGGEST/HITL)."""
        if await self.is_frozen(tenant):
            return GateDecision(False, "tenant_frozen_circuit_breaker")
        state = await self.ensure_seeded(tenant, domain, playbook_id, initial, track)
        if not state:
            return GateDecision(False, "graduation_state_unreadable")
        if state == GRAD_GRADUATED:
            return GateDecision(True, "graduated", state)
        if state == GRAD_CANDIDATE:
            return GateDecision(False, "candidate_requires_hitl_or_suggest", state)
        return GateDecision(False, f"graduation_state_{state.lower()}", state)
