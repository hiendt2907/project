"""Proactive daemon: Prometheus evaluate → Kafka omni-proactive-incidents → SOP tool → audit topic."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from typing import Any, NamedTuple

from execution.memory_normalize import (
    canonical_symptom_text,
    extract_workload_fingerprint,
    strip_ephemeral_from_args,
)
from observability.normalize import (
    canonical_query_from_rule_name,
    infer_error_hint_from_promql,
    redact,
)
from pkg.reasoning.known_fix_resolver import (
    embedding_from_response as _embedding_from_response,
    resolve_known_fix,
)
from rag.pgvector_store import COLLECTION_ACTION_EXPERIENCE, EMBED_DIM, PointStruct
from workers.handlers import WorkerHandlerContext, resolve_remediation_from_memory
from workers.request_trace import pop_trace_id, push_trace_id
from workers.metrics_exporter import (
    inc_experience_saved,
    inc_anomaly_events,
    inc_learning_governance,
    inc_proactive_events,
    inc_learning_upsert,
    inc_proactive_fallback,
    inc_proactive_verify,
    inc_proactive_event_timeout,
    inc_proactive_freeze,
    inc_proactive_requires_human,
    inc_proactive_tombstone_no_k8s,
    inc_proactive_outcome,
    observe_proactive_incident_duration,
    set_learning_unique_patterns,
)
from workers.sdk_service_tools import _prometheus_get_json
from workers.tools import ToolCallPayload
from workers.proactive_guardrails import (
    PROACTIVE_MUTATE_TOOLS,
    extract_resource_ref,
    proactive_gigo_cluster_identity_ok,
    set_namespace_freeze_fallback,
    set_resource_freeze,
)
from workers.llm_context_budget import truncate_for_llm
from workers.k8s_resource_snapshot import fetch_last_known_state
from workers.proactive_models import DEFAULT_RULE, AnomalyEvent
from workers.proactive_policy_gate import learning_governance_decision as _learning_governance_decision
from workers.proactive_react_runner import run_proactive_react_fallback
from workers.otel_tracing import child_span, proactive_trace_span
from workers.autonomy_contract import (
    TRANSITION_CONTEXT_READY,
    TRANSITION_INGESTED,
    TRANSITION_REQUIRES_HUMAN,
    TRANSITION_VERIFIED_SUCCESS,
    emit_terminal_tombstone,
    emit_transition,
)

logger = logging.getLogger(__name__)



def _negative_pattern_redis_key(pattern_key: str) -> str:
    return f"omni:learning:negative:proactive:{pattern_key}"


async def _is_negative_pattern(ctx: WorkerHandlerContext, pattern_key: str) -> bool:
    if not (pattern_key or "").strip():
        return False
    try:
        v = await ctx.redis.get(_negative_pattern_redis_key(pattern_key))
        return v is not None and str(v).strip() != ""
    except Exception:
        return False


async def _set_negative_pattern(ctx: WorkerHandlerContext, pattern_key: str, reason: str) -> None:
    if not (pattern_key or "").strip():
        return
    ttl = int(getattr(ctx.settings, "proactive_negative_pattern_ttl_sec", 604800) or 604800)
    try:
        await ctx.redis.setex(
            _negative_pattern_redis_key(pattern_key),
            ttl,
            (reason or "negative")[:500],
        )
    except Exception as e:
        logger.debug("negative pattern set skip: %s", e)


def _sanitize_proactive_telegram_body(text: str, max_chars: int = 3500) -> str:
    """Bỏ [DEBUG]/[DETAIL] khỏi tin admin — tránh spam kỹ thuật."""
    lines_out: list[str] = []
    for ln in (text or "").splitlines():
        sl = ln.strip()
        u = sl.upper()
        if u.startswith("[DEBUG]") or u.startswith("[DETAIL]"):
            continue
        lines_out.append(ln)
    body = "\n".join(lines_out).strip()
    if not body:
        body = "[OPERATOR_VIEW] Đã lọc debug — xem stream audit:proactive hoặc logs worker."
    return body[:max_chars]


def _react_mem_key(trace_id: str) -> str:
    return f"omni:proactive:react_mem:{trace_id}"


async def _react_mem_append(ctx: WorkerHandlerContext, trace_id: str, line: str, ttl_sec: int = 3600) -> None:
    try:
        key = _react_mem_key(trace_id)
        cap = int(getattr(ctx.settings, "proactive_react_memory_line_max_chars", 2000) or 2000)
        await ctx.redis.rpush(key, truncate_for_llm(line, cap, tail=True))
        await ctx.redis.expire(key, ttl_sec)
    except Exception:
        pass


async def _react_mem_recent(ctx: WorkerHandlerContext, trace_id: str, limit: int = 6) -> list[str]:
    try:
        key = _react_mem_key(trace_id)
        rows = await ctx.redis.lrange(key, -max(1, limit), -1)
        out: list[str] = []
        for r in rows:
            if isinstance(r, bytes):
                out.append(r.decode("utf-8", errors="replace"))
            else:
                out.append(str(r))
        return out
    except Exception:
        return []


def _stable_args_hash(args: dict[str, Any]) -> str:
    raw = json.dumps(args or {}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _pattern_key_from_event(ev: "AnomalyEvent") -> str:
    base = f"{ev.rule_name}|{ev.canonical_query}|{int(float(ev.threshold or 0.0) * 1000)}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:24]


def _quick_verify_output(text: str, fail_keywords_csv: str) -> bool:
    t = (text or "").lower()
    if "[status] business_hit" in t:
        return True
    if "[status] empty_result" in t or "[status] error" in t:
        return False
    if any(k in t for k in ("thiếu args", "missing arg", "invalid args", "required", "missing required")):
        return False
    if not t.strip():
        return False
    for kw in [k.strip().lower() for k in (fail_keywords_csv or "").split(",") if k.strip()]:
        if kw and kw in t:
            return False
    return True


def _result_status(text: str) -> str:
    t = (text or "").lower()
    if "[status] business_hit" in t:
        return "business_hit"
    if "[status] empty_result" in t:
        return "empty_result"
    if "[status] error" in t:
        return "error"
    return "unknown"


def _allow_learning_upsert(tool: str, output: str, verified: bool) -> bool:
    """
    Learning policy override: PromQL records are persisted only on business hits.
    """
    if not verified:
        return False
    if tool in {"promql_instant", "vm_promql_instant"}:
        return _result_status(output) == "business_hit"
    return True


async def _save_proactive_learning_record(
    ctx: WorkerHandlerContext,
    *,
    trace_id: str,
    pattern_key: str,
    lesson: str,
    tool: str,
    args: dict[str, Any],
    exec_outcome: str,
    biz_outcome: str,
    verification_result: str,
    unknown_reason: str = "",
) -> None:
    try:
        strip_pods = bool(getattr(ctx.settings, "memory_canonical_strip_pods", True))
        symptom_text = canonical_symptom_text(lesson[:4000], strip_pods=strip_pods)
        emb = await ctx.llm.embed(
            model=ctx.settings.embed_model,
            input=symptom_text[:4000],
        )
        vec = _embedding_from_response(emb)
        if len(vec) != EMBED_DIM:
            vec = (vec + [0.0] * EMBED_DIM)[:EMBED_DIM]
        args_pb = strip_ephemeral_from_args(args)
        args_hash = _stable_args_hash(args_pb)
        # Episodic memory: keep each run as a distinct lesson (trace_id), aggregate via pattern_key for governance.
        pid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"proactive:{trace_id}:{tool}:{args_hash}"))
        payload = {
            "memory_kind": "playbook",
            "symptom_text": symptom_text[:2000],
            "workload_fingerprint": extract_workload_fingerprint(lesson),
            "args_playbook": args_pb,
            "lesson": lesson[:1200],
            "routing_source": "proactive_fallback",
            "tool": tool,
            "args": args,
            "args_hash": args_hash,
            "pattern_key": pattern_key,
            "auto_execute": True,
            "match_text": lesson[:2000],
            "trace_id": trace_id,
            "exec_outcome": exec_outcome,
            "biz_outcome": biz_outcome,
            "verification_result": verification_result,
            "unknown_reason": unknown_reason[:200],
            "latency_ms": 0,
            "safety_flag": "normal",
            "ts": str(int(time.time())),
        }
        await ctx.vector_store.upsert(
            collection_name=COLLECTION_ACTION_EXPERIENCE,
            points=[PointStruct(id=pid, vector=vec, payload=payload)],
        )
        inc_experience_saved()
    except Exception as e:
        logger.debug("[%s] proactive learning upsert skip: %s", trace_id, e)


async def _resolve_from_action_experience(
    ctx: WorkerHandlerContext,
    *,
    query_text: str,
    score_threshold: float,
    pattern_key: str = "",
    host_scope: frozenset[str] | None = None,
) -> tuple[bool, str | None, str | None, dict[str, Any]]:
    """Try learned action_experience before LLM fallback.

    Thực thi thẳng thay caller — không phải một gợi ý, một quyết định mutate
    thật. Vì vậy phải qua `resolve_known_fix()` (guard placeholder + phạm vi
    host) thay vì tự thực thi ứng viên top-1 chỉ dựa trên điểm giống nhau —
    xem docstring `pkg.reasoning.known_fix_resolver` cho sự cố production đã
    xảy ra khi thiếu guard này (dispatch `k8s_rollout_restart` với
    `deployment='<valid_deployment>'`).

    `host_scope`: tập tên tài nguyên CÓ THẬT trên mục tiêu hiện tại (ví dụ từ
    discovery snapshot của agent) — None nếu caller chưa có cách liệt kê
    (proactive cluster hiện chưa có cluster-inventory context).
    """
    if await _is_negative_pattern(ctx, pattern_key):
        return False, None, None, {}
    result = await resolve_known_fix(
        ctx,
        query_text=query_text,
        score_threshold=score_threshold,
        host_scope=host_scope,
    )
    return result.ok, result.output, result.tool, result.meta


async def _parse_fallback_tool_call(ctx: WorkerHandlerContext, user_prompt: str) -> tuple[ToolCallPayload | None, float, str]:
    ws = ctx.settings
    conf = 0.0
    rationale = ""
    for attempt in range(ws.proactive_fallback_max_attempts):
        resp = await ctx.llm.chat_structured(
            model=ctx.settings.chat_model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Trả về đúng một JSON object dạng "
                        '{"tool":"<ascii>","args":{...},"confidence":0.0,"reason":"..."} '
                        "không markdown, không ```."
                    ),
                },
                {"role": "user", "content": user_prompt[:7000]},
            ],
            options={"temperature": 0.0, "num_ctx": getattr(getattr(ctx, "settings", None), "proactive_llm_num_ctx", 4096)},
        )
        content = ((resp.get("message") or {}).get("content") or "").strip()
        try:
            data = json.loads(content)
        except Exception:
            continue
        conf = float(data.get("confidence") or 0.0)
        rationale = str(data.get("reason") or "")[:500]
        try:
            call = ToolCallPayload.model_validate({"tool": data.get("tool"), "args": data.get("args") or {}})
            return call, conf, rationale
        except Exception:
            continue
    return None, conf, rationale


async def _update_learning_pattern_stats(
    ctx: WorkerHandlerContext,
    *,
    source: str,
    pattern_key: str,
    outcome: str,
) -> None:
    ws = ctx.settings
    key = f"omni:learning:pattern:{pattern_key}"
    try:
        pipe = ctx.redis.pipeline()
        pipe.hincrby(key, "total", 1)
        if outcome == "success":
            pipe.hincrby(key, "success", 1)
        else:
            pipe.hincrby(key, "fail", 1)
        pipe.expire(key, ws.learning_stats_ttl_sec)
        await pipe.execute()
        set_key = f"omni:learning:unique:set:{source}"
        added = await ctx.redis.sadd(set_key, pattern_key)
        await ctx.redis.expire(set_key, ws.learning_stats_ttl_sec)
        if added:
            approx_unique_all = await ctx.redis.scard(set_key)
            set_learning_unique_patterns(source, "all", float(approx_unique_all))
        set_key_outcome = f"{set_key}:{outcome}"
        added_outcome = await ctx.redis.sadd(set_key_outcome, pattern_key)
        await ctx.redis.expire(set_key_outcome, ws.learning_stats_ttl_sec)
        if added_outcome:
            approx_unique_outcome = await ctx.redis.scard(set_key_outcome)
            set_learning_unique_patterns(source, outcome, float(approx_unique_outcome))
    except Exception as e:
        logger.debug("learning pattern stat update skip: %s", e)


async def proactive_kill_switch_engaged(r: Any, key: str) -> bool:
    """True = bypass toàn bộ proactive (value ``1``). Thiếu key hoặc ``0`` = chạy."""
    try:
        v = await r.get(key)
    except Exception:
        return False
    if v is None:
        return False
    return str(v).strip() == "1"


async def _instant_scalar(ctx: WorkerHandlerContext, promql: str) -> float | None:
    try:
        data = await _prometheus_get_json(ctx, "/api/v1/query", {"query": promql})
    except Exception as e:
        logger.warning("proactive promql fail: %s", e)
        return None
    if data.get("status") != "success":
        return None
    res = (data.get("data") or {}).get("result") or []
    if not res:
        return None
    v = res[0].get("value")
    if v and len(v) >= 2:
        try:
            return float(v[1])
        except (TypeError, ValueError):
            return None
    return None


class ProactiveRule(NamedTuple):
    name: str
    promql: str
    threshold: float


def _load_proactive_rules(ws: Any) -> list[ProactiveRule]:
    """Parse ``ws.proactive_promql_rules`` (JSON array) — cho phép theo dõi NHIỀU rule
    thay vì 1 rule hardcode duy nhất (bug omni-core: proactive gần như không bao giờ
    trigger trong lab vì chỉ theo dõi CrashLoopBackOff). Rỗng/không parse được -> fallback
    fail-closed về đúng 1 rule cũ (``proactive_promql``/``proactive_trigger_threshold``),
    không breaking config hiện có."""
    raw = getattr(ws, "proactive_promql_rules", "") or ""
    rules: list[ProactiveRule] = []
    if raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                for item in parsed:
                    if not isinstance(item, dict):
                        continue
                    promql = str(item.get("promql") or "").strip()
                    if not promql:
                        continue
                    name = str(item.get("name") or promql[:40])
                    threshold = float(item.get("threshold", 0.0))
                    rules.append(ProactiveRule(name=name, promql=promql, threshold=threshold))
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            logger.warning("[PROACTIVE] proactive_promql_rules parse fail, fallback single rule: %s", e)
    if not rules:
        rules.append(
            ProactiveRule(name=DEFAULT_RULE, promql=ws.proactive_promql, threshold=ws.proactive_trigger_threshold)
        )
    return rules


async def _evaluate_one_proactive_rule(ctx: WorkerHandlerContext, rule: ProactiveRule) -> int:
    ws = ctx.settings
    val = await _instant_scalar(ctx, rule.promql)
    if val is None:
        return 0
    if val <= rule.threshold:
        return 0
    dedupe = f"{rule.name}:{rule.promql[:120]}"
    ck = f"omni:proactive:cooldown:{hash(dedupe) & 0xFFFFFFFF:X}"
    if await ctx.redis.get(ck):
        return 0
    trace_id = f"proact-{uuid.uuid4().hex[:12]}"
    hint = infer_error_hint_from_promql(rule.promql)
    cq = canonical_query_from_rule_name(
        rule.name,
        target="cluster",
        error_hint=hint,
        promql_context=rule.promql[:2000],
    )
    cq = redact(cq)
    ev = AnomalyEvent(
        trace_id=trace_id,
        rule_name=rule.name,
        target="cluster",
        namespace="",
        metric_value=val,
        threshold=rule.threshold,
        canonical_query=cq,
        timestamp=str(int(time.time())),
        trigger_promql=rule.promql[:2000],
        error_hint=hint,
    )
    assert ctx.kafka is not None
    await ctx.kafka.send_envelope_inner(ws.kafka_topic_proactive_incidents, ev.model_dump())
    inc_proactive_events()
    inc_anomaly_events()
    await ctx.redis.setex(ck, ws.proactive_cooldown_sec, "1")
    logger.info("[%s] proactive_event_pushed rule=%s metric=%s thr=%s", trace_id, rule.name, val, rule.threshold)
    return 1


async def evaluate_proactive_triggers(ctx: WorkerHandlerContext) -> int:
    """Một tick: với MỖI rule đã cấu hình (mặc định 1 rule hợp lệ, có thể nhiều qua
    ``proactive_promql_rules``) — PromQL instant → nếu vượt ngưỡng + hết cooldown →
    produce ``kafka_topic_proactive_incidents``. Trả tổng số incident enqueue trong tick."""
    ws = ctx.settings
    if await proactive_kill_switch_engaged(ctx.redis, ws.proactive_kill_switch_key):
        logger.info(
            "[PROACTIVE] Bypassed proactively due to kill_switch=1. Skipping Prometheus evaluate; no new incidents enqueued."
        )
        return 0
    fired = 0
    for rule in _load_proactive_rules(ws):
        fired += await _evaluate_one_proactive_rule(ctx, rule)
    return fired


async def _append_dlq_proactive(
    ctx: WorkerHandlerContext,
    *,
    trace_id: str,
    msg_id: str,
    reason: str,
    tombstone: dict[str, Any],
    raw_event: str,
) -> str | None:
    payload = {
        "component": "proactive_guardrails",
        "trace_id": trace_id,
        "msg_id": msg_id,
        "reason": reason,
        "tombstone": tombstone,
    }
    assert ctx.kafka is not None
    await ctx.kafka.send_dict(
        ctx.settings.kafka_topic_dlq,
        {
            "data": json.dumps(payload, ensure_ascii=False),
            "trace_id": trace_id,
            "data_event": raw_event[:8000],
        },
    )
    return f"dlq-{trace_id}"


async def _fail_safe_after_tool_error(
    ctx: WorkerHandlerContext,
    ev: "AnomalyEvent",
    trace: str,
    pattern_key: str,
    call: ToolCallPayload,
    err: BaseException,
    *,
    reason_code: str,
    stream_msg_id: str = "",
) -> None:
    """REQUIRES_HUMAN + DLQ tombstone + optional resource freeze."""
    ws = ctx.settings
    ref = extract_resource_ref(call.tool, call.args)
    k8s_state: dict[str, Any]
    if ref:
        ns, kind, name = ref
        k8s_state = await fetch_last_known_state(
            ns,
            kind,
            name,
            timeout_sec=ws.proactive_k8s_snapshot_timeout_sec,
        )
    else:
        k8s_state = {"unavailable": True, "reason": "no_resource_ref"}
    if k8s_state.get("unavailable"):
        inc_proactive_tombstone_no_k8s()
    freeze_key = ""
    if ws.proactive_resource_freeze_enabled and ref:
        ns, kind, name = ref
        if not k8s_state.get("unavailable"):
            freeze_key = await set_resource_freeze(
                ctx.redis,
                key_prefix=ws.proactive_freeze_key_prefix,
                namespace=ns,
                kind=kind,
                name=name,
                ttl_sec=ws.proactive_resource_freeze_ttl_sec,
                trace_id=trace,
                reason=reason_code,
            )
            inc_proactive_freeze("resource")
        elif ws.proactive_freeze_namespace_fallback_allowed and ns:
            freeze_key = await set_namespace_freeze_fallback(
                ctx.redis,
                key_prefix=ws.proactive_freeze_key_prefix,
                namespace=ns,
                ttl_sec=ws.proactive_resource_freeze_ttl_sec,
                trace_id=trace,
                reason=f"{reason_code}_ns_fallback",
            )
            inc_proactive_freeze("namespace_fallback")
    dlq_id = None
    try:
        dlq_id = await _append_dlq_proactive(
            ctx,
            trace_id=trace,
            msg_id=stream_msg_id or trace,
            reason=reason_code,
            tombstone={
                "tool": call.tool,
                "args_hash": _stable_args_hash(call.args),
                "error": str(err)[:2000],
                "k8s_state": k8s_state,
                "resource_ref": list(ref) if ref else None,
            },
            raw_event=json.dumps(ev.model_dump(), ensure_ascii=False),
        )
    except Exception:
        logger.exception("[%s] dlq append failed", trace)
    inc_proactive_requires_human(reason_code)
    await _append_audit(
        ctx,
        trace_id=trace,
        rule_id=ev.rule_name,
        outcome="REQUIRES_HUMAN_INTERVENTION",
        commands_run=call.tool,
        detail=f"{reason_code}: {type(err).__name__}: {err}"[:2000],
        meta={
            "path": "fallback",
            "pattern_key": pattern_key,
            "tool": call.tool,
            "args_hash": _stable_args_hash(call.args),
            "k8s_state": k8s_state,
            "freeze_key": freeze_key,
            "dlq_msg_id": dlq_id or "",
            "reason_code": reason_code,
        },
    )
    await _save_proactive_learning_record(
        ctx,
        trace_id=trace,
        pattern_key=pattern_key,
        lesson=f"[proactive fail_safe] tool={call.tool} query={ev.canonical_query[:300]}",
        tool=call.tool,
        args=call.args,
        exec_outcome="fail",
        biz_outcome="unknown",
        verification_result="fail_safe",
        unknown_reason=reason_code,
    )
    if ctx.telegram and ws.telegram_admin_chat_id:
        try:
            await ctx.telegram.send_message(
                int(ws.telegram_admin_chat_id),
                f"[REQUIRES_HUMAN] trace={trace} tool={call.tool} reason={reason_code}\n"
                f"freeze_key={freeze_key or 'none'}\n{str(err)[:1500]}",
            )
        except Exception as e:
            logger.warning("[%s] proactive telegram fail_safe: %s", trace, e)
    await emit_terminal_tombstone(
        ctx,
        trace_id=trace,
        reason_code=reason_code,
        component="proactive_observer",
        detail=str(err)[:1200],
        meta={"tool": call.tool, "pattern_key": pattern_key},
    )


async def _append_audit(
    ctx: WorkerHandlerContext,
    *,
    trace_id: str,
    rule_id: str,
    outcome: str,
    commands_run: str = "",
    detail: str = "",
    meta: dict[str, Any] | None = None,
) -> None:
    ws = ctx.settings
    payload = {
        "kind": "proactive",
        "trace_id": trace_id,
        "rule_id": rule_id,
        "outcome": outcome,
        "commands_run": commands_run[:4000],
        "detail": redact(detail)[:4000],
        "ts": str(int(time.time())),
    }
    if meta:
        payload["meta"] = meta
    assert ctx.kafka is not None
    await ctx.kafka.send_dict(ws.kafka_topic_audit_proactive, {"data": json.dumps(payload, ensure_ascii=False)})


async def _proactive_event_pipeline(
    ctx: WorkerHandlerContext,
    ev: AnomalyEvent,
    msg_id: str,
    pattern_key: str,
    raw: str,
) -> None:
    ws = ctx.settings
    trace = ev.trace_id
    _ = raw
    with child_span("proactive_sop_lookup"):
        ok, out, _ = await resolve_remediation_from_memory(
            ctx,
            ev.canonical_query,
            trace=trace,
            collection_name=ws.proactive_sop_collection,
            score_threshold=ws.proactive_sop_score_threshold,
        )
    if ok:
        inc_proactive_verify("success")
        inc_proactive_outcome("sop_success")
        inc_learning_upsert("proactive_sop", "success")
        await _update_learning_pattern_stats(
            ctx, source="proactive_sop", pattern_key=pattern_key, outcome="success"
        )
        await _append_audit(
            ctx,
            trace_id=trace,
            rule_id=ev.rule_name,
            outcome="SUCCESS",
            commands_run="resolve_remediation_from_memory",
            detail=(out or "")[:2000],
            meta={"path": "sop", "pattern_key": pattern_key},
        )
        if ctx.telegram and ws.telegram_admin_chat_id:
            try:
                await ctx.telegram.send_message(
                    int(ws.telegram_admin_chat_id),
                    f"[AUTO-FIX] trace={trace} rule={ev.rule_name} metric={ev.metric_value}\n{(out or '')[:3500]}",
                )
            except Exception as e:
                logger.warning("[%s] proactive telegram notify: %s", trace, e)
    else:
        inc_proactive_fallback("sop_miss")
        await _append_audit(
            ctx,
            trace_id=trace,
            rule_id=ev.rule_name,
            outcome="SOP_MISS",
            detail=f"canonical_query={ev.canonical_query[:500]}",
            meta={"path": "sop_miss", "pattern_key": pattern_key},
        )
        with child_span("proactive_action_experience_lookup"):
            mem_ok, mem_out, mem_tool, mem_meta = await _resolve_from_action_experience(
                ctx,
                query_text=ev.canonical_query,
                score_threshold=ctx.settings.action_experience_score_threshold,
                pattern_key=pattern_key,
            )
        if mem_ok:
            mem_out_s = str(mem_out or "")
            verified_mem = _quick_verify_output(mem_out_s, ws.proactive_verify_keywords_fail)
            learned_score = float(mem_meta.get("score") or 0.0)
            actionable_learning = bool(verified_mem and str(mem_tool or "") in PROACTIVE_MUTATE_TOOLS)
            if _result_status(mem_out_s) == "empty_result":
                learned_score = 0.0
            inc_proactive_fallback("learning_hit")
            inc_proactive_verify("success" if verified_mem else "fail")
            await _append_audit(
                ctx,
                trace_id=trace,
                rule_id=ev.rule_name,
                outcome=(
                    "LEARNING_HIT_OK"
                    if actionable_learning
                    else ("LEARNING_HIT_OBSERVE" if verified_mem else "LEARNING_HIT_VERIFY_FAIL")
                ),
                commands_run=str(mem_tool or ""),
                detail=str(mem_out or "")[:2000],
                meta={
                    "path": "learning_memory",
                    "pattern_key": pattern_key,
                    "tool": mem_tool or "",
                    "args_hash": _stable_args_hash(mem_meta.get("args") or {}),
                    "score": round(learned_score, 4),
                    "result_status": _result_status(mem_out_s),
                },
            )
            if actionable_learning and ctx.telegram and ws.telegram_admin_chat_id:
                try:
                    pfx = "[AUTO-FIX-LEARNING]"
                    safe_body = _sanitize_proactive_telegram_body(mem_out_s)
                    await ctx.telegram.send_message(
                        int(ws.telegram_admin_chat_id),
                        f"{pfx} trace={trace} tool={mem_tool} score={learned_score:.2f}\n{safe_body}",
                    )
                except Exception as e:
                    logger.warning("[%s] proactive telegram learning-hit: %s", trace, e)
            if not verified_mem:
                inc_proactive_outcome("learning_verify_fail")
            allow_upsert = _allow_learning_upsert(str(mem_tool or ""), mem_out_s, verified_mem) and actionable_learning
            if allow_upsert:
                await _save_proactive_learning_record(
                    ctx,
                    trace_id=trace,
                    pattern_key=pattern_key,
                    lesson=f"[proactive learning hit] tool={mem_tool} query={ev.canonical_query[:300]}",
                    tool=str(mem_tool or ""),
                    args=mem_meta.get("args") or {},
                    exec_outcome="success",
                    biz_outcome="correct",
                    verification_result="pass",
                    unknown_reason="",
                )
                inc_learning_upsert("proactive_learning_hit", "success")
            else:
                inc_learning_upsert("proactive_learning_hit", "fail")
            await _update_learning_pattern_stats(
                ctx,
                source="proactive_learning_hit",
                pattern_key=pattern_key,
                outcome="success" if allow_upsert else "fail",
            )
            if allow_upsert:
                inc_proactive_outcome("learning_resolved")
                return
            if verified_mem and not actionable_learning:
                inc_proactive_outcome("learning_observe")
                return
        decision, lb = await _learning_governance_decision(ctx, pattern_key)
        if ws.proactive_fallback_bypass_policy_in_god_mode and (ws.god_mode or ws.lab_unchained):
            decision, lb = "allow", 1.0
        inc_learning_governance(decision)
        if decision == "deny":
            inc_proactive_outcome("governance_deny")
            inc_proactive_verify("denied")
            await _append_audit(
                ctx,
                trace_id=trace,
                rule_id=ev.rule_name,
                outcome="FALLBACK_DENY",
                detail=f"governance deny lb={lb:.3f}",
                meta={"path": "fallback", "pattern_key": pattern_key, "decision": decision, "lb95": round(lb, 4)},
            )
            if ctx.telegram and ws.telegram_admin_chat_id:
                try:
                    await ctx.telegram.send_message(
                        int(ws.telegram_admin_chat_id),
                        f"[PROACTIVE] trace={trace} FALLBACK_DENY lb95={lb:.2f}\nquery={ev.canonical_query[:500]}",
                    )
                except Exception as e:
                    logger.warning("[%s] proactive telegram deny: %s", trace, e)
            return
        # S1 — Sinh bằng chứng KHÔNG còn nằm sau cờ `proactive_fallback_enabled`.
        # Trước đây tắt cờ đó là tắt luôn `run_diagnostic_pipeline`, tức mất cả bằng
        # chứng chứ không chỉ mất vòng ReAct. Muốn engine chủ động thành đường duy
        # nhất thì bằng chứng phải LUÔN được sinh; chỉ vòng ReAct 6 lượt (đắt, tốn
        # LLM) mới là thứ đáng bật/tắt.
        if ws.diagnostic_dictionary_enabled:
            try:
                from workers.diagnostic_dispatcher import run_diagnostic_pipeline

                await run_diagnostic_pipeline(ctx, ev)
            except Exception:
                logger.exception("[%s] diagnostic pipeline failed", trace)
        if ws.proactive_fallback_enabled:
            # S2 — Token LLM chỉ bao ĐÚNG vòng ReAct, không bao cả pipeline.
            # Lý do: `LLMSemaphore` chia làn khi num_parallel>=2 ⇒ làn proactive có
            # đúng 1 token, `acquire_proactive` timeout 120s rồi ném, và exception
            # đó đẩy thẳng message vào DLQ. Nếu giữ token suốt cả pipeline (kể cả
            # giai đoạn chạy probe không cần LLM) thì sau khi gom alert về đây, sự
            # cố thứ hai trở đi sẽ timeout → MẤT CẢNH BÁO THẬT, mà log lại chỉ hiện
            # "proactive handler error". Đây là điều kiện tiên quyết của bước gom.
            _tok = await ctx.semaphore.acquire_proactive()
            try:
                await run_proactive_react_fallback(
                    ctx, ev, trace=trace, pattern_key=pattern_key, msg_id=msg_id
                )
            finally:
                await ctx.semaphore.release(_tok)
            return
        if ctx.telegram and ws.telegram_admin_chat_id:
            try:
                await ctx.telegram.send_message(
                    int(ws.telegram_admin_chat_id),
                    f"[PROACTIVE] trace={trace} SOP miss — cần reasoning hoặc ingest v2.\nquery={ev.canonical_query[:500]}",
                )
            except Exception as e:
                logger.warning("[%s] proactive telegram miss: %s", trace, e)


async def _process_proactive_message(
    ctx: WorkerHandlerContext,
    msg_id: str,
    raw: str,
) -> None:
    ws = ctx.settings
    if await proactive_kill_switch_engaged(ctx.redis, ws.proactive_kill_switch_key):
        await _append_audit(
            ctx,
            trace_id=f"skip-{msg_id}",
            rule_id="kill_switch",
            outcome="SKIPPED_KILL_SWITCH",
            detail="omni:proactive:kill_switch=1",
        )
        return
    try:
        ev = AnomalyEvent.model_validate(json.loads(raw))
    except Exception as e:
        logger.warning("proactive bad payload msg_id=%s err=%s", msg_id, e)
        await _append_audit(
            ctx,
            trace_id=f"bad-{msg_id}",
            rule_id="parse",
            outcome="FAIL",
            detail=str(e),
        )
        return

    trace = ev.trace_id
    await emit_transition(
        ctx,
        trace_id=trace,
        transition=TRANSITION_INGESTED,
        component="proactive_observer",
        detail=f"msg_id={msg_id}",
    )
    if ws.proactive_gigo_require_cluster_identity:
        ok_gigo, gigo_detail = proactive_gigo_cluster_identity_ok(ev)
        if not ok_gigo:
            logger.info("[%s] proactive skipped: GIGO %s", trace, gigo_detail)
            await _append_audit(
                ctx,
                trace_id=trace,
                rule_id=ev.rule_name,
                outcome="SKIPPED_GIGO",
                detail=gigo_detail,
                meta={"msg_id": msg_id, "namespace": ev.namespace, "trigger_len": len(ev.trigger_promql or "")},
            )
            return

    tok_trace = push_trace_id(trace)
    try:
        pattern_key = _pattern_key_from_event(ev)
        timed_out = False
        await emit_transition(
            ctx,
            trace_id=trace,
            transition=TRANSITION_CONTEXT_READY,
            component="proactive_observer",
            detail=f"pattern_key={pattern_key}",
        )
        prev_proactive = getattr(ctx, "inbound_proactive", False)
        prev_trace = getattr(ctx, "inbound_trace_id", "")
        ctx.inbound_proactive = True
        ctx.inbound_trace_id = trace
        # S2 — KHÔNG giữ token LLM ở đây nữa. Token nay chỉ bao đúng vòng ReAct
        # trong `_proactive_event_pipeline`; giai đoạn chạy probe/sinh bằng chứng
        # không cần LLM nên không được chiếm slot. Xem chú thích S2 ở chỗ acquire.
        t0_incident = time.monotonic()
        try:
            with proactive_trace_span(trace):
                try:
                    await asyncio.wait_for(
                        _proactive_event_pipeline(ctx, ev, msg_id, pattern_key, raw),
                        timeout=ws.proactive_event_timeout_sec,
                    )
                except asyncio.TimeoutError:
                    timed_out = True
                    inc_proactive_event_timeout()
                    logger.warning("[%s] proactive_event_timeout_sec exceeded", trace)
                    await _append_audit(
                        ctx,
                        trace_id=trace,
                        rule_id=ev.rule_name,
                        outcome="EVENT_TIMEOUT",
                        detail=f"limit_sec={ws.proactive_event_timeout_sec}",
                        meta={"path": "pipeline", "pattern_key": pattern_key, "msg_id": msg_id},
                    )
                    try:
                        await _append_dlq_proactive(
                            ctx,
                            trace_id=trace,
                            msg_id=msg_id,
                            reason="EVENT_TIMEOUT",
                            tombstone={"event_timeout_sec": ws.proactive_event_timeout_sec},
                            raw_event=raw[:8000] if isinstance(raw, str) else str(raw),
                        )
                    except Exception:
                        logger.exception("[%s] dlq event timeout", trace)
                    await emit_terminal_tombstone(
                        ctx,
                        trace_id=trace,
                        reason_code="EVENT_TIMEOUT",
                        component="proactive_observer",
                        detail=f"msg_id={msg_id}",
                    )
        finally:
            observe_proactive_incident_duration(time.monotonic() - t0_incident)
            ctx.inbound_proactive = prev_proactive
            ctx.inbound_trace_id = prev_trace
        if not timed_out:
            await emit_transition(
                ctx,
                trace_id=trace,
                transition=TRANSITION_VERIFIED_SUCCESS,
                component="proactive_observer",
                detail="pipeline_complete",
            )
        else:
            await emit_transition(
                ctx,
                trace_id=trace,
                transition=TRANSITION_REQUIRES_HUMAN,
                status="error",
                component="proactive_observer",
                detail="pipeline_timeout",
            )
    finally:
        pop_trace_id(tok_trace)


async def kafka_proactive_incidents_loop(ctx: WorkerHandlerContext, stop: asyncio.Event) -> None:
    from aiokafka import AIOKafkaConsumer

    from messaging.kafka_bus import decode_kafka_value_to_fields, kafka_msg_id

    ws = ctx.settings
    await ctx.scout_ready.wait()
    consumer = AIOKafkaConsumer(
        ws.kafka_topic_proactive_incidents,
        bootstrap_servers=ws.kafka_bootstrap_servers,
        group_id=ws.consumer_group_proactive,
        enable_auto_commit=False,
        client_id=ws.consumer_name_proactive,
    )
    await consumer.start()
    try:
        while not stop.is_set():
            if not ws.proactive_enabled:
                await asyncio.sleep(2)
                continue
            if await proactive_kill_switch_engaged(ctx.redis, ws.proactive_kill_switch_key):
                await asyncio.sleep(2)
                continue
            try:
                records = await consumer.getmany(timeout_ms=ws.proactive_block_ms, max_records=5)
            except Exception as e:
                logger.exception("proactive kafka getmany: %s", e)
                await asyncio.sleep(1)
                continue
            if not records:
                continue
            for _tp, batch in records.items():
                for msg in batch:
                    if stop.is_set():
                        return
                    fields = decode_kafka_value_to_fields(msg.value, msg.headers)
                    raw = fields.get("data") or fields.get("payload") or "{}"
                    msg_id = kafka_msg_id(msg.topic, msg.partition, msg.offset)
                    try:
                        await _process_proactive_message(ctx, msg_id, raw)
                    except Exception as e:
                        logger.exception("[%s] proactive handler error: %s", msg_id, e)
                        await ctx.ledger.record_exception(
                            e, phase="4", component="proactive_control", swallow_errors=True
                        )
                        assert ctx.kafka is not None
                        await ctx.kafka.send_dict(
                            ctx.settings.kafka_topic_dlq,
                            {"error": str(e), "trace_id": msg_id, "data": raw},
                        )
                    finally:
                        await consumer.commit()
    finally:
        await consumer.stop()


async def proactive_evaluate_loop(ctx: WorkerHandlerContext, stop: asyncio.Event) -> None:
    ws = ctx.settings
    await ctx.scout_ready.wait()
    while not stop.is_set():
        if not ws.proactive_enabled:
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            continue
        try:
            await evaluate_proactive_triggers(ctx)
        except Exception as e:
            logger.exception("proactive_evaluate: %s", e)
            try:
                await ctx.ledger.record_exception(e, phase="4", component="proactive_evaluate", swallow_errors=True)
            except Exception:
                pass
        # S2.3: check elevated watch flag (set by forecast loop on threshold breach).
        # If any workload is under elevated watch, halve the sleep interval.
        eval_interval = float(ws.proactive_eval_interval_sec)
        try:
            if bool(getattr(ws, "forecast_proactive_integration_enabled", True)):
                elevated_keys = []
                _cursor = 0
                while True:
                    _cursor, _batch = await ctx.redis.scan(_cursor, match="omni:proactive:elevated:*", count=100)
                    elevated_keys.extend(_batch)
                    if _cursor == 0:
                        break
                if elevated_keys:
                    eval_interval = max(10.0, eval_interval / 2)
                    logger.debug(
                        "proactive elevated_watch active keys=%d interval_sec=%.1f",
                        len(elevated_keys), eval_interval,
                    )
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=eval_interval)
        except asyncio.TimeoutError:
            pass
