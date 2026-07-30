"""Lô B (2026-07-31): thẻ Telegram hết rác — trace không trùng, không nhại
placeholder, không phát báo động khi chẩn đoán kết luận 'bình thường'."""

from __future__ import annotations

import pytest

from workers.remote_diagnosis_emitter import (
    _short_trace,
    diagnosis_has_real_finding,
    has_placeholder_parroting,
)


@pytest.mark.parametrize("trace_id,expected", [
    ("ra-f984c2c40010-cpu_percent", "#f984c2c4"),   # KHÔNG còn #_percent
    ("ra-abc123def456-cpu_percent", "#abc123de"),   # ca CPU khác ⇒ mã khác
    ("ra-d1b29771ee4c", "#d1b29771"),
    ("gw-prom-8b4e91913abc", "#8b4e9191"),
    ("", "#?"),
])
def test_short_trace_uses_hash_not_metric_suffix(trace_id: str, expected: str) -> None:
    assert _short_trace(trace_id) == expected


def test_two_cpu_traces_do_not_collide() -> None:
    """Bug gốc: mọi ca CPU đều ra #_percent. Nay phải khác nhau."""
    a = _short_trace("ra-aaaaaaaaaaaa-cpu_percent")
    b = _short_trace("ra-bbbbbbbbbbbb-cpu_percent")
    assert a != b


@pytest.mark.parametrize("rc", [
    "<copy from input>",
    "The unit <unit> is down",
    "restart <exact unit name copied verbatim>",
])
def test_placeholder_parroting_detected(rc: str) -> None:
    assert has_placeholder_parroting({"root_cause": rc}) is True


def test_clean_root_cause_not_flagged_placeholder() -> None:
    assert has_placeholder_parroting(
        {"root_cause": "nginx config error: upstream cust-app not found"}
    ) is False


@pytest.mark.parametrize("rc", [
    "The system is operating normally with no immediate issues.",
    "Hệ thống hoạt động bình thường",
    "No anomalies detected on the host",
    "",
])
def test_no_issue_conclusion_not_a_real_finding(rc: str) -> None:
    assert diagnosis_has_real_finding({"root_cause": rc}) is False


def test_real_incident_is_a_finding() -> None:
    assert diagnosis_has_real_finding(
        {"root_cause": "nginx down: upstream cust-app not found in config"}
    ) is True
