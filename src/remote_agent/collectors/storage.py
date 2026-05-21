"""Remote agent collector — storage health (disk partitions, NFS mounts).

Probes:
  disk_usage      → DOMAIN_STORAGE  lane=SYS_RESOURCE / SYS_HARD_FAIL
  storage_nfs     → DOMAIN_STORAGE  lane=SYS_HARD_FAIL

All commands are read-only; no mutations.
Uses asyncio.create_subprocess_exec — no blocking subprocess.run().
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from remote_agent.evidence import build_envelope

logger = logging.getLogger(__name__)

_DISK_CRITICAL_PCT = 95
_DISK_WARN_PCT = 90
_INODE_CRITICAL_PCT = 95
_NFS_STALE_ERROR_KEYWORDS = ("stale", "i/o error", "input/output", "transport endpoint")


async def _run(cmd: list[str], timeout: float = 10.0) -> tuple[str, str, int]:
    """Run subprocess. Never raises."""
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


async def collect_disk_usage(hostname: str) -> dict[str, Any] | None:
    """Collect disk partition usage via df (read-only)."""
    # --output is Linux util-linux only; fallback to POSIX df on macOS / older systems
    out, err, rc = await _run(["df", "-h", "--output=source,fstype,size,used,avail,pcent,target"])
    use_posix_df = rc != 0 and not out.strip()
    if use_posix_df:
        out, err, rc = await _run(["df", "-h"])
    if rc != 0 and not out.strip():
        logger.warning("[collector.storage] df failed: %s", err[:200])
        return None
    if rc != 0:
        logger.warning("[collector.storage] df partial failure (some mounts inaccessible): %s", err[:100])

    partitions: list[dict[str, Any]] = []
    critical_partitions: list[str] = []
    warn_partitions: list[str] = []
    nfs_mounts: list[str] = []

    for line in out.splitlines():
        parts = line.split()
        if not parts or parts[0] in ("Filesystem", "Source"):
            continue
        # POSIX df: Filesystem Size Used Avail Use% Mounted (6 cols, no fstype)
        # GNU df --output: source fstype size used avail pcent target (7 cols)
        if use_posix_df:
            if len(parts) < 6:
                continue
            source = parts[0]
            size, used, avail, pct_str, mount = parts[1], parts[2], parts[3], parts[4], parts[5]
            fstype = "unknown"
        else:
            if len(parts) < 7:
                continue
            source, fstype, size, used, avail, pct_str, mount = parts[:7]

        # Skip pseudo-filesystems
        if fstype in ("tmpfs", "devtmpfs", "squashfs", "overlay", "proc", "sysfs", "cgroup"):
            continue

        pct = 0
        try:
            pct = int(pct_str.rstrip("%"))
        except ValueError:
            pass

        entry: dict[str, Any] = {
            "source": source, "fstype": fstype, "size": size,
            "used": used, "avail": avail, "pct": pct, "mount": mount,
        }
        partitions.append(entry)

        if "nfs" in fstype.lower():
            nfs_mounts.append(mount)

        if pct >= _DISK_CRITICAL_PCT:
            critical_partitions.append(f"{mount}({pct}%)")
        elif pct >= _DISK_WARN_PCT:
            warn_partitions.append(f"{mount}({pct}%)")

    # Inode check
    out_i, _, rc_i = await _run(["df", "-i", "--output=source,ipcent,target"])
    inode_critical: list[str] = []
    if rc_i == 0:
        for line in out_i.splitlines():
            parts = line.split()
            if len(parts) < 3 or parts[0] in ("Filesystem", "Source") or parts[1] == "-":
                continue
            try:
                ipct = int(parts[1].rstrip("%"))
                if ipct >= _INODE_CRITICAL_PCT:
                    inode_critical.append(f"{parts[2]}(inode {ipct}%)")
            except ValueError:
                pass

    anomalies = critical_partitions + inode_critical
    warnings = warn_partitions

    fact: dict[str, Any] = {
        "partitions": partitions,
        "critical_partitions": critical_partitions,
        "warn_partitions": warn_partitions,
        "inode_critical": inode_critical,
        "nfs_mounts": nfs_mounts,
        "disk_critical_count": len(critical_partitions),
        "disk_warn_count": len(warn_partitions),
    }

    result = "FAILED" if anomalies else ("WARN" if warnings else "PASSED")
    parts_summary = ", ".join(anomalies[:5]) if anomalies else (", ".join(warnings[:3]) if warnings else "all partitions OK")
    hint = f"[{hostname}] disk: {parts_summary}"

    return build_envelope(
        probe="disk_usage",
        lane="SYS_HARD_FAIL" if anomalies else "SYS_RESOURCE",
        result=result,
        extracted_fact=fact,
        alert_rule="DiskCritical" if critical_partitions else ("DiskWarning" if warn_partitions else "DiskHealthy"),
        alert_hint=hint,
        symptom_group="storage_state",
        namespace=hostname,
    )


async def collect_nfs_health(hostname: str) -> dict[str, Any] | None:
    """Check NFS mounts for stale handles and I/O errors (read-only)."""
    # Read /proc/mounts to enumerate NFS entries
    out_mounts, _, rc_m = await _run(["cat", "/proc/mounts"])
    if rc_m != 0:
        return None

    nfs_entries: list[dict[str, Any]] = []
    for line in out_mounts.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        dev, mountpoint, fstype = parts[0], parts[1], parts[2]
        if "nfs" not in fstype.lower():
            continue
        nfs_entries.append({"dev": dev, "mount": mountpoint, "fstype": fstype})

    if not nfs_entries:
        return None

    # Test each NFS mount with a stat call (2s timeout per mount)
    stale_mounts: list[str] = []
    io_error_mounts: list[str] = []

    for entry in nfs_entries:
        mp = entry["mount"]
        out_stat, err_stat, rc_stat = await _run(["stat", "--file-system", mp], timeout=4.0)
        err_lower = err_stat.lower()
        if any(kw in err_lower for kw in _NFS_STALE_ERROR_KEYWORDS):
            if "stale" in err_lower:
                stale_mounts.append(mp)
            else:
                io_error_mounts.append(mp)
        elif rc_stat != 0:
            io_error_mounts.append(mp)

    # Also check dmesg for recent NFS errors
    out_dmesg, _, _ = await _run(["dmesg", "-T", "--level=err,crit", "--notime"])
    nfs_dmesg_errors: list[str] = []
    for line in out_dmesg.splitlines()[-50:]:
        if "nfs" in line.lower() and any(kw in line.lower() for kw in ("error", "stale", "timeout", "failed")):
            nfs_dmesg_errors.append(line.strip()[:120])

    anomalies = stale_mounts + io_error_mounts
    fact: dict[str, Any] = {
        "nfs_mounts_total": len(nfs_entries),
        "nfs_mounts": [e["mount"] for e in nfs_entries],
        "stale_mounts": stale_mounts,
        "io_error_mounts": io_error_mounts,
        "nfs_dmesg_errors": nfs_dmesg_errors[:5],
        "nfs_error_count": len(anomalies),
    }

    result = "FAILED" if anomalies else "PASSED"
    hint = (
        f"[{hostname}] NFS: stale={stale_mounts}, io_error={io_error_mounts}"
        if anomalies else
        f"[{hostname}] NFS: {len(nfs_entries)} mounts healthy"
    )

    return build_envelope(
        probe="storage_nfs",
        lane="SYS_HARD_FAIL" if anomalies else "SYS_RESOURCE",
        result=result,
        extracted_fact=fact,
        alert_rule="NFSStaleMount" if stale_mounts else ("NFSIOError" if io_error_mounts else "NFSHealthy"),
        alert_hint=hint,
        symptom_group="storage_state",
        namespace=hostname,
    )
