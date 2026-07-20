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
from aoip.agent.idempotency import (
    STATUS_MUTATION_STARTED,
    IdempotencyLedger,
    command_identity,
    idempotency_key,
    payload_hash,
)
from aoip.agent.lease import ExecutionLease
from aoip.agent.operations import (
    build_recovery_executor,
    decode_recovery_command,
    operations_loop,
    run_guarded_recovery,
)
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


def test_command_identity_separates_incidents_with_same_plan():
    first = _req()
    second = RecoveryRequest(**{**first.__dict__, "incident_id": "incident-2",
                                "mission_id": "mission-2", "decision_id": "decision-2",
                                "action_id": "action-2", "command_id": "command-2"})
    first = RecoveryRequest(**{**first.__dict__, "mission_id": "mission-1",
                               "incident_id": "incident-1", "decision_id": "decision-1",
                               "action_id": "action-1", "command_id": "command-1"})
    from aoip.agent.operations import _key_for
    assert _key_for(first) != _key_for(second)


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


async def test_redelivery_after_mutation_phase_escalates_without_remutation(tmp_path):
    r = _redis()
    led = IdempotencyLedger(r)
    req = _req()
    from aoip.agent.operations import _key_for
    key = _key_for(req)
    await led.claim(key, holder="dead-agent")
    await led.set_phase(key, phase=STATUS_MUTATION_STARTED, holder="dead-agent",
                        meta={"unit": "redis-server"})

    t = FakeSystemd(state="inactive")
    out = await _guard(r, t, req=req, approval=_approval(req), audit_log=_log(tmp_path),
                       holder="agent-2")
    assert out.status == "escalated"
    assert "reconcile_required" in out.reason
    assert t.restarts == 0


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


# ── Lease renewal: long-running mutation safety ──────────────────────────────

async def test_long_running_execution_renews_lease_no_redelivery(tmp_path, monkeypatch):
    """Transport chậm hơn lease TTL gốc; renewal (interval nhỏ) giữ lease sống suốt."""
    import asyncio

    class SlowSystemd(FakeSystemd):
        async def run(self, argv, *, timeout=15.0):
            if "restart" in " ".join(argv):
                await asyncio.sleep(0.2)
            return await super().run(argv, timeout=timeout)

    r = _redis()
    t = SlowSystemd(state="inactive")
    req = _req()
    out = await run_guarded_recovery(
        FakeCtx(), req=req, transport=t, audit_log=_log(tmp_path), gate=_gate(),
        approval=_approval(req), env_auto_execute=False, now=NOW, redis=r, holder="agent-1",
        lease_ttl_s=1, lease_renewal_interval_s=0.02)  # TTL gốc (1s) < thời gian chạy (0.2s+overhead)
    assert out.status == "recovered"
    assert t.restarts == 1


async def test_ownership_lost_during_mutation_becomes_escalated_not_completed(tmp_path):
    """Lease bị agent khác giành TRONG LÚC mutation chạy → KHÔNG tự nhận COMPLETED."""
    import asyncio

    r = _redis()
    lease = ExecutionLease(r)

    class HijackingSystemd(FakeSystemd):
        async def run(self, argv, *, timeout=15.0):
            if "restart" in " ".join(argv):
                # mô phỏng lease hết hạn + agent khác giành được NGAY khi renew sắp chạy
                await r.set("lease:svc:db", "other-agent-token", ex=120)
                await asyncio.sleep(0.05)
            return await super().run(argv, timeout=timeout)

    t = HijackingSystemd(state="inactive")
    req = _req()
    out = await run_guarded_recovery(
        FakeCtx(), req=req, transport=t, audit_log=_log(tmp_path), gate=_gate(),
        approval=_approval(req), env_auto_execute=False, now=NOW, redis=r, holder="agent-1",
        lease_ttl_s=120, lease_renewal_interval_s=0.01)
    assert out.status == "escalated"
    assert "ownership_lost_during_mutation_ambiguous" in out.reason
    # KHÔNG ghi đè lease của agent khác khi release (release chỉ xoá nếu token khớp)
    assert await lease.holder_token("svc:db") == "other-agent-token"


async def test_ownership_lost_but_already_healthy_stays_completed_no_action(tmp_path):
    """Mất ownership nhưng KHÔNG có mutation thật (đã healthy từ trước) → an toàn COMPLETED."""
    import asyncio

    r = _redis()

    class HijackAfterHealthyCheck(FakeSystemd):
        async def run(self, argv, *, timeout=15.0):
            cmd = " ".join(argv)
            if "is-active" in cmd:
                await r.set("lease:svc:db", "other-agent-token", ex=120)
                await asyncio.sleep(0.05)
            return await super().run(argv, timeout=timeout)

    t = HijackAfterHealthyCheck(state="active")  # đã healthy → execute_recovery abort, KHÔNG mutate
    req = _req()
    out = await run_guarded_recovery(
        FakeCtx(), req=req, transport=t, audit_log=_log(tmp_path), gate=_gate(),
        approval=_approval(req), env_auto_execute=False, now=NOW, redis=r, holder="agent-1",
        lease_ttl_s=120, lease_renewal_interval_s=0.01)
    assert t.restarts == 0  # KHÔNG mutation nào từng chạy
    assert out.status != "escalated"  # mất ownership vô hại vì không có side effect để bảo vệ


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


# ── Durable-command executor adapter (daemon → run_guarded_recovery) ────────────────
def _payload(*, tenant="acme", failure_mode="process_down", substrate="systemd",
            expires_at=NOW + 100.0, healthy=False):
    return {
        "recovery": {
            "failed_node": "svc:db", "failure_mode": failure_mode, "substrate": substrate,
            "unit": "redis-server", "port": 6379, "risk": 0.3, "diagnosed_at": NOW,
            "tenant": tenant, "dependents": [],
        },
        "approval": {
            "approver": "alice", "tenant": tenant, "decision_goal": "recover:process_down",
            "expires_at": expires_at, "action_id": "act-1", "canonical_scope": "svc:db",
            "issued_at": NOW - 10.0,
        },
        "evidence": {
            "diagnosis_confidence": 0.787,
            "findings": [
                {"claim": "svc:db is DOWN (probe failed)", "verdict": True, "confidence": 0.95,
                 "references": ["i"]},
                {"claim": "svc:db: process_down", "verdict": True, "confidence": 0.9,
                 "references": ["d"]},
            ],
        },
    }


def test_decode_recovery_command_builds_request_and_approval():
    req, approval, ctx = decode_recovery_command(_payload())
    assert req.failed_node == "svc:db" and req.tenant == "acme"
    assert approval.approved and approval.action_scope == req.action.scope
    assert ctx.diagnosis_confidence == 0.787 and len(ctx.findings) == 2


def test_decode_recovery_command_binds_action_id_from_approval():
    """req.action_id must come from the issued approval — this is the correlation
    identity _key_for() needs to pick the per-command idempotency key instead of
    the coarser legacy intent-based key (see operations._key_for)."""
    req, approval, _ctx = decode_recovery_command(_payload())
    assert req.action_id == approval.action_id == "act-1"


def test_key_for_uses_correlation_identity_when_payload_fully_bound():
    """A fully-bound production payload (mission/incident/decision/action/command
    ids all present) must produce the correlation-based key, not the legacy
    intent-only key — else two distinct approved commands for the same
    target+failure_mode+unit would collide and the second mutation would be
    silently skipped as 'already done'."""
    from aoip.agent.operations import _key_for

    payload = _payload()
    payload["mission_id"] = "mission-1"
    payload["incident_id"] = "incident-1"
    payload["decision_id"] = "decision-1"
    payload["command_id"] = "command-1"
    req, _approval, _ctx = decode_recovery_command(payload)

    corr_hash = payload_hash(unit=req.unit, verb=req.action.plan, port=req.port,
                             failure_mode=req.failure_mode, substrate=req.substrate)
    assert _key_for(req) == command_identity(req, payload_hash=corr_hash)


def test_decode_recovery_command_missing_field_raises():
    bad = _payload()
    del bad["recovery"]["unit"]
    from aoip.agent.operations import UnsupportedRecoveryPayload
    with pytest.raises(UnsupportedRecoveryPayload):
        decode_recovery_command(bad)


def test_decode_recovery_command_unsupported_capability_raises():
    from aoip.agent.operations import UnsupportedRecoveryPayload
    with pytest.raises(UnsupportedRecoveryPayload):
        decode_recovery_command(_payload(failure_mode="disk_full", substrate="systemd"))


# A. Wiring — happy path mutates exactly once and returns COMPLETED
async def test_executor_happy_path_completes_and_mutates_once(tmp_path):
    r = _redis()
    t = FakeSystemd(state="inactive", heal_on_restart=True)
    executor = build_recovery_executor(redis=r, holder="agent-1", transport=t,
                                       audit_log=_log(tmp_path), gate=_gate(), now=lambda: NOW)
    state, outcome = await executor(_payload())
    assert state == "COMPLETED" and outcome["rc"] == 0
    assert t.restarts == 1


# B. Duplicate delivery — two invocations, mutate once (ledger + lease backstop)
async def test_executor_duplicate_delivery_mutates_once(tmp_path):
    r = _redis()
    t = FakeSystemd(state="inactive", heal_on_restart=True)
    executor = build_recovery_executor(redis=r, holder="agent-1", transport=t,
                                       audit_log=_log(tmp_path), gate=_gate(), now=lambda: NOW)
    s1, o1 = await executor(_payload())
    s2, o2 = await executor(_payload())  # giao trùng
    assert s1 == "COMPLETED" and s2 == "COMPLETED"
    assert t.restarts == 1


# H. Already healthy — no mutation, COMPLETED + NO_ACTION_NEEDED evidence
async def test_executor_already_healthy_completes_without_mutation(tmp_path):
    r = _redis()
    t = FakeSystemd(state="active")  # đã khỏe từ đầu
    executor = build_recovery_executor(redis=r, holder="agent-1", transport=t,
                                       audit_log=_log(tmp_path), gate=_gate(), now=lambda: NOW)
    state, outcome = await executor(_payload())
    assert state == "COMPLETED"
    assert outcome["outcome"] == "NO_ACTION_NEEDED"
    assert t.restarts == 0


# F. Verification failure → ESCALATED, not COMPLETED, not silently retried
async def test_executor_verification_failure_escalates(tmp_path):
    r = _redis()
    t = FakeSystemd(state="inactive", heal_on_restart=False)  # restart không cứu được
    executor = build_recovery_executor(redis=r, holder="agent-1", transport=t,
                                       audit_log=_log(tmp_path), gate=_gate(), now=lambda: NOW)
    state, outcome = await executor(_payload())
    assert state == "ESCALATED"
    assert t.restarts == 1  # đã thử 1 lần, KHÔNG retry vô hạn


# F. Approval expired → FAILED, zero mutation
async def test_executor_expired_approval_fails_zero_mutation(tmp_path):
    r = _redis()
    t = FakeSystemd(state="inactive")
    executor = build_recovery_executor(redis=r, holder="agent-1", transport=t,
                                       audit_log=_log(tmp_path), gate=_gate(), now=lambda: NOW)
    state, outcome = await executor(_payload(expires_at=NOW - 1.0))
    assert state == "FAILED" and t.restarts == 0


# G. Unsupported input → fail closed without ever calling the mutation executor
async def test_executor_unsupported_payload_fails_without_calling_recovery(tmp_path):
    t = FakeSystemd(state="inactive")
    executor = build_recovery_executor(redis=_redis(), holder="agent-1", transport=t,
                                       audit_log=_log(tmp_path), gate=_gate())
    bad = _payload()
    del bad["approval"]["approver"]
    state, outcome = await executor(bad)
    assert state == "FAILED" and t.restarts == 0
    assert "unsupported_or_invalid_payload" in outcome["reason"]


# Exception in transport during mutation → FAILED, never becomes a false success
async def test_executor_transport_exception_fails_not_completed(tmp_path):
    class ExplodingTransport:
        async def run(self, argv, timeout=15.0):
            raise ConnectionError("ssh broke")

    executor = build_recovery_executor(redis=_redis(), holder="agent-1",
                                       transport=ExplodingTransport(), audit_log=_log(tmp_path),
                                       gate=_gate(), now=lambda: NOW)
    state, outcome = await executor(_payload())
    assert state == "FAILED"
    assert "executor_exception" in outcome["reason"]


# Wired into the daemon: default executor becomes the recovery adapter when deps given
async def test_daemon_uses_recovery_executor_when_deps_provided(tmp_path):
    from aoip.agent.daemon import run_daemon
    from aoip.agent.inbox import LocalInbox

    r = _redis()
    t = FakeSystemd(state="inactive", heal_on_restart=True)

    class FakeClient:
        def __init__(self, commands):
            self._commands = commands

        async def poll_runtime(self, agent_id):
            out, self._commands = self._commands, []
            return out

        async def accept(self, *a, **k):
            pass

        async def progress(self, *a, **k):
            pass

        async def report_terminal(self, agent_id, tenant_id, command_id, state, outcome, **k):
            return {"acknowledged": True, "state": state}

    client = FakeClient([{"command_id": "cmd-1", "tenant_id": "acme", "payload": _payload()}])
    await run_daemon(agent_id="agent-1", tenant="acme", gateway="http://x", api_key="",
                     inbox_root=str(tmp_path), interval_s=0, max_ticks=1, client=client,
                     redis=r, transport=t, audit_log=_log(tmp_path), gate=_gate(),
                     now=lambda: NOW)
    assert t.restarts == 1  # mutation thật đã chạy qua adapter, KHÔNG rơi về _noop_executor
