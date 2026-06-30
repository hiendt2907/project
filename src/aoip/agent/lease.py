"""Execution lease — chỉ MỘT writer hợp lệ được mutate một scope tại một thời điểm.

Vì sao tồn tại (Living Operations Runtime): hai agent (hoặc hai vòng lặp) có thể cùng
nhắm một target. Nếu cả hai mutate → blast radius nhân đôi, race. Lease là khóa
single-writer per scope trên Redis (SET NX PX): ai acquire được mới cầm token và được
mutate; người khác acquire THẤT BẠI → ZERO mutation. Lease có TTL: holder crash →
lease tự hết hạn → scope giải phóng (không kẹt vĩnh viễn).

Idempotency dedup theo THỜI GIAN (retry/restart); Lease dedup theo KHÔNG GIAN (đồng
thời nhiều agent). Hai cơ chế bù nhau. KHÔNG noun ontology mới — đây là khóa vận hành.
"""
from __future__ import annotations

import hashlib

_DEFAULT_TTL_S = 120


def _token(scope: str, holder: str) -> str:
    return hashlib.sha256(f"{scope}|{holder}".encode()).hexdigest()[:16]


def lease_key(scope: str) -> str:
    return "lease:" + scope


class ExecutionLease:
    """Redis-backed single-writer lock. Inject async redis (real TCP hoặc FakeRedis)."""

    def __init__(self, redis) -> None:
        self._r = redis

    async def acquire(self, scope: str, *, holder: str, ttl_s: int = _DEFAULT_TTL_S) -> str | None:
        """SET NX PX. Trả token nếu giành được lease; None nếu scope đang bị giữ."""
        token = _token(scope, holder)
        ok = await self._r.set(lease_key(scope), token, nx=True, ex=ttl_s)
        return token if ok else None

    async def holder_token(self, scope: str) -> str | None:
        raw = await self._r.get(scope if scope.startswith("lease:") else lease_key(scope))
        return raw if isinstance(raw, str) or raw is None else raw.decode()

    async def refresh(self, scope: str, *, token: str, ttl_s: int = _DEFAULT_TTL_S) -> bool:
        """Gia hạn lease NẾU mình còn là holder (long mission). Không cướp của ai."""
        cur = await self.holder_token(scope)
        if cur != token:
            return False
        await self._r.set(lease_key(scope), token, ex=ttl_s)
        return True

    async def release(self, scope: str, *, token: str) -> bool:
        """Chỉ holder hợp lệ mới release (compare-and-delete). Tránh xoá lease người khác."""
        cur = await self.holder_token(scope)
        if cur != token:
            return False
        await self._r.delete(lease_key(scope))
        return True
