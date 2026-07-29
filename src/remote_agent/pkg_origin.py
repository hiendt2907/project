"""Package-manager based origin classification for a systemd unit file.

Whether a unit is "the customer's own application" vs "base OS package" is
determined by asking the real package manager (dpkg or rpm) whether it owns
the unit's FragmentPath — never a hardcoded service-name list, since a
customer's own service names can't be known in advance.
"""
from __future__ import annotations

import asyncio
import shutil

from remote_agent import exec_guard

ORIGIN_CUSTOM = "custom"
ORIGIN_UNKNOWN = "unknown"

_DPKG = shutil.which("dpkg")
_RPM = shutil.which("rpm")


async def _run(cmd: list[str], timeout: float = 5.0) -> tuple[str, str, int]:
    # Cùng validator với command channel — collector KHÔNG có đường riêng.
    reason = exec_guard.check(cmd)
    if reason:
        return "", f"blocked: {reason}", 1
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return out.decode(errors="replace"), err.decode(errors="replace"), proc.returncode or 0
    except asyncio.TimeoutError:
        return "", "timeout", 1
    except Exception as exc:
        return "", str(exc), 1


async def get_fragment_path(unit_full: str) -> str:
    out, _, rc = await _run(["systemctl", "show", unit_full, "-p", "FragmentPath", "--value"])
    return out.strip() if rc == 0 else ""


async def classify_unit_origin(fragment_path: str) -> str:
    """"package:<name>" if a distro package owns the file, "custom" if no
    package claims it, "unknown" if no package manager is available."""
    if not fragment_path:
        return ORIGIN_UNKNOWN
    if _DPKG:
        out, _, rc = await _run([_DPKG, "-S", fragment_path])
        if rc == 0 and ":" in out:
            pkg = out.split(":", 1)[0].strip()
            if pkg:
                return f"package:{pkg}"
        return ORIGIN_CUSTOM
    if _RPM:
        out, _, rc = await _run([_RPM, "-qf", fragment_path])
        if rc == 0 and out.strip():
            return f"package:{out.strip()}"
        return ORIGIN_CUSTOM
    return ORIGIN_UNKNOWN
