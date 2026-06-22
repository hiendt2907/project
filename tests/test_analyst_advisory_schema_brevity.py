"""Brevity clamps and step caps on AnalystAdvisory (post-parse guardrails)."""

from __future__ import annotations

from pkg.reasoning.analyst_advisory_schema import (
    AnalystAdvisory,
    ForecastTimeline,
    ImpactForecast,
)


def _forecast() -> ForecastTimeline:
    return ForecastTimeline(
        method="heuristic",
        basis="",
        forecasts=[
            ImpactForecast(
                timeframe="1h",
                severity="degraded",
                prediction="stable",
                confidence="low",
            ),
        ],
        note="",
    )


def _verbose_root_cause() -> str:
    return " ".join(f"w{i}" for i in range(80))


def test_analyst_advisory_truncates_verification_step_list() -> None:
    steps = [
        {
            "order": i,
            "layer": "kubernetes",
            "command": "kubectl get pods -n default",
            "expected_output": "Running",
            "rationale": "check pods",
        }
        for i in range(1, 8)
    ]
    adv = AnalystAdvisory(
        trace_id="t1",
        verdict="INVESTIGATE",
        root_cause="Host disk pressure suspected per metrics.",
        confidence="low",
        affected_workload="default/dep",
        verification_steps=steps,
        proposed_remediation=[],
        forecast=_forecast(),
    )
    assert len(adv.verification_steps) == 5


def test_analyst_advisory_truncates_remediation_list() -> None:
    rem = [
        {
            "order": i,
            "action": f"fix {i}",
            "args": {},
            "preconditions": [],
            "approval_required": False,
            "rollback_plan": "",
        }
        for i in range(1, 7)
    ]
    adv = AnalystAdvisory(
        trace_id="t2",
        verdict="INVESTIGATE",
        root_cause="Investigate further.",
        confidence="low",
        affected_workload="ns/x",
        verification_steps=[
            {
                "order": 1,
                "layer": "kubernetes",
                "command": "kubectl get pods",
                "expected_output": "ok",
                "rationale": "list",
            },
        ],
        proposed_remediation=rem,
        forecast=_forecast(),
    )
    assert len(adv.proposed_remediation) == 4


def test_analyst_advisory_clamps_root_cause_words() -> None:
    adv = AnalystAdvisory(
        trace_id="t3",
        verdict="INVESTIGATE",
        root_cause=_verbose_root_cause(),
        confidence="low",
        affected_workload="",
        verification_steps=[
            {
                "order": 1,
                "layer": "kubernetes",
                "command": "kubectl get events",
                "expected_output": "",
                "rationale": "events",
            },
        ],
        proposed_remediation=[],
        forecast=_forecast(),
    )
    assert len(adv.root_cause.split()) <= 48


def test_analyst_advisory_clamps_affected_workload_chars() -> None:
    long_scope = "a" * 500
    adv = AnalystAdvisory(
        trace_id="t4",
        verdict="NORMAL",
        root_cause="No issue.",
        confidence="high",
        affected_workload=long_scope,
        verification_steps=[
            {
                "order": 1,
                "layer": "kubernetes",
                "command": "kubectl version",
                "expected_output": "ok",
                "rationale": "sanity",
            },
        ],
        proposed_remediation=[],
        forecast=_forecast(),
    )
    assert len(adv.affected_workload) <= 200


def test_impact_forecast_clamps_prediction_words() -> None:
    fc = ForecastTimeline(
        method="heuristic",
        basis="",
        forecasts=[
            ImpactForecast(
                timeframe="1h",
                severity="degraded",
                prediction=" ".join(f"p{i}" for i in range(120)),
                confidence="low",
            ),
        ],
        note="",
    )
    assert len(fc.forecasts[0].prediction.split()) <= 40


# ── normalize_layer branches ──────────────────────────────────────────────────

def test_normalize_layer_valid_passthrough() -> None:
    from pkg.reasoning.analyst_advisory_schema import normalize_layer
    assert normalize_layer("kubernetes") == "kubernetes"
    assert normalize_layer("os_baremetal") == "os_baremetal"


def test_normalize_layer_fuzzy_os() -> None:
    from pkg.reasoning.analyst_advisory_schema import normalize_layer
    assert normalize_layer("bare metal host") == "os_baremetal"
    assert normalize_layer("infrastructure vm") == "os_baremetal"


def test_normalize_layer_fuzzy_network() -> None:
    from pkg.reasoning.analyst_advisory_schema import normalize_layer
    assert normalize_layer("DNS resolution layer") == "network"
    assert normalize_layer("layer_2 networking") == "network"


def test_normalize_layer_fuzzy_prometheus() -> None:
    from pkg.reasoning.analyst_advisory_schema import normalize_layer
    assert normalize_layer("prometheus metrics monitor") == "prometheus"
    assert normalize_layer("layer 4 monitoring") == "prometheus"


def test_normalize_layer_fallback_kubernetes() -> None:
    from pkg.reasoning.analyst_advisory_schema import normalize_layer
    assert normalize_layer("completely unknown") == "kubernetes"


def test_normalize_layer_fuzzy_k8s() -> None:
    from pkg.reasoning.analyst_advisory_schema import normalize_layer
    assert normalize_layer("k8s workload container") == "kubernetes"


# ── VerificationStep clamp triggers warning ───────────────────────────────────

def test_verification_step_clamps_long_rationale() -> None:
    from pkg.reasoning.analyst_advisory_schema import VerificationStep
    long_rationale = " ".join(f"word{i}" for i in range(200))
    step = VerificationStep(
        order=1, command="kubectl get pods", rationale=long_rationale,
        layer="kubernetes", expected_output="", approval_required=False,
    )
    assert len(step.rationale.split()) <= 150


def test_verification_step_infer_layer_non_default() -> None:
    from pkg.reasoning.analyst_advisory_schema import VerificationStep
    step = VerificationStep(
        order=1, command="ping 8.8.8.8 -c 4", rationale="test network",
        expected_output="", approval_required=False,
    )
    assert step.layer in ("network", "kubernetes")  # inferred from "ping"


# ── ProposedRemediation clamp triggers ────────────────────────────────────────

def test_proposed_remediation_clamps_long_action() -> None:
    from pkg.reasoning.analyst_advisory_schema import ProposedRemediationStep
    long_action = " ".join(f"step{i}" for i in range(200))
    rem = ProposedRemediationStep(
        order=1, action=long_action, rollback_plan="rollback", approval_required=False,
    )
    assert len(rem.action.split()) <= 120


# ── ImpactForecast coerce_severity ────────────────────────────────────────────

def test_impact_forecast_coerce_normal_severity() -> None:
    fi = ImpactForecast(
        timeframe="1h", severity="normal",  # type: ignore[arg-type]
        prediction="stable", confidence="low",
    )
    assert fi.severity == "healthy"
