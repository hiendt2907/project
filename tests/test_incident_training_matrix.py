from __future__ import annotations

from pathlib import Path

import yaml

from pkg.reasoning.incident_matrix_profile import invalidate_matrix_cache


def test_incident_training_matrix_has_expected_unique_scenarios() -> None:
    path = Path("config/incident_training_matrix.yaml")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rows = data.get("scenarios") or []
    ids = [str((r or {}).get("id") or "").strip() for r in rows]
    ids = [x for x in ids if x]
    assert len(ids) >= 31
    assert len(set(ids)) == len(ids)


def test_incident_training_matrix_has_expected_contract_fields() -> None:
    invalidate_matrix_cache()
    path = Path("config/incident_training_matrix.yaml")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rows = data.get("scenarios") or []
    lanes = {"resource", "state", "app_log"}
    stages = {"sigma_verify", "state_confirm", "log_surge_verify"}
    for row in rows:
        assert row.get("id")
        assert row.get("group")
        assert row.get("runner")
        assert row.get("expected_channel") in {"EXECUTE_MUTATE", "SUGGEST_REMEDIATION"}
        assert row.get("proof_lane") in lanes
        assert row.get("expected_stage") in stages
        erc = row.get("expected_reason_codes")
        if erc is not None:
            assert isinstance(erc, list)


def test_nginx_waiting_fault_matrix_expects_invariant_reason() -> None:
    invalidate_matrix_cache()
    path = Path("config/incident_training_matrix.yaml")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rows = {str(r.get("id")): r for r in (data.get("scenarios") or []) if r.get("id")}
    row = rows.get("nginx_waiting_fault") or {}
    assert row.get("expected_channel") == "SUGGEST_REMEDIATION"
    assert "INV_NO_RESTART_ON_BROKEN_SPEC" in (row.get("expected_reason_codes") or [])
