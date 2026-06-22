"""Coverage: pkg.autonomy gigo, lifecycle, transform."""

from __future__ import annotations

import pytest

from pkg.autonomy import gigo, lifecycle, transform


@pytest.mark.parametrize(
    "labels,annotations,expect_keys",
    [
        (
            {"namespace": "ns", "pod_name": "p1", "alertname": "HighCPU", "severity": "warning"},
            {"summary": "s" * 600},
            {"namespace", "pod", "alertname", "error_code", "annotation_summary"},
        ),
        (
            {"alertname": "X", "reason": "OOMKilled"},
            None,
            {"error_code"},
        ),
    ],
)
def test_build_gigo_metadata(
    labels: dict[str, str],
    annotations: dict[str, str] | None,
    expect_keys: set[str],
) -> None:
    meta = gigo.build_gigo_metadata(labels, annotations)
    for k in expect_keys:
        assert k in meta


@pytest.mark.parametrize(
    "transition,phase",
    [
        (lifecycle.TRANSITION_INGESTED, lifecycle.AlertPhase.PENDING),
        (lifecycle.TRANSITION_CONTEXT_READY, lifecycle.AlertPhase.TRIAGE),
        (lifecycle.TRANSITION_PLAN_EMITTED, lifecycle.AlertPhase.ACTION),
        (lifecycle.TRANSITION_STATE_MACHINE_VERIFIED, lifecycle.AlertPhase.DONE),
        ("unknown", lifecycle.AlertPhase.PENDING),
    ],
)
def test_transition_to_alert_phase(transition: str, phase: lifecycle.AlertPhase) -> None:
    assert lifecycle.transition_to_alert_phase(transition) == phase


@pytest.mark.parametrize(
    "num_ctx,reserved,minimum",
    [
        (None, None, 512),
        (1024, 500, 512),
        (4096, 2200, 4096 * 2 - 2200),
    ],
)
def test_llm_evidence_char_budget(num_ctx: int | None, reserved: int | None, minimum: int) -> None:
    b = transform.llm_evidence_char_budget(num_ctx=num_ctx, reserved=reserved)
    assert b >= minimum


def test_clamp_evidence_text_truncates() -> None:
    long = "word " * 2000
    clipped = transform.clamp_evidence_text(long, max_chars=100)
    assert "truncated" in clipped.lower()
    short = transform.clamp_evidence_text("  ok  ", max_chars=500)
    assert short == "ok"
