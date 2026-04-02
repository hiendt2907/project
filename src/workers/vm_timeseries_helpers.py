"""Helper: chuỗi Prometheus → matplotlib line chart — dùng với query_vm_timeseries / query_prometheus_metrics."""

from __future__ import annotations

from visualization.chart_bytes import line_chart_png_bytes


def prometheus_timeseries_to_line_chart_png_bytes(
    timestamps_unix: list[float],
    values: list[float],
    *,
    title: str,
) -> bytes:
    """Vẽ line chart; trục X: giờ từ mốc đầu (tránh số unix lớn)."""
    if not values or len(timestamps_unix) != len(values):
        raise ValueError("timestamps và values phải cùng độ dài và không rỗng")
    t0 = timestamps_unix[0]
    x_hours = [(t - t0) / 3600.0 for t in timestamps_unix]
    return line_chart_png_bytes(x_hours, values, title=title, xlabel="t (giờ)", ylabel="value")


vm_timeseries_to_line_chart_png_bytes = prometheus_timeseries_to_line_chart_png_bytes
