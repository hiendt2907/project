"""Advisory golden dataset schema validation — no LLM required, always runs in CI.

Validates that every golden case in advisory_golden/ has the correct structure.
If schema is broken, advisory benchmark cannot run and quality cannot be measured.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

GOLDEN_DIR = Path(__file__).parent / "advisory_golden"

_VALID_VERDICTS = {"NORMAL", "INVESTIGATE", "URGENT", "CRITICAL"}
_VALID_LANES = {"SYS_RESOURCE", "SYS_HARD_FAIL", "APP_HTTP", "SIEM_SECURITY"}


def _load_cases() -> list[dict]:  # type: ignore[type-arg]
    cases = sorted(GOLDEN_DIR.glob("case_*.json"))
    assert cases, f"No golden cases found in {GOLDEN_DIR}"
    return [json.loads(p.read_text()) for p in cases]


@pytest.mark.parametrize("case", _load_cases(), ids=lambda c: c["id"])
def test_golden_case_required_fields(case: dict) -> None:  # type: ignore[type-arg]
    """Every golden case must have id, description, lane, evidence_text, expected."""
    assert "id" in case, "Missing field: id"
    assert "description" in case, f"[{case['id']}] Missing field: description"
    assert "lane" in case, f"[{case['id']}] Missing field: lane"
    assert "evidence_text" in case, f"[{case['id']}] Missing field: evidence_text"
    assert "expected" in case, f"[{case['id']}] Missing field: expected"


@pytest.mark.parametrize("case", _load_cases(), ids=lambda c: c["id"])
def test_golden_case_verdict_valid(case: dict) -> None:  # type: ignore[type-arg]
    """verdict must be one of NORMAL/INVESTIGATE/URGENT/CRITICAL."""
    verdict = case.get("expected", {}).get("verdict")
    assert verdict in _VALID_VERDICTS, (
        f"[{case['id']}] verdict={verdict!r} not in {_VALID_VERDICTS}"
    )


@pytest.mark.parametrize("case", _load_cases(), ids=lambda c: c["id"])
def test_golden_case_lane_valid(case: dict) -> None:  # type: ignore[type-arg]
    """lane must be one of the 4 diagnostic lanes."""
    lane = case.get("lane")
    assert lane in _VALID_LANES, (
        f"[{case['id']}] lane={lane!r} not in {_VALID_LANES}"
    )


@pytest.mark.parametrize("case", _load_cases(), ids=lambda c: c["id"])
def test_golden_case_keywords_are_list(case: dict) -> None:  # type: ignore[type-arg]
    """root_cause_contains and should_not_contain must be lists if present."""
    expected = case.get("expected", {})
    assert isinstance(expected.get("root_cause_contains", []), list), (
        f"[{case['id']}] root_cause_contains must be a list"
    )
    assert isinstance(expected.get("should_not_contain", []), list), (
        f"[{case['id']}] should_not_contain must be a list"
    )


@pytest.mark.parametrize("case", _load_cases(), ids=lambda c: c["id"])
def test_golden_case_numeric_fields(case: dict) -> None:  # type: ignore[type-arg]
    """min_verification_steps must be int >= 1; remediation_approval_required must be bool."""
    expected = case.get("expected", {})
    min_steps = expected.get("min_verification_steps", 1)
    assert isinstance(min_steps, int) and min_steps >= 1, (
        f"[{case['id']}] min_verification_steps={min_steps!r} must be int >= 1"
    )
    approval = expected.get("remediation_approval_required", False)
    assert isinstance(approval, bool), (
        f"[{case['id']}] remediation_approval_required={approval!r} must be bool"
    )


@pytest.mark.parametrize("case", _load_cases(), ids=lambda c: c["id"])
def test_golden_case_evidence_text_nonempty(case: dict) -> None:  # type: ignore[type-arg]
    """evidence_text must be a non-empty string."""
    ev = case.get("evidence_text", "")
    assert isinstance(ev, str) and len(ev.strip()) > 10, (
        f"[{case['id']}] evidence_text is empty or too short"
    )
