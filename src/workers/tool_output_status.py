"""Canonical success/failure classifier cho tool output dạng text.

Root cause thật (2026-08-13): `proactive_observer._quick_verify_output()` và
`tool_registry.py`'s `is_error_output` mỗi nơi tự đoán trạng thái tool riêng, qua HAI
danh sách từ khoá không đồng bộ — cùng một token thất bại (`deployment_not_found`) lọt
qua một bên nhưng không lọt qua bên kia. Hậu quả xảy ra thật:
  1. `_quick_verify_output` báo verified=True sai → Telegram báo [AUTO-FIX-LEARNING]
     thành công dù `k8s_rollout_restart` thực tế fail, và bản ghi lỗi bị học ngược vào
     `action_experience` như một pattern "đã sửa đúng".
  2. `is_error_output` trong `tool_registry.py` KHÔNG nhận ra fail này, nên idempotency
     lock không được nhả ra để retry — bug song sinh cùng gốc, phát hiện khi rà lại.

Thay vì thêm từ khoá vào từng nơi riêng (hardcode nhân bản, sai lệch dần theo thời
gian), mọi nơi cần biết "tool này chạy ổn không" PHẢI gọi qua đây — một nguồn thật duy
nhất. Thêm token lỗi mới (khi phát hiện tool mới dùng từ khoá thất bại chưa có) chỉ cần
sửa MỘT chỗ trong `_DATA_FAIL_TOKENS`.

Thứ tự ưu tiên đọc (dừng ở bước đầu tiên khớp):
1. `[STATUS] ok` / `[STATUS] fail` — tag tường minh nếu tool đã áp dụng (khuyến khích
   tool mới dùng cái này thay vì để đoán qua token `[DATA]`).
2. `[STATUS] business_hit` (ok) / `[STATUS] empty_result|error` (fail) — convention
   3 trạng thái đã có sẵn ở nhóm tool tra cứu (PromQL/vendor knowledge).
3. Token đầu dòng `[DATA] <token>` — convention phổ biến nhất (k8s_tools.py,
   k8s_cluster_tools.py, kubectl_cluster.py, ...). Token kết thúc `_ok` → thành công;
   khớp `_DATA_FAIL_TOKENS` → thất bại.
4. CSV từ khoá cấu hình được (tham số `extra_fail_keywords_csv`) — lưới an toàn cuối
   cho tool/free-text chưa theo convention nào ở trên.

Không khớp gì ở cả 4 bước → "unknown" (không đoán bừa); caller tự quyết định coi
"unknown" là ok hay fail tuỳ ngữ cảnh (proactive_observer coi unknown là "chưa đủ bằng
chứng để nói fail" nên vẫn permissive; tool_registry coi unknown là "không phải lỗi rõ
ràng" nên cũng không chặn — cả hai đúng với hành vi gốc trước khi có module này).
"""

from __future__ import annotations

import re

_DATA_LINE_RE = re.compile(r"^\[DATA\]\s*(\S+)", re.IGNORECASE | re.MULTILINE)

# Token trên dòng [DATA] biết chắc là THẤT BẠI. Nguồn thật duy nhất — không nhân bản ở
# nơi khác. Bổ sung tại đây khi phát hiện tool mới dùng token lỗi chưa liệt kê.
_DATA_FAIL_TOKENS: tuple[str, ...] = (
    "error",
    "not_found",
    "ambiguous",
    "confirm_required",
    "stale_state",
    "khong_co_quyen",
    "no_data",
    "no_redis",
)

_MISSING_ARG_PHRASES: tuple[str, ...] = (
    "thiếu args",
    "missing arg",
    "invalid args",
    "required",
    "missing required",
)


def classify_tool_output(text: str, extra_fail_keywords_csv: str = "") -> str:
    """Trả "ok" | "fail" | "unknown". Không bao giờ ném exception."""
    t = text or ""
    tl = t.lower()
    if not tl.strip():
        return "fail"

    if "[status] ok" in tl:
        return "ok"
    if "[status] fail" in tl:
        return "fail"

    if "[status] business_hit" in tl:
        return "ok"
    if "[status] empty_result" in tl or "[status] error" in tl:
        return "fail"

    m = _DATA_LINE_RE.search(t)
    if m:
        token = m.group(1).lower()
        if token.startswith("kubectl_exit_"):
            return "ok" if token == "kubectl_exit_0" else "fail"
        if any(fail_tok in token for fail_tok in _DATA_FAIL_TOKENS):
            return "fail"
        if token.endswith("_ok") or token == "ok":
            return "ok"

    for kw in [k.strip().lower() for k in (extra_fail_keywords_csv or "").split(",") if k.strip()]:
        if kw and kw in tl:
            return "fail"
    if any(phrase in tl for phrase in _MISSING_ARG_PHRASES):
        return "fail"

    return "unknown"
