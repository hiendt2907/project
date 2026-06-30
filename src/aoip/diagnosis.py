"""Diagnosis Engine (core) — multi-hypothesis + falsification, domain-AGNOSTIC.

Vì sao tồn tại: một triệu chứng (vd "timeout") KHÔNG đồng nghĩa một nguyên nhân.
Recovery chỉ đáng tin khi Diagnosis đáng tin — nếu không sẽ rơi vào anti-pattern
chữa-triệu-chứng lặp vô hạn. Engine sinh tin cậy bằng cách LOẠI giả thuyết
(INV_FALSIFICATION_FIRST): predicted_evidence vắng → bác bỏ; còn lại → Finding.

QUAN TRỌNG (giữ lõi tổng quát): engine này KHÔNG biết domain. Nó chỉ biết
``Hypothesis`` (có predicted_evidence) + một ``probe`` trả về "evidence có mặt
không". Tầng domain sinh candidate ở module riêng (``sre_diagnosis.py``) —
một discipline cụ thể chỉ là plugin trên runtime tổng quát này.

Diagnosis Confidence: cô lập đúng MỘT nguyên nhân → cao; nhiều cái sống → mơ hồ →
thấp; không cái nào sống → unknown → rất thấp (chặn hành động mù).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from aoip.objects import Finding, Hypothesis

# Probe domain-cung-cấp: trả True nếu predicted_evidence của giả thuyết CÓ MẶT.
Probe = Callable[[], "bool | Awaitable[bool]"]
Candidate = tuple[Hypothesis, Probe]


async def _maybe_await(value):
    if hasattr(value, "__await__"):
        return await value
    return value


@dataclass(frozen=True)
class DiagnosisResult:
    """Derived (không persist): nguyên nhân sống sót + bị bác bỏ + độ tự tin."""

    findings: tuple[Finding, ...]   # giả thuyết được chứng thực (root cause khả dĩ)
    rejected: tuple[str, ...]       # claim bị bác bỏ (predicted_evidence vắng)
    confidence: float               # Diagnosis Confidence

    @property
    def top(self) -> Finding | None:
        return max(self.findings, key=lambda f: f.confidence) if self.findings else None


def _confidence(confirmed: list[Finding]) -> float:
    if not confirmed:
        return 0.1                          # không biết → không được hành động tự tin
    if len(confirmed) == 1:
        return round(min(0.95, confirmed[0].confidence), 3)
    return 0.5                              # nhiều nguyên nhân sống → mơ hồ


async def diagnose(candidates: list[Candidate]) -> DiagnosisResult:
    """Chạy falsification trên từng giả thuyết → Finding sống sót + Diagnosis Confidence."""
    confirmed: list[Finding] = []
    rejected: list[str] = []
    for hyp, probe in candidates:
        present = await _maybe_await(probe())
        if present:
            confirmed.append(Finding(
                claim=hyp.claim,
                references=(hyp.origin,),
                verdict=True,
                confidence=round(min(0.95, hyp.prior + 0.45), 3),
            ))
        else:
            rejected.append(hyp.claim)   # predicted_evidence vắng → loại
    return DiagnosisResult(
        findings=tuple(confirmed), rejected=tuple(rejected), confidence=_confidence(confirmed),
    )
