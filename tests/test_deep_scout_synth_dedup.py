"""Deep scout chỉ được gọi LLM cho entity THẬT SỰ đổi.

Bối cảnh: đo trên cụm GCP ngày 2026-08-09 — 93/95 LLM call trong 24 phút là
`deep_scout_autonomous` synth lại toàn bộ pod+service dù không có gì đổi
(~15 phút LLM chạy liên tục mỗi chu kỳ 30 phút, tranh hàng đợi với advisory
thật vì Ollama chạy num_parallel=1).
"""
from __future__ import annotations

import json
import time

import pytest

from init.deep_scout_autonomous import (
    _cached_summary,
    _entity_fingerprint,
    _store_summary,
)


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value


def _pod_entity(**overrides):
    entity = {
        "entity_type": "pod",
        "namespace": "multi-agent",
        "name": "omni-fullstack-abc",
        "pod_ip": "10.42.0.7",
        "ports": [{"container": "worker", "port": 8090, "name": "http", "protocol": "TCP"}],
        "containers": [{"name": "worker", "image": "omni:latest"}],
        "services_same_namespace": ["redis", "kafka"],
        "namespace_cpu_rate_sample": 0.41,
        "namespace_mem_sample": 1234.5,
    }
    entity.update(overrides)
    return entity


def test_fingerprint_ignores_live_metric_samples():
    """Số đo cpu/mem đổi mỗi vòng quét — tính vào hash là dedup chết ngay."""
    a = _pod_entity(namespace_cpu_rate_sample=0.41, namespace_mem_sample=1000.0)
    b = _pod_entity(namespace_cpu_rate_sample=0.98, namespace_mem_sample=9999.0)

    assert _entity_fingerprint(a) == _entity_fingerprint(b)


def test_fingerprint_changes_when_structure_changes():
    baseline = _entity_fingerprint(_pod_entity())

    assert _entity_fingerprint(_pod_entity(pod_ip="10.42.0.99")) != baseline
    assert _entity_fingerprint(
        _pod_entity(containers=[{"name": "worker", "image": "omni:v2"}])
    ) != baseline


def test_fingerprint_is_order_independent():
    """dict K8s trả về không đảm bảo thứ tự key; hash phải ổn định."""
    a = {"entity_type": "pod", "namespace": "ns", "name": "p"}
    b = {"name": "p", "namespace": "ns", "entity_type": "pod"}

    assert _entity_fingerprint(a) == _entity_fingerprint(b)


@pytest.mark.asyncio
async def test_unchanged_entity_reuses_cached_summary():
    redis = FakeRedis()
    entity = _pod_entity()
    fp = _entity_fingerprint(entity)
    await _store_summary(redis, "pid-1", fp, "tóm tắt cũ")

    # Vòng quét sau: số đo cpu/mem khác, cấu trúc y nguyên.
    fp_next = _entity_fingerprint(_pod_entity(namespace_cpu_rate_sample=0.77))

    assert await _cached_summary(redis, "pid-1", fp_next, 86400) == "tóm tắt cũ"


@pytest.mark.asyncio
async def test_changed_entity_forces_resynth():
    redis = FakeRedis()
    await _store_summary(redis, "pid-1", _entity_fingerprint(_pod_entity()), "tóm tắt cũ")

    changed = _entity_fingerprint(_pod_entity(pod_ip="10.42.0.99"))

    assert await _cached_summary(redis, "pid-1", changed, 86400) is None


@pytest.mark.asyncio
async def test_stale_cache_forces_resynth():
    """Dedup không được đóng băng baseline vĩnh viễn ở lần quét đầu."""
    redis = FakeRedis()
    fp = _entity_fingerprint(_pod_entity())
    redis.store["omni:scout:synth:pid-1"] = json.dumps(
        {"fingerprint": fp, "summary": "rất cũ", "ts": time.time() - 90000}
    )

    assert await _cached_summary(redis, "pid-1", fp, 86400) is None
    assert await _cached_summary(redis, "pid-1", fp, 172800) == "rất cũ"


@pytest.mark.asyncio
async def test_missing_or_broken_cache_falls_back_to_synth():
    fp = _entity_fingerprint(_pod_entity())

    assert await _cached_summary(None, "pid-1", fp, 86400) is None

    redis = FakeRedis()
    assert await _cached_summary(redis, "pid-chưa-có", fp, 86400) is None

    redis.store["omni:scout:synth:pid-1"] = "{không phải json"
    assert await _cached_summary(redis, "pid-1", fp, 86400) is None


@pytest.mark.asyncio
async def test_redis_failure_never_breaks_the_scan():
    """Redis hỏng chỉ được làm mất dedup, không được làm chết vòng quét."""

    class BrokenRedis:
        async def get(self, key):
            raise ConnectionError("redis down")

        async def set(self, key, value, ex=None):
            raise ConnectionError("redis down")

    broken = BrokenRedis()
    fp = _entity_fingerprint(_pod_entity())

    assert await _cached_summary(broken, "pid-1", fp, 86400) is None
    await _store_summary(broken, "pid-1", fp, "tóm tắt")  # không được raise
