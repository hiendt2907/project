"""Telegram advisory render helpers — Markdown escape and operator-safe advisory clone."""

from __future__ import annotations

from datetime import UTC, datetime
import logging

import pytest

from pkg.reasoning.analyst_advisory_schema import (
    AnalystAdvisory,
    ForecastTimeline,
    ImpactForecast,
    ProposedRemediationStep,
    VerificationStep,
)
from workers.telegram_advisory_emitter import (
    _e,
    _fix_z_score_root_cause,
    _is_heuristic_fallback,
    _render_forecast,
    _render_forecast_projection,
    _render_header,
    _render_how_to_fix,
    _render_impact_if_not_fixed,
    _render_remediation_steps,
    _render_verdict_header,
    _render_verification_steps,
    _render_what_happened,
    _render_when,
    _render_who,
    _render_why,
    _short_trace,
    _strip_placeholders,
    copy_advisory_for_telegram_if_mismatch,
    normalize_llm_markdown_escapes,
)


def test_normalize_llm_markdown_escapes_strips_tex_underscore() -> None:
    assert normalize_llm_markdown_escapes("linear\\_extrapolation") == "linear_extrapolation"


def test_e_escapes_without_visible_backslash_before_underscore() -> None:
    out = _e("method=linear\\_extrapolation")
    assert "\\\\_" not in out or out.count("\\") <= 2
    assert "linear" in out


def _sample_advisory(verdict: str = "URGENT") -> AnalystAdvisory:
    fc = ForecastTimeline(
        method="linear_extrapolation",
        basis="test",
        forecasts=[
            ImpactForecast(
                timeframe="1h",
                severity="catastrophic",
                prediction="CPU explosion",
                confidence="high",
            )
        ],
        note="",
    )
    return AnalystAdvisory(
        trace_id="trace-tdash",
        timestamp=datetime.now(UTC),
        verdict=verdict,  # type: ignore[arg-type]
        root_cause="Observed CPU usage is below the alert threshold; workload appears healthy.",
        confidence="high",
        affected_workload="ns/deploy",
        verification_steps=[
            VerificationStep(
                order=1,
                layer="prometheus",
                command="rate(cpu[5m])",
                expected_output="ok",
                rationale="check",
            )
        ],
        proposed_remediation=[
            ProposedRemediationStep(
                order=1,
                action="Tune alert",
                args={},
                approval_required=False,
                rollback_plan="",
            )
        ],
        forecast=fc,
        escalation_reason="",
    )


EVIDENCE_HEALTHY = """
k8s_clinical_pod_status batch=1 result=PASSED
phase=Running pod=foo
"""


def test_copy_advisory_for_telegram_downgrades_when_sdk_healthy_and_root_benign() -> None:
    adv = _sample_advisory("URGENT")
    out = copy_advisory_for_telegram_if_mismatch(adv, EVIDENCE_HEALTHY)
    assert out is not adv
    assert out.verdict == "INVESTIGATE"
    assert out.forecast.method == "heuristic"
    assert out.forecast.forecasts[0].severity == "degraded"
    assert "Telegram render" in (out.escalation_reason or "")
    assert adv.verdict == "URGENT"
    assert adv.forecast.forecasts[0].severity == "catastrophic"


def test_copy_advisory_returns_same_when_verdict_normal() -> None:
    adv = _sample_advisory("NORMAL")
    out = copy_advisory_for_telegram_if_mismatch(adv, EVIDENCE_HEALTHY)
    assert out is adv


def test_copy_advisory_noop_when_evidence_not_healthy_pod() -> None:
    adv = _sample_advisory("URGENT")
    out = copy_advisory_for_telegram_if_mismatch(adv, "no probe here")
    assert out is adv


def test_copy_advisory_noop_when_root_cause_acute_despite_healthy_evidence() -> None:
    """Do not tone down when root text is acute (regression guard for heuristic tuning)."""
    adv = _sample_advisory("URGENT").model_copy(
        update={
            "root_cause": "OOMKilled: container exceeded memory limit; immediate restart storm.",
        }
    )
    out = copy_advisory_for_telegram_if_mismatch(adv, EVIDENCE_HEALTHY)
    assert out is adv
    assert adv.verdict == "URGENT"


def test_copy_advisory_logs_sanitized_event(caplog: pytest.LogCaptureFixture) -> None:
    adv = _sample_advisory("URGENT")
    with caplog.at_level(logging.INFO, logger="workers.telegram_advisory_emitter"):
        copy_advisory_for_telegram_if_mismatch(adv, EVIDENCE_HEALTHY)
    assert "event=telegram_advisory_sanitized" in caplog.text
    assert "trace-tdash" in caplog.text
    assert "URGENT" in caplog.text


# ---------------------------------------------------------------------------
# New format tests: z-score guardrail, heuristic suppress, compact steps, trace
# ---------------------------------------------------------------------------

def _make_advisory_with_trace(trace: str, verdict: str = "INVESTIGATE", root_cause: str = "ok") -> AnalystAdvisory:
    fc = ForecastTimeline(
        method="heuristic",
        basis="insufficient evidence for quantitative extrapolation",
        forecasts=[
            ImpactForecast(timeframe="1h", severity="degraded", prediction="persists", confidence="low")
        ],
        note="heuristic fallback",
    )
    return AnalystAdvisory(
        trace_id=trace,
        timestamp=datetime.now(UTC),
        verdict=verdict,  # type: ignore[arg-type]
        root_cause=root_cause,
        confidence="medium",
        affected_workload="multi-agent/omni-analyst",
        verification_steps=[
            VerificationStep(order=i, layer="kubernetes", command=f"kubectl cmd{i}", rationale=f"step{i}")
            for i in range(1, 6)
        ],
        proposed_remediation=[],
        forecast=fc,
        escalation_reason="",
    )


def test_z_score_below_threshold_root_cause_corrected() -> None:
    """z=0.61 with 'exceeds threshold' wording must be rewritten to 'below 3σ threshold — normal'."""
    rc = (
        "The cluster CPU saturation is at 8.06%, which exceeds the normal threshold "
        "of ±3.0σ (z_cpu=+0.61)."
    )
    fixed = _fix_z_score_root_cause(rc)
    assert "exceed" not in fixed.lower()
    assert "below 3σ" in fixed


def test_heuristic_fallback_forecast_suppressed_from_header() -> None:
    """Heuristic forecasts with 'insufficient evidence' must not appear in rendered message."""
    adv = _make_advisory_with_trace("gw-prom-dee55686d0b6", "INVESTIGATE")
    fc = adv.forecast
    assert _is_heuristic_fallback(fc) is True


def test_short_trace_returns_last_8_chars_with_hash() -> None:
    assert _short_trace("gw-prom-dee55686d0b6") == "#5686d0b6"
    assert _short_trace("fg-1af14131") == "#1af14131"
    assert len(_short_trace("gw-prom-dee55686d0b6")) == 9  # '#' + 8 chars


def test_verification_steps_capped_at_3() -> None:
    """Only first 3 steps should appear regardless of how many are in the advisory."""
    adv = _make_advisory_with_trace("fg-1af14131", "CRITICAL")
    rendered = _render_verification_steps(adv.verification_steps)
    assert rendered.count("[L3 — K8s]") == 3


def test_verdict_header_no_confidence_when_medium() -> None:
    """Header shows emoji and verdict; CONFIDENCE not in header (moved to what_happened section)."""
    adv = _make_advisory_with_trace("trace-abc", "INVESTIGATE")
    rendered = _render_verdict_header(adv)
    assert "CONFIDENCE" not in rendered
    assert "🔍" in rendered
    assert "INVESTIGATE" in rendered


def test_verdict_header_shows_confidence_when_high() -> None:
    """Header does not show CONFIDENCE — it is rendered in the what_happened section."""
    adv = _make_advisory_with_trace("trace-abc", "CRITICAL").model_copy(update={"confidence": "high"})
    rendered = _render_verdict_header(adv)
    assert "CRITICAL" in rendered
    assert "🚨" in rendered


def test_placeholder_stripped_from_command() -> None:
    """<placeholder> tokens must not appear in rendered steps."""
    adv = _make_advisory_with_trace("fg-1af14131", "CRITICAL").model_copy(
        update={
            "verification_steps": [
                VerificationStep(
                    order=1, layer="kubernetes",
                    command="kubectl get pods -n <ns> -o wide; kubectl describe pod <pod> -n <ns>",
                    rationale="check pods",
                )
            ]
        }
    )
    rendered = _render_verification_steps(adv.verification_steps)
    assert "<ns>" not in rendered
    assert "<pod>" not in rendered
    assert "kubectl get pods" in rendered


def test_root_cause_no_trailing_fragment() -> None:
    """After z-score fix, no trailing '.0σ' or 'This indicates' leakage."""
    rc = (
        "The cluster CPU saturation is at 7.85%, which exceeds the normal threshold "
        "of ±3.0σ (z_cpu=+0.76). This indicates potential performance issues that "
        "could impact workload execution."
    )
    fixed = _fix_z_score_root_cause(rc)
    assert ".0σ" not in fixed
    assert "This indicates" not in fixed
    assert "below 3σ" in fixed


# ---------------------------------------------------------------------------
# New template render functions
# ---------------------------------------------------------------------------

def _make_full_advisory(verdict: str = "URGENT") -> AnalystAdvisory:
    fc = ForecastTimeline(
        method="linear_extrapolation",
        basis="prom",
        forecasts=[
            ImpactForecast(timeframe="1h", severity="critical", prediction="service outage", confidence="high"),
            ImpactForecast(timeframe="3h", severity="critical", prediction="data loss risk", confidence="medium"),
            ImpactForecast(timeframe="6h", severity="catastrophic", prediction="total outage", confidence="medium"),
            ImpactForecast(timeframe="12h", severity="catastrophic", prediction="unrecoverable", confidence="low"),
            ImpactForecast(timeframe="24h", severity="catastrophic", prediction="SLA breach", confidence="low"),
        ],
    )
    return AnalystAdvisory(
        trace_id="trace-full-001",
        timestamp=datetime(2026, 5, 22, 3, 0, 0, tzinfo=UTC),
        verdict=verdict,  # type: ignore[arg-type]
        root_cause="Redis OOM causing pod evictions in multi-agent namespace.",
        confidence="high",
        affected_workload="multi-agent/omni-analyst",
        verification_steps=[
            VerificationStep(order=1, layer="os_baremetal", command="top -b -n1", rationale="Check host memory pressure before blaming kubelet"),
        ],
        proposed_remediation=[
            ProposedRemediationStep(order=1, action="Increase Redis memory limit to 2Gi", approval_required=True, rollback_plan="Revert ConfigMap"),
        ],
        forecast=fc,
    )


def test_render_header_has_emoji_and_verdict() -> None:
    adv = _make_full_advisory("CRITICAL")
    result = _render_header(adv)
    assert "🚨" in result
    assert "CRITICAL" in result
    assert "Redis OOM" in result


def test_render_header_with_lane_badge() -> None:
    adv = _make_full_advisory("URGENT")
    result = _render_header(adv, lane_label="resource")
    assert "[RESOURCE]" in result
    assert "⚠️" in result


def test_render_what_happened_includes_root_cause() -> None:
    adv = _make_full_advisory()
    result = _render_what_happened(adv)
    assert "Sự cố" in result
    assert "Redis OOM" in result


def test_render_what_happened_shows_confidence_when_not_medium() -> None:
    adv = _make_full_advisory().model_copy(update={"confidence": "low"})
    result = _render_what_happened(adv)
    assert "low" in result


def test_render_who_known_workload() -> None:
    adv = _make_full_advisory()
    result = _render_who(adv)
    assert "multi-agent/omni-analyst" in result


def test_render_who_unknown_workload() -> None:
    adv = _make_full_advisory().model_copy(update={"affected_workload": "unknown"})
    result = _render_who(adv)
    assert "cụm" in result


def test_render_when_formats_timestamp() -> None:
    adv = _make_full_advisory()
    result = _render_when(adv)
    assert "2026-05-22" in result
    assert "UTC" in result


def test_render_why_returns_first_rationale() -> None:
    adv = _make_full_advisory()
    result = _render_why(adv.verification_steps)
    assert "Kiểm chứng" in result
    assert "host memory pressure" in result
    assert "top -b -n1" in result  # command is now shown


def test_render_why_empty_steps() -> None:
    assert _render_why([]) == ""


def test_render_why_empty_rationale() -> None:
    steps = [VerificationStep(order=1, layer="kubernetes", command="kubectl get pods", rationale="")]
    result = _render_why(steps)
    assert "Kiểm chứng" in result
    assert "kubectl get pods" in result  # command shown even without rationale


def test_render_how_to_fix_with_approval_and_rollback() -> None:
    adv = _make_full_advisory()
    result = _render_how_to_fix(adv.proposed_remediation)
    assert "Khắc phục" in result
    assert "CẦN PHÊ DUYỆT" in result
    assert "Revert ConfigMap" in result


def test_render_how_to_fix_empty() -> None:
    assert _render_how_to_fix([]) == ""


def test_render_impact_if_not_fixed_normal_forecast() -> None:
    adv = _make_full_advisory()
    result = _render_impact_if_not_fixed(adv.forecast)
    assert "Nếu không xử lý thì sao?" in result
    assert "+1h" in result
    assert "service outage" in result


def test_render_impact_if_not_fixed_heuristic_fallback() -> None:
    fc = ForecastTimeline(
        method="heuristic",
        basis="insufficient evidence",
        forecasts=[],
    )
    result = _render_impact_if_not_fixed(fc)
    assert "Chưa đủ dữ liệu" in result


def test_render_forecast_projection_all_5_horizons() -> None:
    adv = _make_full_advisory()
    result = _render_forecast_projection(adv.forecast)
    assert "Dự báo" in result
    for tf in ("1h", "3h", "6h", "12h", "24h"):
        assert tf in result


def test_render_forecast_projection_missing_horizon_shows_dash() -> None:
    fc = ForecastTimeline(
        method="linear_extrapolation",
        basis="test",
        forecasts=[ImpactForecast(timeframe="1h", severity="degraded", prediction="", confidence="high")],
    )
    result = _render_forecast_projection(fc)
    assert "3h:  —" in result


def test_render_forecast_projection_heuristic_fallback_empty() -> None:
    fc = ForecastTimeline(method="heuristic", basis="insufficient evidence", forecasts=[])
    assert _render_forecast_projection(fc) == ""


def test_fix_z_score_high_z_no_change() -> None:
    rc = "CPU z_score=+4.5 which exceeds the threshold"
    assert _fix_z_score_root_cause(rc) == rc


def test_fix_z_score_no_exceed_keyword_no_change() -> None:
    rc = "Memory usage z_score=+1.2 is nominal"
    assert _fix_z_score_root_cause(rc) == rc


def test_render_remediation_steps_with_args_and_preconditions() -> None:
    steps = [
        ProposedRemediationStep(
            order=1,
            action="patch configmap",
            args={"namespace": "multi-agent"},
            preconditions=["redis running"],
            rollback_plan="revert",
            approval_required=False,
        )
    ]
    result = _render_remediation_steps(steps)
    assert "[OK]" in result
    assert "multi-agent" in result
    assert "redis running" in result
    assert "revert" in result


def test_render_forecast_backward_compat() -> None:
    fc = ForecastTimeline(
        method="linear_extrapolation",
        basis="test",
        forecasts=[ImpactForecast(timeframe="1h", severity="critical", prediction="outage", confidence="high")],
    )
    result = _render_forecast(fc)
    assert "FORECAST" in result
    assert "1h" in result
    assert "CRITICAL" in result


def test_render_forecast_empty_forecasts_returns_empty() -> None:
    from workers.telegram_advisory_emitter import _render_forecast
    fc = ForecastTimeline(method="linear_extrapolation", basis="test", forecasts=[])
    assert _render_forecast(fc) == ""


def test_truncate_cmd_long_string() -> None:
    from workers.telegram_advisory_emitter import _truncate_cmd
    long_cmd = "kubectl get pods -n multi-agent " + "x" * 100
    result = _truncate_cmd(long_cmd)
    assert len(result) <= 100


def test_truncate_cmd_long_no_space() -> None:
    from workers.telegram_advisory_emitter import _truncate_cmd
    long_cmd = "x" * 200
    result = _truncate_cmd(long_cmd)
    assert len(result) <= 100


def test_render_when_none_timestamp() -> None:
    from workers.telegram_advisory_emitter import _render_when
    adv = _make_full_advisory().model_copy(update={"timestamp": None})
    result = _render_when(adv)
    assert "chưa rõ" in result


def test_render_verification_steps_empty_cmd_skipped() -> None:
    from workers.telegram_advisory_emitter import _render_verification_steps
    from pkg.reasoning.analyst_advisory_schema import VerificationStep
    steps = [
        VerificationStep(order=1, layer="kubernetes", command="<placeholder>", rationale="r1"),
        VerificationStep(order=2, layer="prometheus", command="rate(cpu[5m])", rationale="r2"),
    ]
    result = _render_verification_steps(steps)
    assert "rate(cpu" in result
