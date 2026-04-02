"""visualization chart với confidence interval."""

from __future__ import annotations

from visualization.chart_bytes import line_chart_history_forecast_ci_png_bytes


def test_line_chart_history_forecast_ci_png_bytes() -> None:
    hist_x = list(range(10))
    hist_y = [float(i) * 0.1 for i in range(10)]
    fore_x = list(range(10, 14))
    fore_yhat = [1.0, 1.1, 1.2, 1.3]
    lo = [0.9, 1.0, 1.1, 1.2]
    hi = [1.1, 1.2, 1.3, 1.4]
    png = line_chart_history_forecast_ci_png_bytes(
        hist_x,
        hist_y,
        fore_x,
        fore_yhat,
        lo,
        hi,
        title="Test CI",
    )
    assert len(png) > 500
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
