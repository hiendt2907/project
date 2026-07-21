"""M2 — Human-approved Systemd Failed-State Cleanup (capability #2 of the VM/AOIP lane).

Mirrors tests/test_capability_systemd_restart.py's structure: typed payload →
preflight (capability/version/hash/allowlist/unit-exists) → guarded execution
(SAME lease/ledger/fencing infra as restart_unit, not bypassed) → verification
→ structured product outcome. KHÔNG mutation OS thật — FakeSystemd transport.

Unlike restart_unit, this capability never starts/stops/restarts the unit —
``apply()`` only runs ``systemctl reset-failed <unit>``, so the fixture models
``is-failed``/``reset-failed`` rather than ``is-active``/``restart``.
"""
from __future__ import annotations

import time

import fakeredis.aioredis as aioredis
import pytest

from aoip import audit
from aoip.capabilities.systemd_reset_failed import (
    CAPABILITY_NAME,
    CAPABILITY_VERSION,
    OUTCOME_APPROVAL_REJECTED,
    OUTCOME_BLOCKED_BY_POLICY,
    OUTCOME_EXECUTED_AND_VERIFIED,
    OUTCOME_NO_ACTION_NEEDED,
    OUTCOME_PRECONDITION_FAILED,
    OUTCOME_SHADOW_RECOMMENDATION,
    OUTCOME_UNSUPPORTED_CAPABILITY,
    OUTCOME_VERIFICATION_FAILED,
    MODE_SHADOW,
    SystemdRestartPolicy,
    build_systemd_reset_failed_executor,
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
    return RecoveryGate(allowed_failure_modes=frozenset({"failed_state_stale"}),
                        allowed_substrates=frozenset({"systemd"}), max_risk=0.5,
                        scope_prefix="svc:", min_diagnosis_confidence=0.0, max_diagnosis_age_s=1e9,
                        allowed_targets=frozenset({"nginx.service", "other.service"}))


def _policy(*units):
    return SystemdRestartPolicy(allowed_units=frozenset(units))


class FakeSystemd:
    """Transport giả — KHÔNG chạm OS thật. Hỗ trợ is-failed/reset-failed/LoadState.

    CỐ Ý không có ``restart``/``is-active`` heal path — capability này không
    bao giờ start/stop tiến trình, chỉ dọn failed bookkeeping.
    """

    target = "h"

    def __init__(self, *, is_failed=True, exists=True, reset_rc=0, heal_on_reset=True):
        self.is_failed_state = is_failed
        self.exists = exists
        self.reset_rc = reset_rc
        self.heal_on_reset = heal_on_reset
        self.resets = 0

    async def run(self, argv, *, timeout=15.0):
        cmd = " ".join(argv)
        if "LoadState" in cmd:
            return ("loaded\n" if self.exists else "not-found\n", 0)
        if "reset-failed" in cmd:
            self.resets += 1
            if self.heal_on_reset and self.reset_rc == 0:
                self.is_failed_state = False
            return ("", self.reset_rc)
        if "is-failed" in cmd:
            state = "failed" if self.is_failed_state else "active"
            return (state + "\n", 1 if self.is_failed_state else 0)
        return ("", 0)


def _command(*, unit="nginx.service", tenant=TENANT, ttl_s=300, approver="alice", approved=True):
    typed = build_typed_payload(mission_id="mis-1", decision_id="dec-1", incident_id="inc-1",
                                summary="unit stuck in failed state, dependency now healthy", unit=unit)
    findings = (Finding(claim=f"svc:{unit} failed_state_stale (start-limit hit, dependency recovered)",
                        references=("i",), verdict=True, confidence=0.95),)
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
    assert describe_capability("systemd.restart_unit", "1") is None


def test_capability_name_is_distinct_from_restart_unit():
    assert CAPABILITY_NAME == "systemd.reset_failed"
    assert CAPABILITY_NAME != "systemd.restart_unit"


@pytest.mark.parametrize("unit", ["nginx.service", "my-app_v2.service", "app@1.service"])
def test_valid_unit_names_accepted(unit):
    assert validate_unit_name(unit) is None


@pytest.mark.parametrize("unit", [
    "nginx", "../etc/passwd.service", "nginx.service; rm -rf /", "",
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


def test_payload_hash_distinct_from_restart_unit_for_same_unit():
    """Same unit, different capability → different hash (capability name is
    part of the hashed contract, so an approval for one can never decode as
    the other)."""
    from aoip.capabilities.systemd_restart import build_typed_payload as restart_typed

    reset_typed = build_typed_payload(mission_id="m", decision_id="d", incident_id="i",
                                      summary="s", unit="nginx.service")
    restart_payload = restart_typed(mission_id="m", decision_id="d", incident_id="i",
                                    summary="s", unit="nginx.service")
    assert capability_payload_hash(reset_typed) != capability_payload_hash(restart_payload)


async def test_payload_mutated_after_approval_invalidates_command(tmp_path):
    cmd = _command()
    cmd["target"] = {"unit": "other.service"}  # kẻ tấn công đổi target sau approval
    executor = await build_systemd_reset_failed_executor(
        redis=_redis(), holder="agent-1", transport=FakeSystemd(), audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy("nginx.service", "other.service"), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "FAILED"
    assert outcome["product_outcome"] == OUTCOME_PRECONDITION_FAILED
    assert "payload_hash_mismatch" in outcome["reason"]


async def test_unsupported_capability_version_fails_closed(tmp_path):
    cmd = _command()
    cmd["capability_version"] = "999"
    executor = await build_systemd_reset_failed_executor(
        redis=_redis(), holder="agent-1", transport=FakeSystemd(), audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy("nginx.service"), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "FAILED"
    assert outcome["product_outcome"] == OUTCOME_UNSUPPORTED_CAPABILITY


# ── Policy reuse (SAME env var as restart_unit — no new env var invented) ────

def test_reuses_same_allowlist_env_var_as_restart_unit():
    policy = load_policy_from_env({"AOIP_ALLOWED_SYSTEMD_UNITS": "nginx.service"})
    assert policy.is_allowed("nginx.service")
    assert not policy.is_allowed("other.service")


async def test_unit_not_allowlisted_blocked_by_policy(tmp_path):
    cmd = _command(unit="nginx.service")
    executor = await build_systemd_reset_failed_executor(
        redis=_redis(), holder="agent-1", transport=FakeSystemd(is_failed=True),
        audit_log=_log(tmp_path), gate=_gate(), policy=_policy("other.service"), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "FAILED"
    assert outcome["product_outcome"] == OUTCOME_BLOCKED_BY_POLICY


async def test_approval_rejected_blocks_execution(tmp_path):
    cmd = _command(approved=False)
    t = FakeSystemd(is_failed=True)
    executor = await build_systemd_reset_failed_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy("nginx.service"), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "FAILED"
    assert outcome["product_outcome"] == OUTCOME_APPROVAL_REJECTED
    assert t.resets == 0


# ── Execution / verification ─────────────────────────────────────────────────

async def test_unit_missing_precondition_failed(tmp_path):
    cmd = _command()
    t = FakeSystemd(is_failed=True, exists=False)
    executor = await build_systemd_reset_failed_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy("nginx.service"), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "FAILED"
    assert outcome["product_outcome"] == OUTCOME_PRECONDITION_FAILED
    assert t.resets == 0


async def test_happy_path_executed_and_verified(tmp_path):
    """Unit stuck failed → reset-failed clears it → verified NOT failed → COMPLETED.
    Zero downtime: the fake never toggles into "active", only out of "failed"."""
    cmd = _command()
    t = FakeSystemd(is_failed=True, heal_on_reset=True)
    executor = await build_systemd_reset_failed_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy("nginx.service"), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "COMPLETED"
    assert outcome["product_outcome"] == OUTCOME_EXECUTED_AND_VERIFIED
    assert t.resets == 1
    assert outcome["attempted"] == "reset-failed nginx.service"
    assert outcome["next_step"]


async def test_already_not_failed_no_action_needed(tmp_path):
    """Unit is NOT in failed state (flag already cleared, or never was) —
    the generic current-state gate aborts with zero mutation, same mechanism
    restart_unit uses for 'already healthy'."""
    cmd = _command()
    t = FakeSystemd(is_failed=False)
    executor = await build_systemd_reset_failed_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy("nginx.service"), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "COMPLETED"
    assert outcome["product_outcome"] == OUTCOME_NO_ACTION_NEEDED
    assert t.resets == 0


async def test_verification_fails_still_failed_after_reset(tmp_path):
    cmd = _command()
    t = FakeSystemd(is_failed=True, heal_on_reset=False)  # reset rc=0 nhưng KHÔNG cleared
    executor = await build_systemd_reset_failed_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy("nginx.service"), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "ESCALATED"
    assert outcome["product_outcome"] == OUTCOME_VERIFICATION_FAILED
    assert t.resets == 1


async def test_duplicate_command_does_not_duplicate_active_execution(tmp_path):
    """Idempotency ledger (đã có, KHÔNG viết lại) chặn re-mutation nếu cùng command chạy lại."""
    cmd = _command()
    r = _redis()
    log = _log(tmp_path)
    t = FakeSystemd(is_failed=True, heal_on_reset=True)
    executor = await build_systemd_reset_failed_executor(
        redis=r, holder="agent-1", transport=t, audit_log=log,
        gate=_gate(), policy=_policy("nginx.service"), tenant=TENANT)
    state1, _ = await executor(cmd)
    assert state1 == "COMPLETED" and t.resets == 1
    state2, _ = await executor(cmd)
    assert state2 == "COMPLETED"
    assert t.resets == 1  # KHÔNG reset-failed lần 2


# ── Shadow mode ──────────────────────────────────────────────────────────────

async def test_shadow_mode_produces_recommendation_without_mutation(tmp_path):
    cmd = _command()
    t = FakeSystemd(is_failed=True)
    executor = await build_systemd_reset_failed_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy("nginx.service"), tenant=TENANT, mode=MODE_SHADOW)
    state, outcome = await executor(cmd)
    assert outcome["product_outcome"] == OUTCOME_SHADOW_RECOMMENDATION
    assert t.resets == 0
    assert outcome["evidence"]["would_execute"] == "systemctl reset-failed nginx.service"
    assert "predicted_verification_plan" in outcome["evidence"]


async def test_shadow_mode_still_blocked_by_policy_if_not_allowlisted(tmp_path):
    cmd = _command(unit="nginx.service")
    t = FakeSystemd(is_failed=True)
    executor = await build_systemd_reset_failed_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy("other.service"), tenant=TENANT, mode=MODE_SHADOW)
    state, outcome = await executor(cmd)
    assert outcome["product_outcome"] == OUTCOME_BLOCKED_BY_POLICY
    assert t.resets == 0


# ── Tier_gate / risk_taxonomy — capability đi qua đúng gate hiện có ──────────

def test_risk_class_is_registered_and_not_high_fallback():
    from pkg.risk_taxonomy import HIGH, risk_class_of

    risk_class = risk_class_of(CAPABILITY_NAME)
    assert risk_class != HIGH  # missing-from-table fail-closed default — must NOT apply here
    assert risk_class == "LOW"
