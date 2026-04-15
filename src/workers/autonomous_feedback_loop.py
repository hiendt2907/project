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
from workers.archivist import write_incident_postmortem
from workers.metrics_exporter import inc_experience_saved, inc_learning_upsert
from workers.telegram_escalation import emit_telegram_escalation
from workers.request_trace import pop_trace_id, push_trace_id
from workers.autonomy_contract import (
    TRANSITION_EXECUTED,
    TRANSITION_PLAN_EMITTED,
    TRANSITION_POST_VERIFY_STATE_FAIL,
    TRANSITION_POST_VERIFY_STATE_OK,
    TRANSITION_REQUIRES_HUMAN,
    TRANSITION_VERIFIED_SUCCESS,
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
    except Exception:
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


def _archive_postmortem(
    trace: str,
    tool_name: str,
    mutate_args: dict[str, Any],
    ctx_obj: dict[str, Any] | None,
) -> None:
    """Fire-and-forget: write REDACTED post-mortem to disk. Never raises."""
    co = ctx_obj if isinstance(ctx_obj, dict) else {}
    try:
        write_incident_postmortem(
            trace,
            tool_name=tool_name,
            arg_keys=sorted(str(k) for k in (mutate_args or {}).keys()),
            alertname=str(co.get("alertname") or co.get("drift_type") or "unknown"),
            namespace=str(co.get("namespace") or ""),
            workload=str(co.get("workload") or co.get("deployment") or ""),
        )
    except Exception as e:
        logger.warning("event=archivist_postmortem_error trace=%s err=%s", trace, e)


async def _finalize_feedback_success_verified(
    ctx: WorkerHandlerContext,
    *,
    trace: str,
    body: dict[str, Any],
    mutate_args: dict[str, Any],
    stdout: str,
    sdk_verify_summary: str,
    ctx_obj: dict[str, Any] | None = None,
) -> None:
    _archive_postmortem(trace, str(body.get("tool_name") or ""), mutate_args, ctx_obj)
    await _upsert_action_experience_on_success(
        ctx,
        trace=trace,
        tool_name=str(body.get("tool_name") or ""),
        mutate_args=mutate_args,
        stdout=stdout,
        sdk_verify_summary=sdk_verify_summary,
        ctx_obj=ctx_obj,
    )
    await _write_success_hot_cache(ctx, trace, stdout)
    await ctx.redis.delete(_STATE_KEY.format(trace=trace))
    await emit_transition(
        ctx,
        trace_id=trace,
        transition=TRANSITION_VERIFIED_SUCCESS,
        component="autonomous_feedback_loop",
        detail="action_feedback_success_sdk_verified",
    )


async def _finalize_feedback_success_legacy(
    ctx: WorkerHandlerContext,
    *,
    trace: str,
    body: dict[str, Any],
    mutate_args: dict[str, Any],
    stdout: str,
    ctx_obj: dict[str, Any] | None = None,
) -> None:
    _archive_postmortem(trace, str(body.get("tool_name") or ""), mutate_args, ctx_obj)
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
        transition=TRANSITION_VERIFIED_SUCCESS,
        component="autonomous_feedback_loop",
        detail="action_feedback_success",
    )


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
    await emit_execute_mutate(
        ctx,
        trace=trace,
        tool_name=tn,
        args=dict(args),
        attempt_count=next_attempt,
        reasoning_chain=rc if isinstance(rc, dict) else None,
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
    model = getattr(ws, "diag_evidence_llm_model", None) or getattr(ws, "chat_model", "qwen2.5-coder-3b")
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
        if tn == "k8s_rollout_restart" and str((args or {}).get("namespace") or "").strip() and str(
            (args or {}).get("deployment") or ""
        ).strip():
            return {"tool_name": tn, "args": dict(args)}
    except Exception as e:
        logger.warning("replan llm: %s", e)
    return None


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
    exit_code = int(body.get("exit_code", 0))
    stdout = str(body.get("stdout") or "")
    stderr = str(body.get("stderr") or "")
    skipped = str(body.get("skipped_reason") or "").strip()
    mutate_args = body.get("mutate_args") if isinstance(body.get("mutate_args"), dict) else {}

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
            and "secret_patch_ok" in (stdout or "")
            and "chaos_credential" in (stdout or "").lower()
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
                        await emit_execute_mutate(
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
                await emit_execute_mutate(
                    ctx,
                    trace=trace,
                    tool_name=tn_plan,
                    args=dict(args_p),
                    attempt_count=next_attempt,
                    reasoning_chain=rc_p,
                )
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
        legacy_det_fallback = bool(getattr(ws, "omni_legacy_deterministic_fallback", True))
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
        await emit_execute_mutate(
            ctx,
            trace=trace,
            tool_name=str(plan["tool_name"]),
            args=dict(plan.get("args") or {}),
            attempt_count=next_attempt,
            reasoning_chain=rc_fb,
        )
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
            except Exception:
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
    if not plan:
        logger.error("event=ESCALATE_TO_HUMAN trace=%s reason=replan_empty", trace)
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

    await ctx.redis.setex(
        _STATE_KEY.format(trace=trace),
        7200,
        json.dumps(
            {
                "last_attempt_count": last_attempt,
                "feedback_failures": feedback_failures,
                "sdk_verify_round": int(st.get("sdk_verify_round") or 0),
                "state_verify_attempt": int(st.get("state_verify_attempt") or 0),
            },
            ensure_ascii=False,
        ),
    )
    rc_fb = plan.get("reasoning_chain") if isinstance(plan.get("reasoning_chain"), dict) else None
    await emit_execute_mutate(
        ctx,
        trace=trace,
        tool_name=plan["tool_name"],
        args=plan["args"],
        attempt_count=next_attempt,
        reasoning_chain=rc_fb,
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
