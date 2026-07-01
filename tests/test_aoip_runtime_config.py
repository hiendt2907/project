"""Acceptance tests: runtime mode bootstrap fail-closed (aoip.agent.runtime_config).

Chứng minh: MUTATION_ENABLED thiếu dependency → startup fail (raise), KHÔNG silent
no-op; OBSERVE_ONLY khởi động rõ ràng và KHÔNG bao giờ COMPLETED mutating command.
"""
from __future__ import annotations

import pytest

from aoip.agent.runtime_config import (
    MODE_MUTATION_ENABLED,
    MODE_OBSERVE_ONLY,
    STATUS_ACTIVE,
    STATUS_DISABLED,
    AgentBootstrapError,
    build_agent_runtime,
)

_FULL_MUTATION_ENV = {
    "AOIP_REDIS_URL": "redis://localhost:6379",
    "AOIP_AUDIT_LOG_PATH": "/tmp/aoip-test-audit.jsonl",
    "AOIP_GATE_ALLOWED_FAILURE_MODES": "process_down",
    "AOIP_GATE_ALLOWED_SUBSTRATES": "systemd",
    "AOIP_GATE_SCOPE_PREFIX": "svc:",
    "AOIP_GATE_MAX_RISK": "0.5",
    "AOIP_GATE_MIN_DIAGNOSIS_CONFIDENCE": "0.7",
    "AOIP_GATE_MAX_DIAGNOSIS_AGE_S": "300",
}


def test_mutation_enabled_with_full_dependency_builds_active_executor():
    executor, status = build_agent_runtime(
        mode=MODE_MUTATION_ENABLED, agent_id="agent-1", env=_FULL_MUTATION_ENV)
    assert status.executor_mode == MODE_MUTATION_ENABLED
    assert status.executor_status == STATUS_ACTIVE
    assert callable(executor)


def test_mutation_enabled_missing_redis_url_fails_closed():
    env = {k: v for k, v in _FULL_MUTATION_ENV.items() if k != "AOIP_REDIS_URL"}
    with pytest.raises(AgentBootstrapError, match="AOIP_REDIS_URL"):
        build_agent_runtime(mode=MODE_MUTATION_ENABLED, agent_id="agent-1", env=env)


def test_mutation_enabled_missing_audit_log_path_fails_closed():
    env = {k: v for k, v in _FULL_MUTATION_ENV.items() if k != "AOIP_AUDIT_LOG_PATH"}
    with pytest.raises(AgentBootstrapError, match="AOIP_AUDIT_LOG_PATH"):
        build_agent_runtime(mode=MODE_MUTATION_ENABLED, agent_id="agent-1", env=env)


def test_mutation_enabled_missing_gate_config_fails_closed():
    env = {k: v for k, v in _FULL_MUTATION_ENV.items() if k != "AOIP_GATE_MAX_RISK"}
    with pytest.raises(AgentBootstrapError, match="AOIP_GATE_MAX_RISK"):
        build_agent_runtime(mode=MODE_MUTATION_ENABLED, agent_id="agent-1", env=env)


def test_mutation_enabled_invalid_numeric_gate_config_fails_closed():
    env = dict(_FULL_MUTATION_ENV)
    env["AOIP_GATE_MAX_RISK"] = "not-a-number"
    with pytest.raises(AgentBootstrapError):
        build_agent_runtime(mode=MODE_MUTATION_ENABLED, agent_id="agent-1", env=env)


def test_invalid_mode_fails_closed():
    with pytest.raises(AgentBootstrapError, match="invalid AOIP_AGENT_MODE"):
        build_agent_runtime(mode="something_else", agent_id="agent-1", env={})


async def test_observe_only_builds_disabled_status_no_dependency_needed():
    executor, status = build_agent_runtime(mode=MODE_OBSERVE_ONLY, agent_id="agent-1", env={})
    assert status.executor_mode == MODE_OBSERVE_ONLY
    assert status.executor_status == STATUS_DISABLED
    state, outcome = await executor({"verb": "restart_service"})
    assert state == "ESCALATED"
    assert outcome["reason"] == "executor_disabled_observe_only"


async def test_observe_only_never_completes_mutating_command_end_to_end():
    """Full DeliveryLoop wiring: observe-only executor must never report COMPLETED."""
    from aoip.agent.delivery_loop import DeliveryLoop
    from aoip.agent.inbox import LocalInbox
    import tempfile

    executor, _ = build_agent_runtime(mode=MODE_OBSERVE_ONLY, agent_id="agent-1", env={})

    class FakeClient:
        def __init__(self, commands):
            self._commands = commands
            self.reported = []

        async def poll_runtime(self, agent_id):
            out, self._commands = self._commands, []
            return out

        async def accept(self, *a, **k):
            pass

        async def progress(self, *a, **k):
            pass

        async def report_terminal(self, agent_id, tenant_id, command_id, state, outcome, **k):
            self.reported.append((state, outcome))
            return {"acknowledged": True}

    with tempfile.TemporaryDirectory() as tmp:
        client = FakeClient([{"command_id": "cmd-1", "tenant_id": "acme",
                              "payload": {"verb": "restart_service"}}])
        loop = DeliveryLoop(agent_id="agent-1", client=client,
                           inbox=LocalInbox(tmp), executor=executor)
        await loop.tick()
        assert client.reported[0][0] == "ESCALATED"
        assert client.reported[0][0] != "COMPLETED"
