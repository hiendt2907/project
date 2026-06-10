"""PlaybookEngine — thực thi PlaybookSpec qua backend (k8s-first), fail-closed mọi gate.

Pipeline 1 lần chạy:
  kill-switch → governor.gate (frozen/graduation) → blast-radius lock
  → CRAT PLAYBOOK_EXECUTED(phase=start) [FAIL-CLOSED: CRAT fail = abort]
  → proof-of-fault (reconcile ground truth, verdict PHẢI "confirmed")
  → steps: render params → run_execute_mutate_tool → verify (reconcile settle loop)
  → verify fail ⇒ rollback snapshot + breaker.record_rollback + outcome fail (demote)
  → verify ok  ⇒ outcome success (có thể promote CANDIDATE→GRADUATED)
  → release lock + publish_action_feedback.

LLM không xuất hiện ở đây — engine chỉ nhận PlaybookSpec + render_ctx đã typed.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from services.audit_ledger.chain_writer import write_audit_block
from services.audit_ledger.crat_event_types import (
    CRAT_EVENT_CIRCUIT_BREAKER_TRIPPED,
    CRAT_EVENT_PLAYBOOK_DEMOTED,
    CRAT_EVENT_PLAYBOOK_EXECUTED,
    CRAT_EVENT_PLAYBOOK_GRADUATED,
    CRAT_EVENT_PLAYBOOK_PROOF_FAILED,
)
from services.audit_ledger.signer import AuditLedgerError
from workers.playbook_governor import PlaybookGovernor
from workers.schemas.playbook import PlaybookSpec, render_params

logger = logging.getLogger(__name__)

_PROMOTE_MIN_SUCCESS = 3


@dataclass(frozen=True)
class PlaybookRunResult:
    status: str  # "ok" | "skipped" | "proof_failed" | "rolled_back" | "error"
    detail: str
    steps_executed: int = 0


def _advisory_from_render_ctx(spec: PlaybookSpec, render_ctx: dict[str, str]) -> SimpleNamespace:
    """Claim tối thiểu cho reconcile_advisory: root_cause + affected_workload."""
    ns = str(render_ctx.get("namespace") or "").strip()
    target = str(
        render_ctx.get("deployment") or render_ctx.get("pod") or render_ctx.get("name") or ""
    ).strip()
    workload = f"{ns}/{target}" if ns and target else target
    keywords = " ".join(spec.proof_of_fault.fault_keywords or spec.trigger.fault_keywords)
    root_cause = f"{spec.name}: {keywords} on {workload}".strip()
    return SimpleNamespace(root_cause=root_cause, affected_workload=workload)


async def _crat(ctx: Any, event_type: str, trace: str, payload: dict[str, Any]) -> bool:
    """Ghi CRAT; trả False khi fail (caller quyết định fail-closed hay best-effort)."""
    ws = ctx.settings
    try:
        await write_audit_block(
            event_type=event_type,
            trace_id=trace,
            payload={"trace_id": trace, **payload},
            redis=ctx.redis,
            kafka=getattr(ctx, "kafka", None),
            kafka_topic=getattr(ws, "kafka_topic_audit_chain", "omni-audit-chain"),
        )
        return True
    except AuditLedgerError as exc:
        logger.critical("event=playbook_crat_failed type=%s trace=%s err=%s", event_type, trace, exc)
        return False
    except Exception as exc:  # noqa: BLE001
        logger.critical("event=playbook_crat_unexpected type=%s trace=%s err=%s", event_type, trace, exc)
        return False


async def _verify_step(
    ctx: Any, trace: str, advisory: SimpleNamespace, *, settle_sec: int, attempts: int
) -> tuple[bool, str]:
    """Settle-window reconcile: healthy đọc được trong window ⇒ pass; lần đọc CUỐI
    vẫn refuted/unhealthy ⇒ fail. (Cùng semantics với _post_mutate_reconcile_and_rollback.)"""
    from workers.verify_reconcile import reconcile_advisory

    interval = max(1.0, settle_sec / max(1, attempts))
    verdict = "unverifiable"
    pod_unhealthy = False
    for attempt in range(1, attempts + 1):
        await asyncio.sleep(interval)
        outcome = await reconcile_advisory(ctx, advisory)
        verdict = str(getattr(outcome, "verdict", "unverifiable"))
        pod = getattr(outcome, "pod", None)
        pod_unhealthy = bool(pod is not None and pod.found and not pod.is_healthy())
        if verdict != "confirmed" and not pod_unhealthy:
            # Fault claim không còn confirm trên ground truth + pod khoẻ ⇒ đã hồi phục.
            return True, f"verify_pass attempt={attempt} verdict={verdict}"
        logger.info(
            "[%s] event=playbook_verify_wait attempt=%d/%d verdict=%s pod_unhealthy=%s",
            trace, attempt, attempts, verdict, pod_unhealthy,
        )
    return False, f"verify_fail verdict={verdict} pod_unhealthy={pod_unhealthy}"


async def run_playbook(
    ctx: Any,
    *,
    trace: str,
    spec: PlaybookSpec,
    render_ctx: dict[str, str],
    tenant: str = "default",
    hitl_approved: bool = False,
) -> PlaybookRunResult:
    """Chạy 1 playbook. Caller (kafka_actions_consumer) lo publish_action_feedback."""
    from workers.autonomous_execute import run_execute_mutate_tool
    from workers.rollback_executor import apply_rollback_from_snapshot

    ws = ctx.settings
    gov = PlaybookGovernor(ctx.redis)

    if not bool(getattr(ws, "omni_auto_execute_enabled", False)):
        return PlaybookRunResult("skipped", "OMNI_AUTO_EXECUTE_ENABLED is false — playbook not run.")

    decision = await gov.gate(tenant, spec.domain, spec.playbook_id, initial=spec.initial_graduation)
    requires_hitl = spec.any_step_requires_hitl() or decision.graduation_state == "CANDIDATE"
    if not decision.allowed and not (hitl_approved and requires_hitl and decision.reason == "candidate_requires_hitl_or_suggest"):
        return PlaybookRunResult("skipped", f"governor_denied reason={decision.reason} state={decision.graduation_state}")
    if spec.any_step_requires_hitl() and not hitl_approved:
        return PlaybookRunResult("skipped", "playbook step requires HITL approval (not granted)")

    lock_ttl = max(s.timeout_sec + s.verify.settle_sec for s in spec.steps) + 60
    if not await gov.acquire_blast_lock(tenant, trace, ttl_sec=lock_ttl):
        return PlaybookRunResult("skipped", "blast_radius_lock_busy (1 mutation in-flight per tenant)")

    try:
        # CRAT fail-closed TRƯỚC mọi mutate.
        crat_ok = await _crat(ctx, CRAT_EVENT_PLAYBOOK_EXECUTED, trace, {
            "phase": "start", "playbook_id": spec.playbook_id, "version": spec.version,
            "domain": spec.domain, "tenant": tenant,
            "graduation_state": decision.graduation_state, "hitl_approved": hitl_approved,
        })
        if not crat_ok:
            return PlaybookRunResult("error", "CRAT_WRITE_FAILED_FAIL_CLOSED — playbook aborted before mutate")

        # Proof-of-fault: fault PHẢI còn confirm trên ground truth ngay trước mutate.
        from workers.verify_reconcile import reconcile_advisory

        advisory = _advisory_from_render_ctx(spec, render_ctx)
        proof = await reconcile_advisory(ctx, advisory)
        proof_verdict = str(getattr(proof, "verdict", "unverifiable"))
        pod = getattr(proof, "pod", None)
        pod_unhealthy = bool(pod is not None and pod.found and not pod.is_healthy())
        if proof_verdict != "confirmed" and not pod_unhealthy:
            await _crat(ctx, CRAT_EVENT_PLAYBOOK_PROOF_FAILED, trace, {
                "playbook_id": spec.playbook_id, "verdict": proof_verdict,
                "evidence": str(getattr(proof, "evidence", ""))[:800],
            })
            return PlaybookRunResult(
                "proof_failed",
                f"PROOF_OF_FAULT_FAILED verdict={proof_verdict} — fault not confirmed on ground truth; no mutate.",
            )

        steps_done = 0
        for step in spec.ordered_steps():
            try:
                args = render_params(step.params_template, render_ctx)
            except ValueError as exc:
                return PlaybookRunResult("error", f"param_render_failed step={step.step_order}: {exc}", steps_done)

            out, exit_code = await run_execute_mutate_tool(
                ctx, tool_name=step.action, args=args, trace_id=trace,
            )
            if exit_code != 0:
                await gov.record_outcome(tenant, spec.domain, spec.playbook_id,
                                         success=False, promote_min_success=_PROMOTE_MIN_SUCCESS)
                return PlaybookRunResult(
                    "error", f"step={step.step_order} tool={step.action} exit={exit_code} out={out[:400]}", steps_done,
                )
            steps_done += 1

            ok, vmsg = await _verify_step(
                ctx, trace, advisory,
                settle_sec=step.verify.settle_sec, attempts=step.verify.attempts,
            )
            if ok:
                continue

            # Verify fail ⇒ rollback (nếu có snapshot) + breaker + demote.
            rb_msg = "rollback_type=none (no rollback performed)"
            if step.rollback_type == "snapshot":
                target = str(args.get("name") or args.get("deployment") or args.get("pod") or "").strip()
                ctx.rollback_target_name = target  # type: ignore[attr-defined]
                _ok_rb, rb_msg = await apply_rollback_from_snapshot(ctx, trace)
            tripped = await gov.record_rollback(tenant, trace)
            from_s, to_s = await gov.record_outcome(
                tenant, spec.domain, spec.playbook_id, success=False,
                promote_min_success=_PROMOTE_MIN_SUCCESS,
            )
            if from_s != to_s:
                await _crat(ctx, CRAT_EVENT_PLAYBOOK_DEMOTED, trace, {
                    "playbook_id": spec.playbook_id, "from": from_s, "to": to_s,
                    "reason": vmsg, "tenant": tenant,
                })
            if tripped:
                await gov.freeze(tenant, spec.domain, spec.playbook_id)
                await _crat(ctx, CRAT_EVENT_CIRCUIT_BREAKER_TRIPPED, trace, {
                    "tenant": tenant, "playbook_id": spec.playbook_id, "detail": vmsg,
                })
            return PlaybookRunResult(
                "rolled_back",
                f"step={step.step_order} {vmsg}; {rb_msg}; breaker_tripped={tripped}",
                steps_done,
            )

        from_s, to_s = await gov.record_outcome(
            tenant, spec.domain, spec.playbook_id, success=True,
            promote_min_success=_PROMOTE_MIN_SUCCESS,
        )
        if from_s != to_s:
            await _crat(ctx, CRAT_EVENT_PLAYBOOK_GRADUATED, trace, {
                "playbook_id": spec.playbook_id, "from": from_s, "to": to_s, "tenant": tenant,
            })
        await _crat(ctx, CRAT_EVENT_PLAYBOOK_EXECUTED, trace, {
            "phase": "done", "playbook_id": spec.playbook_id, "steps": steps_done,
            "tenant": tenant, "graduation": to_s or from_s,
        })
        return PlaybookRunResult("ok", f"playbook completed steps={steps_done} graduation={to_s or from_s}", steps_done)
    finally:
        await gov.release_blast_lock(tenant, trace)
