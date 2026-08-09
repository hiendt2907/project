"""MTTD phải đo được — `observe_kpi_mttd` từng là code chết (0 call site trong toàn repo).

Đo tại P1 (docs/audit/PROACTIVE_FREEZE_2026-08-09.md): histogram `omni_kpi_mttd_seconds`
chỉ có HELP/TYPE, không một series nào, nên mọi so sánh "phát hiện nhanh hơn" đều không
kiểm chứng được. Mốc "sự cố bắt đầu" (`startsAt` của Alertmanager) đã được nhận ở gateway
nhưng chưa bao giờ được giữ lại tới nơi tính được hiệu.
"""
from __future__ import annotations

import time

import pytest

from workers.alert_to_event import (
    GIGO_KEY_ALERT_STARTS_AT,
    build_anomaly_event_from_alert_payload,
    parse_alert_starts_at,
)


# ── parse_alert_starts_at ────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "raw,expected_ok",
    [
        ("2026-08-09T06:00:00Z", True),
        ("2026-08-09T06:00:00.123Z", True),
        ("2026-08-09T06:00:00+00:00", True),
        ("2026-08-09T13:00:00+07:00", True),
    ],
)
def test_parses_alertmanager_timestamps(raw, expected_ok):
    assert (parse_alert_starts_at(raw) is not None) is expected_ok


@pytest.mark.parametrize("raw", ["", None, "   ", "not-a-time", "2026-13-45T99:99:99Z"])
def test_invalid_returns_none_not_zero(raw):
    """None chứ không phải 0.0: 0.0 sẽ lặng lẽ thành MTTD ~57 năm."""
    assert parse_alert_starts_at(raw) is None


def test_alertmanager_zero_time_rejected():
    """`0001-01-01T00:00:00Z` là giá trị "chưa đặt" của Alertmanager, không phải mốc thật."""
    assert parse_alert_starts_at("0001-01-01T00:00:00Z") is None


def test_timezone_offsets_agree():
    """Cùng một thời điểm viết ở hai múi giờ phải ra cùng epoch."""
    assert parse_alert_starts_at("2026-08-09T06:00:00Z") == parse_alert_starts_at(
        "2026-08-09T13:00:00+07:00"
    )


# ── startsAt đi được tới AnomalyEvent ────────────────────────────────────────

def _payload(starts_at: str | None) -> dict:
    alert: dict = {
        "labels": {
            "alertname": "PodMemoryWorkingSetVsLimitHigh",
            "namespace": "multi-agent",
            "pod": "kafka-0",
            "severity": "warning",
        },
        "annotations": {"description": "working_set/limit > 90% for 5m"},
    }
    if starts_at is not None:
        alert["startsAt"] = starts_at
    return {
        "source": "prometheus",
        "trace_id": "gw-prom-testmttd01",
        "received_at": time.time(),
        "data": {"alerts": [alert]},
    }


def test_starts_at_carried_in_existing_gigo_dict():
    """Mang bằng `gigo_metadata` — dict ĐANG CÓ, không thêm trường/khoá/topic mới."""
    ev = build_anomaly_event_from_alert_payload(_payload("2026-08-09T06:00:00Z"))
    assert ev.gigo_metadata[GIGO_KEY_ALERT_STARTS_AT] == "2026-08-09T06:00:00Z"


def test_absent_starts_at_leaves_gigo_clean():
    ev = build_anomaly_event_from_alert_payload(_payload(None))
    assert GIGO_KEY_ALERT_STARTS_AT not in ev.gigo_metadata


def test_zero_time_not_carried():
    """Giá trị "chưa đặt" không được lọt vào, nếu không call site sẽ tính ra MTTD khổng lồ."""
    ev = build_anomaly_event_from_alert_payload(_payload("0001-01-01T00:00:00Z"))
    assert GIGO_KEY_ALERT_STARTS_AT not in ev.gigo_metadata


def test_other_gigo_fields_untouched():
    ev = build_anomaly_event_from_alert_payload(_payload("2026-08-09T06:00:00Z"))
    assert ev.gigo_metadata.get("namespace") == "multi-agent"
    assert ev.gigo_metadata.get("pod") == "kafka-0"


# ── histogram thật sự nhận được mẫu ──────────────────────────────────────────

def test_histogram_records_sample_under_domain_label():
    """Label là `domain`, không phải `lane` — trục lane đã gỡ khỏi tầng trace."""
    from prometheus_client import REGISTRY

    import workers.metrics_exporter as me

    me.observe_kpi_mttd("kubernetes", 42.0)
    count = REGISTRY.get_sample_value(
        "omni_kpi_mttd_seconds_count", {"domain": "kubernetes"}
    )
    assert count is not None and count >= 1.0


def test_empty_domain_falls_back_not_crash():
    import workers.metrics_exporter as me

    me.observe_kpi_mttd("", 1.0)  # không được ném
