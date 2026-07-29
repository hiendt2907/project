"""Tests for remote_agent.command_executor — INV_READONLY_CMDS / INV_DIAG_SCOPE_BOUNDED.

This is the single most security-critical path in remote_agent: it is the last
line of defense between a command pushed by Omni (or a compromised gateway) and
an arbitrary shell on a customer VM.

Chính sách nay đến từ `config/diagnostic_commands.yaml` qua
`pkg.diagnostics.validator` (dùng chung với gateway và collectors) — nên các test ở
đây kiểm TÍNH CHẤT AN TOÀN qua đường mới, không kiểm một frozenset hardcode. Tính chất
"gateway và agent trả cùng câu trả lời" được pin ở
tests/test_diagnostic_catalog_unification.py.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from remote_agent.command_executor import (
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

    @pytest.mark.parametrize("blocked_cmd", [
        "strings", "od", "xxd", "hexdump", "base64", "nc", "ncat", "wget", "scp",
        "sed", "cut", "sort", "uniq", "more", "less", "tac", "rev", "md5sum",
    ])
    def test_content_dump_commands_absent_from_catalog(self, blocked_cmd):
        """Các lệnh dump/exfil thuần không có entry nào ⇒ vẫn bị chặn, nhưng nay vì
        KHÔNG NẰM TRONG CATALOGUE, không vì một blocklist tên lệnh song song."""
        allowed, reason = _is_command_allowed(blocked_cmd, [])
        assert allowed is False
        assert "command_not_whitelisted" in reason

    @pytest.mark.parametrize("path", [
        "/var/lib/mysql/orders.ibd", "/var/lib/postgresql/base/1/2",
        "/home/app/.env", "/root/.ssh/id_rsa", "/etc/shadow", "/var/backups/db.sql",
        "/var/log/../../home/khach/.ssh/id_ed25519",
    ])
    def test_content_read_outside_scope_blocked(self, path):
        """INV_DIAG_SCOPE_BOUNDED: `cat` được phép, nhưng chỉ trong phạm vi khai báo.
        Đường dẫn chuẩn hoá TRƯỚC khi so, nên `..` không vòng ra ngoài được."""
        allowed, reason = _is_command_allowed("cat", [path])
        assert allowed is False, path
        assert reason

    @pytest.mark.parametrize("path", ["/proc/meminfo", "/etc/hosts", "/var/log/syslog"])
    def test_content_read_inside_scope_allowed(self, path):
        allowed, reason = _is_command_allowed("cat", [path])
        assert allowed is True, reason

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
        # Hàng rào WRITE_VERBS chạy độc lập catalogue, nên một entry khai lỏng cũng
        # không mở được đường mutate; `deny_subcommands` là lớp thứ hai.
        assert "write_verb_blocked" in reason or "subcommand_denied" in reason

    def test_systemctl_unknown_subcommand_blocked(self):
        allowed, reason = _is_command_allowed("systemctl", ["frobnicate", "nginx"])
        assert allowed is False
        assert "subcommand_not_in_catalog" in reason

    def test_systemctl_with_no_args_allowed(self):
        # no subcommand at all — nothing to validate, falls through
        allowed, _ = _is_command_allowed("systemctl", [])
        assert allowed is True

    @pytest.mark.parametrize("flag", ["--start", "--stop", "--restart", "--enable", "--kill"])
    def test_global_write_flag_blocked_on_any_whitelisted_command(self, flag):
        allowed, reason = _is_command_allowed("ps", [flag])
        assert allowed is False
        assert "write_verb_blocked" in reason

    def test_catalog_declares_no_write_verb_subcommand(self):
        """Hàng rào ở tầng LOAD: không entry nào được khai một subcommand mang nghĩa
        ghi. Kiểm ở đây nghĩa là một PR sửa YAML làm test VỠ, chứ không âm thầm cấp
        quyền mutate."""
        from pkg.diagnostics.command_catalog import WRITE_VERBS
        from pkg.diagnostics.validator import get_catalog

        for spec in get_catalog().specs.values():
            offending = {s.lower() for s in spec.subcommands} & WRITE_VERBS
            assert not offending, f"{spec.command}: {offending}"

    # --- CRITICAL #1: subcommand/flag allowlist (audit 2026-07-22) ---------

    @pytest.mark.parametrize("bad_args", [
        ["auxe"],       # BSD bundled: a,u,x,e — `e` = show environment of other procs
        ["axe"],
        ["e"],          # lone BSD environment flag
        ["auxww", "e"],
        ["--environ"],
    ])
    def test_ps_environment_flag_blocked(self, bad_args):
        """`ps auxe`/`ps e` prints OTHER processes' environments (secret leak)."""
        allowed, reason = _is_command_allowed("ps", bad_args)
        assert allowed is False
        assert "ps_environment_flag_blocked" in reason

    @pytest.mark.parametrize("ok_args", [
        ["-ef"],
        ["-eo", "comm"],     # the real discovery_evidence.py invocation
        ["aux"],
        ["-e"],              # Unix select-all — NOT the BSD env flag
        ["-p", "1234"],
        ["-u", "eve"],       # username containing 'e' must not false-positive
    ])
    def test_ps_safe_invocations_allowed(self, ok_args):
        allowed, reason = _is_command_allowed("ps", ok_args)
        assert allowed is True, reason

    @pytest.mark.parametrize("subcmd", [
        "shutdown", "drop", "create", "password", "kill",
        "flush-hosts", "flush-logs", "start-slave", "stop-slave",
        "old-password", "debug", "refresh",
    ])
    def test_mysqladmin_mutating_subcommands_blocked(self, subcmd):
        allowed, reason = _is_command_allowed("mysqladmin", [subcmd])
        assert allowed is False
        assert "mysqladmin_subcommand_not_allowed" in reason

    @pytest.mark.parametrize("subcmd", [
        "status", "ping", "processlist", "extended-status", "version", "variables",
    ])
    def test_mysqladmin_readonly_subcommands_allowed(self, subcmd):
        # Real usage (services.analyst.diagnosis_loop prompt) is the bare
        # subcommand, no connection flags.
        allowed, reason = _is_command_allowed("mysqladmin", [subcmd])
        assert allowed is True, reason

    def test_mysqladmin_shutdown_after_flags_blocked(self):
        allowed, reason = _is_command_allowed("mysqladmin", ["-uroot", "-ppw", "shutdown"])
        assert allowed is False
        assert "mysqladmin_flags_not_allowed" in reason

    def test_mysqladmin_no_subcommand_blocked(self):
        allowed, reason = _is_command_allowed("mysqladmin", ["--help"])
        assert allowed is False
        assert "mysqladmin_flags_not_allowed" in reason

    def test_mysqladmin_no_args_blocked(self):
        allowed, reason = _is_command_allowed("mysqladmin", [])
        assert allowed is False
        assert "mysqladmin_subcommand_not_allowed" in reason

    def test_mysqladmin_any_flag_blocked_even_with_valid_subcommand(self):
        """Closes the flag-value-smuggling bypass: a flag that could consume
        the next token as a value must never be allowed alongside a subcommand."""
        allowed, reason = _is_command_allowed("mysqladmin", ["-h", "status"])
        assert allowed is False
        assert "mysqladmin_flags_not_allowed" in reason

    def test_mysqladmin_command_chaining_bypass_blocked(self):
        """mysqladmin runs every non-flag token as a separate command in
        sequence — `status shutdown` would otherwise run both. Two positional
        tokens must never pass, even if the first is read-only."""
        allowed, reason = _is_command_allowed("mysqladmin", ["status", "shutdown"])
        assert allowed is False
        assert "mysqladmin_subcommand_not_allowed" in reason

    # --- dpkg/rpm/ip subcommand/flag allowlist (ultrareview follow-up) ------

    @pytest.mark.parametrize("bad_flag", ["-i", "-r", "-P", "--install", "--remove", "--purge", "--configure", "--unpack"])
    def test_dpkg_mutating_flags_blocked(self, bad_flag):
        allowed, reason = _is_command_allowed("dpkg", [bad_flag, "somepkg"])
        assert allowed is False
        assert "dpkg_flag_not_allowed" in reason

    @pytest.mark.parametrize("ok_args", [["-l"], ["-s", "nginx"], ["-L", "nginx"], ["--status", "nginx"]])
    def test_dpkg_readonly_flags_allowed(self, ok_args):
        allowed, reason = _is_command_allowed("dpkg", ok_args)
        assert allowed is True, reason

    @pytest.mark.parametrize("bad_args", [["-i", "pkg.rpm"], ["-e", "pkg"], ["-U", "pkg.rpm"], ["--install", "pkg.rpm"], ["--erase", "pkg"]])
    def test_rpm_mutating_flags_blocked(self, bad_args):
        allowed, reason = _is_command_allowed("rpm", bad_args)
        assert allowed is False
        assert "rpm_destructive_flag_blocked" in reason

    def test_rpm_without_query_mode_blocked(self):
        allowed, reason = _is_command_allowed("rpm", ["-a"])
        assert allowed is False
        assert "rpm_query_mode_required" in reason

    @pytest.mark.parametrize("ok_args", [["-qa"], ["-qi", "nginx"], ["--query", "-a"]])
    def test_rpm_query_mode_allowed(self, ok_args):
        allowed, reason = _is_command_allowed("rpm", ok_args)
        assert allowed is True, reason

    @pytest.mark.parametrize("bad_args", [
        ["route", "add", "10.0.0.0/8", "via", "1.2.3.4"],
        ["link", "set", "eth0", "down"],
        ["addr", "flush", "dev", "eth0"],
        ["netns", "add", "foo"],
    ])
    def test_ip_mutating_subcommands_blocked(self, bad_args):
        allowed, reason = _is_command_allowed("ip", bad_args)
        assert allowed is False
        assert "ip_mutating_subcommand_blocked" in reason

    @pytest.mark.parametrize("ok_args", [["addr", "show"], ["route", "show"], ["-s", "link", "show"], ["neigh", "show"]])
    def test_ip_readonly_subcommands_allowed(self, ok_args):
        allowed, reason = _is_command_allowed("ip", ok_args)
        assert allowed is True, reason

    # --- ps: dash-prefixed multi-letter clusters must not bypass the check -

    @pytest.mark.parametrize("bad_args", [["-auxe"], ["-axe"]])
    def test_ps_dashed_bsd_cluster_still_blocked(self, bad_args):
        """procps-ng legacy-compat can reinterpret a dashed cluster as BSD-style
        (`ps -aux` behaves like `ps aux`) — a leading dash must not exempt a
        multi-letter env-dump cluster from the check."""
        allowed, reason = _is_command_allowed("ps", bad_args)
        assert allowed is False
        assert "ps_environment_flag_blocked" in reason

    def test_ps_bare_dash_e_stays_allowed(self):
        """Single-letter `-e` is unambiguous POSIX ("select all") in every ps
        implementation — must not be caught by the dashed-cluster fix above."""
        allowed, reason = _is_command_allowed("ps", ["-e"])
        assert allowed is True, reason


class TestSubprocessEnvSandbox:
    """CRITICAL #2: spawned commands must NOT inherit the agent's environment
    (would leak OMNI_AGENT_API_KEY via `ps auxe`, `/proc/self/environ`, etc.)."""

    @pytest.mark.asyncio
    async def test_subprocess_spawned_with_sanitized_env(self):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"ok", b""))
        proc.returncode = 0
        captured = {}

        async def _fake_exec(*args, **kwargs):
            captured["env"] = kwargs.get("env")
            return proc

        with patch("remote_agent.command_executor.asyncio.create_subprocess_exec", _fake_exec):
            await execute_command("c-env", "ps", ["-ef"])

        env = captured["env"]
        assert env is not None, "env must be explicitly set, never inherited (None)"
        # No secret-bearing keys leak into the child.
        assert "OMNI_AGENT_API_KEY" not in env
        assert all("API_KEY" not in k and "SECRET" not in k and "TOKEN" not in k for k in env)
        # A minimal, safe PATH is still provided so binaries resolve.
        assert env.get("PATH")

    @pytest.mark.asyncio
    async def test_secret_env_not_inherited_even_if_present(self, monkeypatch):
        monkeypatch.setenv("OMNI_AGENT_API_KEY", "super-secret-123")
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"ok", b""))
        proc.returncode = 0
        captured = {}

        async def _fake_exec(*args, **kwargs):
            captured["env"] = kwargs.get("env")
            return proc

        with patch("remote_agent.command_executor.asyncio.create_subprocess_exec", _fake_exec):
            await execute_command("c-env2", "df", ["-h"])

        assert "super-secret-123" not in str(captured["env"])


class TestTrustedBinaryResolution:
    """CRITICAL #1 (ultrareview follow-up): the whitelist checked only the
    basename, but the caller-supplied path was what actually executed —
    `/tmp/x/ps` passed the "ps" check and then ran an attacker binary."""

    @pytest.mark.asyncio
    async def test_caller_supplied_path_is_ignored_resolved_binary_used(self, tmp_path, monkeypatch):
        from remote_agent import command_executor

        decoy_ps = tmp_path / "ps"
        decoy_ps.write_text("#!/bin/sh\necho PWNED\n")
        decoy_ps.chmod(0o755)

        command_executor._RESOLVED_BINARY_CACHE.clear()
        monkeypatch.setenv("OMNI_AGENT_CMD_PATH", "/usr/bin:/bin")  # excludes tmp_path

        captured = {}
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"ok", b""))
        proc.returncode = 0

        async def _fake_exec(*args, **kwargs):
            captured["argv0"] = args[0]
            return proc

        with patch("remote_agent.command_executor.asyncio.create_subprocess_exec", _fake_exec):
            await execute_command("c-path", str(decoy_ps), ["-ef"])

        command_executor._RESOLVED_BINARY_CACHE.clear()
        assert captured["argv0"] != str(decoy_ps)
        assert captured["argv0"].endswith("/ps")

    @pytest.mark.asyncio
    async def test_binary_not_on_sandbox_path_is_blocked(self, monkeypatch):
        from remote_agent import command_executor

        command_executor._RESOLVED_BINARY_CACHE.clear()
        monkeypatch.setenv("OMNI_AGENT_CMD_PATH", "/nonexistent-dir-xyz")

        with patch("remote_agent.command_executor.asyncio.create_subprocess_exec") as mock_exec:
            result = await execute_command("c-missing", "ps", ["-ef"])

        command_executor._RESOLVED_BINARY_CACHE.clear()
        mock_exec.assert_not_called()
        assert result["blocked"] is True
        assert "binary_not_found_on_sandbox_path" in result["block_reason"]

    @pytest.mark.asyncio
    async def test_subprocess_uses_sandbox_cwd(self, monkeypatch):
        """Finding #5: the child must not inherit the agent's cwd (could leak
        sensitive filenames via relative-path listings)."""
        monkeypatch.delenv("OMNI_AGENT_CMD_CWD", raising=False)
        captured = {}
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"ok", b""))
        proc.returncode = 0

        async def _fake_exec(*args, **kwargs):
            captured["cwd"] = kwargs.get("cwd")
            return proc

        with patch("remote_agent.command_executor.asyncio.create_subprocess_exec", _fake_exec):
            await execute_command("c-cwd", "df", ["-h"])

        assert captured["cwd"] == "/tmp"


class TestExecuteCommand:
    @pytest.mark.asyncio
    async def test_blocked_command_returns_blocked_result_without_subprocess(self):
        with patch("remote_agent.command_executor.asyncio.create_subprocess_exec") as mock_exec:
            result = await execute_command("c1", "cat", ["/etc/shadow"])
        mock_exec.assert_not_called()
        assert result["blocked"] is True
        assert result["rc"] == -1
        assert "secret_like_path" in result["block_reason"]

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
            results = await execute_batch(cmds, current_version="1.1.0", api_key="agent-secret")
        mock_update.assert_awaited_once()
        assert mock_update.call_args.kwargs["api_key"] == "agent-secret"
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
