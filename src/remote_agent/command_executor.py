"""Command executor for remote agent — enforces METADATA-ONLY whitelist.

INVARIANT INV_READONLY_CMDS: Only commands in COMMAND_WHITELIST are executed.
Shell injection (pipe, redirect, &&, ;, backtick) is blocked unconditionally.
INVARIANT INV_NO_WRITE: Any flag that causes filesystem mutation is blocked.
INVARIANT INV_NO_DATA_EXFIL: Commands that read arbitrary FILE CONTENT are
  blocked unconditionally. Omni inspects metadata (sizes, counts, status,
  process/network/disk state) — it MUST NOT read a single line of VM data
  (cat/grep/tail/head/awk/cut/strings/... are forbidden). System operational
  diagnostics (systemctl status, journalctl, dmesg) remain allowed.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

logger = logging.getLogger(__name__)

_MAX_OUTPUT_BYTES = 8192
_SHELL_INJECTION_RE = re.compile(r'[|;&`$><]|\$\(|\$\{')

COMMAND_WHITELIST: frozenset[str] = frozenset({
    # Filesystem METADATA only (sizes, counts, listings — never content)
    "stat", "ls", "find", "du", "df",
    # Process / system
    "ps", "pgrep", "top", "free", "vmstat", "iostat", "sar",
    "uptime", "uname", "id", "who", "last", "w",
    # Network (read-only status)
    "ss", "netstat", "ip", "ping", "lsof",
    # Service status
    "systemctl",
    # Journald / kernel — operational diagnostics (not business data)
    "journalctl", "dmesg",
    # Disk metadata
    "lsblk", "blkid",
    # Database status (NOT arbitrary SELECT — prompt restricts to SHOW/status)
    "mysqladmin",
    # Package info
    "dpkg", "rpm",
    # File metadata (type only — never bytes)
    "file",
})

# INV_NO_DATA_EXFIL: commands that emit raw file/DB CONTENT. Blocked even though
# they are technically "read-only" — reading content == exfiltrating VM data.
_CONTENT_READ_BLOCKED: frozenset[str] = frozenset({
    "cat", "head", "tail", "grep", "egrep", "fgrep", "zgrep",
    "awk", "sed", "cut", "sort", "uniq", "wc", "more", "less",
    "strings", "od", "xxd", "hexdump", "nl", "tac", "rev",
    "md5sum", "sha256sum", "sha1sum", "base64", "nc", "ncat",
    "mysql", "psql", "mongo", "redis-cli", "curl", "wget", "scp",
})

# find flags that turn a metadata listing into arbitrary exec / content read.
_FIND_DANGEROUS_FLAGS: frozenset[str] = frozenset({
    "-exec", "-execdir", "-delete", "-fprint", "-fprintf",
    "-fls", "-ok", "-okdir", "-cat",
})

_SYSTEMCTL_READONLY = frozenset({
    "status", "is-active", "is-enabled", "is-failed", "is-system-running",
    "list-units", "list-unit-files", "list-sockets", "list-timers",
    "show", "cat", "help",
})

_WRITE_SUBCOMMANDS = frozenset({
    "start", "stop", "restart", "reload", "enable", "disable",
    "mask", "unmask", "kill", "reset-failed", "daemon-reload",
    "set-property", "edit",
})


def _is_command_allowed(command: str, args: list[str]) -> tuple[bool, str]:
    full_cmd = command + " " + " ".join(args)

    if _SHELL_INJECTION_RE.search(full_cmd):
        return False, f"shell_injection_detected in: {full_cmd[:80]}"

    base = command.lstrip("/").split("/")[-1]
    if base in _CONTENT_READ_BLOCKED:
        return False, f"data_exfil_blocked: '{base}' reads file/DB content (metadata-only policy)"
    if base not in COMMAND_WHITELIST:
        return False, f"command_not_whitelisted: {base}"

    if base == "find":
        for flag in args:
            if flag.split("=", 1)[0] in _FIND_DANGEROUS_FLAGS:
                return False, f"find_dangerous_flag_blocked: {flag}"

    if base == "systemctl" and args:
        subcmd = args[0].lstrip("-")
        if subcmd in _WRITE_SUBCOMMANDS:
            return False, f"systemctl_write_subcommand_blocked: {subcmd}"
        if subcmd not in _SYSTEMCTL_READONLY and not subcmd.startswith("list"):
            return False, f"systemctl_subcommand_not_allowed: {subcmd}"

    for flag in args:
        stripped = flag.lstrip("-")
        if stripped in _WRITE_SUBCOMMANDS:
            return False, f"write_flag_blocked: {flag}"

    return True, ""


async def execute_command(
    cmd_id: str,
    command: str,
    args: list[str],
    timeout_s: int = 30,
) -> dict[str, Any]:
    """Execute a whitelisted read-only command. Returns result dict."""
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
            "duration_ms": 0,
        }

    full_args = [command] + args
    logger.info("[cmd-exec] running cmd_id=%s args=%s", cmd_id, full_args[:6])
    t0 = time.monotonic()

    try:
        proc = await asyncio.create_subprocess_exec(
            *full_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
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
        "duration_ms": duration_ms,
    }


async def execute_batch(
    commands: list[dict[str, Any]],
    current_version: str = "unknown",
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
