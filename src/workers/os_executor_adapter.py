"""Shadow OS executor adapter for host-level command wrapping."""

from __future__ import annotations

import hashlib
import json
import shlex
from dataclasses import dataclass


NSENTER_FLAGS = ("-t", "1", "-m", "-u", "-i", "-n", "-p", "--")


@dataclass(frozen=True)
class WrappedCommand:
    command: str
    command_hash: str
    nsenter_flags: tuple[str, ...]


def wrap_host_command(command: str) -> WrappedCommand:
    """Wrap a linux command to run in host PID=1 namespaces via nsenter."""
    raw = (command or "").strip()
    if not raw:
        raise ValueError("command is required")
    wrapped = "nsenter " + " ".join(NSENTER_FLAGS) + " " + raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return WrappedCommand(command=wrapped, command_hash=digest, nsenter_flags=NSENTER_FLAGS)


def command_feedback_digest(payload: dict[str, object]) -> str:
    """Small stable digest for audit transition metadata."""
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def shell_escape(command: str) -> str:
    """Escape command for local shell runner wrappers."""
    return shlex.quote((command or "").strip())
