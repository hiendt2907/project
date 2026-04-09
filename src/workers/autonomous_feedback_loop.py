"""Analyst: consume omni-action-feedback — success → hot cache; fail → LLM replan + EXECUTE_MUTATE; escalate on limits."""

from __future__ import annotations

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
from workers.analyst_agentic_loop import _parse_tool_json
from workers.evidence_mutate_emit import emit_execute_mutate
from workers.handler_context import WorkerHandlerContext
from workers.metrics_exporter import inc_experience_saved, inc_learning_upsert
from workers.telegram_escalation import emit_telegram_escalation
from workers.request_trace import pop_trace_id, push_trace_id
from workers.autonomy_contract import (
    TRANSITION_EXECUTED,
    TRANSITION_PLAN_EMITTED,
    TRANSITION_REQUIRES_HUMAN,
    TRANSITION_VERIFIED_SUCCESS,
    emit_terminal_tombstone,
    emit_transition,
)

logger = logging.getLogger(__name__)

_STATE_KEY = "omni:autonomous:state:{trace}"
_HOT_KEY = "omni:autonomous:hot:{trace}"


async def _load_state(redis: Any, trace: str) -> dict[str, Any]:
    raw = await redis.get(_STATE_KEY.format(trace=trace))
    if not raw:
        return {"last_attempt_count": 0, "feedback_failures": 0}
    try:
        s = raw.decode() if isinstance(raw, bytes) else raw
        o = json.loads(s)
        return o if isinstance(o, dict) else {}
    except Exception:
        return {"last_attempt_count": 0, "feedback_failures": 0}


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


def _embedding_from_ollama(resp: dict[str, Any]) -> list[float]:
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
) -> None:
    try:
        ctx_text = await _load_autonomous_ctx_text(ctx.redis, trace)
        symptom_raw = f"{ctx_text}\n\n{stdout[:1200]}".strip()
        symptom_text = canonical_symptom_text(symptom_raw, strip_pods=True)
        emb = await ctx.ollama.embed(
            model=ctx.settings.embed_model,
            input=symptom_text[:4000],
            keep_alive=ctx.settings.ollama_keep_alive,
        )
        vec = _embedding_from_ollama(emb)
        if len(vec) != EMBED_DIM:
            vec = (vec + [0.0] * EMBED_DIM)[:EMBED_DIM]
        args_pb = strip_ephemeral_from_args(mutate_args)
        ah = _args_hash(args_pb)
        pid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"diag-feedback:{trace}:{tool_name}:{ah}"))
        payload = {
            "memory_kind": "playbook",
            "symptom_text": symptom_text[:2000],
            "workload_fingerprint": extract_workload_fingerprint(ctx_text or symptom_text),
            "args_playbook": args_pb,
            "lesson": f"[diagnostic feedback success] tool={tool_name} trace={trace}"[:1200],
            "routing_source": "diagnostic_autonomous_feedback",
            "tool": tool_name,
            "args": mutate_args,
            "args_hash": ah,
            "auto_execute": True,
            "match_text": (ctx_text or symptom_text)[:2000],
            "trace_id": trace,
            "exec_outcome": "success",
            "biz_outcome": "correct",
            "verification_result": "pass",
            "unknown_reason": "",
            "latency_ms": 0,
            "safety_flag": "normal",
            "ts": str(int(time.time())),
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
    ollama = ctx.ollama
    model = getattr(ws, "diag_evidence_llm_model", None) or getattr(ws, "ollama_model", "llama3")
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
        resp = await ollama.chat(model=str(model), messages=messages, stream=False)
        msg = (resp or {}).get("message") or {}
        content = str(msg.get("content") or "")
        parsed = _parse_tool_json(content)
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
        await _upsert_action_experience_on_success(
            ctx,
            trace=trace,
            tool_name=str(body.get("tool_name") or ""),
            mutate_args=mutate_args,
            stdout=stdout,
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
