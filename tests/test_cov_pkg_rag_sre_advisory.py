"""Coverage: rag.gate helpers, sre_output, analyst_advisory_schema."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pkg.rag import gate as rag_gate
from pkg.reasoning import analyst_advisory_schema, sre_output


def test_clean_and_truncate_context() -> None:
    raw = "Status: 400\n" + "x" * 500 + "\nWarning: something happened\n"
    out = rag_gate.clean_and_truncate_context(raw, {"alertname": "CPUHigh"}, max_tokens=256)
    assert "alert_name=CPUHigh" in out or "CPUHigh" in out


def test_normalize_rag_query() -> None:
    hints = {
        "namespace": "ns",
        "pod_name": "p1",
        "service_name": "svc",
        "alertname": "A",
        "symptom_group": "cpu",
        "diagnostic_pattern": "dp",
    }
    q = rag_gate.normalize_rag_query("tail", hints)
    assert "namespace=ns" in q and "tail" in q


def test_rag_gate_outcome_dataclass() -> None:
    o = rag_gate.RagGateOutcome(hit=False, detail={"reason": "unit"})
    assert o.hit is False


@pytest.mark.parametrize(
    "text,max_words",
    [
        ("Hi there,\nhere is the diagnosis: CPU high", 20),
        ("", 10),
    ],
)
def test_compact_sre_diagnosis(text: str, max_words: int) -> None:
    out = sre_output.compact_sre_diagnosis(text, max_words=max_words)
    assert isinstance(out, str)


def test_strip_sre_fluff() -> None:
    assert "diagnosis" in sre_output.strip_sre_fluff("Hello,\ndiagnosis: bad pod").lower()


@pytest.mark.parametrize(
    "command,expected_layer",
    [
        ("kubectl get pods", "kubernetes"),
        ("rate(http_requests[5m])", "prometheus"),
        ("df -h", "os_baremetal"),
    ],
)
def test_verification_step_infer_layer(command: str, expected_layer: str) -> None:
    step = analyst_advisory_schema.VerificationStep(
        order=1,
        command=command,
        rationale="because",
    )
    assert step.layer == expected_layer
