"""TDD cho phản xạ nhanh remote-agent gắn vào knowledge_pipeline._decide_and_promote.

Remote host (VM khách) không có Prometheus để cào như proactive cluster — trigger
ở đây là chính deviation z-score/ngưỡng tĩnh Omni đã tự tính. Trước khi nâng một
deviation lên toàn bộ vòng chẩn đoán RAG+LLM, thử phản xạ nhanh: đã có cách sửa
đã biết + đã kiểm chứng qua discovery snapshot của đúng host này chưa?
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fakeredis.aioredis import FakeRedis

from remote_agent.discovery import save_discovery_snapshot
from workers import knowledge_pipeline as kp
from workers.knowledge_pipeline import handle_knowledge_evidence


@pytest.fixture
def redis():
    return FakeRedis(decode_responses=True)


def _ctx(redis_client):
    return SimpleNamespace(
        redis=redis_client,
        telegram=None,
        telegram_chat_id=None,
        kafka=_CaptureKafka(),
        settings=SimpleNamespace(
            kafka_topic_diagnostic_evidence="omni-diagnostic-evidence",
            action_experience_score_threshold=0.55,
        ),
        ledger=SimpleNamespace(record_exception=AsyncMock()),
    )


class _CaptureKafka:
    def __init__(self):
        self.sent: list[tuple[str, dict, bytes | None]] = []

    async def send_dict(self, topic, value, key=None):
        self.sent.append((topic, value, key))

    def envelopes(self, topic="omni-diagnostic-evidence"):
        return [json.loads(v["data"]) for t, v, _ in self.sent if t == topic]


def _metric_ev(tenant: str, host: str, agent_id: str, **metrics):
    return {
        "signal_type": "METRIC_SAMPLE",
        "tenant_id": tenant,
        "namespace": host,
        "lane": "SYS_RESOURCE",
        "probe": "remote_system_metrics",
        "trace_id": f"ra-{host}",
        "extracted_fact": {"agent_id": agent_id, "hostname": host, **metrics},
    }


@pytest.mark.asyncio
async def test_no_discovery_snapshot_falls_through_to_promote_unchanged(
    redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Không có snapshot => KHÔNG mạo hiểm thử phản xạ, đi đường đầy đủ như trước
    khi có tính năng này (không regress hành vi cũ khi chưa có discovery)."""
    called = {"reflex": False}

    async def _fake_try_remote(*a, **kw):
        called["reflex"] = True
        return {"resolved": True}

    monkeypatch.setattr("workers.remote_known_fix.try_remote_known_fix", _fake_try_remote)

    ctx = _ctx(redis)
    await handle_knowledge_evidence(
        ctx, _metric_ev("t1", "h1", "agent-h1", cpu_percent=95.0)
    )

    assert called["reflex"] is False
    assert len(ctx.kafka.envelopes()) == 1  # promote xảy ra như cũ


@pytest.mark.asyncio
async def test_reflex_resolves_skips_promotion(redis, monkeypatch: pytest.MonkeyPatch) -> None:
    """Discovery snapshot có thật + reflex dispatch thành công => KHÔNG nâng
    ANOMALY cho deviation đó (tránh tốn cả vòng RAG+LLM cho ca đã biết cách sửa)."""
    await save_discovery_snapshot(
        redis, tenant_id="t1", agent_id="agent-h1",
        snapshot={"services": [{"name": "nginx"}]},
    )

    captured: dict = {}

    async def _fake_try_remote(ctx, *, query_text, score_threshold, host_scope, agent_id, tenant_id, trace_id):
        captured.update(
            query_text=query_text, host_scope=host_scope, agent_id=agent_id,
            tenant_id=tenant_id,
        )
        return {"resolved": True, "command_id": "cmd-1"}

    monkeypatch.setattr("workers.remote_known_fix.try_remote_known_fix", _fake_try_remote)

    ctx = _ctx(redis)
    await handle_knowledge_evidence(
        ctx, _metric_ev("t1", "h1", "agent-h1", cpu_percent=95.0)
    )

    assert ctx.kafka.envelopes() == []  # promote bị bỏ qua
    assert captured["agent_id"] == "agent-h1"
    assert captured["tenant_id"] == "t1"
    assert "nginx" in captured["host_scope"]
    assert "nginx.service" in captured["host_scope"]


@pytest.mark.asyncio
async def test_reflex_no_candidate_falls_through_to_promote(
    redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Có snapshot nhưng không tìm được cách sửa đã biết => hành vi cũ không đổi:
    nâng ANOMALY, đi vòng chẩn đoán đầy đủ."""
    await save_discovery_snapshot(
        redis, tenant_id="t1", agent_id="agent-h1",
        snapshot={"services": [{"name": "nginx"}]},
    )

    async def _fake_try_remote(*a, **kw):
        return {"resolved": False, "reason": "no_candidate"}

    monkeypatch.setattr("workers.remote_known_fix.try_remote_known_fix", _fake_try_remote)

    ctx = _ctx(redis)
    await handle_knowledge_evidence(
        ctx, _metric_ev("t1", "h1", "agent-h1", cpu_percent=95.0)
    )

    envs = ctx.kafka.envelopes()
    assert len(envs) == 1
    assert envs[0]["signal_type"] == "ANOMALY"


@pytest.mark.asyncio
async def test_empty_services_snapshot_falls_through(redis, monkeypatch: pytest.MonkeyPatch) -> None:
    """Snapshot tồn tại nhưng rỗng dịch vụ (chưa discovery xong) => không mạo
    hiểm, không gọi reflex."""
    await save_discovery_snapshot(
        redis, tenant_id="t1", agent_id="agent-h1", snapshot={"services": []},
    )
    called = {"reflex": False}

    async def _fake_try_remote(*a, **kw):
        called["reflex"] = True
        return {"resolved": True}

    monkeypatch.setattr("workers.remote_known_fix.try_remote_known_fix", _fake_try_remote)

    ctx = _ctx(redis)
    await handle_knowledge_evidence(
        ctx, _metric_ev("t1", "h1", "agent-h1", cpu_percent=95.0)
    )

    assert called["reflex"] is False
    assert len(ctx.kafka.envelopes()) == 1


@pytest.mark.asyncio
async def test_discovery_snapshot_load_error_falls_through_not_raises(
    redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lỗi đọc snapshot (Redis chập chờn) không được làm cả deviation biến mất
    im lặng — phải rơi về đường đầy đủ, không văng exception ra ngoài."""

    async def _raise(*a, **kw):
        raise RuntimeError("redis timeout")

    monkeypatch.setattr("remote_agent.discovery.load_discovery_snapshot", _raise)

    ctx = _ctx(redis)
    await handle_knowledge_evidence(
        ctx, _metric_ev("t1", "h1", "agent-h1", cpu_percent=95.0)
    )

    envs = ctx.kafka.envelopes()
    assert len(envs) == 1
    assert envs[0]["signal_type"] == "ANOMALY"
