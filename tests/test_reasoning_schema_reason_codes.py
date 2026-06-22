"""Coverage for pkg.reasoning.schema and reason_codes edge branches."""

from __future__ import annotations

from pkg.reasoning import reason_codes
from pkg.reasoning.schema import coerce_evidence_dict


def test_reason_severity_unknown_defaults_info() -> None:
    assert reason_codes.reason_severity("UNKNOWN_CODE_XYZ") == "info"
    assert reason_codes.reason_severity("") == "info"


def test_coerce_evidence_dict_non_dict() -> None:
    out = coerce_evidence_dict("not a dict")
    assert out["kind"] == "invalid"
    assert out["trace_id"] == "evidence-unknown"


def test_coerce_evidence_dict_extracted_fact_json_and_trace_fallback() -> None:
    out = coerce_evidence_dict(
        {
            "extracted_fact": {"a": 1},
            "kind": "k",
        }
    )
    assert '"a": 1' in out["extracted_fact"] or '"a": 1' in str(out["extracted_fact"])


def test_coerce_evidence_dict_extracted_fact_scalar() -> None:
    out = coerce_evidence_dict({"extracted_fact": 42})
    assert out["extracted_fact"] == "42"


def test_coerce_evidence_dict_trace_id_blank_falls_back() -> None:
    out = coerce_evidence_dict({"trace_id": "   "})
    assert out["trace_id"] == "evidence-unknown"
