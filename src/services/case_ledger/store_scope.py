"""Truy cập ``omni_admin.scope_request`` / ``omni_admin.scope_grant``.

Tách khỏi ``CaseLedgerStore`` có chủ đích: sổ ca là **bằng chứng** (append-mostly,
bất biến cưỡng chế bằng trigger), còn hai bảng ở đây là **quyền hạn** (đổi liên
tục, do người quyết). Trộn chung sẽ khiến một lần refactor "cho gọn" dễ mang thói
quen ghi đè của bảng quyền sang bảng bằng chứng — đúng thứ mà trigger DB đang
phải chặn.

Bất đối xứng có chủ đích, hiện diện ngay ở API của module này: có
``upsert_grant`` để NGƯỜI cấp quyền và ``freeze_grant`` để NGƯỜI đóng băng, nhưng
KHÔNG có hàm nào gỡ ``frozen``. Omni tự lên bậc được, không tự gỡ án được. Muốn
gỡ thì phải là một thao tác của người, viết ở nơi khác, có danh tính.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

STATE_PENDING = "PENDING"
STATE_APPROVED = "APPROVED"
STATE_REJECTED = "REJECTED"
STATE_WITHDRAWN = "WITHDRAWN"

VALID_DECISIONS = (STATE_APPROVED, STATE_REJECTED)

# Khoá xin lại sau khi bị từ chối. Con số này là một tham số an toàn, không phải
# tham số hiệu năng: nếu xin miễn phí thì chiến lược tối ưu của một hệ thống tự
# động là xin liên tục tới lúc admin mệt mà bấm duyệt. Đó là lỗ hổng con người,
# và nó có thật.
DEFAULT_REJECT_COOLDOWN_DAYS = 14


def _loads(value: Any) -> dict[str, Any]:
    """asyncpg trả JSONB dưới dạng str khi chưa đăng ký codec — chuẩn hoá về dict."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:  # noqa: BLE001 — evidence hỏng không được làm chết báo cáo
            return {}
    return dict(value) if isinstance(value, dict) else {}


class ScopeStore:
    """Đọc/ghi đơn xin quyền và khuôn khổ quyền đã cấp. ``pool`` là asyncpg pool."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    # ── scope_grant ──────────────────────────────────────────────────────────

    async def get_grant(self, *, tenant_id: str, pattern_key: str) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM omni_admin.scope_grant "
                "WHERE tenant_id=$1 AND pattern_key=$2",
                tenant_id,
                pattern_key,
            )
            return dict(row) if row else None

    async def list_grants(self, *, tenant_id: str) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM omni_admin.scope_grant WHERE tenant_id=$1 "
                "ORDER BY pattern_key",
                tenant_id,
            )
            return [dict(r) for r in rows]

    async def upsert_grant(
        self,
        *,
        tenant_id: str,
        pattern_key: str,
        granted_scope: str,
        granted_by: str,
    ) -> dict[str, Any]:
        """Cấp/nâng quyền cho MỘT pattern. Chỉ gọi từ đường quyết định của người.

        ``WHERE NOT frozen`` nằm ngay trong câu lệnh chứ không kiểm tra ở tầng
        Python phía trên: một pattern đang bị đóng băng thì kể cả đường ghi khác
        bỏ qua tầng kiểm tra cũng không nâng được quyền cho nó.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO omni_admin.scope_grant "
                "(tenant_id, pattern_key, granted_scope, granted_by, granted_at) "
                "VALUES ($1,$2,$3,$4, now()) "
                "ON CONFLICT (tenant_id, pattern_key) DO UPDATE SET "
                "  granted_scope = EXCLUDED.granted_scope, "
                "  granted_by    = EXCLUDED.granted_by, "
                "  granted_at    = now() "
                "WHERE NOT omni_admin.scope_grant.frozen "
                "RETURNING *",
                tenant_id,
                pattern_key,
                granted_scope,
                granted_by,
            )
            return dict(row) if row else {}

    async def freeze_grant(
        self, *, tenant_id: str, pattern_key: str, reason: str
    ) -> dict[str, Any]:
        """Đóng băng một pattern. KHÔNG có hàm ngược lại trong module này — cố ý."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO omni_admin.scope_grant "
                "(tenant_id, pattern_key, frozen, frozen_reason) "
                "VALUES ($1,$2, TRUE, $3) "
                "ON CONFLICT (tenant_id, pattern_key) DO UPDATE SET "
                "  frozen = TRUE, frozen_reason = EXCLUDED.frozen_reason "
                "RETURNING *",
                tenant_id,
                pattern_key,
                reason,
            )
            return dict(row) if row else {}

    # ── scope_request ────────────────────────────────────────────────────────

    async def open_request(
        self, *, tenant_id: str, pattern_key: str
    ) -> dict[str, Any] | None:
        """Đơn PENDING đang treo cho pattern này, nếu có. Chặn xin trùng."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM omni_admin.scope_request "
                "WHERE tenant_id=$1 AND pattern_key=$2 AND state='PENDING' "
                "ORDER BY created_at DESC LIMIT 1",
                tenant_id,
                pattern_key,
            )
            return dict(row) if row else None

    async def active_cooldown(
        self, *, tenant_id: str, pattern_key: str
    ) -> dict[str, Any] | None:
        """Đơn bị từ chối còn trong thời gian khoá, nếu có.

        So sánh với ``now()`` của DB chứ không phải đồng hồ tiến trình: nhiều pod
        worker/gateway cùng chạy, để mỗi tiến trình tự chấm thời gian là mở đường
        cho một pod lệch giờ xin lại sớm.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM omni_admin.scope_request "
                "WHERE tenant_id=$1 AND pattern_key=$2 "
                "  AND cooldown_until IS NOT NULL AND cooldown_until > now() "
                "ORDER BY cooldown_until DESC LIMIT 1",
                tenant_id,
                pattern_key,
            )
            return dict(row) if row else None

    async def create_request(
        self,
        *,
        tenant_id: str,
        pattern_key: str,
        requested_scope: str,
        evidence: dict[str, Any],
        crat_ref: str | None = None,
    ) -> dict[str, Any]:
        """Tạo đơn xin quyền với ``evidence`` ĐÓNG BĂNG tại thời điểm xin.

        Không lưu tham chiếu "tính lại lúc đọc": khách phải đối chiếu được đơn này
        với sổ ca ở đúng thời điểm nó được nộp. Nếu báo cáo tự tính lại khi mở ra
        xem thì mọi con số đều trôi theo dữ liệu mới và không ai kiểm chứng được
        rằng đơn từng đủ điều kiện.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO omni_admin.scope_request "
                "(tenant_id, pattern_key, requested_scope, evidence, state, crat_ref) "
                "VALUES ($1,$2,$3,$4::jsonb,'PENDING',$5) RETURNING *",
                tenant_id,
                pattern_key,
                requested_scope,
                json.dumps(evidence, ensure_ascii=False, default=str),
                crat_ref,
            )
            out = dict(row) if row else {}
            if "evidence" in out:
                out["evidence"] = _loads(out["evidence"])
            return out

    async def list_requests(
        self,
        *,
        tenant_id: str,
        state: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM omni_admin.scope_request "
                "WHERE tenant_id=$1 AND ($2::text IS NULL OR state=$2) "
                "ORDER BY created_at DESC LIMIT $3",
                tenant_id,
                state,
                limit,
            )
            out: list[dict[str, Any]] = []
            for r in rows:
                d = dict(r)
                d["evidence"] = _loads(d.get("evidence"))
                out.append(d)
            return out

    async def decide_request(
        self,
        *,
        request_id: int,
        tenant_id: str,
        decision: str,
        actor: str,
        note: str = "",
        cooldown_days: int = DEFAULT_REJECT_COOLDOWN_DAYS,
    ) -> dict[str, Any] | None:
        """Người phán quyết một đơn. ``tenant_id`` nằm trong WHERE — không phải lọc
        ở tầng Python, để một id đoán được cũng không chạm sang tenant khác.

        REJECTED thì đặt ``cooldown_until`` ngay trong cùng câu lệnh: khoá xin lại
        phải là hệ quả không thể tách rời của việc từ chối, không phải một lời gọi
        thứ hai mà ai đó có thể quên (hoặc cố tình bỏ).
        """
        if decision not in VALID_DECISIONS:
            raise ValueError(f"decision khong hop le: {decision!r}")
        cooldown_days = max(0, int(cooldown_days))
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE omni_admin.scope_request SET "
                "  state = $3, decided_by = $4, decided_at = now(), "
                "  decision_note = $5, "
                "  cooldown_until = CASE WHEN $3 = 'REJECTED' "
                "    THEN now() + ($6::int * INTERVAL '1 day') ELSE cooldown_until END "
                "WHERE id = $1 AND tenant_id = $2 AND state = 'PENDING' "
                "RETURNING *",
                request_id,
                tenant_id,
                decision,
                actor,
                note,
                cooldown_days,
            )
            if row is None:
                return None
            out = dict(row)
            out["evidence"] = _loads(out.get("evidence"))
            return out
