"""Chấm điểm năng lực từ sổ ca — thuần tuý, không I/O, không side-effect.

Thiết kế + lý do: `plans/case-ledger-design-2026-07-30.md`.

Toàn bộ module này tồn tại để trả lời một câu: *Omni có đủ bằng chứng xin thêm
quyền cho loại việc này chưa?* Nó cố tình khó làm đẹp:

- Dùng **cận dưới Wilson**, không dùng tỉ lệ thô. 3/3 = 100% là con số vô nghĩa;
  cận dưới của nó chỉ ~29%. Nhờ vậy không cần đặt ngưỡng `n` tuỳ tiện — ít mẫu
  tự động trượt.
- Trả **hai số kéo ngược nhau** (chính xác và độ phủ). Nếu chỉ đo chính xác,
  chiến lược tối ưu không phải nói dối mà là từ chối mọi ca khó.
- Ca `UNJUDGED` không vào tử số lẫn mẫu số, nhưng tỉ lệ của nó lộ ra và tự chặn
  việc xin quyền. Im lặng không phải là đồng ý.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# 95% hai phía. Cố định — để cận dưới có thể tái dựng độc lập bởi khách hàng.
Z_95 = 1.959963984540054

POSTURE_DIAGNOSED = "DIAGNOSED"
POSTURE_REFUSED = "REFUSED"
POSTURE_OUT_OF_SCOPE = "OUT_OF_SCOPE"

VERDICT_UNJUDGED = "UNJUDGED"
VERDICT_CORRECT = "CORRECT"
VERDICT_INCORRECT = "INCORRECT"
VERDICT_PARTIAL = "PARTIAL"
VERDICT_NOT_APPLICABLE = "NOT_APPLICABLE"

# Ngưỡng mặc định để một pattern đủ điều kiện xin quyền. Đây là cận dưới, không
# phải tỉ lệ thô — 0.7 ở đây khắt khe hơn nhiều so với "70% đúng".
DEFAULT_MIN_ACCURACY_LB = 0.70
DEFAULT_MIN_COVERAGE = 0.50
DEFAULT_MAX_UNJUDGED_RATIO = 0.40


def wilson_lower_bound(successes: int, total: int, *, z: float = Z_95) -> float:
    """Cận dưới khoảng tin cậy Wilson cho tỉ lệ thành công.

    Vì sao không dùng ``successes / total``: với n nhỏ, tỉ lệ thô nói dối một cách
    hợp pháp. 3/3 cho ra 1.0 và trông như bằng chứng hoàn hảo, trong khi thực tế
    ba lần đúng liên tiếp hoàn toàn có thể là may. Cận dưới Wilson phạt mẫu nhỏ
    đúng theo mức bất định thống kê, nên không cần thêm một ngưỡng ``n`` do người
    tự nghĩ ra — thứ mà ai cũng có thể tranh cãi hoặc nới dần.
    """
    if total <= 0:
        return 0.0
    successes = max(0, min(successes, total))
    p = successes / total
    z2 = z * z
    denom = 1.0 + z2 / total
    centre = p + z2 / (2 * total)
    margin = z * math.sqrt((p * (1.0 - p) + z2 / (4 * total)) / total)
    return max(0.0, (centre - margin) / denom)


@dataclass(frozen=True)
class CompetencyReport:
    """Hồ sơ năng lực của MỘT ``pattern_key``. Mọi số đều tái dựng được từ sổ ca."""

    pattern_key: str
    tenant_id: str

    total_cases: int
    diagnosed: int
    refused: int
    out_of_scope: int

    correct: int
    incorrect: int
    partial: int
    unjudged: int

    accuracy_lower_bound: float
    accuracy_raw: float
    coverage: float
    unjudged_ratio: float
    recurrence_rate: float

    eligible: bool
    blockers: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "pattern_key": self.pattern_key,
            "tenant_id": self.tenant_id,
            "total_cases": self.total_cases,
            "diagnosed": self.diagnosed,
            "refused": self.refused,
            "out_of_scope": self.out_of_scope,
            "correct": self.correct,
            "incorrect": self.incorrect,
            "partial": self.partial,
            "unjudged": self.unjudged,
            "accuracy_lower_bound": round(self.accuracy_lower_bound, 4),
            "accuracy_raw": round(self.accuracy_raw, 4),
            "coverage": round(self.coverage, 4),
            "unjudged_ratio": round(self.unjudged_ratio, 4),
            "recurrence_rate": round(self.recurrence_rate, 4),
            "eligible": self.eligible,
            "blockers": list(self.blockers),
        }


def build_competency_report(
    cases: list[dict[str, Any]],
    *,
    pattern_key: str,
    tenant_id: str,
    verdict_field: str = "diagnosis_verdict",
    min_accuracy_lb: float = DEFAULT_MIN_ACCURACY_LB,
    min_coverage: float = DEFAULT_MIN_COVERAGE,
    max_unjudged_ratio: float = DEFAULT_MAX_UNJUDGED_RATIO,
) -> CompetencyReport:
    """Dựng hồ sơ năng lực từ danh sách hàng sổ ca đã lọc theo pattern.

    ``PARTIAL`` cố ý KHÔNG tính là thành công. Nửa đúng không phải bằng chứng để
    được trao quyền tự thực thi — nó chỉ chứng minh Omni không lạc hoàn toàn.
    """
    diagnosed = refused = out_of_scope = unknown_posture = 0
    correct = incorrect = partial = unjudged = 0
    recurred = 0

    for row in cases:
        posture = str(row.get("posture") or "")
        if posture == POSTURE_REFUSED:
            refused += 1
        elif posture == POSTURE_OUT_OF_SCOPE:
            out_of_scope += 1
        elif posture == POSTURE_DIAGNOSED:
            diagnosed += 1
        else:
            # Fail-closed. Trước đây mọi giá trị lạ (rỗng, None, sai chính tả) rơi vào
            # nhánh DIAGNOSED — tức một đường ghi bỏ qua CaseLedgerStore có thể phồng
            # mẫu số độ chính xác bằng dữ liệu rác. Đếm riêng và chặn eligible thì
            # hỏng dữ liệu lộ ra thay vì âm thầm có lợi cho Omni.
            unknown_posture += 1
            continue

        if bool(row.get("recurred")):
            recurred += 1

        # Chỉ ca DIAGNOSED mới có nghĩa khi chấm đúng/sai: một ca bị từ chối thì
        # không có chẩn đoán nào để chấm. Nhưng nó VẪN nằm trong mẫu số độ phủ.
        if posture != POSTURE_DIAGNOSED:
            continue
        verdict = str(row.get(verdict_field) or VERDICT_UNJUDGED)
        if verdict == VERDICT_CORRECT:
            correct += 1
        elif verdict == VERDICT_INCORRECT:
            incorrect += 1
        elif verdict == VERDICT_PARTIAL:
            partial += 1
        else:
            unjudged += 1

    total = diagnosed + refused + out_of_scope
    judged = correct + incorrect + partial
    accuracy_raw = (correct / judged) if judged else 0.0
    accuracy_lb = wilson_lower_bound(correct, judged)

    # Mẫu số độ phủ KHÔNG gồm OUT_OF_SCOPE: những ca đó Omni chẩn đoán được nhưng
    # không có quyền hành động — phạt nó vì giới hạn do người đặt là vô lý.
    coverage_denom = diagnosed + refused
    coverage = (diagnosed / coverage_denom) if coverage_denom else 0.0
    unjudged_ratio = (unjudged / diagnosed) if diagnosed else 1.0
    recurrence_rate = (recurred / total) if total else 0.0

    # Blocker là thứ admin khách đọc để hiểu vì sao Omni chưa được trao quyền — nêu
    # nguyên nhân GỐC, không liệt kê cả những con số vốn chỉ là hệ quả của nó.
    blockers: list[str] = []
    if unknown_posture:
        blockers.append(f"{unknown_posture} ca có posture không hợp lệ (dữ liệu hỏng)")
    if diagnosed == 0:
        blockers.append("chưa có ca nào được chẩn đoán")
    elif judged == 0:
        blockers.append("chưa có ca nào được phán quyết")
    else:
        if accuracy_lb < min_accuracy_lb:
            blockers.append(
                f"cận dưới độ chính xác {accuracy_lb:.2f} < {min_accuracy_lb:.2f}"
            )
        if unjudged_ratio > max_unjudged_ratio:
            blockers.append(
                f"tỉ lệ chưa phán quyết {unjudged_ratio:.2f} > {max_unjudged_ratio:.2f}"
            )
    if coverage < min_coverage and (diagnosed or refused):
        blockers.append(f"độ phủ {coverage:.2f} < {min_coverage:.2f}")

    return CompetencyReport(
        pattern_key=pattern_key,
        tenant_id=tenant_id,
        total_cases=total,
        diagnosed=diagnosed,
        refused=refused,
        out_of_scope=out_of_scope,
        correct=correct,
        incorrect=incorrect,
        partial=partial,
        unjudged=unjudged,
        accuracy_lower_bound=accuracy_lb,
        accuracy_raw=accuracy_raw,
        coverage=coverage,
        unjudged_ratio=unjudged_ratio,
        recurrence_rate=recurrence_rate,
        eligible=not blockers,
        blockers=tuple(blockers),
    )
