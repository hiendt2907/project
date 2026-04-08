from __future__ import annotations

from pathlib import Path

import yaml


def test_incident_training_matrix_has_expected_unique_scenarios() -> None:
    path = Path("config/incident_training_matrix.yaml")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rows = data.get("scenarios") or []
    ids = [str((r or {}).get("id") or "").strip() for r in rows]
    ids = [x for x in ids if x]
    assert len(ids) >= 31
    assert len(set(ids)) == len(ids)


def test_incident_training_matrix_has_expected_contract_fields() -> None:
    path = Path("config/incident_training_matrix.yaml")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rows = data.get("scenarios") or []
    for row in rows:
        assert row.get("id")
        assert row.get("group")
        assert row.get("runner")
        assert row.get("expected_channel") in {"EXECUTE_MUTATE", "SUGGEST_REMEDIATION"}
