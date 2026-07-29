"""G2 — writer và reader KPI phải dùng CÙNG một hình dạng key.

Bug thật tìm được 2026-07-29: `KPIStore` GHI vào `omni:kpi:z:{tenant}:accepted`
(per-tenant) trong khi `promoter._get_fp_rate` và `pkg.autonomy.gate` ĐỌC
`omni:kpi:z:accepted` (không tenant). Redis lab có cả 2 dạng key cùng lúc —
split-brain. Hệ quả: mọi phép đọc FP-rate luôn thấy 0 mẫu → gate chất lượng
im lặng cho qua mọi thứ.

Test này khoá contract lại: chỉ có MỘT hàm dựng key, dùng chung cả 2 đầu.
"""

from __future__ import annotations

import time

import pytest

from workers.kpi_metrics import KPIStore, kpi_outcome_key, read_outcome_rates


class FakeRedis:
    """ZSET tối thiểu — dự án cấm AsyncMock cho ZSET (instinct 90%)."""

    def __init__(self) -> None:
        self.z: dict[str, dict[str, float]] = {}

    async def zadd(self, key, mapping):
        self.z.setdefault(key, {}).update(mapping)

    async def zremrangebyscore(self, key, lo, hi):
        return 0

    async def expire(self, key, ttl):
        return True

    async def zcount(self, key, lo, hi):
        members = self.z.get(key, {})
        lo_f = float("-inf") if lo in ("-inf", "-inf") else float(lo)
        return sum(1 for s in members.values() if s >= lo_f)


def test_key_builder_is_tenant_scoped():
    assert kpi_outcome_key("acme", "accepted") == "omni:kpi:z:acme:accepted"


def test_key_builder_used_by_writer():
    """Writer không được tự nối chuỗi key riêng."""
    assert kpi_outcome_key("default", "rejected") == "omni:kpi:z:default:rejected"


@pytest.mark.asyncio
async def test_writer_and_reader_agree_on_key():
    r = FakeRedis()
    store = KPIStore(r)

    await store.record_accepted("t1", tenant_id="acme")
    await store.record_accepted("t2", tenant_id="acme")
    await store.record_false_positive("t3", tenant_id="acme")

    rates = await read_outcome_rates(r, tenant_id="acme")

    assert rates["total"] == 3
    assert rates["accepted"] == 2
    assert rates["false_positive"] == 1


@pytest.mark.asyncio
async def test_reader_isolates_tenants():
    r = FakeRedis()
    store = KPIStore(r)
    await store.record_accepted("t1", tenant_id="acme")

    rates = await read_outcome_rates(r, tenant_id="globex")

    assert rates["total"] == 0


@pytest.mark.asyncio
async def test_no_data_returns_none_rate_not_zero():
    """Phân biệt 'chưa có dữ liệu' với '0% false positive'.

    Trả 0.0 khi rỗng là fail-OPEN: gate tưởng chất lượng hoàn hảo và cho qua.
    """
    rates = await read_outcome_rates(FakeRedis(), tenant_id="acme")

    assert rates["total"] == 0
    assert rates["fp_rate"] is None
    assert rates["acceptance_rate"] is None


@pytest.mark.asyncio
async def test_rates_computed_when_data_present():
    r = FakeRedis()
    store = KPIStore(r)
    for i in range(8):
        await store.record_accepted(f"a{i}", tenant_id="acme")
    for i in range(2):
        await store.record_false_positive(f"f{i}", tenant_id="acme")

    rates = await read_outcome_rates(r, tenant_id="acme")

    assert rates["acceptance_rate"] == pytest.approx(0.8)
    assert rates["fp_rate"] == pytest.approx(0.2)
