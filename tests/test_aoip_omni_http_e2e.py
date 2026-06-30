"""E2E: HTTPOmniClient ↔ gateway Omni THẬT (in-process qua ASGITransport).

Theo convention repo (test_remote_agent_e2e): client AOIP nói chuyện với ROUTER
gateway THẬT (agent_webhook + agent_commands), không mock business logic. Chỉ
fake infra vắng mặt: Redis→FakeRedis. Chứng minh wiring nói đúng protocol
production: register → fetch mission (commands) → submit result → submit evidence.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
from fakeredis.aioredis import FakeRedis
from fastapi import FastAPI

from aoip.agent.identity import derive_identity
from aoip.agent.omni_client import HTTPOmniClient
from aoip.agent.runtime import RemoteAgent
from aoip.transport import LocalTransport


def _build_real_gateway() -> FastAPI:
    """Gateway THẬT: 2 router agent, FakeRedis, không auth (lab mode)."""
    from gateway.routes.agent_commands import router as commands_router
    from gateway.routes.agent_webhook import router as webhook_router

    app = FastAPI()
    app.state.redis = FakeRedis(decode_responses=True)
    app.state.kafka = AsyncMock()
    app.state.kafka_topic_evidence = "omni-diagnostic-evidence"
    app.state.kafka_topic_knowledge_evidence = "omni-knowledge-evidence"
    app.include_router(webhook_router)
    app.include_router(commands_router)
    return app


def _client_for(app: FastAPI) -> HTTPOmniClient:
    asgi = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://omni")
    return HTTPOmniClient(client=asgi)


async def test_register_reaches_real_gateway_registry():
    app = _build_real_gateway()
    omni = _client_for(app)
    ident = derive_identity(LocalTransport(target="ec2-1"), tenant="acme")

    await omni.register(ident, version="2.0.0", capabilities=["discovery"], platform="linux")
    # gateway lưu vào Redis registry (key prefix THẬT từ agent_webhook).
    raw = await app.state.redis.get(f"omni:remote_agent:registry:{ident.agent_id}")
    assert raw is not None
    assert ident.host in raw


async def test_full_loop_register_fetch_result_through_gateway():
    app = _build_real_gateway()
    omni = _client_for(app)
    agent = RemoteAgent(transport=LocalTransport(target="ec2-1"), tenant="acme", omni=omni)

    await agent.register()
    await agent.heartbeat()

    # Omni-side: analyst enqueue một command (mission) qua route THẬT.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://omni") as c:
        r = await c.post("/webhook/agent/commands/enqueue", json={
            "agent_id": agent.identity.agent_id,
            "commands": [{"command": "systemctl", "purpose": "understand_host"}],
        })
        assert r.status_code == 200 and r.json()["enqueued"] == 1

    goal = await agent.pull_mission()
    assert goal == "understand_host"  # đọc qua GET /commands/{id} THẬT

    await agent.report_result(rc=0, stdout="ok")
    # kết quả lưu Redis theo cmd_id THẬT.
    assert agent._active_cmd_id
    stored = await app.state.redis.get(f"omni:diag:cmdresult:{agent._active_cmd_id}")
    assert stored is not None and '"rc": 0' in stored


async def test_submit_evidence_accepted_by_gateway():
    app = _build_real_gateway()
    omni = _client_for(app)
    agent = RemoteAgent(transport=LocalTransport(target="ec2-1"), tenant="acme", omni=omni)
    await agent.register()

    # evidence hợp lệ (có nội dung) → gateway nhận, không lỗi.
    await agent.report_evidence([{
        "trace_id": "t1", "probe": "discovery", "result": "PASSED",
        "alert_hint": "redis on 6379", "signal_type": "DISCOVERY",
        "evidence_source": "DiscoveryEvidence",
    }])
