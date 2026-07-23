"""Command executor for remote agent — enforces METADATA-ONLY whitelist.

INVARIANT INV_READONLY_CMDS: Only commands in COMMAND_WHITELIST are executed.
Shell injection (pipe, redirect, &&, ;, backtick) is blocked unconditionally.
INVARIANT INV_NO_WRITE: Any flag that causes filesystem mutation is blocked.
INVARIANT INV_NO_DATA_EXFIL: Commands that read arbitrary FILE CONTENT are
  blocked unconditionally. Omni inspects metadata (sizes, counts, status,
  process/network/disk state) — it MUST NOT read a single line of VM data
  (cat/grep/tail/head/awk/cut/strings/... are forbidden). System operational
  diagnostics (systemctl status, journalctl, dmesg) remain allowed.
INVARIANT INV_TRUSTED_BINARY_RESOLUTION: whitelist membership is checked by
  basename, so the executable actually run is resolved via _resolve_trusted_binary
  (sandboxed PATH) rather than the caller-supplied path — a caller cannot smuggle
  an arbitrary binary in by naming it e.g. "/tmp/x/ps".
INVARIANT INV_NO_ENV_INHERIT: spawned commands run with _sandbox_env(), never
  the agent's own environment (which holds OMNI_AGENT_API_KEY etc).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
from typing import Any

logger = logging.getLogger(__name__)

_MAX_OUTPUT_BYTES = 8192
_SHELL_INJECTION_RE = re.compile(r'[|;&`$><]|\$\(|\$\{')

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

# mysqladmin is whitelisted for status/metadata only. Real usage in this
# codebase (services.analyst.diagnosis_loop prompt) is exactly `mysqladmin
# status` / `mysqladmin processlist` — no connection flags. That lets us be
# maximally strict rather than trying to fully parse mysqladmin's CLI grammar:
# mysqladmin runs EVERY non-option token it's given as a separate command in
# sequence (`mysqladmin status shutdown` runs both), and options like -h/-u
# can consume the following token as a value — either property defeats a
# "first non-flag token" scanner. So: no flags at all, exactly one arg, and
# that arg must be a read-only verb.
_MYSQLADMIN_READONLY = frozenset({
    "status", "extended-status", "ping", "processlist",
    "version", "variables",
})

# ps BSD "e" flag (bundled, no leading dash) prints the ENVIRONMENT of every
# process — a cross-process secret leak. `--environ` is blocked defensively
# even though procps does not define it. Some procps-ng builds also silently
# reinterpret a dashed cluster as BSD-style (`ps -aux` behaves like `ps aux`,
# with a "bogus '-'" warning) — so a dash prefix must NOT exempt a multi-letter
# cluster from the same check; only the single well-defined POSIX `-e` (select
# all processes, unrelated meaning) stays allowed.
_PS_ENV_LONGFLAGS = frozenset({"--environ", "--environment"})

# Deliberately narrow: only the letters that actually appear in the classic
# `ps auxe`/`ps axe`/`ps auxww e` BSD-mode env-dump idiom (all=a, user=u,
# no-tty=x, wide=w, environment=e). Kept small on purpose so a positional
# value that happens to be all-letters (a username like "eve", a comm name
# like "comm") is never misclassified as a flag cluster — see
# test_ps_safe_invocations_allowed. `-eo`/`-ef` (real usage in
# collectors/discovery_evidence.py) stay safe because 'o'/'f' aren't members.
_PS_BSD_FLAG_LETTERS = frozenset({"a", "u", "x", "w", "e"})

# dpkg: allowlist of read-only query flags. Anything NOT in this set
# (-i/-r/-P/--purge/--remove/--configure/--unpack/--force-*, ...) is refused —
# an allowlist here (vs. blocklist) means a dpkg flag added to some future
# procps/dpkg version defaults to blocked, not silently allowed.
_DPKG_SAFE_FLAGS = frozenset({
    "-l", "-s", "-L", "-p", "-S",
    "--status", "--listfiles", "--print-avail", "--search", "--list",
    "--get-selections",
})

# rpm: only query mode (-q/--query) is allowed; -i/-e/-U/-F (install/erase/
# upgrade/freshen) are blocked in both long and short-cluster form.
_RPM_DESTRUCTIVE_LONGFLAGS = frozenset({
    "--install", "--erase", "--upgrade", "--freshen", "--reinstall",
    "--force", "--nodeps", "--replacepkgs", "--justdb",
})
_RPM_DESTRUCTIVE_SHORTLETTERS = frozenset({"i", "e", "U", "F"})

# ip: object+action tool (`ip route add`, `ip link set eth0 down`, `ip addr
# flush dev eth0`). The action can appear at any positional slot, so block by
# presence anywhere rather than by position.
_IP_MUTATING_SUBCOMMANDS = frozenset({
    "add", "del", "delete", "change", "replace", "set", "flush",
    "append", "prepend",
})


def _first_positional(args: list[str]) -> str | None:
    """First arg that isn't a flag (doesn't start with '-'), or None."""
    for arg in args:
        if not arg.startswith("-"):
            return arg
    return None


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

    if base == "ps":
        for arg in args:
            if arg.lower() in _PS_ENV_LONGFLAGS:
                return False, f"ps_environment_flag_blocked: {arg}"
            if "=" in arg:
                continue
            # Strip at most one leading dash before the letter-cluster check —
            # some procps-ng builds reinterpret a dashed cluster as BSD-style
            # (`-aux` behaves like `aux`), so a dash must not be a free pass.
            # The single well-defined POSIX `-e` (bare, one letter) is the
            # ONLY dash-prefixed exemption: it has an unrelated, unambiguous
            # meaning ("select all") in every ps implementation.
            letters = arg[1:] if arg.startswith("-") else arg
            if arg.startswith("-") and len(letters) == 1:
                continue
            if letters and letters.isalpha() and "e" in letters:
                if all(ch in _PS_BSD_FLAG_LETTERS for ch in letters):
                    return False, f"ps_environment_flag_blocked: {arg}"

    if base == "mysqladmin":
        if any(a.startswith("-") for a in args):
            return False, f"mysqladmin_flags_not_allowed: {' '.join(args)}"
        if len(args) != 1 or args[0].lower() not in _MYSQLADMIN_READONLY:
            return False, f"mysqladmin_subcommand_not_allowed: {args[0] if args else ''}"

    if base == "dpkg":
        for a in args:
            if a.startswith("-") and a not in _DPKG_SAFE_FLAGS:
                return False, f"dpkg_flag_not_allowed: {a}"

    if base == "rpm":
        has_query = False
        for a in args:
            if a in ("-q", "--query"):
                has_query = True
                continue
            if a.lower() in _RPM_DESTRUCTIVE_LONGFLAGS:
                return False, f"rpm_destructive_flag_blocked: {a}"
            if a.startswith("-") and not a.startswith("--"):
                letters = a[1:]
                if "q" in letters:
                    # Query-modified cluster (-qa, -qi, ...) — i/e here mean
                    # info/etc, not install/erase; not an action flag.
                    has_query = True
                    continue
                if any(ch in _RPM_DESTRUCTIVE_SHORTLETTERS for ch in letters):
                    return False, f"rpm_destructive_flag_blocked: {a}"
        if not has_query:
            return False, "rpm_query_mode_required"

    if base == "ip":
        for a in args:
            if not a.startswith("-") and a.lower() in _IP_MUTATING_SUBCOMMANDS:
                return False, f"ip_mutating_subcommand_blocked: {a}"

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
