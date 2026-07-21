"""Tests for the anti-hallucination fixes from the /mnt/mac false-incident post-mortem:

1. Grounding gate (INV_DIAG_GROUNDED) in services/analyst/diagnosis_loop.py —
   paths/percentages in conclusions must appear verbatim in session evidence.
2. Host-share mount scoping in remote_agent/collectors/storage.py.
3. disabled+failed systemd units ignored in remote_agent/collectors/services.py.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from services.analyst.diagnosis_loop import (
    _apply_grounding_gate,
    _extract_groundable_claims,
    _parse_suggested_recovery,
)


# ── 1. Grounding gate ─────────────────────────────────────────────────────────

class TestGroundingGate:
    def test_extracts_paths_and_percentages(self):
        claims = _extract_groundable_claims(
            "Disk /mnt/mac is 95% full — truncate /mnt/mac/vmware/hostd.log"
        )
        assert "/mnt/mac/vmware/hostd.log" in claims
        assert "95%" in claims

    def test_grounded_claims_pass_untouched(self):
        final = {
            "root_cause": "Disk /var/log is 97% full",
            "remediation_steps": ["sudo du -sh /var/log"],
            "confidence": 0.9,
        }
        corpus = "df output: /dev/vdb1 97% /var/log"
        out = _apply_grounding_gate(final, corpus)
        assert out["confidence"] == 0.9
        assert "ungrounded_claims" not in out
        assert out["root_cause"] == final["root_cause"]

    def test_ungrounded_root_cause_flagged_and_confidence_capped(self):
        # Replay of the real incident: 95% + inode claim never measured
        final = {
            "root_cause": "Disk /mnt/mac is 95% full — inode exhaustion confirmed",
            "remediation_steps": [],
            "confidence": 0.9,
        }
        corpus = "df -h: /dev/vdb1 47G 27G 21G 57% /"
        out = _apply_grounding_gate(final, corpus)
        assert out["confidence"] <= 0.3
        assert "95%" in out["ungrounded_claims"]
        assert out["root_cause"].startswith("[UNVERIFIED:")

    def test_ungrounded_remediation_step_dropped(self):
        final = {
            "root_cause": "journal too large",
            "remediation_steps": [
                "sudo journalctl --vacuum-size=500M",
                "sudo truncate -s 0 /mnt/mac/vmware/hostd.log",
            ],
            "confidence": 0.9,
        }
        corpus = "journalctl --disk-usage: 4G"
        out = _apply_grounding_gate(final, corpus)
        assert "sudo journalctl --vacuum-size=500M" in out["remediation_steps"]
        assert not any("hostd.log" in s for s in out["remediation_steps"])
        assert out["dropped_remediation_steps"] == [
            "sudo truncate -s 0 /mnt/mac/vmware/hostd.log"
        ]
        assert out["confidence"] <= 0.3

    def test_returns_new_dict_no_mutation(self):
        final = {"root_cause": "x /a/b 99%", "remediation_steps": [], "confidence": 0.8}
        out = _apply_grounding_gate(final, "")
        assert final["root_cause"] == "x /a/b 99%"
        assert final["confidence"] == 0.8
        assert out is not final

    def test_grounded_suggested_recovery_passes_untouched(self):
        final = {
            "root_cause": "payment-api.service is inactive",
            "remediation_steps": [],
            "confidence": 0.9,
            "suggested_recovery": {"capability": "systemd.restart_unit", "unit": "payment-api.service"},
        }
        corpus = "systemctl status payment-api.service: Active: inactive (dead)"
        out = _apply_grounding_gate(final, corpus)
        assert out["suggested_recovery"] == {
            "capability": "systemd.restart_unit", "unit": "payment-api.service",
        }
        assert out["confidence"] == 0.9

    def test_ungrounded_suggested_recovery_unit_dropped(self):
        """A unit name the LLM invented (never appeared in this session's
        command output) must never reach the automated-dispatch bridge —
        stripped to None even when the rest of the conclusion is grounded."""
        final = {
            "root_cause": "disk /var/log is 97% full",
            "remediation_steps": ["sudo du -sh /var/log"],
            "confidence": 0.9,
            "suggested_recovery": {"capability": "systemd.restart_unit", "unit": "phantom-service.service"},
        }
        corpus = "df output: /dev/vdb1 97% /var/log"
        out = _apply_grounding_gate(final, corpus)
        assert out["suggested_recovery"] is None
        # the rest of the conclusion, which WAS grounded, is untouched
        assert out["root_cause"] == final["root_cause"]
        assert out["confidence"] == 0.9

    def test_none_suggested_recovery_is_a_noop(self):
        final = {"root_cause": "x", "remediation_steps": [], "confidence": 0.9,
                 "suggested_recovery": None}
        out = _apply_grounding_gate(final, "")
        assert out["suggested_recovery"] is None

    def test_grounded_reset_failed_suggestion_passes_untouched(self):
        """Capability #2 (reset_failed) goes through the SAME grounding check
        as restart_unit — the gate only inspects .unit, not .capability."""
        final = {
            "root_cause": "payment-api.service stuck in failed state, mariadb dependency now active",
            "remediation_steps": [],
            "confidence": 0.9,
            "suggested_recovery": {"capability": "systemd.reset_failed", "unit": "payment-api.service"},
        }
        corpus = "systemctl is-failed payment-api.service: failed\nsystemctl is-active mariadb: active"
        out = _apply_grounding_gate(final, corpus)
        assert out["suggested_recovery"] == {
            "capability": "systemd.reset_failed", "unit": "payment-api.service",
        }
        assert out["confidence"] == 0.9

    def test_ungrounded_reset_failed_unit_dropped(self):
        final = {
            "root_cause": "disk /var/log is 97% full",
            "remediation_steps": ["sudo du -sh /var/log"],
            "confidence": 0.9,
            "suggested_recovery": {"capability": "systemd.reset_failed", "unit": "phantom-service.service"},
        }
        corpus = "df output: /dev/vdb1 97% /var/log"
        out = _apply_grounding_gate(final, corpus)
        assert out["suggested_recovery"] is None

    def test_grounded_journal_vacuum_suggestion_passes_untouched(self):
        """Capability #3 (journal_vacuum) is fixed-target — its unit is
        ALWAYS 'systemd-journald.service', which is grounded against that one
        valid literal instead of the evidence corpus substring check the
        other two capabilities use (the corpus here deliberately never
        contains the literal unit string, proving the gate does NOT
        substring-match for this capability)."""
        final = {
            "root_cause": "journal disk usage over threshold, confirmed by journalctl --disk-usage",
            "remediation_steps": [],
            "confidence": 0.9,
            "suggested_recovery": {"capability": "systemd.journal_vacuum",
                                   "unit": "systemd-journald.service"},
        }
        corpus = "journalctl --disk-usage: Archived and active journals take up 3.0G in the file system."
        out = _apply_grounding_gate(final, corpus)
        assert out["suggested_recovery"] == {
            "capability": "systemd.journal_vacuum", "unit": "systemd-journald.service",
        }
        assert out["confidence"] == 0.9

    def test_journal_vacuum_wrong_unit_dropped_even_if_in_corpus(self):
        """The fixed-literal check is STRICTER than substring-matching: even
        if the LLM's hallucinated unit happens to appear in the evidence
        corpus for some other reason, journal_vacuum must only ever target
        systemd-journald.service."""
        final = {
            "root_cause": "journal disk usage over threshold",
            "remediation_steps": [],
            "confidence": 0.9,
            "suggested_recovery": {"capability": "systemd.journal_vacuum", "unit": "nginx.service"},
        }
        corpus = "systemctl status nginx.service: active\njournalctl --disk-usage: 3.0G"
        out = _apply_grounding_gate(final, corpus)
        assert out["suggested_recovery"] is None

    def test_journal_vacuum_unit_not_required_in_corpus(self):
        """Unlike restart_unit/reset_failed, the correct journal_vacuum unit
        passes even when the literal string never appears anywhere in the
        evidence corpus — it is validated against the fixed constant, not
        evidence text."""
        final = {
            "root_cause": "journal disk usage over threshold",
            "remediation_steps": [],
            "confidence": 0.9,
            "suggested_recovery": {"capability": "systemd.journal_vacuum",
                                   "unit": "systemd-journald.service"},
        }
        corpus = "df -h: /dev/vdb1 47G 45G 1.0G 96% /"
        out = _apply_grounding_gate(final, corpus)
        assert out["suggested_recovery"] == {
            "capability": "systemd.journal_vacuum", "unit": "systemd-journald.service",
        }


class TestParseSuggestedRecovery:
    def test_none_when_not_a_dict(self):
        assert _parse_suggested_recovery(None) is None
        assert _parse_suggested_recovery("payment-api.service") is None

    def test_none_when_capability_not_restart_unit(self):
        raw = {"capability": "systemd.stop_unit", "unit": "payment-api.service"}
        assert _parse_suggested_recovery(raw) is None

    def test_none_when_unit_missing_or_blank(self):
        assert _parse_suggested_recovery({"capability": "systemd.restart_unit"}) is None
        assert _parse_suggested_recovery(
            {"capability": "systemd.restart_unit", "unit": "  "}
        ) is None

    def test_valid_shape_parsed(self):
        raw = {"capability": "systemd.restart_unit", "unit": "payment-api.service"}
        assert _parse_suggested_recovery(raw) == raw

    def test_reset_failed_capability_accepted(self):
        raw = {"capability": "systemd.reset_failed", "unit": "payment-api.service"}
        assert _parse_suggested_recovery(raw) == raw

    def test_none_when_unit_missing_for_reset_failed(self):
        assert _parse_suggested_recovery({"capability": "systemd.reset_failed"}) is None

    def test_journal_vacuum_capability_accepted(self):
        raw = {"capability": "systemd.journal_vacuum", "unit": "systemd-journald.service"}
        assert _parse_suggested_recovery(raw) == raw

    def test_none_when_unit_missing_for_journal_vacuum(self):
        assert _parse_suggested_recovery({"capability": "systemd.journal_vacuum"}) is None


# ── 2. Host-share mount scoping ───────────────────────────────────────────────

_DF_WITH_HOST_SHARE = (
    "Filesystem     Type     Size  Used Avail Use% Mounted on\n"
    "/dev/vdb1      ext4      47G   27G   21G  57% /\n"
    "mac            virtiofs 461G  444G   18G  97% /mnt/mac\n"
    "/dev/vdb1      ext4      47G   27G   21G  57% /mnt/machines/docker/volumes/bf4b\n"
)


class TestHostShareScoping:
    @pytest.mark.asyncio
    async def test_host_share_mount_not_critical(self):
        from remote_agent.collectors.storage import collect_disk_usage

        async def fake_run(cmd, timeout=10.0):
            if "-i" in cmd:
                return "", "", 1
            return _DF_WITH_HOST_SHARE, "", 0

        with patch("remote_agent.collectors.storage._run", side_effect=fake_run):
            env = await collect_disk_usage("cust-edge")

        fact = env["extracted_fact"]
        assert fact["critical_partitions"] == []
        assert "/mnt/mac" in fact["host_share_excluded"]
        assert env["result"] == "PASSED"
        mounts = [p["mount"] for p in fact["partitions"]]
        assert "/mnt/mac" not in mounts

    @pytest.mark.asyncio
    async def test_real_vm_partition_still_critical(self):
        from remote_agent.collectors.storage import collect_disk_usage

        df = (
            "Filesystem     Type     Size  Used Avail Use% Mounted on\n"
            "/dev/vdb1      ext4      47G   45G  1.0G  96% /\n"
            "mac            virtiofs 461G  444G   18G  97% /mnt/mac\n"
        )

        async def fake_run(cmd, timeout=10.0):
            if "-i" in cmd:
                return "", "", 1
            return df, "", 0

        with patch("remote_agent.collectors.storage._run", side_effect=fake_run):
            env = await collect_disk_usage("cust-edge")

        assert env["extracted_fact"]["critical_partitions"] == ["/(96%)"]
        assert env["result"] == "FAILED"


# ── 3. systemd disabled+failed residue ────────────────────────────────────────

class TestSystemdDisabledResidue:
    @pytest.mark.asyncio
    async def test_disabled_failed_unit_ignored(self):
        from remote_agent.collectors.services import collect_systemd_units

        async def fake_run(cmd, stdin=None, timeout=8.0):
            if cmd[:2] == ["systemctl", "list-units"]:
                return "omni-remote-agent.service loaded failed failed Omni Remote Agent\n", "", 0
            if cmd[:2] == ["systemctl", "is-enabled"]:
                return "disabled\n", "", 1
            return "", "", 0

        with patch("remote_agent.collectors.services._run", side_effect=fake_run):
            env = await collect_systemd_units("cust-edge")

        fact = env["extracted_fact"]
        assert fact["failed_units"] == []
        assert fact["ignored_disabled_units"] == ["omni-remote-agent"]
        assert env["result"] == "PASSED"

    @pytest.mark.asyncio
    async def test_enabled_failed_unit_still_reported(self):
        from remote_agent.collectors.services import collect_systemd_units

        async def fake_run(cmd, stdin=None, timeout=8.0):
            if cmd[:2] == ["systemctl", "list-units"]:
                return "nginx.service loaded failed failed nginx\n", "", 0
            if cmd[:2] == ["systemctl", "is-enabled"]:
                return "enabled\n", "", 0
            return "", "", 0

        with patch("remote_agent.collectors.services._run", side_effect=fake_run), \
             patch("remote_agent.collectors.services.pkg_origin.get_fragment_path",
                   AsyncMock(return_value="/usr/lib/systemd/system/nginx.service")), \
             patch("remote_agent.collectors.services.pkg_origin.classify_unit_origin",
                   AsyncMock(return_value="package:nginx-common")):
            env = await collect_systemd_units("cust-edge")

        fact = env["extracted_fact"]
        assert fact["failed_units"] == ["nginx"]
        assert env["result"] == "FAILED"
        assert env["lane"] == "SYS_HARD_FAIL"
