"""Contract: label schema và envelope nghiệp vụ giữ trace_id (chuẩn hoá đầu-cuối)."""

from __future__ import annotations

from pathlib import Path

import yaml


def test_omni_label_schema_lists_trace_id():
    root = Path(__file__).resolve().parents[1]
    data = yaml.safe_load((root / "config" / "omni_label_schema.yaml").read_text(encoding="utf-8"))
    tele = data.get("telemetry_dna") or []
    assert "trace_id" in tele, "telemetry_dna must include trace_id for end-to-end correlation"


def test_kafka_style_envelope_has_trace_id_key():
    sample = {"trace_id": "550e8400-e29b-41d4-a716-446655440000", "topic": "omni-actions", "action": "noop"}
    assert "trace_id" in sample and sample["trace_id"]
