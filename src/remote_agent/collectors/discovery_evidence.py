"""Onboarding discovery probes — process_list/port_scan/service_topology/doc-snapshot.

INVARIANT INV_NO_DATA_EXFIL + INV_DATA_RESIDENCY: only names/ports/paths/metadata
leave this host. The doc-snapshot probe hashes document content IN PLACE (sha256 +
length + mtime) — raw text never enters the envelope, so it never crosses Kafka.

Each probe stamps evidence_source="DiscoveryEvidence" so evidence_consumer.py
routes it straight to the onboarding pipeline without touching K8s-specific
diagnostic logic.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from pathlib import Path
from typing import Any

from remote_agent import exec_guard
from remote_agent.evidence import build_envelope

logger = logging.getLogger(__name__)

_DOC_CANDIDATES = (
    "README.md", "README", "readme.md",
    "openapi.json", "openapi.yaml", "swagger.json",
)
_DOC_MAX_BYTES = 8000


async def _run(cmd: list[str], timeout: float = 10.0) -> tuple[str, int]:
    """Run read-only subprocess. Returns (stdout, returncode). Never raises."""
    # Cùng validator với command channel — collector KHÔNG có đường riêng.
    reason = exec_guard.check(cmd)
    if reason:
        return "", 1
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return out.decode(errors="replace"), proc.returncode or 0
    except asyncio.TimeoutError:
        return "", 1
    except Exception:
        return "", 1


async def collect_process_list(hostname: str) -> dict[str, Any] | None:
    """Running process names + counts (no command-line args, no env)."""
    out, rc = await _run(["ps", "-eo", "comm"], timeout=8.0)
    if rc != 0 or not out.strip():
        return None
    counts: dict[str, int] = {}
    for line in out.splitlines()[1:]:
        name = line.strip()
        if name:
            counts[name] = counts.get(name, 0) + 1
    processes = [{"name": n, "count": c} for n, c in sorted(counts.items(), key=lambda kv: -kv[1])][:100]
    return build_envelope(
        probe="process_list",
        lane="SYS_RESOURCE",
        result="PASSED",
        extracted_fact={"discovery_data": {"processes": processes}},
        symptom_group="onboarding_discovery",
        namespace=hostname,
        evidence_source="DiscoveryEvidence",
        signal_type="DISCOVERY",
    )


async def collect_port_scan(hostname: str) -> dict[str, Any] | None:
    """Listening TCP ports + owning process name (no payload inspection)."""
    out, rc = await _run(["ss", "-tlnp"], timeout=8.0)
    if rc != 0:
        out, rc = await _run(["netstat", "-tlnp"], timeout=8.0)
    if rc != 0 or not out.strip():
        return None
    ports: list[dict[str, Any]] = []
    for line in out.splitlines():
        if "LISTEN" not in line:
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        local = parts[3]
        port = local.rsplit(":", 1)[-1] if ":" in local else ""
        service = ""
        if len(parts) >= 6:
            m = re.search(r'"([^"]+)"', parts[-1])
            if m:
                service = m.group(1)
        if port and port.isdigit():
            ports.append({"port": int(port), "service": service})
    return build_envelope(
        probe="port_scan",
        lane="SYS_RESOURCE",
        result="PASSED",
        extracted_fact={"discovery_data": {"listening_ports": ports[:50]}},
        symptom_group="onboarding_discovery",
        namespace=hostname,
        evidence_source="DiscoveryEvidence",
        signal_type="DISCOVERY",
    )


async def collect_service_topology(hostname: str) -> dict[str, Any] | None:
    """Systemd services (any state) + their real state — coarse topology, no
    config content.

    No --state filter: same anti-pattern already fixed in
    discovery.py::_collect_running_services (2026-07-21) — filtering to
    --state=running made a crashed/failed unit invisible to the onboarding
    topology snapshot exactly when it crashed, which is precisely when the
    System Twin/entity graph most needs to know the service exists. A failed
    unit stays loaded/"in memory" until reset-failed, so dropping the state
    filter (not just widening it) keeps it discoverable.
    """
    out, rc = await _run(
        ["systemctl", "list-units", "--type=service",
         "--no-legend", "--no-pager", "--plain"],
        timeout=10.0,
    )
    if rc != 0 or not out.strip():
        return None
    services: list[dict[str, str]] = []
    for line in out.splitlines():
        parts = line.split(None, 4)
        if not parts:
            continue
        # Columns: UNIT LOAD ACTIVE SUB DESCRIPTION. SUB is the real,
        # granular state (running/exited/failed/dead/...), not a literal
        # hardcoded "running" that was only ever true because of the filter.
        sub_state = parts[3] if len(parts) > 3 else "unknown"
        services.append({
            "name": parts[0].removesuffix(".service"),
            "status": sub_state,
            "description": parts[4].strip()[:120] if len(parts) > 4 else "",
        })
    return build_envelope(
        probe="service_topology",
        lane="SYS_RESOURCE",
        result="PASSED",
        extracted_fact={"discovery_data": {"services": services[:200]}},
        symptom_group="onboarding_discovery",
        namespace=hostname,
        evidence_source="DiscoveryEvidence",
        signal_type="DISCOVERY",
    )


async def collect_connection_scan(hostname: str) -> dict[str, Any] | None:
    """Established TCP connections (remote peer IP:port + local process only).

    INV_NO_DATA_EXFIL: only local_port/remote_ip/remote_port/process name are
    collected — never connection payload or content. Uses ``ss -tnp``
    (established connections; NOT ``-l``, which is listen-only and already
    covered by ``collect_port_scan``). Falls back to ``netstat -tnp``.
    """
    out, rc = await _run(["ss", "-tnp"], timeout=8.0)
    if rc != 0:
        out, rc = await _run(["netstat", "-tnp"], timeout=8.0)
    if rc != 0 or not out.strip():
        return None
    connections: list[dict[str, Any]] = []
    for line in out.splitlines():
        if "ESTAB" not in line:
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        local, remote = parts[3], parts[4]
        local_port = local.rsplit(":", 1)[-1] if ":" in local else ""
        remote_ip = remote.rsplit(":", 1)[0] if ":" in remote else ""
        remote_port = remote.rsplit(":", 1)[-1] if ":" in remote else ""
        process = ""
        if len(parts) >= 6:
            m = re.search(r'"([^"]+)"', parts[-1])
            if m:
                process = m.group(1)
        if remote_ip and remote_port.isdigit() and local_port.isdigit():
            connections.append({
                "local_port": int(local_port),
                "remote_ip": remote_ip,
                "remote_port": int(remote_port),
                "process": process,
            })
    return build_envelope(
        probe="connection_scan",
        lane="SYS_RESOURCE",
        result="PASSED",
        extracted_fact={"discovery_data": {"connections": connections[:100]}},
        symptom_group="onboarding_discovery",
        namespace=hostname,
        evidence_source="DiscoveryEvidence",
        signal_type="DISCOVERY",
    )


async def collect_doc_snapshot(hostname: str, search_dirs: list[str]) -> dict[str, Any] | None:
    """Reference small onboarding documents (README/OpenAPI/sample config) by hash.

    INV_DATA_RESIDENCY: content is read and hashed HERE, on the customer host —
    the envelope carries only path + sha256 + length + mtime, never the text.
    The truncation window (_DOC_MAX_BYTES) matches the legacy server-side hash
    so hashes stay comparable across agent versions.
    """
    found: list[dict[str, Any]] = []
    seen_inodes: set[tuple[int, int]] = set()
    for d in search_dirs:
        base = Path(d)
        if not base.is_dir():
            continue
        for name in _DOC_CANDIDATES:
            path = base / name
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                resolved_base = base.resolve()
                if not path.resolve().is_relative_to(resolved_base):
                    continue
                st = path.stat()
                inode_key = (st.st_dev, st.st_ino)
                if inode_key in seen_inodes:
                    continue
                seen_inodes.add(inode_key)
                content = path.read_text(errors="replace")[:_DOC_MAX_BYTES]
                found.append({
                    "path": str(path),
                    "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    "content_length": len(content),
                    "mtime": int(st.st_mtime),
                })
            except Exception as exc:
                logger.debug("[collector.discovery] doc read failed path=%s err=%s", path, exc)
    if not found:
        return None
    return build_envelope(
        probe="doc_snapshot",
        lane="SYS_RESOURCE",
        result="PASSED",
        extracted_fact={"discovery_data": {"documents": found[:20]}},
        symptom_group="onboarding_discovery",
        namespace=hostname,
        evidence_source="DiscoveryEvidence",
        signal_type="DISCOVERY",
    )
