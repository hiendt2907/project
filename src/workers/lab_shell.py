"""LAB: subprocess shell trên omni-worker — audit Redis stream audit:sandbox (cùng key với OpenSandbox)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any

from execution.policy import PolicyVerdict, check_sandbox_command

logger = logging.getLogger(__name__)

_MAX_CMD = 8000
_MAX_OUT = 400_000
_TIMEOUT_SEC = 120.0


async def _audit_lab_shell(
    ctx: Any,
    *,
    trace_id: str,
    command: str,
    exit_code: int | None,
    stdout: str,
    stderr: str,
) -> None:
    ws = ctx.settings
    body: dict[str, Any] = {
        "ts": time.time(),
        "trace_id": trace_id,
        "session_id": "lab_shell",
        "run_id": str(uuid.uuid4()),
        "command": command[:2000],
        "command_truncated": len(command) > 2000,
        "policy_result": "lab_shell",
        "policy_reason": "",
        "exit_code": exit_code,
        "source": "lab_shell",
        "stdout_preview": stdout[:8000],
        "stderr_preview": stderr[:4000],
    }
    try:
        await ctx.redis.xadd(
            ws.audit_sandbox_stream,
            {"data": json.dumps(body, ensure_ascii=False)},
            maxlen=ws.audit_sandbox_maxlen,
            approximate=True,
        )
    except Exception as e:
        logger.debug("audit lab_shell skip: %s", e)


async def tool_execute_shell_command(ctx: Any, args: dict[str, Any]) -> str:
    """
    Chạy lệnh shell trên pod worker (LAB ONLY).
    args: command (bắt buộc), trace_id?, timeout_sec?
    """
    if not getattr(ctx.settings, "lab_unchained", False):
        return (
            "[DATA] error\n[DIAGNOSIS] `execute_shell_command` chỉ bật khi OMNI_LAB_UNCHAINED=true "
            "(hoặc OMNI_GOD_MODE=true)."
        )
    cmd = str(args.get("command") or "").strip()
    trace = str(args.get("trace_id") or getattr(ctx, "inbound_trace_id", "") or "unknown").strip()
    if not cmd:
        return "[DATA] error\n[DIAGNOSIS] Thiếu args.command."
    if len(cmd) > _MAX_CMD:
        return f"[DATA] error\n[DIAGNOSIS] Lệnh quá dài (max {_MAX_CMD})."

    pol = check_sandbox_command(cmd, lab_unchained=True)
    if pol.verdict == PolicyVerdict.DENIED:
        await _audit_lab_shell(
            ctx,
            trace_id=trace,
            command=cmd,
            exit_code=-2,
            stdout="",
            stderr=pol.reason,
        )
        return f"[DATA] error\n[DIAGNOSIS] Policy: {pol.reason}\n[TRACE] {trace}"

    timeout = float(args.get("timeout_sec") or _TIMEOUT_SEC)
    timeout = max(5.0, min(timeout, 600.0))
    logger.info("[LAB_MODE] Unchained. shell trace=%s cmd_preview=%s", trace, cmd[:120])

    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=_MAX_OUT,
    )
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await _audit_lab_shell(
            ctx,
            trace_id=trace,
            command=cmd,
            exit_code=-1,
            stdout="",
            stderr="timeout",
        )
        return f"[DATA] error\n[DIAGNOSIS] Timeout {timeout}s\n[TRACE] {trace}"

    stdout = (out_b or b"").decode("utf-8", errors="replace")
    stderr = (err_b or b"").decode("utf-8", errors="replace")
    code = int(proc.returncode if proc.returncode is not None else -1)
    await _audit_lab_shell(
        ctx,
        trace_id=trace,
        command=cmd,
        exit_code=code,
        stdout=stdout,
        stderr=stderr,
    )
    clip = 12000
    so = stdout[:clip] + ("…" if len(stdout) > clip else "")
    se = stderr[:4000] + ("…" if len(stderr) > 4000 else "")
    return f"[DATA] exit={code}\n[STDOUT]\n{so}\n[STDERR]\n{se}\n[TRACE] {trace}"
