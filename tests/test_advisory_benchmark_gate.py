"""Gate chống thụt lùi của advisory benchmark.

Bối cảnh (Đ74, 2026-08-17): target `benchmark-advisory` từng không đo được gì vì
BA khiếm khuyết chồng nhau — `|| true` nuốt kết quả, pytest test dùng LLM giả, và
mã thoát đòi 23/23 case hoàn hảo nên lần chạy tốt nhất vẫn thoát 1. Bộ test này
khoá hành vi gate mới lại để không tái diễn.
"""
from __future__ import annotations

import json

import pytest

from tests.benchmarks.run_advisory_benchmark import BASELINE_PATH, _check_gate

BASE = {"avg_score": 73.6, "avg_score_tolerance": 5.0, "max_no_advisory": 0}


@pytest.fixture
def baseline(tmp_path, monkeypatch):
    """Trỏ gate sang baseline tạm để test không phụ thuộc file thật."""
    def _write(**overrides):
        data = {**BASE, **overrides}
        p = tmp_path / "baseline.json"
        p.write_text(json.dumps(data))
        monkeypatch.setattr(
            "tests.benchmarks.run_advisory_benchmark.BASELINE_PATH", p,
        )
        return p
    return _write


def _report(avg_score: float, no_advisory: int = 0) -> dict:
    return {"avg_score": avg_score, "no_advisory_count": no_advisory}


class TestBaselineFileItself:
    """File baseline thật phải hợp lệ — nó là hợp đồng của gate."""

    def test_exists_and_parses(self) -> None:
        data = json.loads(BASELINE_PATH.read_text())
        for key in ("avg_score", "avg_score_tolerance", "max_no_advisory", "model"):
            assert key in data, f"baseline thiếu khoá {key}"

    def test_records_production_params_not_harness_defaults(self) -> None:
        # Default của harness là 512/4096; baseline PHẢI là giá trị production,
        # nếu không phép đo không mô tả thứ đang chạy thật (bài học Đ74).
        data = json.loads(BASELINE_PATH.read_text())
        assert data["num_predict"] == 1024
        assert data["num_ctx"] == 8192

    def test_no_advisory_is_zero_tolerance(self) -> None:
        # Case không sinh ra advisory = lỗi hạ tầng/cấu hình, không phải nhiễu
        # chất lượng. Không được nới.
        assert json.loads(BASELINE_PATH.read_text())["max_no_advisory"] == 0


class TestGatePasses:
    def test_exact_baseline(self, baseline) -> None:
        baseline()
        assert _check_gate(_report(73.6)) == (True, [])

    def test_improvement(self, baseline) -> None:
        baseline()
        assert _check_gate(_report(85.0))[0] is True

    def test_within_tolerance_band(self, baseline) -> None:
        # Biến động run-to-run có thật; 3 điểm dưới baseline vẫn chấp nhận.
        baseline()
        assert _check_gate(_report(70.6))[0] is True

    def test_missing_baseline_file_does_not_block(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(
            "tests.benchmarks.run_advisory_benchmark.BASELINE_PATH",
            tmp_path / "khong-ton-tai.json",
        )
        assert _check_gate(_report(1.0)) == (True, [])


class TestGateBlocks:
    def test_score_below_tolerance_floor(self, baseline) -> None:
        baseline()
        ok, reasons = _check_gate(_report(60.0))
        assert ok is False
        assert any("avg_score" in r for r in reasons)

    def test_any_no_advisory_blocks(self, baseline) -> None:
        """Đúng lớp lỗi đã im lặng hơn một tháng: điểm vẫn ổn nhưng có case
        không sinh ra advisory (JSON bị cắt ở num_predict)."""
        baseline()
        ok, reasons = _check_gate(_report(73.6, no_advisory=1))
        assert ok is False
        assert any("advisory" in r for r in reasons)
        # Thông báo phải chỉ đường tới log truncation, không chỉ báo lỗi suông.
        assert any("truncated" in r for r in reasons)

    def test_the_actual_nim_regression_would_have_been_caught(self, baseline) -> None:
        """Tái hiện số thật của lần chạy hỏng 2026-08-17: avg 14.1, 19 case
        không sinh advisory. Gate phải đỏ vì CẢ HAI lý do."""
        baseline()
        ok, reasons = _check_gate(_report(14.1, no_advisory=19))
        assert ok is False
        assert len(reasons) == 2

    def test_both_reasons_reported_not_just_first(self, baseline) -> None:
        baseline()
        _, reasons = _check_gate(_report(10.0, no_advisory=5))
        assert len(reasons) == 2
