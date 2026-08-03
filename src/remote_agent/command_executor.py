"""Command executor for remote agent — cưỡng chế catalogue lệnh chẩn đoán.

Chính sách "lệnh nào được chạy" KHÔNG còn ở file này: nó ở
`config/diagnostic_commands.yaml`, cưỡng chế bởi `pkg.diagnostics.validator` —
**cùng một hàm** mà gateway và collectors gọi. Trước đây danh sách bị hardcode ở ba
chỗ (frozenset ở đây, bản sao ở `gateway/routes/agent_commands.py`, và collectors
không qua validator nào), hai bản đồng bộ bằng tay và bản thứ ba đã lệch sẵn.

INVARIANT INV_READONLY_CMDS: chỉ lệnh có trong catalogue được chạy. Catalogue load
  LỖI ⇒ từ chối MỌI lệnh (fail-closed), KHÔNG rơi về whitelist cũ.
INVARIANT INV_DIAG_SCOPE_BOUNDED: lệnh `reads_content` đọc được nội dung, nhưng mọi
  đối số trông như đường dẫn phải nằm trong `read_allow` của entry. Đây là bản thay
  thế của INV_NO_DATA_EXFIL: chặn theo TÊN LỆNH khiến Omni không bao giờ đọc được log
  ứng dụng, tức không thể chẩn đoán tầng app.
INVARIANT INV_NO_WRITE: động từ mang nghĩa ghi bị chặn độc lập với catalogue
  (`WRITE_VERBS`), nên một entry khai lỏng cũng không mở được đường mutate.
INVARIANT INV_TRUSTED_BINARY_RESOLUTION: catalogue tra theo basename, nên binary
  thực thi được resolve qua `_resolve_trusted_binary` (PATH sandbox) chứ không dùng
  path do caller đưa — không ai nhét được binary lạ bằng cách đặt tên `/tmp/x/ps`.
INVARIANT INV_NO_ENV_INHERIT: lệnh con chạy với `_sandbox_env()`, không bao giờ kế
  thừa môi trường của agent (nơi có OMNI_AGENT_API_KEY).
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from typing import Any

from pkg.diagnostics.command_normalize import normalize_command
from pkg.diagnostics.validator import validate_command

logger = logging.getLogger(__name__)

_MAX_OUTPUT_BYTES = 8192

# INV_NO_ENV_INHERIT: spawned commands must NOT inherit the agent's environment,
# which holds OMNI_AGENT_API_KEY and other secrets a whitelisted command
# (`ps auxe`, anything reading /proc/self/environ) could otherwise exfiltrate.
# Only a minimal, non-secret set is passed through. PATH is overridable via env
# so operators can widen binary resolution without re-inheriting secrets.
_ENV_SAFE_PATH = "OMNI_AGENT_CMD_PATH"
_DEFAULT_SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_SANDBOX_PASSTHROUGH_KEYS = ("LANG", "LC_ALL", "TZ")


def _sandbox_env() -> dict[str, str]:
    """Minimal environment for a spawned diagnostic command — never inherits
    the agent's secrets. Reads PATH/LANG/... from os.environ at call time."""
    src = os.environ
    env = {"PATH": (src.get(_ENV_SAFE_PATH) or "").strip() or _DEFAULT_SAFE_PATH}
    for key in _SANDBOX_PASSTHROUGH_KEYS:
        val = src.get(key)
        if val:
            env[key] = val
    return env


# Audit finding CRITICAL #1 (2026-07-22, ultrareview follow-up): the whitelist
# check above matched only the basename (command.lstrip("/").split("/")[-1]),
# but the caller-supplied path was what actually executed — `/tmp/x/ps`
# passes the "ps" check and then runs an attacker-controlled binary. Every
# whitelisted command is now resolved ONCE to a real path on the sandboxed
# PATH and that resolved path is what execve()s, regardless of what path the
# caller supplied.
_ENV_SANDBOX_CWD = "OMNI_AGENT_CMD_CWD"
_DEFAULT_SANDBOX_CWD = "/tmp"
_RESOLVED_BINARY_CACHE: dict[str, str] = {}


def _resolve_trusted_binary(base: str) -> str | None:
    """Resolve a whitelisted basename to a real executable on the sandboxed
    PATH, ignoring any path component the caller supplied. Cached per-process
    since the filesystem layout doesn't change mid-run."""
    cached = _RESOLVED_BINARY_CACHE.get(base)
    if cached:
        return cached
    found = shutil.which(base, path=_sandbox_env()["PATH"])
    if found:
        _RESOLVED_BINARY_CACHE[base] = found
    return found


def _sandbox_cwd() -> str:
    return (os.environ.get(_ENV_SANDBOX_CWD) or "").strip() or _DEFAULT_SANDBOX_CWD


def _is_command_allowed(command: str, args: list[str]) -> tuple[bool, str]:
    """Uỷ quyền cho validator dùng chung — giữ tên cũ vì đây là điểm gọi nội bộ đã
    được test bám theo, nhưng chính sách nay đến từ catalogue, không từ file này."""
    return validate_command(command, args)


async def execute_command(
    cmd_id: str,
    command: str,
    args: list[str],
    timeout_s: int = 30,
) -> dict[str, Any]:
    """Execute a whitelisted read-only command. Returns result dict."""
    # Chuẩn hoá TRƯỚC khi validate — hàng rào cuối phía agent. Model 7B nhồi cả
    # dòng lệnh vào args[0] (`ps` ← ["aux --sort=-%cpu"] ⇒ "unsupported option
    # (BSD syntax)") và gọi `top` không cờ (không tty ⇒ rc=1 im lặng). Chuẩn hoá
    # KHÔNG nới guard nào: validate_command chạy sau, trên chính token đã tách.
    command, args = normalize_command(command, args)

    allowed, reason = _is_command_allowed(command, args)
    if not allowed:
        logger.warning(
            "[cmd-exec] BLOCKED cmd_id=%s command=%s reason=%s", cmd_id, command, reason
        )
        return {
            "cmd_id": cmd_id,
            "blocked": True,
            "block_reason": reason,
            "stdout": "",
            "stderr": reason,
            "rc": -1,
            # Cùng một con số, hai tên. Trước đây chỉ có `rc`, nên mọi consumer đọc
            # `exit_code` (UI/script kiểm tra) thấy None trong khi thẻ Telegram in
            # rc=1 — hai đầu mô tả cùng một lần chạy mà không khớp.
            "exit_code": -1,
            "duration_ms": 0,
        }

    base = command.lstrip("/").split("/")[-1]
    resolved = _resolve_trusted_binary(base)
    if resolved is None:
        reason = f"binary_not_found_on_sandbox_path: {base}"
        logger.warning("[cmd-exec] BLOCKED cmd_id=%s command=%s reason=%s", cmd_id, command, reason)
        return {
            "cmd_id": cmd_id,
            "blocked": True,
            "block_reason": reason,
            "stdout": "",
            "stderr": reason,
            "rc": -1,
            "exit_code": -1,
            "duration_ms": 0,
        }

    full_args = [resolved] + args
    logger.info("[cmd-exec] running cmd_id=%s args=%s", cmd_id, full_args[:6])
    t0 = time.monotonic()

    try:
        proc = await asyncio.create_subprocess_exec(
            *full_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_sandbox_env(),
            cwd=_sandbox_cwd(),
        )
        out_bytes, err_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=float(timeout_s)
        )
        rc = proc.returncode or 0
    except asyncio.TimeoutError:
        out_bytes, err_bytes, rc = b"", b"timeout", 1
    except Exception as exc:
        out_bytes, err_bytes, rc = b"", str(exc).encode(), 1

    duration_ms = int((time.monotonic() - t0) * 1000)
    stdout = out_bytes.decode(errors="replace")[:_MAX_OUTPUT_BYTES]
    stderr = err_bytes.decode(errors="replace")[:512]

    logger.info(
        "[cmd-exec] done cmd_id=%s rc=%d duration_ms=%d stdout_len=%d",
        cmd_id, rc, duration_ms, len(stdout),
    )
    return {
        "cmd_id": cmd_id,
        "blocked": False,
        "stdout": stdout,
        "stderr": stderr,
        "rc": rc,
        "exit_code": rc,
        "duration_ms": duration_ms,
    }


async def execute_batch(
    commands: list[dict[str, Any]],
    current_version: str = "unknown",
    api_key: str = "",
) -> list[dict[str, Any]]:
    """Execute a list of command dicts from the command channel poll response.

    UPDATE_AGENT type is routed to updater.handle_update_command().
    All other commands go through the read-only whitelist.
    """
    from remote_agent.updater import handle_update_command

    results = []
    for cmd in commands:
        if cmd.get("type") == "UPDATE_AGENT":
            result = await handle_update_command(
                cmd_id=cmd.get("cmd_id", "unknown"),
                version=str(cmd.get("version", "")),
                download_url=str(cmd.get("download_url", "")),
                sha256_checksum=str(cmd.get("sha256_checksum", "")),
                current_version=current_version,
                api_key=api_key,
            )
        else:
            result = await execute_command(
                cmd_id=cmd.get("cmd_id", "unknown"),
                command=cmd.get("command", ""),
                args=cmd.get("args", []),
                timeout_s=int(cmd.get("timeout_s", 30)),
            )
        results.append(result)
    return results
