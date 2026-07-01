"""Execution lease — chỉ MỘT writer hợp lệ được mutate một scope tại một thời điểm.

Vì sao tồn tại (Living Operations Runtime): hai agent (hoặc hai vòng lặp) có thể cùng
nhắm một target. Nếu cả hai mutate → blast radius nhân đôi, race. Lease là khóa
single-writer per scope trên Redis (SET NX PX): ai acquire được mới cầm token và được
mutate; người khác acquire THẤT BẠI → ZERO mutation. Lease có TTL: holder crash →
lease tự hết hạn → scope giải phóng (không kẹt vĩnh viễn).

Idempotency dedup theo THỜI GIAN (retry/restart); Lease dedup theo KHÔNG GIAN (đồng
thời nhiều agent). Hai cơ chế bù nhau. KHÔNG noun ontology mới — đây là khóa vận hành.

``renew``/``release`` là compare-and-expire / compare-and-delete ATOMIC (Lua, một round-
trip) — KHÔNG phải GET rồi SET/DEL rời. Race cũ: giữa GET (đọc holder) và SET/DEL (ghi),
lease có thể hết hạn và bị agent khác acquire; caller cũ (đọc thấy token mình lúc GET)
vẫn ghi/xoá đè lên lease của agent MỚI. Lua script loại race này vì Redis chạy Lua
single-threaded — so sánh + ghi trong CÙNG MỘT operation, không ai chen giữa được.
"""
from __future__ import annotations

import hashlib

_DEFAULT_TTL_S = 120

# KEYS[1]=lease_key ARGV[1]=token ARGV[2]=ttl_s
_RENEW_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  redis.call('SET', KEYS[1], ARGV[1], 'EX', tonumber(ARGV[2]))
  return 1
end
return 0
"""

# KEYS[1]=lease_key ARGV[1]=token
_RELEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  redis.call('DEL', KEYS[1])
  return 1
end
return 0
"""


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

    async def renew(self, scope: str, *, token: str, ttl_s: int = _DEFAULT_TTL_S) -> bool:
        """Gia hạn lease ATOMIC nếu ``token`` còn khớp holder hiện tại (compare-and-expire).

        KHÔNG cho owner mới bị owner cũ renew đè: nếu lease đã hết hạn và agent khác đã
        acquire, GET trong script thấy token KHÁC → trả 0 (ownership_lost), KHÔNG ghi gì.
        Monotonic: TTL luôn được set lại từ ``ttl_s`` (không bao giờ làm deadline lùi vì
        script chỉ SET khi renew thành công, không có nhánh giảm TTL).
        """
        ok = await self._r.eval(_RENEW_SCRIPT, 1, lease_key(scope), token, ttl_s)
        return bool(ok)

    async def refresh(self, scope: str, *, token: str, ttl_s: int = _DEFAULT_TTL_S) -> bool:
        """DEPRECATED alias cho ``renew`` — giữ lại tương thích ngược, KHÔNG dùng mới."""
        return await self.renew(scope, token=token, ttl_s=ttl_s)

    async def release(self, scope: str, *, token: str) -> bool:
        """Chỉ holder hợp lệ mới release, ATOMIC (compare-and-delete)."""
        ok = await self._r.eval(_RELEASE_SCRIPT, 1, lease_key(scope), token)
        return bool(ok)
