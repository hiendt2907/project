"""Analyst: consume omni-action-feedback — success → hot cache; fail → LLM replan + EXECUTE_MUTATE; escalate on limits."""

from __future__ import annotations

import asyncio
import json
import logging
import hashlib
import time
import uuid
from typing import Any

from aiokafka import AIOKafkaConsumer

from execution.memory_normalize import canonical_symptom_text, extract_workload_fingerprint, strip_ephemeral_from_args
from messaging.kafka_bus import decode_kafka_value_to_fields, kafka_msg_id
from rag.pgvector_store import COLLECTION_ACTION_EXPERIENCE, EMBED_DIM, PointStruct
from pkg.reasoning.sanitize import format_batch_sanitized_analyst_user_text
from workers.analyst_agentic_loop import (
    _parse_tool_json,
    run_post_mutate_state_verify_planner,
    run_post_verify_react_loop,
)
from workers.evidence_consumer import _emit_agentic_mutate_if_any
from pkg.reasoning.deterministic_mutate_from_evidence import (
    deterministic_mutate_plan_from_batch,
    default_remediation_namespace,
    probe_driven_mutate_tools_for_settings,
)
from workers.evidence_mutate_emit import emit_execute_mutate, store_autonomous_trace_context
from workers.llm_trace import agentic_parse_failure_hint, log_llm_trace
from pkg.reasoning.alert_identity import infer_root_cause_id, resolution_labels_payload
from workers.post_mutate_sdk_verify import probe_raws_to_batch_for_deterministic, run_verify_probes
from workers.proactive_models import AnomalyEvent
from workers.diagnostic_dispatcher import probe_ids_for_alertname
from workers.handler_context import WorkerHandlerContext
from workers.memory.initial_symptom import InitialSymptom
from workers.memory.trace_memory import append_post_mutate_verify_record
from pkg.trace_orchestrator.state import mark_trace_orchestrator_resolved_verified
from workers.archivist import write_incident_postmortem
from workers.rollback_executor import apply_rollback_from_snapshot
from workers.metrics_exporter import inc_experience_saved, inc_learning_upsert
from workers.telegram_escalation import emit_telegram_escalation
from workers.pipeline_stages import mark_stage
from workers.request_trace import pop_trace_id, push_trace_id
from workers.autonomy_contract import (
    TRANSITION_COMMAND_FEEDBACK_INGESTED,
    TRANSITION_EXECUTED,
    TRANSITION_PLAN_EMITTED,
    TRANSITION_POST_VERIFY_STATE_FAIL,
    TRANSITION_POST_VERIFY_STATE_OK,
    TRANSITION_REQUIRES_HUMAN,
    TRANSITION_RE_EVALUATED,
    TRANSITION_STATE_MACHINE_VERIFIED,
    emit_terminal_tombstone,
    emit_transition,
)
from workers.post_verify_deployment_state import (
    check_deployment_rollout_healthy,
    resolve_namespace_deployment_for_state_gate,
)

logger = logging.getLogger(__name__)

_STATE_KEY = "omni:autonomous:state:{trace}"
_HOT_KEY = "omni:autonomous:hot:{trace}"
_NEGATIVE_RECALL_THRESHOLD = 2  # Number of failures before adding to permanent negative set


async def _track_negative_recall(ctx: Any, trace: str) -> None:
    """S2.4: Downvote recall that contributed to a terminal failure."""
    redis = getattr(ctx, "redis", None)
    if redis is None:
        return
    try:
        raw = await redis.get(f"omni:recall:trace_point_id:{trace}")
        if not raw:
            return
        point_id = raw.decode() if isinstance(raw, bytes) else str(raw)
        if not point_id:
            return
        count = int(await redis.incr(f"omni:recall:negative:{point_id}"))
        await redis.expire(f"omni:recall:negative:{point_id}", 86400 * 30)
        logger.info(
            "event=recall_negative_tracked trace=%s point_id=%s count=%d",
            trace, point_id, count,
        )
        if count >= _NEGATIVE_RECALL_THRESHOLD:
            await redis.sadd("omni:recall:negative_set", point_id)
            logger.warning(
                "event=recall_added_to_negative_set trace=%s point_id=%s count=%d",
                trace, point_id, count,
            )
    except Exception as e:
        logger.debug("event=track_negative_recall_fail trace=%s err=%s", trace, e)


async def _attempt_auto_rollback(ctx: Any, trace: str, reason_code: str) -> bool:
    """S1.2: Try auto-rollback if enabled and snapshot exists. Writes ROLLBACK_EXECUTED to CRAT.

    Returns True when safe to continue (no rollback attempted, or rollback+CRAT both succeeded).
    Returns False when rollback succeeded but CRAT write failed — caller must NOT fire Telegram.
    """
    from services.audit_ledger.chain_writer import write_audit_block
    from services.audit_ledger.crat_event_types import CRAT_EVENT_ROLLBACK_EXECUTED
    from services.audit_ledger.signer import AuditLedgerError

    ws = getattr(ctx, "settings", None)
    if not bool(getattr(ws, "omni_auto_rollback_enabled", True)):
        return True
    try:
        ok, msg = await apply_rollback_from_snapshot(ctx, trace)
        if not ok:
            logger.info("[%s] event=auto_rollback_skip reason=%s msg=%s", trace, reason_code, msg)
            return True
        logger.info("[%s] event=auto_rollback_success reason=%s msg=%s", trace, reason_code, msg)
        kafka = getattr(ctx, "kafka", None)
        audit_topic = getattr(ws, "kafka_topic_audit_chain", "omni-audit-chain")
        try:
            await write_audit_block(
                event_type=CRAT_EVENT_ROLLBACK_EXECUTED,
                trace_id=trace,
                payload={"trace_id": trace, "reason_code": reason_code, "rollback_msg": msg},
                redis=ctx.redis,
                kafka=kafka,
                kafka_topic=audit_topic,
            )
        except AuditLedgerError as crat_err:
            logger.critical(
                "[%s] event=rollback_crat_failed reason=%s err=%s FAIL_CLOSED no_telegram",
                trace, reason_code, crat_err,
            )
            return False
        return True
    except Exception as e:
        logger.warning("[%s] event=auto_rollback_exception reason=%s err=%s", trace, reason_code, e)
        return True


async def _load_state(redis: Any, trace: str) -> dict[str, Any]:
    raw = await redis.get(_STATE_KEY.format(trace=trace))
    if not raw:
        return {"last_attempt_count": 0, "feedback_failures": 0, "sdk_verify_round": 0, "state_verify_attempt": 0}
    try:
        s = raw.decode() if isinstance(raw, bytes) else raw
        o = json.loads(s)
        if isinstance(o, dict):
            o.setdefault("sdk_verify_round", 0)
            o.setdefault("state_verify_attempt", 0)
            return o
        return {"last_attempt_count": 0, "feedback_failures": 0, "sdk_verify_round": 0, "state_verify_attempt": 0}
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as e:
        logger.warning("event=feedback_state_parse_error err=%r", e)
        return {"last_attempt_count": 0, "feedback_failures": 0, "sdk_verify_round": 0, "state_verify_attempt": 0}


async def _write_success_hot_cache(ctx: WorkerHandlerContext, trace: str, stdout: str) -> None:
    ws = ctx.settings
    ttl = int(getattr(ws, "rag_hot_cache_ttl_sec", 3600) or 3600)
    payload = {
        "trace_id": trace,
        "closed": True,
        "stdout_preview": (stdout or "")[:2000],
    }
    try:
        await ctx.redis.setex(_HOT_KEY.format(trace=trace), ttl, json.dumps(payload, ensure_ascii=False))
        logger.info("event=autonomous_case_closed trace=%s hot_cache=1", trace)
    except Exception as e:
        logger.warning("autonomous hot cache: %s", e)


def _args_hash(args: dict[str, Any]) -> str:
    raw = json.dumps(args or {}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


async def _load_autonomous_ctx_text(redis: Any, trace: str) -> str:
    raw = await redis.get(f"omni:autonomous:ctx:{trace}")
    if not raw:
        return ""
    try:
        s = raw.decode() if isinstance(raw, bytes) else raw
        obj = json.loads(s)
        if isinstance(obj, dict):
            return str(obj.get("sanitized_text") or "")[:4000]
    except Exception:
        return ""
    return ""


def _embedding_from_response(resp: dict[str, Any]) -> list[float]:
    if "embedding" in resp:
        emb = resp["embedding"]
        return list(emb) if not isinstance(emb, list) else emb
    embs = resp.get("embeddings")
    if isinstance(embs, list) and embs:
        return list(embs[0])
    return []


async def _upsert_action_experience_on_success(
    ctx: WorkerHandlerContext,
    *,
    trace: str,
    tool_name: str,
    mutate_args: dict[str, Any],
    stdout: str,
    sdk_verify_summary: str = "",
    ctx_obj: dict[str, Any] | None = None,
) -> None:
    try:
        ctx_text = await _load_autonomous_ctx_text(ctx.redis, trace)
        extra = ""
        if (sdk_verify_summary or "").strip():
            extra = f"\n---sdk_verify_ok---\n{(sdk_verify_summary or '')[:2000]}"
        symptom_raw = f"{ctx_text}\n\n{stdout[:1200]}{extra}".strip()
        symptom_text = canonical_symptom_text(symptom_raw, strip_pods=True)
        emb = await ctx.llm.embed(
            model=ctx.settings.embed_model,
            input=symptom_text[:4000],
        )
        vec = _embedding_from_response(emb)
        if len(vec) != EMBED_DIM:
            vec = (vec + [0.0] * EMBED_DIM)[:EMBED_DIM]
        args_pb = strip_ephemeral_from_args(mutate_args)
        ah = _args_hash(args_pb)
        pid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"diag-feedback:{trace}:{tool_name}:{ah}"))
        co = ctx_obj if isinstance(ctx_obj, dict) else {}
        rc_id = infer_root_cause_id(
            str(co.get("drift_type") or ""),
            str(co.get("alertname") or ""),
        )
        rc_desc = (
            f"tool={tool_name} alertname={co.get('alertname') or ''} "
            f"drift_type={co.get('drift_type') or ''}"
        ).strip()[:2000]
        verify_method = "sdk_probe_verify" if (sdk_verify_summary or "").strip() else "executor_only"
        res_labs = resolution_labels_payload(
            root_cause_id=rc_id,
            root_cause_desc=rc_desc,
            resolution_tool=str(tool_name or "")[:256],
            verify_method=verify_method,
        )
        payload = {
            "memory_kind": "playbook",
            "symptom_text": symptom_text[:2000],
            "workload_fingerprint": extract_workload_fingerprint(ctx_text or symptom_text),
            "args_playbook": args_pb,
            "lesson": f"[diagnostic feedback success] tool={tool_name} trace={trace} sdk_verify=ok"[:1200],
            "routing_source": "diagnostic_autonomous_feedback",
            "tool": tool_name,
            "arg_keys": sorted(str(k) for k in (mutate_args or {}).keys()),
            "args_hash": ah,
            "auto_execute": True,
            "match_text": (ctx_text or symptom_text)[:2000],
            "trace_id": trace,
            "exec_outcome": "success",
            "biz_outcome": "correct",
            "verification_result": "pass",
            "sdk_verify_summary": (sdk_verify_summary or "")[:2000],
            "unknown_reason": "",
            "latency_ms": 0,
            "safety_flag": "normal",
            "ts": str(int(time.time())),
            **res_labs,
        }
        await ctx.vector_store.upsert(
            collection_name=COLLECTION_ACTION_EXPERIENCE,
            points=[PointStruct(id=pid, vector=vec, payload=payload)],
        )
        inc_learning_upsert("diagnostic_feedback", "success")
        inc_experience_saved()
    except Exception as e:
        inc_learning_upsert("diagnostic_feedback", "fail")
        logger.debug("[%s] diagnostic feedback upsert skip: %s", trace, e)


async def _archive_postmortem(
    trace: str,
    tool_name: str,
    mutate_args: dict[str, Any],
    ctx_obj: dict[str, Any] | None,
) -> None:
    """Write REDACTED post-mortem to disk in a thread pool. Never raises."""
    co = ctx_obj if isinstance(ctx_obj, dict) else {}
    try:
        await asyncio.to_thread(
            write_incident_postmortem,
            trace,
            tool_name=tool_name,
            arg_keys=sorted(str(k) for k in (mutate_args or {}).keys()),
            alertname=str(co.get("alertname") or co.get("drift_type") or "unknown"),
            namespace=str(co.get("namespace") or ""),
            workload=str(co.get("workload") or co.get("deployment") or ""),
        )
    except Exception as e:
        logger.warning("event=archivist_postmortem_error trace=%s err=%s", trace, e)


async def _verify_state_machine_gate(
    ctx: WorkerHandlerContext,
    *,
    trace: str,
    body: dict[str, Any],
    mutate_args: dict[str, Any],
    ctx_obj: dict[str, Any] | None,
) -> tuple[bool, str]:
    """Require deployment-level healthy rollout before terminal success."""
    ev = _anomaly_event_from_redis_ctx(trace, ctx_obj or {})
    if ev is None:
        return True, "state_gate_not_applicable_missing_anomaly_event"
    tool_nm = str(body.get("tool_name") or "")
    ns_gate, dep_gate = resolve_namespace_deployment_for_state_gate(mutate_args, tool_nm, ev)
    if not ns_gate or not dep_gate:
        return True, "state_gate_not_applicable_missing_namespace_or_deployment"
    healthy, dep_detail = await check_deployment_rollout_healthy(ns_gate, dep_gate)
    await emit_transition(
        ctx,
        trace_id=trace,
        transition=TRANSITION_POST_VERIFY_STATE_OK if healthy else TRANSITION_POST_VERIFY_STATE_FAIL,
        component="autonomous_feedback_loop",
        detail="state_machine_gate_before_terminal_success",
        meta={
            "namespace": ns_gate,
            "deployment": dep_gate,
            "healthy": healthy,
            "detail": dep_detail[:1500],
        },
    )
    return healthy, dep_detail


async def _finalize_feedback_success_verified(
    ctx: WorkerHandlerContext,
    *,
    trace: str,
    body: dict[str, Any],
    mutate_args: dict[str, Any],
    stdout: str,
    sdk_verify_summary: str,
    ctx_obj: dict[str, Any] | None = None,
) -> bool:
    ok_sm, sm_detail = await _verify_state_machine_gate(
        ctx,
        trace=trace,
        body=body,
        mutate_args=mutate_args,
        ctx_obj=ctx_obj,
    )
    if not ok_sm:
        await emit_telegram_escalation(
            ctx,
            trace,
            f"STATE_MACHINE_GATE_FAIL detail={sm_detail[:1500]}",
            reason="STATE_MACHINE_GATE_FAIL",
        )
        await ctx.redis.delete(_STATE_KEY.format(trace=trace))
        await emit_terminal_tombstone(
            ctx,
            trace_id=trace,
            reason_code="STATE_MACHINE_GATE_FAIL",
            component="autonomous_feedback_loop",
            detail=sm_detail[:1200],
        )
        return False
    await _archive_postmortem(trace, str(body.get("tool_name") or ""), mutate_args, ctx_obj)
    _tool_name = str(body.get("tool_name") or "")
    await _upsert_action_experience_on_success(
        ctx,
        trace=trace,
        tool_name=_tool_name,
        mutate_args=mutate_args,
        stdout=stdout,
        sdk_verify_summary=sdk_verify_summary,
        ctx_obj=ctx_obj,
    )

    # Load ctx text once — reused by both SOP promotion and temporal pattern blocks below.
    _ctx_text = await _load_autonomous_ctx_text(ctx.redis, trace) or ""
    _co = ctx_obj if isinstance(ctx_obj, dict) else {}

    # S2.2: evaluate this pattern for SOP promotion (best-effort — never blocks success path).
    try:
        from services.learning_promoter.promoter import evaluate_for_promotion
        from execution.memory_normalize import extract_workload_fingerprint
        _pattern_key = extract_workload_fingerprint(_ctx_text or stdout) or ""
        if _pattern_key:
            await evaluate_for_promotion(
                ctx,
                pattern_key=_pattern_key,
                trace_id=trace,
                tool_name=_tool_name,
                match_text=_ctx_text[:2000],
                args_playbook=strip_ephemeral_from_args(mutate_args),
            )
    except Exception as _promo_err:
        if isinstance(_promo_err, ImportError):
            logger.warning("event=sop_promo_import_error trace=%s err=%s", trace, _promo_err)
        else:
            logger.debug("event=sop_promo_skip trace=%s err=%s", trace, _promo_err)

    await _write_success_hot_cache(ctx, trace, stdout)
    await ctx.redis.delete(_STATE_KEY.format(trace=trace))
    await emit_transition(
        ctx,
        trace_id=trace,
        transition=TRANSITION_STATE_MACHINE_VERIFIED,
        component="autonomous_feedback_loop",
        detail="action_feedback_success_state_machine_verified",
    )
    await mark_trace_orchestrator_resolved_verified(ctx.redis, trace)

    # S3.3: record A/B success outcome (best-effort).
    try:
        from pkg.prompt_optimizer.ab_test import record_outcome, evaluate_winner
        _raw_variant = await ctx.redis.get(f"omni:prompt:ab:trace:{trace}")
        _variant = (_raw_variant.decode() if isinstance(_raw_variant, bytes) else str(_raw_variant or "")).strip()
        if _variant in ("A", "B"):
            await record_outcome(ctx.redis, _variant, json_ok=True, steps=0, success=True)
            await evaluate_winner(ctx.redis)
    except Exception as _ab_err:
        logger.debug("event=ab_record_skip trace=%s err=%s", trace, _ab_err)

    # S3.4: record incident timestamp for temporal pattern analysis (best-effort).
    try:
        from pkg.temporal.pattern_matcher import record_incident_timestamp, maybe_schedule_prediction
        from execution.memory_normalize import extract_workload_fingerprint as _ewf
        ws = getattr(ctx, "settings", None)
        _p_key = _co.get("workload_fingerprint") or (_ewf(_ctx_text) if _ctx_text else "")
        if _p_key:
            await record_incident_timestamp(ctx.redis, pattern_key=_p_key)
            _kafka_topic = getattr(ws, "kafka_topic_proactive_incidents", "omni-proactive-incidents")
            await maybe_schedule_prediction(ctx.redis, pattern_key=_p_key, kafka_topic=_kafka_topic)
    except Exception as _tp_err:
        logger.debug("event=temporal_record_skip trace=%s err=%s", trace, _tp_err)

    return True


async def _finalize_feedback_success_legacy(
    ctx: WorkerHandlerContext,
    *,
    trace: str,
    body: dict[str, Any],
    mutate_args: dict[str, Any],
    stdout: str,
    ctx_obj: dict[str, Any] | None = None,
) -> bool:
    ok_sm, sm_detail = await _verify_state_machine_gate(
        ctx,
        trace=trace,
        body=body,
        mutate_args=mutate_args,
        ctx_obj=ctx_obj,
    )
    if not ok_sm:
        await emit_telegram_escalation(
            ctx,
            trace,
            f"STATE_MACHINE_GATE_FAIL detail={sm_detail[:1500]}",
            reason="STATE_MACHINE_GATE_FAIL",
        )
        await ctx.redis.delete(_STATE_KEY.format(trace=trace))
        await emit_terminal_tombstone(
            ctx,
            trace_id=trace,
            reason_code="STATE_MACHINE_GATE_FAIL",
            component="autonomous_feedback_loop",
            detail=sm_detail[:1200],
        )
        return False
    await _archive_postmortem(trace, str(body.get("tool_name") or ""), mutate_args, ctx_obj)
    if not bool(getattr(ctx.settings, "omni_experience_requires_sdk_verify", True)):
        await _upsert_action_experience_on_success(
            ctx,
            trace=trace,
            tool_name=str(body.get("tool_name") or ""),
            mutate_args=mutate_args,
            stdout=stdout,
            ctx_obj=ctx_obj,
        )
    await _write_success_hot_cache(ctx, trace, stdout)
    await ctx.redis.delete(_STATE_KEY.format(trace=trace))
    await emit_transition(
        ctx,
        trace_id=trace,
        transition=TRANSITION_STATE_MACHINE_VERIFIED,
        component="autonomous_feedback_loop",
        detail="action_feedback_success_legacy_state_machine_verified",
    )
    return True


async def _llm_post_verify_state_react(
    ctx: WorkerHandlerContext,
    *,
    trace: str,
    namespace: str,
    deployment: str,
    verify_summary: str,
    dep_detail: str,
    stdout: str,
    last_attempt: int,
) -> bool:
    """
    Multi-round ReAct + CoT with short-term memory (see run_post_verify_react_loop).
    Emits EXECUTE_MUTATE when the loop returns a mutate plan. Returns True if emitted.
    """
    if not bool(getattr(ctx.settings, "omni_post_verify_state_llm_enabled", True)):
        return False
    ws = ctx.settings
    max_attempts = int(getattr(ws, "autonomous_execute_max_attempts", 3) or 3)
    next_attempt = last_attempt + 1
    if next_attempt > max_attempts:
        logger.warning("[%s] post_verify_react skip: max_attempts", trace)
        return False
    plan = await run_post_verify_react_loop(
        ctx,
        trace=trace,
        namespace=namespace,
        deployment=deployment,
        verify_summary=verify_summary,
        dep_detail=dep_detail,
        executor_stdout=stdout,
    )
    if not plan:
        return False
    tn = str(plan.get("tool_name") or "").strip()
    args = plan.get("args") if isinstance(plan.get("args"), dict) else {}
    if not tn:
        return False
    rc = plan.get("reasoning_chain") if isinstance(plan.get("reasoning_chain"), dict) else None
    st_cur = await _load_state(ctx.redis, trace)
    emitted_pv = await emit_execute_mutate(
        ctx,
        trace=trace,
        tool_name=tn,
        args=dict(args),
        attempt_count=next_attempt,
        reasoning_chain=rc if isinstance(rc, dict) else None,
    )
    if not emitted_pv:
        logger.warning(
            "[%s] event=post_verify_react_emit_mutate_failed attempt=%s tool=%s",
            trace,
            next_attempt,
            tn,
        )
        await emit_telegram_escalation(
            ctx,
            trace,
            f"post_verify_react MUTATE_ENQUEUE_FAILED tool={tn} attempt={next_attempt}",
            reason="MUTATE_ENQUEUE_FAILED",
        )
        return False
    await ctx.redis.setex(
        _STATE_KEY.format(trace=trace),
        7200,
        json.dumps(
            {
                "last_attempt_count": next_attempt,
                "feedback_failures": int(st_cur.get("feedback_failures") or 0),
                "sdk_verify_round": int(st_cur.get("sdk_verify_round") or 0),
                "state_verify_attempt": int(st_cur.get("state_verify_attempt") or 0),
            },
            ensure_ascii=False,
        ),
    )
    logger.info("[%s] event=post_verify_react_emit_mutate attempt=%s tool=%s", trace, next_attempt, tn)
    return True


async def _llm_replan_after_feedback(
    ctx: WorkerHandlerContext,
    trace: str,
    stdout: str,
    stderr: str,
    exit_code: int,
) -> dict[str, Any] | None:
    ctx_blob = await ctx.redis.get(f"omni:autonomous:ctx:{trace}")
    snippet = ""
    if ctx_blob:
        try:
            s = ctx_blob.decode() if isinstance(ctx_blob, bytes) else ctx_blob
            snippet = str(json.loads(s).get("sanitized_text") or "")[:4000]
        except Exception:
            snippet = ""
    ws = ctx.settings
    llm = ctx.llm
    model = getattr(ws, "diag_evidence_llm_model", None) or getattr(ws, "chat_model", "qwen3.6")
    user = (
        f"Previous mutate failed. exit_code={exit_code}\n"
        f"stdout:\n{stdout[:3000]}\nstderr:\n{stderr[:1500]}\n\n"
        f"Evidence context:\n{snippet[:3000]}\n\n"
        'Reply one JSON: {"tool_name":"k8s_rollout_restart","args":{"namespace":"...","deployment":"..."}} '
        "or {\"tool_name\":\"\",\"args\":{}} if cannot proceed."
    )
    messages = [
        {"role": "system", "content": "JSON only. English."},
        {"role": "user", "content": user},
    ]
    try:
        resp = await llm.chat(model=str(model), messages=messages, stream=False)
        msg = (resp or {}).get("message") or {}
        content = str(msg.get("content") or "")
        parsed = _parse_tool_json(content)

        # S1.4: store raw LLM output for CRAT compliance audit (best-effort, non-fatal).
        if content:
            _llm_hash = hashlib.sha256(content.encode()).hexdigest()
            _llm_ref = f"omni:crat:llm_reason:{trace}:replan"
            try:
                await ctx.redis.setex(_llm_ref, 86400, content)
            except Exception as _he:
                logger.debug("event=llm_reason_store_fail trace=%s err=%s", trace, _he)
            logger.info(
                "event=llm_replan_hash trace=%s hash=%s ref=%s", trace, _llm_hash, _llm_ref
            )

        log_llm_trace(
            ws,
            trace=trace,
            phase="feedback_llm_replan",
            model=str(model),
            raw_response=content,
            parse_ok=parsed is not None,
            parse_hint=agentic_parse_failure_hint(content) if not parsed else "ok",
            parsed_tool=str(parsed.get("tool_name") or "").strip() or None if parsed else None,
        )
        if not parsed:
            return None
        tn = str(parsed.get("tool_name") or "").strip()
        args = parsed.get("args") if isinstance(parsed.get("args"), dict) else {}
        if not tn:
            return {"tool_name": "no_op", "args": {}}
        if tn == "k8s_rollout_restart" and str((args or {}).get("namespace") or "").strip() and str(
            (args or {}).get("deployment") or ""
        ).strip():
            return {"tool_name": tn, "args": dict(args)}
    except Exception as e:
        logger.warning("replan llm: %s", e)
    return None


async def _finalize_if_deployment_rollout_healthy(
    ctx: WorkerHandlerContext,
    trace: str,
    *,
    body: dict[str, Any],
    mutate_args: dict[str, Any],
    ctx_obj: dict[str, Any] | None,
    verify_summary: str,
    stdout: str,
    ev: AnomalyEvent,
    reason_tag: str,
) -> bool:
    """
    If the incident Deployment has sufficient ready replicas, treat feedback as verified success
    without Telegram — even when SDK probes or planner disagree (stale pod in PromQL, loop-guard).
    """
    ws = ctx.settings
    if not bool(getattr(ws, "omni_post_verify_deployment_state_enabled", True)):
        return False
    if not bool(getattr(ws, "omni_telegram_suppress_when_deployment_healthy", True)):
        return False
    tool_nm = str(body.get("tool_name") or "")
    ns_gate, dep_gate = resolve_namespace_deployment_for_state_gate(mutate_args, tool_nm, ev)
    if not ns_gate or not dep_gate:
        return False
    healthy, dep_detail = await check_deployment_rollout_healthy(ns_gate, dep_gate)
    if not healthy:
        return False
    logger.info(
        "[%s] event=telegram_escalation_suppressed reason=%s ns=%s dep=%s detail=%s",
        trace,
        reason_tag,
        ns_gate,
        dep_gate,
        dep_detail[:400],
    )
    merged_out = (
        stdout
        + f"\n---suppressed_escalation:{reason_tag}---\n"
        + f"deployment_rollout_ok ns={ns_gate} dep={dep_gate} {dep_detail}\n"
        + verify_summary
    )[:12000]
    await _finalize_feedback_success_verified(
        ctx,
        trace=trace,
        body=body,
        mutate_args=mutate_args,
        stdout=merged_out,
        sdk_verify_summary=verify_summary,
        ctx_obj=ctx_obj,
    )
    return True


def _anomaly_event_from_redis_ctx(trace: str, ctx_obj: dict[str, Any] | None) -> AnomalyEvent | None:
    if not ctx_obj:
        return None
    ev_min = ctx_obj.get("anomaly_event_min")
    if not isinstance(ev_min, dict):
        return None
    try:
        ev_d = dict(ev_min)
        ev_d["trace_id"] = trace
        return AnomalyEvent.model_validate(ev_d)
    except Exception:
        return None


async def _finalize_if_deployment_rollout_healthy_from_stored_ctx(
    ctx: WorkerHandlerContext,
    trace: str,
    *,
    body: dict[str, Any],
    mutate_args: dict[str, Any],
    verify_summary: str,
    stdout: str,
    reason_tag: str,
) -> bool:
    raw = await ctx.redis.get(f"omni:autonomous:ctx:{trace}")
    ctx_obj: dict[str, Any] = {}
    if raw:
        try:
            ctx_obj = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        except Exception:
            ctx_obj = {}
    ev = _anomaly_event_from_redis_ctx(trace, ctx_obj)
    if ev is None:
        return False
    return await _finalize_if_deployment_rollout_healthy(
        ctx,
        trace,
        body=body,
        mutate_args=mutate_args,
        ctx_obj=ctx_obj or None,
        verify_summary=verify_summary,
        stdout=stdout,
        ev=ev,
        reason_tag=reason_tag,
    )


def _initial_symptom_from_ctx(ctx_obj: dict[str, Any]) -> InitialSymptom | None:
    raw = ctx_obj.get("initial_symptom")
    if isinstance(raw, dict):
        try:
            return InitialSymptom.model_validate(raw)
        except Exception:
            return None
    return None


async def handle_action_feedback_envelope(ctx: WorkerHandlerContext, fields: dict[str, str]) -> None:
    raw = fields.get("data") or "{}"
    try:
        body = json.loads(raw)
    except Exception:
        logger.warning("action_feedback parse fail")
        return
    trace = str(body.get("trace_id") or fields.get("trace_id") or "").strip()
    if not trace:
        return
    await mark_stage(
        ctx.redis, trace, "FEEDBACK",
        "ok" if int(body.get("exit_code", 0)) == 0 else "fail",
        detail=f"status={body.get('status') or ''}",
    )
    exit_code = int(body.get("exit_code", 0))
    stdout = str(body.get("stdout") or "")
    stderr = str(body.get("stderr") or "")
    skipped = str(body.get("skipped_reason") or "").strip()
    mutate_args = body.get("mutate_args") if isinstance(body.get("mutate_args"), dict) else {}
    status_lc = str(body.get("status") or "").strip().lower()

    # F18-followup: an auto-rolled-back action is TERMINAL. Re-evaluating it would let
    # the analyst re-plan and re-publish EXECUTE_MUTATE on the same trace, looping
    # snapshot→settle→rollback forever. A rollback means the attempted fix made things
    # worse, so the trace must stop and a human takes over. Also short-circuit if the
    # trace was already tombstoned (idempotent against late/duplicate feedback).
    _already_terminal = False
    try:
        _already_terminal = bool(await ctx.redis.get(f"omni:autonomous:terminal:{trace}"))
    except Exception:
        _already_terminal = False
    if status_lc == "rolled_back" or _already_terminal:
        if not _already_terminal:
            await emit_terminal_tombstone(
                ctx,
                trace_id=trace,
                reason_code="auto_rollback_terminal",
                component="autonomous_feedback_loop",
                detail="action auto-rolled-back — trace terminal, no re-evaluation (F18-followup)",
                meta={"status": status_lc, "exit_code": exit_code},
            )
        logger.info(
            "[%s] event=feedback_terminal_skip status=%s already_terminal=%s — no re-evaluation",
            trace, status_lc, _already_terminal,
        )
        return
    await emit_transition(
        ctx,
        trace_id=trace,
        transition=TRANSITION_COMMAND_FEEDBACK_INGESTED,
        component="autonomous_feedback_loop",
        detail="action_feedback_received",
        meta={
            "status": str(body.get("status") or ""),
            "exit_code": exit_code,
            "step_id": str(mutate_args.get("step_id") or ""),
            "command_hash": str(mutate_args.get("command_hash") or ""),
            "host_identity": str(mutate_args.get("host_identity") or ""),
        },
    )
    await emit_transition(
        ctx,
        trace_id=trace,
        transition=TRANSITION_RE_EVALUATED,
        component="autonomous_feedback_loop",
        detail="feedback_ingested_re_evaluate_started",
    )

    st = await _load_state(ctx.redis, trace)
    last_attempt = int(st.get("last_attempt_count") or 0)
    feedback_failures = int(st.get("feedback_failures") or 0)

    ws = ctx.settings
    max_attempts = int(getattr(ws, "autonomous_execute_max_attempts", 3) or 3)
    max_verify = int(getattr(ws, "autonomous_verify_max_rounds", 3) or 3)

    if exit_code == 0 and not skipped:
        raw_ctx = await ctx.redis.get(f"omni:autonomous:ctx:{trace}")
        ctx_obj: dict[str, Any] = {}
        if raw_ctx:
            try:
                ctx_obj = json.loads(raw_ctx.decode() if isinstance(raw_ctx, bytes) else raw_ctx)
            except Exception:
                ctx_obj = {}

        tool_nm0 = str(body.get("tool_name") or "")
        if (
            tool_nm0 == "k8s_patch_secret"
            and bool(getattr(ws, "lab_chaos_credential_autofix_enabled", False))
        ):
            rr_dep = ctx_obj.get("rollout_ns_dep")
            if isinstance(rr_dep, dict):
                ns_rr = str(rr_dep.get("namespace") or "").strip()
                de_rr = str(rr_dep.get("deployment") or "").strip()
                if ns_rr and de_rr:
                    next_a = last_attempt + 1
                    if next_a <= max_attempts:
                        rc_r = {
                            "verdict": "CHAOS_LAB_POST_SECRET_ROLLOUT",
                            "lane": "state",
                            "thought_process": [f"post_chaos_secret_patch_rollout:{ns_rr}/{de_rr}"],
                        }
                        emitted_cs = await emit_execute_mutate(
                            ctx,
                            trace=trace,
                            tool_name="k8s_rollout_restart",
                            args={
                                "namespace": ns_rr,
                                "deployment": de_rr,
                                "reasoning": (
                                    "Restart workload after chaos_credential secret patch so pods "
                                    "reload env from the updated Secret."
                                ),
                            },
                            attempt_count=next_a,
                            reasoning_chain=rc_r,
                        )
                        if not emitted_cs:
                            await emit_telegram_escalation(
                                ctx,
                                trace,
                                f"chaos_lab_post_secret_rollout MUTATE_ENQUEUE_FAILED ns={ns_rr}",
                                reason="MUTATE_ENQUEUE_FAILED",
                            )
                            return
                        await emit_transition(
                            ctx,
                            trace_id=trace,
                            transition=TRANSITION_PLAN_EMITTED,
                            component="autonomous_feedback_loop",
                            detail="chaos_lab_post_secret_rollout_emit",
                        )
                        logger.info(
                            "[%s] event=chaos_lab_post_secret_rollout ns=%s dep=%s attempt=%s",
                            trace,
                            ns_rr,
                            de_rr,
                            next_a,
                        )
                        return

        # Lab: second feedback after chaos secret fix — rollout_restart; skip slow post_mutate planner if Deployment is healthy.
        if (
            tool_nm0 == "k8s_rollout_restart"
            and bool(getattr(ws, "lab_chaos_credential_autofix_enabled", False))
            and exit_code == 0
        ):
            rs_fb = str(mutate_args.get("reasoning") or "").lower()
            if "chaos_credential" in rs_fb:
                ns_r = str(mutate_args.get("namespace") or "").strip()
                de_r = str(mutate_args.get("deployment") or "").strip()
                if ns_r and de_r:
                    healthy_r = False
                    dep_det_r = ""
                    for _attempt in range(24):
                        healthy_r, dep_det_r = await check_deployment_rollout_healthy(ns_r, de_r)
                        if healthy_r:
                            break
                        await asyncio.sleep(2.0)
                    if healthy_r:
                        merged_fb = (
                            stdout + "\n---chaos_lab_rollout_verify---\n" + dep_det_r
                        )[:12000]
                        await _finalize_feedback_success_verified(
                            ctx,
                            trace=trace,
                            body=body,
                            mutate_args=mutate_args,
                            stdout=merged_fb,
                            sdk_verify_summary="chaos_lab_rollout_restart_deployment_healthy",
                            ctx_obj=ctx_obj,
                        )
                        logger.info(
                            "[%s] event=chaos_lab_rollout_finalize_verified ns=%s dep=%s",
                            trace,
                            ns_r,
                            de_r,
                        )
                        return

        do_verify = bool(getattr(ws, "omni_post_mutate_sdk_verify_enabled", True))
        raw_verify = ctx_obj.get("omni_verify_required")
        if raw_verify is False:
            do_verify = False
        probe_ids = [str(x) for x in (ctx_obj.get("verify_probe_ids") or []) if str(x).strip()]
        if not probe_ids:
            probe_ids = probe_ids_for_alertname(str(ctx_obj.get("alertname") or ""))
        ev_min = ctx_obj.get("anomaly_event_min")
        if not isinstance(ev_min, dict):
            ev_min = None
        sym_hint = str(ctx_obj.get("symptom_group") or "")

        if not do_verify or not probe_ids or ev_min is None:
            await _finalize_feedback_success_legacy(
                ctx,
                trace=trace,
                body=body,
                mutate_args=mutate_args,
                stdout=stdout,
                ctx_obj=ctx_obj,
            )
            return

        ev_d = dict(ev_min)
        ev_d["trace_id"] = trace
        ev = AnomalyEvent.model_validate(ev_d)

        pmsv = bool(getattr(ws, "omni_post_mutate_verify_planner_enabled", True))
        sdk_fail_after_pmsv = False
        all_ok = False
        verify_summary = ""
        raws = []

        if pmsv:
            st_sv = await _load_state(ctx.redis, trace)
            sv_att = int(st_sv.get("state_verify_attempt") or 0) + 1
            max_sv = int(getattr(ws, "omni_state_verify_max_attempts", 2) or 2)
            if sv_att > max_sv:
                if await _finalize_if_deployment_rollout_healthy(
                    ctx,
                    trace,
                    body=body,
                    mutate_args=mutate_args,
                    ctx_obj=ctx_obj,
                    verify_summary="",
                    stdout=stdout,
                    ev=ev,
                    reason_tag="STATE_VERIFY_MAX_ATTEMPTS",
                ):
                    return
                await emit_telegram_escalation(
                    ctx,
                    trace,
                    f"STATE_VERIFY_MAX_ATTEMPTS sv_att={sv_att} max={max_sv}",
                    reason="STATE_VERIFY_MAX_ATTEMPTS",
                )
                await ctx.redis.delete(_STATE_KEY.format(trace=trace))
                await emit_terminal_tombstone(
                    ctx,
                    trace_id=trace,
                    reason_code="STATE_VERIFY_MAX_ATTEMPTS",
                    component="autonomous_feedback_loop",
                    detail=f"sv_att={sv_att}",
                )
                return

            await ctx.redis.setex(
                _STATE_KEY.format(trace=trace),
                7200,
                json.dumps(
                    {
                        "last_attempt_count": last_attempt,
                        "feedback_failures": feedback_failures,
                        "sdk_verify_round": int(st_sv.get("sdk_verify_round") or 0),
                        "state_verify_attempt": sv_att,
                    },
                    ensure_ascii=False,
                ),
            )

            delay_vm = float(getattr(ws, "omni_verify_delay_sec", 10) or 10)
            if delay_vm > 0:
                await asyncio.sleep(delay_vm)

            all_ok, verify_summary, raws = await run_verify_probes(
                ctx, trace=trace, probe_ids=probe_ids, ev=ev
            )
            batch_pm = probe_raws_to_batch_for_deterministic(
                trace, raws, symptom_group=sym_hint
            )
            sanitized_pm = format_batch_sanitized_analyst_user_text(batch_pm)
            init_sym = _initial_symptom_from_ctx(ctx_obj)
            await append_post_mutate_verify_record(
                ctx.redis,
                trace,
                verify_summary=verify_summary,
                initial_symptoms=str(ctx_obj.get("sanitized_text") or "")[:4000],
                initial_symptom=init_sym,
            )
            logger.info(
                "[%s] event=action_record_post_mutate_verify kind=post_mutate_verify probes=%s",
                trace,
                probe_ids,
            )

            tool_nm_body = str(body.get("tool_name") or "")
            plan_out = await run_post_mutate_state_verify_planner(
                ctx,
                trace=trace,
                batch=batch_pm,
                sanitized_text=sanitized_pm,
                stdout=stdout,
                tool_name=tool_nm_body,
                verify_summary=verify_summary,
                sdk_all_passed=all_ok,
                initial_symptom=init_sym,
            )

            ph_done = str((plan_out or {}).get("phase") or "").strip().lower() == "done"
            if ph_done and not all_ok:
                logger.warning(
                    "[%s] event=post_mutate_planner_done_disagrees_with_probes — forcing remediation path",
                    trace,
                )
                plan_out = None
            elif ph_done and all_ok:
                merged_out = (stdout + "\n---sdk_verify_ok---\n" + verify_summary)[:12000]
                logger.info(
                    "[%s] event=post_mutate_state_verify_planner_ok probes=%s",
                    trace,
                    probe_ids,
                )
                await _finalize_feedback_success_verified(
                    ctx,
                    trace=trace,
                    body=body,
                    mutate_args=mutate_args,
                    stdout=merged_out,
                    sdk_verify_summary=verify_summary,
                    ctx_obj=ctx_obj,
                )
                return

            tn_plan = str((plan_out or {}).get("tool_name") or "").strip()
            if plan_out and tn_plan:
                next_attempt = last_attempt + 1
                if next_attempt > max_attempts:
                    if await _finalize_if_deployment_rollout_healthy(
                        ctx,
                        trace,
                        body=body,
                        mutate_args=mutate_args,
                        ctx_obj=ctx_obj,
                        verify_summary=verify_summary,
                        stdout=stdout,
                        ev=ev,
                        reason_tag="MAX_MUTATE_ATTEMPTS_POST_STATE_VERIFY",
                    ):
                        return
                    await emit_telegram_escalation(
                        ctx,
                        trace,
                        f"MAX_MUTATE_ATTEMPTS_POST_STATE_VERIFY next_attempt={next_attempt}",
                        reason="MAX_MUTATE_ATTEMPTS",
                    )
                    await ctx.redis.delete(_STATE_KEY.format(trace=trace))
                    await emit_terminal_tombstone(
                        ctx,
                        trace_id=trace,
                        reason_code="MAX_MUTATE_ATTEMPTS",
                        component="autonomous_feedback_loop",
                        detail=f"next_attempt={next_attempt}",
                    )
                    return
                rc_p = plan_out.get("reasoning_chain") if isinstance(plan_out.get("reasoning_chain"), dict) else None
                args_p = plan_out.get("args") if isinstance(plan_out.get("args"), dict) else {}
                emitted_psv = await emit_execute_mutate(
                    ctx,
                    trace=trace,
                    tool_name=tn_plan,
                    args=dict(args_p),
                    attempt_count=next_attempt,
                    reasoning_chain=rc_p,
                )
                if not emitted_psv:
                    await emit_telegram_escalation(
                        ctx,
                        trace,
                        f"post_mutate_state_verify_planner MUTATE_ENQUEUE_FAILED tool={tn_plan}",
                        reason="MUTATE_ENQUEUE_FAILED",
                    )
                    return
                await emit_transition(
                    ctx,
                    trace_id=trace,
                    transition=TRANSITION_PLAN_EMITTED,
                    component="autonomous_feedback_loop",
                    detail=f"post_mutate_state_verify_planner_emit_mutate tool={tn_plan} attempt={next_attempt}",
                )
                logger.info(
                    "[%s] event=post_mutate_state_verify_planner_mutate tool=%s attempt=%s",
                    trace,
                    tn_plan,
                    next_attempt,
                )
                return

            if not all_ok:
                sdk_fail_after_pmsv = True
            else:
                if await _finalize_if_deployment_rollout_healthy(
                    ctx,
                    trace,
                    body=body,
                    mutate_args=mutate_args,
                    ctx_obj=ctx_obj,
                    verify_summary=verify_summary,
                    stdout=stdout,
                    ev=ev,
                    reason_tag="POST_MUTATE_STATE_VERIFY_NO_DONE",
                ):
                    return
                await emit_telegram_escalation(
                    ctx,
                    trace,
                    f"POST_MUTATE_STATE_VERIFY_NO_DONE probes_ok but planner did not conclude recovery",
                    reason="POST_MUTATE_STATE_VERIFY_NO_DONE",
                )
                await ctx.redis.delete(_STATE_KEY.format(trace=trace))
                await emit_terminal_tombstone(
                    ctx,
                    trace_id=trace,
                    reason_code="POST_MUTATE_STATE_VERIFY_NO_DONE",
                    component="autonomous_feedback_loop",
                    detail="planner_no_done_with_passing_probes",
                )
                return

        if not pmsv:
            delay = float(getattr(ws, "omni_sdk_verify_initial_delay_sec", 0) or 0)
            if delay > 0:
                await asyncio.sleep(delay)

            all_ok, verify_summary, raws = await run_verify_probes(
                ctx, trace=trace, probe_ids=probe_ids, ev=ev
            )

        if not pmsv and all_ok:
            merged_out = (stdout + "\n---sdk_verify_ok---\n" + verify_summary)[:12000]
            logger.info(
                "[%s] event=post_mutate_sdk_verify_ok probes=%s",
                trace,
                probe_ids,
            )
            tool_nm = str(body.get("tool_name") or "")
            if bool(getattr(ws, "omni_post_verify_deployment_state_enabled", True)):
                ns_gate, dep_gate = resolve_namespace_deployment_for_state_gate(
                    mutate_args, tool_nm, ev
                )
                if ns_gate and dep_gate:
                    healthy, dep_detail = await check_deployment_rollout_healthy(ns_gate, dep_gate)
                    await emit_transition(
                        ctx,
                        trace_id=trace,
                        transition=(
                            TRANSITION_POST_VERIFY_STATE_OK
                            if healthy
                            else TRANSITION_POST_VERIFY_STATE_FAIL
                        ),
                        component="autonomous_feedback_loop",
                        detail="deployment_rollout_state_after_sdk_probes",
                        meta={
                            "namespace": ns_gate,
                            "deployment": dep_gate,
                            "healthy": healthy,
                            "detail": dep_detail[:1500],
                        },
                    )
                    if not healthy:
                        logger.warning(
                            "[%s] event=post_verify_deployment_unhealthy ns=%s dep=%s detail=%s",
                            trace,
                            ns_gate,
                            dep_gate,
                            dep_detail,
                        )
                        retry_emitted = await _llm_post_verify_state_react(
                            ctx,
                            trace=trace,
                            namespace=ns_gate,
                            deployment=dep_gate,
                            verify_summary=verify_summary,
                            dep_detail=dep_detail,
                            stdout=stdout,
                            last_attempt=last_attempt,
                        )
                        if retry_emitted:
                            await emit_transition(
                                ctx,
                                trace_id=trace,
                                transition=TRANSITION_PLAN_EMITTED,
                                component="autonomous_feedback_loop",
                                detail="post_verify_state_llm_retry_rollout",
                            )
                            return
                        await emit_telegram_escalation(
                            ctx,
                            trace,
                            f"POST_VERIFY_DEPLOYMENT_UNHEALTHY ns={ns_gate} dep={dep_gate} {dep_detail[:1500]}",
                            reason="POST_VERIFY_DEPLOYMENT_UNHEALTHY",
                        )
                        await ctx.redis.delete(_STATE_KEY.format(trace=trace))
                        await emit_terminal_tombstone(
                            ctx,
                            trace_id=trace,
                            reason_code="POST_VERIFY_DEPLOYMENT_UNHEALTHY",
                            component="autonomous_feedback_loop",
                            detail=dep_detail[:1200],
                        )
                        return
            await _finalize_feedback_success_verified(
                ctx,
                trace=trace,
                body=body,
                mutate_args=mutate_args,
                stdout=merged_out,
                sdk_verify_summary=verify_summary,
                ctx_obj=ctx_obj,
            )
            return

        if not ((not pmsv and not all_ok) or sdk_fail_after_pmsv):
            return

        st_ok = await _load_state(ctx.redis, trace)
        last_att = int(st_ok.get("last_attempt_count") or 0)
        sdk_round = int(st_ok.get("sdk_verify_round") or 0) + 1
        max_sdk = int(getattr(ws, "omni_sdk_verify_max_rounds", 3) or 3)
        next_attempt = last_att + 1

        logger.warning(
            "[%s] event=post_mutate_sdk_verify_fail sdk_round=%s/%s last_attempt=%s next_attempt=%s",
            trace,
            sdk_round,
            max_sdk,
            last_att,
            next_attempt,
        )

        if sdk_round > max_sdk:
            if await _finalize_if_deployment_rollout_healthy(
                ctx,
                trace,
                body=body,
                mutate_args=mutate_args,
                ctx_obj=ctx_obj,
                verify_summary=verify_summary,
                stdout=stdout,
                ev=ev,
                reason_tag="SDK_VERIFY_EXHAUSTED",
            ):
                return
            _rollback_ok = await _attempt_auto_rollback(ctx, trace, "SDK_VERIFY_EXHAUSTED")
            await _track_negative_recall(ctx, trace)  # S2.4
            if _rollback_ok:
                await emit_telegram_escalation(
                    ctx,
                    trace,
                    f"SDK_VERIFY_EXHAUSTED verify={verify_summary[:2000]}",
                    reason="SDK_VERIFY_EXHAUSTED",
                )
            await ctx.redis.delete(_STATE_KEY.format(trace=trace))
            await emit_terminal_tombstone(
                ctx,
                trace_id=trace,
                reason_code="SDK_VERIFY_EXHAUSTED",
                component="autonomous_feedback_loop",
                detail=f"sdk_round={sdk_round}",
            )
            return

        if next_attempt > max_attempts:
            if await _finalize_if_deployment_rollout_healthy(
                ctx,
                trace,
                body=body,
                mutate_args=mutate_args,
                ctx_obj=ctx_obj,
                verify_summary=verify_summary,
                stdout=stdout,
                ev=ev,
                reason_tag="MAX_MUTATE_ATTEMPTS_POST_VERIFY",
            ):
                return
            _rollback_ok = await _attempt_auto_rollback(ctx, trace, "MAX_MUTATE_ATTEMPTS")
            await _track_negative_recall(ctx, trace)  # S2.4
            if _rollback_ok:
                await emit_telegram_escalation(
                    ctx,
                    trace,
                    f"MAX_MUTATE_ATTEMPTS_POST_VERIFY last_attempt={last_att} verify={verify_summary[:1500]}",
                    reason="MAX_MUTATE_ATTEMPTS",
                )
            await ctx.redis.delete(_STATE_KEY.format(trace=trace))
            await emit_terminal_tombstone(
                ctx,
                trace_id=trace,
                reason_code="MAX_MUTATE_ATTEMPTS",
                component="autonomous_feedback_loop",
                detail=f"next_attempt={next_attempt}",
            )
            return

        batch = probe_raws_to_batch_for_deterministic(
            trace, raws, symptom_group=sym_hint
        )
        sanitized_sdk = format_batch_sanitized_analyst_user_text(batch)
        await store_autonomous_trace_context(
            ctx.redis,
            trace,
            batch=batch,
            sanitized_text=sanitized_sdk,
        )

        await ctx.redis.setex(
            _STATE_KEY.format(trace=trace),
            7200,
            json.dumps(
                {
                    "last_attempt_count": last_att,
                    "feedback_failures": int(st_ok.get("feedback_failures") or 0),
                    "sdk_verify_round": sdk_round,
                    "state_verify_attempt": int(st_ok.get("state_verify_attempt") or 0),
                },
                ensure_ascii=False,
            ),
        )

        llm_first = bool(getattr(ws, "omni_llm_first_autonomy_enabled", False))
        legacy_det_fallback = bool(getattr(ws, "omni_legacy_deterministic_fallback", False))
        full_fb = bool(getattr(ws, "omni_feedback_full_agentic_planner_enabled", False)) or llm_first
        if full_fb:
            hints_sdk = (
                f"[feedback_path=sdk_verify_fail] sdk_round={sdk_round} max_sdk={max_sdk} "
                f"next_attempt={next_attempt}\nFresh probe summary:\n{verify_summary[:3500]}"
            )
            emitted_fb = await _emit_agentic_mutate_if_any(
                ctx,
                trace,
                batch,
                sanitized_text=sanitized_sdk,
                rag_reasoning_hints=hints_sdk,
                attempt_count=next_attempt,
            )
            if emitted_fb:
                await emit_transition(
                    ctx,
                    trace_id=trace,
                    transition=TRANSITION_PLAN_EMITTED,
                    component="autonomous_feedback_loop",
                    detail=(
                        f"sdk_verify_fail_full_agentic_emit attempt={next_attempt} sdk_round={sdk_round}"
                    ),
                )
                logger.info(
                    "event=feedback_full_agentic_sdk_verify_emit trace=%s attempt=%s sdk_round=%s",
                    trace,
                    next_attempt,
                    sdk_round,
                )
                return
            if llm_first and not legacy_det_fallback:
                logger.error("event=ESCALATE_TO_HUMAN trace=%s reason=sdk_verify_no_agentic_plan", trace)
                if await _finalize_if_deployment_rollout_healthy(
                    ctx,
                    trace,
                    body=body,
                    mutate_args=mutate_args,
                    ctx_obj=ctx_obj,
                    verify_summary=verify_summary,
                    stdout=stdout,
                    ev=ev,
                    reason_tag="SDK_VERIFY_NO_AGENTIC_PLAN",
                ):
                    return
                await emit_telegram_escalation(
                    ctx,
                    trace,
                    f"SDK_VERIFY_NO_AGENTIC_PLAN verify={verify_summary[:2000]}",
                    reason="SDK_VERIFY_NO_AGENTIC_PLAN",
                )
                await ctx.redis.delete(_STATE_KEY.format(trace=trace))
                await emit_terminal_tombstone(
                    ctx,
                    trace_id=trace,
                    reason_code="SDK_VERIFY_NO_AGENTIC_PLAN",
                    component="autonomous_feedback_loop",
                    detail="llm_first_no_plan_after_verify_fail",
                )
                return

        if llm_first and not legacy_det_fallback:
            logger.error("event=ESCALATE_TO_HUMAN trace=%s reason=sdk_verify_no_agentic_plan", trace)
            if await _finalize_if_deployment_rollout_healthy(
                ctx,
                trace,
                body=body,
                mutate_args=mutate_args,
                ctx_obj=ctx_obj,
                verify_summary=verify_summary,
                stdout=stdout,
                ev=ev,
                reason_tag="SDK_VERIFY_NO_AGENTIC_PLAN",
            ):
                return
            await emit_telegram_escalation(
                ctx,
                trace,
                f"SDK_VERIFY_NO_AGENTIC_PLAN verify={verify_summary[:2000]}",
                reason="SDK_VERIFY_NO_AGENTIC_PLAN",
            )
            await ctx.redis.delete(_STATE_KEY.format(trace=trace))
            await emit_terminal_tombstone(
                ctx,
                trace_id=trace,
                reason_code="SDK_VERIFY_NO_AGENTIC_PLAN",
                component="autonomous_feedback_loop",
                detail="llm_first_no_plan_after_verify_fail",
            )
            return

        plan = deterministic_mutate_plan_from_batch(
            batch,
            default_ns=default_remediation_namespace(ctx.settings),
            allowed_tools=probe_driven_mutate_tools_for_settings(ctx.settings),
            ws=ctx.settings,
        )
        if not plan:
            logger.error(
                "event=ESCALATE_TO_HUMAN trace=%s reason=sdk_verify_no_deterministic_plan",
                trace,
            )
            if await _finalize_if_deployment_rollout_healthy(
                ctx,
                trace,
                body=body,
                mutate_args=mutate_args,
                ctx_obj=ctx_obj,
                verify_summary=verify_summary,
                stdout=stdout,
                ev=ev,
                reason_tag="SDK_VERIFY_NO_PLAN",
            ):
                return
            await emit_telegram_escalation(
                ctx,
                trace,
                f"SDK_VERIFY_NO_PLAN verify={verify_summary[:2000]}",
                reason="SDK_VERIFY_NO_PLAN",
            )
            await ctx.redis.delete(_STATE_KEY.format(trace=trace))
            await emit_terminal_tombstone(
                ctx,
                trace_id=trace,
                reason_code="SDK_VERIFY_NO_PLAN",
                component="autonomous_feedback_loop",
                detail="deterministic_mutate_plan_empty",
            )
            return

        rc_fb = plan.get("reasoning_chain") if isinstance(plan.get("reasoning_chain"), dict) else None
        emitted_sdk = await emit_execute_mutate(
            ctx,
            trace=trace,
            tool_name=str(plan["tool_name"]),
            args=dict(plan.get("args") or {}),
            attempt_count=next_attempt,
            reasoning_chain=rc_fb,
        )
        if not emitted_sdk:
            await emit_telegram_escalation(
                ctx,
                trace,
                f"sdk_verify_failed_remediate MUTATE_ENQUEUE_FAILED tool={plan.get('tool_name')}",
                reason="MUTATE_ENQUEUE_FAILED",
            )
            await emit_terminal_tombstone(
                ctx,
                trace_id=trace,
                reason_code="MUTATE_ENQUEUE_FAILED",
                component="autonomous_feedback_loop",
                detail="sdk_verify_failed_remediate_kafka_or_audit",
            )
            return
        await emit_transition(
            ctx,
            trace_id=trace,
            transition=TRANSITION_PLAN_EMITTED,
            component="autonomous_feedback_loop",
            detail=(
                f"sdk_verify_failed_remediate tool={plan.get('tool_name')} "
                f"attempt={next_attempt} sdk_round={sdk_round}"
            ),
        )
        return

    if skipped and "auto_execute" in skipped.lower():
        logger.info("[%s] feedback skipped (auto_execute off) — no replan", trace)
        await emit_transition(
            ctx,
            trace_id=trace,
            transition=TRANSITION_EXECUTED,
            status="skipped",
            component="autonomous_feedback_loop",
            detail="auto_execute_disabled",
        )
        return

    feedback_failures += 1
    if feedback_failures > max_verify:
        logger.error("event=ESCALATE_TO_HUMAN trace=%s reason=max_verify_rounds", trace)
        if await _finalize_if_deployment_rollout_healthy_from_stored_ctx(
            ctx,
            trace,
            body=body,
            mutate_args=mutate_args,
            verify_summary="",
            stdout=stdout,
            reason_tag="MAX_VERIFY_ROUNDS",
        ):
            return
        await emit_telegram_escalation(
            ctx,
            trace,
            f"max_verify_rounds feedback_failures={feedback_failures} stdout={stdout[:1500]}",
            reason="MAX_VERIFY_ROUNDS",
        )
        await ctx.redis.delete(_STATE_KEY.format(trace=trace))
        await emit_terminal_tombstone(
            ctx,
            trace_id=trace,
            reason_code="MAX_VERIFY_ROUNDS",
            component="autonomous_feedback_loop",
            detail=f"feedback_failures={feedback_failures}",
        )
        return

    if last_attempt >= max_attempts:
        logger.error("event=ESCALATE_TO_HUMAN trace=%s reason=max_mutate_attempts", trace)
        if await _finalize_if_deployment_rollout_healthy_from_stored_ctx(
            ctx,
            trace,
            body=body,
            mutate_args=mutate_args,
            verify_summary="",
            stdout=stdout,
            reason_tag="MAX_MUTATE_ATTEMPTS",
        ):
            return
        await emit_telegram_escalation(
            ctx,
            trace,
            f"max_mutate_attempts last_attempt={last_attempt} stdout={stdout[:1500]}",
            reason="MAX_MUTATE_ATTEMPTS",
        )
        await ctx.redis.delete(_STATE_KEY.format(trace=trace))
        await emit_terminal_tombstone(
            ctx,
            trace_id=trace,
            reason_code="MAX_MUTATE_ATTEMPTS",
            component="autonomous_feedback_loop",
            detail=f"last_attempt={last_attempt}",
        )
        return

    next_attempt = last_attempt + 1
    if next_attempt > max_attempts:
        logger.error("event=ESCALATE_TO_HUMAN trace=%s reason=attempt_count_exceeded", trace)
        if await _finalize_if_deployment_rollout_healthy_from_stored_ctx(
            ctx,
            trace,
            body=body,
            mutate_args=mutate_args,
            verify_summary="",
            stdout=stdout,
            reason_tag="ATTEMPT_COUNT_EXCEEDED",
        ):
            return
        await emit_telegram_escalation(
            ctx,
            trace,
            f"attempt_count_exceeded next_attempt={next_attempt}",
            reason="ATTEMPT_COUNT_EXCEEDED",
        )
        await ctx.redis.delete(_STATE_KEY.format(trace=trace))
        await emit_terminal_tombstone(
            ctx,
            trace_id=trace,
            reason_code="ATTEMPT_COUNT_EXCEEDED",
            component="autonomous_feedback_loop",
            detail=f"next_attempt={next_attempt}",
        )
        return

    full_fb_mf = bool(getattr(ws, "omni_feedback_full_agentic_planner_enabled", False))
    if full_fb_mf:
        raw_mf = await ctx.redis.get(f"omni:autonomous:ctx:{trace}")
        ctx_obj_mf: dict[str, Any] = {}
        if raw_mf:
            try:
                ctx_obj_mf = json.loads(raw_mf.decode() if isinstance(raw_mf, bytes) else raw_mf)
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as e:
                logger.warning("event=feedback_ctx_mf_parse_error trace=%s err=%r", trace, e)
                ctx_obj_mf = {}
        probe_ids_mf = [str(x) for x in (ctx_obj_mf.get("verify_probe_ids") or []) if str(x).strip()]
        ev_min_mf = ctx_obj_mf.get("anomaly_event_min")
        sym_mf = str(ctx_obj_mf.get("symptom_group") or "")
        if probe_ids_mf and isinstance(ev_min_mf, dict):
            try:
                ev_mf = AnomalyEvent.model_validate(ev_min_mf)
                _, verify_summary_mf, raws_mf = await run_verify_probes(
                    ctx, trace=trace, probe_ids=probe_ids_mf, ev=ev_mf
                )
                batch_mf = probe_raws_to_batch_for_deterministic(
                    trace, raws_mf, symptom_group=sym_mf
                )
                st_mf_txt = format_batch_sanitized_analyst_user_text(batch_mf)
                await store_autonomous_trace_context(
                    ctx.redis,
                    trace,
                    batch=batch_mf,
                    sanitized_text=st_mf_txt,
                )
                hints_mf = (
                    f"[feedback_path=mutate_fail] exit_code={exit_code} next_attempt={next_attempt}\n"
                    f"stdout:\n{stdout[:2000]}\nstderr:\n{stderr[:1500]}\n"
                    f"verify_reprobe_summary:\n{verify_summary_mf[:2000]}"
                )
                emitted_mf = await _emit_agentic_mutate_if_any(
                    ctx,
                    trace,
                    batch_mf,
                    sanitized_text=st_mf_txt,
                    rag_reasoning_hints=hints_mf,
                    attempt_count=next_attempt,
                )
                if emitted_mf:
                    await emit_transition(
                        ctx,
                        trace_id=trace,
                        transition=TRANSITION_PLAN_EMITTED,
                        component="autonomous_feedback_loop",
                        detail=f"mutate_fail_full_agentic_emit attempt={next_attempt}",
                    )
                    logger.info(
                        "event=feedback_full_agentic_mutate_fail_emit trace=%s attempt=%s",
                        trace,
                        next_attempt,
                    )
                    return
            except Exception as e:
                logger.warning("event=mutate_fail_full_agentic_probe_err trace=%s err=%s", trace, e)

    plan = await _llm_replan_after_feedback(ctx, trace, stdout, stderr, exit_code)
    if plan and plan.get("tool_name") == "no_op":
        logger.info(
            "event=replan_no_op trace=%s detail=llm_signalled_no_further_action",
            trace,
        )
        await ctx.redis.delete(_STATE_KEY.format(trace=trace))
        return
    if not plan:
        logger.error("event=ESCALATE_TO_HUMAN trace=%s reason=replan_empty", trace)
        if await _finalize_if_deployment_rollout_healthy_from_stored_ctx(
            ctx,
            trace,
            body=body,
            mutate_args=mutate_args,
            verify_summary="",
            stdout=stdout,
            reason_tag="REPLAN_EMPTY",
        ):
            return
        await emit_telegram_escalation(
            ctx,
            trace,
            f"replan_empty exit_code={exit_code} stderr={stderr[:2000]}",
            reason="REPLAN_EMPTY",
        )
        await ctx.redis.delete(_STATE_KEY.format(trace=trace))
        await emit_terminal_tombstone(
            ctx,
            trace_id=trace,
            reason_code="REPLAN_EMPTY",
            component="autonomous_feedback_loop",
            detail=f"exit_code={exit_code}",
        )
        return

    rc_fb = plan.get("reasoning_chain") if isinstance(plan.get("reasoning_chain"), dict) else None
    emitted_rp = await emit_execute_mutate(
        ctx,
        trace=trace,
        tool_name=plan["tool_name"],
        args=plan["args"],
        attempt_count=next_attempt,
        reasoning_chain=rc_fb,
    )
    if not emitted_rp:
        await emit_telegram_escalation(
            ctx,
            trace,
            f"replan_emit MUTATE_ENQUEUE_FAILED tool={plan.get('tool_name')}",
            reason="MUTATE_ENQUEUE_FAILED",
        )
        return
    await ctx.redis.setex(
        _STATE_KEY.format(trace=trace),
        7200,
        json.dumps(
            {
                "last_attempt_count": next_attempt,
                "feedback_failures": feedback_failures,
                "sdk_verify_round": int(st.get("sdk_verify_round") or 0),
                "state_verify_attempt": int(st.get("state_verify_attempt") or 0),
            },
            ensure_ascii=False,
        ),
    )
    await emit_transition(
        ctx,
        trace_id=trace,
        transition=TRANSITION_PLAN_EMITTED,
        component="autonomous_feedback_loop",
        detail=f"replan_emit tool={plan['tool_name']} attempt={next_attempt}",
    )


async def kafka_action_feedback_loop(ctx: WorkerHandlerContext, stop: object) -> None:
    ws = ctx.settings
    consumer = AIOKafkaConsumer(
        ws.kafka_topic_action_feedback,
        bootstrap_servers=ws.kafka_bootstrap_servers,
        group_id=ws.consumer_group_analyst_feedback,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        client_id=f"{ws.consumer_name_analyst}-feedback",
    )
    await consumer.start()
    try:
        async for msg in consumer:
            if getattr(stop, "is_set", lambda: False)():
                break
            try:
                fields = decode_kafka_value_to_fields(msg.value, msg.headers)
                trace = ""
                try:
                    raw = fields.get("data") or "{}"
                    trace = str(json.loads(raw).get("trace_id") or "")
                except Exception:
                    trace = kafka_msg_id(msg.topic, msg.partition, msg.offset)
                tok = push_trace_id(trace or "unknown")
                try:
                    ctx.inbound_trace_id = trace or "unknown"
                    await handle_action_feedback_envelope(ctx, fields)
                finally:
                    pop_trace_id(tok)
                await consumer.commit()
            except Exception as e:
                await ctx.ledger.record_exception(
                    e, phase="4", component="kafka_action_feedback_loop", swallow_errors=True
                )
                logger.exception("kafka_action_feedback_loop: %s", e)
    finally:
        await consumer.stop()
