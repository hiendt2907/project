"""Omni-actions SUGGEST_REMEDIATION contract and executor log preview."""

from __future__ import annotations

from workers.kafka_actions_consumer import _omni_actions_body_preview
from workers.omni_actions_remediation import ACTION_SUGGEST_REMEDIATION, build_suggest_remediation_body


def test_build_suggest_remediation_body_shape() -> None:
    b = build_suggest_remediation_body(
        "tr-1",
        diagnosis="Readiness probe failed: context deadline.",
        confidence=0.6844,
        source="RAG_HIT",
        suggested_tool="kubectl_describe_pod",
    )
    assert b["action"] == ACTION_SUGGEST_REMEDIATION
    assert b["trace_id"] == "tr-1"
    assert b["data"]["source"] == "RAG_HIT"
    assert b["data"]["confidence"] == 0.6844
    assert "Readiness" in b["data"]["diagnosis"]


def test_executor_body_preview_english() -> None:
    inner = build_suggest_remediation_body(
        "tr-2",
        diagnosis="Probe timeout on port 9121.",
        confidence=0.9,
        source="RAG_HIT",
        suggested_tool="kubectl_describe_pod",
    )
    prev = _omni_actions_body_preview(inner)
    assert prev.startswith("Diagnosis:")
    assert "9121" in prev
    assert "Suggested tool:" in prev
