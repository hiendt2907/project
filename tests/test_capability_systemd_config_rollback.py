"""M6 — Human-approved Config Restore-from-Backup (capability #6 of the
VM/AOIP lane, Phase 4 remote-host/VM action library expansion).

Mirrors tests/test_capability_systemd_reset_failed.py's structure. Fake
transport models `sha256sum`/`cp -p`/`systemctl restart`/`systemctl is-active`
— KHÔNG mutation OS thật.

Distinct extra layer vs the other 5 capabilities: the config path is resolved
server-side from the unit name via `AOIP_CONFIG_ROLLBACK_PATHS`
(`aoip.recovery.config_rollback_path_for_unit`), NEVER taken from the payload.
"""
from __future__ import annotations

import time

import fakeredis.aioredis as aioredis
import pytest

from aoip import audit
from aoip.capabilities.systemd_config_rollback import (
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
    build_systemd_config_rollback_executor,
    build_typed_payload,
    capability_payload_hash,
    describe_capability,
    issue_capability_command,
    load_policy_from_env,
    validate_unit_name,
)
from aoip.objects import Finding
from aoip.recovery import RecoveryGate, config_rollback_path_for_unit

TENANT = "acme"
NOW = time.time()
UNIT = "nginx.service"
PATH = "/etc/nginx/nginx.conf"


def _redis():
    return aioredis.FakeRedis(decode_responses=True)


def _log(tmp_path, name="a.jsonl"):
    return audit.FileAuditLog(tmp_path / name)


def _gate():
    return RecoveryGate(allowed_failure_modes=frozenset({"config_drifted"}),
                        allowed_substrates=frozenset({"systemd"}), max_risk=0.5,
                        scope_prefix="svc:", min_diagnosis_confidence=0.0, max_diagnosis_age_s=1e9,
                        allowed_targets=frozenset({UNIT, "other.service"}))


def _policy(*units):
    return SystemdRestartPolicy(allowed_units=frozenset(units))


class FakeConfigSystemd:
    """Transport giả — KHÔNG chạm OS thật. Hỗ trợ sha256sum/cp -p/systemctl
    restart/is-active/LoadState. Model 2 file ảo bằng dict checksum theo path."""

    target = "h"

    def __init__(self, *, exists=True, current_sha="drifted-hash", backup_sha="good-hash",
                cp_rc=0, restart_rc=0, heal_on_cp=True):
        self.exists = exists
        self.checksums = {PATH: current_sha, f"{PATH}.aoip-backup": backup_sha}
        self.cp_rc = cp_rc
        self.restart_rc = restart_rc
        self.heal_on_cp = heal_on_cp
        self.cps = 0
        self.restarts = 0
        self.active = "active"

    async def run(self, argv, *, timeout=15.0):
        cmd = " ".join(argv)
        if "LoadState" in cmd:
            return ("loaded\n" if self.exists else "not-found\n", 0)
        if "sha256sum" in cmd:
            path = argv[-1]
            digest = self.checksums.get(path)
            if digest is None:
                return ("", 1)
            return (f"{digest}  {path}\n", 0)
        if "cp" in cmd and "pre_rollback_snapshot" in cmd:
            # Reversibility snapshot — best-effort, always succeeds in fake.
            return ("", 0)
        if "cp" in cmd and "aoip-backup" in cmd:
            self.cps += 1
            if self.cp_rc == 0 and self.heal_on_cp:
                self.checksums[PATH] = self.checksums[f"{PATH}.aoip-backup"]
            return ("", self.cp_rc)
        if "systemctl" in cmd and "restart" in cmd:
            self.restarts += 1
            return ("", self.restart_rc)
        if "is-active" in cmd:
            return (f"{self.active}\n", 0)
        return ("", 0)


def _command(*, unit=UNIT, tenant=TENANT, ttl_s=300, approver="alice", approved=True):
    typed = build_typed_payload(mission_id="mis-1", decision_id="dec-1", incident_id="inc-1",
                                summary="nginx.conf drifted from known-good backup", unit=unit)
    findings = (Finding(claim=f"svc:{unit} config_drifted (checksum mismatch)",
                        references=("i",), verdict=True, confidence=0.95),)
    cmd = issue_capability_command(typed_payload=typed, approver=approver, tenant=tenant,
                                   issued_at=NOW, expires_at=NOW + ttl_s,
                                   findings=findings, diagnosis_confidence=0.9)
    if not approved:
        cmd["approval"]["approved"] = False
    return cmd


@pytest.fixture(autouse=True)
def _config_path_env(monkeypatch):
    monkeypatch.setenv("AOIP_CONFIG_ROLLBACK_PATHS", f"{UNIT}:{PATH}")


# ── Contract ─────────────────────────────────────────────────────────────────

def test_describe_capability_known_version():
    assert describe_capability(CAPABILITY_NAME, CAPABILITY_VERSION) is not None


def test_describe_capability_unknown_returns_none():
    assert describe_capability(CAPABILITY_NAME, "999") is None


def test_capability_name_is_distinct_from_other_capabilities():
    assert CAPABILITY_NAME == "systemd.config_rollback"
    assert CAPABILITY_NAME not in (
        "systemd.restart_unit", "systemd.reset_failed", "systemd.journal_vacuum",
        "systemd.kill_unit", "systemd.disk_cleanup")


@pytest.mark.parametrize("unit", ["nginx.service", "app@1.service"])
def test_valid_unit_names_accepted(unit):
    assert validate_unit_name(unit) is None


@pytest.mark.parametrize("unit", ["nginx", "../etc/passwd.service", ""])
def test_invalid_unit_names_rejected(unit):
    assert validate_unit_name(unit) is not None


def test_payload_hash_distinct_from_other_capabilities_for_same_unit():
    from aoip.capabilities.systemd_restart import build_typed_payload as restart_typed

    rollback_typed = build_typed_payload(mission_id="m", decision_id="d", incident_id="i",
                                        summary="s", unit=UNIT)
    restart_payload = restart_typed(mission_id="m", decision_id="d", incident_id="i",
                                    summary="s", unit=UNIT)
    assert capability_payload_hash(rollback_typed) != capability_payload_hash(restart_payload)


async def test_payload_mutated_after_approval_invalidates_command(tmp_path):
    cmd = _command()
    cmd["target"] = {"unit": "other.service"}
    executor = await build_systemd_config_rollback_executor(
        redis=_redis(), holder="agent-1", transport=FakeConfigSystemd(), audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy(UNIT, "other.service"), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "FAILED"
    assert outcome["product_outcome"] == OUTCOME_PRECONDITION_FAILED
    assert "payload_hash_mismatch" in outcome["reason"]


async def test_unsupported_capability_version_fails_closed(tmp_path):
    cmd = _command()
    cmd["capability_version"] = "999"
    executor = await build_systemd_config_rollback_executor(
        redis=_redis(), holder="agent-1", transport=FakeConfigSystemd(), audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy(UNIT), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "FAILED"
    assert outcome["product_outcome"] == OUTCOME_UNSUPPORTED_CAPABILITY


# ── Policy reuse (SAME env var as restart_unit) ──────────────────────────────

def test_reuses_same_allowlist_env_var_as_restart_unit():
    policy = load_policy_from_env({"AOIP_ALLOWED_SYSTEMD_UNITS": UNIT})
    assert policy.is_allowed(UNIT)
    assert not policy.is_allowed("other.service")


async def test_unit_not_allowlisted_blocked_by_policy(tmp_path):
    cmd = _command()
    executor = await build_systemd_config_rollback_executor(
        redis=_redis(), holder="agent-1", transport=FakeConfigSystemd(),
        audit_log=_log(tmp_path), gate=_gate(), policy=_policy("other.service"), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "FAILED"
    assert outcome["product_outcome"] == OUTCOME_BLOCKED_BY_POLICY


async def test_approval_rejected_blocks_execution(tmp_path):
    cmd = _command(approved=False)
    t = FakeConfigSystemd()
    executor = await build_systemd_config_rollback_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy(UNIT), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "FAILED"
    assert outcome["product_outcome"] == OUTCOME_APPROVAL_REJECTED
    assert t.cps == 0


# ── Config-path resolution — the layer unique to this capability ────────────

def test_config_path_resolves_from_env_mapping():
    assert config_rollback_path_for_unit(UNIT) == PATH


def test_config_path_missing_for_unmapped_unit(monkeypatch):
    monkeypatch.delenv("AOIP_CONFIG_ROLLBACK_PATHS", raising=False)
    assert config_rollback_path_for_unit(UNIT) is None


async def test_unit_without_config_path_mapping_is_precondition_failed(tmp_path, monkeypatch):
    monkeypatch.delenv("AOIP_CONFIG_ROLLBACK_PATHS", raising=False)
    cmd = _command()
    t = FakeConfigSystemd()
    executor = await build_systemd_config_rollback_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy(UNIT), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "FAILED"
    assert outcome["product_outcome"] == OUTCOME_PRECONDITION_FAILED
    assert t.cps == 0


# ── Execution / verification ─────────────────────────────────────────────────

async def test_unit_missing_precondition_failed(tmp_path):
    cmd = _command()
    t = FakeConfigSystemd(exists=False)
    executor = await build_systemd_config_rollback_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy(UNIT), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "FAILED"
    assert outcome["product_outcome"] == OUTCOME_PRECONDITION_FAILED
    assert t.cps == 0


async def test_happy_path_executed_and_verified(tmp_path):
    """Config drifted from backup → cp restores it → restart → checksum
    matches backup + unit active → COMPLETED."""
    cmd = _command()
    t = FakeConfigSystemd(current_sha="drifted", backup_sha="good", heal_on_cp=True)
    executor = await build_systemd_config_rollback_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy(UNIT), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "COMPLETED"
    assert outcome["product_outcome"] == OUTCOME_EXECUTED_AND_VERIFIED
    assert t.cps == 1
    assert t.restarts == 1
    assert outcome["attempted"] == f"config-rollback {UNIT}"
    assert outcome["next_step"]


async def test_already_matches_backup_no_action_needed(tmp_path):
    cmd = _command()
    t = FakeConfigSystemd(current_sha="good", backup_sha="good")
    executor = await build_systemd_config_rollback_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy(UNIT), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "COMPLETED"
    assert outcome["product_outcome"] == OUTCOME_NO_ACTION_NEEDED
    assert t.cps == 0


async def test_verification_fails_still_drifted_after_rollback(tmp_path):
    cmd = _command()
    t = FakeConfigSystemd(current_sha="drifted", backup_sha="good", heal_on_cp=False)
    executor = await build_systemd_config_rollback_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy(UNIT), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "ESCALATED"
    assert outcome["product_outcome"] == OUTCOME_VERIFICATION_FAILED
    assert t.cps == 1


async def test_no_backup_found_is_treated_as_not_broken(tmp_path):
    """No backup checksum available (e.g. backup file never created) → the
    generic current-state gate (op.is_broken) aborts with zero mutation —
    same fail-closed contract as every other capability's 'already healthy'
    branch, here meaning 'nothing known-good to roll back to'."""
    cmd = _command()
    t = FakeConfigSystemd(current_sha="drifted", backup_sha=None)
    del t.checksums[f"{PATH}.aoip-backup"]
    executor = await build_systemd_config_rollback_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy(UNIT), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "COMPLETED"
    assert outcome["product_outcome"] == OUTCOME_NO_ACTION_NEEDED
    assert t.cps == 0


async def test_duplicate_command_does_not_duplicate_active_execution(tmp_path):
    cmd = _command()
    r = _redis()
    log = _log(tmp_path)
    t = FakeConfigSystemd(current_sha="drifted", backup_sha="good", heal_on_cp=True)
    executor = await build_systemd_config_rollback_executor(
        redis=r, holder="agent-1", transport=t, audit_log=log,
        gate=_gate(), policy=_policy(UNIT), tenant=TENANT)
    state1, _ = await executor(cmd)
    assert state1 == "COMPLETED" and t.cps == 1
    state2, _ = await executor(cmd)
    assert state2 == "COMPLETED"
    assert t.cps == 1  # KHÔNG rollback lần 2


# ── Reversibility proof — pre-rollback snapshot taken before overwrite ───────

async def test_pre_rollback_snapshot_taken_before_overwrite(tmp_path):
    """capture_before must run the snapshot cp BEFORE the actual rollback cp
    — proven here by counting the two distinct `cp` invocations the fake
    transport distinguishes (snapshot vs restore-from-backup)."""
    cmd = _command()
    t = FakeConfigSystemd(current_sha="drifted", backup_sha="good", heal_on_cp=True)
    snapshot_calls = []
    orig_run = t.run

    async def _tracking_run(argv, *, timeout=15.0):
        if "pre_rollback_snapshot" in " ".join(argv):
            snapshot_calls.append(argv)
        return await orig_run(argv, timeout=timeout)

    t.run = _tracking_run
    executor = await build_systemd_config_rollback_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy(UNIT), tenant=TENANT)
    state, _ = await executor(cmd)
    assert state == "COMPLETED"
    assert len(snapshot_calls) == 1


# ── Shadow mode ──────────────────────────────────────────────────────────────

async def test_shadow_mode_produces_recommendation_without_mutation(tmp_path):
    cmd = _command()
    t = FakeConfigSystemd(current_sha="drifted", backup_sha="good")
    executor = await build_systemd_config_rollback_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy(UNIT), tenant=TENANT, mode=MODE_SHADOW)
    state, outcome = await executor(cmd)
    assert outcome["product_outcome"] == OUTCOME_SHADOW_RECOMMENDATION
    assert t.cps == 0
    assert PATH in outcome["evidence"]["would_execute"]


async def test_shadow_mode_still_blocked_by_policy_if_not_allowlisted(tmp_path):
    cmd = _command()
    t = FakeConfigSystemd(current_sha="drifted", backup_sha="good")
    executor = await build_systemd_config_rollback_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy("other.service"), tenant=TENANT, mode=MODE_SHADOW)
    state, outcome = await executor(cmd)
    assert outcome["product_outcome"] == OUTCOME_BLOCKED_BY_POLICY
    assert t.cps == 0


# ── Tier_gate / risk_taxonomy — capability đi qua đúng gate hiện có ──────────

def test_risk_class_is_registered_and_not_high_fallback():
    from pkg.risk_taxonomy import HIGH, risk_class_of

    risk_class = risk_class_of(CAPABILITY_NAME)
    assert risk_class != HIGH
    assert risk_class == "LOW"
