"""Vòng học chạy được trong chế độ shadow/advisory (G1).

`promoter.evaluate_for_promotion` chỉ kích hoạt sau VERIFIED_SUCCESS của một mutation
thật. Với `OMNI_AUTO_EXECUTE_ENABLED=false` (mặc định, fail-closed) Omni không bao giờ
mutate → không bao giờ học. Đo runtime 2026-07-29 xác nhận: `omni:learn:promo:*` rỗng,
`omni_admin.playbook_graduation` 0 hàng, dù code đã wired ở
`autonomous_feedback_loop.py:388`.

Module này bổ sung đường học thứ hai, dùng tín hiệu DUY NHẤT có thật trong shadow mode:
phán quyết ack/reject của operator trên advisory (`advisory_ack.py`, vòng 2).

INVARIANT AN TOÀN — không được nới:
- Playbook tốt nghiệp theo đường này LUÔN ``auto_execute=False``. Người đồng ý với một
  CHẨN ĐOÁN không phải là uỷ quyền cho máy TỰ THỰC THI. Đường auto-execute vẫn chỉ đến
  từ `promoter.evaluate_for_promotion` (cần VERIFIED_SUCCESS thật).
- Bộ đếm tách theo `tenant_id` (INV_NAMESPACE_ISOLATION) — kinh nghiệm của khách hàng A
  không bao giờ đẩy playbook của khách hàng B lên bậc.
- Tốt nghiệp có thể **mất bậc**: fail-rate vượt ngưỡng → FROZEN, kể cả khi đã GRADUATED.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

logger = logging.getLogger(__name__)

DRAFT = "DRAFT"
CANDIDATE = "CANDIDATE"
GRADUATED = "GRADUATED"
FROZEN = "FROZEN"

DEFAULT_MIN_SUCCESS = 3
DEFAULT_MAX_FAIL_RATE = 0.25
DOMAIN_ADVISORY = "advisory"


def advisory_pattern_key(advisory: dict[str, Any]) -> str:
    """Khoá gom nhóm advisory cùng một lớp triệu chứng.

    Dùng ``lane`` + ``alertname`` — hai trường ổn định nhất; cố ý KHÔNG dùng namespace/
    pod name vì chúng đổi theo từng lần xảy ra, sẽ làm mỗi sự cố thành một pattern riêng
    và không bao giờ đủ số lần để tốt nghiệp.
    """
    if not isinstance(advisory, dict):
        return ""
    lane = str(advisory.get("lane") or "").strip()
    alertname = str(advisory.get("alertname") or "").strip()
    if not lane and not alertname:
        return ""
    raw = f"{lane}|{alertname}".lower()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def next_graduation_state(
    *, success: int, fail: int, min_success: int, max_fail_rate: float
) -> str:
    """Trạng thái kế tiếp từ bộ đếm. Thuần tuý, không side-effect — dễ kiểm thử."""
    total = success + fail
    fail_rate = (fail / total) if total else 0.0

    if total and fail_rate > max_fail_rate:
        return FROZEN
    if success >= min_success:
        return GRADUATED
    if success > 0:
        return CANDIDATE
    return DRAFT


async def record_advisory_verdict(
    ctx: Any,
    *,
    tenant_id: str,
    trace_id: str,
    accepted: bool,
    advisory: dict[str, Any],
    pattern_key: str | None = None,
) -> str | None:
    """Ghi nhận 1 phán quyết người dùng, trả trạng thái tốt nghiệp mới (None nếu bỏ qua).

    ``pattern_key`` truyền vào là pattern ĐÃ ĐÓNG BĂNG trong sổ ca lúc advisory phát ra.
    Ưu tiên nó thay vì tính lại từ ``advisory``: tính lại lúc đã biết đúng/sai cho phép
    một điểm xấu rơi sang nhóm khác, tức là mẫu số đổi sau khi biết kết quả.

    Best-effort: không bao giờ ném lỗi ra ngoài vì đây nằm trên đường ack của operator —
    hỏng việc học KHÔNG được phép làm hỏng việc ghi nhận advisory.
    """
    pattern_key = (pattern_key or "").strip() or advisory_pattern_key(advisory)
    if not pattern_key:
        logger.debug("advisory_promoter: no pattern_key trace=%s", trace_id)
        return None

    repo = getattr(ctx, "admin_repo", None)
    if repo is None or not hasattr(repo, "bump_playbook_graduation"):
        logger.debug("advisory_promoter: no repo trace=%s", trace_id)
        return None

    ws = getattr(ctx, "settings", None)
    min_success = int(
        getattr(ws, "omni_advisory_graduation_min_success", DEFAULT_MIN_SUCCESS)
        or DEFAULT_MIN_SUCCESS
    )
    max_fail_rate = float(
        getattr(ws, "omni_advisory_graduation_max_fail_rate", DEFAULT_MAX_FAIL_RATE)
        or DEFAULT_MAX_FAIL_RATE
    )

    try:
        row = await repo.bump_playbook_graduation(
            tenant_id=tenant_id,
            domain=DOMAIN_ADVISORY,
            playbook_id=pattern_key,
            success=accepted,
        )
    except Exception as exc:  # noqa: BLE001 — học hỏng không được chặn đường ack
        logger.warning("advisory_promoter: bump fail trace=%s err=%s", trace_id, exc)
        return None

    state = next_graduation_state(
        success=int(row.get("success_count", 0)),
        fail=int(row.get("fail_count", 0)),
        min_success=min_success,
        max_fail_rate=max_fail_rate,
    )

    if state != row.get("state"):
        try:
            await repo.set_playbook_graduation_state(
                tenant_id=tenant_id,
                domain=DOMAIN_ADVISORY,
                playbook_id=pattern_key,
                state=state,
            )
            logger.info(
                "event=advisory_graduation_state tenant=%s pattern=%s state=%s "
                "success=%s fail=%s trace=%s",
                tenant_id, pattern_key, state,
                row.get("success_count"), row.get("fail_count"), trace_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("advisory_promoter: state fail trace=%s err=%s", trace_id, exc)

    return state
