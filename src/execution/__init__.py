"""Execution plane — OpenSandbox HTTP client (không subprocess trên omni-worker)."""

from execution.manager import (
    SandboxExecResult,
    SandboxManager,
    auto_cleanup_sandboxes,
    sandbox_result_to_user_text,
)

__all__ = [
    "SandboxExecResult",
    "SandboxManager",
    "auto_cleanup_sandboxes",
    "sandbox_result_to_user_text",
]
