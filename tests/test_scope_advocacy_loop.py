"""Vòng lặp tự xin quyền — tính chất vận hành, không phải "hàm có chạy không".

Ba thứ được tấn công trực tiếp (mỗi cái là một cách vòng lặp này có thể im lặng
làm hỏng hệ thống):

1. Một tenant ném lỗi **không được** chặn tenant sau — nếu không, một tenant có
   sổ ca hỏng sẽ khoá đường xin quyền của mọi khách còn lại.
2. Không có pool PG ⇒ loop thoát êm, không crash worker vì một tính năng phụ.
3. Flag tắt ⇒ không đụng gì tới PG.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace

import fakeredis.aioredis
import pytest

from workers import tier_loops


class FakePool:
    """Pool asyncpg giả: chỉ cần ``.acquire()`` là asynccontextmanager."""

    def __init__(self) -> None:
        self.acquired = 0

    @contextlib.asynccontextmanager
    async def acquire(self):
        self.acquired += 1
        yield SimpleNamespace()


def _ctx(*, pool, enabled=True, interval_hours=24.0):
    return SimpleNamespace(
        admin_pool=pool,
        redis=fakeredis.aioredis.FakeRedis(decode_responses=True),
        settings=SimpleNamespace(
            scope_advocacy_enabled=enabled,
            scope_advocacy_interval_hours=interval_hours,
        ),
    )


class _FakeRepo:
    """Thay AdminConfigRepo — chỉ cần ``list_tenants``."""

    tenants: list[dict] = []

    def __init__(self, pool, redis=None) -> None:  # noqa: D107
        self.pool = pool

    async def list_tenants(self):
        return list(type(self).tenants)


class _FakeAdvocate:
    """Thay ScopeAdvocate — ghi lại tenant đã chạy, ném lỗi theo yêu cầu."""

    seen: list[str] = []
    boom: set[str] = set()

    def __init__(self, ledger, scope) -> None:  # noqa: D107
        pass

    async def run(self, *, tenant_id: str):
        type(self).seen.append(tenant_id)
        if tenant_id in type(self).boom:
            raise RuntimeError("so ca hong")
        return []


@pytest.fixture()
def patched(monkeypatch):
    import services.admin_config as admin_config
    import services.case_ledger.advocacy as advocacy

    _FakeRepo.tenants = []
    _FakeAdvocate.seen = []
    _FakeAdvocate.boom = set()
    monkeypatch.setattr(admin_config, "AdminConfigRepo", _FakeRepo)
    monkeypatch.setattr(advocacy, "ScopeAdvocate", _FakeAdvocate)
    return SimpleNamespace(repo=_FakeRepo, advocate=_FakeAdvocate)


async def _run_one_cycle(ctx) -> None:
    """Chạy loop đúng một chu kỳ rồi dừng (interval đặt rất lớn, stop set ngay)."""
    stop = asyncio.Event()
    task = asyncio.create_task(tier_loops.scope_advocacy_loop(ctx, stop))
    await asyncio.sleep(0)
    for _ in range(50):
        if _FakeAdvocate.seen or task.done():
            break
        await asyncio.sleep(0)
    stop.set()
    await asyncio.wait_for(task, timeout=2)


async def test_mot_tenant_loi_khong_chan_tenant_sau(patched):
    """Tenant `bad` ném lỗi; `zeta` đứng sau vẫn phải được xét."""
    _FakeRepo.tenants = [
        {"tenant_id": "alpha", "status": "active"},
        {"tenant_id": "bad", "status": "active"},
        {"tenant_id": "zeta", "status": "active"},
    ]
    _FakeAdvocate.boom = {"bad"}

    await _run_one_cycle(_ctx(pool=FakePool()))

    assert _FakeAdvocate.seen == ["alpha", "bad", "zeta"]


async def test_tenant_khong_active_bi_bo_qua(patched):
    _FakeRepo.tenants = [
        {"tenant_id": "alpha", "status": "suspended"},
        {"tenant_id": "beta", "status": "active"},
    ]

    await _run_one_cycle(_ctx(pool=FakePool()))

    assert _FakeAdvocate.seen == ["beta"]


async def test_list_tenants_loi_khong_lam_chet_loop(patched):
    """Không đọc được danh sách tenant ⇒ bỏ chu kỳ, loop vẫn sống tới khi stop."""

    async def boom(self):
        raise RuntimeError("pg down")

    _FakeRepo.list_tenants = boom  # type: ignore[assignment]
    try:
        stop = asyncio.Event()
        task = asyncio.create_task(tier_loops.scope_advocacy_loop(_ctx(pool=FakePool()), stop))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not task.done()  # còn sống, đang chờ chu kỳ sau
        stop.set()
        await asyncio.wait_for(task, timeout=2)
    finally:
        del _FakeRepo.list_tenants


async def test_khong_co_pool_thi_loop_van_song(patched):
    """Không có PG ⇒ thoát êm, KHÔNG raise, không chạm advocate."""
    stop = asyncio.Event()
    await asyncio.wait_for(
        tier_loops.scope_advocacy_loop(_ctx(pool=None), stop), timeout=2
    )
    assert _FakeAdvocate.seen == []


async def test_flag_tat_thi_khong_cham_pg(patched):
    _FakeRepo.tenants = [{"tenant_id": "alpha", "status": "active"}]
    pool = FakePool()
    stop = asyncio.Event()

    await asyncio.wait_for(
        tier_loops.scope_advocacy_loop(_ctx(pool=pool, enabled=False), stop), timeout=2
    )

    assert _FakeAdvocate.seen == []
    assert pool.acquired == 0
