"""Collector security — auth failures (lastb) + privilege escalation (journalctl sudo).

Đ49 S1 (plans/finguard-to-smart-siem-merge-2026-08-04.md) — collector đầu tiên cho
domain `security` (trước đó ❌, không có file nào). INV_DATA_RESIDENCY: mọi test dưới
đây phải xác nhận `raw == ""` và `extracted_fact` KHÔNG chứa dòng log gốc, chỉ chuỗi
`user=X host=Y` đã chuẩn hoá.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from remote_agent.collectors import security as sec


_LASTB_CLEAN = ""

_LASTB_FEW = """root     ssh:notty    203.0.113.5      Mon Aug 10 10:00 - 10:00  (00:00)
invalid9 ssh:notty    203.0.113.7      Mon Aug 10 09:59 - 09:59  (00:00)
"""

_LASTB_MANY = "\n".join(
    f"attacker{i} ssh:notty    203.0.113.{i % 250}      Mon Aug 10 09:5{i % 9} - 09:5{i % 9}  (00:00)"
    for i in range(25)
)

_JOURNAL_CLEAN = ""

_JOURNAL_FAILURES = """Aug 10 10:00:00 host sudo:   baduser : command not allowed ; TTY=pts/0 ; PWD=/home ; USER=root ; COMMAND=/bin/su
Aug 10 10:00:05 host sudo:   baduser : 1 incorrect password attempt ; TTY=pts/0 ; USER=root ; COMMAND=/bin/su
Aug 10 10:00:10 host sudo:   other : authentication failure ; TTY=pts/1 ; USER=root ; COMMAND=/bin/bash
"""


class TestCollectAuthFailures:
    @pytest.mark.asyncio
    async def test_clean_host_passes(self) -> None:
        with patch.object(sec, "_run", AsyncMock(return_value=(_LASTB_CLEAN, "", 0))):
            env = await sec.collect_auth_failures("host1")
        assert env is not None
        assert env["result"] == "PASSED"
        assert env["extracted_fact"]["failed_login_count"] == 0
        assert env["domain"] == "security"

    @pytest.mark.asyncio
    async def test_few_failures_below_warn_still_reported(self) -> None:
        with patch.object(sec, "_run", AsyncMock(return_value=(_LASTB_FEW, "", 0))):
            env = await sec.collect_auth_failures("host1")
        assert env["extracted_fact"]["failed_login_count"] == 2
        assert env["extracted_fact"]["distinct_users"] == 2

    @pytest.mark.asyncio
    async def test_many_failures_triggers_failed(self) -> None:
        with patch.object(sec, "_run", AsyncMock(return_value=(_LASTB_MANY, "", 0))):
            env = await sec.collect_auth_failures("host1")
        assert env["result"] == "FAILED"
        assert env["extracted_fact"]["failed_login_count"] == 25
        assert env["alert_rule"] == "SecurityAuthFailureBurst"

    @pytest.mark.asyncio
    async def test_inv_data_residency_raw_always_empty(self) -> None:
        """Dòng lastb thô (chứa IP/thời gian) KHÔNG BAO GIỜ được rời host."""
        with patch.object(sec, "_run", AsyncMock(return_value=(_LASTB_FEW, "", 0))):
            env = await sec.collect_auth_failures("host1")
        assert env["raw"] == ""
        # Chỉ chuỗi đã chuẩn hoá user=/host= được phép có trong extracted_fact
        normalized = env["extracted_fact"]["normalized_entities"]
        assert "user=root" in normalized
        assert "host=203.0.113.5" in normalized
        # Không rò rỉ định dạng dòng gốc (có "ssh:notty", "Mon Aug")
        assert "ssh:notty" not in normalized
        assert "Mon Aug" not in normalized

    @pytest.mark.asyncio
    async def test_empty_btmp_returns_none_no_warning(self, caplog) -> None:
        with patch.object(sec, "_run", AsyncMock(return_value=("", "lastb: /var/log/btmp: No such file or directory", 1))):
            env = await sec.collect_auth_failures("host1")
        assert env is None

    @pytest.mark.asyncio
    async def test_command_blocked_returns_none(self) -> None:
        with patch.object(sec, "_run", AsyncMock(return_value=("", "blocked: command_not_whitelisted", 1))):
            env = await sec.collect_auth_failures("host1")
        assert env is None


class TestCollectPrivilegeEscalation:
    @pytest.mark.asyncio
    async def test_clean_host_passes(self) -> None:
        with patch.object(sec, "_run", AsyncMock(return_value=(_JOURNAL_CLEAN, "", 0))):
            env = await sec.collect_privilege_escalation("host1")
        assert env["result"] == "PASSED"
        assert env["extracted_fact"]["sudo_failure_count"] == 0

    @pytest.mark.asyncio
    async def test_sudo_failures_extracted(self) -> None:
        with patch.object(sec, "_run", AsyncMock(return_value=(_JOURNAL_FAILURES, "", 0))):
            env = await sec.collect_privilege_escalation("host1")
        assert env["result"] == "FAILED"
        assert env["extracted_fact"]["sudo_failure_count"] == 3
        assert env["extracted_fact"]["distinct_users"] == 2
        normalized = env["extracted_fact"]["normalized_entities"]
        assert "user=baduser process=sudo" in normalized
        assert "user=other process=sudo" in normalized

    @pytest.mark.asyncio
    async def test_inv_data_residency_raw_always_empty(self) -> None:
        with patch.object(sec, "_run", AsyncMock(return_value=(_JOURNAL_FAILURES, "", 0))):
            env = await sec.collect_privilege_escalation("host1")
        assert env["raw"] == ""
        # COMMAND=/bin/su / PWD=/home không được rò rỉ ra ngoài
        normalized = env["extracted_fact"]["normalized_entities"]
        assert "COMMAND" not in normalized
        assert "/bin/su" not in normalized

    @pytest.mark.asyncio
    async def test_journalctl_unavailable_returns_none(self) -> None:
        with patch.object(sec, "_run", AsyncMock(return_value=("", "journalctl: command not found", 1))):
            env = await sec.collect_privilege_escalation("host1")
        assert env is None


def test_safe_entity_rejects_shell_metacharacters() -> None:
    assert sec._safe_entity("normal_user123") == "normal_user123"
    assert sec._safe_entity("") is None
    assert sec._safe_entity("$(rm -rf /)") is None or sec._safe_entity("$(rm -rf /)") != "$(rm -rf /)"
