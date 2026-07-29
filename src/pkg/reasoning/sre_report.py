"""Báo cáo SRE định kỳ cho một tenant — trục S4 "giao tiếp" (G4).

Người đọc mục tiêu là chủ hệ thống phía khách hàng, không phải kỹ sư Omni. Vì vậy
báo cáo nói bằng tiếng Việt đời thường, và khi CHƯA có dữ liệu thì nói thẳng là chưa
có — không tô vẽ thành "mọi thứ ổn định", vì hai điều đó khác nhau hoàn toàn.

INVARIANT: thuần tuý (không I/O) và chỉ ghi nhận SỰ TỒN TẠI của rủi ro, không bao giờ
chép giá trị secret vào báo cáo (INV_DATA_RESIDENCY).
"""

from __future__ import annotations

from typing import Any

from pkg.reasoning.capacity_advisor import (
    ACTION_HOLD,
    ACTION_INSUFFICIENT_DATA,
    CapacityAdvice,
)

_ACTION_LABEL_VI = {
    "SCALE_UP": "Cần nâng dung lượng",
    "SCALE_DOWN": "Có thể thu hẹp để tiết kiệm",
    "INVESTIGATE_LEAK": "Nghi rò rỉ — cần điều tra",
    ACTION_HOLD: "Giữ nguyên",
    ACTION_INSUFFICIENT_DATA: "Chưa đủ dữ liệu",
}


def _capacity_sort_key(a: CapacityAdvice) -> tuple[int, float]:
    """Khẩn cấp lên đầu; trong cùng nhóm thì cái sắp chạm ngưỡng trước đứng trước."""
    urgent_rank = 0 if a.urgent else 1
    days = a.days_to_threshold if a.days_to_threshold is not None else float("inf")
    return (urgent_rank, days)


def build_sre_report(
    *,
    tenant_id: str,
    period_days: int,
    rates: dict[str, Any],
    graduations: list[dict[str, Any]],
    capacity: list[CapacityAdvice],
    topology_facts: int,
    notes: list[str] | None = None,
) -> str:
    """Dựng báo cáo markdown cho *tenant_id* trong *period_days* ngày gần nhất."""
    lines: list[str] = [
        f"# Báo cáo vận hành hệ thống — {tenant_id}",
        "",
        f"Kỳ báo cáo: **{period_days} ngày** gần nhất.",
        "",
        "## 1. Mức độ hiểu hệ thống",
        "",
    ]

    if topology_facts > 0:
        lines.append(
            f"Omni đang nắm **{topology_facts}** dữ kiện về hạ tầng của bạn "
            "(máy chủ, dịch vụ, quan hệ phụ thuộc)."
        )
    else:
        lines.append(
            "**Chưa có** dữ kiện hạ tầng nào được thu thập — cần chạy khảo sát "
            "(discovery) trước khi Omni có thể chẩn đoán chính xác."
        )

    lines += ["", "## 2. Chất lượng chẩn đoán", ""]
    total = int(rates.get("total") or 0)
    if total == 0:
        lines.append(
            "**Chưa có** phán quyết nào từ người vận hành trong kỳ này, nên chưa thể "
            "đánh giá độ chính xác của Omni. Đây là *thiếu dữ liệu*, không phải điểm tốt."
        )
    else:
        acc = rates.get("acceptance_rate")
        fp = rates.get("fp_rate")
        lines.append(
            f"- Số chẩn đoán được người vận hành xem xét: **{total}**"
        )
        if acc is not None:
            lines.append(f"- Tỉ lệ được chấp nhận: **{acc:.0%}**")
        if fp is not None:
            lines.append(f"- Tỉ lệ báo động nhầm: **{fp:.0%}**")

    lines += ["", "## 3. Kinh nghiệm đã tích luỹ", ""]
    if graduations:
        lines.append("Các quy trình xử lý đã được kiểm chứng đủ nhiều lần để tin dùng:")
        lines.append("")
        lines.append("| Quy trình | Trạng thái | Đúng | Sai |")
        lines.append("|---|---|---|---|")
        for g in graduations:
            lines.append(
                f"| `{g.get('playbook_id', '')}` | {g.get('state', '')} | "
                f"{g.get('success_count', 0)} | {g.get('fail_count', 0)} |"
            )
    else:
        lines.append(
            "**Chưa có** quy trình nào đủ số lần kiểm chứng để tốt nghiệp. Omni vẫn "
            "phải suy luận lại từ đầu cho mỗi sự cố."
        )

    lines += ["", "## 4. Dung lượng và đề xuất mở rộng", ""]
    actionable = [a for a in capacity if a.action not in (ACTION_HOLD, ACTION_INSUFFICIENT_DATA)]
    if actionable:
        for a in sorted(actionable, key=_capacity_sort_key):
            flag = "🔴 " if a.urgent else ""
            label = _ACTION_LABEL_VI.get(a.action, a.action)
            lines.append(f"- {flag}**{a.host}** / `{a.metric}` — {label}. {a.summary}")
    elif capacity:
        lines.append("Tất cả chỉ số đang trong ngưỡng an toàn, chưa cần thay đổi dung lượng.")
    else:
        lines.append("**Chưa có** dữ liệu dung lượng nào để phân tích.")

    lines += ["", "## 5. Ghi chú khác", ""]
    if notes:
        lines += [f"- {n}" for n in notes]
    else:
        lines.append("Không có ghi chú bổ sung.")

    lines += [
        "",
        "---",
        "",
        "*Mọi đề xuất trong báo cáo này cần con người phê duyệt trước khi thực hiện. "
        "Omni không tự thay đổi hệ thống của bạn.*",
        "",
    ]
    return "\n".join(lines)
