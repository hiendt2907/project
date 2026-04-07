"""Sandbox command policy — strict denylist + promotion tool allowlist (SDK-only cluster writes)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class PolicyVerdict(str, Enum):
    ALLOWED_AUTO = "allowed_auto"
    REQUIRES_TELEGRAM_APPROVE = "requires_telegram_approve"
    DENIED = "denied"


@dataclass(frozen=True)
class PolicyResult:
    verdict: PolicyVerdict
    reason: str = ""


# Tools that gated promotion may invoke after sandbox + validation (SDK / kubectl — không shell tự do).
PROMOTION_TOOL_ALLOWLIST: frozenset[str] = frozenset({"k8s_rollout_restart"})

# Khi OMNI_CLUSTER_FULL_ACCESS: promotion được gọi trực tiếp các tool K8s sau (sau sandbox + validation JSON).
PROMOTION_CLUSTER_TOOLS: frozenset[str] = frozenset(
    {
        "k8s_rollout_restart",
        "k8s_scale_deployment",
        "k8s_patch_resource",
        "kubectl_cluster",
    }
)


def normalize_command(command: str) -> str:
    return " ".join((command or "").split())


# Strict denylist — case-insensitive on normalized command.
_STRICT_DENY_COMPILED: tuple[re.Pattern[str], ...] = (
    re.compile(r"\brm\b[^\n;|&]*?-\s*rf\b", re.IGNORECASE | re.DOTALL),
    re.compile(r"\brm\b[^\n;|&]*?-\s*fr\b", re.IGNORECASE | re.DOTALL),
    re.compile(r"\brm\b[^\n;|&]*?--no-preserve-root\b", re.IGNORECASE | re.DOTALL),
    re.compile(r"\bmkfs\b", re.IGNORECASE),
    # `dd` as a command (avoid matching "dd" inside unrelated tokens)
    re.compile(r"(?:^|[\s;&|()])\bdd\s+", re.IGNORECASE),
    re.compile(r">\s*/dev/sd[a-z]+\b", re.IGNORECASE),
    re.compile(r">\s*/dev/nvme\d*n?\d*\b", re.IGNORECASE),
    re.compile(r">\s*/dev/vd[a-z]+\b", re.IGNORECASE),
)


def check_sandbox_command(command: str, *, lab_unchained: bool = False, env_mode: str = "prod") -> PolicyResult:
    """
    Cửa ngõ trước mọi sandbox exec: chặn lệnh phá hoại cấp thấp.
    Khớp denylist → DENIED; ngược lại ALLOWED_AUTO (chỉ nghĩa là được thử trong sandbox).
    LAB (OMNI_LAB_UNCHAINED): bỏ denylist — vẫn chặn lệnh rỗng.
    """
    cmd = normalize_command(command)
    if not cmd:
        return PolicyResult(PolicyVerdict.DENIED, reason="empty_command")
    if str(env_mode).strip().lower() == "dev":
        return PolicyResult(PolicyVerdict.ALLOWED_AUTO, reason="env_mode_dev")
    if lab_unchained:
        return PolicyResult(PolicyVerdict.ALLOWED_AUTO, reason="lab_unchained")
    for pat in _STRICT_DENY_COMPILED:
        if pat.search(cmd):
            return PolicyResult(PolicyVerdict.DENIED, reason=f"strict_denylist:{pat.pattern[:48]}")
    return PolicyResult(PolicyVerdict.ALLOWED_AUTO, reason="sandbox_ok")


def check_promotion_tool(
    intended_tool: str | None,
    *,
    lab_unchained: bool = False,
    cluster_full_access: bool = False,
    env_mode: str = "prod",
) -> PolicyResult:
    """Chỉ tool trong allowlist (hoặc cluster toolkit khi full access / lab) được gọi sau gated pipeline."""
    name = (intended_tool or "").strip()
    if not name:
        return PolicyResult(PolicyVerdict.DENIED, reason="missing_intended_tool")
    if str(env_mode).strip().lower() == "dev":
        return PolicyResult(PolicyVerdict.ALLOWED_AUTO, reason="env_mode_dev")
    if lab_unchained:
        return PolicyResult(PolicyVerdict.ALLOWED_AUTO, reason="lab_unchained")
    if cluster_full_access and name in PROMOTION_CLUSTER_TOOLS:
        return PolicyResult(PolicyVerdict.ALLOWED_AUTO, reason="cluster_full_access")
    if name not in PROMOTION_TOOL_ALLOWLIST:
        return PolicyResult(PolicyVerdict.DENIED, reason=f"tool_not_allowlisted:{name}")
    return PolicyResult(PolicyVerdict.ALLOWED_AUTO, reason="promotion_ok")
