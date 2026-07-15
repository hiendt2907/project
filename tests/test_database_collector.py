"""Tests for remote_agent/collectors/database.py — password security + basic probes."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch, call
import pytest


class TestPasswordNotInArgv:
    """CRITICAL security: passwords must never appear in subprocess argv."""

    @pytest.mark.asyncio
    async def test_mysql_password_not_in_argv(self):
        from remote_agent.collectors.database import collect_mysql_health

        captured_calls: list[dict] = []

        async def fake_run(cmd, timeout=8.0, env=None):
            captured_calls.append({"cmd": list(cmd), "env": env})
            return "Threads_connected\t5\nUptime\t3600\n", "", 0

        with patch("remote_agent.collectors.database._run", side_effect=fake_run):
            await collect_mysql_health("myhost", mysql_pass="s3cr3t")

        for call_info in captured_calls:
            for arg in call_info["cmd"]:
                assert "--password" not in arg, f"Password leaked in argv: {arg}"
            # Password must be in env, not cmd
            env = call_info["env"]
            assert env is not None
            assert env.get("MYSQL_PWD") == "s3cr3t"

    @pytest.mark.asyncio
    async def test_mysql_no_env_when_no_password(self):
        from remote_agent.collectors.database import collect_mysql_health

        captured_calls: list[dict] = []

        async def fake_run(cmd, timeout=8.0, env=None):
            captured_calls.append({"cmd": list(cmd), "env": env})
            return "Threads_connected\t5\n", "", 0

        with patch("remote_agent.collectors.database._run", side_effect=fake_run):
            await collect_mysql_health("myhost", mysql_pass="")

        for call_info in captured_calls:
            assert call_info["env"] is None

    @pytest.mark.asyncio
    async def test_proxysql_password_not_in_argv(self):
        from remote_agent.collectors.database import collect_proxysql_stats

        captured_calls: list[dict] = []

        async def fake_run(cmd, timeout=8.0, env=None):
            captured_calls.append({"cmd": list(cmd), "env": env})
            return "Client_Connections_connected\t10\nActive_Transactions\t2\n", "", 0

        with patch("remote_agent.collectors.database._run", side_effect=fake_run):
            await collect_proxysql_stats("myhost", proxysql_admin_pass="p@ss")

        assert captured_calls, "No subprocess calls made"
        call_info = captured_calls[0]
        for arg in call_info["cmd"]:
            assert "--password" not in arg, f"Password leaked in argv: {arg}"
        assert call_info["env"] is not None
        assert call_info["env"].get("MYSQL_PWD") == "p@ss"

    @pytest.mark.asyncio
    async def test_mysql_unreachable_returns_failed_envelope(self):
        from remote_agent.collectors.database import collect_mysql_health

        async def fake_run(cmd, timeout=8.0, env=None):
            return "", "Connection refused", 1

        with patch("remote_agent.collectors.database._run", side_effect=fake_run):
            result = await collect_mysql_health("myhost")

        assert result is not None
        assert result["result"] == "FAILED"
        assert result["lane"] == "SYS_HARD_FAIL"
        assert result["extracted_fact"]["result"] == "FAILED"
        assert "Connection refused" in result["extracted_fact"]["error"]
        assert result["alert_rule"] == "MySQLDown"


class TestMysqlAnomalyDetection:
    @pytest.mark.asyncio
    async def test_detects_high_replication_lag(self):
        from remote_agent.collectors.database import collect_mysql_health

        status_out = "Threads_connected\t5\nUptime\t3600\nSlow_queries\t2\n"
        repl_out = "Seconds_Behind_Source: 400\nReplica_SQL_Running: Yes\n"

        call_count = 0

        async def fake_run(cmd, timeout=8.0, env=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return status_out, "", 0
            return repl_out, "", 0

        with patch("remote_agent.collectors.database._run", side_effect=fake_run):
            result = await collect_mysql_health("myhost")

        assert result is not None
        assert result["result"] == "FAILED"
        assert "replication_lag" in result["alert_hint"]

    @pytest.mark.asyncio
    async def test_detects_replica_sql_stopped(self):
        from remote_agent.collectors.database import collect_mysql_health

        status_out = "Threads_connected\t10\nUptime\t3600\n"
        repl_out = "Seconds_Behind_Source: 0\nReplica_SQL_Running: No\n"

        call_count = 0

        async def fake_run(cmd, timeout=8.0, env=None):
            nonlocal call_count
            call_count += 1
            return (status_out if call_count == 1 else repl_out), "", 0

        with patch("remote_agent.collectors.database._run", side_effect=fake_run):
            result = await collect_mysql_health("myhost")

        assert result is not None
        assert result["result"] == "FAILED"
        assert "replication" in result["alert_hint"].lower()

    @pytest.mark.asyncio
    async def test_detects_high_threads(self):
        from remote_agent.collectors.database import collect_mysql_health

        status_out = "Threads_connected\t600\nUptime\t3600\nSlow_queries\t5\n"
        repl_out = ""

        call_count = 0

        async def fake_run(cmd, timeout=8.0, env=None):
            nonlocal call_count
            call_count += 1
            return (status_out if call_count == 1 else repl_out), "", 0

        with patch("remote_agent.collectors.database._run", side_effect=fake_run):
            result = await collect_mysql_health("myhost")

        assert result is not None
        assert result["result"] == "FAILED"
        assert "threads" in result["alert_hint"].lower()

    @pytest.mark.asyncio
    async def test_detects_high_slow_queries(self):
        from remote_agent.collectors.database import collect_mysql_health

        status_out = "Threads_connected\t5\nSlow_queries\t200\nUptime\t3600\n"
        repl_out = ""

        call_count = 0

        async def fake_run(cmd, timeout=8.0, env=None):
            nonlocal call_count
            call_count += 1
            return (status_out if call_count == 1 else repl_out), "", 0

        with patch("remote_agent.collectors.database._run", side_effect=fake_run):
            result = await collect_mysql_health("myhost")

        assert result is not None
        assert result["result"] == "FAILED"
        assert "slow_queries" in result["alert_hint"].lower()

    @pytest.mark.asyncio
    async def test_mysql_non_numeric_status_value_handled(self):
        from remote_agent.collectors.database import collect_mysql_health

        status_out = "Threads_connected\t5\nUptime\t3600\nSome_var\tnot_a_number\n"
        repl_out = ""

        call_count = 0

        async def fake_run(cmd, timeout=8.0, env=None):
            nonlocal call_count
            call_count += 1
            return (status_out if call_count == 1 else repl_out), "", 0

        with patch("remote_agent.collectors.database._run", side_effect=fake_run):
            result = await collect_mysql_health("myhost")

        assert result is not None
        assert result["extracted_fact"].get("some_var") == "not_a_number"


class TestProxySQLAnomalyDetection:
    @pytest.mark.asyncio
    async def test_detects_high_clients(self):
        from remote_agent.collectors.database import collect_proxysql_stats

        out = "Client_Connections_connected\t2500\nActive_Transactions\t10\n"

        async def fake_run(cmd, timeout=8.0, env=None):
            return out, "", 0

        with patch("remote_agent.collectors.database._run", side_effect=fake_run):
            result = await collect_proxysql_stats("myhost")

        assert result is not None
        assert result["result"] == "FAILED"
        assert "proxysql_clients" in result["alert_hint"]

    @pytest.mark.asyncio
    async def test_detects_high_active_transactions(self):
        from remote_agent.collectors.database import collect_proxysql_stats

        out = "Client_Connections_connected\t100\nActive_Transactions\t600\n"

        async def fake_run(cmd, timeout=8.0, env=None):
            return out, "", 0

        with patch("remote_agent.collectors.database._run", side_effect=fake_run):
            result = await collect_proxysql_stats("myhost")

        assert result is not None
        assert result["result"] == "FAILED"
        assert "active_transactions" in result["alert_hint"]

    @pytest.mark.asyncio
    async def test_proxysql_non_numeric_value_handled(self):
        from remote_agent.collectors.database import collect_proxysql_stats

        out = "Client_Connections_connected\t10\nActive_Transactions\t2\nSome_metric\tnot_num\n"

        async def fake_run(cmd, timeout=8.0, env=None):
            return out, "", 0

        with patch("remote_agent.collectors.database._run", side_effect=fake_run):
            result = await collect_proxysql_stats("myhost")

        assert result is not None
        assert result["extracted_fact"].get("some_metric") == "not_num"


class TestMysqlUserParam:
    @pytest.mark.asyncio
    async def test_mysql_user_inserted_in_cmd(self):
        from remote_agent.collectors.database import collect_mysql_health

        captured_calls: list[dict] = []

        async def fake_run(cmd, timeout=8.0, env=None):
            captured_calls.append({"cmd": list(cmd), "env": env})
            return "Threads_connected\t5\nUptime\t3600\n", "", 0

        with patch("remote_agent.collectors.database._run", side_effect=fake_run):
            await collect_mysql_health("myhost", mysql_user="myuser")

        assert any("--user=myuser" in arg for call_info in captured_calls for arg in call_info["cmd"])

    @pytest.mark.asyncio
    async def test_proxysql_unreachable_returns_failed_envelope(self):
        from remote_agent.collectors.database import collect_proxysql_stats

        async def fake_run(cmd, timeout=8.0, env=None):
            return "", "Connection refused", 1

        with patch("remote_agent.collectors.database._run", side_effect=fake_run):
            result = await collect_proxysql_stats("myhost")

        assert result is not None
        assert result["result"] == "FAILED"
        assert result["lane"] == "SYS_HARD_FAIL"
        assert result["extracted_fact"]["result"] == "FAILED"
        assert "Connection refused" in result["extracted_fact"]["error"]
        assert result["alert_rule"] == "ProxySQLDown"


class TestRunFunction:
    @pytest.mark.asyncio
    async def test_run_timeout(self):
        from remote_agent.collectors.database import _run

        def _wf_timeout(coro, *a, **k):
            # đóng coroutine communicate() của subprocess thật trước khi raise
            if hasattr(coro, "close"):
                coro.close()
            raise asyncio.TimeoutError()

        with patch("remote_agent.collectors.database.asyncio.wait_for", side_effect=_wf_timeout):
            out, err, rc = await _run(["echo", "x"], timeout=0.001)

        assert rc == 1
        assert "timeout" in err

    @pytest.mark.asyncio
    async def test_run_exception(self):
        from remote_agent.collectors.database import _run

        with patch("asyncio.create_subprocess_exec", side_effect=OSError("no such file")):
            out, err, rc = await _run(["nonexistent_xyz"])

        assert rc == 1
        assert "no such file" in err

    @pytest.mark.asyncio
    async def test_run_success(self):
        from remote_agent.collectors.database import _run

        out, err, rc = await _run(["echo", "hello"])
        assert rc == 0
        assert "hello" in out
