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
    operator_for,
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
        allowed_targets=frozenset({"redis-server", "mariadb", "nginx"}),
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
    assert outcome.verification.status.value == "PASS"
    assert outcome.verification.evidence_refs


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
    assert outcome.verification.status.value == "FAIL"


async def test_dependent_unhealthy_escalates(tmp_path):
    t = FakeSystemd(state="inactive", heal_on_restart=True)
    req = _request(dependents=("svc:payment-api",))

    async def probe_dependent(node):
        return False  # dependent vẫn hỏng → verify fail dù service đã khỏe

    outcome, _ = await _run(tmp_path, t, req=req, probe_dependent=probe_dependent)
    assert outcome.status == "escalated"


async def test_verification_transport_error_is_unknown_and_escalated(tmp_path):
    class BrokenHealth(FakeSystemd):
        async def run(self, argv, *, timeout=15.0):
            if "is-active" in " ".join(argv) and self.restarts:
                raise ConnectionError("verification transport unavailable")
            return await super().run(argv, timeout=timeout)

    t = BrokenHealth(state="inactive", heal_on_restart=True)
    outcome, log = await _run(tmp_path, t)
    assert outcome.status == "escalated"
    assert outcome.verification.status.value == "UNKNOWN"
    assert "transport" in outcome.verification.reason
    assert audit.EV_RECOVERY_ESCALATED in log.events()


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
                        scope_prefix="svc:", min_diagnosis_confidence=0.3, max_diagnosis_age_s=300.0,
                        allowed_targets=frozenset({"redis-server"}))
    outcome, _ = await _run(tmp_path, t, gate=gate)
    assert outcome.status == "aborted" and t.restarts == 0


async def test_out_of_scope_zero_mutation(tmp_path):
    t = FakeSystemd(state="inactive")
    gate = RecoveryGate(allowed_failure_modes=frozenset({"process_down"}),
                        allowed_substrates=frozenset({"systemd"}), max_risk=0.5,
                        scope_prefix="host:", min_diagnosis_confidence=0.3, max_diagnosis_age_s=300.0,
                        allowed_targets=frozenset({"redis-server"}))
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


# ── Operator #2: failed_state_stale (systemd.reset_failed capability) ────────
# Registry test — new operator entry, KHÔNG sửa execute_recovery loop.
def test_failed_state_stale_operator_registered():
    op = operator_for("failed_state_stale", "systemd")
    assert op is not None
    assert op.action_verb == "reset-failed"


def test_plan_recovery_reset_failed_verb():
    action = plan_recovery(failed_node="svc:payment-api", failure_mode="failed_state_stale",
                           substrate="systemd", unit="payment-api", port=None, risk=0.1)
    assert action.state == ActionState.PLANNED
    assert action.result["verb"] == "reset-failed"
    assert "payment-api" in action.plan


class FakeSystemdFailedState:
    """Transport giả cho operator failed_state_stale — is-failed/reset-failed.

    KHÔNG có restart/is-active heal path: operator này chỉ dọn bookkeeping,
    không bao giờ start/stop tiến trình (zero downtime).
    """

    target = "h"

    def __init__(self, *, is_failed=True, heal_on_reset=True):
        self.is_failed_state = is_failed
        self.heal_on_reset = heal_on_reset
        self.resets = 0

    async def run(self, argv, *, timeout=15.0):
        cmd = " ".join(argv)
        if "reset-failed" in cmd:
            self.resets += 1
            if self.heal_on_reset:
                self.is_failed_state = False
            return ("", 0)
        if "is-failed" in cmd:
            return ("failed\n" if self.is_failed_state else "active\n", 0)
        return ("", 0)


def _failed_state_request(unit="payment-api", *, approved_state=True):
    action = plan_recovery(failed_node="svc:payment-api", failure_mode="failed_state_stale",
                           substrate="systemd", unit=unit, port=None, risk=0.1)
    if approved_state:
        action = action.at(ActionState.APPROVED)
    return RecoveryRequest(
        failed_node="svc:payment-api", failure_mode="failed_state_stale", substrate="systemd",
        unit=unit, port=None, action=action, risk=0.1, diagnosed_at=NOW,
    )


@dataclass
class FailedStateCtx:
    """Claim mentions the failure_mode itself, NOT 'DOWN' — proves the
    generalized incident_verified gate check (aoip.recovery._gate_checks)
    accepts a capability-specific claim instead of only the legacy 'DOWN'
    phrasing hardcoded for process_down."""

    diagnosis_confidence: float | None = 0.9
    findings: list = field(default_factory=lambda: [
        Finding(claim="svc:payment-api failed_state_stale (dependency now healthy)",
               references=("i",), verdict=True, confidence=0.9),
    ])
    trace: list = field(default_factory=list)

    def log(self, verb, detail):
        self.trace.append(f"{verb}: {detail}")


async def test_reset_failed_operator_clears_stale_flag(tmp_path):
    t = FakeSystemdFailedState(is_failed=True, heal_on_reset=True)
    req = _failed_state_request()
    log = audit.FileAuditLog(tmp_path / "audit.jsonl")
    outcome = await execute_recovery(
        FailedStateCtx(), req=req, transport=t, audit_log=log,
        gate=RecoveryGate(allowed_failure_modes=frozenset({"failed_state_stale"}),
                          allowed_substrates=frozenset({"systemd"}), max_risk=0.5,
                          scope_prefix="svc:", min_diagnosis_confidence=0.3, max_diagnosis_age_s=300.0,
                          allowed_targets=frozenset({"payment-api"})),
        approval=Approval(approved=True, approver="alice", action_scope=req.action.scope),
        env_auto_execute=False, now=NOW)
    assert outcome.status == "recovered"
    assert t.resets == 1
    assert t.is_failed_state is False


async def test_reset_failed_operator_zero_mutation_when_not_failed(tmp_path):
    t = FakeSystemdFailedState(is_failed=False)  # đã không còn failed
    req = _failed_state_request()
    log = audit.FileAuditLog(tmp_path / "audit.jsonl")
    outcome = await execute_recovery(
        FailedStateCtx(), req=req, transport=t, audit_log=log,
        gate=RecoveryGate(allowed_failure_modes=frozenset({"failed_state_stale"}),
                          allowed_substrates=frozenset({"systemd"}), max_risk=0.5,
                          scope_prefix="svc:", min_diagnosis_confidence=0.3, max_diagnosis_age_s=300.0,
                          allowed_targets=frozenset({"payment-api"})),
        approval=Approval(approved=True, approver="alice", action_scope=req.action.scope),
        env_auto_execute=False, now=NOW)
    assert outcome.status == "aborted"
    assert t.resets == 0


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


async def test_recovery_audit_carries_end_to_end_correlation(tmp_path):
    t = FakeSystemd(state="inactive")
    req = _request()
    req = req.__class__(**{
        **req.__dict__,
        "tenant": "acme",
        "mission_id": "mission-1",
        "incident_id": "incident-1",
        "decision_id": "decision-1",
        "action_id": "action-1",
        "command_id": "command-1",
        "trace_id": "trace-1",
    })
    _, log = await _run(tmp_path, t, req=req)
    first = log._blocks()[0]
    assert first["trace_id"] == "trace-1"
    assert first["payload"]["tenant_id"] == "acme"
    assert first["payload"]["command_id"] == "command-1"


# ── Operator #3: disk_pressure_journal (systemd.journal_vacuum capability) ───
# Registry test — new operator entry, KHÔNG sửa execute_recovery loop. First
# auto-remediation for the SYS_RESOURCE lane (the other two operators are
# SYS_HARD_FAIL).
def test_disk_pressure_journal_operator_registered():
    op = operator_for("disk_pressure_journal", "systemd")
    assert op is not None
    assert op.action_verb == "vacuum"


def test_plan_recovery_journal_vacuum_verb():
    action = plan_recovery(failed_node="svc:systemd-journald.service",
                           failure_mode="disk_pressure_journal", substrate="systemd",
                           unit="systemd-journald.service", port=None, risk=0.12)
    assert action.state == ActionState.PLANNED
    assert action.result["verb"] == "vacuum"
    assert "systemd-journald.service" in action.plan


class FakeSystemdJournal:
    """Transport giả cho operator disk_pressure_journal — --disk-usage/--vacuum-size=.

    KHÔNG có restart/is-active/is-failed heal path: operator này chỉ dọn dữ
    liệu journal qua journalctl chính thức, không bao giờ start/stop tiến
    trình nào.
    """

    target = "h"

    def __init__(self, *, disk_usage_bytes=3 * 1024**3, heal_on_vacuum=True,
                post_vacuum_bytes=50 * 1024**2):
        self.disk_usage_bytes = disk_usage_bytes
        self.heal_on_vacuum = heal_on_vacuum
        self.post_vacuum_bytes = post_vacuum_bytes
        self.vacuums = 0

    async def run(self, argv, *, timeout=15.0):
        cmd = " ".join(argv)
        if "--vacuum-size=" in cmd:
            self.vacuums += 1
            if self.heal_on_vacuum:
                self.disk_usage_bytes = self.post_vacuum_bytes
            return ("", 0)
        if "--disk-usage" in cmd:
            gib = self.disk_usage_bytes / (1024**3)
            return (f"Archived and active journals take up {gib:.2f}G in the file system.\n", 0)
        return ("", 0)


def _journal_request(unit="systemd-journald.service", *, approved_state=True):
    action = plan_recovery(failed_node="svc:systemd-journald.service",
                           failure_mode="disk_pressure_journal", substrate="systemd",
                           unit=unit, port=None, risk=0.12)
    if approved_state:
        action = action.at(ActionState.APPROVED)
    return RecoveryRequest(
        failed_node="svc:systemd-journald.service", failure_mode="disk_pressure_journal",
        substrate="systemd", unit=unit, port=None, action=action, risk=0.12, diagnosed_at=NOW,
    )


@dataclass
class JournalCtx:
    """Claim mentions the failure_mode itself, NOT 'DOWN' — proves the
    generalized incident_verified gate check (aoip.recovery._gate_checks)
    accepts a capability-specific claim instead of only the legacy 'DOWN'
    phrasing hardcoded for process_down."""

    diagnosis_confidence: float | None = 0.9
    findings: list = field(default_factory=lambda: [
        Finding(claim="svc:systemd-journald.service disk_pressure_journal (3.0G retained)",
               references=("i",), verdict=True, confidence=0.9),
    ])
    trace: list = field(default_factory=list)

    def log(self, verb, detail):
        self.trace.append(f"{verb}: {detail}")


async def test_journal_vacuum_operator_clears_disk_pressure(tmp_path):
    t = FakeSystemdJournal(disk_usage_bytes=3 * 1024**3, heal_on_vacuum=True,
                           post_vacuum_bytes=50 * 1024**2)
    req = _journal_request()
    log = audit.FileAuditLog(tmp_path / "audit.jsonl")
    outcome = await execute_recovery(
        JournalCtx(), req=req, transport=t, audit_log=log,
        gate=RecoveryGate(allowed_failure_modes=frozenset({"disk_pressure_journal"}),
                          allowed_substrates=frozenset({"systemd"}), max_risk=0.5,
                          scope_prefix="svc:", min_diagnosis_confidence=0.3, max_diagnosis_age_s=300.0,
                          allowed_targets=frozenset({"systemd-journald.service"})),
        approval=Approval(approved=True, approver="alice", action_scope=req.action.scope),
        env_auto_execute=False, now=NOW)
    assert outcome.status == "recovered"
    assert t.vacuums == 1
    assert t.disk_usage_bytes < 2 * 1024**3


async def test_journal_vacuum_operator_zero_mutation_when_below_threshold(tmp_path):
    t = FakeSystemdJournal(disk_usage_bytes=10 * 1024**2)  # 10MiB, already under default 2GiB
    req = _journal_request()
    log = audit.FileAuditLog(tmp_path / "audit.jsonl")
    outcome = await execute_recovery(
        JournalCtx(), req=req, transport=t, audit_log=log,
        gate=RecoveryGate(allowed_failure_modes=frozenset({"disk_pressure_journal"}),
                          allowed_substrates=frozenset({"systemd"}), max_risk=0.5,
                          scope_prefix="svc:", min_diagnosis_confidence=0.3, max_diagnosis_age_s=300.0,
                          allowed_targets=frozenset({"systemd-journald.service"})),
        approval=Approval(approved=True, approver="alice", action_scope=req.action.scope),
        env_auto_execute=False, now=NOW)
    assert outcome.status == "aborted"
    assert t.vacuums == 0


# ── Journal disk-usage parsing/threshold helpers (env-configurable, KHÔNG hardcode) ─
def test_parse_disk_usage_bytes_various_units():
    from aoip.recovery import _parse_disk_usage_bytes

    assert _parse_disk_usage_bytes("Archived and active journals take up 4.0G in the file system.") \
        == int(4.0 * 1024**3)
    assert _parse_disk_usage_bytes("Archived and active journals take up 500.0M in the file system.") \
        == int(500.0 * 1024**2)
    assert _parse_disk_usage_bytes("Archived and active journals take up 8.0K in the file system.") \
        == int(8.0 * 1024)


def test_parse_disk_usage_bytes_unparseable_returns_none():
    from aoip.recovery import _parse_disk_usage_bytes

    assert _parse_disk_usage_bytes("") is None
    assert _parse_disk_usage_bytes("no numbers here") is None
