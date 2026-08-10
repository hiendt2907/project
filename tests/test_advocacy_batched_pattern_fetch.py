"""P1 #6 — ScopeAdvocate.build_reports không còn N round-trip Postgres TUẦN TỰ.

Bối cảnh (audit 2026-08-10, docs/audit/BACKEND_AUDIT_PLAN_2026-08-10.md #6):
`GET /competency/patterns` gọi `build_reports`, trước đây await từng
`list_cases_for_pattern` một-một — tenant N pattern giữ pool connection qua N
round-trip tuần tự trên đường HTTP. Fix: chạy song song, giới hạn bởi
`_MAX_CONCURRENT_PATTERN_FETCHES` để không chiếm hết pool (mặc định pool max 8).

Test không đổi ngữ nghĩa SQL/dual-key (rủi ro compliance, không đáng viết lại)
— chỉ xác nhận: (1) không vượt giới hạn đồng thời, (2) kết quả giữ đúng thứ tự
theo pattern_key đã sort, (3) nội dung báo cáo giống hệt chạy tuần tự.
"""

from __future__ import annotations

import asyncio

import pytest

from services.case_ledger.advocacy import _MAX_CONCURRENT_PATTERN_FETCHES, ScopeAdvocate


class _ConcurrencyTrackingLedger:
    """Ledger giả: mỗi list_cases_for_pattern ngủ một nhịp để buộc các lời gọi
    chồng lấp thời gian nếu build_reports thực sự chạy song song."""

    def __init__(self, patterns: list[str]) -> None:
        self._patterns = patterns
        self.in_flight = 0
        self.max_in_flight = 0
        self.call_order: list[str] = []

    async def list_patterns(self, *, tenant_id: str) -> list[str]:
        return list(self._patterns)

    async def list_cases_for_pattern(
        self, *, tenant_id: str, pattern_key: str, limit: int
    ) -> list[dict]:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        self.call_order.append(pattern_key)
        await asyncio.sleep(0.01)
        self.in_flight -= 1
        # 3 case DIAGNOSED/CONFIRMED đủ để build_competency_report tính ra một
        # cận dưới khác 0 — nội dung cụ thể không quan trọng, chỉ cần xác định.
        return [
            {
                "case_id": f"{pattern_key}-{i}",
                "tenant_id": tenant_id,
                "pattern_key": pattern_key,
                "diagnosis_verdict": "CONFIRMED",
                "remedy_verdict": "CONFIRMED",
                "opened_at": i,
            }
            for i in range(3)
        ]


@pytest.mark.asyncio
async def test_build_reports_runs_concurrently_bounded() -> None:
    patterns = [f"pattern-{i}" for i in range(10)]
    ledger = _ConcurrencyTrackingLedger(patterns)
    advocate = ScopeAdvocate(ledger, scope_store=None)

    reports = await advocate.build_reports(tenant_id="t1")

    assert ledger.max_in_flight > 1, "phải thực sự chạy song song, không phải tuần tự"
    assert ledger.max_in_flight <= _MAX_CONCURRENT_PATTERN_FETCHES, (
        "không được vượt giới hạn đồng thời — sẽ chiếm hết pool connection"
    )
    assert [r.pattern_key for r in reports] == sorted(patterns), (
        "kết quả phải giữ đúng thứ tự theo pattern_key đã sort dù chạy song song"
    )


@pytest.mark.asyncio
async def test_build_reports_content_matches_sequential_baseline() -> None:
    patterns = ["b-pattern", "a-pattern", "c-pattern"]
    ledger = _ConcurrencyTrackingLedger(patterns)
    advocate = ScopeAdvocate(ledger, scope_store=None)

    concurrent_reports = await advocate.build_reports(tenant_id="t1")

    # Baseline tuần tự thủ công, tự viết tay để không phụ thuộc lại vào code đang test.
    sequential_reports = []
    from services.case_ledger.scoring import build_competency_report

    for pk in sorted(patterns):
        cases = await ledger.list_cases_for_pattern(tenant_id="t1", pattern_key=pk, limit=500)
        sequential_reports.append(build_competency_report(cases, pattern_key=pk, tenant_id="t1"))

    assert [r.as_dict() for r in concurrent_reports] == [r.as_dict() for r in sequential_reports]
