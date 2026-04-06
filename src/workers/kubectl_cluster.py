"""kubectl subprocess — dùng khi kubernetes_asyncio không đủ (apply, delete, CRD, …)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any

from pydantic import BaseModel, Field

from workers.tool_registry import register_tool

logger = logging.getLogger(__name__)

_MAX_ARGV = 64
_MAX_OUT = 400_000
_DEFAULT_TIMEOUT = 300.0


def cluster_ops_allowed(ctx: Any) -> bool:
    ws = getattr(ctx, "settings", None)
    if ws is None:
        return False
    return bool(
        getattr(ws, "cluster_full_access", False)
        or getattr(ws, "lab_unchained", False)
        or getattr(ws, "god_mode", False)
    )


class KubectlClusterArgs(BaseModel):
    """Đối số sau `kubectl` (không bao gồm tiền tố kubectl). Ví dụ: ['rollout', 'restart', 'deploy/x', '-n', 'ns']."""

    args: list[str] = Field(
        ...,
        min_length=1,
        description="Argv sau kubectl, ví dụ ['get','pods','-A']",
    )
    timeout_sec: float = Field(default=_DEFAULT_TIMEOUT, ge=5.0, le=3600.0)
    reasoning: str = Field(default="", max_length=500)


async def _audit_kubectl(
    ctx: Any,
    *,
    trace_id: str,
    argv: list[str],
    exit_code: int | None,
    stdout: str,
    stderr: str,
) -> None:
    ws = getattr(ctx, "settings", None)
    if ws is None or getattr(ctx, "redis", None) is None:
        return
    body: dict[str, Any] = {
        "ts": time.time(),
        "trace_id": trace_id,
        "run_id": str(uuid.uuid4()),
        "argv": argv[:80],
        "exit_code": exit_code,
        "source": "kubectl_cluster",
        "stdout_preview": stdout[:8000],
        "stderr_preview": stderr[:4000],
    }
    try:
        k = getattr(ctx, "kafka", None)
        if k is not None:
            await k.send_dict(ws.kafka_topic_audit_sandbox, {"data": json.dumps(body, ensure_ascii=False)})
    except Exception as e:
        logger.debug("audit kubectl_cluster skip: %s", e)


@register_tool("kubectl_cluster", KubectlClusterArgs)
async def tool_kubectl_cluster(ctx: Any, args: KubectlClusterArgs) -> str:
    """Chạy `kubectl` với argv an toàn (list, không shell) — bật khi OMNI_CLUSTER_FULL_ACCESS hoặc lab/god."""
    if not cluster_ops_allowed(ctx):
        return (
            "[DATA] error\n[DIAGNOSIS] `kubectl_cluster` requires OMNI_CLUSTER_FULL_ACCESS=true "
            "or OMNI_LAB_UNCHAINED / OMNI_GOD_MODE."
        )
    raw = [str(x).strip() for x in args.args if str(x).strip()]
    if not raw:
        return "[DATA] error\n[DIAGNOSIS] args.args is empty."
    if len(raw) > _MAX_ARGV:
        return f"[DATA] error\n[DIAGNOSIS] Too many arguments (max {_MAX_ARGV})."
    argv = ["kubectl", *raw]
    trace = str(getattr(ctx, "inbound_trace_id", "") or "kubectl").strip() or "kubectl"
    timeout = float(args.timeout_sec)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        code = proc.returncode
    except asyncio.TimeoutError:
        await _audit_kubectl(
            ctx,
            trace_id=trace,
            argv=argv,
            exit_code=None,
            stdout="",
            stderr=f"timeout after {timeout}s",
        )
        return f"[DATA] error\n[DIAGNOSIS] kubectl timeout {timeout}s\n[TRACE] {trace}"
    except Exception as e:
        await _audit_kubectl(ctx, trace_id=trace, argv=argv, exit_code=None, stdout="", stderr=str(e))
        return f"[DATA] error\n[DIAGNOSIS] kubectl exec failed: {e!s}\n[TRACE] {trace}"

    out = (out_b or b"").decode("utf-8", errors="replace")
    err = (err_b or b"").decode("utf-8", errors="replace")
    if len(out) > _MAX_OUT:
        out = out[: _MAX_OUT - 80] + "\n... [truncated]"
    await _audit_kubectl(ctx, trace_id=trace, argv=argv, exit_code=code, stdout=out, stderr=err)
    rs = (args.reasoning or "").strip()
    tail = f" reasoning={rs[:200]}" if rs else ""
    if code != 0:
        return (
            f"[DATA] kubectl_exit_{code}\n[DIAGNOSIS] stderr={err[:4000]!s}\nstdout={out[:4000]!s}{tail}\n"
            f"[TRACE] {trace}"
        )
    return (
        f"[DATA] kubectl_ok exit=0\n[DIAGNOSIS]\n{out[:12000]}{tail}\n[TRACE] {trace}"
    )
