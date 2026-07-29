"""G1 — vòng học chạy được TRONG chế độ shadow/advisory.

Bối cảnh (đo runtime 2026-07-29): `OMNI_AUTO_EXECUTE_ENABLED=false` (đúng, an toàn)
→ không mutation → không VERIFIED_SUCCESS → `evaluate_for_promotion` chưa từng chạy
(`omni:learn:promo:*` rỗng) → `omni_admin.playbook_graduation` = 0 hàng.

Nghĩa là Omni không học được vì nó không hành động. Trong shadow mode, tín hiệu học
DUY NHẤT khả dụng là phán quyết của con người trên advisory. Module này biến chuỗi
ack/reject đó thành playbook tốt nghiệp.

INVARIANT: playbook tốt nghiệp từ advisory LUÔN ``auto_execute=False``. Con người đồng
ý với một CHẨN ĐOÁN không đồng nghĩa uỷ quyền cho máy TỰ THỰC THI — đường auto-execute
vẫn phải đi qua VERIFIED_SUCCESS thật của `promoter.evaluate_for_promotion`.
"""

from __future__ import annotations

import pytest

from services.learning_promoter.advisory_promoter import (
    GRADUATED,
    CANDIDATE,
    FROZEN,
    advisory_pattern_key,
    next_graduation_state,
    record_advisory_verdict,
)


class _FakeRepo:
    def __init__(self, rows=None):
        self.rows = rows or {}
        self.calls = []

    async def bump_playbook_graduation(
        self, *, tenant_id, domain, playbook_id, success, crat_ref=None, track="playbook"
    ):
        key = (tenant_id, track, domain, playbook_id)
        row = self.rows.get(key, {"success_count": 0, "fail_count": 0, "state": "DRAFT"})
        if success:
            row["success_count"] += 1
        else:
            row["fail_count"] += 1
        self.rows[key] = row
        self.calls.append((key, success))
        return dict(row)

    async def set_playbook_graduation_state(
        self, *, tenant_id, domain, playbook_id, state, crat_ref=None, track="playbook"
    ):
        self.rows[(tenant_id, track, domain, playbook_id)]["state"] = state


class _Ctx:
    def __init__(self, repo, min_success=3):
        self.admin_repo = repo
        self.settings = type(
            "S", (), {"omni_advisory_graduation_min_success": min_success,
                      "omni_advisory_graduation_max_fail_rate": 0.25}
        )()
        self.redis = None
        self.kafka = None


# --- pattern key -----------------------------------------------------------

def test_pattern_key_stable_for_same_alert_shape():
    a = advisory_pattern_key({"alertname": "PodOOMKilled", "lane": "SYS_RESOURCE"})
    b = advisory_pattern_key({"alertname": "PodOOMKilled", "lane": "SYS_RESOURCE"})

    assert a == b and a != ""


def test_pattern_key_differs_across_lanes():
    a = advisory_pattern_key({"alertname": "PodOOMKilled", "lane": "SYS_RESOURCE"})
    b = advisory_pattern_key({"alertname": "PodOOMKilled", "lane": "SIEM_SECURITY"})

    assert a != b


def test_pattern_key_empty_when_no_signal():
    assert advisory_pattern_key({}) == ""


# --- state machine ---------------------------------------------------------

def test_first_success_moves_draft_to_candidate():
    assert next_graduation_state(
        success=1, fail=0, min_success=3, max_fail_rate=0.25
    ) == CANDIDATE


def test_graduates_at_threshold():
    assert next_graduation_state(
        success=3, fail=0, min_success=3, max_fail_rate=0.25
    ) == GRADUATED


def test_high_fail_rate_freezes_even_past_threshold():
    # 3 success / 3 fail = 50% fail > 25% → không được tốt nghiệp
    assert next_graduation_state(
        success=3, fail=3, min_success=3, max_fail_rate=0.25
    ) == FROZEN


def test_graduated_playbook_downgrades_on_failures():
    """Đã tốt nghiệp nhưng bắt đầu sai → phải mất bậc, không im lặng."""
    assert next_graduation_state(
        success=5, fail=4, min_success=3, max_fail_rate=0.25
    ) == FROZEN


# --- record verdict --------------------------------------------------------

@pytest.mark.asyncio
async def test_accept_increments_success():
    repo = _FakeRepo()
    ctx = _Ctx(repo)

    state = await record_advisory_verdict(
        ctx, tenant_id="acme", trace_id="t1", accepted=True,
        advisory={"alertname": "PodOOMKilled", "lane": "SYS_RESOURCE"},
    )

    assert repo.calls[0][1] is True
    assert state == CANDIDATE


@pytest.mark.asyncio
async def test_three_accepts_graduate():
    repo = _FakeRepo()
    ctx = _Ctx(repo, min_success=3)
    adv = {"alertname": "PodOOMKilled", "lane": "SYS_RESOURCE"}

    for _ in range(3):
        state = await record_advisory_verdict(
            ctx, tenant_id="acme", trace_id="t", accepted=True, advisory=adv
        )

    assert state == GRADUATED


@pytest.mark.asyncio
async def test_reject_increments_fail_and_can_freeze():
    repo = _FakeRepo()
    ctx = _Ctx(repo, min_success=3)
    adv = {"alertname": "PodOOMKilled", "lane": "SYS_RESOURCE"}

    for _ in range(3):
        await record_advisory_verdict(ctx, tenant_id="acme", trace_id="t",
                                      accepted=True, advisory=adv)
    for _ in range(3):
        state = await record_advisory_verdict(ctx, tenant_id="acme", trace_id="t",
                                              accepted=False, advisory=adv)

    assert state == FROZEN


@pytest.mark.asyncio
async def test_tenant_isolation_separate_counters():
    """INV_NAMESPACE_ISOLATION — acme không được đẩy globex lên tốt nghiệp."""
    repo = _FakeRepo()
    ctx = _Ctx(repo, min_success=3)
    adv = {"alertname": "PodOOMKilled", "lane": "SYS_RESOURCE"}

    for _ in range(3):
        await record_advisory_verdict(ctx, tenant_id="acme", trace_id="t",
                                      accepted=True, advisory=adv)
    state_globex = await record_advisory_verdict(
        ctx, tenant_id="globex", trace_id="t", accepted=True, advisory=adv
    )

    assert state_globex == CANDIDATE  # không phải GRADUATED


@pytest.mark.asyncio
async def test_no_pattern_key_is_noop():
    repo = _FakeRepo()
    ctx = _Ctx(repo)

    state = await record_advisory_verdict(
        ctx, tenant_id="acme", trace_id="t1", accepted=True, advisory={}
    )

    assert state is None
    assert repo.calls == []


@pytest.mark.asyncio
async def test_missing_repo_is_noop_not_crash():
    ctx = _Ctx(None)
    ctx.admin_repo = None

    state = await record_advisory_verdict(
        ctx, tenant_id="acme", trace_id="t1", accepted=True,
        advisory={"alertname": "X", "lane": "SYS_RESOURCE"},
    )

    assert state is None
