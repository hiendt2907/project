"""Unit tests for the SIEM correlation-chain consumer (Phase 4)."""

from __future__ import annotations

import pytest

from services.analyst.chain_consumer import (
    CohesionResult,
    build_recall_query,
    compute_cohesion,
    heuristic_actions,
    member_signature,
    parse_chain_message,
    to_advisory,
)


def _chain() -> dict:
    return {
        "chain_id": "11111111-1111-1111-1111-111111111111",
        "tenant_id": "acme",
        "attack_category": "lateral_movement",
        "kill_chain_stage": "lateral_movement",
        "kill_chain_ordered": True,
        "confidence": 0.82,
        "signals": {"entity": 1.0, "sequence": 0.5, "volume": 0.5, "confidence": 0.82},
        "common_dimensions": [{"type": "ip", "value": "203.0.113.7"}],
        "member_events": [
            {"incident_id": "e1", "category": "auth_failure", "kill_chain_stage": "initial_access", "kill_chain_order": 2},
            {"incident_id": "e2", "category": "new_process", "kill_chain_stage": "execution", "kill_chain_order": 3},
        ],
        "schema_version": "1.0.0",
    }


def test_compute_cohesion_single_member_is_perfect():
    res = compute_cohesion([[1.0, 0.0]])
    assert res == CohesionResult(score=1.0, weak_indices=())


def test_compute_cohesion_all_aligned():
    res = compute_cohesion([[1.0, 0.0], [0.99, 0.01], [1.0, 0.02]])
    assert res.score == 1.0
    assert res.weak_indices == ()


def test_compute_cohesion_flags_outlier():
    # Two aligned vectors + one orthogonal outlier → outlier flagged weak.
    res = compute_cohesion([[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]], floor=0.7)
    assert 2 in res.weak_indices
    assert res.score == pytest.approx(2 / 3, abs=1e-3)


def test_member_signature_excludes_log_body():
    sig = member_signature({"category": "auth_failure", "source_ip": "1.2.3.4", "raw_log": "SECRET"})
    assert "auth_failure" in sig
    assert "1.2.3.4" in sig
    assert "SECRET" not in sig  # raw log body must never appear


def test_build_recall_query_contains_dimensions():
    q = build_recall_query(_chain())
    assert "lateral_movement" in q
    assert "ip=203.0.113.7" in q


def test_to_advisory_marks_weak_members():
    cohesion = CohesionResult(score=0.5, weak_indices=(1,))
    adv = to_advisory(_chain(), cohesion=cohesion, source="heuristic")
    assert adv.chain_id == "11111111-1111-1111-1111-111111111111"
    assert adv.tenant_id == "acme"
    assert adv.attack_category == "lateral_movement"
    assert adv.cohesion == 0.5
    assert adv.member_events[0].weak_member is False
    assert adv.member_events[1].weak_member is True
    assert len(adv.common_dimensions) == 1
    assert adv.recommended_actions  # non-empty


def test_heuristic_actions_fallback():
    assert heuristic_actions("unknown_category")
    assert heuristic_actions("lateral_movement") != heuristic_actions("impact")


def test_parse_chain_message_variants():
    d = {"chain_id": "x"}
    assert parse_chain_message(d) == d
    assert parse_chain_message(b'{"chain_id": "y"}') == {"chain_id": "y"}
    assert parse_chain_message("not json") is None
