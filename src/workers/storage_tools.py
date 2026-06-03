"""Diagnostic tools — storage domain (disk, NFS, I/O).

Tools (read-only, no mutations):
  tool_disk_health    — Partition usage + inode check
  tool_nfs_health     — NFS mount reachability + stale handle detection

All async; registered in tools.py; all go into READ_ONLY_FAST_PATH_TOOLS.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

_DISK_WARN_PCT = 85
_DISK_CRITICAL_PCT = 95
_INODE_WARN_PCT = 90


async def _run_cmd(cmd: list[str], timeout: float = 10.0) -> tuple[str, str, int]:
    """Run subprocess. Never raises; returns (stdout, stderr, rc)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return out.decode(errors="replace"), err.decode(errors="replace"), proc.returncode or 0
    except asyncio.TimeoutError:
        return "", "timeout", 1
    except Exception as exc:
        return "", str(exc), 1


async def tool_disk_health(ctx: Any, args: dict[str, Any]) -> str:
    """
    Check disk partition usage and inode exhaustion (read-only).
    Highlights partitions above warn/critical thresholds.

    args:
      warn_pct     — warn threshold % (default: 85)
      critical_pct — critical threshold % (default: 95)
      include_all  — include pseudo-filesystems (default: false)
    """
    warn_pct = int(args.get("warn_pct") or _DISK_WARN_PCT)
    critical_pct = int(args.get("critical_pct") or _DISK_CRITICAL_PCT)

    lines = ["=== disk_health ==="]

    # Block usage
    out_df, err_df, rc_df = await _run_cmd(["df", "-hT"])
    if rc_df != 0:
        return f"[disk_health] ERROR: df failed — {err_df[:200]}"

    lines.append("=== block usage (df -hT) ===")
    critical_found: list[str] = []
    warn_found: list[str] = []

    for line in out_df.splitlines():
        parts = line.split()
        if len(parts) < 6 or parts[0] == "Filesystem":
            lines.append(line)
            continue
        # Skip pseudo-fs unless include_all
        fstype = parts[1] if len(parts) >= 7 else ""
        if not args.get("include_all") and fstype in ("tmpfs", "devtmpfs", "squashfs", "overlay"):
            continue

        pct_str = parts[-2] if parts[-2].endswith("%") else ""
        try:
            pct = int(pct_str.rstrip("%"))
            if pct >= critical_pct:
                lines.append(f"  !! CRITICAL {line}")
                critical_found.append(line.split()[-1])
            elif pct >= warn_pct:
                lines.append(f"  !  WARN     {line}")
                warn_found.append(line.split()[-1])
            else:
                lines.append(f"     OK       {line}")
        except ValueError:
            lines.append(line)

    # Inode usage
    out_inode, _, rc_inode = await _run_cmd(["df", "-ih"])
    if rc_inode == 0:
        lines.append("=== inode usage (df -ih) ===")
        for line in out_inode.splitlines():
            if "%" in line:
                pct_str = [p for p in line.split() if p.endswith("%")]
                if pct_str:
                    try:
                        pct = int(pct_str[0].rstrip("%"))
                        prefix = "  !! " if pct >= _INODE_WARN_PCT else "     "
                        lines.append(f"{prefix}{line}")
                        if pct >= _INODE_WARN_PCT:
                            critical_found.append(f"inode:{line.split()[-1]}")
                    except ValueError:
                        lines.append(f"     {line}")
                else:
                    lines.append(f"     {line}")
            else:
                lines.append(f"     {line}")

    # Summary
    lines.append("")
    if critical_found:
        lines.append(f"CRITICAL: {critical_found}")
    if warn_found:
        lines.append(f"WARN: {warn_found}")
    if not critical_found and not warn_found:
        lines.append("All partitions within healthy thresholds.")

    return "\n".join(lines)


async def tool_nfs_health(ctx: Any, args: dict[str, Any]) -> str:
    """
    Check NFS mounts for reachability, stale handles, and I/O errors (read-only).

    args:
      timeout_per_mount — stat timeout per NFS mountpoint in seconds (default: 4)
    """
    timeout_pm = float(args.get("timeout_per_mount") or 4.0)

    lines = ["=== nfs_health ==="]

    out_mounts, _, rc = await _run_cmd(["cat", "/proc/mounts"])
    if rc != 0:
        return "[nfs_health] ERROR: cannot read /proc/mounts"

    nfs_entries: list[tuple[str, str]] = []
    for line in out_mounts.splitlines():
        parts = line.split()
        if len(parts) >= 3 and "nfs" in parts[2].lower():
            nfs_entries.append((parts[0], parts[1]))

    if not nfs_entries:
        return "=== nfs_health ===\nNo NFS mounts found."

    lines.append(f"Found {len(nfs_entries)} NFS mount(s)")

    stale_mounts: list[str] = []
    io_error_mounts: list[str] = []

    for dev, mp in nfs_entries:
        out_stat, err_stat, rc_stat = await _run_cmd(["stat", "--file-system", mp], timeout=timeout_pm)
        err_lower = err_stat.lower()
        if "stale" in err_lower:
            status = "STALE"
            stale_mounts.append(mp)
        elif any(kw in err_lower for kw in ("i/o error", "input/output", "transport endpoint", "connection timed out")):
            status = "IO_ERROR"
            io_error_mounts.append(mp)
        elif rc_stat != 0:
            status = f"ERROR({rc_stat})"
            io_error_mounts.append(mp)
        else:
            # Parse stat output for key metrics
            fs_type = next((l.split(":")[-1].strip() for l in out_stat.splitlines() if "Type:" in l), "?")
            avail_b = next((l.split()[-1] for l in out_stat.splitlines() if "Available:" in l), "?")
            status = f"OK fstype={fs_type} avail={avail_b}B"

        lines.append(f"  {dev} → {mp}: {status}")

    # Check dmesg for NFS errors
    out_dmesg, _, _ = await _run_cmd(["dmesg", "-T", "--level=err,crit,warn"], timeout=5.0)
    nfs_errors = [
        l.strip() for l in out_dmesg.splitlines()[-100:]
        if "nfs" in l.lower() and any(k in l.lower() for k in ("error", "stale", "timeout", "failed", "lost"))
    ]
    if nfs_errors:
        lines.append(f"=== recent dmesg NFS errors ({len(nfs_errors)}) ===")
        lines.extend(nfs_errors[-10:])

    lines.append("")
    if stale_mounts:
        lines.append(f"STALE MOUNTS: {stale_mounts}")
    if io_error_mounts:
        lines.append(f"IO ERROR MOUNTS: {io_error_mounts}")
    if not stale_mounts and not io_error_mounts:
        lines.append("All NFS mounts accessible.")

    return "\n".join(lines)
