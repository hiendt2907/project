import pytest


def test_verified_outcome_payload_rejects_unknown_or_missing_proof():
    from execution.experience import build_verified_outcome_payload

    with pytest.raises(ValueError, match="PASS"):
        build_verified_outcome_payload(
            tenant_id="acme", trace_id="t1", action="restart", outcome="COMPLETED",
            verification={"status": "UNKNOWN", "evidence_refs": ["x"]},
        )

    with pytest.raises(ValueError, match="evidence_refs"):
        build_verified_outcome_payload(
            tenant_id="acme", trace_id="t1", action="restart", outcome="COMPLETED",
            verification={"status": "PASS", "evidence_refs": []},
        )


def test_verified_outcome_payload_is_tenant_scoped_and_auditable():
    from execution.experience import build_verified_outcome_payload

    payload = build_verified_outcome_payload(
        tenant_id="acme", trace_id="t1", action="restart", outcome="COMPLETED",
        verification={"status": "PASS", "evidence_refs": ["probe:after:1"], "confidence": 0.99},
    )

    assert payload["tenant_id"] == "acme"
    assert payload["verification_result"] == "PASS"
    assert payload["evidence_refs"] == ["probe:after:1"]
    assert payload["verified"] is True
