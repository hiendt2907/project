"""trace_id contract for diagnostic evidence."""

from __future__ import annotations

from pkg.reasoning import coerce_evidence_dict


def test_coerce_evidence_always_has_trace_id() -> None:
    d = coerce_evidence_dict({"kind": "x", "probe": "redis_ping"})
    assert d.get("trace_id") == "evidence-unknown"
    d2 = coerce_evidence_dict({"trace_id": "  abc-123  ", "kind": "x"})
    assert d2.get("trace_id") == "abc-123"
