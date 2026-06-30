"""Idempotency ledger — mỗi mutation chạy ĐÚNG MỘT LẦN dù giao trùng/agent restart.

Vì sao tồn tại (Living Operations Runtime): mission/command có thể được GIAO HAI LẦN
(at-least-once delivery), agent có thể CRASH sau mutation trước khi report. Không có
idempotency → restart/duplicate = mutation lặp = nguy hiểm. Ledger này khóa theo một
KEY tất định (tenant+scope+decision+failure_mode+unit): claim trước khi chạy, record
sau khi xong. Lần giao thứ hai thấy key đã COMPLETED → reconcile, ZERO mutation mới.

Trạng thái key: CLAIMED (đang chạy, có thể crash) → COMPLETED|ESCALATED|ABORTED.
Lưu trên Redis (durable, sống qua agent restart). KHÔNG noun ontology mới — đây là
sổ vận hành (records of Action), như audit.
"""
from __future__ import annotations

import hashlib
import json

STATUS_CLAIMED = "claimed"
STATUS_TERMINAL = frozenset({"recovered", "escalated", "aborted", "completed"})
_TTL_CLAIM_S = 900       # claim sống 15' (đủ cho 1 mutation + verify); crash → tự hết
_TTL_TERMINAL_S = 604800  # record terminal giữ 7 ngày để dedup giao trùng muộn


def idempotency_key(*, tenant: str, scope: str, decision_goal: str,
                    failure_mode: str, unit: str) -> str:
    """Key tất định cho một ý định mutation. Cùng intent → cùng key → chạy 1 lần."""
    raw = f"{tenant}|{scope}|{decision_goal}|{failure_mode}|{unit}"
    return "idem:" + hashlib.sha256(raw.encode()).hexdigest()[:24]


class IdempotencyLedger:
    """Redis-backed. Inject async redis (real qua TCP, hoặc FakeRedis trong test)."""

    def __init__(self, redis) -> None:
        self._r = redis

    async def claim(self, key: str, *, holder: str) -> bool:
        """SET NX: True nếu lần đầu (được phép chạy); False nếu đã có (trùng/đang chạy)."""
        payload = json.dumps({"status": STATUS_CLAIMED, "holder": holder})
        return bool(await self._r.set(key, payload, nx=True, ex=_TTL_CLAIM_S))

    async def get(self, key: str) -> dict | None:
        raw = await self._r.get(key)
        if raw is None:
            return None
        return json.loads(raw)

    async def record(self, key: str, *, status: str, outcome: dict) -> None:
        """Ghi kết quả terminal (overwrite claim), TTL dài để dedup giao trùng muộn."""
        payload = json.dumps({"status": status, "outcome": outcome})
        await self._r.set(key, payload, ex=_TTL_TERMINAL_S)

    async def release_claim(self, key: str) -> None:
        """Xoá claim chưa terminal (vd gate chặn trước mutation) để cho phép thử lại."""
        cur = await self.get(key)
        if cur is not None and cur.get("status") == STATUS_CLAIMED:
            await self._r.delete(key)
