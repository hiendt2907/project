"""`mark_stage(domain=...)` — trục domain trong trace meta.

Trước đây `mark_stage` chỉ lưu `lane`, nên `/trace/recent` trả `domain` rỗng cho 100%
trace (đo trên GCP: 30/30) và cột "Lĩnh vực" của portal phải rơi về nhãn lane — hai
domain khác nhau (`service` vs `network`) hiện CÙNG một tên vì chung lane SYS_HARD_FAIL.
"""
from __future__ import annotations

import json

import pytest
from fakeredis.aioredis import FakeRedis

from pkg.observability.pipeline_stages import mark_stage


async def _meta(redis: FakeRedis, trace_id: str) -> dict:
    raw = await redis.hget(f"omni:trace:stages:{trace_id}", "__meta__")
    return json.loads(raw) if raw else {}


@pytest.fixture
def redis() -> FakeRedis:
    return FakeRedis(decode_responses=True)


@pytest.mark.asyncio
async def test_domain_persisted_in_meta(redis):
    await mark_stage(redis, "t1", "INGEST", "ok", lane="SYS_HARD_FAIL", domain="network")
    assert (await _meta(redis, "t1"))["domain"] == "network"


@pytest.mark.asyncio
async def test_domain_defaults_empty_not_missing(redis):
    """Callers cũ không truyền domain vẫn phải ghi được — trường có mặt, giá trị rỗng."""
    await mark_stage(redis, "t2", "INGEST", "ok", lane="SYS_RESOURCE")
    meta = await _meta(redis, "t2")
    assert meta["domain"] == ""
    assert meta["lane"] == "SYS_RESOURCE"


@pytest.mark.asyncio
async def test_late_domain_fills_earlier_empty(redis):
    """INGEST mark chạy TRƯỚC detect_domain ⇒ domain phải lấp được về sau."""
    await mark_stage(redis, "t3", "INGEST", "ok", lane="SYS_HARD_FAIL")
    await mark_stage(redis, "t3", "EVIDENCE", "ok", lane="SYS_HARD_FAIL", domain="service")
    assert (await _meta(redis, "t3"))["domain"] == "service"


@pytest.mark.asyncio
async def test_empty_domain_never_clears_existing(redis):
    """last-non-empty-wins: mark sau không truyền domain không được xoá domain đã có."""
    await mark_stage(redis, "t4", "EVIDENCE", "ok", domain="storage")
    await mark_stage(redis, "t4", "LLM", "ok")
    await mark_stage(redis, "t4", "CRAT", "ok", lane="SYS_RESOURCE")
    assert (await _meta(redis, "t4"))["domain"] == "storage"


@pytest.mark.asyncio
async def test_domain_does_not_disturb_lane(redis):
    await mark_stage(redis, "t5", "INGEST", "ok", lane="APP_HTTP")
    await mark_stage(redis, "t5", "LLM", "ok", domain="application")
    meta = await _meta(redis, "t5")
    assert meta["lane"] == "APP_HTTP"
    assert meta["domain"] == "application"


@pytest.mark.asyncio
async def test_domain_published_on_event_stream(redis):
    await mark_stage(redis, "t6", "INGEST", "ok", lane="SYS_RESOURCE", domain="os_host")
    entries = await redis.xrevrange("omni:trace:events", count=1)
    assert entries[0][1]["domain"] == "os_host"


@pytest.mark.asyncio
async def test_two_scenarios_same_lane_keep_distinct_domains(redis):
    """Chính cái mà cột "Lĩnh vực" từng làm mất: service và network chung lane."""
    await mark_stage(redis, "svc", "EVIDENCE", "ok", lane="SYS_HARD_FAIL", domain="service")
    await mark_stage(redis, "net", "EVIDENCE", "ok", lane="SYS_HARD_FAIL", domain="network")
    assert (await _meta(redis, "svc"))["domain"] == "service"
    assert (await _meta(redis, "net"))["domain"] == "network"
    assert (await _meta(redis, "svc"))["lane"] == (await _meta(redis, "net"))["lane"]
