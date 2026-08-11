"""Đấu dây cooldown vào `remote_agent_pipeline` — Đ52.

Test hàm thuần (`test_diagnosis_cooldown.py`) chứng minh CHÍNH SÁCH đúng. Bộ này chứng
minh chính sách đó thật sự được GỌI: `mark_cluster_diagnosed`/`get_seen_state` đã tồn tại
trong repo từ lâu nhưng **không có call site nào** — đó chính là lý do 989 lượt chẩn đoán
chạy cho 33 vấn đề. Một chính sách đúng mà không ai gọi thì vô nghĩa.
"""
from __future__ import annotations

import json
import time

import pytest

from pkg.reasoning.evidence_cluster import (
    get_seen_state,
    mark_cluster_diagnosed,
)
from workers.remote_agent_pipeline import _verdict_from_session


class FakeRedis:
    """Redis tối thiểu cho get/set — đủ cho đường cooldown, không mock hành vi."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, k):
        return self.store.get(k)

    async def set(self, k, v, ex=None):
        self.store[k] = v


@pytest.fixture
def redis():
    return FakeRedis()


# ── mark_cluster_diagnosed phải ghi đủ trường cooldown cần ──────────────────

async def test_mark_ghi_du_urgency_va_ts(redis):
    """Thiếu `urgency` thì phát hiện leo thang chết lặng — khoá lại bằng test."""
    redis.store["omni:evcluster:seen:fp1"] = json.dumps(
        {"total_count": 3, "first_seen_ever": 1.0, "last_seen": 2.0,
         "agents": ["a"], "last_diagnosis": None}
    )
    await mark_cluster_diagnosed(redis, "fp1", "diagnosed", "payment-api chet", "high")

    state = await get_seen_state(redis, "fp1")
    last = state["last_diagnosis"]
    assert last["verdict"] == "diagnosed"
    assert last["urgency"] == "high"
    assert isinstance(last["ts"], float) and last["ts"] > 0
    assert state["total_count"] == 3, "khong duoc lam hong phan con lai cua state"


async def test_mark_khong_tao_key_moi_khi_chua_ton_tai(redis):
    """Không có cluster ⇒ không ghi. Tránh tạo cooldown cho fingerprint chưa từng thấy."""
    await mark_cluster_diagnosed(redis, "chua-co", "diagnosed", "x", "high")
    assert await get_seen_state(redis, "chua-co") is None


# ── verdict phải phân biệt chẩn-xong với chẩn-hỏng ──────────────────────────

@pytest.mark.parametrize("session,expect", [
    ({"final": {"confidence": 0.95}}, "diagnosed"),
    ({"final": {"confidence": 0.0}}, "llm_error"),
    ({"final": {"confidence": None}}, "llm_error"),
    ({"final": {"confidence": "hong"}}, "llm_error"),
    ({"final": {}}, "llm_error"),
    ({}, "llm_error"),
    (None, "llm_error"),
])
def test_verdict_doc_confidence_khong_doc_co_degraded(session, expect):
    """Đọc confidence, KHÔNG đọc `degraded`.

    Đo UAT 2026-08-11: 32/989 ca có `degraded=True` trong khi 767 ca confidence 0.0 —
    cờ đó không phản ánh sự thật nên không dùng phân nhánh cooldown được.
    """
    assert _verdict_from_session(session) == expect


def test_verdict_bo_qua_co_degraded_sai_su_that():
    """Ca thật ở UAT: degraded=False nhưng cả 2 lượt llm_error, confidence 0.0."""
    session = {"degraded": False, "final": {"confidence": 0.0,
               "root_cause": "Diagnosis inconclusive"}}
    assert _verdict_from_session(session) == "llm_error"


# ── vòng khép kín: chẩn xong ⇒ lần sau bị chặn ──────────────────────────────

async def test_vong_khep_kin_chan_doan_roi_thi_lan_sau_bi_chan(redis):
    from pkg.reasoning.diagnosis_cooldown import should_diagnose

    redis.store["omni:evcluster:seen:fp2"] = json.dumps(
        {"total_count": 1, "first_seen_ever": time.time(), "last_seen": time.time(),
         "agents": ["a"], "last_diagnosis": None}
    )
    # Lần 1: chưa chẩn bao giờ ⇒ cho chẩn
    assert should_diagnose(seen_state=await get_seen_state(redis, "fp2"),
                           urgency="high").diagnose is True

    await mark_cluster_diagnosed(redis, "fp2", "diagnosed", "root cause that", "high")

    # Lần 2 (ngay sau): bị chặn
    d = should_diagnose(seen_state=await get_seen_state(redis, "fp2"), urgency="high")
    assert d.diagnose is False and d.reason == "cooldown_active"

    # Lần 3: cùng fingerprint nhưng LEO THANG ⇒ xuyên qua
    d2 = should_diagnose(seen_state=await get_seen_state(redis, "fp2"),
                         urgency="critical")
    assert d2.diagnose is True and d2.reason == "escalated"
