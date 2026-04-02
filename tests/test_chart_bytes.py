"""Chart chỉ sinh bytes RAM — không đụng filesystem."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from visualization.chart_bytes import line_chart_png_bytes


def test_png_magic_and_non_empty() -> None:
    png = line_chart_png_bytes([0, 1, 2], [10, 20, 5], title="Kiểm thử")
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(png) > 200


def test_mismatched_length_raises() -> None:
    with pytest.raises(ValueError, match="cùng độ dài"):
        line_chart_png_bytes([1, 2], [1], title="x")


def test_empty_series_raises() -> None:
    with pytest.raises(ValueError, match="ít nhất một điểm"):
        line_chart_png_bytes([], [], title="x")


def test_no_open_write_to_tmp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nếu ai gọi open('/tmp/...','wb') sẽ fail test."""
    real_open = open

    def guard_open(*args: object, **kwargs: object):
        path = args[0] if args else ""
        if isinstance(path, str) and "/tmp/" in path:
            raise AssertionError("cấm ghi /tmp trong chart_bytes")
        return real_open(*args, **kwargs)

    monkeypatch.setattr("builtins.open", guard_open)
    png = line_chart_png_bytes([1], [2], title="RAM only")
    assert len(png) > 100
