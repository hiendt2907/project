"""Runtime mode bootstrap cho AOIP Agent — fail-closed, KHÔNG silent no-op.

Vì sao tồn tại: ``daemon.main()`` (CLI/systemd entrypoint) trước đây không tiêm
``redis``/``transport``/``audit_log``/``gate`` nên LUÔN rơi về ``_noop_executor`` mà
không có tín hiệu nào cho operator biết mutation không hoạt động. Module này buộc
operator chọn RÕ một trong hai mode; ``MUTATION_ENABLED`` thiếu bất kỳ dependency nào
phải làm startup THẤT BẠI (raise), KHÔNG bao giờ tự động rơi về observe-only.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Awaitable, Callable

from aoip import audit
from aoip.agent.operations import build_recovery_executor
from aoip.recovery import RecoveryGate
from aoip.transport import LocalTransport, SSHTransport

logger = logging.getLogger(__name__)

_ENV_ALLOW_SELF_RESTART = "AOIP_ALLOW_SELF_RESTART"

MODE_OBSERVE_ONLY = "observe_only"
MODE_MUTATION_ENABLED = "mutation_enabled"
_VALID_MODES = (MODE_OBSERVE_ONLY, MODE_MUTATION_ENABLED)

STATUS_ACTIVE = "ACTIVE"
STATUS_DISABLED = "DISABLED"

Executor = Callable[[dict], Awaitable[tuple[str, dict]]]


class AgentBootstrapError(RuntimeError):
    """Startup fail-closed: thiếu/lỗi dependency cho mode đã chọn. KHÔNG silent fallback."""


@dataclass(frozen=True)
class RuntimeStatus:
    executor_mode: str      # OBSERVE_ONLY | MUTATION_ENABLED
    executor_status: str    # ACTIVE | DISABLED

    def render(self) -> str:
        return f"executor_mode={self.executor_mode} executor_status={self.executor_status}"


async def observe_only_executor(payload: dict) -> tuple[str, dict]:
    """Explicit no-mutation executor cho OBSERVE_ONLY. KHÔNG bao giờ COMPLETED mutating command."""
    return "ESCALATED", {"rc": 1, "reason": "executor_disabled_observe_only",
                         "verb": payload.get("verb", ""), "evidence": []}


def _require(env: dict, key: str) -> str:
    value = env.get(key, "").strip()
    if not value:
        raise AgentBootstrapError(
            f"mutation_enabled mode requires {key} — not set or empty")
    return value


def self_unit_names(env: dict) -> frozenset[str]:
    """Systemd unit(s) that ARE this agent — never a legal mutation target.

    Derived from config, not hardcoded to one string, because the unit has been
    renamed once already (``omni-remote-agent.service`` -> ``aoip-agent.service``)
    and a stale hardcoded name is a silent hole. ``OMNI_AGENT_SYSTEMD_SERVICE`` is
    the key run.env already carries; ``AOIP_AGENT_SERVICE_NAME`` is the key
    ``capabilities.systemd_restart.SystemdRestartPolicy`` reads. Both are honoured
    plus the two historical names, so the guard holds under either convention.
    """
    names: set[str] = {"aoip-agent.service", "omni-remote-agent.service"}
    for key in ("AOIP_AGENT_SERVICE_NAME", "OMNI_AGENT_SYSTEMD_SERVICE"):
        raw = env.get(key, "").strip()
        if raw:
            names.add(raw if raw.endswith(".service") else f"{raw}.service")
    return frozenset(names)


def _strip_self_units(units: frozenset[str], env: dict) -> frozenset[str]:
    """Remove the agent's own unit from an allowlist unless EXPLICITLY unlocked.

    Why this exists as code and not as an operator convention: the live daemon's
    executor (``operations.build_recovery_executor`` -> ``recovery._gate_checks``
    -> ``target_allowlisted``) checks ONLY ``RecoveryGate.allowed_targets``. It
    never consults ``AOIP_ALLOW_SELF_RESTART`` — that flag is read exclusively by
    ``capabilities.systemd_restart.SystemdRestartPolicy``, which this path does
    not call. So before this guard, the single thing standing between Omni and
    "restart the agent that is executing this command" was a human remembering
    not to type it into ``AOIP_ALLOWED_SYSTEMD_UNITS``.

    Self-restart is an observability-loss loop, not a normal recovery: the agent
    dies mid-execution -> telemetry stops -> Omni sees the host go dark -> orders
    another restart. Removing the target here (rather than raising) keeps the
    rest of a mixed allowlist working, and is logged loudly by the caller.
    """
    if env.get(_ENV_ALLOW_SELF_RESTART, "false").strip().lower() == "true":
        return units
    return frozenset(units - self_unit_names(env))


def _build_gate(env: dict) -> RecoveryGate:
    modes = _require(env, "AOIP_GATE_ALLOWED_FAILURE_MODES")
    substrates = _require(env, "AOIP_GATE_ALLOWED_SUBSTRATES")
    scope_prefix = _require(env, "AOIP_GATE_SCOPE_PREFIX")
    try:
        max_risk = float(_require(env, "AOIP_GATE_MAX_RISK"))
        min_conf = float(_require(env, "AOIP_GATE_MIN_DIAGNOSIS_CONFIDENCE"))
        max_age_s = float(_require(env, "AOIP_GATE_MAX_DIAGNOSIS_AGE_S"))
    except ValueError as exc:
        raise AgentBootstrapError(f"invalid numeric gate config: {exc}") from exc
    # ADR-005: same env var + same fail-closed convention as
    # capabilities.systemd_restart.SystemdRestartPolicy.load_policy_from_env —
    # missing/empty → allowlist RỖNG (KHÔNG restart gì), never permit-all.
    # Required (not _require-optional) so MUTATION_ENABLED can't silently
    # start with an unrestricted target gate.
    allowed_units = _require(env, "AOIP_ALLOWED_SYSTEMD_UNITS")
    requested = frozenset(u.strip() for u in allowed_units.split(",") if u.strip())
    targets = _strip_self_units(requested, env)
    if targets != requested:
        logger.warning(
            "event=self_restart_target_stripped removed=%s reason=agent_own_unit "
            "(set %s=true to override — creates an observability-loss loop)",
            sorted(requested - targets), _ENV_ALLOW_SELF_RESTART,
        )
    return RecoveryGate(
        allowed_failure_modes=frozenset(m.strip() for m in modes.split(",") if m.strip()),
        allowed_substrates=frozenset(s.strip() for s in substrates.split(",") if s.strip()),
        max_risk=max_risk, scope_prefix=scope_prefix,
        min_diagnosis_confidence=min_conf, max_diagnosis_age_s=max_age_s,
        allowed_targets=targets)


def _build_transport(env: dict):
    ssh_host = env.get("AOIP_RECOVERY_SSH_HOST", "").strip()
    if ssh_host:
        return SSHTransport(ssh_host, user=env.get("AOIP_RECOVERY_SSH_USER", "").strip() or None)
    return LocalTransport(target=env.get("AOIP_RECOVERY_TARGET", "").strip() or "localhost")


def build_agent_runtime(*, mode: str, agent_id: str,
                        env: dict | None = None) -> tuple[Executor, RuntimeStatus]:
    """Chọn executor theo mode đã chọn RÕ RÀNG. KHÔNG infer mode từ lỗi dependency.

    ``MUTATION_ENABLED``: build đầy đủ redis/audit_log/gate/transport/recovery-executor.
    Thiếu hoặc lỗi bất kỳ dependency nào → ``AgentBootstrapError`` (không broad-catch,
    không fallback no-op) — caller (``daemon.main``) để exception propagate → exit non-zero.
    """
    env = os.environ if env is None else env
    if mode not in _VALID_MODES:
        raise AgentBootstrapError(
            f"invalid AOIP_AGENT_MODE={mode!r} — must be one of {_VALID_MODES}")

    if mode == MODE_OBSERVE_ONLY:
        return observe_only_executor, RuntimeStatus(MODE_OBSERVE_ONLY, STATUS_DISABLED)

    # MODE_MUTATION_ENABLED — mọi dependency bắt buộc, không silent fallback.
    import redis.asyncio as aioredis  # import cục bộ: observe-only không cần cài redis client

    redis_url = _require(env, "AOIP_REDIS_URL")
    audit_path = _require(env, "AOIP_AUDIT_LOG_PATH")
    try:
        redis_client = aioredis.from_url(redis_url, decode_responses=True)
    except Exception as exc:  # noqa: BLE001 — không broad-catch-fallback, chỉ làm rõ nguyên nhân rồi raise
        raise AgentBootstrapError(f"failed to construct redis client from AOIP_REDIS_URL: {exc}") from exc

    audit_log = audit.FileAuditLog(audit_path)
    gate = _build_gate(env)
    transport = _build_transport(env)
    env_auto_execute = env.get("AOIP_AUTO_EXECUTE_ENABLED", "false").strip().lower() == "true"

    executor = build_recovery_executor(
        redis=redis_client, holder=agent_id, transport=transport,
        audit_log=audit_log, gate=gate, env_auto_execute=env_auto_execute)
    return executor, RuntimeStatus(MODE_MUTATION_ENABLED, STATUS_ACTIVE)
