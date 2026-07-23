"""M5 — Human-approved Filesystem Disk Cleanup (capability #5 of the VM/AOIP
lane, Phase 4 remote-host/VM action library expansion).

Mirrors tests/test_capability_systemd_journal_vacuum.py's structure. Target
unit is ALWAYS the fixed literal "systemd-tmpfiles-clean.service" and the
fixture models `df --output=pcent`/`systemctl start` rather than
`journalctl --disk-usage`/`--vacuum-size=`.
"""
from __future__ import annotations

import time

import fakeredis.aioredis as aioredis
import pytest

from aoip import audit
from aoip.capabilities.systemd_disk_cleanup import (
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
    build_systemd_disk_cleanup_executor,
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
UNIT = "systemd-tmpfiles-clean.service"


def _redis():
    return aioredis.FakeRedis(decode_responses=True)


def _log(tmp_path, name="a.jsonl"):
    return audit.FileAuditLog(tmp_path / name)


def _gate():
    return RecoveryGate(allowed_failure_modes=frozenset({"disk_pressure_tmp"}),
                        allowed_substrates=frozenset({"systemd"}), max_risk=0.5,
                        scope_prefix="svc:", min_diagnosis_confidence=0.0, max_diagnosis_age_s=1e9,
                        allowed_targets=frozenset({UNIT, "other.service"}))


def _policy(*units):
    return SystemdRestartPolicy(allowed_units=frozenset(units))


class FakeDiskSystemd:
    """Transport giả — KHÔNG chạm OS thật. Hỗ trợ df --output=pcent/LoadState/
    systemctl start. CỐ Ý không có is-active/restart heal path — capability
    này chỉ khởi động unit oneshot tmpfiles-clean chính thức."""

    target = "h"

    def __init__(self, *, usage_pct=90.0, exists=True, start_rc=0,
                heal_on_start=True, post_start_pct=40.0):
        self.usage_pct = usage_pct
        self.exists = exists
        self.start_rc = start_rc
        self.heal_on_start = heal_on_start
        self.post_start_pct = post_start_pct
        self.starts = 0

    async def run(self, argv, *, timeout=15.0):
        cmd = " ".join(argv)
        if "LoadState" in cmd:
            return ("loaded\n" if self.exists else "not-found\n", 0)
        if "systemctl" in cmd and "start" in cmd:
            self.starts += 1
            if self.heal_on_start and self.start_rc == 0:
                self.usage_pct = self.post_start_pct
            return ("", self.start_rc)
        if "df" in cmd:
            return (f"Use%\n{self.usage_pct:.0f}%\n", 0)
        return ("", 0)


def _command(*, unit=UNIT, tenant=TENANT, ttl_s=300, approver="alice", approved=True):
    typed = build_typed_payload(mission_id="mis-1", decision_id="dec-1", incident_id="inc-1",
                                summary="root filesystem 90% over threshold", unit=unit)
    findings = (Finding(claim=f"svc:{unit} disk_pressure_tmp (df 90%)",
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


def test_capability_name_is_distinct_from_other_capabilities():
    assert CAPABILITY_NAME == "systemd.disk_cleanup"
    assert CAPABILITY_NAME not in (
        "systemd.restart_unit", "systemd.reset_failed", "systemd.journal_vacuum",
        "systemd.kill_unit")


@pytest.mark.parametrize("unit", ["systemd-tmpfiles-clean.service", "nginx.service"])
def test_valid_unit_names_accepted(unit):
    assert validate_unit_name(unit) is None


@pytest.mark.parametrize("unit", [
    "systemd-tmpfiles-clean", "../etc/passwd.service", "app.service; rm -rf /", "",
])
def test_invalid_unit_names_rejected(unit):
    assert validate_unit_name(unit) is not None


def test_payload_hash_distinct_from_other_capabilities_for_same_unit():
    from aoip.capabilities.systemd_journal_vacuum import build_typed_payload as vacuum_typed

    disk_typed = build_typed_payload(mission_id="m", decision_id="d", incident_id="i",
                                     summary="s", unit=UNIT)
    vacuum_payload = vacuum_typed(mission_id="m", decision_id="d", incident_id="i",
                                  summary="s", unit=UNIT)
    assert capability_payload_hash(disk_typed) != capability_payload_hash(vacuum_payload)


async def test_payload_mutated_after_approval_invalidates_command(tmp_path):
    cmd = _command()
    cmd["target"] = {"unit": "other.service"}
    executor = await build_systemd_disk_cleanup_executor(
        redis=_redis(), holder="agent-1", transport=FakeDiskSystemd(), audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy(UNIT, "other.service"), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "FAILED"
    assert outcome["product_outcome"] == OUTCOME_PRECONDITION_FAILED
    assert "payload_hash_mismatch" in outcome["reason"]


async def test_unsupported_capability_version_fails_closed(tmp_path):
    cmd = _command()
    cmd["capability_version"] = "999"
    executor = await build_systemd_disk_cleanup_executor(
        redis=_redis(), holder="agent-1", transport=FakeDiskSystemd(), audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy(UNIT), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "FAILED"
    assert outcome["product_outcome"] == OUTCOME_UNSUPPORTED_CAPABILITY


# ── Policy reuse (SAME env var as restart_unit) — must be explicitly allowlisted ─

def test_reuses_same_allowlist_env_var_as_restart_unit():
    policy = load_policy_from_env({"AOIP_ALLOWED_SYSTEMD_UNITS": UNIT})
    assert policy.is_allowed(UNIT)
    assert not policy.is_allowed("other.service")


async def test_unit_not_allowlisted_blocked_by_policy(tmp_path):
    cmd = _command()
    executor = await build_systemd_disk_cleanup_executor(
        redis=_redis(), holder="agent-1", transport=FakeDiskSystemd(),
        audit_log=_log(tmp_path), gate=_gate(), policy=_policy("other.service"), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "FAILED"
    assert outcome["product_outcome"] == OUTCOME_BLOCKED_BY_POLICY


async def test_approval_rejected_blocks_execution(tmp_path):
    cmd = _command(approved=False)
    t = FakeDiskSystemd()
    executor = await build_systemd_disk_cleanup_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy(UNIT), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "FAILED"
    assert outcome["product_outcome"] == OUTCOME_APPROVAL_REJECTED
    assert t.starts == 0


# ── Execution / verification ─────────────────────────────────────────────────

async def test_unit_missing_precondition_failed(tmp_path):
    cmd = _command()
    t = FakeDiskSystemd(exists=False)
    executor = await build_systemd_disk_cleanup_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy(UNIT), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "FAILED"
    assert outcome["product_outcome"] == OUTCOME_PRECONDITION_FAILED
    assert t.starts == 0


async def test_happy_path_executed_and_verified(tmp_path):
    cmd = _command()
    t = FakeDiskSystemd(usage_pct=90.0, heal_on_start=True, post_start_pct=40.0)
    executor = await build_systemd_disk_cleanup_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy(UNIT), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "COMPLETED"
    assert outcome["product_outcome"] == OUTCOME_EXECUTED_AND_VERIFIED
    assert t.starts == 1
    assert outcome["attempted"] == f"tmpfiles-clean {UNIT}"
    assert outcome["next_step"]


async def test_already_below_threshold_no_action_needed(tmp_path):
    cmd = _command()
    t = FakeDiskSystemd(usage_pct=20.0)  # well under 85% default
    executor = await build_systemd_disk_cleanup_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy(UNIT), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "COMPLETED"
    assert outcome["product_outcome"] == OUTCOME_NO_ACTION_NEEDED
    assert t.starts == 0


async def test_verification_fails_still_above_threshold_after_cleanup(tmp_path):
    cmd = _command()
    t = FakeDiskSystemd(usage_pct=90.0, heal_on_start=False)
    executor = await build_systemd_disk_cleanup_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy(UNIT), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "ESCALATED"
    assert outcome["product_outcome"] == OUTCOME_VERIFICATION_FAILED
    assert t.starts == 1


async def test_duplicate_command_does_not_duplicate_active_execution(tmp_path):
    cmd = _command()
    r = _redis()
    log = _log(tmp_path)
    t = FakeDiskSystemd(usage_pct=90.0, heal_on_start=True)
    executor = await build_systemd_disk_cleanup_executor(
        redis=r, holder="agent-1", transport=t, audit_log=log,
        gate=_gate(), policy=_policy(UNIT), tenant=TENANT)
    state1, _ = await executor(cmd)
    assert state1 == "COMPLETED" and t.starts == 1
    state2, _ = await executor(cmd)
    assert state2 == "COMPLETED"
    assert t.starts == 1


# ── Shadow mode ──────────────────────────────────────────────────────────────

async def test_shadow_mode_produces_recommendation_without_mutation(tmp_path):
    cmd = _command()
    t = FakeDiskSystemd(usage_pct=90.0)
    executor = await build_systemd_disk_cleanup_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy(UNIT), tenant=TENANT, mode=MODE_SHADOW)
    state, outcome = await executor(cmd)
    assert outcome["product_outcome"] == OUTCOME_SHADOW_RECOMMENDATION
    assert t.starts == 0
    assert outcome["evidence"]["would_execute"] == f"systemctl start {UNIT}"


async def test_shadow_mode_still_blocked_by_policy_if_not_allowlisted(tmp_path):
    cmd = _command()
    t = FakeDiskSystemd(usage_pct=90.0)
    executor = await build_systemd_disk_cleanup_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy("other.service"), tenant=TENANT, mode=MODE_SHADOW)
    state, outcome = await executor(cmd)
    assert outcome["product_outcome"] == OUTCOME_BLOCKED_BY_POLICY
    assert t.starts == 0


# ── Env-configurable threshold/path — KHÔNG hardcode ─────────────────────────

def test_threshold_and_path_default_when_env_unset(monkeypatch):
    from aoip.recovery import _disk_cleanup_path, _disk_cleanup_threshold_pct

    monkeypatch.delenv("AOIP_DISK_CLEANUP_THRESHOLD_PCT", raising=False)
    monkeypatch.delenv("AOIP_DISK_CLEANUP_PATH", raising=False)
    assert _disk_cleanup_threshold_pct() == 85.0
    assert _disk_cleanup_path() == "/"


def test_threshold_and_path_read_from_env(monkeypatch):
    from aoip.recovery import _disk_cleanup_path, _disk_cleanup_threshold_pct

    monkeypatch.setenv("AOIP_DISK_CLEANUP_THRESHOLD_PCT", "70")
    monkeypatch.setenv("AOIP_DISK_CLEANUP_PATH", "/var")
    assert _disk_cleanup_threshold_pct() == 70.0
    assert _disk_cleanup_path() == "/var"


def test_threshold_falls_back_to_default_when_env_unparseable(monkeypatch):
    from aoip.recovery import _disk_cleanup_threshold_pct

    monkeypatch.setenv("AOIP_DISK_CLEANUP_THRESHOLD_PCT", "not-a-number")
    assert _disk_cleanup_threshold_pct() == 85.0
    monkeypatch.setenv("AOIP_DISK_CLEANUP_THRESHOLD_PCT", "150")  # out of (0,100]
    assert _disk_cleanup_threshold_pct() == 85.0


async def test_env_threshold_override_changes_is_broken_decision(tmp_path, monkeypatch):
    monkeypatch.setenv("AOIP_DISK_CLEANUP_THRESHOLD_PCT", "30")
    cmd = _command()
    t = FakeDiskSystemd(usage_pct=50.0, heal_on_start=True, post_start_pct=5.0)
    executor = await build_systemd_disk_cleanup_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy(UNIT), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "COMPLETED"
    assert outcome["product_outcome"] == OUTCOME_EXECUTED_AND_VERIFIED
    assert t.starts == 1


def test_disk_usage_pct_unparseable_output_is_fail_closed():
    from aoip.recovery import _parse_disk_usage_pct

    assert _parse_disk_usage_pct("") is None
    assert _parse_disk_usage_pct("Use%\n87%") == 87.0


# ── Tier_gate / risk_taxonomy — capability đi qua đúng gate hiện có ──────────

def test_risk_class_is_registered_and_not_high_fallback():
    from pkg.risk_taxonomy import HIGH, risk_class_of

    risk_class = risk_class_of(CAPABILITY_NAME)
    assert risk_class != HIGH
    assert risk_class == "LOW"
