"""Diagnosis Engine (core) — multi-hypothesis falsification, domain-AGNOSTIC.

Vì sao tồn tại: một triệu chứng KHÔNG đồng nghĩa một nguyên nhân. Recovery chỉ
đáng tin khi Diagnosis đáng tin (chống anti-pattern chữa-triệu-chứng lặp vô hạn).

Hardening cho production (3 điểm cốt lõi):
  1. Probe BA TRẠNG THÁI: PRESENT / ABSENT / UNAVAILABLE. UNAVAILABLE (không kiểm
     được: thiếu quyền, không phải substrate phù hợp, timeout) KHÔNG phải
     counter-evidence — giả thuyết thành UNTESTED, KHÔNG bị bác bỏ.
  2. Nhiều nguyên nhân có thể ĐỒNG THỜI (disk full → process crash). Giữ cả
     confirmed lẫn untested; bác bỏ cái khác KHÔNG chứng minh cái còn lại là root
     (catalog có thể chưa đầy đủ).
  3. Confidence dựa trên POSITIVE evidence + COVERAGE (đã kiểm được bao nhiêu),
     KHÔNG chỉ đếm survivor.

Core KHÔNG biết domain: chỉ ``Hypothesis`` + ``probe``. Domain ở capability_*.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable

from aoip.objects import Finding, Hypothesis


class ProbeOutcome(str, Enum):
    PRESENT = "present"          # evidence của giả thuyết CÓ MẶT
    ABSENT = "absent"            # đã kiểm, KHÔNG có (counter-evidence)
    UNAVAILABLE = "unavailable"  # KHÔNG kiểm được (không áp dụng/thiếu quyền/lỗi)


Probe = Callable[[], "ProbeOutcome | Awaitable[ProbeOutcome]"]
Candidate = tuple[Hypothesis, Probe]


async def _maybe_await(value):
    if hasattr(value, "__await__"):
        return await value
    return value


@dataclass(frozen=True)
class DiagnosisResult:
    """Derived (không persist): nguyên nhân + bị bác bỏ + chưa kiểm được + tự tin."""

    findings: tuple[Finding, ...]          # confirmed (PRESENT) — có thể nhiều (đồng thời)
    rejected: tuple[str, ...]              # ABSENT (counter-evidence thật)
    untested: tuple[tuple[str, str], ...]  # (claim, reason) — UNAVAILABLE, KHÔNG bác bỏ
    confidence: float                      # Diagnosis Confidence (evidence × coverage)

    @property
    def top(self) -> Finding | None:
        return max(self.findings, key=lambda f: f.confidence) if self.findings else None


def _confidence(confirmed: list[Finding], coverage: float) -> float:
    """Tự tin = sức mạnh evidence dương × độ phủ kiểm tra, giảm nếu mơ hồ nhiều nguyên nhân.

    coverage thấp (nhiều UNAVAILABLE) → kém tự tin dù 'còn 1 survivor': bác bỏ/không-
    kiểm cái khác KHÔNG chứng minh cái còn lại (catalog có thể thiếu).
    """
    if not confirmed:
        return 0.1
    base = max(f.confidence for f in confirmed)
    cov_factor = 0.5 + 0.5 * coverage
    conf = base * cov_factor
    if len(confirmed) > 1:
        conf *= 0.8  # nhiều nguyên nhân sống → mơ hồ về root đơn
    return round(conf, 3)


async def diagnose(candidates: list[Candidate]) -> DiagnosisResult:
    """Chạy probe từng giả thuyết; phân loại 3 trạng thái → DiagnosisResult."""
    confirmed: list[Finding] = []
    rejected: list[str] = []
    untested: list[tuple[str, str]] = []

    for hyp, probe in candidates:
        outcome = await _maybe_await(probe())
        if outcome is ProbeOutcome.PRESENT:
            confirmed.append(Finding(
                claim=hyp.claim, references=(hyp.origin,), verdict=True,
                confidence=round(min(0.9, hyp.prior + 0.45), 3),
            ))
        elif outcome is ProbeOutcome.ABSENT:
            rejected.append(hyp.claim)
        else:  # UNAVAILABLE
            untested.append((hyp.claim, "probe unavailable (substrate/permission/timeout)"))

    tested = len(confirmed) + len(rejected)
    coverage = (tested / len(candidates)) if candidates else 0.0
    return DiagnosisResult(
        findings=tuple(confirmed), rejected=tuple(rejected),
        untested=tuple(untested), confidence=_confidence(confirmed, coverage),
    )
