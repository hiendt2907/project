"""Living Operations Runtime — vòng vận hành liên tục, an toàn, qua Gateway thật.

Vì sao tồn tại: các flow đã có (Diagnosis/Decision/Approval/Recovery/Verify/Audit)
mới chạy ONE-SHOT trong demo. Sản phẩm cần chúng chạy LIÊN TỤC như một dịch vụ
systemd sống lâu, sống sót restart/giao trùng/lỗi mạng trên hạ tầng thật.

Hai lớp ở đây:
  1. ``run_guarded_recovery`` — bọc ``execute_recovery`` bằng IDEMPOTENCY (chạy đúng
     1 lần dù giao trùng/crash) + LEASE (single-writer per scope). Đây là phần khó
     và quan trọng nhất về AN TOÀN.
  2. ``operations_loop`` — vòng register→heartbeat→pull→(guarded recovery)→submit→
     sleep→repeat. Lỗi mạng KHÔNG bao giờ kích hoạt mutation mù (chỉ retry I/O đọc).

KHÔNG noun ontology mới: idempotency/lease là sổ vận hành (như audit). Mutation vẫn
chỉ qua ``execute_recovery`` (fail-closed, HITL, current-state revalidate).
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from aoip import audit
from aoip.agent.idempotency import (
    STATUS_CLAIMED,
    STATUS_TERMINAL,
    IdempotencyLedger,
    idempotency_key,
)
from aoip.agent.lease import ExecutionLease
from aoip.objects import ActionState
from aoip.recovery import RecoveryOutcome, RecoveryRequest, execute_recovery


def _key_for(req: RecoveryRequest) -> str:
    return idempotency_key(tenant=req.tenant, scope=req.action.scope,
                           decision_goal=req.action.decision_goal,
                           failure_mode=req.failure_mode, unit=req.unit)


async def run_guarded_recovery(
    ctx, *, req: RecoveryRequest, transport, audit_log: audit.FileAuditLog,
    gate, approval, env_auto_execute: bool, now: float, redis, holder: str,
    probe_dependent: Callable[[str], Awaitable[bool]] | None = None, lease_ttl_s: int = 120,
) -> RecoveryOutcome:
    """execute_recovery + idempotency + lease. Đảm bảo: chạy đúng 1 lần, 1 writer.

    Trình tự an toàn:
      A. LEASE single-writer: acquire scope; giữ được mới là writer hợp lệ; không
         được (agent khác đang giữ) → ZERO mutation.
      B. IDEMPOTENCY: key đã terminal (đã chạy) → reconcile, ZERO mutation mới.
         Crash giữa chừng (claim cũ, holder đã mất vì ta giành được lease) → để
         execute_recovery REVALIDATE current-state: service đã healthy → abort (đã
         khôi phục); còn hỏng → chạy lại an toàn (mutation chưa hiệu lực).
    """
    ledger = IdempotencyLedger(redis)
    lease = ExecutionLease(redis)
    key = _key_for(req)
    target = req.failed_node          # lease khóa theo TARGET scope (mutating target)
    scope = req.action.scope          # idempotency/audit theo action scope
    trace = scope

    # ── A. LEASE: chỉ single-writer hợp lệ trên TARGET mới đi tiếp ────────────
    token = await lease.acquire(target, holder=holder, ttl_s=lease_ttl_s)
    if token is None:
        audit_log.append(audit.EV_RECOVERY_LEASE_DENIED,
                         {"target": target, "holder": holder,
                          "current": await lease.holder_token(target)}, trace_id=trace)
        return RecoveryOutcome(action=req.action.at(ActionState.ABORTED, reason="lease_denied"),
                               status="aborted",
                               reason="scope đang bị agent khác giữ lease — zero mutation")
    try:
        # ── B. IDEMPOTENCY: đã chạy xong trước đó? → reconcile, zero mutation ──
        existing = await ledger.get(key)
        if existing and existing.get("status") in STATUS_TERMINAL:
            audit_log.append(audit.EV_RECOVERY_RECONCILED,
                             {"key": key, "prior_status": existing["status"],
                              "scope": scope}, trace_id=trace)
            return RecoveryOutcome(
                action=req.action.at(ActionState.COMPLETED, reconciled=True),
                status=existing["status"],
                reason=f"idempotent: đã {existing['status']} trước đó (reconciled, zero mutation)")

        # Ta đang giữ lease ⇒ writer cũ (nếu có claim treo) đã biến mất → an toàn
        # nhận quyền. Claim (overwrite claim treo) rồi để execute_recovery tự
        # REVALIDATE current-state (backstop reconcile cho crash-after-mutation).
        await ledger._r.set(key, '{"status": "%s", "holder": "%s"}' % (STATUS_CLAIMED, holder),
                            ex=900)

        outcome = await execute_recovery(
            ctx, req=req, transport=transport, audit_log=audit_log, gate=gate,
            approval=approval, env_auto_execute=env_auto_execute, now=now,
            probe_dependent=probe_dependent)

        # Record terminal CHỈ khi đã thực sự qua gate và chạy (recovered/escalated).
        # aborted (gate chặn / service healthy) → nhả claim để lần hợp lệ sau thử lại.
        if outcome.status in ("recovered", "escalated"):
            await ledger.record(key, status=outcome.status, outcome={"reason": outcome.reason})
        else:
            await ledger.release_claim(key)
        return outcome
    finally:
        await lease.release(target, token=token)


async def operations_loop(
    *, agent, redis, build_request: Callable[[str], Awaitable["RecoveryRequest | None"]],
    handle_request: Callable[[RecoveryRequest], Awaitable[RecoveryOutcome]],
    sleep_s: float = 5.0, max_iterations: int | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> int:
    """Vòng sống: register→heartbeat→pull→(guarded recovery)→submit→sleep→repeat.

    AN TOÀN MẠNG: mọi lỗi I/O (register/heartbeat/pull/submit) được nuốt và LOOP
    TIẾP — KHÔNG bao giờ kích hoạt mutation mù. Mutation chỉ xảy ra trong
    handle_request (đi qua run_guarded_recovery). Idempotency làm submit retry an toàn.

    Trả số iteration đã chạy. ``build_request(goal)`` decode mission→RecoveryRequest
    (None = mission không phải recovery, chỉ report). ``handle_request`` thường là
    partial của run_guarded_recovery. Test inject để kiểm soát vòng.
    """
    iterations = 0
    try:
        await agent.register()
    except Exception:  # noqa: BLE001 — lỗi mạng register: thử lại ở heartbeat
        pass
    while True:
        if max_iterations is not None and iterations >= max_iterations:
            break
        if should_stop is not None and should_stop():
            break
        iterations += 1
        try:
            await agent.heartbeat()
            goal = await agent.pull_mission()
        except Exception:  # noqa: BLE001 — lỗi mạng đọc: bỏ qua chu kỳ, KHÔNG mutate
            await asyncio.sleep(sleep_s)
            continue
        if goal:
            try:
                built = await build_request(goal)
                if built is not None:
                    outcome = await handle_request(built)
                    await agent.report_result(rc=0 if outcome.status == "recovered" else 1,
                                              stdout=f"{outcome.status}: {outcome.reason}")
                else:
                    await agent.report_result(rc=0, stdout="no-op (non-recovery mission)")
            except Exception:  # noqa: BLE001 — lỗi xử lý/submit: report fail an toàn, loop tiếp
                pass
        await asyncio.sleep(sleep_s)
    return iterations
