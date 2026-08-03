"""Step 2–5 — tier gate matrix, tier resolve, readiness, Telegram HITL callback.

Ref: MASTER_PLAN §3 (gate), §5 (readiness), §4 (HITL). No mocks: fakeredis +
FakeKafka. Bất biến: READONLY luôn ALLOW; HIGH luôn HITL; shadow → SUGGEST;
CRAT HITL_DECISION ghi TRƯỚC dispatch (fail-closed).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import fakeredis.aioredis
import pytest

from workers.advisory_mode_kill_switch import AdvisoryModeKillSwitch as KS
from workers.tier_gate import (
    ALLOW,
    HITL,
    SUGGEST,
    derive_tier_from_legacy,
    evaluate_tier_gate,
    gate_decision_for_tool,
    confidence_ceiling,
    is_trusted_origin,
    normalize_tier,
    resolve_tier,
    effective_tier,
)


def test_normalize_tier_mode_aliases():
    """SRE-Autonomous mode names map to canonical tiers; junk → None."""
    assert normalize_tier("minimal") == "assist"
    assert normalize_tier("autonomous") == "auto"
    assert normalize_tier("shadow") == "shadow"
    assert normalize_tier("OBSERVE") == "shadow"
    assert normalize_tier("assist") == "assist"
    assert normalize_tier("nonsense") is None
    assert normalize_tier("") is None
    assert normalize_tier(None) is None


def test_is_trusted_origin():
    assert is_trusted_origin("deterministic_safety_net_hoisted") is True
    assert is_trusted_origin("recall_strong") is True
    assert is_trusted_origin("chaos_lab_autofix_after_planner") is True
    assert is_trusted_origin("llm") is False
    assert is_trusted_origin("") is False
    assert is_trusted_origin(None) is False


def test_minimal_origin_guard_downgrades_llm_but_allows_trusted():
    """minimal(assist): LOW tool auto-runs ONLY from a trusted (RAG/deterministic)
    origin; raw LLM-ReAct origin is downgraded ALLOW→SUGGEST."""
    d_llm, risk = gate_decision_for_tool("k8s_rollout_restart", tier="assist", plan_origin="llm")
    assert d_llm == SUGGEST and risk == "LOW"
    d_det, _ = gate_decision_for_tool(
        "k8s_rollout_restart", tier="assist", plan_origin="deterministic_safety_net_hoisted"
    )
    assert d_det == ALLOW
    # No provenance supplied → tier-only matrix (backward compatible) → ALLOW.
    d_none, _ = gate_decision_for_tool("k8s_rollout_restart", tier="assist")
    assert d_none == ALLOW


def test_autonomous_allows_llm_origin_low_medium_but_hitl_high():
    """autonomous(auto): LLM-ReAct origin auto-runs for LOW+MEDIUM; HIGH → HITL."""
    assert gate_decision_for_tool("k8s_rollout_restart", tier="auto", plan_origin="llm")[0] == ALLOW
    assert gate_decision_for_tool("k8s_patch_configmap", tier="auto", plan_origin="llm")[0] == ALLOW  # MEDIUM
    assert gate_decision_for_tool("k8s_patch_secret", tier="auto", plan_origin="llm")[0] == HITL  # HIGH


def test_shadow_never_runs_regardless_of_origin():
    assert gate_decision_for_tool("k8s_rollout_restart", tier="shadow", plan_origin="deterministic")[0] == SUGGEST
    assert gate_decision_for_tool("k8s_rollout_restart", tier="shadow", plan_origin="llm")[0] == SUGGEST


class _FakeKafka:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def send_dict(self, topic: str, message: dict[str, Any], key: bytes | None = None) -> None:
        self.sent.append((topic, message))


@pytest.fixture
async def redis() -> Any:
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


# ── §3 ma trận tier×risk ──────────────────────────────────────────────────────
@pytest.mark.parametrize("tier,risk,expected", [
    ("shadow", "READONLY", ALLOW),
    ("shadow", "LOW", SUGGEST),
    ("shadow", "MEDIUM", SUGGEST),
    ("shadow", "HIGH", SUGGEST),
    ("assist", "READONLY", ALLOW),
    ("assist", "LOW", ALLOW),
    ("assist", "MEDIUM", HITL),
    ("assist", "HIGH", HITL),
    ("auto", "READONLY", ALLOW),
    ("auto", "LOW", ALLOW),
    ("auto", "MEDIUM", ALLOW),
    ("auto", "HIGH", HITL),
    ("garbage", "LOW", SUGGEST),  # tier lạ fail-closed
])
def test_tier_matrix(tier, risk, expected):
    assert evaluate_tier_gate(tier, risk) == expected


def test_high_always_hitl_every_tier():
    for t in ("assist", "auto"):
        assert evaluate_tier_gate(t, "HIGH") == HITL


def test_confidence_is_a_ceiling_for_tenant_autonomy():
    assert confidence_ceiling(0) == "shadow"
    assert confidence_ceiling(49) == "shadow"
    assert confidence_ceiling(50) == "assist"
    assert confidence_ceiling(74) == "assist"
    assert confidence_ceiling(75) == "auto"
    assert effective_tier("auto", 49) == "shadow"
    assert effective_tier("auto", 60) == "assist"
    assert effective_tier("auto", 90) == "auto"
    assert effective_tier("assist", 90) == "assist"
    assert effective_tier("unknown", 90) == "shadow"


def test_derive_tier_from_legacy():
    assert derive_tier_from_legacy(False) == "shadow"
    assert derive_tier_from_legacy(True) == "auto"


# ── §3 kill switch tích hợp (backward-compat + tier path) ─────────────────────
def test_killswitch_legacy_unchanged():
    # tier=None giữ nguyên hành vi cũ
    assert KS.validate_execution_gate("k8s_rollout_restart", {}, auto_execute_enabled=False)[0] is False
    assert KS.validate_execution_gate("k8s_delete_pod", {}, auto_execute_enabled=True)[0] is False
    assert KS.validate_execution_gate("k8s_rollout_restart", {}, auto_execute_enabled=True)[0] is True


def test_killswitch_tier_path():
    assert KS.validate_execution_gate("k8s_rollout_restart", {}, tier="assist")[0] is True
    ok, reason = KS.validate_execution_gate("k8s_scale_resource", {}, tier="assist")
    assert ok is False and "HITL" in reason
    ok, reason = KS.validate_execution_gate("k8s_delete_pod", {}, tier="auto")
    assert ok is False and "HITL" in reason
    ok, reason = KS.validate_execution_gate("k8s_rollout_restart", {}, tier="shadow")
    assert ok is False and "SUGGEST" in reason


def test_killswitch_tier_respects_override():
    # override nâng k8s_rollout_restart lên MEDIUM → assist phải HITL
    ok, reason = KS.validate_execution_gate(
        "k8s_rollout_restart", {}, tier="assist", risk_override="MEDIUM"
    )
    assert ok is False and "HITL" in reason


# ── §3 resolve_tier: cache → DB → env derive ─────────────────────────────────
async def test_resolve_tier_env_derive(redis):
    s = SimpleNamespace(omni_autonomy_tier="", omni_auto_execute_enabled=False)
    assert await resolve_tier(settings=s, redis=redis) == "shadow"
    s2 = SimpleNamespace(omni_autonomy_tier="", omni_auto_execute_enabled=True)
    r2 = fakeredis.aioredis.FakeRedis(decode_responses=True)
    assert await resolve_tier(settings=s2, redis=r2) == "auto"
    await r2.aclose()


async def test_resolve_tier_env_explicit(redis):
    s = SimpleNamespace(omni_autonomy_tier="assist", omni_auto_execute_enabled=False)
    assert await resolve_tier(settings=s, redis=redis) == "assist"


async def test_resolve_tier_cache_hit(redis):
    await redis.set("omni:cfg:tier:default", "auto")
    s = SimpleNamespace(omni_autonomy_tier="", omni_auto_execute_enabled=False)
    assert await resolve_tier(settings=s, redis=redis) == "auto"


class _FakeRepo:
    def __init__(self, tier: str | None) -> None:
        self._tier = tier

    async def get_tier(self, tenant_id: str = "default") -> str | None:
        return self._tier



async def test_resolve_tier_db_over_env(redis):
    s = SimpleNamespace(omni_autonomy_tier="shadow", omni_auto_execute_enabled=False)
    assert await resolve_tier(settings=s, repo=_FakeRepo("assist"), redis=redis) == "assist"


class _ExplodingRepo:
    async def get_tier(self, tenant_id: str = "default") -> str | None:
        raise ConnectionError("postgres down")


async def test_resolve_tier_db_lookup_fail_closed(redis):
    """Postgres lookup raising (e.g. connection lost) must fail-closed to shadow,
    not propagate — mirrors _apply_plan_ceiling's existing fail-closed pattern."""
    s = SimpleNamespace(omni_autonomy_tier="auto", omni_auto_execute_enabled=True)
    assert await resolve_tier(settings=s, repo=_ExplodingRepo(), redis=redis) == "shadow"


@pytest.mark.asyncio
async def test_resolve_tier_applies_provider_plan_ceiling(redis):
    s = SimpleNamespace(omni_autonomy_tier="auto", omni_auto_execute_enabled=True)
    class _PlanRepo(_FakeRepo):
        async def get_autonomy_ceiling(self, tenant_id: str = "default") -> str | None:
            return "assist"
    repo = _PlanRepo("auto")
    assert await resolve_tier(settings=s, repo=repo, redis=redis, tenant_id="acme") == "assist"


# ── §5 readiness ──────────────────────────────────────────────────────────────
async def test_readiness_not_ready_low_volume(redis):
    from workers.tier_readiness import compute_tier_readiness

    s = SimpleNamespace(
        omni_tier_min_days_shadow=90, omni_tier_min_advisories=50,
        omni_tier_shadow_assist_wilson=0.80, omni_tier_max_false_positive_rate=0.10,
    )
    r = await compute_tier_readiness(redis=redis, settings=s, current_tier="shadow")
    assert r.next_tier == "assist"
    assert r.ready is False
    assert any("total" in x for x in r.reasons)


async def test_readiness_ready_when_criteria_met(redis):
    import time

    from workers.tier_readiness import compute_tier_readiness

    # 60 accepted, 0 rejected/fp → wilson cao, total ≥ 50
    for i in range(60):
        await redis.zadd("omni:kpi:z:default:accepted", {f"t{i}": i})
    s = SimpleNamespace(
        omni_tier_min_days_shadow=90, omni_tier_min_advisories=50,
        omni_tier_shadow_assist_wilson=0.80, omni_tier_max_false_positive_rate=0.10,
        omni_tier_min_graduated_playbooks=1,
    )
    entered = time.time() - 100 * 86400  # 100 ngày trước
    # `graduated_playbooks` là tiêu chí bổ sung 2026-07-29: acceptance cao chứng minh
    # chẩn đoán đúng, KHÔNG chứng minh đã rút ra quy trình lặp lại được. Mặc định 0 nên
    # phải truyền tường minh ở đây; bỏ trống = fail-closed (có test riêng phủ).
    r = await compute_tier_readiness(
        redis=redis, settings=s, current_tier="shadow", tier_entered_at=entered,
        graduated_playbooks=1,
    )
    assert r.ready is True
    assert r.reasons == ()
    assert r.accepted == 60


async def test_readiness_auto_tier_top():
    from workers.tier_readiness import compute_tier_readiness

    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    s = SimpleNamespace()
    out = await compute_tier_readiness(redis=r, settings=s, current_tier="auto")
    assert out.next_tier is None and out.ready is False
    await r.aclose()


# ── §4 Telegram HITL callback ─────────────────────────────────────────────────
def test_parse_hitl_callback():
    from workers.hitl_telegram import parse_hitl_callback

    assert parse_hitl_callback("hitl:approve:p123") == ("approve", "p123")
    assert parse_hitl_callback("hitl:reject:p123") == ("reject", "p123")
    assert parse_hitl_callback("ofs:abc:1") is None
    assert parse_hitl_callback("hitl:bogus:p1") is None
    assert parse_hitl_callback("hitl:approve:") is None


def test_build_hitl_card():
    from workers.hitl_telegram import build_hitl_card

    text, markup = build_hitl_card(
        pending_id="p1", tool_name="k8s_scale_resource", risk_class="MEDIUM", tier="assist",
    )
    assert "k8s_scale_resource" in text
    kb = markup["inline_keyboard"][0]
    assert kb[0]["callback_data"] == "hitl:approve:p1"
    assert kb[1]["callback_data"] == "hitl:reject:p1"


class _FakeTelegram:
    def __init__(self) -> None:
        self.acks: list[tuple[str, str]] = []

    async def answer_callback_query(self, cq_id: str, *, text: str | None = None, show_alert: bool = False):
        self.acks.append((cq_id, text or ""))
        return {}


async def test_hitl_approve_writes_crat_before_dispatch(redis):
    import json

    from workers.hitl_telegram import handle_hitl_callback, pending_key

    await redis.set(pending_key("p1"), json.dumps({
        "trace_id": "trace-1", "tool_name": "k8s_scale_resource",
        "risk_class": "MEDIUM", "tier": "assist",
        "action_body": {"tool_name": "k8s_scale_resource", "args": {"replicas": 3}},
    }))
    kafka = _FakeKafka()
    tg = _FakeTelegram()
    ctx = SimpleNamespace(
        redis=redis, kafka=kafka, telegram=tg,
        settings=SimpleNamespace(
            kafka_topic_audit_chain="omni-audit-chain",
            kafka_topic_actions="omni-actions",
            kafka_topic_action_feedback="omni-action-feedback",
        ),
    )
    update = {"callback_query": {"id": "cq1", "data": "hitl:approve:p1", "from": {"id": 42}}}
    consumed = await handle_hitl_callback(ctx, update)

    assert consumed is True
    # CRAT block ghi (audit chain trên Redis)
    assert await redis.llen("audit_chain:blocks") == 1
    # action dispatched
    topics = [t for t, _ in kafka.sent]
    assert "omni-audit-chain" in topics  # CRAT kafka mirror
    assert "omni-actions" in topics
    # pending dọn + ack
    assert await redis.get(pending_key("p1")) is None
    assert tg.acks and "duyệt" in tg.acks[0][1]


async def test_hitl_reject_routes_feedback(redis):
    import json

    from workers.hitl_telegram import handle_hitl_callback, pending_key

    await redis.set(pending_key("p2"), json.dumps({"trace_id": "t2", "tool_name": "k8s_delete_pod"}))
    kafka = _FakeKafka()
    tg = _FakeTelegram()
    ctx = SimpleNamespace(
        redis=redis, kafka=kafka, telegram=tg,
        settings=SimpleNamespace(
            kafka_topic_audit_chain="omni-audit-chain",
            kafka_topic_actions="omni-actions",
            kafka_topic_action_feedback="omni-action-feedback",
        ),
    )
    update = {"callback_query": {"id": "cq2", "data": "hitl:reject:p2", "from": {"id": 7}}}
    await handle_hitl_callback(ctx, update)
    topics = [t for t, _ in kafka.sent]
    assert "omni-action-feedback" in topics
    assert "omni-actions" not in topics


async def test_hitl_ignores_non_hitl_callback(redis):
    from workers.hitl_telegram import handle_hitl_callback

    ctx = SimpleNamespace(redis=redis, kafka=None, telegram=None, settings=SimpleNamespace())
    assert await handle_hitl_callback(ctx, {"callback_query": {"data": "ofs:x:1"}}) is False
