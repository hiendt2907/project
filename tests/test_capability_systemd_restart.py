"""M1 — Human-approved Systemd Service Recovery: capability contract + E2E.

Vertical slice: typed payload → preflight (capability/version/hash/allowlist/unit-
exists) → guarded execution (lease/ledger/fencing đã có, không bypass) → verification
→ structured product outcome. KHÔNG mutation OS thật — FakeSystemd transport.
"""
from __future__ import annotations

import time

import fakeredis.aioredis as aioredis
import pytest

from aoip import audit
from aoip.capabilities.systemd_restart import (
    CAPABILITY_NAME,
    CAPABILITY_VERSION,
    OUTCOME_APPROVAL_REJECTED,
    OUTCOME_BLOCKED_BY_POLICY,
    OUTCOME_EXECUTED_AND_VERIFIED,
    OUTCOME_EXECUTION_FAILED,
    OUTCOME_NO_ACTION_NEEDED,
    OUTCOME_OWNERSHIP_LOST_AMBIGUOUS,
    OUTCOME_PRECONDITION_FAILED,
    OUTCOME_SHADOW_RECOMMENDATION,
    OUTCOME_UNSUPPORTED_CAPABILITY,
    OUTCOME_VERIFICATION_FAILED,
    MODE_SHADOW,
    SystemdRestartPolicy,
    build_systemd_restart_executor,
    build_typed_payload,
    capability_payload_hash,
    describe_capability,
    issue_capability_command,
    load_policy_from_env,
    validate_unit_name,
)
from aoip.objects import Finding
from aoip.recovery import RecoveryGate

TENANT = "acme"
NOW = time.time()


def _redis():
    return aioredis.FakeRedis(decode_responses=True)


def _log(tmp_path, name="a.jsonl"):
    return audit.FileAuditLog(tmp_path / name)


def _gate():
    return RecoveryGate(allowed_failure_modes=frozenset({"process_down"}),
                        allowed_substrates=frozenset({"systemd"}), max_risk=0.5,
                        scope_prefix="svc:", min_diagnosis_confidence=0.0, max_diagnosis_age_s=1e9)


def _policy(*units):
    return SystemdRestartPolicy(allowed_units=frozenset(units))


class FakeSystemd:
    """Transport giả — KHÔNG chạm OS thật. Hỗ trợ is-active/restart/LoadState."""

    target = "h"

    def __init__(self, *, state="inactive", heal_on_restart=True, exists=True, restart_rc=0):
        self.state = state
        self.heal_on_restart = heal_on_restart
        self.exists = exists
        self.restart_rc = restart_rc
        self.restarts = 0

    async def run(self, argv, *, timeout=15.0):
        cmd = " ".join(argv)
        if "LoadState" in cmd:
            return ("loaded\n" if self.exists else "not-found\n", 0)
        if "restart" in cmd:
            self.restarts += 1
            if self.heal_on_restart and self.restart_rc == 0:
                self.state = "active"
            return ("", self.restart_rc)
        if "is-active" in cmd:
            return (self.state + "\n", 0 if self.state == "active" else 3)
        if "ActiveEnterTimestamp" in cmd:
            return ("Mon 2026-06-30\n", 0)
        return ("", 0)


def _command(*, unit="nginx.service", tenant=TENANT, ttl_s=300, approver="alice", approved=True):
    typed = build_typed_payload(mission_id="mis-1", decision_id="dec-1", incident_id="inc-1",
                                summary="svc down", unit=unit)
    findings = (Finding(claim=f"svc:{unit} is DOWN (probe failed)", references=("i",),
                        verdict=True, confidence=0.95),
               Finding(claim=f"svc:{unit}: process_down", references=("d",),
                       verdict=True, confidence=0.9))
    cmd = issue_capability_command(typed_payload=typed, approver=approver, tenant=tenant,
                                   issued_at=NOW, expires_at=NOW + ttl_s,
                                   findings=findings, diagnosis_confidence=0.9)
    if not approved:
        cmd["approval"]["approved"] = False
    return cmd


# ── Contract ─────────────────────────────────────────────────────────────────

def test_describe_capability_known_version():
    assert describe_capability(CAPABILITY_NAME, CAPABILITY_VERSION) is not None


def test_describe_capability_unknown_returns_none():
    assert describe_capability(CAPABILITY_NAME, "999") is None
    assert describe_capability("k8s.restart_deployment", "1") is None


@pytest.mark.parametrize("unit", ["nginx.service", "my-app_v2.service", "app@1.service"])
def test_valid_unit_names_accepted(unit):
    assert validate_unit_name(unit) is None


@pytest.mark.parametrize("unit", [
    "nginx", "../etc/passwd.service", "nginx.service; rm -rf /", "nginx service.service",
    "/etc/systemd/nginx.service", "nginx.service && echo pwned", "",
])
def test_invalid_unit_names_rejected(unit):
    assert validate_unit_name(unit) is not None


def test_payload_hash_changes_when_any_field_changes():
    typed = build_typed_payload(mission_id="m", decision_id="d", incident_id="i",
                                summary="s", unit="nginx.service")
    h1 = capability_payload_hash(typed)
    typed2 = {**typed, "reason": {**typed["reason"], "summary": "changed"}}
    h2 = capability_payload_hash(typed2)
    assert h1 != h2


async def test_payload_mutated_after_approval_invalidates_command(tmp_path):
    cmd = _command()
    cmd["target"] = {"unit": "OTHER.service"}  # kẻ tấn công đổi target sau approval
    executor = await build_systemd_restart_executor(
        redis=_redis(), holder="agent-1", transport=FakeSystemd(), audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy("nginx.service", "OTHER.service"), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "FAILED"
    assert outcome["product_outcome"] == OUTCOME_PRECONDITION_FAILED
    assert "payload_hash_mismatch" in outcome["reason"]


async def test_unsupported_capability_version_fails_closed(tmp_path):
    cmd = _command()
    cmd["capability_version"] = "999"
    executor = await build_systemd_restart_executor(
        redis=_redis(), holder="agent-1", transport=FakeSystemd(), audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy("nginx.service"), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "FAILED"
    assert outcome["product_outcome"] == OUTCOME_UNSUPPORTED_CAPABILITY


# ── Policy ───────────────────────────────────────────────────────────────────

def test_load_policy_from_env_empty_is_fail_closed():
    policy = load_policy_from_env({})
    assert policy.allowed_units == frozenset()
    assert policy.is_allowed("nginx.service") is False


def test_load_policy_from_env_parses_csv():
    policy = load_policy_from_env({"AOIP_ALLOWED_SYSTEMD_UNITS": "nginx.service, myapp.service"})
    assert policy.is_allowed("nginx.service")
    assert policy.is_allowed("myapp.service")
    assert not policy.is_allowed("other.service")


def test_self_restart_blocked_by_default():
    policy = SystemdRestartPolicy(allowed_units=frozenset({"aoip-agent.service"}),
                                  agent_service_name="aoip-agent.service")
    assert policy.is_allowed("aoip-agent.service") is False


def test_self_restart_allowed_when_explicit():
    policy = SystemdRestartPolicy(allowed_units=frozenset({"aoip-agent.service"}),
                                  agent_service_name="aoip-agent.service", allow_self_restart=True)
    assert policy.is_allowed("aoip-agent.service") is True


async def test_unit_not_allowlisted_blocked_by_policy(tmp_path):
    cmd = _command(unit="nginx.service")
    executor = await build_systemd_restart_executor(
        redis=_redis(), holder="agent-1", transport=FakeSystemd(state="inactive"),
        audit_log=_log(tmp_path), gate=_gate(), policy=_policy("other.service"), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "FAILED"
    assert outcome["product_outcome"] == OUTCOME_BLOCKED_BY_POLICY


async def test_approval_rejected_blocks_execution(tmp_path):
    cmd = _command(approved=False)
    t = FakeSystemd(state="inactive")
    executor = await build_systemd_restart_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy("nginx.service"), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "FAILED"
    assert outcome["product_outcome"] == OUTCOME_APPROVAL_REJECTED
    assert t.restarts == 0


async def test_expired_approval_blocks_execution(tmp_path):
    cmd = _command(ttl_s=1)  # hợp lệ lúc issue, hết hạn trước khi agent chạy
    t = FakeSystemd(state="inactive")
    executor = await build_systemd_restart_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy("nginx.service"), tenant=TENANT, now=lambda: NOW + 10)
    state, outcome = await executor(cmd)
    assert state == "FAILED"
    assert t.restarts == 0


async def test_wrong_tenant_blocks_execution(tmp_path):
    cmd = _command(tenant="other-tenant")
    t = FakeSystemd(state="inactive")
    executor = await build_systemd_restart_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy("nginx.service"), tenant=TENANT)  # executor built cho "acme"
    state, outcome = await executor(cmd)
    assert state == "FAILED"
    assert t.restarts == 0


# ── Execution / verification ─────────────────────────────────────────────────

async def test_unit_missing_precondition_failed(tmp_path):
    cmd = _command()
    t = FakeSystemd(state="inactive", exists=False)
    executor = await build_systemd_restart_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy("nginx.service"), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "FAILED"
    assert outcome["product_outcome"] == OUTCOME_PRECONDITION_FAILED
    assert t.restarts == 0


async def test_happy_path_executed_and_verified(tmp_path):
    cmd = _command()
    t = FakeSystemd(state="inactive", heal_on_restart=True)
    executor = await build_systemd_restart_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy("nginx.service"), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "COMPLETED"
    assert outcome["product_outcome"] == OUTCOME_EXECUTED_AND_VERIFIED
    assert t.restarts == 1
    assert outcome["next_step"]


async def test_already_active_no_action_needed(tmp_path):
    cmd = _command()
    t = FakeSystemd(state="active")  # đã healthy trước khi agent chạm vào
    executor = await build_systemd_restart_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy("nginx.service"), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "COMPLETED"
    assert outcome["product_outcome"] == OUTCOME_NO_ACTION_NEEDED
    assert t.restarts == 0


async def test_execution_nonzero_rc_execution_failed(tmp_path):
    cmd = _command()
    t = FakeSystemd(state="inactive", heal_on_restart=False, restart_rc=1)
    executor = await build_systemd_restart_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy("nginx.service"), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "ESCALATED"  # verify fails → escalate (existing domain semantics)
    assert outcome["product_outcome"] == OUTCOME_VERIFICATION_FAILED
    assert t.restarts == 1


async def test_verification_fails_service_not_active_after_restart(tmp_path):
    cmd = _command()
    t = FakeSystemd(state="inactive", heal_on_restart=False)  # restart rc=0 nhưng KHÔNG healed
    executor = await build_systemd_restart_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy("nginx.service"), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "ESCALATED"
    assert outcome["product_outcome"] == OUTCOME_VERIFICATION_FAILED


async def test_ownership_lost_during_mutation_ambiguous(tmp_path):
    from aoip.agent.lease import ExecutionLease
    import asyncio

    r = _redis()

    class HijackingSystemd(FakeSystemd):
        async def run(self, argv, *, timeout=15.0):
            if "restart" in " ".join(argv):
                await r.set("lease:svc:nginx.service", "other-agent-token", ex=120)
                await asyncio.sleep(0.03)
            return await super().run(argv, timeout=timeout)

    cmd = _command()
    t = HijackingSystemd(state="inactive")
    executor = await build_systemd_restart_executor(
        redis=r, holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy("nginx.service"), tenant=TENANT)
    # renewal interval nhỏ để lease renew chạy trong lúc "restart" ngủ 30ms
    from aoip.agent import timing_config as tc
    timing = tc.TimingConfig(lease_renewal_interval_s=0.01, execution_lease_ttl_s=120,
                             gateway_visibility_s=60, visibility_renewal_interval_s=15)
    executor = await build_systemd_restart_executor(
        redis=r, holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy("nginx.service"), tenant=TENANT, timing=timing)
    state, outcome = await executor(cmd)
    assert state == "ESCALATED"
    assert outcome["product_outcome"] == OUTCOME_OWNERSHIP_LOST_AMBIGUOUS


async def test_duplicate_command_does_not_duplicate_active_execution(tmp_path):
    """Idempotency ledger (đã có) chặn re-mutation nếu cùng command chạy lại."""
    cmd = _command()
    r = _redis()
    log = _log(tmp_path)
    t = FakeSystemd(state="inactive", heal_on_restart=True)
    executor = await build_systemd_restart_executor(
        redis=r, holder="agent-1", transport=t, audit_log=log,
        gate=_gate(), policy=_policy("nginx.service"), tenant=TENANT)
    state1, _ = await executor(cmd)
    assert state1 == "COMPLETED" and t.restarts == 1
    # duplicate delivery của CÙNG command (Gateway at-least-once) → idempotency ledger reconcile
    state2, outcome2 = await executor(cmd)
    assert state2 == "COMPLETED"
    assert t.restarts == 1  # KHÔNG restart lần 2


# ── Shadow mode ──────────────────────────────────────────────────────────────

async def test_shadow_mode_produces_recommendation_without_mutation(tmp_path):
    cmd = _command()
    t = FakeSystemd(state="inactive")
    executor = await build_systemd_restart_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy("nginx.service"), tenant=TENANT, mode=MODE_SHADOW)
    state, outcome = await executor(cmd)
    assert outcome["product_outcome"] == OUTCOME_SHADOW_RECOMMENDATION
    assert t.restarts == 0
    assert "would_execute" in outcome["evidence"]
    assert "predicted_verification_plan" in outcome["evidence"]


async def test_shadow_mode_still_blocked_by_policy_if_not_allowlisted(tmp_path):
    cmd = _command(unit="nginx.service")
    t = FakeSystemd(state="inactive")
    executor = await build_systemd_restart_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy("other.service"), tenant=TENANT, mode=MODE_SHADOW)
    state, outcome = await executor(cmd)
    assert outcome["product_outcome"] == OUTCOME_BLOCKED_BY_POLICY
    assert t.restarts == 0
