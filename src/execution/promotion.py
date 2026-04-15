"""Gated path: sandbox → lesson (tool layer) → DeepSeek JSON validate → allowlisted SDK tool only."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from execution.manager import SandboxManager
from execution.policy import PolicyVerdict, check_promotion_tool
from workers.k8s_tools import (
    deployment_evidence_snapshot,
    execute_rollout_restart_from_pending,
    redis_key_write_pending,
)
from workers.settings import WorkerSettings
from workers.tool_registry import get_tool_registry
from workers.tools import TOOL_REGISTRY

logger = logging.getLogger(__name__)

_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)


def _strip_model_noise(text: str) -> str:
    s = _THINK_RE.sub("", text or "")
    s = s.strip()
    if s.startswith("```"):
        lines = s.split("\n")
        s = "\n".join(lines[1:-1] if len(lines) > 2 and lines[-1].strip().startswith("```") else lines[1:])
    return s.strip()


def _parse_validation_json(raw: str) -> dict[str, Any]:
    s = _strip_model_noise(raw)
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        raise ValueError("no json object")
    return json.loads(s[i : j + 1])


async def run_gated_allowlisted_execute(ctx: Any, args: dict[str, Any]) -> str:
    ws: WorkerSettings = ctx.settings
    trace = str(args.get("trace_id") or getattr(ctx, "inbound_trace_id", "") or "unknown")
    sandbox_command = str(args.get("sandbox_command") or args.get("command") or "").strip()
    intended_tool = str(args.get("intended_tool") or "").strip()
    tool_args = args.get("tool_args") if isinstance(args.get("tool_args"), dict) else {}
    user_goal = str(args.get("user_goal") or getattr(ctx, "inbound_user_text", "") or "")[:4000]

    pol = check_promotion_tool(
        intended_tool,
        lab_unchained=bool(ws.lab_unchained),
        cluster_full_access=bool(getattr(ws, "cluster_full_access", False)),
        env_mode=str(getattr(ws, "env_mode", "prod") or "prod"),
    )
    if pol.verdict == PolicyVerdict.DENIED:
        return f"[DATA] error\n[DIAGNOSIS] Promotion policy: {pol.reason}"

    mgr = SandboxManager(ws)
    res = await mgr.execute_shell_structured(
        kafka=ctx.kafka,
        command=sandbox_command,
        session_id=str(args.get("session_id") or trace)[:200],
        trace_id=trace,
        image=str(args.get("image") or "").strip() or None,
        env=args.get("env") if isinstance(args.get("env"), list) else None,
        pod_labels=args.get("pod_labels") if isinstance(args.get("pod_labels"), dict) else None,
    )

    from execution.experience import SandboxLessonInput, record_sandbox_lesson

    await record_sandbox_lesson(
        ctx,
        SandboxLessonInput(
            trace_id=res.trace_id,
            run_id=res.run_id,
            command=res.command,
            exit_code=res.exit_code,
            stdout=res.stdout,
            stderr=res.stderr,
            user_snippet=user_goal,
            policy_blocked=res.exit_code == -2,
            policy_reason=res.policy_reason,
        ),
    )

    if res.exit_code == -2:
        return (
            "[DATA] error\n[DIAGNOSIS] Sandbox command blocked by policy — no cluster write.\n"
            f"[TRACE] {trace}"
        )

    sandbox_summary = (
        f"exit={res.exit_code}\nstdout[:1200]={res.stdout[:1200]!r}\nstderr[:800]={res.stderr[:800]!r}"
    )
    val_prompt = (
        f"User goal:\n{user_goal[:3000]}\n\nSandbox result:\n{sandbox_summary}\n\n"
        "Trả về DUY NHẤT một JSON object keys: pass (boolean), confidence (0-1 float), rationale (string ngắn). "
        "pass=true chỉ nếu kết quả sandbox cho thấy thao tác an toàn và phù hợp goal (restart deployment, không phá cluster). "
        "confidence là độ tin cậy của bạn."
    )

    slot_held = bool(getattr(ctx, "llm_slot_held", False))
    val_token: str | None = None
    if not slot_held:
        val_token = await ctx.semaphore.acquire()
    try:
        oresp = await ctx.llm.chat(
            model=ws.model_reasoning_engine,
            messages=[
                {"role": "system", "content": "Chỉ JSON, không markdown."},
                {"role": "user", "content": val_prompt},
            ],
            options={"temperature": 0.1, "num_predict": 256},
        )
        vraw = ((oresp.get("message") or {}).get("content") or "").strip()
        try:
            vd = _parse_validation_json(vraw)
        except Exception as e:
            logger.info("gated validation json fail: %s raw=%s", e, vraw[:200])
            return (
                "[DATA] error\n[DIAGNOSIS] Could not parse validation JSON — no cluster write.\n"
                f"[TRACE] {trace}"
            )
        ok = bool(vd.get("pass"))
        conf = float(vd.get("confidence") or 0.0)
        rationale = str(vd.get("rationale") or "")[:500]
    finally:
        if val_token is not None:
            await ctx.semaphore.release(val_token)

    if not ok or conf < ws.promotion_confidence_min:
        return (
            "[DATA] sandbox_only\n[DIAGNOSIS] Sandbox ran; validation did not pass "
            f"(pass={ok}, conf={conf:.2f}). {rationale}\n"
            f"[TRACE] {trace}"
        )

    chat_id = getattr(ctx, "telegram_chat_id", None)
    skip_tg_confirm = bool(ws.lab_unchained or getattr(ws, "cluster_full_access", False))
    if intended_tool == "k8s_rollout_restart" and chat_id is not None and not skip_tg_confirm:
        ns = str(tool_args.get("namespace") or tool_args.get("ns") or "").strip()
        dep = str(
            tool_args.get("deployment")
            or tool_args.get("deployment_name")
            or tool_args.get("name")
            or ""
        ).strip()
        if ns and dep:
            pending = {
                "kind": "k8s_rollout_restart",
                "namespace": ns,
                "deployment": dep,
                "trace_id": trace,
            }
            if bool(getattr(ws, "pre_action_state_revalidate_enabled", True)):
                try:
                    pending["evidence_snapshot"] = await deployment_evidence_snapshot(ns, dep)
                except Exception as e:
                    logger.warning("write_pending evidence_snapshot: %s", e)
            await ctx.redis.set(
                redis_key_write_pending(int(chat_id)),
                json.dumps(pending, ensure_ascii=False),
                ex=ws.write_pending_ttl_sec,
            )
            return (
                "[CONFIRM_REQUIRED] Sau sandbox + validation, em sẵn sàng rollout thật. "
                f"deployment={dep} namespace={ns}. Trả lời xác nhận (ok/confirm) để thực hiện.\n"
                f"[TRACE] {trace}"
            )

    reg = get_tool_registry()
    if reg.has(intended_tool):
        out = await reg.invoke(ctx, intended_tool, tool_args if isinstance(tool_args, dict) else {})
        return f"{out}\n[TRACE] {trace}"
    fn = TOOL_REGISTRY.get(intended_tool)
    if fn is not None:
        out = await fn(ctx, tool_args if isinstance(tool_args, dict) else {})
        return f"{out}\n[TRACE] {trace}"

    return f"[DATA] error\n[DIAGNOSIS] Tool chưa wired: {intended_tool}\n[TRACE] {trace}"


async def execute_write_pending_from_redis(ctx: Any, data: dict[str, Any]) -> str:
    kind = str(data.get("kind") or "")
    trace = str(data.get("trace_id") or getattr(ctx, "inbound_trace_id", "") or "unknown")
    if kind == "k8s_rollout_restart":
        out = await execute_rollout_restart_from_pending(ctx, data)
        return f"{out}\n[TRACE] {trace}"
    return f"[DATA] error\n[DIAGNOSIS] Unknown pending kind={kind!r}"
