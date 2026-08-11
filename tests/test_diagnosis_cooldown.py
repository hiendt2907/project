"""Cooldown chẩn đoán theo fingerprint — Đ52.

Vì sao cần: một sự cố ĐANG DIỄN RA phát `result=FAILED` mỗi chu kỳ collect (20s).
`assess_domain_severity` Priority 1 nâng nó thành `high`, mà lưới chặn lặp duy nhất
(`remote_agent_pipeline.py`, `_NOTIFY_TIERS`) chỉ chặn khi mức độ THẤP — nên mọi sự cố
nghiêm trọng bị chẩn đoán lại từ đầu mỗi 20 giây, vô hạn.

Đo trên UAT thật 2026-08-11 (audit Đ51): 989 lượt chẩn đoán cho **33** vấn đề duy nhất
(lặp 96.7%), đẩy nhu cầu LLM lên 115% công suất ⇒ 74.3% lượt chết timeout ⇒ 91.9% tin
Telegram là lời khuyên chung chung vô giá trị.

`mark_cluster_diagnosed()` / `get_seen_state()` / trường `last_diagnosis` đã tồn tại sẵn
trong `pkg.reasoning.evidence_cluster` nhưng **không có call site nào** — hạ tầng cooldown
đã viết mà chưa bao giờ đấu dây. Bộ test này khoá đúng hành vi đó.
"""
from __future__ import annotations

import time

import pytest

from pkg.reasoning.diagnosis_cooldown import (
    COOLDOWN_S,
    RETRY_COOLDOWN_S,
    CooldownDecision,
    should_diagnose,
)


def _seen(*, last_diag_ts: float | None, verdict: str = "resolved",
          urgency: str = "high", total_count: int = 5) -> dict:
    """Bản ghi `omni:evcluster:seen:*` như hình dạng thật trong Redis."""
    state: dict = {
        "total_count": total_count,
        "first_seen_ever": time.time() - 3600,
        "last_seen": time.time(),
        "agents": ["loyalty-uat_cust-edge"],
        "last_diagnosis": None,
    }
    if last_diag_ts is not None:
        state["last_diagnosis"] = {
            "verdict": verdict,
            "root_cause": "payment-api không phục vụ /api/health",
            "ts": last_diag_ts,
            "urgency": urgency,
        }
    return state


# ── Hành vi cốt lõi: chặn lặp ────────────────────────────────────────────────

def test_lan_dau_tien_luon_chan_doan():
    """Chưa từng chẩn đoán ⇒ phải chẩn. Không được để cooldown bịt ca mới."""
    d = should_diagnose(seen_state=None, urgency="high")
    assert d.diagnose is True
    assert d.reason == "first_time"


def test_da_chan_doan_gan_day_thi_bo_qua():
    """Cùng fingerprint, vừa chẩn xong 60s trước ⇒ KHÔNG gọi lại LLM."""
    d = should_diagnose(seen_state=_seen(last_diag_ts=time.time() - 60), urgency="high")
    assert d.diagnose is False
    assert d.reason == "cooldown_active"
    assert d.cooldown_remaining_s > 0


def test_het_cooldown_thi_chan_doan_lai():
    """Sự cố vẫn còn sau khi hết hạn ⇒ phải chẩn lại, không im lặng vĩnh viễn."""
    d = should_diagnose(
        seen_state=_seen(last_diag_ts=time.time() - COOLDOWN_S - 1), urgency="high"
    )
    assert d.diagnose is True
    assert d.reason == "cooldown_expired"


# ── An toàn: leo thang phải xuyên qua được cooldown ──────────────────────────

def test_leo_thang_high_len_critical_xuyen_qua_cooldown():
    """Vấn đề xấu đi trong lúc cooldown ⇒ PHẢI chẩn lại ngay.

    Đây là bất biến an toàn: cooldown chỉ được phép nén tiếng ồn, tuyệt đối không
    được che một sự cố đang trở nặng.
    """
    d = should_diagnose(
        seen_state=_seen(last_diag_ts=time.time() - 10, urgency="high"),
        urgency="critical",
    )
    assert d.diagnose is True
    assert d.reason == "escalated"


def test_khong_leo_thang_khi_muc_do_giu_nguyen_hoac_giam():
    for urgency in ("high", "medium"):
        d = should_diagnose(
            seen_state=_seen(last_diag_ts=time.time() - 10, urgency="high"),
            urgency=urgency,
        )
        assert d.diagnose is False, urgency


def test_lan_truoc_that_bai_thi_thu_lai_som_hon_nhung_KHONG_ngay_lap_tuc():
    """Lượt trước LLM chết ⇒ thử lại sớm, nhưng vẫn phải có quãng nghỉ.

    Đây là bất biến chống-tự-hủy. Ở UAT 74.3% lượt chết vì LLM quá tải. Nếu 'lượt trước
    thất bại' được bỏ qua cooldown HOÀN TOÀN thì mọi ca hỏng sẽ thử lại sau đúng 20s ⇒
    tải không giảm ⇒ LLM vẫn quá tải ⇒ vẫn hỏng: cooldown thành vô tác dụng đúng vào lúc
    cần nó nhất. Nên dùng cooldown NGẮN (`RETRY_COOLDOWN_S`) thay vì không có.
    """
    just_failed = _seen(last_diag_ts=time.time() - 10, verdict="llm_error")
    d = should_diagnose(seen_state=just_failed, urgency="high")
    assert d.diagnose is False
    assert d.reason == "retry_cooldown_active"

    later = _seen(last_diag_ts=time.time() - RETRY_COOLDOWN_S - 1, verdict="llm_error")
    d2 = should_diagnose(seen_state=later, urgency="high")
    assert d2.diagnose is True
    assert d2.reason == "retry_after_failure"


def test_cooldown_that_bai_phai_ngan_hon_cooldown_thanh_cong():
    """Chẩn hỏng thì thử lại sớm hơn chẩn xong — nếu không, quan hệ này vô nghĩa."""
    assert 0 < RETRY_COOLDOWN_S < COOLDOWN_S


# ── Chống dữ liệu bẩn ────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [
    {"last_diagnosis": "khong-phai-dict"},
    {"last_diagnosis": {"verdict": "resolved"}},        # thiếu ts
    {"last_diagnosis": {"verdict": "resolved", "ts": "hong"}},
    {},
])
def test_ban_ghi_hong_thi_fail_open_chan_doan(bad):
    """State hỏng ⇒ chẩn đoán (fail-open). Thà tốn LLM còn hơn bịt sự cố thật."""
    assert should_diagnose(seen_state=bad, urgency="high").diagnose is True


def test_moc_thoi_gian_tuong_lai_khong_khoa_vinh_vien():
    """`ts` ở tương lai (lệch đồng hồ) không được tạo cooldown dài vô hạn."""
    d = should_diagnose(
        seen_state=_seen(last_diag_ts=time.time() + 86400), urgency="high"
    )
    assert d.diagnose is True
    assert d.reason == "clock_skew"


def test_decision_la_immutable():
    d = should_diagnose(seen_state=None, urgency="high")
    assert isinstance(d, CooldownDecision)
    with pytest.raises(Exception):
        d.diagnose = False  # type: ignore[misc]
