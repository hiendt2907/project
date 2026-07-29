"""Omni tự xin thêm quyền — theo TỪNG ``pattern_key``, kèm số liệu tự chứng minh.

Thiết kế + lý do: `plans/case-ledger-design-2026-07-30.md`, mục "Xin quyền".

Omni ở đây được mô hình hoá như một nhân viên đang thử việc: nó không ngồi chờ
được đánh giá, nó tự dựng hồ sơ năng lực từ sổ ca rồi chủ động nộp đơn. Nhưng đơn
đó phải khó nộp, vì bên nộp đơn cũng chính là bên sinh ra dữ liệu.

Bốn cái phanh, mỗi cái chặn một chiến lược lách khác nhau:

1. **Xin theo từng pattern, không xin nâng tier tổng.** Bằng chứng Omni có là
   bằng chứng theo loại việc. Giỏi khởi động lại pod không nói lên điều gì về
   việc xoay credential — mà nâng tier tổng thì mở cả hai.
2. **Chỉ pattern ``eligible``** (cận dưới Wilson, xem ``scoring.py``). 3/3 đúng
   không đủ: cận dưới của nó chỉ ~29%.
3. **Bị từ chối → khoá xin lại.** Nếu xin miễn phí thì chiến lược tối ưu không
   phải làm tốt hơn mà là xin liên tục tới lúc admin mệt mà duyệt.
4. **``frozen`` chỉ người gỡ.** Bất đối xứng có chủ đích: tự lên bậc được, không
   tự gỡ án được. Module này cố tình không có đường nào gỡ đóng băng.

Và một quy tắc về nội dung: ``evidence`` nộp kèm đơn là ``report.as_dict()``
nguyên vẹn — không thêm bất kỳ trường nào do LLM sinh ra. Nếu đơn có một đoạn
văn thuyết phục do model viết thì bùa số nằm ngay ở khâu kể chuyện, chưa cần đụng
tới dữ liệu.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from services.case_ledger.scoring import CompetencyReport, build_competency_report
from services.case_ledger.store_scope import (
    DEFAULT_REJECT_COOLDOWN_DAYS,
    ScopeStore,
)

logger = logging.getLogger(__name__)

# Thang quyền, đi lên từng bậc một. Không có đường tắt từ SUGGEST_ONLY thẳng lên
# AUTO_EXECUTE: mỗi bậc là một lần người nhìn lại bằng chứng.
SCOPE_LADDER = ("SUGGEST_ONLY", "HITL_REQUIRED", "AUTO_EXECUTE")
DEFAULT_SCOPE = SCOPE_LADDER[0]

# Lý do KHÔNG xin được — chuỗi ổn định để portal hiển thị và test khẳng định.
SKIP_NOT_ELIGIBLE = "chưa đủ bằng chứng"
SKIP_PENDING = "đã có đơn đang chờ duyệt"
SKIP_COOLDOWN = "đang trong thời gian khoá sau khi bị từ chối"
SKIP_FROZEN = "pattern đang bị đóng băng — chỉ người gỡ được"
SKIP_MAX_SCOPE = "đã ở bậc quyền cao nhất"


def next_scope(current: str | None) -> str | None:
    """Bậc kế tiếp trên thang quyền, hoặc None nếu đã kịch trần."""
    cur = current or DEFAULT_SCOPE
    if cur not in SCOPE_LADDER:
        return None
    idx = SCOPE_LADDER.index(cur)
    return SCOPE_LADDER[idx + 1] if idx + 1 < len(SCOPE_LADDER) else None


@dataclass(frozen=True)
class AdvocacyOutcome:
    """Kết quả xét một ``pattern_key``: xin được hay không, và vì sao không."""

    pattern_key: str
    report: CompetencyReport
    requested: bool
    requested_scope: str | None = None
    request_id: int | None = None
    skip_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "pattern_key": self.pattern_key,
            "requested": self.requested,
            "requested_scope": self.requested_scope,
            "request_id": self.request_id,
            "skip_reason": self.skip_reason,
            # Kể cả khi KHÔNG xin được, số liệu vẫn hiện ra: khách cần thấy Omni
            # còn thiếu gì, không chỉ thấy nó im lặng.
            "report": self.report.as_dict(),
        }


class ScopeAdvocate:
    """Duyệt các pattern của một tenant và nộp đơn cho những cái đủ điều kiện."""

    def __init__(
        self,
        ledger_store: Any,
        scope_store: ScopeStore,
        *,
        cooldown_days: int = DEFAULT_REJECT_COOLDOWN_DAYS,
        case_limit: int = 500,
    ) -> None:
        self._ledger = ledger_store
        self._scope = scope_store
        self._cooldown_days = cooldown_days
        self._case_limit = case_limit

    async def build_reports(self, *, tenant_id: str) -> list[CompetencyReport]:
        """Hồ sơ năng lực cho mọi pattern tenant từng gặp.

        Mẫu số lấy từ chính sổ ca, không lọc thêm gì ở đây. Mọi cơ hội "chọn ca"
        đều là cơ hội bùa số, nên tầng này cố ý không có tham số lọc nào ngoài
        ``tenant_id``.
        """
        patterns = await self._ledger.list_patterns(tenant_id=tenant_id)
        reports: list[CompetencyReport] = []
        for pattern_key in sorted(patterns):
            cases = await self._ledger.list_cases_for_pattern(
                tenant_id=tenant_id, pattern_key=pattern_key, limit=self._case_limit
            )
            reports.append(
                build_competency_report(
                    cases, pattern_key=pattern_key, tenant_id=tenant_id
                )
            )
        return reports

    async def evaluate_pattern(
        self, *, tenant_id: str, report: CompetencyReport
    ) -> AdvocacyOutcome:
        """Xét MỘT pattern. Thứ tự kiểm tra là cố ý: bằng chứng trước, quyền sau.

        Nếu kiểm tra ``frozen`` trước thì một pattern bị đóng băng sẽ luôn báo
        "đóng băng" và che mất sự thật rằng nó cũng chưa đủ bằng chứng — người gỡ
        án sẽ gỡ mà không biết mình vừa gỡ cho cái gì.
        """
        pattern_key = report.pattern_key
        if not report.eligible:
            return AdvocacyOutcome(
                pattern_key=pattern_key,
                report=report,
                requested=False,
                skip_reason=SKIP_NOT_ELIGIBLE,
            )

        grant = await self._scope.get_grant(
            tenant_id=tenant_id, pattern_key=pattern_key
        )
        if grant and bool(grant.get("frozen")):
            return AdvocacyOutcome(
                pattern_key=pattern_key,
                report=report,
                requested=False,
                skip_reason=SKIP_FROZEN,
            )

        target = next_scope(str(grant.get("granted_scope")) if grant else None)
        if target is None:
            return AdvocacyOutcome(
                pattern_key=pattern_key,
                report=report,
                requested=False,
                skip_reason=SKIP_MAX_SCOPE,
            )

        if await self._scope.open_request(
            tenant_id=tenant_id, pattern_key=pattern_key
        ):
            return AdvocacyOutcome(
                pattern_key=pattern_key,
                report=report,
                requested=False,
                skip_reason=SKIP_PENDING,
            )

        if await self._scope.active_cooldown(
            tenant_id=tenant_id, pattern_key=pattern_key
        ):
            return AdvocacyOutcome(
                pattern_key=pattern_key,
                report=report,
                requested=False,
                skip_reason=SKIP_COOLDOWN,
            )

        # evidence = report.as_dict() NGUYÊN VẸN. Mọi số trong đó truy được về sổ
        # ca; không thêm trường tự do nào để không ai chèn được lời kể vào bằng
        # chứng.
        row = await self._scope.create_request(
            tenant_id=tenant_id,
            pattern_key=pattern_key,
            requested_scope=target,
            evidence=report.as_dict(),
        )
        return AdvocacyOutcome(
            pattern_key=pattern_key,
            report=report,
            requested=True,
            requested_scope=target,
            request_id=row.get("id"),
        )

    async def run(self, *, tenant_id: str) -> list[AdvocacyOutcome]:
        """Một vòng tự đánh giá + xin quyền cho toàn bộ pattern của tenant."""
        outcomes: list[AdvocacyOutcome] = []
        for report in await self.build_reports(tenant_id=tenant_id):
            try:
                outcomes.append(
                    await self.evaluate_pattern(tenant_id=tenant_id, report=report)
                )
            except Exception as exc:  # noqa: BLE001 — một pattern hỏng không được
                # làm chết cả vòng: các pattern còn lại vẫn phải được xét.
                logger.error(
                    "advocacy: pattern %s cua tenant %s that bai: %s",
                    report.pattern_key,
                    tenant_id,
                    exc,
                )
                outcomes.append(
                    AdvocacyOutcome(
                        pattern_key=report.pattern_key,
                        report=report,
                        requested=False,
                        skip_reason=f"loi: {exc}",
                    )
                )
        return outcomes


async def approve_request(
    scope_store: ScopeStore,
    *,
    request_id: int,
    tenant_id: str,
    actor: str,
    note: str = "",
) -> dict[str, Any] | None:
    """Người duyệt: chuyển đơn sang APPROVED VÀ ghi ``scope_grant``.

    Hai bước phải đi liền nhau — một đơn APPROVED mà không có grant tương ứng là
    quyền đã hứa nhưng không có hiệu lực, còn grant không có đơn là quyền không
    truy được về bằng chứng nào. ``upsert_grant`` tự bỏ qua pattern đang ``frozen``
    (``WHERE NOT frozen``), nên duyệt nhầm một pattern bị đóng băng vẫn không cấp
    được quyền.
    """
    row = await scope_store.decide_request(
        request_id=request_id,
        tenant_id=tenant_id,
        decision="APPROVED",
        actor=actor,
        note=note,
    )
    if row is None:
        return None
    grant = await scope_store.upsert_grant(
        tenant_id=tenant_id,
        pattern_key=str(row.get("pattern_key")),
        granted_scope=str(row.get("requested_scope")),
        granted_by=actor,
    )
    return {**row, "grant": grant}


async def reject_request(
    scope_store: ScopeStore,
    *,
    request_id: int,
    tenant_id: str,
    actor: str,
    note: str = "",
    cooldown_days: int = DEFAULT_REJECT_COOLDOWN_DAYS,
) -> dict[str, Any] | None:
    """Người từ chối: đặt luôn ``cooldown_until`` trong cùng câu UPDATE."""
    return await scope_store.decide_request(
        request_id=request_id,
        tenant_id=tenant_id,
        decision="REJECTED",
        actor=actor,
        note=note,
        cooldown_days=cooldown_days,
    )
