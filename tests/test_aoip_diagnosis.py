"""Tests Diagnosis Engine (core) — three-valued probe + coverage-aware confidence.

Hardening (review production): probe trả PRESENT/ABSENT/UNAVAILABLE. UNAVAILABLE
(không kiểm được: thiếu quyền/không systemd/timeout) KHÔNG phải counter-evidence —
giả thuyết thành UNTESTED, không bị bác bỏ. Confidence dựa trên positive evidence
+ COVERAGE (đã kiểm bao nhiêu), không chỉ "còn 1 survivor". Nhiều nguyên nhân có
thể đồng thời. Core domain-AGNOSTIC.
"""
from __future__ import annotations

import pytest

from aoip.diagnosis import ProbeOutcome, diagnose
from aoip.objects import Hypothesis

P, A, U = ProbeOutcome.PRESENT, ProbeOutcome.ABSENT, ProbeOutcome.UNAVAILABLE


def _h(claim: str, prior: float = 0.4) -> Hypothesis:
    return Hypothesis(claim=claim, predicted_evidence=(f"evidence of {claim}",),
                      prior=prior, origin="DIAGNOSIS")


async def test_present_absent_unavailable_classified():
    result = await diagnose([
        (_h("process_down"), lambda: P),
        (_h("disk_full"), lambda: A),
        (_h("oom_kill"), lambda: U),
    ])
    assert {f.claim for f in result.findings} == {"process_down"}
    assert set(result.rejected) == {"disk_full"}
    assert [c for c, _ in result.untested] == ["oom_kill"]


async def test_full_coverage_single_cause_high_confidence():
    result = await diagnose([
        (_h("process_down"), lambda: P),
        (_h("disk_full"), lambda: A),
        (_h("oom_kill"), lambda: A),
        (_h("network"), lambda: A),
    ])
    assert result.confidence >= 0.8  # 1 nguyên nhân, kiểm hết → tự tin


async def test_unavailable_is_not_counter_evidence_and_lowers_confidence():
    # Chỉ 1 probe chạy được (present), 3 cái UNAVAILABLE → coverage thấp → confidence thấp,
    # dù "chỉ còn 1 survivor". Bác bỏ KHÔNG xảy ra với cái không kiểm được.
    low = await diagnose([
        (_h("process_down"), lambda: P),
        (_h("disk_full"), lambda: U),
        (_h("oom_kill"), lambda: U),
        (_h("network"), lambda: U),
    ])
    assert {f.claim for f in low.findings} == {"process_down"}
    assert low.rejected == ()                 # UNAVAILABLE không phải counter-evidence
    assert len(low.untested) == 3
    full = await diagnose([
        (_h("process_down"), lambda: P),
        (_h("disk_full"), lambda: A),
        (_h("oom_kill"), lambda: A),
        (_h("network"), lambda: A),
    ])
    assert low.confidence < full.confidence   # coverage thấp → kém tự tin hơn


async def test_multiple_simultaneous_causes_allowed_but_ambiguous():
    result = await diagnose([
        (_h("disk_full"), lambda: P),
        (_h("process_down"), lambda: P),   # disk full → process crash: cùng xảy ra
    ])
    assert len(result.findings) == 2          # giữ cả hai (contributing causes)
    assert result.confidence < 0.8            # mơ hồ về root cause đơn


async def test_no_cause_found_low_confidence():
    result = await diagnose([(_h("process_down"), lambda: A), (_h("disk_full"), lambda: A)])
    assert result.findings == ()
    assert result.confidence <= 0.2


async def test_all_unavailable_means_unknown_not_healthy():
    # Không kiểm được gì → KHÔNG kết luận khỏe, KHÔNG kết luận sự cố.
    result = await diagnose([(_h("process_down"), lambda: U), (_h("disk_full"), lambda: U)])
    assert result.findings == () and result.rejected == ()
    assert len(result.untested) == 2
    assert result.confidence <= 0.2


async def test_async_probe_supported():
    async def aprobe():
        return P
    result = await diagnose([(_h("process_down"), aprobe)])
    assert {f.claim for f in result.findings} == {"process_down"}


def test_confidence_is_internal_score_not_calibrated_probability():
    # GUARD: confidence phải được mô tả là SCORE nội bộ, KHÔNG phải xác suất calibrate.
    # Tránh ai đó diễn giải 0.787 thành "78,7% khả năng đúng" khi chưa có dữ liệu lịch sử.
    import aoip.diagnosis as core
    src = open(core.__file__).read().lower()
    assert "score" in src
    assert "calibrate" in src and "xác suất" in src


def test_core_engine_is_domain_agnostic():
    import aoip.diagnosis as core
    src = open(core.__file__).read().lower()
    for w in ("redis", "systemctl", "dmesg", "disk_full", "df ", "restart", "cache"):
        assert w not in src
