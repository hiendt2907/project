"""Canonical agent-bundle hash — drift detection (Sprint NV-SRE IT-2).

The SAME algorithm runs in two places so the results are comparable:
- on the agent at startup (self-hash of the installed package dir), reported
  in every /webhook/agent/register heartbeat;
- in ``scripts/publish_agent_release.py`` over ``src/remote_agent`` in the
  repo, producing the expected release manifest the gateway compares against.

Only source files that define behaviour are hashed (*.py + VERSION); bytecode
caches and editor droppings are excluded so a hash mismatch always means the
shipped code differs, never that the VM merely ran the code.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

_INCLUDE_SUFFIXES = frozenset({".py"})
_INCLUDE_NAMES = frozenset({"VERSION"})


def compute_bundle_hash(package_root: Path) -> str:
    """sha256 over sorted (relative-posix-path, content) pairs under package_root."""
    digest = hashlib.sha256()
    files = sorted(
        p
        for p in package_root.rglob("*")
        if p.is_file()
        and "__pycache__" not in p.parts
        and (p.suffix in _INCLUDE_SUFFIXES or p.name in _INCLUDE_NAMES)
    )
    for path in files:
        rel = path.relative_to(package_root).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def self_bundle_hash() -> str:
    """Hash of the remote_agent package this process is actually running."""
    return compute_bundle_hash(Path(__file__).resolve().parent)
