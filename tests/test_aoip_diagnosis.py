"""Tests EPIC Operate (Diagnosis Engine) — multi-hypothesis + falsification.

Reviewer: "timeout" KHÔNG mặc định "DOWN". Agent sinh NHIỀU giả thuyết root-cause,
VERIFY từng cái bằng bác bỏ (INV_FALSIFICATION_FIRST: predicted_evidence vắng →
loại). Diagnosis Confidence quyết định Decision Confidence. Core engine domain-
AGNOSTIC (chỉ biết Hypothesis + probe); domain SRE planner sinh candidate.
"""
from __future__ import annotations

import pytest

from aoip.diagnosis import diagnose
from aoip.objects import Hypothesis


def _h(claim: str, prior: float = 0.4) -> Hypothesis:
    return Hypothesis(claim=claim, predicted_evidence=(f"evidence of {claim}",),
                      prior=prior, origin="DIAGNOSIS")


async def test_single_surviving_hypothesis_high_confidence():
    # Chỉ 'process_dead' có evidence → sống; còn lại bị bác bỏ.
    cands = [
        (_h("process_dead"), lambda: True),
        (_h("disk_full"), lambda: False),
        (_h("oom_kill"), lambda: False),
        (_h("network_partition"), lambda: False),
    ]
    result = await diagnose(cands)
    confirmed = {f.claim for f in result.findings}
    assert confirmed == {"process_dead"}
    assert set(result.rejected) == {"disk_full", "oom_kill", "network_partition"}
    assert result.confidence >= 0.8  # cô lập được đúng 1 nguyên nhân → tự tin


async def test_ambiguous_when_multiple_survive_lower_confidence():
    cands = [(_h("disk_full"), lambda: True), (_h("oom_kill"), lambda: True)]
    result = await diagnose(cands)
    assert len(result.findings) == 2
    assert result.confidence < 0.7  # mơ hồ → kém tự tin


async def test_no_hypothesis_survives_unknown_low_confidence():
    cands = [(_h("process_dead"), lambda: False), (_h("disk_full"), lambda: False)]
    result = await diagnose(cands)
    assert result.findings == ()
    assert result.confidence <= 0.2  # không biết → KHÔNG được hành động tự tin


async def test_async_probe_supported():
    async def aprobe():
        return True
    result = await diagnose([(_h("process_dead"), aprobe)])
    assert {f.claim for f in result.findings} == {"process_dead"}


async def test_sre_planner_is_domain_separate_from_core():
    # Core diagnosis KHÔNG nhúng tên domain (redis/cpu/disk...). Domain ở sre_diagnosis.
    import aoip.diagnosis as core
    src = open(core.__file__).read().lower()
    for domain_word in ("redis", "systemctl", "dmesg", "oom", "disk_full", "df ", "restart"):
        assert domain_word not in src


def test_sre_candidates_have_real_probes_and_evidence():
    from aoip.sre_diagnosis import sre_root_cause_candidates

    class FakeTransport:
        target = "h"
        async def run(self, argv, *, timeout=15.0):
            joined = " ".join(argv)
            if "is-active" in joined:
                return ("inactive\n", 0)   # process dead
            return ("", 0)

    cands = sre_root_cause_candidates("svc:redis", "h", FakeTransport(), port=6379)
    claims = {h.claim for h, _ in cands}
    # ít nhất các nhánh production hay gặp.
    assert any("process" in c for c in claims)
    assert any("disk" in c for c in claims)
    assert any("oom" in c.lower() for c in claims)
    assert any("network" in c for c in claims)
    assert all(h.predicted_evidence for h, _ in cands)  # mỗi giả thuyết có evidence dự đoán
