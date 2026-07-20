"""TDD — inject System Twin summary vào evidence advisory (P1 gap "liên kết").

Omni có bản đồ hệ thống khách hàng (omni:aoip:system_model:{tenant}) nhưng bộ não
chẩn đoán advisory chưa bao giờ được xem nó — impact_chain phải đoán dependency.
build_system_twin_block đọc store thật (format của aoip.system_model_store) và
render block compact, capped, cho vào evidence_text.
"""

from __future__ import annotations

import json

import pytest
from fakeredis.aioredis import FakeRedis

from workers.system_twin_context import build_system_twin_block


def _fact(subject: str, predicate: str, obj: str, confidence: float = 0.9) -> dict:
    return {
        "subject": subject,
        "predicate": predicate,
        "obj": obj,
        "confidence": confidence,
        "provenance": ["probe:discovery"],
        "observation_time": 1000.0,
        "verified_time": 1000.0,
    }


@pytest.fixture
def redis() -> FakeRedis:
    return FakeRedis(decode_responses=True)


async def _seed(redis: FakeRedis, tenant: str, facts: list[dict]) -> None:
    await redis.hset(
        f"omni:aoip:system_model:{tenant}",
        mapping={"facts": json.dumps(facts), "revision": "3"},
    )


async def test_empty_twin_returns_empty_string(redis: FakeRedis):
    block = await build_system_twin_block(redis, "default")
    assert block == ""


async def test_twin_block_groups_facts_by_host(redis: FakeRedis):
    await _seed(
        redis,
        "staging-sim",
        [
            _fact("host:cust-edge", "listens_on", "nginx:80"),
            _fact("host:cust-edge", "mounts", "nfs:/exports/data"),
            _fact("host:cust-db", "listens_on", "mysql:3306"),
        ],
    )
    block = await build_system_twin_block(redis, "staging-sim")
    assert block.startswith("=== SYSTEM TWIN")
    assert "host:cust-edge" in block
    assert "nginx:80" in block
    assert "host:cust-db" in block
    assert "mysql:3306" in block


async def test_twin_block_is_capped(redis: FakeRedis):
    facts = [
        _fact(f"host:h{i}", f"pred_{j}", "v" * 50) for i in range(30) for j in range(10)
    ]
    await _seed(redis, "big", facts)
    block = await build_system_twin_block(redis, "big", max_chars=800)
    assert len(block) <= 800


async def test_twin_block_survives_redis_error():
    class _Boom:
        async def hgetall(self, *_a, **_k):
            raise ConnectionError("down")

    block = await build_system_twin_block(_Boom(), "default")
    assert block == ""
