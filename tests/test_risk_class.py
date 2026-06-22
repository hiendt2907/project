"""Step 1 — risk-class taxonomy tĩnh + risk_class_of fail-closed HIGH + resolver override.

Ref: docs/MASTER_PLAN_autonomy_tiers.md §2, §8 step 1.
Bất biến: mọi tool có class; thiếu → HIGH; dangerous_tools không bao giờ < HIGH.
"""

from __future__ import annotations

from typing import Any

import fakeredis.aioredis
import pytest

from workers.risk_class import (
    DANGEROUS_TOOLS,
    HIGH,
    LOW,
    MEDIUM,
    READONLY,
    STATIC_RISK_CLASS,
    is_dangerous,
    rank,
    risk_class_of,
)
from workers.risk_class_resolver import RiskClassResolver


# ── bảng tĩnh §2 ──────────────────────────────────────────────────────────────
def test_static_map_matches_master_plan():
    assert risk_class_of("k8s_rollout_restart") == LOW
    assert risk_class_of("k8s_create_or_patch_configmap") == LOW
    assert risk_class_of("k8s_scale_resource") == MEDIUM
    assert risk_class_of("k8s_scale_deployment") == MEDIUM
    assert risk_class_of("k8s_patch_resource") == MEDIUM
    # RBAC mutation luôn security-sensitive → HIGH (F-C3)
    assert risk_class_of("k8s_apply_rbac_least_privilege") == HIGH
    assert risk_class_of("k8s_delete_pod") == HIGH
    assert risk_class_of("k8s_patch_secret") == HIGH


def test_readonly_tools_classified_readonly():
    for t in ("k8s_get_logs", "k8s_list_nodes", "promql_instant", "redis_health"):
        assert risk_class_of(t) == READONLY


def test_unknown_tool_fail_closed_high():
    assert risk_class_of("some_brand_new_tool") == HIGH
    assert risk_class_of("") == HIGH


def test_arbitrary_execution_is_high():
    for t in ("execute_shell_command", "execute_in_sandbox", "kubectl_cluster",
              "gated_allowlisted_execute"):
        assert risk_class_of(t) == HIGH


def test_dangerous_tools_all_high_in_static_map():
    for t in DANGEROUS_TOOLS:
        assert STATIC_RISK_CLASS[t] == HIGH
        assert is_dangerous(t)


def test_rank_ordering():
    assert rank(READONLY) < rank(LOW) < rank(MEDIUM) < rank(HIGH)
    assert rank("garbage") == rank(HIGH)


# ── override + clamp dangerous ────────────────────────────────────────────────
def test_override_applies():
    assert risk_class_of("k8s_rollout_restart", override=MEDIUM) == MEDIUM


def test_override_cannot_lower_dangerous_below_high():
    assert risk_class_of("k8s_delete_pod", override=LOW) == HIGH
    assert risk_class_of("k8s_patch_secret", override=READONLY) == HIGH


def test_override_can_raise_non_dangerous():
    assert risk_class_of("k8s_scale_resource", override=HIGH) == HIGH


def test_invalid_override_fail_closed_high():
    assert risk_class_of("k8s_rollout_restart", override="BOGUS") == HIGH


# ── resolver: static khi không repo ───────────────────────────────────────────
async def test_resolver_static_without_repo():
    r = RiskClassResolver()
    assert await r.resolve("k8s_scale_resource") == MEDIUM
    assert await r.resolve("unknown_tool") == HIGH


# ── resolver: đọc override từ repo + cache negative/positive ──────────────────
class _FakeRepo:
    def __init__(self, overrides: dict[str, str]) -> None:
        self._o = overrides
        self.calls = 0

    async def get_risk_class_override(self, tool_name: str, tenant_id: str = "default") -> str | None:
        self.calls += 1
        return self._o.get(tool_name)


@pytest.fixture
async def redis() -> Any:
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


async def test_resolver_reads_override_and_caches(redis):
    repo = _FakeRepo({"k8s_rollout_restart": MEDIUM})
    r = RiskClassResolver(repo=repo, redis=redis)

    assert await r.resolve("k8s_rollout_restart") == MEDIUM
    assert repo.calls == 1
    # lần 2 trúng cache, không query repo
    assert await r.resolve("k8s_rollout_restart") == MEDIUM
    assert repo.calls == 1
    assert await redis.get("omni:cfg:risk:default:k8s_rollout_restart") == MEDIUM


async def test_resolver_caches_negative_lookup(redis):
    repo = _FakeRepo({})
    r = RiskClassResolver(repo=repo, redis=redis)

    assert await r.resolve("k8s_scale_resource") == MEDIUM  # static, không override
    assert await r.resolve("k8s_scale_resource") == MEDIUM
    assert repo.calls == 1  # negative cached → không query lần 2
    assert await redis.get("omni:cfg:risk:default:k8s_scale_resource") == "__none__"


async def test_resolver_clamps_dangerous_override(redis):
    repo = _FakeRepo({"k8s_delete_pod": LOW})  # override bậy
    r = RiskClassResolver(repo=repo, redis=redis)
    assert await r.resolve("k8s_delete_pod") == HIGH  # clamp


# ── write-side invariant: repo từ chối hạ dangerous ───────────────────────────
async def test_repo_rejects_lowering_dangerous():
    from services.admin_config.repo import AdminConfigRepo

    repo = AdminConfigRepo(pool=None, redis=None)
    with pytest.raises(ValueError, match="dangerous_tool"):
        await repo.set_risk_class_override(
            tool_name="k8s_delete_pod", risk_class="LOW", actor="op"
        )
