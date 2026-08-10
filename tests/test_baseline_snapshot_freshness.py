"""Ngưỡng tươi của baseline snapshot phải SUY RA từ chu kỳ sync, không phải hằng số.

Bối cảnh (đo thật 2026-08-09 trên GCP): ngưỡng từng là số 300 gõ tay ở hai chỗ trong
`evidence_consumer`, hoàn toàn độc lập với `baseline_snapshot_interval_sec`. Chu kỳ
trên GCP là 600s ⇒ theo đúng thiết kế thì snapshot "quá hạn" trong NỬA mỗi chu kỳ.
Khi cổng 3σ còn fail-OPEN thì vô hại; sau khi vá cho fail-closed thật, nửa đó thành
mất advisory thật — trace `proact-s3fresh-1786283080`: "quá hạn (388s > 300s)".

Bài học đóng lại ở đây: một ngưỡng và cái chu kỳ nó phụ thuộc vào KHÔNG được là hai
con số cấu hình rời nhau, vì chúng sẽ trôi khỏi nhau mà không ai thấy.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

from workers.baseline_snapshot import snapshot_freshness_budget_sec


def test_budget_covers_more_than_one_sync_cycle() -> None:
    """Cấu hình GCP thật: 600s. Ngưỡng phải lớn hơn hẳn một chu kỳ."""
    budget = snapshot_freshness_budget_sec(SimpleNamespace(baseline_snapshot_interval_sec=600))
    assert budget > 600, "ngưỡng nhỏ hơn chu kỳ = snapshot luôn quá hạn một phần chu kỳ"
    assert budget == 1500.0


def test_missing_two_cycles_in_a_row_is_stale() -> None:
    """Lỡ một nhịp sync là nhiễu; lỡ hai nhịp là vòng sync hỏng thật."""
    interval = 600
    budget = snapshot_freshness_budget_sec(SimpleNamespace(baseline_snapshot_interval_sec=interval))
    assert budget > interval * 2
    assert budget < interval * 3


def test_short_interval_does_not_produce_an_absurdly_tight_budget() -> None:
    budget = snapshot_freshness_budget_sec(SimpleNamespace(baseline_snapshot_interval_sec=60))
    assert budget == 300.0


def test_settings_without_the_field_falls_back_not_crashes() -> None:
    assert snapshot_freshness_budget_sec(SimpleNamespace()) == 750.0


def test_evidence_consumer_no_longer_hardcodes_the_threshold() -> None:
    """Chặn việc gõ lại hằng số. Cả hai chỗ kiểm tuổi phải gọi helper."""
    src = open("src/workers/evidence_consumer.py", encoding="utf-8").read()
    body = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert body.count("snapshot_freshness_budget_sec(ctx.settings)") == 2
    assert not re.search(r"_(adv_)?snap_age\s*>\s*300\b", body)
