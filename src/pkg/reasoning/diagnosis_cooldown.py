"""Cooldown chẩn đoán theo fingerprint — quyết định thuần, không I/O.

Vấn đề nó giải: một sự cố ĐANG DIỄN RA phát ``result=FAILED`` mỗi chu kỳ collect
(20s). ``assess_domain_severity`` Priority 1 nâng thẳng lên ``high``, mà lưới chặn lặp
duy nhất ở ``remote_agent_pipeline`` chỉ chặn khi mức độ THẤP (``_NOTIFY_TIERS``) — nên
mọi sự cố nghiêm trọng bị chẩn đoán lại từ đầu mỗi 20 giây, vô hạn.

Đo trên UAT thật (audit Đ51, 2026-08-11): **989 lượt chẩn đoán cho 33 vấn đề duy nhất**
— lặp 96.7%. Nhu cầu LLM ~173 lượt/giờ so với công suất ~150 (``NUM_PARALLEL=1``,
24s/lượt) ⇒ hàng đợi tăng vô hạn ⇒ 74.3% lượt chết timeout ⇒ 91.9% tin Telegram chỉ còn
lời khuyên chung chung. Nén lặp ở đây cắt tải xuống ~4% công suất, tức chữa nguyên nhân
chứ không phải bịt triệu chứng.

Tách khỏi ``evidence_cluster`` vì đây là CHÍNH SÁCH (khi nào đáng tốn một lượt LLM), còn
bên kia là LƯU TRỮ trạng thái cluster. Thuần và không I/O nên test được mọi nhánh biên mà
không cần Redis.

Hai bất biến, cả hai đều nghiêng về AN TOÀN chứ không nghiêng về tiết kiệm LLM:
  1. **Leo thang luôn xuyên qua** — vấn đề trở nặng phải được chẩn lại ngay lập tức.
     Cooldown chỉ được nén tiếng ồn, tuyệt đối không được che sự cố đang xấu đi.
  2. **Fail-open** — state hỏng/thiếu/lệch đồng hồ thì CHẨN. Thà tốn một lượt LLM còn hơn
     bịt một sự cố thật.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

# 15 phút. Chọn theo số đo thật chứ không theo cảm tính: chu kỳ collect là 20s, nên
# cooldown này gộp ~45 lần phát lặp thành 1 lượt chẩn đoán. Với 33 vấn đề duy nhất quan
# sát được trong 16 giờ, tải LLM rơi từ ~173 xuống ~4 lượt/giờ — dưới xa công suất ~150,
# nên hàng đợi không còn tích luỹ. Cũng đủ ngắn để một sự cố kéo dài vẫn được nhắc lại
# 4 lần/giờ thay vì im lặng hẳn.
COOLDOWN_S = 900

# Thang mức độ để phát hiện leo thang. Chỉ cần thứ tự, không cần trùng khớp với bất kỳ
# bảng nào khác — giá trị lạ rơi về -1 và không bao giờ tính là leo thang.
_URGENCY_RANK: dict[str, int] = {
    "baseline": 0, "low": 1, "medium": 2, "high": 3, "critical": 4,
}

# Quãng nghỉ khi lượt trước KHÔNG ra được kết luận: ngắn hơn nhiều, nhưng khác 0.
#
# Vì sao không phải 0 (bỏ qua cooldown hẳn) — đây là cái bẫy tự huỷ: ở UAT 74.3% lượt
# chết vì LLM quá tải. Nếu mọi ca hỏng đều thử lại ngay chu kỳ sau (20s) thì tải không
# giảm ⇒ LLM vẫn quá tải ⇒ vẫn hỏng, và cooldown mất tác dụng ĐÚNG lúc cần nhất.
# Vì sao không phải COOLDOWN_S đầy đủ: một sự cố thật không được im lặng 15 phút chỉ vì
# LLM tình cờ timeout một lần.
RETRY_COOLDOWN_S = 180

# Verdict cho biết lượt chẩn đoán TRƯỚC không ra được kết luận.
_FAILED_VERDICTS = frozenset({"llm_error", "parse_error", "error", "inconclusive", ""})


@dataclass(frozen=True)
class CooldownDecision:
    diagnose: bool
    reason: str
    cooldown_remaining_s: float = 0.0


def _rank(urgency: Any) -> int:
    return _URGENCY_RANK.get(str(urgency or "").strip().lower(), -1)


def should_diagnose(
    *,
    seen_state: dict[str, Any] | None,
    urgency: str,
    now: float | None = None,
    cooldown_s: float = COOLDOWN_S,
) -> CooldownDecision:
    """Có đáng tốn một lượt chẩn đoán LLM cho fingerprint này không?

    ``seen_state`` là bản ghi ``omni:evcluster:seen:{fingerprint}`` (xem
    ``evidence_cluster.get_seen_state``); ``None`` = chưa từng thấy.
    """
    now = time.time() if now is None else now

    if not seen_state:
        return CooldownDecision(True, "first_time")

    last = seen_state.get("last_diagnosis")
    if not isinstance(last, dict):
        # Bao gồm cả `None` (chưa chẩn lần nào) lẫn dữ liệu bẩn — cả hai đều fail-open.
        return CooldownDecision(True, "never_diagnosed")

    try:
        last_ts = float(last["ts"])
    except (KeyError, TypeError, ValueError):
        return CooldownDecision(True, "malformed_state")

    if last_ts > now:
        # Lệch đồng hồ giữa các worker: nếu tin mốc tương lai, cooldown sẽ kéo dài tuỳ ý
        # và bịt sự cố thật cho tới khi đồng hồ đuổi kịp.
        return CooldownDecision(True, "clock_skew")

    elapsed = now - last_ts

    # Leo thang kiểm TRƯỚC mọi ngưỡng thời gian: vấn đề trở nặng phải được chẩn lại ngay,
    # bất kể còn bao nhiêu cooldown.
    if _rank(urgency) > _rank(last.get("urgency")):
        return CooldownDecision(True, "escalated")

    # Lượt trước không ra kết luận ⇒ dùng quãng nghỉ NGẮN (xem RETRY_COOLDOWN_S).
    if str(last.get("verdict") or "").strip().lower() in _FAILED_VERDICTS:
        if elapsed >= RETRY_COOLDOWN_S:
            return CooldownDecision(True, "retry_after_failure")
        return CooldownDecision(
            False, "retry_cooldown_active", RETRY_COOLDOWN_S - elapsed
        )

    if elapsed >= cooldown_s:
        return CooldownDecision(True, "cooldown_expired")

    return CooldownDecision(False, "cooldown_active", cooldown_s - elapsed)
