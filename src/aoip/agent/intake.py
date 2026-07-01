"""Slice 1A — Intake + fail-closed gates. KHÔNG mutation ở tầng này.

Ranh giới: từ lúc nhận command đến lúc admit/abort — trước bất kỳ tác động nào lên host.
Không cầm transport → về mặt cấu trúc KHÔNG thể mutate. Mọi transition phát ra event
TƯƠNG QUAN (Track B) ngay khi quyết định (Track A):

  COMMAND_RECEIVED → IDEMPOTENCY_CLAIMED → LEASE_ACQUIRED
                   → APPROVAL_VALIDATED | APPROVAL_REJECTED → (ABORTED khi bị chặn)

Fail-closed: duplicate / expired / wrong-tenant / wrong-scope / wrong-decision / wrong-action
/ ngoài capability / stale diagnosis / lease bị giữ → ZERO mutation, admitted=False.

Trace là READ MODEL: event chỉ PHẢN ÁNH quyết định của safety runtime, KHÔNG điều khiển nó.
``source_version`` = ordinal của phase an toàn (1..N) để operator phát hiện thiếu/đảo.
"""
from __future__ import annotations

from dataclasses import dataclass

from aoip import audit as audit_mod
from aoip.agent.idempotency import IdempotencyLedger, command_identity, payload_hash
from aoip.agent.idempotency import STATUS_TERMINAL
from aoip.agent.lease import ExecutionLease
from aoip.agent.trace import (
    EV_ABORTED,
    EV_APPROVAL_REJECTED,
    EV_APPROVAL_VALIDATED,
    EV_COMMAND_RECEIVED,
    EV_IDEMPOTENCY_CLAIMED,
    EV_LEASE_ACQUIRED,
    Correlation,
    RuntimeTrace,
)
from aoip.objects import ActionState


@dataclass(frozen=True)
class Admission:
    """Kết quả intake. admitted=True kèm idem_key + lease_token để tầng mutation (1B) tiếp."""

    admitted: bool
    reason: str
    idem_key: str | None = None
    lease_token: str | None = None
    duplicate: dict | None = None       # nếu bị chặn do trùng key (đã claim/terminal)


def _static_checks(corr: Correlation, req, gate, approval, now: float):
    """Kiểm fail-closed KHÔNG cần transport (identity/approval/authority/freshness).

    Trả list (name, ok, reason). Bất kỳ fail → APPROVAL_REJECTED → zero mutation.
    """
    age = now - req.diagnosed_at
    return [
        ("explicit_approval", approval.approved and approval.action_scope == req.action.scope,
         "thiếu approval tường minh cho đúng action scope"),
        ("approval_action_bound", bool(approval.action_id) and approval.action_id == corr.action_id,
         f"approval sai/thiếu Action binding ({approval.action_id!r} ≠ {corr.action_id!r})"),
        ("approval_scope_bound",
         bool(approval.canonical_scope) and approval.canonical_scope == corr.canonical_scope,
         f"approval sai/thiếu canonical scope ({approval.canonical_scope!r} ≠ {corr.canonical_scope!r})"),
        ("approval_tenant_bound", bool(approval.tenant) and approval.tenant == corr.tenant,
         f"approval sai/thiếu tenant ({approval.tenant!r} ≠ {corr.tenant!r})"),
        ("approval_decision_bound",
         bool(approval.decision_goal) and approval.decision_goal == req.action.decision_goal,
         f"approval sai/thiếu decision ({approval.decision_goal!r} ≠ {req.action.decision_goal!r})"),
        ("approval_not_expired", now <= approval.expires_at,
         f"approval hết hạn (now {now:.0f} > expires_at {approval.expires_at:.0f})"),
        ("approval_not_future", approval.issued_at <= now,
         f"approval phát hành từ tương lai (issued_at {approval.issued_at:.0f} > now {now:.0f})"),
        ("action_approved_state", req.action.state == ActionState.APPROVED,
         f"action state {req.action.state.value} ≠ approved"),
        ("capability_authorized",
         req.failure_mode in gate.allowed_failure_modes and req.substrate in gate.allowed_substrates,
         "failure_mode/substrate ngoài capability agent"),
        ("risk_within_gate", req.risk <= gate.max_risk, f"risk {req.risk} > max {gate.max_risk}"),
        ("scope_in_authority", req.failed_node.startswith(gate.scope_prefix),
         f"node {req.failed_node} ngoài scope {gate.scope_prefix!r}"),
        ("diagnosis_fresh", age <= gate.max_diagnosis_age_s,
         f"diagnosis stale ({age:.0f}s > {gate.max_diagnosis_age_s:.0f}s)"),
    ]


def _idem_key(corr: Correlation, req) -> str:
    ph = payload_hash(unit=req.unit, verb=req.action.result.get("verb", ""),
                      port=req.port, failure_mode=req.failure_mode, substrate=req.substrate)
    return command_identity(corr, payload_hash=ph)


async def admit_command(
    *, corr: Correlation, req, gate, approval, now: float, redis, holder: str,
    trace: RuntimeTrace, audit_log: "audit_mod.FileAuditLog | None" = None,
) -> Admission:
    """Nhận + gate 1 command. Trả Admission. KHÔNG chạm host (zero mutation ở tầng này)."""
    ledger = IdempotencyLedger(redis)
    lease = ExecutionLease(redis)
    key = _idem_key(corr, req)

    await trace.emit(EV_COMMAND_RECEIVED, corr, state_before="", state_after="received",
                     reason=f"mission {corr.mission_id} → recover {req.failure_mode} {req.unit}",
                     evidence_refs=(), ts=now, source_version=1)

    # ── IDEMPOTENCY: claim NX. Trùng (đã claim/terminal) → zero mutation ────────
    claimed = await ledger.claim(key, holder=holder)
    if not claimed:
        existing = await ledger.get(key) or {}
        dup_kind = "terminal" if existing.get("status") in STATUS_TERMINAL else "in-flight"
        reason = f"duplicate delivery ({dup_kind}) — cùng command identity, zero mutation"
        await trace.emit(EV_ABORTED, corr, state_before="received", state_after="aborted",
                         reason=reason, evidence_refs=(f"idem:{dup_kind}",), ts=now, source_version=2)
        return Admission(admitted=False, reason=reason, idem_key=key, duplicate=existing)
    await trace.emit(EV_IDEMPOTENCY_CLAIMED, corr, state_before="received", state_after="claimed",
                     reason="claim NX thành công — lần giao đầu tiên của command này",
                     evidence_refs=(key,), ts=now, source_version=2)

    # ── LEASE: single-writer trên canonical scope. Bị giữ → zero mutation ──────
    token = await lease.acquire(corr.canonical_scope, holder=holder)
    if token is None:
        cur = await lease.holder_token(corr.canonical_scope)
        reason = f"lease bị agent khác giữ ({cur}) — zero mutation"
        if audit_log is not None:
            audit_log.append(audit_mod.EV_RECOVERY_LEASE_DENIED,
                             {"scope": corr.canonical_scope, "holder": holder, "current": cur},
                             trace_id=corr.correlation_id)
        await ledger.release_claim(key)
        await trace.emit(EV_ABORTED, corr, state_before="claimed", state_after="aborted",
                         reason=reason, evidence_refs=(f"lease_holder:{cur}",), ts=now, source_version=3)
        return Admission(admitted=False, reason=reason, idem_key=key)
    await trace.emit(EV_LEASE_ACQUIRED, corr, state_before="claimed", state_after="leased",
                     reason=f"single-writer trên {corr.canonical_scope}",
                     evidence_refs=(f"lease_token:{token}",), ts=now, source_version=3)

    # ── APPROVAL + AUTHORITY: fail-closed. Bất kỳ fail → release, zero mutation ─
    blocked = [(n, r) for (n, ok, r) in _static_checks(corr, req, gate, approval, now) if not ok]
    if blocked:
        reason = "; ".join(f"{n}: {r}" for n, r in blocked)
        if audit_log is not None:
            audit_log.append(audit_mod.EV_RECOVERY_GATE_BLOCKED,
                             {"scope": corr.canonical_scope, "blocked": [n for n, _ in blocked],
                              "reason": reason}, trace_id=corr.correlation_id)
        await trace.emit(EV_APPROVAL_REJECTED, corr, state_before="leased", state_after="rejected",
                         reason=reason, evidence_refs=tuple(n for n, _ in blocked), ts=now, source_version=4)
        await lease.release(corr.canonical_scope, token=token)
        await ledger.release_claim(key)
        await trace.emit(EV_ABORTED, corr, state_before="rejected", state_after="aborted",
                         reason="fail-closed: gate chặn", evidence_refs=(), ts=now, source_version=5)
        return Admission(admitted=False, reason=reason, idem_key=key)

    await trace.emit(EV_APPROVAL_VALIDATED, corr, state_before="leased", state_after="approved",
                     reason=f"bounded approval hợp lệ bởi {approval.approver}",
                     evidence_refs=(f"approver:{approval.approver}", f"action:{corr.action_id}"),
                     ts=now, source_version=4)
    return Admission(admitted=True, reason="admitted", idem_key=key, lease_token=token)
