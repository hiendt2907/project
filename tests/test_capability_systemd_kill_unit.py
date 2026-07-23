"""M4 — Human-approved Runaway-Process Kill (capability #4 of the VM/AOIP lane,
Phase 4 remote-host/VM action library expansion).

Mirrors tests/test_capability_systemd_journal_vacuum.py's structure: typed
payload → preflight (capability/version/hash/allowlist/unit-exists) → guarded
execution (SAME lease/ledger/fencing infra as the other capabilities, not
bypassed) → verification → structured product outcome. KHÔNG mutation OS thật
— FakeRunawaySystemd transport.
"""
from __future__ import annotations

import time

import fakeredis.aioredis as aioredis
import pytest

from aoip import audit
from aoip.capabilities.systemd_kill_unit import (
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
    build_systemd_kill_unit_executor,
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
UNIT = "runaway-app.service"

_ONE_GIB = 1 * 1024**3


def _redis():
    return aioredis.FakeRedis(decode_responses=True)


def _log(tmp_path, name="a.jsonl"):
    return audit.FileAuditLog(tmp_path / name)


def _gate():
    return RecoveryGate(allowed_failure_modes=frozenset({"resource_runaway"}),
                        allowed_substrates=frozenset({"systemd"}), max_risk=0.5,
                        scope_prefix="svc:", min_diagnosis_confidence=0.0, max_diagnosis_age_s=1e9,
                        allowed_targets=frozenset({UNIT, "other.service"}))


def _policy(*units):
    return SystemdRestartPolicy(allowed_units=frozenset(units))


class FakeRunawaySystemd:
    """Transport giả — KHÔNG chạm OS thật. Hỗ trợ MemoryCurrent/LoadState/kill.

    CỐ Ý không có restart/is-failed heal path riêng — capability này chỉ gửi
    SIGTERM qua ``systemctl kill``, hồi phục (nếu có) là do Restart= policy giả
    lập qua ``heal_on_kill``.
    """

    target = "h"

    def __init__(self, *, memory_bytes=2 * 1024**3, exists=True, kill_rc=0,
                heal_on_kill=True, post_kill_bytes=10 * 1024**2):
        self.memory_bytes = memory_bytes
        self.exists = exists
        self.kill_rc = kill_rc
        self.heal_on_kill = heal_on_kill
        self.post_kill_bytes = post_kill_bytes
        self.kills = 0

    async def run(self, argv, *, timeout=15.0):
        cmd = " ".join(argv)
        if "LoadState" in cmd:
            return ("loaded\n" if self.exists else "not-found\n", 0)
        if "kill" in cmd and "--signal=SIGTERM" in cmd:
            self.kills += 1
            if self.heal_on_kill and self.kill_rc == 0:
                self.memory_bytes = self.post_kill_bytes
            return ("", self.kill_rc)
        if "MemoryCurrent" in cmd:
            return (f"{self.memory_bytes}\n", 0)
        if "is-active" in cmd:
            return ("active\n", 0)
        return ("", 0)


def _command(*, unit=UNIT, tenant=TENANT, ttl_s=300, approver="alice", approved=True):
    typed = build_typed_payload(mission_id="mis-1", decision_id="dec-1", incident_id="inc-1",
                                summary="unit memory 2.0GiB over threshold", unit=unit)
    findings = (Finding(claim=f"svc:{unit} resource_runaway (MemoryCurrent 2.0GiB)",
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
    assert describe_capability("systemd.reset_failed", "1") is None


def test_capability_name_is_distinct_from_other_capabilities():
    assert CAPABILITY_NAME == "systemd.kill_unit"
    assert CAPABILITY_NAME not in (
        "systemd.restart_unit", "systemd.reset_failed", "systemd.journal_vacuum")


@pytest.mark.parametrize("unit", ["runaway-app.service", "nginx.service", "app@1.service"])
def test_valid_unit_names_accepted(unit):
    assert validate_unit_name(unit) is None


@pytest.mark.parametrize("unit", [
    "runaway-app", "../etc/passwd.service", "app.service; rm -rf /", "",
])
def test_invalid_unit_names_rejected(unit):
    assert validate_unit_name(unit) is not None


def test_payload_hash_distinct_from_other_capabilities_for_same_unit():
    from aoip.capabilities.systemd_reset_failed import build_typed_payload as reset_failed_typed
    from aoip.capabilities.systemd_restart import build_typed_payload as restart_typed

    kill_typed = build_typed_payload(mission_id="m", decision_id="d", incident_id="i",
                                     summary="s", unit=UNIT)
    reset_payload = reset_failed_typed(mission_id="m", decision_id="d", incident_id="i",
                                       summary="s", unit=UNIT)
    restart_payload = restart_typed(mission_id="m", decision_id="d", incident_id="i",
                                    summary="s", unit=UNIT)
    assert capability_payload_hash(kill_typed) != capability_payload_hash(reset_payload)
    assert capability_payload_hash(kill_typed) != capability_payload_hash(restart_payload)


async def test_payload_mutated_after_approval_invalidates_command(tmp_path):
    cmd = _command()
    cmd["target"] = {"unit": "other.service"}
    executor = await build_systemd_kill_unit_executor(
        redis=_redis(), holder="agent-1", transport=FakeRunawaySystemd(), audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy(UNIT, "other.service"), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "FAILED"
    assert outcome["product_outcome"] == OUTCOME_PRECONDITION_FAILED
    assert "payload_hash_mismatch" in outcome["reason"]


async def test_unsupported_capability_version_fails_closed(tmp_path):
    cmd = _command()
    cmd["capability_version"] = "999"
    executor = await build_systemd_kill_unit_executor(
        redis=_redis(), holder="agent-1", transport=FakeRunawaySystemd(), audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy(UNIT), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "FAILED"
    assert outcome["product_outcome"] == OUTCOME_UNSUPPORTED_CAPABILITY


# ── Policy reuse (SAME env var as restart_unit/reset_failed/journal_vacuum) ──

def test_reuses_same_allowlist_env_var_as_restart_unit():
    policy = load_policy_from_env({"AOIP_ALLOWED_SYSTEMD_UNITS": UNIT})
    assert policy.is_allowed(UNIT)
    assert not policy.is_allowed("other.service")


async def test_unit_not_allowlisted_blocked_by_policy(tmp_path):
    cmd = _command()
    executor = await build_systemd_kill_unit_executor(
        redis=_redis(), holder="agent-1", transport=FakeRunawaySystemd(),
        audit_log=_log(tmp_path), gate=_gate(), policy=_policy("other.service"), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "FAILED"
    assert outcome["product_outcome"] == OUTCOME_BLOCKED_BY_POLICY


async def test_approval_rejected_blocks_execution(tmp_path):
    cmd = _command(approved=False)
    t = FakeRunawaySystemd()
    executor = await build_systemd_kill_unit_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy(UNIT), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "FAILED"
    assert outcome["product_outcome"] == OUTCOME_APPROVAL_REJECTED
    assert t.kills == 0


# ── Execution / verification ─────────────────────────────────────────────────

async def test_unit_missing_precondition_failed(tmp_path):
    cmd = _command()
    t = FakeRunawaySystemd(exists=False)
    executor = await build_systemd_kill_unit_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy(UNIT), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "FAILED"
    assert outcome["product_outcome"] == OUTCOME_PRECONDITION_FAILED
    assert t.kills == 0


async def test_happy_path_executed_and_verified(tmp_path):
    """Memory usage above threshold → SIGTERM → unit's own Restart= policy
    (simulated by heal_on_kill) brings memory back down → COMPLETED."""
    cmd = _command()
    t = FakeRunawaySystemd(memory_bytes=2 * 1024**3, heal_on_kill=True,
                          post_kill_bytes=10 * 1024**2)
    executor = await build_systemd_kill_unit_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy(UNIT), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "COMPLETED"
    assert outcome["product_outcome"] == OUTCOME_EXECUTED_AND_VERIFIED
    assert t.kills == 1
    assert outcome["attempted"] == f"kill {UNIT}"
    assert outcome["next_step"]


async def test_already_below_threshold_no_action_needed(tmp_path):
    cmd = _command()
    t = FakeRunawaySystemd(memory_bytes=10 * 1024**2)  # 10MiB, well under 1GiB default
    executor = await build_systemd_kill_unit_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy(UNIT), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "COMPLETED"
    assert outcome["product_outcome"] == OUTCOME_NO_ACTION_NEEDED
    assert t.kills == 0


async def test_verification_fails_still_above_threshold_after_kill(tmp_path):
    """Unit has no Restart= policy — kill sends SIGTERM but memory does not
    drop (nothing brought the process back down) → escalate, no retry."""
    cmd = _command()
    t = FakeRunawaySystemd(memory_bytes=2 * 1024**3, heal_on_kill=False)
    executor = await build_systemd_kill_unit_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy(UNIT), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "ESCALATED"
    assert outcome["product_outcome"] == OUTCOME_VERIFICATION_FAILED
    assert t.kills == 1


async def test_duplicate_command_does_not_duplicate_active_execution(tmp_path):
    """Idempotency ledger (đã có, KHÔNG viết lại) chặn re-mutation nếu cùng command chạy lại."""
    cmd = _command()
    r = _redis()
    log = _log(tmp_path)
    t = FakeRunawaySystemd(memory_bytes=2 * 1024**3, heal_on_kill=True)
    executor = await build_systemd_kill_unit_executor(
        redis=r, holder="agent-1", transport=t, audit_log=log,
        gate=_gate(), policy=_policy(UNIT), tenant=TENANT)
    state1, _ = await executor(cmd)
    assert state1 == "COMPLETED" and t.kills == 1
    state2, _ = await executor(cmd)
    assert state2 == "COMPLETED"
    assert t.kills == 1  # KHÔNG kill lần 2


# ── Shadow mode ──────────────────────────────────────────────────────────────

async def test_shadow_mode_produces_recommendation_without_mutation(tmp_path):
    cmd = _command()
    t = FakeRunawaySystemd(memory_bytes=2 * 1024**3)
    executor = await build_systemd_kill_unit_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy(UNIT), tenant=TENANT, mode=MODE_SHADOW)
    state, outcome = await executor(cmd)
    assert outcome["product_outcome"] == OUTCOME_SHADOW_RECOMMENDATION
    assert t.kills == 0
    assert outcome["evidence"]["would_execute"] == f"systemctl kill --signal=SIGTERM {UNIT}"
    assert "predicted_verification_plan" in outcome["evidence"]


async def test_shadow_mode_still_blocked_by_policy_if_not_allowlisted(tmp_path):
    cmd = _command()
    t = FakeRunawaySystemd(memory_bytes=2 * 1024**3)
    executor = await build_systemd_kill_unit_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy("other.service"), tenant=TENANT, mode=MODE_SHADOW)
    state, outcome = await executor(cmd)
    assert outcome["product_outcome"] == OUTCOME_BLOCKED_BY_POLICY
    assert t.kills == 0


# ── Env-configurable threshold — KHÔNG hardcode ───────────────────────────────

def test_threshold_default_when_env_unset(monkeypatch):
    from aoip.recovery import _kill_unit_memory_threshold_bytes

    monkeypatch.delenv("AOIP_KILL_UNIT_MEMORY_THRESHOLD_BYTES", raising=False)
    assert _kill_unit_memory_threshold_bytes() == _ONE_GIB


def test_threshold_read_from_env(monkeypatch):
    from aoip.recovery import _kill_unit_memory_threshold_bytes

    monkeypatch.setenv("AOIP_KILL_UNIT_MEMORY_THRESHOLD_BYTES", str(500 * 1024**2))
    assert _kill_unit_memory_threshold_bytes() == 500 * 1024**2


def test_threshold_falls_back_to_default_when_env_unparseable(monkeypatch):
    from aoip.recovery import _kill_unit_memory_threshold_bytes

    monkeypatch.setenv("AOIP_KILL_UNIT_MEMORY_THRESHOLD_BYTES", "not-a-number")
    assert _kill_unit_memory_threshold_bytes() == _ONE_GIB


async def test_env_threshold_override_changes_is_broken_decision(tmp_path, monkeypatch):
    monkeypatch.setenv("AOIP_KILL_UNIT_MEMORY_THRESHOLD_BYTES", str(10 * 1024**2))  # 10MiB
    cmd = _command()
    t = FakeRunawaySystemd(memory_bytes=20 * 1024**2, heal_on_kill=True,
                          post_kill_bytes=1024**2)  # 20MiB > 10MiB threshold
    executor = await build_systemd_kill_unit_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy(UNIT), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "COMPLETED"
    assert outcome["product_outcome"] == OUTCOME_EXECUTED_AND_VERIFIED
    assert t.kills == 1


# ── MemoryCurrent parsing — "[not set]" is not a valid measurement ───────────

async def test_memory_current_not_set_is_treated_as_unmeasurable(tmp_path):
    """`systemctl show -p MemoryCurrent --value` returns "[not set]" on cgroup
    v1 hosts without memory accounting enabled — this MUST NOT parse as a
    number and MUST NOT be treated as "healthy" or "broken" by guesswork."""
    from aoip.recovery import _parse_memory_current_bytes

    assert _parse_memory_current_bytes("[not set]\n") is None
    assert _parse_memory_current_bytes("") is None
    assert _parse_memory_current_bytes("12345\n") == 12345


# ── Tier_gate / risk_taxonomy — capability đi qua đúng gate hiện có ──────────

def test_risk_class_is_registered_and_not_high_fallback():
    from pkg.risk_taxonomy import HIGH, risk_class_of

    risk_class = risk_class_of(CAPABILITY_NAME)
    assert risk_class != HIGH
    assert risk_class == "LOW"
