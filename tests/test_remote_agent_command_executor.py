"""Tests for remote_agent.command_executor — INV_READONLY_CMDS / INV_NO_DATA_EXFIL guard.

This is the single most security-critical module in remote_agent: it is the
last line of defense between a command pushed by Omni (or a compromised
gateway) and an arbitrary shell on a customer VM. Every branch of
_is_command_allowed must be covered, plus execute_command/execute_batch
plumbing (timeout, exception, UPDATE_AGENT routing).
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from remote_agent.command_executor import (
    COMMAND_WHITELIST,
    _CONTENT_READ_BLOCKED,
    _is_command_allowed,
    execute_batch,
    execute_command,
)


class TestIsCommandAllowed:
    def test_whitelisted_command_with_safe_args_allowed(self):
        allowed, reason = _is_command_allowed("df", ["-h"])
        assert allowed is True
        assert reason == ""

    def test_non_whitelisted_command_blocked(self):
        allowed, reason = _is_command_allowed("rm", ["-rf", "/tmp/x"])
        assert allowed is False
        assert "command_not_whitelisted" in reason

    @pytest.mark.parametrize("blocked_cmd", sorted(_CONTENT_READ_BLOCKED))
    def test_content_read_commands_blocked(self, blocked_cmd):
        allowed, reason = _is_command_allowed(blocked_cmd, [])
        assert allowed is False
        assert "data_exfil_blocked" in reason

    @pytest.mark.parametrize("metachar_args", [
        ["foo;rm", "-rf"],
        ["foo|bar"],
        ["foo&&bar"],
        ["foo`bar`"],
        ["foo$(bar)"],
        ["foo${bar}"],
        ["foo>bar"],
        ["foo<bar"],
    ])
    def test_shell_injection_metacharacters_blocked(self, metachar_args):
        allowed, reason = _is_command_allowed("ls", metachar_args)
        assert allowed is False
        assert "shell_injection_detected" in reason

    def test_leading_slash_and_path_stripped_before_whitelist_check(self):
        allowed, _ = _is_command_allowed("/usr/bin/df", ["-h"])
        assert allowed is True

    @pytest.mark.parametrize("flag", ["-exec", "-execdir", "-delete", "-fprint", "-fprintf", "-fls", "-ok", "-okdir", "-cat"])
    def test_find_dangerous_flags_blocked(self, flag):
        allowed, reason = _is_command_allowed("find", ["/tmp", flag, "echo"])
        assert allowed is False
        assert "find_dangerous_flag_blocked" in reason

    def test_find_safe_flags_allowed(self):
        allowed, _ = _is_command_allowed("find", ["/var/log", "-maxdepth", "2", "-name", "*.log"])
        assert allowed is True

    @pytest.mark.parametrize("subcmd", ["status", "is-active", "is-enabled", "is-failed", "list-units", "show", "cat", "help"])
    def test_systemctl_readonly_subcommands_allowed(self, subcmd):
        allowed, _ = _is_command_allowed("systemctl", [subcmd, "nginx"])
        assert allowed is True

    @pytest.mark.parametrize("subcmd", ["start", "stop", "restart", "reload", "enable", "disable", "mask", "kill", "daemon-reload", "edit"])
    def test_systemctl_write_subcommands_blocked(self, subcmd):
        allowed, reason = _is_command_allowed("systemctl", [subcmd, "nginx"])
        assert allowed is False
        assert "systemctl_write_subcommand_blocked" in reason

    def test_systemctl_unknown_subcommand_blocked(self):
        allowed, reason = _is_command_allowed("systemctl", ["frobnicate", "nginx"])
        assert allowed is False
        assert "systemctl_subcommand_not_allowed" in reason

    def test_systemctl_with_no_args_allowed(self):
        # no subcommand at all — nothing to validate, falls through
        allowed, _ = _is_command_allowed("systemctl", [])
        assert allowed is True

    @pytest.mark.parametrize("flag", ["--start", "--stop", "--restart", "--enable", "--kill"])
    def test_global_write_flag_blocked_on_any_whitelisted_command(self, flag):
        allowed, reason = _is_command_allowed("ps", [flag])
        assert allowed is False
        assert "write_flag_blocked" in reason

    def test_whitelist_and_blocklist_are_disjoint(self):
        assert COMMAND_WHITELIST.isdisjoint(_CONTENT_READ_BLOCKED)


class TestExecuteCommand:
    @pytest.mark.asyncio
    async def test_blocked_command_returns_blocked_result_without_subprocess(self):
        with patch("remote_agent.command_executor.asyncio.create_subprocess_exec") as mock_exec:
            result = await execute_command("c1", "cat", ["/etc/shadow"])
        mock_exec.assert_not_called()
        assert result["blocked"] is True
        assert result["rc"] == -1
        assert "data_exfil_blocked" in result["block_reason"]

    @pytest.mark.asyncio
    async def test_allowed_command_runs_subprocess_and_returns_output(self):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"total 0\n", b""))
        proc.returncode = 0
        with patch("remote_agent.command_executor.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            result = await execute_command("c2", "ls", ["-la", "/tmp"])
        assert result["blocked"] is False
        assert result["rc"] == 0
        assert result["stdout"] == "total 0\n"

    @pytest.mark.asyncio
    async def test_timeout_returns_rc_1_with_timeout_stderr(self):
        proc = MagicMock()
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
        proc.returncode = None
        with patch("remote_agent.command_executor.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            result = await execute_command("c3", "ps", ["-ef"], timeout_s=1)
        assert result["blocked"] is False
        assert result["rc"] == 1
        assert result["stderr"] == "timeout"

    @pytest.mark.asyncio
    async def test_subprocess_exception_returns_rc_1(self):
        with patch(
            "remote_agent.command_executor.asyncio.create_subprocess_exec",
            AsyncMock(side_effect=OSError("no such file")),
        ):
            result = await execute_command("c4", "df", ["-h"])
        assert result["blocked"] is False
        assert result["rc"] == 1
        assert "no such file" in result["stderr"]

    @pytest.mark.asyncio
    async def test_output_truncated_to_max_bytes(self):
        big = b"x" * 20000
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(big, b""))
        proc.returncode = 0
        with patch("remote_agent.command_executor.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            result = await execute_command("c5", "ls", [])
        assert len(result["stdout"]) == 8192


class TestExecuteBatch:
    @pytest.mark.asyncio
    async def test_routes_update_agent_to_updater(self):
        cmds = [{"type": "UPDATE_AGENT", "cmd_id": "u1", "version": "1.2.0",
                 "download_url": "https://x", "sha256_checksum": "a" * 64}]
        with patch(
            "remote_agent.updater.handle_update_command",
            AsyncMock(return_value={"cmd_id": "u1", "update_status": "success"}),
        ) as mock_update:
            results = await execute_batch(cmds, current_version="1.1.0")
        mock_update.assert_awaited_once()
        assert results[0]["update_status"] == "success"

    @pytest.mark.asyncio
    async def test_routes_normal_commands_to_execute_command(self):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"ok", b""))
        proc.returncode = 0
        cmds = [{"cmd_id": "c1", "command": "df", "args": ["-h"], "timeout_s": 5}]
        with patch("remote_agent.command_executor.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            results = await execute_batch(cmds)
        assert results[0]["blocked"] is False
        assert results[0]["stdout"] == "ok"

    @pytest.mark.asyncio
    async def test_empty_batch_returns_empty_list(self):
        results = await execute_batch([])
        assert results == []

    @pytest.mark.asyncio
    async def test_missing_command_field_defaults_to_blocked(self):
        results = await execute_batch([{"cmd_id": "c1"}])
        assert results[0]["blocked"] is True
