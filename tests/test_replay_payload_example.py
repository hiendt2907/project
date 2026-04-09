"""Replay example JSON is valid Alertmanager-style webhook."""

from __future__ import annotations

import json
from pathlib import Path


def test_replay_example_minimal_json_loads() -> None:
    p = Path(__file__).resolve().parents[1] / "scripts" / "alert_payloads" / "replay" / "replay_example_minimal.json"
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw.get("status") == "firing"
    assert raw.get("alerts")
    assert raw["alerts"][0].get("labels", {}).get("alertname") == "ReplayExampleMinimal"


def test_artifact_template_json_loads() -> None:
    p = Path(__file__).resolve().parents[1] / "reports" / "alert-flow-realistic" / "artifact_template.json"
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw.get("schema") == "omni-alert-flow-realistic-run-v1"
