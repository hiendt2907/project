"""Cổng 3σ của advisory phải FAIL-CLOSED khi mất nền so sánh.

Lỗi được sửa 2026-08-09: khi baseline snapshot quá hạn (>300s) hoặc chưa có, code
đặt `_adv_snap_raw = None` rồi rơi xuống `if _adv_snap_raw:` — khối chứa `return ""`
bị bỏ qua nên advisory VẪN CHẠY. Log ghi "fail closed" nhưng hành vi là fail-OPEN.

Đo tại P1 (docs/audit/PROACTIVE_FREEZE_2026-08-09.md): `advisory_sigma_stale` 14 lần
trên đúng 15 lần gọi LLM advisory — gần như mọi advisory đều sinh ra đúng lúc không
còn nền so sánh. Với lane `resource`, 3σ baseline LÀ sự thật nền; thiếu nó thì không
có cơ sở nói alert đúng hay sai, nên không được phép tiêu một lượt LLM.
"""
from __future__ import annotations

import inspect
import re

import workers.evidence_consumer as ec


def _gate_source(*, strip_comments: bool = False) -> str:
    """Nguồn của nhánh cổng 3σ trong `reason_from_diagnostic_evidence`.

    `strip_comments=True` bỏ dòng comment — cần khi khẳng định về CODE, vì comment
    trong vùng này cố ý trích lại đoạn code lỗi cũ để giải thích, và sẽ khớp nhầm.
    """
    src = inspect.getsource(ec.reason_from_diagnostic_evidence)
    start = src.index('_adv_lane == "resource"')
    body = src[start:]
    if strip_comments:
        body = "\n".join(l for l in body.splitlines() if not l.lstrip().startswith("#"))
    return body


def test_stale_snapshot_returns_instead_of_falling_through():
    """Nhánh mất nền so sánh phải KẾT THÚC trace, không được rơi xuống gọi LLM."""
    body = _gate_source(strip_comments=True)
    idx = body.index("if _adv_no_ground_truth:")
    after = body[idx:]
    # Giữa lúc phát hiện thiếu nền và lúc chạy advisory phải có một `return`.
    assert "return" in after, "nhánh thiếu nền so sánh không kết thúc trace"
    ret = after.index("return")
    advisory = after.index("run_advisory_analyst")
    assert ret < advisory, "fail-open: thiếu nền so sánh mà vẫn chạy tới advisory"


def test_missing_snapshot_also_blocks_not_only_stale():
    """Không chỉ 'quá hạn' — 'chưa có snapshot' cũng phải chặn."""
    body = _gate_source()
    assert "chưa có" in body, "thiếu nhánh snapshot vắng mặt"
    assert "quá hạn" in body, "thiếu nhánh snapshot quá hạn"


def test_no_silent_reset_of_snapshot_variable():
    """Không được quay lại thủ thuật `_adv_snap_raw = None` — đó chính là gốc lỗi."""
    body = _gate_source(strip_comments=True)
    assert not re.search(r"_adv_snap_raw\s*=\s*None", body), (
        "đặt _adv_snap_raw=None làm khối suppression bị bỏ qua ⇒ fail-open trở lại"
    )


def test_suppression_is_loud_not_silent():
    """Phải log WARNING và đánh dấu pipeline, nếu không sẽ thành im lặng ngược.

    Rủi ro của fail-closed là `baseline_snapshot_loop` chết thì mọi sự cố tài nguyên
    bị nuốt. Nó phải hiện ra trên dashboard chứ không dừng lặng ở EVIDENCE.
    """
    body = _gate_source()
    assert "logger.warning" in body
    assert "advisory_sigma_no_ground_truth" in body
    for stage in ("RAG", "LLM", "SCHEMA", "VERIFY", "KILLSWITCH", "CRAT", "DISPATCH"):
        assert f'"{stage}"' in body, f"stage {stage} không được đánh dấu ⇒ trace treo trên UI"
    assert "_mark_suggest_only_terminal" in body, "HITL/EXECUTOR/FEEDBACK sẽ treo pending"


def test_gate_only_applies_to_resource_lane():
    """Không được lan sang lane khác — chỉ lane resource mới lấy 3σ làm sự thật nền."""
    body = _gate_source()
    assert body.startswith('_adv_lane == "resource"')
