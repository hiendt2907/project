"""Tool thực thi lệnh qua OpenSandbox — cấm shell trực tiếp trên pod omni-worker."""

from __future__ import annotations

from typing import Any

from execution.experience import SandboxLessonInput, record_sandbox_lesson
from execution.manager import SandboxManager, auto_cleanup_sandboxes, sandbox_result_to_user_text
from execution.pod_env_clone import clone_pod_env_for_sandbox, clone_pod_labels_for_sandbox


async def tool_execute_in_sandbox(ctx: Any, args: dict[str, Any]) -> str:
    """
    Chạy lệnh shell trong sandbox (HTTP → OpenSandbox server).
    args: command (bắt buộc), session_id?, trace_id?, image?,
    reference_namespace + reference_pod [, reference_container], env?, pod_labels?
    """
    m = SandboxManager(ctx.settings)
    trace = str(args.get("trace_id") or getattr(ctx, "inbound_trace_id", "") or "unknown")
    session = str(args.get("session_id") or trace)[:200]
    cmd = str(args.get("command") or args.get("cmd") or "").strip()
    image = str(args.get("image") or "").strip() or None

    env_list: list[dict[str, str]] = []
    pod_labels: dict[str, str] = {}

    ref_ns = str(args.get("reference_namespace") or "").strip()
    ref_pod = str(args.get("reference_pod") or "").strip()
    ref_ctr = str(args.get("reference_container") or "").strip() or None
    if ref_ns and ref_pod:
        env_list = await clone_pod_env_for_sandbox(ref_ns, ref_pod, ref_ctr)
        pod_labels = await clone_pod_labels_for_sandbox(ref_ns, ref_pod)

    extra_env = args.get("env")
    if isinstance(extra_env, list):
        for e in extra_env[:32]:
            if isinstance(e, dict) and e.get("name"):
                env_list.append({"name": str(e["name"])[:253], "value": str(e.get("value", ""))[:1024]})

    extra_lbl = args.get("pod_labels")
    if isinstance(extra_lbl, dict):
        for k, v in list(extra_lbl.items())[:16]:
            pod_labels[str(k)[:63]] = str(v)[:128]

    res = await m.execute_shell_structured(
        redis=ctx.redis,
        command=cmd,
        session_id=session,
        trace_id=trace,
        image=image,
        env=env_list or None,
        pod_labels=pod_labels or None,
    )

    await record_sandbox_lesson(
        ctx,
        SandboxLessonInput(
            trace_id=res.trace_id,
            run_id=res.run_id,
            command=res.command,
            exit_code=res.exit_code,
            stdout=res.stdout,
            stderr=res.stderr,
            user_snippet=str(getattr(ctx, "inbound_user_text", "") or "")[:2000],
            policy_blocked=res.exit_code == -2,
            policy_reason=res.policy_reason,
        ),
    )
    return sandbox_result_to_user_text(res)


async def tool_sandbox_cleanup(ctx: Any, args: dict[str, Any]) -> str:
    """Hook dọn sandbox (placeholder + Redis active set)."""
    trace = str(args.get("trace_id") or getattr(ctx, "inbound_trace_id", "") or "")
    return await auto_cleanup_sandboxes(ctx.redis, trace_id=trace)
