"""Tests: Controlled Recovery — fail-closed, có gate, có bằng chứng, có audit.

Phủ 5 ca reviewer bắt buộc: (1) approved → recover; (2) thiếu approval → zero
mutation; (3) diagnosis stale → zero mutation; (4) service healthy → zero mutation;
(5) verify fail → escalate KHÔNG retry. Cộng: audit tamper-evident; cùng operator
(process_down+systemd) phục hồi redis/mariadb/nginx KHÔNG module riêng.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from aoip import audit
from aoip.objects import ActionState, Finding
from aoip.recovery import (
    Approval,
    RecoveryGate,
    RecoveryRequest,
    execute_recovery,
    plan_recovery,
)

NOW = 1000.0


class FakeSystemd:
    """Giả lập systemd: is-active/show/restart/dev-tcp; đếm restart để bắt retry."""

    target = "h"

    def __init__(self, *, state="inactive", heal_on_restart=True, port_open=True):
        self.state = state
        self.heal_on_restart = heal_on_restart
        self.port_open = port_open
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
            return ("Mon 2026-06-30 10:00:00 UTC\n", 0)
        if "/dev/tcp" in cmd:
            ok = self.state == "active" and self.port_open
            return ("OPEN\n" if ok else "", 0)
        return ("", 0)


@dataclass
class FakeCtx:
    diagnosis_confidence: float | None = 0.787
    findings: list = field(default_factory=lambda: [
        Finding(claim="svc:cust-db is DOWN (probe failed)", references=("i",), verdict=True, confidence=0.95),
        Finding(claim="svc:cust-db: process_down", references=("d",), verdict=True, confidence=0.9),
    ])
    trace: list = field(default_factory=list)

    def log(self, verb, detail):
        self.trace.append(f"{verb}: {detail}")


def _gate():
    return RecoveryGate(
        allowed_failure_modes=frozenset({"process_down"}),
        allowed_substrates=frozenset({"systemd"}),
        max_risk=0.5, scope_prefix="svc:",
        min_diagnosis_confidence=0.3, max_diagnosis_age_s=300.0,
    )


def _request(unit="redis-server", *, approved_state=True, port=6379, dependents=()):
    action = plan_recovery(failed_node="svc:cust-db", failure_mode="process_down",
                           substrate="systemd", unit=unit, port=port, risk=0.3)
    if approved_state:
        action = action.at(ActionState.APPROVED)
    return RecoveryRequest(
        failed_node="svc:cust-db", failure_mode="process_down", substrate="systemd",
        unit=unit, port=port, action=action, risk=0.3, diagnosed_at=NOW, dependents=dependents,
    )


def _approval(req, *, approved=True):
    return Approval(approved=approved, approver="alice", action_scope=req.action.scope)


async def _run(tmp_path, transport, *, ctx=None, req=None, gate=None, approval=None, now=NOW,
               probe_dependent=None):
    ctx = ctx or FakeCtx()
    req = req or _request()
    log = audit.FileAuditLog(tmp_path / "audit.jsonl")
    outcome = await execute_recovery(
        ctx, req=req, transport=transport, audit_log=log, gate=gate or _gate(),
        approval=approval if approval is not None else _approval(req),
        env_auto_execute=False, now=now, probe_dependent=probe_dependent)
    return outcome, log


# ── Ca 1: approved → recover ─────────────────────────────────────────────────
async def test_successful_approved_recovery(tmp_path):
    t = FakeSystemd(state="inactive", heal_on_restart=True)
    outcome, log = await _run(tmp_path, t)
    assert outcome.status == "recovered"
    assert outcome.action.state == ActionState.COMPLETED
    assert t.restarts == 1
    assert audit.EV_RECOVERY_EXECUTED in log.events()
    assert audit.EV_RECOVERY_COMPLETED in log.events()
    assert log.verify_chain()


# ── Ca 2: thiếu approval → zero mutation ─────────────────────────────────────
async def test_missing_approval_zero_mutation(tmp_path):
    t = FakeSystemd(state="inactive")
    req = _request()
    outcome, log = await _run(tmp_path, t, req=req, approval=_approval(req, approved=False))
    assert outcome.status == "aborted"
    assert t.restarts == 0
    assert audit.EV_RECOVERY_EXECUTED not in log.events()
    assert audit.EV_RECOVERY_GATE_BLOCKED in log.events()


# ── Ca 3: diagnosis stale → zero mutation ────────────────────────────────────
async def test_stale_diagnosis_zero_mutation(tmp_path):
    t = FakeSystemd(state="inactive")
    outcome, log = await _run(tmp_path, t, now=NOW + 10_000)  # quá max_diagnosis_age_s
    assert outcome.status == "aborted"
    assert "stale" in outcome.reason
    assert t.restarts == 0
    assert audit.EV_RECOVERY_EXECUTED not in log.events()


# ── Ca 4: service healthy → zero mutation ────────────────────────────────────
async def test_healthy_service_zero_mutation(tmp_path):
    t = FakeSystemd(state="active")  # đang khỏe ngay trước execute
    outcome, log = await _run(tmp_path, t)
    assert outcome.status == "aborted"
    assert "HEALTHY" in outcome.reason
    assert t.restarts == 0
    assert audit.EV_RECOVERY_EXECUTED not in log.events()


# ── Ca 5: verify fail → escalate KHÔNG retry ─────────────────────────────────
async def test_failed_verification_escalates_no_retry(tmp_path):
    t = FakeSystemd(state="inactive", heal_on_restart=False)  # restart không khôi phục
    outcome, log = await _run(tmp_path, t)
    assert outcome.status == "escalated"
    assert outcome.action.state == ActionState.FAILED
    assert t.restarts == 1  # đúng MỘT lần — không retry vô hạn
    assert audit.EV_RECOVERY_VERIFICATION_FAILED in log.events()
    assert audit.EV_RECOVERY_ESCALATED in log.events()


async def test_dependent_unhealthy_escalates(tmp_path):
    t = FakeSystemd(state="inactive", heal_on_restart=True)
    req = _request(dependents=("svc:payment-api",))

    async def probe_dependent(node):
        return False  # dependent vẫn hỏng → verify fail dù service đã khỏe

    outcome, _ = await _run(tmp_path, t, req=req, probe_dependent=probe_dependent)
    assert outcome.status == "escalated"


# ── Gate phụ: state/risk/scope/capability ────────────────────────────────────
async def test_unapproved_action_state_zero_mutation(tmp_path):
    t = FakeSystemd(state="inactive")
    req = _request(approved_state=False)  # action vẫn PLANNED
    outcome, log = await _run(tmp_path, t, req=req, approval=_approval(req))
    assert outcome.status == "aborted"
    assert t.restarts == 0


async def test_risk_over_gate_zero_mutation(tmp_path):
    t = FakeSystemd(state="inactive")
    gate = RecoveryGate(allowed_failure_modes=frozenset({"process_down"}),
                        allowed_substrates=frozenset({"systemd"}), max_risk=0.1,
                        scope_prefix="svc:", min_diagnosis_confidence=0.3, max_diagnosis_age_s=300.0)
    outcome, _ = await _run(tmp_path, t, gate=gate)
    assert outcome.status == "aborted" and t.restarts == 0


async def test_out_of_scope_zero_mutation(tmp_path):
    t = FakeSystemd(state="inactive")
    gate = RecoveryGate(allowed_failure_modes=frozenset({"process_down"}),
                        allowed_substrates=frozenset({"systemd"}), max_risk=0.5,
                        scope_prefix="host:", min_diagnosis_confidence=0.3, max_diagnosis_age_s=300.0)
    outcome, _ = await _run(tmp_path, t, gate=gate)
    assert outcome.status == "aborted" and t.restarts == 0


# ── Planner sinh Action, KHÔNG execute ───────────────────────────────────────
def test_plan_recovery_produces_action_not_shell():
    action = plan_recovery(failed_node="svc:x", failure_mode="process_down",
                           substrate="systemd", unit="redis-server", port=6379, risk=0.3)
    assert action.state == ActionState.PLANNED
    assert action.result["verb"] == "restart"
    assert "redis-server" in action.plan


# ── Cùng operator phục hồi 3 service khác nhau, KHÔNG module riêng ────────────
@pytest.mark.parametrize("unit,port", [("redis-server", 6379), ("mariadb", 3306), ("nginx", 80)])
async def test_same_operator_recovers_three_services(tmp_path, unit, port):
    t = FakeSystemd(state="inactive", heal_on_restart=True)
    req = _request(unit=unit, port=port)
    outcome, _ = await _run(tmp_path, t, req=req)
    assert outcome.status == "recovered"
    assert t.restarts == 1


# ── Audit hash-chain tamper-evident ──────────────────────────────────────────
async def test_audit_chain_tamper_evident(tmp_path):
    t = FakeSystemd(state="inactive")
    _, log = await _run(tmp_path, t)
    assert log.verify_chain()
    p = tmp_path / "audit.jsonl"
    lines = p.read_text().splitlines()
    lines[0] = lines[0].replace('"node": "svc:cust-db"', '"node": "svc:hacked"')
    p.write_text("\n".join(lines) + "\n")
    assert not log.verify_chain()  # sửa lén → gãy chuỗi
