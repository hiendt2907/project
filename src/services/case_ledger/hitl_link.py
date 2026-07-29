"""Nối phán quyết HITL vào sổ ca — dùng chung cho CẢ HAI bề mặt duyệt.

Vì sao module này nằm ở ``src/services/`` chứ không ở ``src/workers/``: đường HITL
đi qua hai nơi — callback Telegram (worker) và ``POST /hitl/{id}/decide`` (gateway).
Invariant của dự án cấm ``src/gateway/`` import ``workers/``, nên chỗ duy nhất hợp lệ
để đặt logic chung là services/.

Vì sao phải nối: approve/reject của HITL là tín hiệu học **chất lượng cao nhất** trong
hệ thống — một con người đã cân nhắc rồi phán quyết về một hành động cụ thể sắp chạy.
Trước bản vá này nó chỉ vào CRAT (bằng chứng tuân thủ) rồi bị vứt: không có gì đọc lại
để biết Omni chẩn đoán trúng hay trật.

Toàn bộ hàm ở đây là **best-effort**: sổ ca là bằng chứng năng lực, không phải đường
quyết định. PG chết KHÔNG được phép chặn một quyết định duyệt/từ chối mà con người đã
bấm — CRAT mới là thứ fail-closed.
"""

from __future__ import annotations

import logging
from typing import Any

from services.case_ledger.store import CaseLedgerStore

logger = logging.getLogger(__name__)

# Nguồn phán quyết cố định cho đường này. CHECK constraint ở DB không nhận
# 'self'/'system' — người làm không được là người chấm.
VERDICT_SOURCE_HITL = "hitl"


def case_id_for_hitl(*, pending_id: str, trace_id: str = "") -> str:
    """Khoá ca cho một lượt duyệt HITL.

    Ưu tiên ``trace_id`` vì một trace là một lần Omni phát biểu — nếu sau này đường
    advisory tự mở ca trước (đúng thiết kế: mở lúc phát biểu), verdict HITL sẽ rơi
    đúng vào ca đó thay vì tạo ca mồ côi. Không có trace thì lùi về ``pending_id``,
    vẫn ổn định và idempotent.
    """
    trace = (trace_id or "").strip()
    if trace:
        return f"case:{trace}"
    return f"case:hitl:{pending_id}"


def pattern_key_for_hitl(*, tool_name: str = "", alertname: str = "", lane: str = "") -> str:
    """``pattern_key`` đóng băng của ca — nhóm việc mà Omni sẽ xin quyền theo.

    Chọn theo loại việc chứ không theo từng sự cố: bằng chứng "tôi làm tốt việc
    restart deployment" không chứng minh được việc chưa gặp bao giờ.
    """
    parts = [p for p in (lane.strip(), alertname.strip(), tool_name.strip()) if p]
    return ":".join(parts) if parts else "hitl:unknown"


async def record_hitl_verdict(
    *,
    pool: Any,
    tenant_id: str,
    pending_id: str,
    decision: str,
    actor: str,
    trace_id: str = "",
    tool_name: str = "",
    alertname: str = "",
    lane: str = "",
    crat_ref: str | None = None,
) -> str | None:
    """Ghi phán quyết HITL vào sổ ca. Trả ``case_id`` nếu ghi được, ngược lại None.

    APPROVED → ``diagnosis=CORRECT``: người duyệt đã đọc chẩn đoán và đồng ý hành
    động theo nó. ``remedy`` CỐ Ý để nguyên ``UNJUDGED`` — lúc bấm duyệt hành động
    còn chưa chạy, nên chưa ai biết nó có khắc phục được không; nhãn đó phải đến từ
    thế giới (``mark_recurred`` / đo lại). Ghi CORRECT ở đây là tự chấm điểm cho
    chính mình, đúng thứ thiết kế sổ ca cấm.

    REJECTED → ``diagnosis=INCORRECT``: con người bác bỏ đề xuất đang chờ.

    Nuốt mọi exception có chủ đích — xem docstring module.
    """
    if pool is None:
        return None
    normalized = (decision or "").strip().upper()
    if normalized not in ("APPROVED", "REJECTED"):
        return None

    case_id = case_id_for_hitl(pending_id=pending_id, trace_id=trace_id)
    try:
        store = CaseLedgerStore(pool)
        # Ca có thể chưa tồn tại nếu advisory không đi qua đường mở ca (ví dụ mutate
        # phát sinh trực tiếp). Mở với posture DIAGNOSED: Omni ĐÃ phát biểu và đề
        # xuất hành động — nó không hề từ chối ca này.
        if await store.get_case(case_id) is None:
            await store.open_case(
                case_id=case_id,
                tenant_id=tenant_id or "default",
                pattern_key=pattern_key_for_hitl(
                    tool_name=tool_name, alertname=alertname, lane=lane
                ),
                posture="DIAGNOSED",
                lane=lane,
                alertname=alertname,
                crat_ref=crat_ref,
            )
        await store.record_verdict(
            case_id=case_id,
            source=VERDICT_SOURCE_HITL,
            actor=actor or "",
            diagnosis="CORRECT" if normalized == "APPROVED" else "INCORRECT",
            crat_ref=crat_ref,
        )
        return case_id
    except Exception as exc:  # noqa: BLE001 — best-effort, không chặn quyết định HITL
        logger.warning(
            "case_ledger: ghi verdict HITL that bai pending=%s case=%s err=%s",
            pending_id, case_id, exc,
        )
        return None
