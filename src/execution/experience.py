"""Sandbox → lesson (1.5B, &lt;200 tokens) → RAG action_experience + retrieval context."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime
from dataclasses import dataclass
from typing import Any

from rag.pgvector_store import (
    COLLECTION_ACTION_EXPERIENCE, 
    EMBED_DIM, 
    PointStruct
)
from execution.memory_normalize import (
    canonical_symptom_text,
    extract_workload_fingerprint,
    stable_playbook_pattern_key,
    strip_ephemeral_from_args,
)
from workers.routing_policy import (
    ROUTING_SOURCE_AGENT_SESSION,
    ROUTING_SOURCE_SLOW_PATH,
    ROUTING_SOURCE_SLOW_PATH_EXHAUSTED,
    is_fast_path_auto_allowed,
)
from workers.slow_path_trace import AttemptRecord, summarize_attempts_for_rag
from workers.settings import WorkerSettings

logger = logging.getLogger(__name__)


def truncate_lesson_to_budget(text: str, max_chars: int) -> str:
    t = (text or "").strip().replace("\n", " ")
    if len(t) <= max_chars:
        return t
    return t[: max_chars - 3].rstrip() + "..."


def _embedding_from_response(resp: dict[str, Any]) -> list[float]:
    if "embedding" in resp:
        emb = resp["embedding"]
        return list(emb) if not isinstance(emb, list) else emb
    embs = resp.get("embeddings")
    if isinstance(embs, list) and embs:
        return list(embs[0])
    raise ValueError("embed response missing embedding(s)")


@dataclass
class SandboxLessonInput:
    trace_id: str
    run_id: str
    command: str
    exit_code: int
    stdout: str
    stderr: str
    user_snippet: str
    policy_blocked: bool
    policy_reason: str


async def synthesize_lesson_text(
    llm: Any,
    ws: WorkerSettings,
    inp: SandboxLessonInput,
    *,
    log_clip: int,
) -> str:
    if inp.policy_blocked:
        return truncate_lesson_to_budget(
            f"Bài học: lệnh bị policy chặn ({inp.policy_reason}). Không chạy sandbox.",
            ws.lesson_max_chars,
        )
    blob = (
        f"exit={inp.exit_code}\n"
        f"cmd={inp.command[:400]}\n"
        f"out={(inp.stdout or '')[:log_clip]}\n"
        f"err={(inp.stderr or '')[:log_clip]}\n"
        f"user={(inp.user_snippet or '')[:500]}"
    )
    try:
        resp = await llm.chat(
            model=ws.model_helper,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Viết ĐÚNG MỘT đoạn bài học kỹ thuật tối đa 3 câu, tiếng Việt. "
                        "Nói rõ lệnh/exit có ổn không và rủi ro. Không markdown, không nhắc policy."
                    ),
                },
                {"role": "user", "content": blob[:6000]},
            ],
            options={"temperature": 0.0, "num_predict": 180},
        )
        raw = ((resp.get("message") or {}).get("content") or "").strip()
    except Exception as e:
        logger.warning("lesson synthesize fail: %s", e)
        raw = f"(lesson_error) exit={inp.exit_code} cmd_snip={inp.command[:80]!r}"
    return truncate_lesson_to_budget(raw, ws.lesson_max_chars)


def routing_experience_point_id(user_text: str, tool: str, args: dict[str, Any]) -> str:
    """Stable id from canonical symptom string + tool + args (use stripped args for playbooks)."""
    norm = " ".join((user_text or "").lower().split())[:4000]
    canon = json.dumps(args or {}, sort_keys=True, ensure_ascii=False, default=str)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"route:{norm}:{tool}:{canon}"))


async def upsert_action_experience(
    vector_store: Any,
    *,
    lesson: str,
    vector: list[float],
    payload: dict[str, Any],
    point_id: str | None = None,
) -> str:
    # Schema initialized via schema.sql
    if len(vector) != EMBED_DIM:
        vector = (vector + [0.0] * EMBED_DIM)[:EMBED_DIM]
    if point_id is not None:
        pid = point_id
    else:
        pid = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                payload.get("trace_id", "") + ":" + payload.get("run_id", "") + ":" + lesson[:200],
            )
        )
    await vector_store.upsert(
        collection_name=COLLECTION_ACTION_EXPERIENCE,
        points=[PointStruct(id=pid, vector=vector, payload=payload)],
    )
    return pid


async def record_routing_from_success(
    ctx: Any,
    *,
    tool: str,
    args: dict[str, Any],
    trace_id: str,
) -> None:
    """Sau slow-path tool OK — embed symptom chuẩn hoá, upsert playbook vào action_experience."""
    ws: WorkerSettings = ctx.settings
    if not getattr(ws, "routing_experience_enabled", True):
        return
    if not getattr(ws, "action_experience_enabled", True):
        return
    t = (tool or "").strip()
    if t in ("echo", "reply"):
        return
    user_text = (getattr(ctx, "inbound_user_text", None) or "").strip()
    if len(user_text) < 4:
        return
    raw_args = dict(args) if isinstance(args, dict) else {}
    args_playbook = strip_ephemeral_from_args(raw_args)
    auto_execute = is_fast_path_auto_allowed(t, ws)
    strip_pods = bool(getattr(ws, "memory_canonical_strip_pods", True))
    symptom_text = canonical_symptom_text(user_text, strip_pods=strip_pods)
    match_text = symptom_text[: ws.routing_experience_max_chars]
    workload_fp = extract_workload_fingerprint(user_text)
    lesson = f"[định tuyến] {t} — {match_text[:120]}{'…' if len(match_text) > 120 else ''}"
    pid = routing_experience_point_id(symptom_text, t, args_playbook)
    args_hash = hashlib.sha256(
        json.dumps(args_playbook, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()[:24]
    pattern_key = stable_playbook_pattern_key(t, symptom_text, args_playbook)
    slot_held = bool(getattr(ctx, "llm_slot_held", False))
    token: str | None = None
    if not slot_held:
        token = await ctx.semaphore.acquire()
    try:
        emb = await ctx.llm.embed(
            model=ws.embed_model,
            input=match_text[:8000],
        )
        vec = _embedding_from_response(emb)
        pay: dict[str, Any] = {
            "memory_kind": "playbook",
            "symptom_text": symptom_text[:2000],
            "workload_fingerprint": workload_fp,
            "args_playbook": args_playbook,
            "lesson": lesson,
            "routing_source": ROUTING_SOURCE_SLOW_PATH,
            "tool": t,
            "args": args_playbook,
            "args_hash": args_hash,
            "pattern_key": pattern_key,
            "auto_execute": auto_execute,
            "match_text": match_text[:2000],
            "exec_outcome": "success",
            "biz_outcome": "unknown",
            "verification_result": "not_checked",
            "latency_ms": 0,
            "safety_flag": "normal",
            "trace_id": trace_id,
            "run_id": "",
            "ts": datetime.now(UTC).isoformat(),
        }
        await upsert_action_experience(ctx.vector_store, lesson=lesson, vector=vec, payload=pay, point_id=pid)
        logger.info("[%s] routing_experience_upsert tool=%s auto_execute=%s", trace_id, t, auto_execute)
    except Exception as e:
        logger.debug("record_routing_from_success skip: %s", e)
    finally:
        if token is not None:
            await ctx.semaphore.release(token)


async def record_agent_playbook_from_trajectory(
    ctx: Any,
    *,
    user_text: str,
    trajectory: list[dict[str, Any]],
    trace_id: str,
    resolution_summary: str = "",
) -> None:
    """Sau omni_mark_resolved — một lần upsert playbook (routing_source=agent_session_resolved)."""
    ws: WorkerSettings = ctx.settings
    if not getattr(ws, "routing_experience_enabled", True):
        return
    if not getattr(ws, "action_experience_enabled", True):
        return
    ut = (user_text or "").strip()
    if len(ut) < 4:
        return
    steps_clean: list[dict[str, Any]] = []
    last_tool = ""
    for step in trajectory:
        if not isinstance(step, dict):
            continue
        tool = str(step.get("tool") or "").strip()
        if tool == "omni_mark_resolved":
            continue
        last_tool = tool or last_tool
        a = step.get("args") if isinstance(step.get("args"), dict) else {}
        steps_clean.append({"tool": tool, "args": strip_ephemeral_from_args(a)})
    tool = last_tool or "reply"
    if tool in ("echo", "reply") and not steps_clean:
        return
    strip_pods = bool(getattr(ws, "memory_canonical_strip_pods", True))
    symptom_text = canonical_symptom_text(ut, strip_pods=strip_pods)
    match_text = symptom_text[: ws.routing_experience_max_chars]
    workload_fp = extract_workload_fingerprint(ut)
    args_playbook: dict[str, Any] = {}
    for step in reversed(trajectory):
        if not isinstance(step, dict):
            continue
        if str(step.get("tool") or "").strip() == "omni_mark_resolved":
            continue
        a = step.get("args") if isinstance(step.get("args"), dict) else {}
        args_playbook = strip_ephemeral_from_args(a)
        break
    if not args_playbook and steps_clean:
        args_playbook = dict(steps_clean[-1].get("args") or {})
    lesson = (
        f"[agentic] {tool} — {resolution_summary[:160] or match_text[:120]}"
        f"{'…' if len(match_text) > 120 else ''}"
    )
    pid = routing_experience_point_id(symptom_text, tool, args_playbook)
    args_hash = hashlib.sha256(
        json.dumps(args_playbook, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()[:24]
    pattern_key = stable_playbook_pattern_key(tool, symptom_text, args_playbook)
    auto_execute = is_fast_path_auto_allowed(tool, ws)
    slot_held = bool(getattr(ctx, "llm_slot_held", False))
    token: str | None = None
    if not slot_held:
        token = await ctx.semaphore.acquire()
    try:
        emb = await ctx.llm.embed(
            model=ws.embed_model,
            input=match_text[:8000],
        )
        vec = _embedding_from_response(emb)
        pay: dict[str, Any] = {
            "memory_kind": "playbook",
            "symptom_text": symptom_text[:2000],
            "workload_fingerprint": workload_fp,
            "args_playbook": args_playbook,
            "steps": steps_clean[:64],
            "lesson": lesson[:1200],
            "routing_source": ROUTING_SOURCE_AGENT_SESSION,
            "tool": tool,
            "args": args_playbook,
            "args_hash": args_hash,
            "pattern_key": pattern_key,
            "auto_execute": auto_execute,
            "match_text": match_text[:2000],
            "exec_outcome": "success",
            "biz_outcome": "unknown",
            "verification_result": "not_checked",
            "latency_ms": 0,
            "safety_flag": "normal",
            "trace_id": trace_id,
            "run_id": "",
            "resolution_summary": (resolution_summary or "")[:2000],
            "ts": datetime.now(UTC).isoformat(),
        }
        await upsert_action_experience(ctx.vector_store, lesson=lesson, vector=vec, payload=pay, point_id=pid)
        logger.info("[%s] agent_playbook_upsert tool=%s auto_execute=%s", trace_id, tool, auto_execute)
    except Exception as e:
        logger.debug("record_agent_playbook_from_trajectory skip: %s", e)
    finally:
        if token is not None:
            await ctx.semaphore.release(token)


async def record_routing_exhausted_no_data(
    ctx: Any,
    user_text: str,
    *,
    trace_id: str,
    detail: str = "",
    attempt_trace: list[AttemptRecord] | None = None,
    exit_reason: str = "max_attempts",
) -> None:
    """Hết vòng slow-path — upsert RAG (không dùng làm gợi ý trong fetch_action_experience_context)."""
    ws: WorkerSettings = ctx.settings
    if not getattr(ws, "action_experience_enabled", True):
        return
    ut = " ".join((user_text or "").strip().lower().split())[:4000]
    if len(ut) < 4:
        return
    tr = attempt_trace or []
    tools_ordered: list[str] = []
    sigs_ordered: list[str] = []
    for r in tr:
        if r.tool and r.tool not in tools_ordered:
            tools_ordered.append(r.tool)
        if r.error_signature not in sigs_ordered:
            sigs_ordered.append(r.error_signature)
    lesson = f"[không có dữ liệu exit={exit_reason}] {ut[:200]}{'…' if len(ut) > 200 else ''}"
    pid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"slow_exhausted:{ut}"))
    slot_held = bool(getattr(ctx, "llm_slot_held", False))
    token: str | None = None
    if not slot_held:
        token = await ctx.semaphore.acquire()
    try:
        emb = await ctx.llm.embed(
            model=ws.embed_model,
            input=(user_text or "")[:8000],
        )
        vec = _embedding_from_response(emb)
        pay: dict[str, Any] = {
            "lesson": lesson,
            "routing_source": ROUTING_SOURCE_SLOW_PATH_EXHAUSTED,
            "outcome": "no_data",
            "exec_outcome": "fail",
            "biz_outcome": "unknown",
            "verification_result": "not_checked",
            "unknown_reason": "slow_path_exhausted",
            "match_text": ut[:2000],
            "trace_id": trace_id,
            "last_error_hint": (detail or "")[:500],
            "exit_reason": (exit_reason or "max_attempts")[:48],
            "attempt_count": len(tr),
            "tools_attempted": tools_ordered[:32],
            "error_signatures": sigs_ordered[:32],
            "attempts_summary": summarize_attempts_for_rag(tr),
            "ts": datetime.now(UTC).isoformat(),
        }
        await upsert_action_experience(ctx.vector_store, lesson=lesson, vector=vec, payload=pay, point_id=pid)
        logger.info("[%s] routing_exhausted_no_data_upsert reason=%s n_attempts=%s", trace_id, exit_reason, len(tr))
    except Exception as e:
        logger.debug("record_routing_exhausted_no_data skip: %s", e)
    finally:
        if token is not None:
            await ctx.semaphore.release(token)


async def record_sandbox_lesson(
    ctx: Any,
    inp: SandboxLessonInput,
) -> None:
    ws: WorkerSettings = ctx.settings
    if not getattr(ws, "action_experience_enabled", True):
        return
    slot_held = bool(getattr(ctx, "llm_slot_held", False))
    token: str | None = None
    if not slot_held:
        token = await ctx.semaphore.acquire()
    try:
        lesson = await synthesize_lesson_text(ctx.llm, ws, inp, log_clip=ws.sandbox_log_clip_chars)
        emb = await ctx.llm.embed(
            model=ws.embed_model,
            input=lesson[:8000],
        )
        vec = _embedding_from_response(emb)
        pay = {
            "lesson": lesson,
            "trace_id": inp.trace_id,
            "run_id": inp.run_id,
            "exit_code": inp.exit_code,
            "command_hash": hashlib.sha256(inp.command.encode()).hexdigest()[:24],
            "outcome": "policy_blocked" if inp.policy_blocked else ("ok" if inp.exit_code == 0 else "fail"),
            "exec_outcome": "fail" if inp.policy_blocked or inp.exit_code != 0 else "success",
            "biz_outcome": "unknown",
            "verification_result": "not_checked",
            "unknown_reason": inp.policy_reason[:200] if inp.policy_blocked else "",
            "latency_ms": 0,
            "safety_flag": "policy_blocked" if inp.policy_blocked else "normal",
            "ts": datetime.now(UTC).isoformat(),
        }
        await upsert_action_experience(ctx.vector_store, lesson=lesson, vector=vec, payload=pay)
    except Exception as e:
        logger.debug("record_sandbox_lesson skip: %s", e)
    finally:
        if token is not None:
            await ctx.semaphore.release(token)


async def fetch_action_experience_context(
    ctx: Any,
    query_text: str,
) -> str:
    ws: WorkerSettings = ctx.settings
    if not getattr(ws, "action_experience_enabled", True):
        return ""
    q_raw = (query_text or "").strip()[:2000]
    strip_pods = bool(getattr(ws, "memory_canonical_strip_pods", True))
    q = canonical_symptom_text(q_raw, strip_pods=strip_pods)
    if len(q) < 8:
        return ""
    token = await ctx.semaphore.acquire()
    try:
        emb = await ctx.llm.embed(
            model=ws.embed_model,
            input=q,
        )
        vec = _embedding_from_response(emb)
        resp = await ctx.vector_store.query_points(
            collection_name=COLLECTION_ACTION_EXPERIENCE,
            query=vec,
            limit=2,
            score_threshold=ws.action_experience_score_threshold,
            with_payload=True,
        )
        lines: list[str] = []
        for pt in resp.points or []:
            pl = dict(pt.payload or {})
            rs = pl.get("routing_source")
            # Exhausted slow-path records are telemetry only (see record_routing_exhausted_no_data).
            # Successful routing (slow_path / agent_session) MUST surface for retrieval.
            if rs == ROUTING_SOURCE_SLOW_PATH_EXHAUSTED:
                continue
            les = str(pl.get("lesson") or "")[:400]
            if les:
                lines.append(f"- (score={pt.score:.2f}) {les}")
        if not lines:
            return ""
        return "[CONTEXT: action_experience]\n" + "\n".join(lines)
    except Exception as e:
        logger.debug("action_experience query skip: %s", e)
        return ""
    finally:
        await ctx.semaphore.release(token)
