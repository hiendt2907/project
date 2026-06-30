"""Live Operations Runtime (slice 1) — idempotency + lease + bounded approval THẬT.

    python -m aoip.live_operations

Chạy trên Redis THẬT qua TCP (localhost:6379) + VM THẬT (cust-db). Chứng minh các
thuộc tính an toàn của Living Operations Runtime trên hạ tầng thật:
  (1) approved recovery hoàn tất;
  (2) command giao HAI LẦN nhưng mutate ĐÚNG MỘT LẦN (idempotency);
  (3) crash-after-mutation → restart reconcile, zero mutation mới;
  (5) approval hết hạn → zero mutation;
  (6) sai tenant → zero mutation;
  (7) hai agent cùng target, chỉ lease-holder execute.

Thuộc tính (4) gateway-unavailable và agent systemd sống-lâu là slice kế (cần vòng
enrollment qua Gateway thật) — slice này khóa nền AN TOÀN trước.
"""
from __future__ import annotations

import asyncio

import redis.asyncio as aioredis

from aoip import audit
from aoip.agent.idempotency import IdempotencyLedger, idempotency_key
from aoip.agent.lease import ExecutionLease
from aoip.agent.operations import run_guarded_recovery
from aoip.capability import CapabilityState
from aoip.objects import ActionState
from aoip.recovery import Approval, RecoveryGate, RecoveryRequest, plan_recovery
from aoip.remote_linux_backend import RemoteLinuxBackend
from aoip.system_graph import infer_edges
from aoip.system_model import SystemModel
from aoip.transport import OrbTransport
from aoip.understanding import UnderstandingContext

AUDIT_PATH = "/tmp/aoip_operations_audit.jsonl"
NOW = 50_000.0
TENANT = "acme"


def _graph():
    edges = infer_edges([{"source": "payment-api", "relation": "depends_on", "target": "cust-db"}])
    return SystemModel(scope="acme").fold(*edges)


def _gate():
    return RecoveryGate(allowed_failure_modes=frozenset({"process_down"}),
                        allowed_substrates=frozenset({"systemd"}), max_risk=0.5,
                        scope_prefix="svc:", min_diagnosis_confidence=0.3, max_diagnosis_age_s=300.0)


def _ctx(backend):
    from aoip.objects import Finding
    ctx = UnderstandingContext(host="cust-db", scope="acme/cust-db", backend=backend,
                               capability=CapabilityState(capability_id="recover", scope="acme/cust-db"),
                               model=_graph())
    ctx.diagnosis_confidence = 0.787
    ctx.findings.append(Finding(claim="svc:cust-db is DOWN (probe failed)", references=("i",),
                                verdict=True, confidence=0.95))
    ctx.findings.append(Finding(claim="svc:cust-db: process_down", references=("d",),
                                verdict=True, confidence=0.9))
    return ctx


def _req(tenant=TENANT):
    action = plan_recovery(failed_node="svc:cust-db", failure_mode="process_down",
                           substrate="systemd", unit="redis-server", port=6379, risk=0.3)
    action = action.at(ActionState.APPROVED)
    return RecoveryRequest(failed_node="svc:cust-db", failure_mode="process_down", substrate="systemd",
                           unit="redis-server", port=6379, action=action, risk=0.3,
                           diagnosed_at=NOW, dependents=(), tenant=tenant)


def _appr(req, *, approved=True, tenant=TENANT, expires_at=float("inf"), decision_goal=None):
    return Approval(approved=approved, approver="human:on-call", action_scope=req.action.scope,
                    tenant=tenant, decision_goal=decision_goal or req.action.decision_goal,
                    expires_at=expires_at)


async def _orb(vm, *argv):
    p = await asyncio.create_subprocess_exec("orb", "-m", vm, *argv,
                                             stdout=asyncio.subprocess.DEVNULL,
                                             stderr=asyncio.subprocess.DEVNULL)
    await p.communicate()


async def run():
    r = aioredis.from_url("redis://localhost:6379", decode_responses=True)
    backend = RemoteLinuxBackend(OrbTransport("cust-db"))
    gate = _gate()
    log = audit.FileAuditLog(AUDIT_PATH)

    async def probe():
        return await backend.probe_port("cust-db", 6379)

    async def clean_keys():
        k = idempotency_key(tenant=TENANT, scope="recover_service:svc:cust-db",
                            decision_goal="recover:process_down",
                            failure_mode="process_down", unit="redis-server")
        await r.delete(k, "lease:svc:cust-db")

    async def guard(req, appr, *, holder="agent-1", now=NOW):
        return await run_guarded_recovery(
            _ctx(backend), req=req, transport=backend._t, audit_log=log, gate=gate,
            approval=appr, env_auto_execute=False, now=now, redis=r, holder=holder)

    print("=== LIVE OPERATIONS RUNTIME slice 1 — Redis TCP thật + VM thật ===\n")
    print(f"[redis] ping = {await r.ping()}")

    # (1)+(2) approved recovery + duplicate delivery → mutate once
    await clean_keys()
    await _orb("cust-db", "sudo", "systemctl", "stop", "redis-server"); await asyncio.sleep(1)
    print(f"\n[chaos] redis DOWN? {not await probe()}")
    req = _req()
    o1 = await guard(req, _appr(req))
    o2 = await guard(req, _appr(req))  # GIAO TRÙNG y hệt
    print(f"(1) approved recovery     → {o1.status}")
    print(f"(2) duplicate delivery    → {o2.status}  (reconcile, KHÔNG restart lại)")
    print(f"    redis reachable lại?  {await probe()}")

    # (3) crash-after-mutation reconcile: claim treo + service đã healthy
    await clean_keys()
    k = idempotency_key(tenant=TENANT, scope="recover_service:svc:cust-db",
                        decision_goal="recover:process_down", failure_mode="process_down",
                        unit="redis-server")
    await IdempotencyLedger(r).claim(k, holder="dead-agent")  # claim treo (agent đã chết)
    # service đang chạy (mutation cũ đã hiệu lực) → reconcile, zero mutation
    o3 = await guard(_req(), _appr(_req()), holder="agent-restarted")
    print(f"\n(3) crash-after-mutation  → {o3.status} ({'zero mutation' if 'HEALTHY' in o3.reason or 'idempotent' in o3.reason else o3.reason[:40]})")

    # (5) approval expired → zero mutation
    await clean_keys()
    await _orb("cust-db", "sudo", "systemctl", "stop", "redis-server"); await asyncio.sleep(1)
    req5 = _req()
    o5 = await guard(req5, _appr(req5, expires_at=NOW - 1))
    print(f"\n(5) approval expired      → {o5.status}; redis vẫn DOWN? {not await probe()}")

    # (6) wrong tenant → zero mutation
    await clean_keys()
    req6 = _req()
    o6 = await guard(req6, _appr(req6, tenant="evil-corp"))
    print(f"(6) wrong tenant          → {o6.status}; redis vẫn DOWN? {not await probe()}")

    # (7) two agents same target → only lease holder executes
    await clean_keys()
    held = await ExecutionLease(r).acquire("svc:cust-db", holder="agent-1")
    req7 = _req()
    o7 = await guard(req7, _appr(req7), holder="agent-2")  # agent-2 không có lease
    print(f"(7) 2 agents, non-holder  → {o7.status} ({o7.reason[:38]}); redis vẫn DOWN? {not await probe()}")
    await ExecutionLease(r).release("svc:cust-db", token=held)

    # cleanup: khôi phục redis
    await _orb("cust-db", "sudo", "systemctl", "start", "redis-server"); await asyncio.sleep(1)
    await clean_keys()
    print(f"\n[cleanup] redis reachable? {await probe()}")
    print(f"[audit] chain verified={log.verify_chain()}  events={log.events()[-8:]}")
    await r.aclose()


if __name__ == "__main__":
    asyncio.run(run())
