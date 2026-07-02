"""Coverage for safe extracted_fact compaction in pkg.reasoning.schema.

Replaces raw-string slicing (which could cut mid-token and emit invalid
JSON — see docs/post-mortems/tr-leg-no-upsert.md) with structure-aware
shrinking that always round-trips through json.loads.
"""
from __future__ import annotations

import json

from pkg.reasoning.schema import EXTRACTED_FACT_BUDGET, coerce_evidence_dict


def test_payload_under_limit_is_not_truncated() -> None:
    ef = {"agent_id": "a1", "hostname": "h1", "discovery_data": {"services": ["nginx"]}}
    out = coerce_evidence_dict({"trace_id": "t1", "extracted_fact": ef})
    assert out["truncated"] is False
    assert "original_size" not in out
    assert json.loads(out["extracted_fact"]) == ef


def test_payload_at_exact_threshold_is_not_truncated() -> None:
    padding = "x" * (EXTRACTED_FACT_BUDGET - 40)
    ef = {"agent_id": "a1", "pad": padding}
    serialized = json.dumps(ef, ensure_ascii=False)
    assert len(serialized) <= EXTRACTED_FACT_BUDGET
    out = coerce_evidence_dict({"trace_id": "t1", "extracted_fact": ef})
    assert out["truncated"] is False
    assert json.loads(out["extracted_fact"]) == ef


def test_payload_far_over_limit_is_truncated_but_valid_json() -> None:
    ef = {
        "agent_id": "a1",
        "hostname": "h1",
        "discovery_data": {"process_list": [{"pid": i, "cmd": "x" * 500} for i in range(200)]},
    }
    out = coerce_evidence_dict({"trace_id": "t1", "extracted_fact": ef})
    assert out["truncated"] is True
    assert len(out["extracted_fact"]) <= EXTRACTED_FACT_BUDGET
    parsed = json.loads(out["extracted_fact"])  # must never raise
    assert isinstance(parsed, dict)
    assert out["original_size"] > EXTRACTED_FACT_BUDGET
    assert "content_hash" in out and len(out["content_hash"]) == 64


def test_unicode_payload_compacts_to_valid_json() -> None:
    ef = {"agent_id": "a1", "note": "xin chào việt nam " * 300}
    out = coerce_evidence_dict({"trace_id": "t1", "extracted_fact": ef})
    assert out["truncated"] is True
    parsed = json.loads(out["extracted_fact"])
    assert isinstance(parsed["note"], str)


def test_nested_payload_compacts_without_error() -> None:
    ef = {
        "agent_id": "a1",
        "discovery_data": {
            "services": [{"name": f"svc{i}", "ports": list(range(50)), "meta": {"env": "x" * 300}} for i in range(50)]
        },
    }
    out = coerce_evidence_dict({"trace_id": "t1", "extracted_fact": ef})
    assert out["truncated"] is True
    parsed = json.loads(out["extracted_fact"])
    assert isinstance(parsed, dict)


def test_malformed_optional_content_does_not_crash() -> None:
    ef = {"agent_id": "a1", "weird": None, "nested_none": {"x": None}}
    out = coerce_evidence_dict({"trace_id": "t1", "extracted_fact": ef})
    assert json.loads(out["extracted_fact"])["agent_id"] == "a1"


def test_identity_fields_always_present_even_when_truncated() -> None:
    ef = {
        "agent_id": "a1",
        "hostname": "h1",
        "discovery_data": {"process_list": [{"pid": i, "cmd": "x" * 500} for i in range(200)]},
    }
    out = coerce_evidence_dict({"trace_id": "t1", "extracted_fact": ef})
    assert out["agent_id"] == "a1"
    assert out["hostname"] == "h1"


def test_list_extracted_fact_truncation_still_valid_json() -> None:
    ef = [{"cmd": "x" * 500} for _ in range(50)]
    out = coerce_evidence_dict({"trace_id": "t1", "extracted_fact": ef})
    assert out["truncated"] is True
    parsed = json.loads(out["extracted_fact"])
    assert isinstance(parsed, list)


def test_schema_version_present_for_dict_and_list_extracted_fact() -> None:
    out = coerce_evidence_dict({"trace_id": "t1", "extracted_fact": {"a": 1}})
    assert out["schema_version"] == "1.0"
