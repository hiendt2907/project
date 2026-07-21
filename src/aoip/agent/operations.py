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
import hashlib
import json
import time
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable

from aoip import audit
from aoip.agent.idempotency import (
    STATUS_CLAIMED,
    STATUS_MUTATION_STARTED,
    STATUS_RECONCILE_REQUIRED,
    STATUS_TERMINAL,
    STATUS_VERIFYING,
    IdempotencyLedger,
    command_identity,
    idempotency_key,
    payload_hash,
)
from aoip.agent.lease import ExecutionLease
from aoip.agent.renewal import run_with_renewal
from aoip.agent.trace import canonical_scope
from aoip.objects import ActionState, Finding
from aoip.recovery import (
    Approval,
    RecoveryGate,
    RecoveryOutcome,
    RecoveryRequest,
    execute_recovery,
    operator_for,
    plan_recovery,
)


def _key_for(req: RecoveryRequest) -> str:
    # Production commands carry immutable delivery/correlation IDs. Keep the
    # intent-based key only for legacy direct callers and old tests.
    if all((req.tenant, req.mission_id, req.incident_id, req.decision_id,
            req.action_id, req.command_id)):
        corr_hash = payload_hash(unit=req.unit, verb=req.action.plan,
                                 port=req.port, failure_mode=req.failure_mode,
                                 substrate=req.substrate)
        return command_identity(req, payload_hash=corr_hash)
    return idempotency_key(tenant=req.tenant, scope=req.action.scope,
                           decision_goal=req.action.decision_goal,
                           failure_mode=req.failure_mode, unit=req.unit)


async def run_guarded_recovery(
    ctx, *, req: RecoveryRequest, transport, audit_log: audit.FileAuditLog,
    gate, approval, env_auto_execute: bool, now: float, redis, holder: str,
    probe_dependent: Callable[[str], Awaitable[bool]] | None = None, lease_ttl_s: int = 120,
    lease_renewal_interval_s: float | None = None,
) -> RecoveryOutcome:
    """execute_recovery + idempotency + lease. Đảm bảo: chạy đúng 1 lần, 1 writer.

    Trình tự an toàn:
      A. LEASE single-writer: acquire scope; giữ được mới là writer hợp lệ; không
         được (agent khác đang giữ) → ZERO mutation.
      B. IDEMPOTENCY: key đã terminal (đã chạy) → reconcile, ZERO mutation mới.
         Crash giữa chừng (claim cũ, holder đã mất vì ta giành được lease) → để
         execute_recovery REVALIDATE current-state: service đã healthy → abort (đã
         khôi phục); còn hỏng → chạy lại an toàn (mutation chưa hiệu lực).

    Lease renewal (long-running safety): ``execute_recovery`` chạy song song với một
    coordinator renew lease mỗi ``lease_renewal_interval_s`` (mặc định ``lease_ttl_s/4``
    — safety margin ≥4x). Nếu renew thất bại (``ownership_lost``: token không còn khớp
    holder — TTL đã hết VÀ agent khác đã acquire), mutation KHÔNG bị huỷ giữa chừng (side
    effect có thể đã xảy ra, huỷ mù không an toàn hơn) nhưng kết quả "recovered" KHÔNG
    còn đáng tin (agent khác có thể ĐANG mutate cùng scope) → escalate cho người xử lý
    thay vì tự nhận COMPLETED. "aborted" (chưa mutate, vd đã healthy/gate chặn) không bị
    ảnh hưởng vì không có side effect nào bị đe doạ bởi mất ownership.
    """
    ledger = IdempotencyLedger(redis)
    lease = ExecutionLease(redis)
    key = _key_for(req)
    target = req.failed_node          # lease khóa theo TARGET scope (mutating target)
    # Lease key PHẢI nhúng tenant (#5): "svc:{unit}" trần khiến 2 tenant khác nhau
    # thao tác cùng unit name đụng chung lease key của nhau. canonical_scope() là
    # cùng convention intake.py (Track A) đã dùng — Track B (đây) trước đây lệch.
    lease_scope = canonical_scope(req.tenant, target)
    scope = req.action.scope          # idempotency/audit theo action scope
    trace = scope
    renewal_interval = lease_renewal_interval_s or max(lease_ttl_s / 4, 1.0)

    # ── A. LEASE: chỉ single-writer hợp lệ trên TARGET (tenant-scoped) mới đi tiếp ─
    token = await lease.acquire(lease_scope, holder=holder, ttl_s=lease_ttl_s)
    if token is None:
        audit_log.append(audit.EV_RECOVERY_LEASE_DENIED,
                         {"target": target, "tenant": req.tenant, "holder": holder,
                          "current": await lease.holder_token(lease_scope)}, trace_id=trace)
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

        if existing and existing.get("status") in {
            STATUS_MUTATION_STARTED, STATUS_VERIFYING, STATUS_RECONCILE_REQUIRED,
        }:
            # A prior process may have dispatched a host side effect. The generic
            # recovery operator cannot prove its outcome safely, so stop here and
            # leave a durable escalation for action-specific reconciliation.
            await ledger.set_phase(
                key, phase=STATUS_RECONCILE_REQUIRED, holder=holder,
                meta={"prior_phase": existing["status"], "scope": scope},
            )
            reason = ("reconcile_required: prior mutation phase is ambiguous; "
                      "zero blind re-dispatch")
            audit_log.append(audit.EV_RECOVERY_RECONCILED,
                             {"key": key, "prior_status": existing["status"],
                              "scope": scope, "action": "escalate"}, trace_id=trace)
            outcome = RecoveryOutcome(
                action=req.action.at(ActionState.FAILED, verified=False),
                status="escalated", reason=reason,
            )
            await ledger.record(key, status=outcome.status, outcome={"reason": reason})
            return outcome

        # Ta đang giữ lease ⇒ writer cũ (nếu có claim treo) đã biến mất → an toàn
        # nhận quyền. Claim (overwrite claim treo) rồi để execute_recovery tự
        # REVALIDATE current-state (backstop reconcile cho crash-after-mutation).
        await ledger._r.set(key, '{"status": "%s", "holder": "%s"}' % (STATUS_CLAIMED, holder),
                            ex=900)

        async def _renew() -> bool:
            return await lease.renew(lease_scope, token=token, ttl_s=lease_ttl_s)

        async def _phase_hook(phase: str, meta: dict) -> None:
            phase_map = {
                "mutation_started": STATUS_MUTATION_STARTED,
                "verifying": STATUS_VERIFYING,
            }
            await ledger.set_phase(key, phase=phase_map[phase], holder=holder, meta=meta)

        renewal = await run_with_renewal(
            execute_recovery(ctx, req=req, transport=transport, audit_log=audit_log, gate=gate,
                             approval=approval, env_auto_execute=env_auto_execute, now=now,
                             probe_dependent=probe_dependent, phase_hook=_phase_hook),
            renew_fn=_renew, interval_s=renewal_interval, label="execution_lease")
        outcome = renewal.result

        if renewal.ownership_lost and outcome.status == "recovered":
            audit_log.append(audit.EV_RECOVERY_OWNERSHIP_LOST,
                             {"target": target, "scope": scope, "holder": holder},
                             trace_id=trace)
            outcome = RecoveryOutcome(
                action=outcome.action.at(ActionState.FAILED, verified=False),
                status="escalated",
                reason=("ownership_lost_during_mutation_ambiguous: lease hết hạn giữa mutation, "
                        "agent khác có thể đã/đang mutate cùng scope — cần xác minh thủ công"),
                evidence=outcome.evidence)

        # Record terminal CHỈ khi đã thực sự qua gate và chạy (recovered/escalated).
        # aborted (gate chặn / service healthy) → nhả claim để lần hợp lệ sau thử lại.
        if outcome.status in ("recovered", "escalated"):
            await ledger.record(key, status=outcome.status, outcome={"reason": outcome.reason})
        else:
            await ledger.release_claim(key)
        return outcome
    finally:
        await lease.release(lease_scope, token=token)


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


# ── Durable-command executor adapter (Step: nối daemon → run_guarded_recovery) ──────
#
# ``daemon.DeliveryLoop`` gọi ``executor(payload) -> (terminal_state, outcome)`` với
# ``payload`` là dict tự do (durable command's ``payload`` field — Gateway KHÔNG biết
# domain aoip). Adapter dưới đây là RANH GIỚI hẹp: parse payload → RecoveryRequest +
# Approval → gọi ``run_guarded_recovery`` (KHÔNG bypass lease/ledger) → map
# RecoveryOutcome sang (terminal_state, outcome dict) theo semantics daemon cần.
#
# Payload contract (typed, không parse được → fail-closed, KHÔNG gọi executor mutation):
#   {
#     "recovery": {failed_node, failure_mode, substrate, unit, risk, diagnosed_at,
#                  tenant, port?, dependents?},
#     "approval": {approver, tenant, decision_goal, expires_at, action_id,
#                  canonical_scope, issued_at, action_scope?},
#     "evidence": {diagnosis_confidence?, findings?: [{claim, verdict, confidence,
#                  references?}]},
#   }
_REQUIRED_RECOVERY_FIELDS = (
    "failed_node", "failure_mode", "substrate", "unit", "risk", "diagnosed_at", "tenant",
)
_REQUIRED_APPROVAL_FIELDS = (
    "approver", "tenant", "decision_goal", "expires_at", "action_id", "canonical_scope", "issued_at",
)
_HEALTHY_NO_ACTION_MARKER = "HEALTHY"


class UnsupportedRecoveryPayload(Exception):
    """Payload không decode được thành RecoveryRequest/Approval hợp lệ.

    Bất kỳ chỗ raise này PHẢI fail-closed: KHÔNG gọi ``run_guarded_recovery``.
    """


@dataclass
class _EvidenceCtx:
    """Carrier tối thiểu cho ``_gate_checks``/``execute_recovery`` (chỉ đọc ``.findings``,
    ``.diagnosis_confidence``, ``.log()``). KHÔNG phải noun ontology mới — chỉ mang
    evidence đã có sẵn trong payload sang đúng shape ``ctx`` mà recovery gate cần."""

    findings: list[Finding] = field(default_factory=list)
    diagnosis_confidence: float | None = None
    trace: list[str] = field(default_factory=list)

    def log(self, verb: str, detail: str) -> None:
        self.trace.append(f"{verb}: {detail}")


def _require(d: dict, keys: tuple[str, ...], what: str) -> None:
    missing = [k for k in keys if d.get(k) is None]
    if missing:
        raise UnsupportedRecoveryPayload(f"{what} thiếu field: {missing}")


# Fields excluded from the hash despite living inside a hashed section —
# high-precision float timestamps that do not survive re-serialization
# identically across the JSON libraries a payload actually passes through in
# production (Python json.dumps on the CLI -> Pydantic/pydantic-core on the
# gateway -> Redis storage -> httpx on the VM daemon). Caught live 2026-07-21:
# a real, untampered payload from the real CLI failed hash verification
# end-to-end (100% false-positive) because reason.diagnosed_at's ~17
# significant digits round-tripped to a bit-identical float but a
# byte-different JSON string somewhere in that chain. This is not a security
# regression — diagnosed_at is freshness metadata (bounded separately by
# approval.issued_at/expires_at, which ARE NOT part of this hash's input at
# all), not an identity field. mission_id/decision_id/incident_id/summary
# inside "reason", and target.unit, remain hashed and protected.
_HASH_VOLATILE_PATHS: tuple[tuple[str, ...], ...] = (("reason", "diagnosed_at"),)


def _strip_volatile_for_hash(typed_payload: dict) -> dict:
    result = json.loads(json.dumps(typed_payload))  # deep copy, JSON-safe
    for *parents, leaf in _HASH_VOLATILE_PATHS:
        node = result
        for key in parents:
            node = node.get(key) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, dict):
            node.pop(leaf, None)
    return result


# Canonical home for this hash (Phase 3, 0-6 roadmap — unify Stack A/Stack B):
# was defined only in aoip/capabilities/systemd_restart.py, which the live
# daemon's executor (build_recovery_executor, below) never called — Stack B's
# decode_recovery_command() had no payload-hash tamper-binding at all.
# systemd_restart.py now imports this definition instead of duplicating it
# (it already imports run_guarded_recovery from this module, so importing in
# the other direction would be circular — this module must own the shared
# definition).
def capability_payload_hash(typed_payload: dict) -> str:
    """Hash canonical (sorted keys, không whitespace) — đổi BẤT KỲ field nào (kể cả
    reason.summary) sau khi approval issue → hash khác → approval mất hiệu lực.
    Volatile-but-non-identity fields (see _HASH_VOLATILE_PATHS) are stripped
    first so re-serialization jitter across the real transport chain does not
    produce false-positive tamper detection."""
    canonical_input = _strip_volatile_for_hash(typed_payload)
    canonical = json.dumps(canonical_input, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


_TYPED_CAPABILITY_HASH_FIELDS = (
    "capability", "capability_version", "target", "reason", "preconditions", "verification",
)


def decode_recovery_command(payload: dict) -> tuple[RecoveryRequest, Approval, _EvidenceCtx]:
    """Parse durable command payload → (RecoveryRequest, Approval, ctx).

    Raise ``UnsupportedRecoveryPayload`` fail-closed nếu thiếu field hoặp không có
    operator cho (failure_mode, substrate) — caller KHÔNG được gọi mutation executor
    khi bắt exception này.

    Payload-hash tamper-binding (Phase 3): when the payload was built via a
    typed capability (issue_capability_command() always sets
    "approved_payload_hash" — every real caller today, both the operator CLI
    and command_bridge.build_durable_command, goes through it), verify the
    hash before trusting anything else in the payload. A payload missing
    "capability" entirely (hypothetical future non-typed caller) skips this —
    there is nothing typed to bind a hash to.
    """
    if payload.get("capability") is not None:
        expected_hash = payload.get("approved_payload_hash", "")
        typed_only = {k: v for k, v in payload.items() if k in _TYPED_CAPABILITY_HASH_FIELDS}
        actual_hash = capability_payload_hash(typed_only)
        if not expected_hash or actual_hash != expected_hash:
            raise UnsupportedRecoveryPayload(
                f"payload_hash_mismatch: payload đã đổi sau khi approval issue — "
                f"approval mất hiệu lực (expected={expected_hash!r}, actual={actual_hash!r})"
            )

    rec_d = payload.get("recovery") or {}
    _require(rec_d, _REQUIRED_RECOVERY_FIELDS, "recovery")
    if operator_for(rec_d["failure_mode"], rec_d["substrate"]) is None:
        raise UnsupportedRecoveryPayload(
            f"unsupported_capability: không có operator cho "
            f"({rec_d['failure_mode']!r}, {rec_d['substrate']!r})")

    action = plan_recovery(failed_node=rec_d["failed_node"], failure_mode=rec_d["failure_mode"],
                           substrate=rec_d["substrate"], unit=rec_d["unit"],
                           port=rec_d.get("port"), risk=float(rec_d["risk"]))
    action = action.at(ActionState.APPROVED)
    req = RecoveryRequest(failed_node=rec_d["failed_node"], failure_mode=rec_d["failure_mode"],
                          substrate=rec_d["substrate"], unit=rec_d["unit"], port=rec_d.get("port"),
                          action=action, risk=float(rec_d["risk"]),
                          diagnosed_at=float(rec_d["diagnosed_at"]),
                          dependents=tuple(rec_d.get("dependents") or ()), tenant=rec_d["tenant"],
                          mission_id=str(payload.get("mission_id") or rec_d.get("mission_id") or ""),
                          incident_id=str(payload.get("incident_id") or rec_d.get("incident_id") or ""),
                          decision_id=str(payload.get("decision_id") or rec_d.get("decision_id") or ""),
                          command_id=str(payload.get("command_id") or rec_d.get("command_id") or ""))

    appr_d = payload.get("approval") or {}
    _require(appr_d, _REQUIRED_APPROVAL_FIELDS, "approval")
    try:
        approval = Approval.issue(
            approver=appr_d["approver"], tenant=appr_d["tenant"],
            canonical_scope=appr_d["canonical_scope"], decision_goal=appr_d["decision_goal"],
            action_id=appr_d["action_id"], action_scope=appr_d.get("action_scope") or action.scope,
            issued_at=float(appr_d["issued_at"]), expires_at=float(appr_d["expires_at"]))
    except ValueError as exc:
        raise UnsupportedRecoveryPayload(f"invalid_approval: {exc}") from exc
    # Bind the immutable action identity from the approval into the request so
    # idempotency cannot collapse two actions with the same intent.
    req = replace(req, action_id=approval.action_id)

    ev_d = payload.get("evidence") or {}
    findings = [
        Finding(claim=f["claim"], references=tuple(f.get("references") or ()),
               verdict=bool(f.get("verdict", False)), confidence=float(f.get("confidence", 0.0)))
        for f in (ev_d.get("findings") or ())
    ]
    ctx = _EvidenceCtx(findings=findings, diagnosis_confidence=ev_d.get("diagnosis_confidence"))
    return req, approval, ctx


def _map_recovery_outcome(outcome: RecoveryOutcome) -> tuple[str, dict]:
    """RecoveryOutcome → (terminal_state, outcome dict) theo semantics daemon durable channel.

    KHÔNG tự phát minh terminal state ngoài COMPLETED/FAILED/ESCALATED (đã có ở
    Gateway ``agent_runtime.TERMINAL``). "aborted vì đã healthy" → COMPLETED +
    NO_ACTION_NEEDED (evidence current-state, KHÔNG mutation). "aborted" khác (gate/
    lease/approval chặn) → FAILED (KHÔNG COMPLETED — đây KHÔNG phải generic no-op).
    """
    base = {"status": outcome.status, "reason": outcome.reason, "evidence": list(outcome.evidence)}
    if outcome.status == "recovered":
        return "COMPLETED", {**base, "rc": 0, "verified": True}
    if outcome.status == "aborted" and _HEALTHY_NO_ACTION_MARKER in outcome.reason:
        return "COMPLETED", {**base, "rc": 0, "outcome": "NO_ACTION_NEEDED"}
    if outcome.status == "escalated":
        return "ESCALATED", {**base, "rc": 1}
    return "FAILED", {**base, "rc": 1}  # aborted (gate/lease/approval blocked)


def build_recovery_executor(
    *, redis, holder: str, transport, audit_log: audit.FileAuditLog, gate: RecoveryGate,
    env_auto_execute: bool = False, lease_ttl_s: int = 120,
    probe_dependent: Callable[[str], Awaitable[bool]] | None = None,
    now: Callable[[], float] | None = None,
) -> Callable[[dict], Awaitable[tuple[str, dict]]]:
    """Production-safe default executor cho ``daemon.run_daemon`` — thay ``_noop_executor``.

    Adapter hẹp: KHÔNG business logic recovery ở đây (chỉ decode + gọi
    ``run_guarded_recovery`` nguyên vẹn, giữ lease+ledger). Exception bất kỳ (transport
    crash, redis lỗi...) → FAILED rõ ràng, KHÔNG bao giờ trở thành COMPLETED ngầm.
    """

    clock = now or time.time

    async def executor(payload: dict) -> tuple[str, dict]:
        try:
            req, approval, ctx = decode_recovery_command(payload)
        except UnsupportedRecoveryPayload as exc:
            return "FAILED", {"rc": 1, "reason": f"unsupported_or_invalid_payload: {exc}",
                              "evidence": []}
        try:
            outcome = await run_guarded_recovery(
                ctx, req=req, transport=transport, audit_log=audit_log, gate=gate,
                approval=approval, env_auto_execute=env_auto_execute, now=clock(),
                redis=redis, holder=holder, probe_dependent=probe_dependent,
                lease_ttl_s=lease_ttl_s)
        except Exception as exc:  # noqa: BLE001 — mutation-path exception KHÔNG được thành success
            return "FAILED", {"rc": 1, "reason": f"executor_exception: {exc}", "evidence": []}
        return _map_recovery_outcome(outcome)

    return executor
