"""M3 — Human-approved Systemd Journal Vacuum (capability #3 of the VM/AOIP lane,
first auto-remediation for the SYS_RESOURCE lane).

Mirrors tests/test_capability_systemd_reset_failed.py's structure: typed payload →
preflight (capability/version/hash/allowlist/unit-exists) → guarded execution
(SAME lease/ledger/fencing infra as the other two capabilities, not bypassed) →
verification → structured product outcome. KHÔNG mutation OS thật — FakeJournalSystemd
transport.

Unlike reset_failed/restart_unit, this capability's target unit is ALWAYS the fixed
literal "systemd-journald.service" and the fixture models
`journalctl --disk-usage`/`journalctl --vacuum-size=` rather than `is-active`/
`is-failed`/`restart`/`reset-failed`.
"""
from __future__ import annotations

import time

import fakeredis.aioredis as aioredis
import pytest

from aoip import audit
from aoip.capabilities.systemd_journal_vacuum import (
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
    build_systemd_journal_vacuum_executor,
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
UNIT = "systemd-journald.service"

_TWO_GIB = 2 * 1024**3
_TWO_HUNDRED_MIB = 200 * 1024**2


def _redis():
    return aioredis.FakeRedis(decode_responses=True)


def _log(tmp_path, name="a.jsonl"):
    return audit.FileAuditLog(tmp_path / name)


def _gate():
    return RecoveryGate(allowed_failure_modes=frozenset({"disk_pressure_journal"}),
                        allowed_substrates=frozenset({"systemd"}), max_risk=0.5,
                        scope_prefix="svc:", min_diagnosis_confidence=0.0, max_diagnosis_age_s=1e9,
                        allowed_targets=frozenset({UNIT, "other.service"}))


def _policy(*units):
    return SystemdRestartPolicy(allowed_units=frozenset(units))


class FakeJournalSystemd:
    """Transport giả — KHÔNG chạm OS thật. Hỗ trợ --disk-usage/--vacuum-size=/LoadState.

    CỐ Ý không có restart/is-active/is-failed heal path — capability này chỉ dọn
    dữ liệu journal qua journalctl chính thức, không bao giờ start/stop/restart
    tiến trình nào.
    """

    target = "h"

    def __init__(self, *, disk_usage_bytes=3 * 1024**3, exists=True, vacuum_rc=0,
                heal_on_vacuum=True, post_vacuum_bytes=50 * 1024**2):
        self.disk_usage_bytes = disk_usage_bytes
        self.exists = exists
        self.vacuum_rc = vacuum_rc
        self.heal_on_vacuum = heal_on_vacuum
        self.post_vacuum_bytes = post_vacuum_bytes
        self.vacuums = 0

    def _disk_usage_text(self) -> str:
        gib = self.disk_usage_bytes / (1024**3)
        return f"Archived and active journals take up {gib:.2f}G in the file system.\n"

    async def run(self, argv, *, timeout=15.0):
        cmd = " ".join(argv)
        if "LoadState" in cmd:
            return ("loaded\n" if self.exists else "not-found\n", 0)
        if "--vacuum-size=" in cmd:
            self.vacuums += 1
            if self.heal_on_vacuum and self.vacuum_rc == 0:
                self.disk_usage_bytes = self.post_vacuum_bytes
            return ("Vacuuming done, freed 0B of archived journals.\n", self.vacuum_rc)
        if "--disk-usage" in cmd:
            return (self._disk_usage_text(), 0)
        return ("", 0)


def _command(*, unit=UNIT, tenant=TENANT, ttl_s=300, approver="alice", approved=True):
    typed = build_typed_payload(mission_id="mis-1", decision_id="dec-1", incident_id="inc-1",
                                summary="journal disk usage 3.0G over threshold", unit=unit)
    findings = (Finding(claim=f"svc:{unit} disk_pressure_journal (journalctl --disk-usage 3.0G)",
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


def test_capability_name_is_distinct_from_other_two():
    assert CAPABILITY_NAME == "systemd.journal_vacuum"
    assert CAPABILITY_NAME not in ("systemd.restart_unit", "systemd.reset_failed")


@pytest.mark.parametrize("unit", ["systemd-journald.service", "nginx.service", "app@1.service"])
def test_valid_unit_names_accepted(unit):
    assert validate_unit_name(unit) is None


@pytest.mark.parametrize("unit", [
    "systemd-journald", "../etc/passwd.service", "systemd-journald.service; rm -rf /", "",
])
def test_invalid_unit_names_rejected(unit):
    assert validate_unit_name(unit) is not None


def test_payload_hash_changes_when_any_field_changes():
    typed = build_typed_payload(mission_id="m", decision_id="d", incident_id="i",
                                summary="s", unit=UNIT)
    h1 = capability_payload_hash(typed)
    typed2 = {**typed, "reason": {**typed["reason"], "summary": "changed"}}
    h2 = capability_payload_hash(typed2)
    assert h1 != h2


def test_payload_hash_distinct_from_other_capabilities_for_same_unit():
    """Same unit, different capability → different hash (capability name is
    part of the hashed contract, so an approval for one can never decode as
    another)."""
    from aoip.capabilities.systemd_reset_failed import build_typed_payload as reset_failed_typed
    from aoip.capabilities.systemd_restart import build_typed_payload as restart_typed

    vacuum_typed = build_typed_payload(mission_id="m", decision_id="d", incident_id="i",
                                       summary="s", unit=UNIT)
    reset_payload = reset_failed_typed(mission_id="m", decision_id="d", incident_id="i",
                                       summary="s", unit=UNIT)
    restart_payload = restart_typed(mission_id="m", decision_id="d", incident_id="i",
                                    summary="s", unit=UNIT)
    assert capability_payload_hash(vacuum_typed) != capability_payload_hash(reset_payload)
    assert capability_payload_hash(vacuum_typed) != capability_payload_hash(restart_payload)


async def test_payload_mutated_after_approval_invalidates_command(tmp_path):
    cmd = _command()
    cmd["target"] = {"unit": "other.service"}  # kẻ tấn công đổi target sau approval
    executor = await build_systemd_journal_vacuum_executor(
        redis=_redis(), holder="agent-1", transport=FakeJournalSystemd(), audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy(UNIT, "other.service"), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "FAILED"
    assert outcome["product_outcome"] == OUTCOME_PRECONDITION_FAILED
    assert "payload_hash_mismatch" in outcome["reason"]


async def test_unsupported_capability_version_fails_closed(tmp_path):
    cmd = _command()
    cmd["capability_version"] = "999"
    executor = await build_systemd_journal_vacuum_executor(
        redis=_redis(), holder="agent-1", transport=FakeJournalSystemd(), audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy(UNIT), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "FAILED"
    assert outcome["product_outcome"] == OUTCOME_UNSUPPORTED_CAPABILITY


# ── Policy reuse (SAME env var as restart_unit/reset_failed — no new env var) ─

def test_reuses_same_allowlist_env_var_as_restart_unit():
    policy = load_policy_from_env({"AOIP_ALLOWED_SYSTEMD_UNITS": UNIT})
    assert policy.is_allowed(UNIT)
    assert not policy.is_allowed("other.service")


async def test_unit_not_allowlisted_blocked_by_policy(tmp_path):
    cmd = _command()
    executor = await build_systemd_journal_vacuum_executor(
        redis=_redis(), holder="agent-1", transport=FakeJournalSystemd(),
        audit_log=_log(tmp_path), gate=_gate(), policy=_policy("other.service"), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "FAILED"
    assert outcome["product_outcome"] == OUTCOME_BLOCKED_BY_POLICY


async def test_approval_rejected_blocks_execution(tmp_path):
    cmd = _command(approved=False)
    t = FakeJournalSystemd()
    executor = await build_systemd_journal_vacuum_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy(UNIT), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "FAILED"
    assert outcome["product_outcome"] == OUTCOME_APPROVAL_REJECTED
    assert t.vacuums == 0


# ── Execution / verification ─────────────────────────────────────────────────

async def test_unit_missing_precondition_failed(tmp_path):
    cmd = _command()
    t = FakeJournalSystemd(exists=False)
    executor = await build_systemd_journal_vacuum_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy(UNIT), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "FAILED"
    assert outcome["product_outcome"] == OUTCOME_PRECONDITION_FAILED
    assert t.vacuums == 0


async def test_happy_path_executed_and_verified(tmp_path):
    """Journal disk usage above threshold → vacuum clears it → verified below
    threshold → COMPLETED. Zero process mutation: the fake never toggles unit
    active/failed state, only journal disk usage."""
    cmd = _command()
    t = FakeJournalSystemd(disk_usage_bytes=3 * 1024**3, heal_on_vacuum=True,
                           post_vacuum_bytes=50 * 1024**2)
    executor = await build_systemd_journal_vacuum_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy(UNIT), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "COMPLETED"
    assert outcome["product_outcome"] == OUTCOME_EXECUTED_AND_VERIFIED
    assert t.vacuums == 1
    assert outcome["attempted"] == f"journal-vacuum {UNIT}"
    assert outcome["next_step"]


async def test_already_below_threshold_no_action_needed(tmp_path):
    """Journal disk usage is ALREADY below threshold — the generic
    current-state gate (op.is_broken) aborts with zero mutation, same
    mechanism the other two capabilities use for 'already healthy'."""
    cmd = _command()
    t = FakeJournalSystemd(disk_usage_bytes=10 * 1024**2)  # 10MiB, well under 2GiB default
    executor = await build_systemd_journal_vacuum_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy(UNIT), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "COMPLETED"
    assert outcome["product_outcome"] == OUTCOME_NO_ACTION_NEEDED
    assert t.vacuums == 0


async def test_verification_fails_still_above_threshold_after_vacuum(tmp_path):
    cmd = _command()
    t = FakeJournalSystemd(disk_usage_bytes=3 * 1024**3, heal_on_vacuum=False)  # rc=0 nhưng KHÔNG giảm
    executor = await build_systemd_journal_vacuum_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy(UNIT), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "ESCALATED"
    assert outcome["product_outcome"] == OUTCOME_VERIFICATION_FAILED
    assert t.vacuums == 1


async def test_duplicate_command_does_not_duplicate_active_execution(tmp_path):
    """Idempotency ledger (đã có, KHÔNG viết lại) chặn re-mutation nếu cùng command chạy lại."""
    cmd = _command()
    r = _redis()
    log = _log(tmp_path)
    t = FakeJournalSystemd(disk_usage_bytes=3 * 1024**3, heal_on_vacuum=True)
    executor = await build_systemd_journal_vacuum_executor(
        redis=r, holder="agent-1", transport=t, audit_log=log,
        gate=_gate(), policy=_policy(UNIT), tenant=TENANT)
    state1, _ = await executor(cmd)
    assert state1 == "COMPLETED" and t.vacuums == 1
    state2, _ = await executor(cmd)
    assert state2 == "COMPLETED"
    assert t.vacuums == 1  # KHÔNG vacuum lần 2


# ── Shadow mode ──────────────────────────────────────────────────────────────

async def test_shadow_mode_produces_recommendation_without_mutation(tmp_path):
    cmd = _command()
    t = FakeJournalSystemd(disk_usage_bytes=3 * 1024**3)
    executor = await build_systemd_journal_vacuum_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy(UNIT), tenant=TENANT, mode=MODE_SHADOW)
    state, outcome = await executor(cmd)
    assert outcome["product_outcome"] == OUTCOME_SHADOW_RECOMMENDATION
    assert t.vacuums == 0
    assert outcome["evidence"]["would_execute"] == "journalctl --vacuum-size=200M"
    assert "predicted_verification_plan" in outcome["evidence"]


async def test_shadow_mode_still_blocked_by_policy_if_not_allowlisted(tmp_path):
    cmd = _command()
    t = FakeJournalSystemd(disk_usage_bytes=3 * 1024**3)
    executor = await build_systemd_journal_vacuum_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy("other.service"), tenant=TENANT, mode=MODE_SHADOW)
    state, outcome = await executor(cmd)
    assert outcome["product_outcome"] == OUTCOME_BLOCKED_BY_POLICY
    assert t.vacuums == 0


# ── Env-configurable threshold/target — KHÔNG hardcode ────────────────────────

def test_threshold_and_target_default_when_env_unset(monkeypatch):
    from aoip.recovery import _journal_vacuum_target_size, _journal_vacuum_threshold_bytes

    monkeypatch.delenv("AOIP_JOURNAL_VACUUM_THRESHOLD_BYTES", raising=False)
    monkeypatch.delenv("AOIP_JOURNAL_VACUUM_TARGET_SIZE", raising=False)
    assert _journal_vacuum_threshold_bytes() == _TWO_GIB
    assert _journal_vacuum_target_size() == "200M"


def test_threshold_and_target_read_from_env(monkeypatch):
    from aoip.recovery import _journal_vacuum_target_size, _journal_vacuum_threshold_bytes

    monkeypatch.setenv("AOIP_JOURNAL_VACUUM_THRESHOLD_BYTES", str(_TWO_HUNDRED_MIB))
    monkeypatch.setenv("AOIP_JOURNAL_VACUUM_TARGET_SIZE", "50M")
    assert _journal_vacuum_threshold_bytes() == _TWO_HUNDRED_MIB
    assert _journal_vacuum_target_size() == "50M"


def test_threshold_falls_back_to_default_when_env_unparseable(monkeypatch):
    from aoip.recovery import _journal_vacuum_threshold_bytes

    monkeypatch.setenv("AOIP_JOURNAL_VACUUM_THRESHOLD_BYTES", "not-a-number")
    assert _journal_vacuum_threshold_bytes() == _TWO_GIB


async def test_env_threshold_override_changes_is_broken_decision(tmp_path, monkeypatch):
    """Lowering the threshold via env makes a previously-'healthy' disk usage
    figure trigger a real vacuum — proves the operator reads env at call
    time, not a hardcoded constant."""
    monkeypatch.setenv("AOIP_JOURNAL_VACUUM_THRESHOLD_BYTES", str(10 * 1024**2))  # 10MiB
    cmd = _command()
    t = FakeJournalSystemd(disk_usage_bytes=20 * 1024**2, heal_on_vacuum=True,
                           post_vacuum_bytes=1024**2)  # 20MiB > 10MiB threshold
    executor = await build_systemd_journal_vacuum_executor(
        redis=_redis(), holder="agent-1", transport=t, audit_log=_log(tmp_path),
        gate=_gate(), policy=_policy(UNIT), tenant=TENANT)
    state, outcome = await executor(cmd)
    assert state == "COMPLETED"
    assert outcome["product_outcome"] == OUTCOME_EXECUTED_AND_VERIFIED
    assert t.vacuums == 1


# ── Tier_gate / risk_taxonomy — capability đi qua đúng gate hiện có ──────────

def test_risk_class_is_registered_and_not_high_fallback():
    from pkg.risk_taxonomy import HIGH, risk_class_of

    risk_class = risk_class_of(CAPABILITY_NAME)
    assert risk_class != HIGH  # missing-from-table fail-closed default — must NOT apply here
    assert risk_class == "LOW"
