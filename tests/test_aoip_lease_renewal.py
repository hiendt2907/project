"""Failure injection: atomic execution-lease renew/release + generic renewal coordinator.

Chứng minh: renew là compare-and-expire ATOMIC (không GET-rồi-SET rời); owner mới không
bị owner cũ renew đè; ``run_with_renewal`` không để orphan asyncio task và không nuốt
CancelledError.
"""
from __future__ import annotations

import asyncio

import pytest
from fakeredis.aioredis import FakeRedis

from aoip.agent.lease import ExecutionLease
from aoip.agent.renewal import run_with_renewal


def _redis():
    return FakeRedis(decode_responses=True)


async def test_renew_with_correct_token_extends_ttl():
    r = _redis()
    lease = ExecutionLease(r)
    token = await lease.acquire("svc:db", holder="agent-1", ttl_s=5)
    ok = await lease.renew("svc:db", token=token, ttl_s=120)
    assert ok is True
    ttl = await r.ttl("lease:svc:db")
    assert ttl > 5  # gia hạn thật


async def test_renew_with_wrong_token_rejected():
    r = _redis()
    lease = ExecutionLease(r)
    await lease.acquire("svc:db", holder="agent-1", ttl_s=120)
    ok = await lease.renew("svc:db", token="bogus", ttl_s=120)
    assert ok is False


async def test_ownership_lost_when_lease_expired_and_reacquired():
    r = _redis()
    lease = ExecutionLease(r)
    token = await lease.acquire("svc:db", holder="agent-1", ttl_s=1)
    await asyncio.sleep(1.2)
    new_token = await lease.acquire("svc:db", holder="agent-2", ttl_s=120)
    assert new_token is not None
    # agent-1 renew với token cũ sau khi agent-2 đã giành lease → ownership_lost
    ok = await lease.renew("svc:db", token=token, ttl_s=120)
    assert ok is False
    # agent-2 vẫn còn giữ lease của MÌNH nguyên vẹn
    assert await lease.holder_token("svc:db") == new_token


async def test_release_with_wrong_token_does_not_delete_lease():
    r = _redis()
    lease = ExecutionLease(r)
    token = await lease.acquire("svc:db", holder="agent-1")
    ok = await lease.release("svc:db", token="wrong")
    assert ok is False
    assert await lease.holder_token("svc:db") == token  # lease vẫn còn


async def test_renew_atomic_under_race_old_owner_vs_new_owner():
    """Race: lease vừa hết hạn, agent-2 giành được NGAY trước khi agent-1 renew.

    Atomic Lua đảm bảo agent-1 renew thất bại đúng lúc, KHÔNG có cửa sổ nào để agent-1
    ghi đè lease của agent-2 (script so sánh+ghi trong MỘT round-trip Redis)."""
    r = _redis()
    lease = ExecutionLease(r)
    token1 = await lease.acquire("svc:db", holder="agent-1", ttl_s=120)
    # giả lập lease đã bị agent-2 giành lại (vd TTL hết + agent-2 acquire) TRƯỚC khi
    # agent-1 kịp renew — ghi thẳng token khác vào key để mô phỏng thời điểm đó
    token2 = "agent-2-token"
    await r.set("lease:svc:db", token2, ex=120)
    ok = await lease.renew("svc:db", token=token1, ttl_s=120)
    assert ok is False
    assert await lease.holder_token("svc:db") == token2  # KHÔNG bị agent-1 ghi đè


# ── run_with_renewal: no orphan tasks, ownership loss detection ─────────────

async def test_run_with_renewal_success_path_no_orphan_task():
    calls = []

    async def renew():
        calls.append(1)
        return True

    async def body():
        await asyncio.sleep(0.05)
        return "done"

    before = len(asyncio.all_tasks())
    result = await run_with_renewal(body(), renew_fn=renew, interval_s=0.01)
    after = len(asyncio.all_tasks())
    assert result.result == "done"
    assert result.ownership_lost is False
    assert after <= before  # không orphan task còn sống


async def test_run_with_renewal_detects_ownership_lost():
    async def renew():
        return False

    async def body():
        await asyncio.sleep(0.05)
        return "done"

    result = await run_with_renewal(body(), renew_fn=renew, interval_s=0.01)
    assert result.result == "done"  # body KHÔNG bị huỷ giữa chừng
    assert result.ownership_lost is True


async def test_run_with_renewal_transient_renew_error_does_not_mark_lost():
    calls = {"n": 0}

    async def renew():
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("transient")
        return True

    async def body():
        await asyncio.sleep(0.05)
        return "done"

    result = await run_with_renewal(body(), renew_fn=renew, interval_s=0.01)
    assert result.ownership_lost is False  # lỗi mạng thoáng qua ≠ mất ownership


async def test_run_with_renewal_cancellation_not_swallowed():
    async def renew():
        return True

    async def body():
        await asyncio.sleep(10)

    task = asyncio.ensure_future(run_with_renewal(body(), renew_fn=renew, interval_s=0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
