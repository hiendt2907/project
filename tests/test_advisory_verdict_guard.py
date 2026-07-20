"""TDD — deterministic verdict guard (SDK/METRIC CONSISTENCY chuyển từ prompt vào code).

Quy tắc gốc nằm trong system prompt nhưng bị clip khỏi vùng model nhìn thấy
(38k chars vs clip 10k) nên chưa bao giờ hiệu lực. Chuyển thành code: evidence
không có failure signal nào (FAILED/OOMKilled/CrashLoop/5xx/z>=3σ/SIEM/...) thì
verdict không được vượt INVESTIGATE và forecast không được critical/catastrophic.
"""

from __future__ import annotations

from pkg.reasoning.analyst_advisory_schema import AnalystAdvisory
from workers.advisory_verdict_guard import (
    apply_verdict_consistency_guard,
    evidence_has_failure_signal,
)


def _advisory(verdict: str = "CRITICAL", confidence: str = "high") -> AnalystAdvisory:
    return AnalystAdvisory(
        trace_id="t-1",
        verdict=verdict,
        root_cause="x",
        confidence=confidence,
        affected_workload="unknown",
        verification_steps=[],
        proposed_remediation=[],
        forecast={
            "method": "heuristic",
            "basis": "b",
            "forecasts": [
                {"timeframe": "6h", "severity": "catastrophic", "prediction": "p", "confidence": "high"},
                {"timeframe": "1h", "severity": "degraded", "prediction": "p", "confidence": "low"},
            ],
            "note": "",
        },
    )


HEALTHY_META_EVIDENCE = (
    "[ALERT_CONTEXT] error_hint: OmniBaselineMemZHigh abs(omni:mem:z) > 3\n"
    "[EVIDENCE] probe: node_cpu_saturation status: PASSED "
    "metrics_or_facts: {\"s0\": 0.064}\n"
    "=== 3-SIGMA RESOURCE BASELINE === z_cpu=+0.80 (normal) | z_mem=+1.10 (normal)"
)


# --------------------------------------------------------------------------- #
# evidence_has_failure_signal                                                  #
# --------------------------------------------------------------------------- #


def test_passed_only_evidence_has_no_failure_signal():
    assert evidence_has_failure_signal(HEALTHY_META_EVIDENCE) is False


def test_oomkilled_is_failure_signal():
    assert evidence_has_failure_signal("pod X OOMKilled restartCount=7") is True


def test_status_failed_is_failure_signal():
    assert evidence_has_failure_signal("probe: systemd_units status: FAILED") is True


def test_high_sigma_z_is_failure_signal():
    assert evidence_has_failure_signal("baseline z_mem=+4.20 (ANOMALY)") is True
    assert evidence_has_failure_signal("z_cpu=-3.5 breach") is True


def test_normal_z_is_not_failure_signal():
    assert evidence_has_failure_signal("z_cpu=+0.80 z_mem=+1.10") is False


def test_http_5xx_is_failure_signal():
    assert evidence_has_failure_signal("upstream returned HTTP 503 rate 12%") is True


def test_status_code_inside_ip_is_not_failure_signal():
    # "503" là substring của IP — word boundary phải chặn false positive
    assert evidence_has_failure_signal("peer 10.85.03.7 connected, all probes PASSED") is False


def test_siem_category_is_failure_signal():
    assert evidence_has_failure_signal("siem_category=malware severity=critical") is True


# --------------------------------------------------------------------------- #
# apply_verdict_consistency_guard                                              #
# --------------------------------------------------------------------------- #


def test_guard_downgrades_critical_without_failure_signal():
    adv = _advisory("CRITICAL", "high")
    gated, fired = apply_verdict_consistency_guard(adv, HEALTHY_META_EVIDENCE)
    assert fired is True
    assert gated.verdict == "INVESTIGATE"
    assert all(f.severity in ("healthy", "degraded") for f in gated.forecast.forecasts)
    assert adv.verdict == "CRITICAL"  # immutability


def test_guard_keeps_urgent_with_real_failure():
    adv = _advisory("URGENT")
    gated, fired = apply_verdict_consistency_guard(
        adv, "pod cart-api OOMKilled 3 times, exit code 137"
    )
    assert fired is False
    assert gated.verdict == "URGENT"
    assert gated.forecast.forecasts[0].severity == "catastrophic"


def test_guard_ignores_investigate_and_normal():
    for v in ("INVESTIGATE", "NORMAL"):
        adv = _advisory(v)
        gated, fired = apply_verdict_consistency_guard(adv, HEALTHY_META_EVIDENCE)
        assert fired is False
        assert gated.verdict == v
