"""AnalystAdvisory parsing edge cases (extra keys, placeholder timestamps)."""

from __future__ import annotations

from datetime import datetime

import pytest

from pkg.reasoning.analyst_advisory_schema import AnalystAdvisory


def _minimal_advisory_dict() -> dict:
    return {
        "trace_id": "t-1",
        "verdict": "NORMAL",
        "root_cause": "No issue",
        "confidence": "high",
        "verification_steps": [
            {
                "order": 1,
                "command": "kubectl get pods -n default",
                "rationale": "List workloads",
            }
        ],
        "proposed_remediation": [
            {
                "order": 1,
                "action": "noop",
            }
        ],
        "forecast": {
            "method": "heuristic",
            "forecasts": [
                {
                    "timeframe": "1h",
                    "severity": "healthy",
                    "prediction": "Stable",
                    "confidence": "high",
                }
            ],
        },
    }


def test_extra_top_level_key_ignored_minimal_advisory_validates() -> None:
    payload = _minimal_advisory_dict()
    payload["llm_noise_field"] = {"nested": True}
    adv = AnalystAdvisory.model_validate(payload)
    assert adv.trace_id == "t-1"
    assert not hasattr(adv, "llm_noise_field")


def test_placeholder_timestamp_string_dropped_uses_default() -> None:
    data = _minimal_advisory_dict()
    data["timestamp"] = "ISO8601"
    adv = AnalystAdvisory.model_validate(data)
    assert isinstance(adv.timestamp, datetime)
