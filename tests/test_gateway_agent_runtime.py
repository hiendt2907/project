"""Integration: durable runtime command delivery over the REAL ASGI gateway route.

Chạy qua ASGITransport trên chính app FastAPI + FakeRedis → gần nhất với "Gateway thật"
trong CI. Chứng minh GET=peek (fix P0) + máy trạng thái ack + terminal ack idempotent qua
HTTP contract thật. Proof trên K8s Gateway + VM thật ở scripts/deploy.

Atomic claim + fencing (delivery ownership): mỗi claim là MỘT Lua round-trip (không GET-
rồi-SET rời) — hai poller cùng agent/tenant chỉ một bên thắng một delivery attempt. Mọi
request sau delivery (accept/progress/terminal) phải khớp delivery_attempt + fencing_token
hiện tại của record; sai → 409 domain reason (stale_delivery_attempt/invalid_fencing_token/
version_conflict/terminal_outcome_conflict), KHÔNG silently accept.
"""
from __future__ import annotations

import asyncio
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


def _ack(command_id, attempt, token, **extra):
    return {"agent_id": AGENT, "tenant_id": TENANT, "command_id": command_id,
            "delivery_attempt": attempt, "fencing_token": token, **extra}


async def _force_visibility_expired(redis, tenant, agent_id, command_id) -> None:
    """Test-only: simulate visibility timeout elapsed without waiting real time.

    The claim script checks the record's OWN ``visibility_deadline`` field (not just the
    outer ready-set score), so both must be forced into the past.
    """
    from gateway.routes.agent_runtime import _ready_key, _rec_key

    await redis.zadd(_ready_key(tenant, agent_id), {command_id: 0})
    raw = await redis.get(_rec_key(tenant, command_id))
    rec = json.loads(raw)
    rec["visibility_deadline"] = 0
    await redis.set(_rec_key(tenant, command_id), json.dumps(rec))


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
        assert cmds[0]["delivery_attempt"] == 1
        assert cmds[0]["fencing_token"] == "cmd-1:1"
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
        polled = await c.get(f"/webhook/agent/rt/commands/{AGENT}")
        cmd = polled.json()["commands"][0]
        attempt, token = cmd["delivery_attempt"], cmd["fencing_token"]

        acc = await c.post("/webhook/agent/rt/commands/accept", json=_ack("cmd-1", attempt, token))
        assert acc.json()["state"] == "ACCEPTED"
        prog = await c.post("/webhook/agent/rt/commands/progress",
                            json=_ack("cmd-1", attempt, token, phase="RUNNING"))
        assert prog.json()["state"] == "RUNNING"
        term = await c.post("/webhook/agent/rt/commands/terminal",
                            json=_ack("cmd-1", attempt, token, state="COMPLETED", outcome={"rc": 0}))
        body = term.json()
        assert body["acknowledged"] is True and body["idempotent"] is False
        # terminal → không còn giao lại
        again = await c.get(f"/webhook/agent/rt/commands/{AGENT}")
        assert again.json()["commands"] == []
        # report lại cùng attempt/token/outcome → ack idempotent
        dup = await c.post("/webhook/agent/rt/commands/terminal",
                           json=_ack("cmd-1", attempt, token, state="COMPLETED", outcome={"rc": 0}))
        assert dup.json()["idempotent"] is True
        rec = await c.get(f"/webhook/agent/rt/commands/record/{TENANT}/cmd-1")
        assert rec.json()["outcome"] == {"rc": 0}


@pytest.mark.asyncio
async def test_terminal_different_outcome_after_terminal_is_conflict_not_overwrite():
    redis = FakeRedis(decode_responses=True)
    await _register(redis)
    app = _make_app(redis)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await c.post("/webhook/agent/rt/commands/enqueue", json=_cmd())
        cmd = (await c.get(f"/webhook/agent/rt/commands/{AGENT}")).json()["commands"][0]
        attempt, token = cmd["delivery_attempt"], cmd["fencing_token"]
        await c.post("/webhook/agent/rt/commands/terminal",
                     json=_ack("cmd-1", attempt, token, state="COMPLETED", outcome={"rc": 0}))
        conflict = await c.post("/webhook/agent/rt/commands/terminal",
                                json=_ack("cmd-1", attempt, token, state="COMPLETED",
                                          outcome={"rc": 999}))
        assert conflict.status_code == 409
        assert conflict.json()["error"] == "terminal_outcome_conflict"
        rec = await c.get(f"/webhook/agent/rt/commands/record/{TENANT}/cmd-1")
        assert rec.json()["outcome"] == {"rc": 0}  # KHÔNG bị ghi đè


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
async def test_expired_delivered_command_not_redelivered():
    """Command đã DELIVERED, hết hạn trước khi visibility timeout → EXPIRED, fail-closed."""
    redis = FakeRedis(decode_responses=True)
    await _register(redis)
    app = _make_app(redis)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await c.post("/webhook/agent/rt/commands/enqueue", json=_cmd(ttl_s=1))
        first = await c.get(f"/webhook/agent/rt/commands/{AGENT}")
        assert first.json()["commands"] != []  # claimed once (DELIVERED, not yet expired)
        time.sleep(1.2)
        # force visibility to be due immediately for redelivery check
        from gateway.routes.agent_runtime import _ready_key
        await redis.zadd(_ready_key(TENANT, AGENT), {"cmd-1": 0})
        again = await c.get(f"/webhook/agent/rt/commands/{AGENT}")
        assert again.json()["commands"] == []
        rec = await c.get(f"/webhook/agent/rt/commands/record/{TENANT}/cmd-1")
        assert rec.json()["state"] == "EXPIRED"


@pytest.mark.asyncio
async def test_terminal_wrong_state_rejected():
    redis = FakeRedis(decode_responses=True)
    await _register(redis)
    app = _make_app(redis)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await c.post("/webhook/agent/rt/commands/enqueue", json=_cmd())
        bad = await c.post("/webhook/agent/rt/commands/terminal",
                           json=_ack("cmd-1", 1, "cmd-1:1", state="RUNNING", outcome={}))
        assert bad.status_code == 422


# ── Atomic claim / concurrency ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_two_concurrent_pollers_only_one_claims_the_attempt():
    redis = FakeRedis(decode_responses=True)
    await _register(redis)
    app = _make_app(redis)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await c.post("/webhook/agent/rt/commands/enqueue", json=_cmd())
        r1, r2 = await asyncio.gather(
            c.get(f"/webhook/agent/rt/commands/{AGENT}"),
            c.get(f"/webhook/agent/rt/commands/{AGENT}"),
        )
        claims = [c_ for r in (r1, r2) for c_ in r.json()["commands"]]
        assert len(claims) == 1  # chỉ một poller thắng attempt này
        assert claims[0]["delivery_attempt"] == 1
        rec = await c.get(f"/webhook/agent/rt/commands/record/{TENANT}/cmd-1")
        assert rec.json()["delivery_attempt"] == 1  # KHÔNG double-increment
        assert rec.json()["delivery_count"] == 1


@pytest.mark.asyncio
async def test_redelivery_after_visibility_timeout_bumps_attempt_and_token():
    redis = FakeRedis(decode_responses=True)
    await _register(redis)
    app = _make_app(redis)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await c.post("/webhook/agent/rt/commands/enqueue", json=_cmd())
        first = (await c.get(f"/webhook/agent/rt/commands/{AGENT}")).json()["commands"][0]
        assert first["delivery_attempt"] == 1

        # simulate visibility timeout elapsed: force both the ready-set score AND the
        # record's own visibility_deadline (the claim script checks the record field, not
        # just the outer index) into the past.
        await _force_visibility_expired(redis, TENANT, AGENT, "cmd-1")

        second = (await c.get(f"/webhook/agent/rt/commands/{AGENT}")).json()["commands"][0]
        assert second["delivery_attempt"] == 2
        assert second["fencing_token"] != first["fencing_token"]
        assert second["fencing_token"] == "cmd-1:2"


@pytest.mark.asyncio
async def test_stale_attempt_ack_rejected_after_redelivery():
    redis = FakeRedis(decode_responses=True)
    await _register(redis)
    app = _make_app(redis)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await c.post("/webhook/agent/rt/commands/enqueue", json=_cmd())
        first = (await c.get(f"/webhook/agent/rt/commands/{AGENT}")).json()["commands"][0]
        old_attempt, old_token = first["delivery_attempt"], first["fencing_token"]

        await _force_visibility_expired(redis, TENANT, AGENT, "cmd-1")
        await c.get(f"/webhook/agent/rt/commands/{AGENT}")  # redelivered → attempt 2

        stale_accept = await c.post("/webhook/agent/rt/commands/accept",
                                    json=_ack("cmd-1", old_attempt, old_token))
        assert stale_accept.status_code == 409
        assert stale_accept.json()["error"] == "stale_delivery_attempt"

        stale_progress = await c.post("/webhook/agent/rt/commands/progress",
                                      json=_ack("cmd-1", old_attempt, old_token, phase="RUNNING"))
        assert stale_progress.status_code == 409
        assert stale_progress.json()["error"] == "stale_delivery_attempt"

        stale_terminal = await c.post("/webhook/agent/rt/commands/terminal",
                                      json=_ack("cmd-1", old_attempt, old_token,
                                                state="COMPLETED", outcome={"rc": 0}))
        assert stale_terminal.status_code == 409
        assert stale_terminal.json()["error"] == "stale_delivery_attempt"
        # record vẫn DELIVERED (attempt 2), KHÔNG bị attempt cũ ghi đè thành COMPLETED
        rec = await c.get(f"/webhook/agent/rt/commands/record/{TENANT}/cmd-1")
        assert rec.json()["state"] == "DELIVERED"
        assert rec.json()["delivery_attempt"] == 2


@pytest.mark.asyncio
async def test_invalid_fencing_token_same_attempt_rejected():
    redis = FakeRedis(decode_responses=True)
    await _register(redis)
    app = _make_app(redis)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await c.post("/webhook/agent/rt/commands/enqueue", json=_cmd())
        cmd = (await c.get(f"/webhook/agent/rt/commands/{AGENT}")).json()["commands"][0]
        bad = await c.post("/webhook/agent/rt/commands/accept",
                           json=_ack("cmd-1", cmd["delivery_attempt"], "totally-wrong-token"))
        assert bad.status_code == 409
        assert bad.json()["error"] == "invalid_fencing_token"


@pytest.mark.asyncio
async def test_version_conflict_rejected():
    redis = FakeRedis(decode_responses=True)
    await _register(redis)
    app = _make_app(redis)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await c.post("/webhook/agent/rt/commands/enqueue", json=_cmd())
        cmd = (await c.get(f"/webhook/agent/rt/commands/{AGENT}")).json()["commands"][0]
        bad = await c.post("/webhook/agent/rt/commands/accept",
                           json=_ack("cmd-1", cmd["delivery_attempt"], cmd["fencing_token"],
                                    expected_version=999))
        assert bad.status_code == 409
        assert bad.json()["error"] == "version_conflict"


@pytest.mark.asyncio
async def test_idempotent_retry_same_attempt_token_version_after_success():
    redis = FakeRedis(decode_responses=True)
    await _register(redis)
    app = _make_app(redis)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await c.post("/webhook/agent/rt/commands/enqueue", json=_cmd())
        cmd = (await c.get(f"/webhook/agent/rt/commands/{AGENT}")).json()["commands"][0]
        attempt, token = cmd["delivery_attempt"], cmd["fencing_token"]
        first = await c.post("/webhook/agent/rt/commands/accept", json=_ack("cmd-1", attempt, token))
        assert first.json()["state"] == "ACCEPTED"
        retry = await c.post("/webhook/agent/rt/commands/accept", json=_ack("cmd-1", attempt, token))
        assert retry.status_code == 200
        assert retry.json()["state"] == "ACCEPTED"  # idempotent, không lỗi


@pytest.mark.asyncio
async def test_tenant_mismatch_with_valid_token_rejected():
    redis = FakeRedis(decode_responses=True)
    await _register(redis)
    app = _make_app(redis)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await c.post("/webhook/agent/rt/commands/enqueue", json=_cmd())
        cmd = (await c.get(f"/webhook/agent/rt/commands/{AGENT}")).json()["commands"][0]
        wrong_tenant = await c.post(
            "/webhook/agent/rt/commands/accept",
            json={"agent_id": AGENT, "tenant_id": "other-tenant", "command_id": "cmd-1",
                  "delivery_attempt": cmd["delivery_attempt"], "fencing_token": cmd["fencing_token"]})
        # tenant khác → record không tồn tại dưới namespace đó (isolation) → 404, KHÔNG 200
        assert wrong_tenant.status_code == 404


@pytest.mark.asyncio
async def test_agent_mismatch_with_valid_tenant_token_rejected():
    redis = FakeRedis(decode_responses=True)
    await _register(redis)
    app = _make_app(redis)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await c.post("/webhook/agent/rt/commands/enqueue", json=_cmd())
        cmd = (await c.get(f"/webhook/agent/rt/commands/{AGENT}")).json()["commands"][0]
        other_agent_registered = await redis.set(
            f"omni:remote_agent:registry:agent-other",
            json.dumps({"agent_id": "agent-other", "tenant_id": TENANT, "last_seen": int(time.time())}))
        wrong_agent = await c.post(
            "/webhook/agent/rt/commands/accept",
            json={"agent_id": "agent-other", "tenant_id": TENANT, "command_id": "cmd-1",
                  "delivery_attempt": cmd["delivery_attempt"], "fencing_token": cmd["fencing_token"]})
        assert wrong_agent.status_code == 409
        assert wrong_agent.json()["error"] == "agent_mismatch"
        rec = await c.get(f"/webhook/agent/rt/commands/record/{TENANT}/cmd-1")
        assert rec.json()["agent_id"] == AGENT  # record identity không đổi theo agent giả mạo
        assert rec.json()["state"] == "DELIVERED"  # KHÔNG advance sang ACCEPTED


# ── Visibility heartbeat ─────────────────────────────────────────────────────

async def _to_running(c, attempt, token):
    await c.post("/webhook/agent/rt/commands/accept", json=_ack("cmd-1", attempt, token))
    await c.post("/webhook/agent/rt/commands/progress",
                 json=_ack("cmd-1", attempt, token, phase="RUNNING"))


@pytest.mark.asyncio
async def test_heartbeat_extends_visibility_while_running():
    redis = FakeRedis(decode_responses=True)
    await _register(redis)
    app = _make_app(redis)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await c.post("/webhook/agent/rt/commands/enqueue", json=_cmd())
        cmd = (await c.get(f"/webhook/agent/rt/commands/{AGENT}")).json()["commands"][0]
        attempt, token = cmd["delivery_attempt"], cmd["fencing_token"]
        await _to_running(c, attempt, token)
        before = (await c.get(f"/webhook/agent/rt/commands/record/{TENANT}/cmd-1")).json()

        hb = await c.post("/webhook/agent/rt/commands/heartbeat", json=_ack("cmd-1", attempt, token))
        assert hb.status_code == 200
        after = hb.json()
        assert after["visibility_deadline"] >= before["visibility_deadline"]
        rec = (await c.get(f"/webhook/agent/rt/commands/record/{TENANT}/cmd-1")).json()
        assert rec["delivery_attempt"] == attempt  # KHÔNG đổi attempt
        assert rec["fencing_token"] == token       # KHÔNG cấp token mới
        assert rec["state"] == "RUNNING"


@pytest.mark.asyncio
async def test_heartbeat_stale_attempt_rejected():
    redis = FakeRedis(decode_responses=True)
    await _register(redis)
    app = _make_app(redis)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await c.post("/webhook/agent/rt/commands/enqueue", json=_cmd())
        first = (await c.get(f"/webhook/agent/rt/commands/{AGENT}")).json()["commands"][0]
        old_attempt, old_token = first["delivery_attempt"], first["fencing_token"]
        await _force_visibility_expired(redis, TENANT, AGENT, "cmd-1")
        await c.get(f"/webhook/agent/rt/commands/{AGENT}")  # attempt 2

        hb = await c.post("/webhook/agent/rt/commands/heartbeat",
                          json=_ack("cmd-1", old_attempt, old_token))
        assert hb.status_code == 409
        assert hb.json()["error"] == "stale_delivery_attempt"


@pytest.mark.asyncio
async def test_heartbeat_wrong_fencing_token_rejected():
    redis = FakeRedis(decode_responses=True)
    await _register(redis)
    app = _make_app(redis)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await c.post("/webhook/agent/rt/commands/enqueue", json=_cmd())
        cmd = (await c.get(f"/webhook/agent/rt/commands/{AGENT}")).json()["commands"][0]
        hb = await c.post("/webhook/agent/rt/commands/heartbeat",
                          json=_ack("cmd-1", cmd["delivery_attempt"], "wrong-token"))
        assert hb.status_code == 409
        assert hb.json()["error"] == "invalid_fencing_token"


@pytest.mark.asyncio
async def test_heartbeat_terminal_command_rejected():
    redis = FakeRedis(decode_responses=True)
    await _register(redis)
    app = _make_app(redis)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await c.post("/webhook/agent/rt/commands/enqueue", json=_cmd())
        cmd = (await c.get(f"/webhook/agent/rt/commands/{AGENT}")).json()["commands"][0]
        attempt, token = cmd["delivery_attempt"], cmd["fencing_token"]
        await c.post("/webhook/agent/rt/commands/terminal",
                     json=_ack("cmd-1", attempt, token, state="COMPLETED", outcome={"rc": 0}))
        hb = await c.post("/webhook/agent/rt/commands/heartbeat", json=_ack("cmd-1", attempt, token))
        assert hb.status_code == 409
        assert hb.json()["error"] == "terminal_no_heartbeat"


@pytest.mark.asyncio
async def test_heartbeat_expired_command_rejected():
    redis = FakeRedis(decode_responses=True)
    await _register(redis)
    app = _make_app(redis)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await c.post("/webhook/agent/rt/commands/enqueue", json=_cmd(ttl_s=1))
        cmd = (await c.get(f"/webhook/agent/rt/commands/{AGENT}")).json()["commands"][0]
        attempt, token = cmd["delivery_attempt"], cmd["fencing_token"]
        time.sleep(1.2)
        hb = await c.post("/webhook/agent/rt/commands/heartbeat", json=_ack("cmd-1", attempt, token))
        assert hb.status_code == 409
        assert hb.json()["error"] == "expired"
        rec = (await c.get(f"/webhook/agent/rt/commands/record/{TENANT}/cmd-1")).json()
        assert rec["state"] == "EXPIRED"


@pytest.mark.asyncio
async def test_heartbeat_before_running_rejected():
    """DELIVERED (chưa accept) hoặc ACCEPTED (chưa RUNNING) không được heartbeat."""
    redis = FakeRedis(decode_responses=True)
    await _register(redis)
    app = _make_app(redis)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await c.post("/webhook/agent/rt/commands/enqueue", json=_cmd())
        cmd = (await c.get(f"/webhook/agent/rt/commands/{AGENT}")).json()["commands"][0]
        attempt, token = cmd["delivery_attempt"], cmd["fencing_token"]
        hb = await c.post("/webhook/agent/rt/commands/heartbeat", json=_ack("cmd-1", attempt, token))
        assert hb.status_code == 409
        assert hb.json()["error"] == "not_running"


@pytest.mark.asyncio
async def test_heartbeat_version_conflict_rejected():
    redis = FakeRedis(decode_responses=True)
    await _register(redis)
    app = _make_app(redis)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await c.post("/webhook/agent/rt/commands/enqueue", json=_cmd())
        cmd = (await c.get(f"/webhook/agent/rt/commands/{AGENT}")).json()["commands"][0]
        attempt, token = cmd["delivery_attempt"], cmd["fencing_token"]
        await _to_running(c, attempt, token)
        hb = await c.post("/webhook/agent/rt/commands/heartbeat",
                          json=_ack("cmd-1", attempt, token, expected_version=999))
        assert hb.status_code == 409
        assert hb.json()["error"] == "version_conflict"
