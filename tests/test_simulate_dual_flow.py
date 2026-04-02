"""Unit tests for scripts/simulate_dual_flow_15m.py (không cần cluster)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "simulate_dual_flow_15m",
    _ROOT / "scripts" / "simulate_dual_flow_15m.py",
)
sim = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(sim)


def test_alert_body_has_alerts_and_labels():
    b = sim._alert_body()
    assert b["status"] == "firing"
    assert "alerts" in b and len(b["alerts"]) >= 1
    assert "labels" in b["alerts"][0]


def test_anomaly_event_matches_minimal_schema():
    ev = sim._anomaly_event_payload()
    assert len(ev["trace_id"]) >= 4
    assert len(ev["canonical_query"]) >= 1
    assert "timestamp" in ev


def test_json_serialize_anomaly_roundtrip():
    ev = sim._anomaly_event_payload()
    assert json.loads(json.dumps(ev)) == ev
