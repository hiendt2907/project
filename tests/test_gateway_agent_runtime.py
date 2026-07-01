"""Integration: durable runtime command delivery over the REAL ASGI gateway route.

Chạy qua ASGITransport trên chính app FastAPI + FakeRedis → gần nhất với "Gateway thật"
trong CI. Chứng minh GET=peek (fix P0) + máy trạng thái ack + terminal ack idempotent qua
HTTP contract thật. Proof trên K8s Gateway + VM thật ở scripts/deploy.
"""
from __future__ import annotations

import json
import time

import pytest
from fakeredis.aioredis import FakeRedis
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

TENANT = "acme"
AGENT = "agent-rt-1"


def _make_app(redis) -> FastAPI:
    app = FastAPI()
    app.state.redis = redis
    from gateway.routes.agent_runtime import router
    app.include_router(router)
    return app


async def _register(redis):
    await redis.set(f"omni:remote_agent:registry:{AGENT}",
                    json.dumps({"agent_id": AGENT, "tenant_id": TENANT,
                                "last_seen": int(time.time())}))


def _cmd(command_id="cmd-1", ttl_s=300):
    return {"command_id": command_id, "agent_id": AGENT, "tenant_id": TENANT,
            "mission_id": "mis-1", "incident_id": "inc-1", "decision_id": "dec-1",
            "action_id": "act-1", "canonical_scope": f"{TENANT}:svc:db",
            "payload_hash": "ph-1", "payload": {"verb": "restart"}, "ttl_s": ttl_s}


@pytest.mark.asyncio
async def test_get_is_peek_over_http():
    redis = FakeRedis(decode_responses=True)
    await _register(redis)
    app = _make_app(redis)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.post("/webhook/agent/rt/commands/enqueue", json=_cmd())).status_code == 200
        r1 = await c.get(f"/webhook/agent/rt/commands/{AGENT}")
        cmds = r1.json()["commands"]
        assert [x["command_id"] for x in cmds] == ["cmd-1"]
        assert cmds[0]["state"] == "DELIVERED" and cmds[0]["delivery_count"] == 1
        # command KHÔNG bị pop — record vẫn còn
        rec = await c.get(f"/webhook/agent/rt/commands/record/{TENANT}/cmd-1")
        assert rec.status_code == 200 and rec.json()["state"] == "DELIVERED"


@pytest.mark.asyncio
async def test_full_ack_lifecycle_and_terminal_idempotent():
    redis = FakeRedis(decode_responses=True)
    await _register(redis)
    app = _make_app(redis)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await c.post("/webhook/agent/rt/commands/enqueue", json=_cmd())
        await c.get(f"/webhook/agent/rt/commands/{AGENT}")
        acc = await c.post("/webhook/agent/rt/commands/accept",
                           json={"agent_id": AGENT, "tenant_id": TENANT, "command_id": "cmd-1"})
        assert acc.json()["state"] == "ACCEPTED"
        prog = await c.post("/webhook/agent/rt/commands/progress",
                            json={"agent_id": AGENT, "tenant_id": TENANT, "command_id": "cmd-1",
                                  "phase": "RUNNING"})
        assert prog.json()["state"] == "RUNNING"
        term = await c.post("/webhook/agent/rt/commands/terminal",
                            json={"agent_id": AGENT, "tenant_id": TENANT, "command_id": "cmd-1",
                                  "state": "COMPLETED", "outcome": {"rc": 0}})
        body = term.json()
        assert body["acknowledged"] is True and body["idempotent"] is False
        # terminal → không còn giao lại
        again = await c.get(f"/webhook/agent/rt/commands/{AGENT}")
        assert again.json()["commands"] == []
        # report lại → ack idempotent, outcome không đổi
        dup = await c.post("/webhook/agent/rt/commands/terminal",
                           json={"agent_id": AGENT, "tenant_id": TENANT, "command_id": "cmd-1",
                                 "state": "COMPLETED", "outcome": {"rc": 999}})
        assert dup.json()["idempotent"] is True
        rec = await c.get(f"/webhook/agent/rt/commands/record/{TENANT}/cmd-1")
        assert rec.json()["outcome"] == {"rc": 0}


@pytest.mark.asyncio
async def test_expired_command_zero_delivery_over_http():
    redis = FakeRedis(decode_responses=True)
    await _register(redis)
    app = _make_app(redis)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        # ttl=1s, chờ hết hạn rồi poll → zero delivery
        await c.post("/webhook/agent/rt/commands/enqueue", json=_cmd(ttl_s=1))
        time.sleep(1.2)
        r = await c.get(f"/webhook/agent/rt/commands/{AGENT}")
        assert r.json()["commands"] == []


@pytest.mark.asyncio
async def test_terminal_wrong_state_rejected():
    redis = FakeRedis(decode_responses=True)
    await _register(redis)
    app = _make_app(redis)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await c.post("/webhook/agent/rt/commands/enqueue", json=_cmd())
        bad = await c.post("/webhook/agent/rt/commands/terminal",
                           json={"agent_id": AGENT, "tenant_id": TENANT, "command_id": "cmd-1",
                                 "state": "RUNNING", "outcome": {}})
        assert bad.status_code == 422
