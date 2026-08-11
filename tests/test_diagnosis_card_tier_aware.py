"""Đ55 — thẻ chẩn đoán Telegram phải nói ĐÚNG quyết định tier×risk thật cho
đề xuất remediation, thay vì câu chung chung "CÓ THỂ tự thực hiện" (Đ53) áp
dụng cho mọi tenant/mọi tier như nhau. Dùng đúng `gate_decision_for_tool()`
mà gateway áp cho dispatch thật (`_enforce_tier_gate`), không suy đoán riêng.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fakeredis.aioredis import FakeRedis

from workers.remote_diagnosis_emitter import (
    _resolve_tier_info,
    render_diagnosis_session,
)


def _ctx(redis) -> SimpleNamespace:
    return SimpleNamespace(
        redis=redis, admin_repo=None,
        settings=SimpleNamespace(omni_autonomy_tier="", omni_auto_execute_enabled=True),
    )


def _session(capability="systemd.restart_unit", unit="payment-api.service") -> dict:
    return {
        "trace_id": "trace-1", "agent_id": "agent-1",
        "final": {
            "root_cause": "payment-api inactive", "confidence": 0.9,
            "affected_components": ["payment-api"],
            "suggested_recovery": {"capability": capability, "unit": unit},
        },
        "turns": [],
    }


class TestResolveTierInfo:
    async def test_no_suggested_recovery_returns_none(self):
        ctx = _ctx(FakeRedis(decode_responses=True))
        session = {"trace_id": "t1", "final": {"suggested_recovery": None}}
        assert await _resolve_tier_info(ctx, session, "acme") is None

    async def test_auto_tier_low_risk_resolves_allow(self):
        redis = FakeRedis(decode_responses=True)
        await redis.set("omni:cfg:tier:acme", "auto")
        ctx = _ctx(redis)
        info = await _resolve_tier_info(ctx, _session(), "acme")
        assert info == {"tier": "auto", "decision": "ALLOW", "risk_class": "LOW"}

    async def test_shadow_tier_resolves_suggest(self):
        redis = FakeRedis(decode_responses=True)
        await redis.set("omni:cfg:tier:acme", "shadow")
        ctx = _ctx(redis)
        info = await _resolve_tier_info(ctx, _session(), "acme")
        assert info == {"tier": "shadow", "decision": "SUGGEST", "risk_class": "LOW"}

    async def test_missing_redis_and_settings_fails_safe_to_shadow(self):
        """resolve_tier() tự fail-closed về shadow khi thiếu redis/settings — không
        raise, không trả tier lạc quan. _resolve_tier_info không cần bọc thêm gì
        cho ca này, chỉ cần không văng lỗi làm mất luôn cả thẻ Telegram."""
        ctx = SimpleNamespace(redis=None, admin_repo=None, settings=None)
        info = await _resolve_tier_info(ctx, _session(), "acme")
        assert info == {"tier": "shadow", "decision": "SUGGEST", "risk_class": "LOW"}


class TestCardWordingPerTier:
    def test_allow_wording_states_auto_execute(self):
        text = render_diagnosis_session(
            _session(), tier_info={"tier": "auto", "decision": "ALLOW", "risk_class": "LOW"}
        )
        assert "AUTO" in text
        assert "TỰ THỰC HIỆN" in text
        assert "Omni không tự thực thi" not in text

    def test_shadow_wording_states_suggest_only(self):
        text = render_diagnosis_session(
            _session(), tier_info={"tier": "shadow", "decision": "SUGGEST", "risk_class": "LOW"}
        )
        assert "SHADOW" in text
        assert "CHỈ đề xuất" in text
        assert "TỰ THỰC HIỆN" not in text

    def test_hitl_wording_states_needs_approval(self):
        text = render_diagnosis_session(
            _session(), tier_info={"tier": "assist", "decision": "HITL", "risk_class": "MEDIUM"}
        )
        assert "ASSIST" in text
        assert "CẦN BẠN DUYỆT" in text

    def test_no_tier_info_falls_back_to_generic_wording(self):
        """Backward compat: caller không truyền tier_info (vd lỗi resolve) vẫn
        ra thẻ hợp lệ, dùng câu chung chung cũ — không được văng lỗi/thiếu section."""
        text = render_diagnosis_session(_session(), tier_info=None)
        assert "Bước rủi ro trung bình/cao luôn cần bạn duyệt" in text
