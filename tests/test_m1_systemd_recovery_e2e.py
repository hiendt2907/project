"""M1 Product E2E — Incident→Decision→Approval→durable delivery→guarded execution→verify.

Chạy qua ASGITransport trên app FastAPI thật (Gateway agent_runtime router) + FakeRedis +
DeliveryLoop thật (agent side) + build_systemd_restart_executor thật — KHÔNG mutation OS
thật (FakeSystemd transport). Chứng minh toàn bộ vertical slice sản phẩm end-to-end,
KHÔNG chỉ unit test từng lớp riêng.
"""
from __future__ import annotations

import json
import time

import pytest
from fakeredis.aioredis import FakeRedis
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from aoip import audit
from aoip.agent.delivery_loop import DeliveryLoop
from aoip.agent.inbox import LocalInbox
from aoip.capabilities.systemd_restart import (
    MODE_SHADOW,
    SystemdRestartPolicy,
    build_systemd_restart_executor,
    build_typed_payload,
    issue_capability_command,
)
from aoip.objects import Finding
from aoip.recovery import RecoveryGate

TENANT = "acme"
AGENT = "agent-nginx-1"


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


def _gate():
    return RecoveryGate(allowed_failure_modes=frozenset({"process_down"}),
                        allowed_substrates=frozenset({"systemd"}), max_risk=0.5,
                        scope_prefix="svc:", min_diagnosis_confidence=0.0, max_diagnosis_age_s=1e9,
                        allowed_targets=frozenset({"nginx.service"}))


def _policy(*units):
    return SystemdRestartPolicy(allowed_units=frozenset(units))


class FakeSystemd:
    target = "h"

    def __init__(self, *, state="inactive", heal_on_restart=True):
        self.state = state
        self.heal_on_restart = heal_on_restart
        self.restarts = 0

    async def run(self, argv, *, timeout=15.0):
        cmd = " ".join(argv)
        if "LoadState" in cmd:
            return ("loaded\n", 0)
        if "restart" in cmd:
            self.restarts += 1
            if self.heal_on_restart:
                self.state = "active"
            return ("", 0)
        if "is-active" in cmd:
            return (self.state + "\n", 0 if self.state == "active" else 3)
        return ("", 0)


def _incident_command(*, unit="nginx.service", approved=True, ttl_s=300):
    """Simula Incident→Mission→Decision→Approval: xây typed payload + approval binding."""
    typed = build_typed_payload(mission_id="mis-nginx-1", decision_id="dec-restart-1",
                                incident_id="inc-nginx-down", summary="nginx DOWN (probe failed)",
                                unit=unit)
    findings = (Finding(claim=f"svc:{unit} is DOWN (probe failed)", references=("probe-1",),
                       verdict=True, confidence=0.95),)
    now = time.time()
    cmd = issue_capability_command(typed_payload=typed, approver="alice", tenant=TENANT,
                                   issued_at=now, expires_at=now + ttl_s,
                                   findings=findings, diagnosis_confidence=0.9)
    if not approved:
        cmd["approval"]["approved"] = False
    return cmd, ttl_s


async def _enqueue(client, cmd, *, unit, ttl_s):
    resp = await client.post("/webhook/agent/rt/commands/enqueue", json={
        "command_id": f"cmd-{unit}", "agent_id": AGENT, "tenant_id": TENANT,
        "mission_id": cmd["reason"]["mission_id"], "incident_id": cmd["reason"]["incident_id"],
        "decision_id": cmd["reason"]["decision_id"], "action_id": cmd["approval"]["action_id"],
        "canonical_scope": cmd["approval"]["canonical_scope"],
        "payload_hash": cmd["approved_payload_hash"], "payload": cmd, "ttl_s": ttl_s})
    assert resp.status_code == 200
    return resp.json()


class _RTOmniClient:
    """Adapter tối thiểu — HTTPOmniClient thật gọi qua httpx; ở đây bọc AsyncClient
    (ASGITransport) trực tiếp để test KHÔNG cần server thật."""

    def __init__(self, client: AsyncClient) -> None:
        self._c = client

    async def poll_runtime(self, agent_id):
        r = await self._c.get(f"/webhook/agent/rt/commands/{agent_id}")
        r.raise_for_status()
        return r.json().get("commands", [])

    async def accept(self, agent_id, tenant_id, command_id, *, delivery_attempt, fencing_token):
        await self._c.post("/webhook/agent/rt/commands/accept",
                           json={"agent_id": agent_id, "tenant_id": tenant_id,
                                "command_id": command_id, "delivery_attempt": delivery_attempt,
                                "fencing_token": fencing_token})

    async def progress(self, agent_id, tenant_id, command_id, phase, *,
                       delivery_attempt, fencing_token):
        await self._c.post("/webhook/agent/rt/commands/progress",
                           json={"agent_id": agent_id, "tenant_id": tenant_id,
                                "command_id": command_id, "phase": phase,
                                "delivery_attempt": delivery_attempt, "fencing_token": fencing_token})

    async def report_terminal(self, agent_id, tenant_id, command_id, state, outcome, *,
                              delivery_attempt, fencing_token):
        r = await self._c.post("/webhook/agent/rt/commands/terminal",
                               json={"agent_id": agent_id, "tenant_id": tenant_id,
                                    "command_id": command_id, "state": state, "outcome": outcome,
                                    "delivery_attempt": delivery_attempt,
                                    "fencing_token": fencing_token})
        if r.status_code == 409:
            return {"acknowledged": False, "conflict": True, "error": r.json().get("error")}
        r.raise_for_status()
        return r.json()


@pytest.mark.asyncio
async def test_happy_path_incident_to_verified_recovery(tmp_path):
    """Incident → approved command → durable delivery → guarded execution → verified recovery."""
    redis = FakeRedis(decode_responses=True)
    await _register(redis)
    app = _make_app(redis)
    unit = "nginx.service"
    cmd, ttl_s = _incident_command(unit=unit)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        # Before approval there is no enqueue at all — no command exists to poll.
        pre = await client.get(f"/webhook/agent/rt/commands/{AGENT}")
        assert pre.json()["commands"] == []

        await _enqueue(client, cmd, unit=unit, ttl_s=ttl_s)

        rt_client = _RTOmniClient(client)
        transport = FakeSystemd(state="inactive", heal_on_restart=True)
        executor = await build_systemd_restart_executor(
            redis=redis, holder=AGENT, transport=transport,
            audit_log=audit.FileAuditLog(tmp_path / "audit.jsonl"), gate=_gate(),
            policy=_policy(unit), tenant=TENANT)
        loop = DeliveryLoop(agent_id=AGENT, client=rt_client, inbox=LocalInbox(str(tmp_path / "inbox")),
                            executor=executor)

        processed = await loop.tick()
        assert processed == 1
        assert transport.restarts == 1

        rec = await client.get(f"/webhook/agent/rt/commands/record/{TENANT}/cmd-{unit}")
        body = rec.json()
        assert body["state"] == "COMPLETED"
        assert body["outcome"]["product_outcome"] == "EXECUTED_AND_VERIFIED"

        # Audit correlation IDs present end to end.
        events = audit.FileAuditLog(tmp_path / "audit.jsonl").events()
        assert "RECOVERY_COMPLETED" in events


@pytest.mark.asyncio
async def test_approval_rejected_never_executes(tmp_path):
    redis = FakeRedis(decode_responses=True)
    await _register(redis)
    app = _make_app(redis)
    unit = "nginx.service"
    cmd, ttl_s = _incident_command(unit=unit, approved=False)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        await _enqueue(client, cmd, unit=unit, ttl_s=ttl_s)
        rt_client = _RTOmniClient(client)
        transport = FakeSystemd(state="inactive")
        executor = await build_systemd_restart_executor(
            redis=redis, holder=AGENT, transport=transport,
            audit_log=audit.FileAuditLog(tmp_path / "audit.jsonl"), gate=_gate(),
            policy=_policy(unit), tenant=TENANT)
        loop = DeliveryLoop(agent_id=AGENT, client=rt_client, inbox=LocalInbox(str(tmp_path / "inbox")),
                            executor=executor)
        await loop.tick()
        assert transport.restarts == 0
        rec = await client.get(f"/webhook/agent/rt/commands/record/{TENANT}/cmd-{unit}")
        assert rec.json()["outcome"]["product_outcome"] == "APPROVAL_REJECTED"


@pytest.mark.asyncio
async def test_verification_failure_escalates(tmp_path):
    redis = FakeRedis(decode_responses=True)
    await _register(redis)
    app = _make_app(redis)
    unit = "nginx.service"
    cmd, ttl_s = _incident_command(unit=unit)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        await _enqueue(client, cmd, unit=unit, ttl_s=ttl_s)
        rt_client = _RTOmniClient(client)
        transport = FakeSystemd(state="inactive", heal_on_restart=False)  # restart KHÔNG heal
        executor = await build_systemd_restart_executor(
            redis=redis, holder=AGENT, transport=transport,
            audit_log=audit.FileAuditLog(tmp_path / "audit.jsonl"), gate=_gate(),
            policy=_policy(unit), tenant=TENANT)
        loop = DeliveryLoop(agent_id=AGENT, client=rt_client, inbox=LocalInbox(str(tmp_path / "inbox")),
                            executor=executor)
        await loop.tick()
        rec = await client.get(f"/webhook/agent/rt/commands/record/{TENANT}/cmd-{unit}")
        body = rec.json()
        assert body["state"] == "ESCALATED"
        assert body["outcome"]["product_outcome"] == "VERIFICATION_FAILED"


@pytest.mark.asyncio
async def test_shadow_mode_end_to_end_no_mutation(tmp_path):
    redis = FakeRedis(decode_responses=True)
    await _register(redis)
    app = _make_app(redis)
    unit = "nginx.service"
    cmd, ttl_s = _incident_command(unit=unit)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        await _enqueue(client, cmd, unit=unit, ttl_s=ttl_s)
        rt_client = _RTOmniClient(client)
        transport = FakeSystemd(state="inactive")
        executor = await build_systemd_restart_executor(
            redis=redis, holder=AGENT, transport=transport,
            audit_log=audit.FileAuditLog(tmp_path / "audit.jsonl"), gate=_gate(),
            policy=_policy(unit), tenant=TENANT, mode=MODE_SHADOW)
        loop = DeliveryLoop(agent_id=AGENT, client=rt_client, inbox=LocalInbox(str(tmp_path / "inbox")),
                            executor=executor)
        await loop.tick()
        assert transport.restarts == 0
        rec = await client.get(f"/webhook/agent/rt/commands/record/{TENANT}/cmd-{unit}")
        body = rec.json()
        assert body["outcome"]["product_outcome"] == "SHADOW_RECOMMENDATION"
        assert "would_execute" in body["outcome"]["evidence"]


@pytest.mark.asyncio
async def test_unit_not_allowlisted_end_to_end(tmp_path):
    redis = FakeRedis(decode_responses=True)
    await _register(redis)
    app = _make_app(redis)
    unit = "nginx.service"
    cmd, ttl_s = _incident_command(unit=unit)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        await _enqueue(client, cmd, unit=unit, ttl_s=ttl_s)
        rt_client = _RTOmniClient(client)
        transport = FakeSystemd(state="inactive")
        executor = await build_systemd_restart_executor(
            redis=redis, holder=AGENT, transport=transport,
            audit_log=audit.FileAuditLog(tmp_path / "audit.jsonl"), gate=_gate(),
            policy=_policy("other.service"), tenant=TENANT)  # nginx.service KHÔNG allowlisted
        loop = DeliveryLoop(agent_id=AGENT, client=rt_client, inbox=LocalInbox(str(tmp_path / "inbox")),
                            executor=executor)
        await loop.tick()
        assert transport.restarts == 0
        rec = await client.get(f"/webhook/agent/rt/commands/record/{TENANT}/cmd-{unit}")
        assert rec.json()["outcome"]["product_outcome"] == "BLOCKED_BY_POLICY"
