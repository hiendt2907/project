"""P1 #7 — remote_agent_pipeline phải dùng REMOTE_Z_THRESHOLD, không phải literal 3.0.

Bối cảnh (audit 2026-08-10, docs/audit/BACKEND_AUDIT_PLAN_2026-08-10.md #7):
`remote_host_baseline.REMOTE_Z_THRESHOLD` đã tồn tại làm nguồn chuẩn (re-export
`three_sigma.DEFAULT_THRESHOLD`, comment tường minh "không phát minh số mới cho remote
host"), nhưng `remote_agent_pipeline.py` gõ lại `3.0` làm literal độc lập ở nhánh quyết
định một mẫu remote-host có được đưa vào pipeline chẩn đoán đầy đủ hay bị park làm
"healthy sample". Hôm nay 2 giá trị trùng nhau nên không lỗi hiện, nhưng sẽ trôi khỏi
nhau âm thầm nếu REMOTE_Z_THRESHOLD được tune sau này mà literal ở đây bị bỏ quên.
"""

from __future__ import annotations

import re


def test_remote_agent_pipeline_imports_remote_z_threshold() -> None:
    import workers.remote_agent_pipeline as rap
    from anomaly.remote_host_baseline import REMOTE_Z_THRESHOLD

    assert rap.REMOTE_Z_THRESHOLD == REMOTE_Z_THRESHOLD == 3.0


def test_remote_agent_pipeline_source_does_not_hardcode_the_old_literal() -> None:
    """Chặn regression: không được gõ lại 3.0 làm ngưỡng anomaly độc lập."""
    src = open("src/workers/remote_agent_pipeline.py", encoding="utf-8").read()
    body = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("#"))
    assert not re.search(r"abs\(v\)\s*>\s*3\.0\b", body)
    assert "REMOTE_Z_THRESHOLD" in body


def test_is_anomalous_decision_uses_remote_z_threshold_value() -> None:
    """z ngay tại/dưới REMOTE_Z_THRESHOLD không anomalous; ngay trên thì có."""
    from anomaly.remote_host_baseline import REMOTE_Z_THRESHOLD

    zscores_below = {"z_cpu": REMOTE_Z_THRESHOLD - 0.01}
    zscores_above = {"z_cpu": REMOTE_Z_THRESHOLD + 0.01}

    assert not any(abs(v) > REMOTE_Z_THRESHOLD for v in zscores_below.values())
    assert any(abs(v) > REMOTE_Z_THRESHOLD for v in zscores_above.values())
