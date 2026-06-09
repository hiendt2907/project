"""Consume ``omni-actions`` — ``execute_write_pending`` (legacy), ``EXECUTE_MUTATE`` (autonomous), audit-only ``SUGGEST_REMEDIATION``."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from types import SimpleNamespace
from typing import Any

from aiokafka import AIOKafkaConsumer

from messaging.kafka_bus import decode_kafka_value_to_fields, kafka_msg_id
from pkg.executor import execute_write_pending_from_redis
from pkg.reasoning.reason_codes import ERR_GOV_UNAUTHORIZED_MUTATION
from workers.env_mode import is_dev_mode
from workers.autonomous_execute import publish_action_feedback, run_execute_mutate_tool
from workers.handler_context import WorkerHandlerContext
from workers.log_preview import json_obj_preview, log_preview
from workers.metrics_exporter import executor_execute_skipped_inc
from workers.pipeline_stages import mark_stage
from workers.request_trace import pop_trace_id, push_trace_id
from workers.autonomy_contract import (
    TRANSITION_COMMAND_FEEDBACK_INGESTED,
    TRANSITION_DRY_RUN_FAILED,
    TRANSITION_DRY_RUN_PASSED,
    TRANSITION_EXECUTED,
    TRANSITION_OS_RUNBOOK_EMITTED,
    TRANSITION_PLAN_EMITTED,
    emit_terminal_tombstone,
    emit_transition,
)

logger = logging.getLogger(__name__)


def _omni_actions_body_preview(body: dict[str, Any]) -> str:
    """Human-readable English preview for executor audit logs."""
    act = str(body.get("action") or "").strip().lower()
    data = body.get("data")
    if act == "suggest_remediation" and isinstance(data, dict):
        diag = str(data.get("diagnosis") or "").replace("\n", " ").strip()[:900]
        tool = str(data.get("suggested_tool") or "").strip()
        conf = data.get("confidence")
        src = str(data.get("source") or "").strip()
        return (
            f"Diagnosis: {diag} "
            f"Confidence: {conf} Source: {src} Suggested tool: {tool}."
        )
    if act == "execute_mutate" and isinstance(data, dict):
        return (
            f"tool={data.get('tool_name')} attempt_count={data.get('attempt_count')} "
            f"correlation_id={data.get('correlation_id')}"
        )
    if act == "suggest_os_runbook" and isinstance(data, dict):
        cmds = data.get("commands") if isinstance(data.get("commands"), list) else []
        return (
            f"title={data.get('runbook_title')} steps={len(cmds)} "
            f"source={data.get('source')} confidence={data.get('confidence')}"
        )
    return json_obj_preview(body, max_chars=1200)


def _action_fingerprint(tool_name: str, args: dict[str, Any]) -> str:
    try:
        norm = json.dumps(
            {
                "tool": str(tool_name or "").strip(),
                "ns": str(args.get("namespace") or "").strip(),
                "deployment": str(args.get("deployment") or "").strip(),
                "name": str(args.get("name") or "").strip(),
                "kind": str(args.get("resource_type") or "").strip(),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:24]
    except Exception:
        return "na"


async def _is_rate_limited(ctx: WorkerHandlerContext, tool_name: str, args: dict[str, Any]) -> bool:
    ws = ctx.settings
    burst = int(getattr(ws, "executor_action_rate_limit_burst", 6) or 6)
    window_sec = int(getattr(ws, "executor_action_rate_limit_window_sec", 60) or 60)
    fp = _action_fingerprint(tool_name, args)
    key = f"omni:executor:rate:{fp}"
    try:
        n = int(await ctx.redis.incr(key))
        if n == 1:
            await ctx.redis.expire(key, window_sec)
        return n > burst
    except Exception:
        return False


async def kafka_actions_loop(ctx: WorkerHandlerContext, stop: asyncio.Event) -> None:
    """Executor: ``execute_write_pending`` | ``EXECUTE_MUTATE``; ``SUGGEST_REMEDIATION`` = audit only."""
    ws = ctx.settings
    consumer = AIOKafkaConsumer(
        ws.kafka_topic_actions,
        bootstrap_servers=ws.kafka_bootstrap_servers,
        group_id=ws.consumer_group_executor,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        client_id=ws.consumer_name_executor,
    )
    await consumer.start()
    try:
        async for msg in consumer:
            if stop.is_set():
                break
            try:
                fields = decode_kafka_value_to_fields(msg.value, msg.headers)
                raw = fields.get("data") or fields.get("payload") or "{}"
                body = json.loads(raw)
                action_raw = str(body.get("action") or "").strip()
                action = action_raw.lower().replace("-", "_")
                trace = str(
                    body.get("trace_id")
                    or fields.get("trace_id")
                    or kafka_msg_id(msg.topic, msg.partition, msg.offset)
                )
                data = body.get("data")
                tok = push_trace_id(trace)
                try:
                    ctx.inbound_trace_id = trace
                    await emit_transition(
                        ctx,
                        trace_id=trace,
                        transition=TRANSITION_PLAN_EMITTED,
                        component="kafka_actions_consumer",
                        detail=f"action_received:{action or 'unknown'}",
                    )
                    logger.info(
                        "[%s] event=omni_actions_in action=%s body_preview=%s",
                        trace,
                        action_raw or "(empty)",
                        _omni_actions_body_preview(body),
                    )
                    if not isinstance(data, dict):
                        logger.warning("[%s] omni-actions skip: data not object", trace)
                        await consumer.commit()
                        continue
                    if action == "execute_write_pending":
                        out = await execute_write_pending_from_redis(ctx, data)
                        await mark_stage(ctx.redis, trace, "EXECUTOR", "ok", detail="execute_write_pending")
                        logger.info(
                            "[%s] omni-actions execute_write_pending ok out_len=%s result_preview=%s",
                            trace,
                            len(out or ""),
                            log_preview(out, max_chars=1200),
                        )
                    elif action in ("execute_mutate", "action_execute_mutate"):
                        await _handle_execute_mutate(ctx, trace, data)
                        await mark_stage(ctx.redis, trace, "EXECUTOR", "ok", detail="execute_mutate")
                    elif action == "suggest_remediation":
                        await mark_stage(ctx.redis, trace, "EXECUTOR", "skip", detail="suggest-only (no execute)")
                        logger.info(
                            "[%s] event=omni_actions_audit_only action=SUGGEST_REMEDIATION (no execute)",
                            trace,
                        )
                    elif action == "suggest_os_runbook":
                        await emit_transition(
                            ctx,
                            trace_id=trace,
                            transition=TRANSITION_OS_RUNBOOK_EMITTED,
                            component="kafka_actions_consumer",
                            detail="suggest_os_runbook_received",
                            meta={"step_count": len(data.get("commands") or [])},
                        )
                        logger.info("[%s] event=omni_actions_shadow_runbook action=SUGGEST_OS_RUNBOOK", trace)
                    else:
                        logger.warning("[%s] omni-actions unknown action=%s", trace, action)
                    await consumer.commit()
                finally:
                    pop_trace_id(tok)
            except Exception as e:
                await ctx.ledger.record_exception(e, phase="4", component="kafka_actions_loop", swallow_errors=True)
                logger.exception("kafka_actions_loop message error: %s", e)
                await asyncio.sleep(0.5)
    finally:
        await consumer.stop()


def _advisory_from_envelope(data: dict[str, Any]) -> SimpleNamespace:
    """Reconstruct a minimal advisory-like object (root_cause + affected_workload)
    from the action envelope so ``reconcile_advisory`` can re-read ground truth.

    The full AnalystAdvisory may not travel in the action; we carry just the claim
    needed for ground-truth reconciliation: the asserted failure mode (root_cause)
    and the target (affected_workload = "namespace/pod-or-deployment").
    """
    args = data.get("args") if isinstance(data.get("args"), dict) else {}
    root_cause = str(
        data.get("root_cause")
        or data.get("claim")
        or data.get("diagnosis")
        or ""
    )
    workload = str(data.get("affected_workload") or "").strip()
    if not workload:
        ns = str(args.get("namespace") or "").strip()
        target = str(
            args.get("pod")
            or args.get("name")
            or args.get("deployment")
            or ""
        ).strip()
        if ns and target:
            workload = f"{ns}/{target}"
        elif target:
            workload = target
    return SimpleNamespace(root_cause=root_cause, affected_workload=workload)


async def _crat_rollback_executed(
    ctx: WorkerHandlerContext, trace: str, *, reason_code: str, rollback_msg: str, verdict: str
) -> None:
    """Write a ROLLBACK_EXECUTED CRAT block (fail-closed INVARIANT).

    Safety-over-audit for the rollback itself: an AuditLedgerError is logged but
    the rollback is still attempted/recorded — we never block a safety rollback on
    audit availability, but the attempt MUST be recorded.
    """
    from services.audit_ledger.chain_writer import write_audit_block
    from services.audit_ledger.crat_event_types import CRAT_EVENT_ROLLBACK_EXECUTED
    from services.audit_ledger.signer import AuditLedgerError

    ws = ctx.settings
    audit_topic = getattr(ws, "kafka_topic_audit_chain", "omni-audit-chain")
    try:
        await write_audit_block(
            event_type=CRAT_EVENT_ROLLBACK_EXECUTED,
            trace_id=trace,
            payload={
                "trace_id": trace,
                "reason_code": reason_code,
                "post_mutate_verdict": verdict,
                "rollback_msg": rollback_msg,
            },
            redis=ctx.redis,
            kafka=getattr(ctx, "kafka", None),
            kafka_topic=audit_topic,
        )
    except AuditLedgerError as crat_err:
        logger.critical(
            "[%s] event=rollback_crat_failed reason=%s err=%s (rollback still recorded)",
            trace, reason_code, crat_err,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[%s] event=rollback_crat_unexpected err=%s", trace, e)


async def _post_mutate_reconcile_and_rollback(
    ctx: WorkerHandlerContext,
    trace: str,
    data: dict[str, Any],
    *,
    tool_name: str,
    correlation_id: str,
    args: dict[str, Any],
) -> None:
    """After a successful mutation, re-read live ground truth. If the problem
    persists (verdict=refuted) or the target became unhealthy, auto-rollback to the
    pre-mutate snapshot and emit a ROLLBACK_EXECUTED CRAT block.
    """
    from workers.verify_reconcile import reconcile_advisory
    from workers.rollback_executor import apply_rollback_from_snapshot

    ws = ctx.settings
    if not bool(getattr(ws, "omni_auto_rollback_enabled", True)):
        return

    advisory = _advisory_from_envelope(data)
    outcome = await reconcile_advisory(ctx, advisory)
    verdict = str(getattr(outcome, "verdict", "unverifiable"))
    pod = getattr(outcome, "pod", None)
    pod_unhealthy = bool(pod is not None and pod.found and not pod.is_healthy())

    should_rollback = verdict == "refuted" or pod_unhealthy
    logger.info(
        "[%s] event=post_mutate_reconcile tool=%s verdict=%s pod_unhealthy=%s evidence=%s",
        trace, tool_name, verdict, pod_unhealthy,
        log_preview(getattr(outcome, "evidence", ""), max_chars=400),
    )

    if not should_rollback:
        # Verified-healthy: the mutate handler already emitted the terminal 'ok'
        # feedback for this action. Do NOT emit a second feedback here — one action
        # yields one feedback (avoids analyst double re-evaluation + KPI double-count).
        # The post-mutate ground-truth verdict is preserved in the log above.
        return

    reason_code = "POST_MUTATE_REFUTED" if verdict == "refuted" else "POST_MUTATE_UNHEALTHY"
    # Ensure rollback target name is set from the envelope (real bug if unset:
    # rollback_executor._apply reads ctx.rollback_target_name).
    target_name = str(args.get("name") or args.get("deployment") or args.get("pod") or "").strip()
    ctx.rollback_target_name = target_name  # type: ignore[attr-defined]

    rolled_ok, rb_msg = await apply_rollback_from_snapshot(ctx, trace)
    # CRAT fail-closed: record the ROLLBACK_EXECUTED attempt regardless.
    await _crat_rollback_executed(
        ctx, trace, reason_code=reason_code, rollback_msg=rb_msg, verdict=verdict,
    )
    logger.warning(
        "[%s] event=post_mutate_auto_rollback reason=%s rolled_back=%s msg=%s",
        trace, reason_code, rolled_ok, log_preview(rb_msg, max_chars=400),
    )
    await publish_action_feedback(
        ctx,
        trace_id=trace,
        tool_name=tool_name or "unknown",
        correlation_id=correlation_id,
        stdout=f"rolled_back={rolled_ok} reason={reason_code} verdict={verdict} {rb_msg}",
        stderr="",
        exit_code=1,
        status="rolled_back",
        mutate_args=args,
    )
    await emit_transition(
        ctx,
        trace_id=trace,
        transition=TRANSITION_EXECUTED,
        status="error",
        component="kafka_actions_consumer",
        detail=f"auto_rollback reason={reason_code} rolled_back={rolled_ok} tool={tool_name}",
    )


async def _handle_execute_mutate(ctx: WorkerHandlerContext, trace: str, data: dict[str, Any]) -> None:
    tool_name = str(data.get("tool_name") or "").strip()
    args = data.get("args") if isinstance(data.get("args"), dict) else {}
    correlation_id = str(data.get("correlation_id") or trace).strip()
    ws = ctx.settings
    dev_mode = is_dev_mode(ws)
    auto = bool(getattr(ws, "omni_auto_execute_enabled", False))
    if bool(getattr(ws, "omni_shadow_os_mode", False)):
        executor_execute_skipped_inc("shadow_os")
        await publish_action_feedback(
            ctx,
            trace_id=trace,
            tool_name=tool_name or "unknown",
            correlation_id=correlation_id,
            stdout="",
            stderr="",
            exit_code=-1,
            status="skipped",
            skipped_reason="OMNI_SHADOW_OS_MODE=true blocks SDK mutate execution.",
            mutate_args=args,
        )
        await emit_transition(
            ctx,
            trace_id=trace,
            transition=TRANSITION_EXECUTED,
            status="error",
            component="kafka_actions_consumer",
            detail=f"shadow_mode_blocked_mutate tool={tool_name}",
        )
        await emit_terminal_tombstone(
            ctx,
            trace_id=trace,
            reason_code="SHADOW_MODE_MUTATE_BLOCKED",
            component="kafka_actions_consumer",
            detail=f"tool={tool_name}",
        )
        return

    if not auto:
        executor_execute_skipped_inc("auto_execute_disabled")
        await publish_action_feedback(
            ctx,
            trace_id=trace,
            tool_name=tool_name or "unknown",
            correlation_id=correlation_id,
            stdout="",
            stderr="",
            exit_code=-1,
            status="skipped",
            skipped_reason="OMNI_AUTO_EXECUTE_ENABLED is false — mutate not run.",
            mutate_args=args,
        )
        logger.info("[%s] EXECUTE_MUTATE skipped (auto_execute disabled)", trace)
        return

    if await _is_rate_limited(ctx, tool_name, args):
        executor_execute_skipped_inc("rate_limited")
        await publish_action_feedback(
            ctx,
            trace_id=trace,
            tool_name=tool_name or "unknown",
            correlation_id=correlation_id,
            stdout="",
            stderr="",
            exit_code=-1,
            status="skipped",
            skipped_reason="EXECUTOR_ACTION_RATE_LIMITED",
            mutate_args=args,
        )
        await emit_terminal_tombstone(
            ctx,
            trace_id=trace,
            reason_code="ACTION_RATE_LIMITED",
            component="kafka_actions_consumer",
            detail=f"tool={tool_name}",
            meta={"fingerprint": _action_fingerprint(tool_name, args)},
        )
        return

    out, exit_code = await run_execute_mutate_tool(
        ctx,
        tool_name=tool_name,
        args=args,
        trace_id=trace,
    )
    await publish_action_feedback(
        ctx,
        trace_id=trace,
        tool_name=tool_name or "unknown",
        correlation_id=correlation_id,
        stdout=out,
        stderr="",
        exit_code=exit_code,
        status="ok" if exit_code == 0 else "error",
        mutate_args=args,
    )
    if exit_code != 0 and ERR_GOV_UNAUTHORIZED_MUTATION in (out or ""):
        logger.warning(
            "[%s] event=mutate_denied_non_mutating_tool tool=%s detail=%s",
            trace,
            tool_name or "unknown",
            log_preview(out, max_chars=400),
        )
    await emit_transition(
        ctx,
        trace_id=trace,
        transition=TRANSITION_EXECUTED,
        status="ok" if exit_code == 0 else "error",
        component="kafka_actions_consumer",
        detail=f"tool={tool_name} exit_code={exit_code}",
    )
    await emit_transition(
        ctx,
        trace_id=trace,
        transition=TRANSITION_COMMAND_FEEDBACK_INGESTED,
        status="ok" if exit_code == 0 else "error",
        component="kafka_actions_consumer",
        detail=f"feedback_published tool={tool_name} exit_code={exit_code}",
    )
    await emit_transition(
        ctx,
        trace_id=trace,
        transition=TRANSITION_DRY_RUN_PASSED if exit_code == 0 else TRANSITION_DRY_RUN_FAILED,
        status="ok" if exit_code == 0 else "error",
        component="kafka_actions_consumer",
        detail=f"dry_run_state tool={tool_name}",
    )

    # Post-mutate ground-truth reconcile → auto-rollback if the mutation did NOT
    # actually fix the problem (verdict still refuted / target now unhealthy).
    # Only runs when the tool-level mutation itself succeeded (exit_code == 0);
    # a failed mutation produced no state change to roll back.
    if exit_code == 0:
        await _post_mutate_reconcile_and_rollback(
            ctx,
            trace,
            data,
            tool_name=tool_name,
            correlation_id=correlation_id,
            args=args,
        )
