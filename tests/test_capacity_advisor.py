"""G4/S5 — đề xuất scale TRƯỚC khi vỡ, dựa trên xu hướng chứ không phải ngưỡng tĩnh.

Nguồn dữ liệu thật: `3sigma:remote:{tenant}:{host}:{metric}` (Redis list, 60 mẫu
trong lab hôm nay). Module này thuần tuý — không đọc Redis, không gọi executor, không
tự scale. Nó chỉ ĐỀ XUẤT; mutation vẫn phải đi qua executor + tier gate.
"""

from __future__ import annotations

import pytest

from pkg.reasoning.capacity_advisor import (
    ACTION_HOLD,
    ACTION_INSUFFICIENT_DATA,
    ACTION_INVESTIGATE_LEAK,
    ACTION_SCALE_DOWN,
    ACTION_SCALE_UP,
    analyze_capacity,
)


def _flat(value=50.0, n=60):
    return [value] * n


def _rising(start=40.0, step=0.5, n=60):
    return [start + i * step for i in range(n)]


def _falling(start=70.0, step=-0.5, n=60):
    return [start + i * step for i in range(n)]


def test_insufficient_samples_refuses_to_guess():
    r = analyze_capacity(samples=[50.0, 51.0], metric="mem", host="h1", tenant_id="t")

    assert r.action == ACTION_INSUFFICIENT_DATA
    assert r.days_to_threshold is None


def test_flat_usage_holds():
    r = analyze_capacity(samples=_flat(50.0), metric="mem", host="h1", tenant_id="t")

    assert r.action == ACTION_HOLD


def test_rising_trend_recommends_scale_up():
    # cpu, không phải mem: chuỗi tăng đơn điệu của mem cố ý rẽ sang nhánh nghi rò rỉ.
    r = analyze_capacity(samples=_rising(), metric="cpu", host="h1", tenant_id="t")

    assert r.action == ACTION_SCALE_UP
    assert r.slope_per_sample > 0


def test_rising_trend_estimates_time_to_threshold():
    """Giá trị của dự báo nằm ở 'còn bao lâu', không phải 'đang cao'."""
    r = analyze_capacity(
        samples=_rising(start=40.0, step=0.5), metric="mem", host="h1",
        tenant_id="t", threshold=90.0, sample_interval_sec=3600,
    )

    assert r.days_to_threshold is not None
    assert r.days_to_threshold > 0


def test_already_above_threshold_is_urgent_with_zero_days():
    r = analyze_capacity(
        samples=_flat(95.0), metric="disk", host="h1", tenant_id="t", threshold=90.0
    )

    assert r.action == ACTION_SCALE_UP
    assert r.days_to_threshold == 0.0
    assert r.urgent is True


def test_sustained_low_usage_recommends_scale_down():
    r = analyze_capacity(samples=_flat(8.0), metric="cpu", host="h1", tenant_id="t")

    assert r.action == ACTION_SCALE_DOWN


def test_falling_trend_does_not_recommend_scale_up():
    r = analyze_capacity(samples=_falling(), metric="mem", host="h1", tenant_id="t")

    assert r.action != ACTION_SCALE_UP


def test_monotonic_memory_growth_flagged_as_leak_not_capacity():
    """Bộ nhớ chỉ tăng, không bao giờ giảm → nghi rò rỉ; thêm RAM là chữa triệu chứng."""
    r = analyze_capacity(
        samples=_rising(start=30.0, step=1.0), metric="mem", host="h1", tenant_id="t",
    )

    assert r.action in (ACTION_SCALE_UP, ACTION_INVESTIGATE_LEAK)
    assert r.action == ACTION_INVESTIGATE_LEAK


def test_cpu_monotonic_growth_is_not_treated_as_leak():
    """Chỉ mem mới có ngữ nghĩa rò rỉ; CPU tăng đều là nhu cầu thật."""
    r = analyze_capacity(
        samples=_rising(start=30.0, step=1.0), metric="cpu", host="h1", tenant_id="t",
    )

    assert r.action == ACTION_SCALE_UP


def test_evidence_carries_identity_for_multi_tenant_report():
    r = analyze_capacity(samples=_flat(50.0), metric="mem", host="db-1", tenant_id="acme")

    assert r.tenant_id == "acme"
    assert r.host == "db-1"
    assert r.metric == "mem"


def test_result_never_contains_an_execution_instruction():
    """Chốt an toàn: advisory là văn bản đề xuất, không phải lệnh chạy được."""
    r = analyze_capacity(samples=_rising(), metric="cpu", host="h1", tenant_id="t")

    assert not hasattr(r, "tool")
    assert not hasattr(r, "args")
    assert r.auto_execute is False


def test_noisy_flat_data_is_not_mistaken_for_a_trend():
    samples = [50.0, 52.0, 48.0, 51.0, 49.0] * 12

    r = analyze_capacity(samples=samples, metric="cpu", host="h1", tenant_id="t")

    assert r.action == ACTION_HOLD


def test_non_numeric_samples_are_ignored_not_fatal():
    samples = ["50.0", "bad", None, 51.0] + _flat(50.0, 56)

    r = analyze_capacity(samples=samples, metric="mem", host="h1", tenant_id="t")

    assert r.action != ACTION_INSUFFICIENT_DATA


def test_summary_is_human_readable_vietnamese():
    r = analyze_capacity(samples=_rising(), metric="mem", host="h1", tenant_id="t")

    assert r.summary
    assert any(w in r.summary.lower() for w in ("tăng", "giảm", "ổn định", "rò rỉ"))
