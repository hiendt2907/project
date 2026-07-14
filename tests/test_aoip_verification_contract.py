import pytest


def test_verification_result_has_explicit_pass_fail_unknown_states():
    from aoip.verification import VerificationResult, VerificationStatus

    result = VerificationResult.pass_(
        expected_state="service.active",
        evidence_refs=("probe:systemctl:1",),
        checks={"service": True, "dependents": True},
        confidence=0.98,
    )

    assert result.status is VerificationStatus.PASS
    assert result.to_dict()["status"] == "PASS"
    assert result.to_dict()["evidence_refs"] == ["probe:systemctl:1"]


@pytest.mark.parametrize("factory", ["pass_", "fail"])
def test_verification_terminal_result_requires_evidence(factory):
    from aoip.verification import VerificationResult

    with pytest.raises(ValueError, match="evidence_refs"):
        getattr(VerificationResult, factory)(expected_state="service.active", evidence_refs=())


def test_unknown_verification_is_not_success():
    from aoip.verification import VerificationResult, VerificationStatus

    result = VerificationResult.unknown(
        expected_state="service.active", reason="agent disconnected", evidence_refs=("before:x",)
    )

    assert result.status is VerificationStatus.UNKNOWN
    assert result.is_success is False
    assert result.to_dict()["reason"] == "agent disconnected"
