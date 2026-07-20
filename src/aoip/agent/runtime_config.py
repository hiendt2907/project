"""Runtime mode bootstrap cho AOIP Agent — fail-closed, KHÔNG silent no-op.

Vì sao tồn tại: ``daemon.main()`` (CLI/systemd entrypoint) trước đây không tiêm
``redis``/``transport``/``audit_log``/``gate`` nên LUÔN rơi về ``_noop_executor`` mà
không có tín hiệu nào cho operator biết mutation không hoạt động. Module này buộc
operator chọn RÕ một trong hai mode; ``MUTATION_ENABLED`` thiếu bất kỳ dependency nào
phải làm startup THẤT BẠI (raise), KHÔNG bao giờ tự động rơi về observe-only.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Awaitable, Callable

from aoip import audit
from aoip.agent.operations import build_recovery_executor
from aoip.recovery import RecoveryGate
from aoip.transport import LocalTransport, SSHTransport

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
    return RecoveryGate(
        allowed_failure_modes=frozenset(m.strip() for m in modes.split(",") if m.strip()),
        allowed_substrates=frozenset(s.strip() for s in substrates.split(",") if s.strip()),
        max_risk=max_risk, scope_prefix=scope_prefix,
        min_diagnosis_confidence=min_conf, max_diagnosis_age_s=max_age_s,
        allowed_targets=frozenset(u.strip() for u in allowed_units.split(",") if u.strip()))


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
