"""Tests: Living Operations Runtime slice 1 — idempotency + lease + bounded approval.

Phủ thuộc tính an toàn epic: (2) giao trùng → mutate 1 lần; (3) crash-after-mutation
reconcile; (5) approval hết hạn → zero mutation; (6) sai tenant/scope → zero mutation;
(7) hai agent cùng target, chỉ lease-holder execute; (9-mạng) lỗi I/O không mutate mù.
Dùng FakeRedis async (real-redis-shaped) theo convention dự án.
"""
from __future__ import annotations

import fakeredis.aioredis as aioredis
import pytest

from aoip import audit
from aoip.agent.idempotency import IdempotencyLedger, idempotency_key
from aoip.agent.lease import ExecutionLease
from aoip.agent.operations import operations_loop, run_guarded_recovery
from aoip.objects import ActionState, Finding
from aoip.recovery import Approval, RecoveryGate, RecoveryRequest, plan_recovery

NOW = 1000.0


def _redis():
    return aioredis.FakeRedis(decode_responses=True)


class FakeSystemd:
    target = "h"

    def __init__(self, *, state="inactive", heal_on_restart=True):
        self.state = state
        self.heal_on_restart = heal_on_restart
        self.restarts = 0

    async def run(self, argv, *, timeout=15.0):
        cmd = " ".join(argv)
        if "restart" in cmd:
            self.restarts += 1
            if self.heal_on_restart:
                self.state = "active"
            return ("", 0)
        if "is-active" in cmd:
            return (self.state + "\n", 0 if self.state == "active" else 3)
        if "ActiveEnterTimestamp" in cmd:
            return ("Mon 2026-06-30\n", 0)
        if "/dev/tcp" in cmd:
            return ("OPEN\n" if self.state == "active" else "", 0)
        return ("", 0)


class FakeCtx:
    def __init__(self):
        self.diagnosis_confidence = 0.787
        self.findings = [
            Finding(claim="svc:db is DOWN (probe failed)", references=("i",), verdict=True, confidence=0.95),
            Finding(claim="svc:db: process_down", references=("d",), verdict=True, confidence=0.9),
        ]
        self.trace = []

    def log(self, verb, detail):
        self.trace.append(f"{verb}: {detail}")


def _gate():
    return RecoveryGate(allowed_failure_modes=frozenset({"process_down"}),
                        allowed_substrates=frozenset({"systemd"}), max_risk=0.5,
                        scope_prefix="svc:", min_diagnosis_confidence=0.3, max_diagnosis_age_s=300.0)


def _req(tenant="acme"):
    action = plan_recovery(failed_node="svc:db", failure_mode="process_down",
                           substrate="systemd", unit="redis-server", port=6379, risk=0.3)
    action = action.at(ActionState.APPROVED)
    return RecoveryRequest(failed_node="svc:db", failure_mode="process_down", substrate="systemd",
                           unit="redis-server", port=6379, action=action, risk=0.3,
                           diagnosed_at=NOW, dependents=(), tenant=tenant)


def _approval(req, *, approved=True, tenant="acme", expires_at=float("inf"), decision_goal=None):
    return Approval(approved=approved, approver="alice", action_scope=req.action.scope,
                    tenant=tenant, decision_goal=decision_goal or req.action.decision_goal,
                    expires_at=expires_at)


async def _guard(r, t, *, req, approval, audit_log, now=NOW, holder="agent-1"):
    return await run_guarded_recovery(
        FakeCtx(), req=req, transport=t, audit_log=audit_log, gate=_gate(), approval=approval,
        env_auto_execute=False, now=now, redis=r, holder=holder)


def _log(tmp_path, name="a.jsonl"):
    return audit.FileAuditLog(tmp_path / name)


# ── Idempotency primitive ────────────────────────────────────────────────────
async def test_idempotency_claim_once():
    r = _redis()
    led = IdempotencyLedger(r)
    k = idempotency_key(tenant="acme", scope="svc:db", decision_goal="recover:process_down",
                        failure_mode="process_down", unit="redis-server")
    assert await led.claim(k, holder="a1") is True
    assert await led.claim(k, holder="a2") is False  # đã claim → không claim lại


# ── Lease primitive ──────────────────────────────────────────────────────────
async def test_lease_single_writer():
    r = _redis()
    lease = ExecutionLease(r)
    t1 = await lease.acquire("svc:db", holder="a1")
    assert t1 is not None
    assert await lease.acquire("svc:db", holder="a2") is None  # đang giữ
    assert await lease.release("svc:db", token=t1) is True
    assert await lease.acquire("svc:db", holder="a2") is not None  # nhả rồi → giành được


async def test_lease_release_only_by_holder():
    r = _redis()
    lease = ExecutionLease(r)
    await lease.acquire("svc:db", holder="a1")
    assert await lease.release("svc:db", token="bogus") is False  # không phải holder


# ── (2) Duplicate delivery → mutate once ─────────────────────────────────────
async def test_duplicate_delivery_mutates_once(tmp_path):
    r = _redis()
    t = FakeSystemd(state="inactive", heal_on_restart=True)
    log = _log(tmp_path)
    req = _req()
    o1 = await _guard(r, t, req=req, approval=_approval(req), audit_log=log)
    o2 = await _guard(r, t, req=req, approval=_approval(req), audit_log=log)  # giao trùng
    assert o1.status == "recovered"
    assert o2.status == "recovered"            # reconcile, không lỗi
    assert t.restarts == 1                      # MUTATE ĐÚNG 1 LẦN
    assert audit.EV_RECOVERY_RECONCILED in log.events()


# ── (3) Crash after mutation → reconcile, zero mutation ──────────────────────
async def test_crash_after_mutation_reconciles(tmp_path):
    r = _redis()
    led = IdempotencyLedger(r)
    # Mô phỏng: agent đã claim + đã restart service (đang active) rồi CRASH trước record.
    k = idempotency_key(tenant="acme", scope="svc:db", decision_goal="recover:process_down",
                        failure_mode="process_down", unit="redis-server")
    await led.claim(k, holder="dead-agent")     # claim treo (chưa terminal)
    t = FakeSystemd(state="active")             # service THỰC TẾ đã khỏe (mutation cũ hiệu lực)
    req = _req()
    out = await _guard(r, t, req=req, approval=_approval(req), audit_log=_log(tmp_path), holder="agent-2")
    assert t.restarts == 0                       # KHÔNG mutate lại — reconcile theo current-state
    assert out.status == "aborted" and "HEALTHY" in out.reason


# ── (5) Approval expired → zero mutation ─────────────────────────────────────
async def test_expired_approval_zero_mutation(tmp_path):
    r = _redis()
    t = FakeSystemd(state="inactive")
    req = _req()
    appr = _approval(req, expires_at=NOW - 1)    # hết hạn trước now
    out = await _guard(r, t, req=req, approval=appr, audit_log=_log(tmp_path))
    assert out.status == "aborted" and t.restarts == 0


# ── (6) Wrong tenant / wrong scope → zero mutation ───────────────────────────
async def test_wrong_tenant_zero_mutation(tmp_path):
    r = _redis()
    t = FakeSystemd(state="inactive")
    req = _req(tenant="acme")
    appr = _approval(req, tenant="evil-corp")    # approval cho tenant khác
    out = await _guard(r, t, req=req, approval=appr, audit_log=_log(tmp_path))
    assert out.status == "aborted" and t.restarts == 0


async def test_wrong_decision_binding_zero_mutation(tmp_path):
    r = _redis()
    t = FakeSystemd(state="inactive")
    req = _req()
    appr = _approval(req, decision_goal="recover:disk_full")  # sai decision
    out = await _guard(r, t, req=req, approval=appr, audit_log=_log(tmp_path))
    assert out.status == "aborted" and t.restarts == 0


# ── (7) Two agents same target → only lease holder executes ──────────────────
async def test_two_agents_only_lease_holder_executes(tmp_path):
    r = _redis()
    lease = ExecutionLease(r)
    held = await lease.acquire("svc:db", holder="agent-1")  # agent-1 đang giữ lease
    assert held is not None
    t = FakeSystemd(state="inactive")
    req = _req()
    # agent-2 thử recover cùng scope nhưng KHÔNG có lease → zero mutation.
    out = await _guard(r, t, req=req, approval=_approval(req), audit_log=_log(tmp_path), holder="agent-2")
    assert out.status == "aborted" and t.restarts == 0
    assert "lease" in out.reason.lower()


# ── (mạng) Network errors never trigger blind mutation ───────────────────────
class FlakyAgent:
    def __init__(self, fail_io=True):
        self.fail_io = fail_io
        self.heartbeats = 0
        self.results = []

    async def register(self):
        if self.fail_io:
            raise ConnectionError("gateway down")

    async def heartbeat(self):
        if self.fail_io:
            raise TimeoutError("network timeout")
        self.heartbeats += 1

    async def pull_mission(self):
        return "investigate_incident"

    async def report_result(self, *, rc=0, stdout=""):
        self.results.append((rc, stdout))


async def test_network_timeout_no_blind_mutation():
    mutated = {"n": 0}

    async def build_request(goal):
        return _req()

    async def handle_request(req):
        mutated["n"] += 1  # nếu được gọi nghĩa là đã vào nhánh mutation
        from aoip.recovery import RecoveryOutcome
        return RecoveryOutcome(action=req.action, status="recovered", reason="x")

    agent = FlakyAgent(fail_io=True)
    n = await operations_loop(agent=agent, redis=_redis(), build_request=build_request,
                              handle_request=handle_request, sleep_s=0, max_iterations=3)
    assert n == 3
    assert mutated["n"] == 0  # lỗi mạng heartbeat/pull → KHÔNG bao giờ tới mutation


async def test_loop_happy_path_reports():
    async def build_request(goal):
        return _req()

    async def handle_request(req):
        from aoip.recovery import RecoveryOutcome
        return RecoveryOutcome(action=req.action, status="recovered", reason="done")

    agent = FlakyAgent(fail_io=False)
    await operations_loop(agent=agent, redis=_redis(), build_request=build_request,
                          handle_request=handle_request, sleep_s=0, max_iterations=2)
    assert agent.heartbeats == 2
    assert agent.results and agent.results[0][0] == 0
