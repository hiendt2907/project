"""Controlled Recovery — vòng phục hồi có kiểm soát, bằng chứng, trách nhiệm.

Vì sao tồn tại (ranh giới quan trọng nhất của AOIP): không chỉ HIỂU vì sao hỏng mà
PHỤC HỒI có kiểm soát. Đây là phần khách trả tiền nhất, nhưng cũng nguy hiểm nhất —
nên mọi bước đều fail-closed, có gate, có audit.

Nguyên tắc kiến trúc cốt lõi (reviewer):
  - Recovery theo (FAILURE_MODE + SUBSTRATE), KHÔNG theo product name. Cùng một
    operator (process_down + systemd) phục hồi redis-server, mariadb, nginx — KHÔNG
    module riêng mỗi service.
  - Planner sinh ``Action`` (ontology đã có); KHÔNG tự chạy shell. Executor mới chạy,
    và CHỈ chạy Action đã APPROVED tường minh (INV_HUMAN_ACCOUNTABILITY).
  - Ngay trước khi execute: validate capability/authority/risk/scope/current-state.
  - Capture before-state + bằng chứng TRƯỚC mutation. Execute action nhỏ nhất, đảo
    được. Verify cả service lẫn dependents. KHÔNG retry vô hạn. Verify fail → dừng,
    giữ bằng chứng, escalate (rollback KHÔNG nhất thiết đối xứng — không giả vờ).
  - OMNI_AUTO_EXECUTE_ENABLED=false vẫn fail-closed: KHÔNG có path nào execute mà
    thiếu approval. Toàn bộ ghi audit hash-chain.

KHÔNG noun ontology mới: dùng Action/Decision/Finding. RecoveryOperator/RecoveryGate/
RecoveryRequest/Approval/RecoveryOutcome là Derived (policy/runtime value, không persist).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable

from aoip import audit
from aoip.objects import Action, ActionState

# Substrate đã hỗ trợ (mở rộng = thêm operator, KHÔNG sửa executor).
SUBSTRATE_SYSTEMD = "systemd"


# ── Operator: cách phục hồi một (failure_mode, substrate) — KHÔNG biết service ───
@dataclass(frozen=True)
class RecoveryOperator:
    """Tập thao tác phục hồi cho một cơ chế hỏng trên một substrate.

    Mỗi callable nhận (transport, unit, port) và async. Cùng operator dùng cho mọi
    service chạy trên substrate đó — đó là điểm khiến redis/mariadb/nginx chung 1 path.
    """

    failure_mode: str
    substrate: str
    action_verb: str                      # smallest reversible action, vd "restart"
    is_broken: Callable[..., Awaitable[bool]]      # state hiện tại còn hỏng?
    capture_before: Callable[..., Awaitable[dict]]  # before-state (bằng chứng)
    apply: Callable[..., Awaitable[tuple[str, int]]]  # mutation nhỏ nhất
    health: Callable[..., Awaitable[bool]]          # service khỏe lại?


# ── systemd / process_down operator (cặp đầu tiên) ───────────────────────────
async def _sd_is_broken(t, unit, port) -> bool:
    out, _ = await t.run(["systemctl", "is-active", unit])
    return out.strip().lower() in ("inactive", "failed", "deactivating")


async def _sd_capture(t, unit, port) -> dict:
    state, _ = await t.run(["systemctl", "is-active", unit])
    since, _ = await t.run(["systemctl", "show", "-p", "ActiveEnterTimestamp", "--value", unit])
    return {"unit": unit, "active_state": state.strip(), "active_since": since.strip()}


async def _sd_apply(t, unit, port) -> tuple[str, int]:
    # Action nhỏ nhất khôi phục tiến trình: restart unit (reversible qua systemd state).
    return await t.run(["sudo", "systemctl", "restart", unit], timeout=30.0)


async def _sd_health(t, unit, port) -> bool:
    state, _ = await t.run(["systemctl", "is-active", unit])
    if state.strip().lower() != "active":
        return False
    if port is None:
        return True
    out, _ = await t.run(
        ["bash", "-c",
         f'timeout 1 bash -c "exec 3<>/dev/tcp/127.0.0.1/{port}" && echo OPEN'], timeout=5.0)
    return "OPEN" in out


_SYSTEMD_PROCESS_DOWN = RecoveryOperator(
    failure_mode="process_down", substrate=SUBSTRATE_SYSTEMD, action_verb="restart",
    is_broken=_sd_is_broken, capture_before=_sd_capture, apply=_sd_apply, health=_sd_health,
)

# Registry: (failure_mode, substrate) → operator. Thêm cặp mới = 1 entry, KHÔNG sửa loop.
OPERATORS: dict[tuple[str, str], RecoveryOperator] = {
    ("process_down", SUBSTRATE_SYSTEMD): _SYSTEMD_PROCESS_DOWN,
}


def operator_for(failure_mode: str, substrate: str) -> RecoveryOperator | None:
    return OPERATORS.get((failure_mode, substrate))


# ── Policy / runtime values (Derived) ────────────────────────────────────────
@dataclass(frozen=True)
class RecoveryGate:
    """Thẩm quyền + ngưỡng của agent (capability/authority/risk/scope/freshness)."""

    allowed_failure_modes: frozenset[str]
    allowed_substrates: frozenset[str]
    max_risk: float
    scope_prefix: str                  # node được phép tác động (vd "svc:")
    min_diagnosis_confidence: float
    max_diagnosis_age_s: float


@dataclass(frozen=True)
class Approval:
    """Phê duyệt người, RÀNG BUỘC tenant + scope + Decision + Action + hạn (HITL).

    Living Operations Runtime: approval không được "chung chung". Nó chỉ hợp lệ cho
    ĐÚNG (tenant, action_scope, decision_goal) và HẾT HẠN sau expires_at — quá hạn /
    sai tenant / sai scope / sai decision → ZERO mutation. Mặc định backward-compatible
    cho call cũ (tenant/decision rỗng = bỏ qua ràng buộc đó, expires_at=∞)."""

    approved: bool
    approver: str
    action_scope: str
    tenant: str = ""
    decision_goal: str = ""
    expires_at: float = float("inf")


@dataclass(frozen=True)
class RecoveryRequest:
    failed_node: str
    failure_mode: str
    substrate: str
    unit: str
    port: int | None
    action: Action
    risk: float
    diagnosed_at: float
    dependents: tuple[str, ...] = ()
    tenant: str = "default"


@dataclass(frozen=True)
class RecoveryOutcome:
    action: Action
    status: str           # "recovered" | "escalated" | "aborted"
    reason: str
    evidence: tuple[str, ...] = ()


def plan_recovery(
    *, failed_node: str, failure_mode: str, substrate: str, unit: str, port: int | None, risk: float,
) -> Action:
    """Sinh Action phục hồi (PLANNED) từ failure_mode+substrate — KHÔNG chạy gì.

    Action mang đủ ngữ cảnh để executor ràng buộc operator; smallest reversible verb.
    """
    op = operator_for(failure_mode, substrate)
    verb = op.action_verb if op else "escalate"
    return Action(
        decision_goal=f"recover:{failure_mode}",
        scope=f"recover_service:{failed_node}",
        plan=f"{verb} {unit} ({substrate}) để khôi phục {failure_mode} trên {failed_node}",
        state=ActionState.PLANNED,
        result={"failure_mode": failure_mode, "substrate": substrate, "unit": unit,
                "port": port, "verb": verb, "risk": risk},
    )


def _gate_checks(ctx, req: RecoveryRequest, gate: RecoveryGate, approval: Approval, now: float):
    """Trả list (name, ok, reason) — kiểm NGAY TRƯỚC execute. Bất kỳ fail → zero mutation."""
    incident_verified = any(f.verdict and "DOWN" in f.claim for f in ctx.findings)
    diag = ctx.diagnosis_confidence
    positive_root = any(f.verdict and req.failure_mode in f.claim for f in ctx.findings) \
        or (diag is not None and diag >= gate.min_diagnosis_confidence)
    age = now - req.diagnosed_at
    return [
        ("incident_verified", incident_verified, "sự cố chưa được verify DOWN"),
        ("diagnosis_positive", (diag is not None and diag >= gate.min_diagnosis_confidence
                                and positive_root),
         f"diagnosis score {diag} < ngưỡng {gate.min_diagnosis_confidence} hoặc thiếu positive evidence"),
        ("diagnosis_fresh", age <= gate.max_diagnosis_age_s,
         f"diagnosis stale ({age:.0f}s > {gate.max_diagnosis_age_s:.0f}s)"),
        ("explicit_approval", approval.approved and approval.action_scope == req.action.scope,
         "thiếu approval tường minh cho đúng action scope"),
        ("approval_not_expired", now <= approval.expires_at,
         f"approval hết hạn (now {now:.0f} > expires_at {approval.expires_at:.0f})"),
        ("approval_tenant_bound", not approval.tenant or approval.tenant == req.tenant,
         f"approval sai tenant ({approval.tenant!r} ≠ {req.tenant!r})"),
        ("approval_decision_bound",
         not approval.decision_goal or approval.decision_goal == req.action.decision_goal,
         f"approval sai decision ({approval.decision_goal!r} ≠ {req.action.decision_goal!r})"),
        ("action_approved_state", req.action.state == ActionState.APPROVED,
         f"action state {req.action.state.value} ≠ approved"),
        ("capability_authorized",
         req.failure_mode in gate.allowed_failure_modes and req.substrate in gate.allowed_substrates,
         "failure_mode/substrate ngoài capability agent"),
        ("risk_within_gate", req.risk <= gate.max_risk,
         f"risk {req.risk} > max {gate.max_risk}"),
        ("scope_in_authority", req.failed_node.startswith(gate.scope_prefix),
         f"node {req.failed_node} ngoài scope {gate.scope_prefix!r}"),
    ]


async def execute_recovery(
    ctx, *, req: RecoveryRequest, transport, audit_log: audit.FileAuditLog,
    gate: RecoveryGate, approval: Approval, env_auto_execute: bool, now: float,
    probe_dependent: Callable[[str], Awaitable[bool]] | None = None,
) -> RecoveryOutcome:
    """Vòng phục hồi có kiểm soát. Trả RecoveryOutcome; ghi audit từng bước.

    Trình tự: gate → capture before → execute smallest action → verify service +
    dependents → complete | escalate. KHÔNG retry. Fail-closed tuyệt đối.
    """
    trace = req.action.scope
    op = operator_for(req.failure_mode, req.substrate)
    audit_log.append(audit.EV_RECOVERY_PLANNED, {
        "node": req.failed_node, "failure_mode": req.failure_mode, "substrate": req.substrate,
        "unit": req.unit, "verb": req.action.result.get("verb"), "risk": req.risk,
        "env_auto_execute": env_auto_execute,
    }, trace_id=trace)

    # ── GATE: mọi điều kiện phải đạt; bất kỳ fail → ZERO mutation ──────────────
    checks = _gate_checks(ctx, req, gate, approval, now)
    if op is None:
        checks.append(("operator_exists", False,
                       f"không có operator cho ({req.failure_mode},{req.substrate})"))
    blocked = [(n, r) for (n, ok, r) in checks if not ok]
    if blocked:
        reason = "; ".join(f"{n}: {r}" for n, r in blocked)
        audit_log.append(audit.EV_RECOVERY_GATE_BLOCKED,
                         {"node": req.failed_node, "blocked": [n for n, _ in blocked],
                          "reason": reason}, trace_id=trace)
        ctx.log("Recover", f"GATE chặn — KHÔNG mutation: {reason}")
        return RecoveryOutcome(action=req.action.at(ActionState.ABORTED, reason=reason),
                               status="aborted", reason=reason)

    # ── CURRENT-STATE GATE (ngay trước mutation): service còn hỏng thật không? ──
    if not await op.is_broken(transport, req.unit, req.port):
        reason = "service đang HEALTHY ngay trước execute — không tác động (zero mutation)"
        audit_log.append(audit.EV_RECOVERY_GATE_BLOCKED,
                         {"node": req.failed_node, "blocked": ["current_state_broken"],
                          "reason": reason}, trace_id=trace)
        ctx.log("Recover", reason)
        return RecoveryOutcome(action=req.action.at(ActionState.ABORTED, reason=reason),
                               status="aborted", reason=reason)

    # ── CAPTURE BEFORE-STATE (bằng chứng trước mutation) ──────────────────────
    before = await op.capture_before(transport, req.unit, req.port)
    audit_log.append(audit.EV_RECOVERY_BEFORE_STATE,
                     {"node": req.failed_node, "before": before}, trace_id=trace)

    # ── EXECUTE smallest reversible action (mutation thật) ────────────────────
    action = req.action.at(ActionState.EXECUTING)
    out, rc = await op.apply(transport, req.unit, req.port)
    audit_log.append(audit.EV_RECOVERY_EXECUTED,
                     {"node": req.failed_node, "verb": op.action_verb, "rc": rc,
                      "stdout": out[:200], "approver": approval.approver}, trace_id=trace)
    ctx.log("Recover", f"executed {op.action_verb} {req.unit} (rc={rc}) bởi {approval.approver}")

    # ── VERIFY: service khỏe lại + dependents hết ảnh hưởng ───────────────────
    service_ok = await op.health(transport, req.unit, req.port)
    dep_results: dict[str, bool] = {}
    if probe_dependent is not None:
        for dep in req.dependents:
            dep_results[dep] = await probe_dependent(dep)
    dependents_ok = all(dep_results.values()) if dep_results else True
    evidence = (f"before={before.get('active_state')}",
                f"service_health={'ok' if service_ok else 'fail'}",
                f"dependents={dep_results or 'n/a'}")

    if service_ok and dependents_ok:
        final = action.at(ActionState.COMPLETED, verified=True, dependents=dep_results)
        audit_log.append(audit.EV_RECOVERY_COMPLETED,
                         {"node": req.failed_node, "evidence": list(evidence)}, trace_id=trace)
        ctx.log("Recover", f"VERIFIED khỏe lại → COMPLETED ({', '.join(evidence)})")
        return RecoveryOutcome(action=final, status="recovered",
                               reason="service + dependents verified", evidence=evidence)

    # ── VERIFY FAIL: dừng, KHÔNG retry, giữ bằng chứng, escalate ──────────────
    final = action.at(ActionState.FAILED, verified=False, dependents=dep_results)
    audit_log.append(audit.EV_RECOVERY_VERIFICATION_FAILED,
                     {"node": req.failed_node, "evidence": list(evidence)}, trace_id=trace)
    audit_log.append(audit.EV_RECOVERY_ESCALATED,
                     {"node": req.failed_node,
                      "reason": "verification failed — no retry, human escalation"}, trace_id=trace)
    ctx.log("Recover", f"VERIFY FAIL → KHÔNG retry, ESCALATE ({', '.join(evidence)})")
    return RecoveryOutcome(action=final, status="escalated",
                           reason="verification failed → escalate (no infinite retry)",
                           evidence=evidence)
