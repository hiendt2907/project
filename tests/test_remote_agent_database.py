"""Tests for remote_agent.collectors.database — mysql_health / proxysql_stats.

Mocks the module-level _run subprocess helper (same convention as
tests/test_remote_agent.py) so no real mysql binary is required.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from remote_agent.collectors import database as db


_STATUS_HEALTHY = (
    "Uptime\t12345\n"
    "Threads_connected\t10\n"
    "Threads_running\t2\n"
    "Questions\t99999\n"
    "Slow_queries\t1\n"
    "Aborted_connects\t0\n"
    "Max_used_connections\t20\n"
)
_REPL_HEALTHY = "Seconds_Behind_Source: 0\nReplica_SQL_Running: Yes\n"

_STATUS_BUSY = (
    "Uptime\t12345\n"
    "Threads_connected\t600\n"
    "Slow_queries\t150\n"
)
_REPL_BROKEN = "Seconds_Behind_Source: 900\nReplica_SQL_Running: No\n"


class TestCollectMysqlHealth:
    @pytest.mark.asyncio
    async def test_healthy_returns_passed_resource_lane(self):
        with patch.object(db, "_run", AsyncMock(side_effect=[
            (_STATUS_HEALTHY, "", 0),
            (_REPL_HEALTHY, "", 0),
        ])):
            result = await db.collect_mysql_health("host1", mysql_user="root", mysql_pass="secret")

        assert result is not None
        assert result["result"] == "PASSED"
        assert result["lane"] == "SYS_RESOURCE"
        assert result["extracted_fact"]["threads_connected"] == 10

    @pytest.mark.asyncio
    async def test_replication_stopped_flags_hard_fail(self):
        with patch.object(db, "_run", AsyncMock(side_effect=[
            (_STATUS_HEALTHY, "", 0),
            (_REPL_BROKEN, "", 0),
        ])):
            result = await db.collect_mysql_health("host1")

        assert result["result"] == "FAILED"
        assert result["lane"] == "SYS_HARD_FAIL"
        assert "replication SQL thread stopped" in result["alert_hint"]
        assert "replication_lag=900s>300s" in result["alert_hint"]

    @pytest.mark.asyncio
    async def test_high_threads_and_slow_queries_flagged(self):
        with patch.object(db, "_run", AsyncMock(side_effect=[
            (_STATUS_BUSY, "", 0),
            ("", "", 0),
        ])):
            result = await db.collect_mysql_health("host1")

        assert result["result"] == "FAILED"
        assert "threads_connected=600>500" in result["alert_hint"]
        assert "slow_queries=150>100" in result["alert_hint"]

    @pytest.mark.asyncio
    async def test_mysql_unreachable_returns_failed_hard_fail(self):
        with patch.object(db, "_run", AsyncMock(return_value=("", "Can't connect to MySQL server", 1))):
            result = await db.collect_mysql_health("host1", mysql_host="10.0.0.5")

        assert result["result"] == "FAILED"
        assert result["lane"] == "SYS_HARD_FAIL"
        assert result["alert_rule"] == "MySQLDown"
        assert result["extracted_fact"]["db_host"] == "10.0.0.5"

    @pytest.mark.asyncio
    async def test_password_passed_via_env_not_argv(self):
        captured_env = {}

        async def fake_run(cmd, timeout=8.0, env=None):
            captured_env.update(env or {})
            return (_STATUS_HEALTHY, "", 0)

        with patch.object(db, "_run", fake_run):
            await db.collect_mysql_health("host1", mysql_user="root", mysql_pass="s3cr3t")

        assert captured_env.get("MYSQL_PWD") == "s3cr3t"

    @pytest.mark.asyncio
    async def test_user_arg_included_when_provided(self):
        seen_cmds = []

        async def fake_run(cmd, timeout=8.0, env=None):
            seen_cmds.append(cmd)
            return (_STATUS_HEALTHY, "", 0)

        with patch.object(db, "_run", fake_run):
            await db.collect_mysql_health("host1", mysql_user="root")

        assert any("--user=root" in c for c in seen_cmds[0])


class TestCollectProxysqlStats:
    @pytest.mark.asyncio
    async def test_healthy_returns_passed(self):
        out = "Active_Transactions\t5\nClient_Connections_connected\t50\n"
        with patch.object(db, "_run", AsyncMock(return_value=(out, "", 0))):
            result = await db.collect_proxysql_stats("host1")

        assert result["result"] == "PASSED"
        assert result["lane"] == "SYS_RESOURCE"
        assert result["extracted_fact"]["active_transactions"] == 5

    @pytest.mark.asyncio
    async def test_too_many_clients_flags_hard_fail(self):
        out = "Active_Transactions\t10\nClient_Connections_connected\t2500\n"
        with patch.object(db, "_run", AsyncMock(return_value=(out, "", 0))):
            result = await db.collect_proxysql_stats("host1")

        assert result["result"] == "FAILED"
        assert result["lane"] == "SYS_HARD_FAIL"
        assert "proxysql_clients=2500>2000" in result["alert_hint"]

    @pytest.mark.asyncio
    async def test_unreachable_returns_failed(self):
        with patch.object(db, "_run", AsyncMock(return_value=("", "Access denied for user 'radmin'", 1))):
            result = await db.collect_proxysql_stats("host1")

        assert result["result"] == "FAILED"
        assert result["alert_rule"] == "ProxySQLDown"

    @pytest.mark.asyncio
    async def test_default_admin_user_is_radmin(self):
        seen_cmds = []

        async def fake_run(cmd, timeout=8.0, env=None):
            seen_cmds.append(cmd)
            return ("", "", 0)

        with patch.object(db, "_run", fake_run):
            await db.collect_proxysql_stats("host1")

        assert any("--user=radmin" in c for c in seen_cmds[0])


class TestDatabaseRunHelper:
    @pytest.mark.asyncio
    async def test_timeout_returns_error_tuple(self):
        import asyncio as _asyncio
        from unittest.mock import MagicMock

        proc = MagicMock()
        proc.communicate = AsyncMock()

        def _wf_timeout(coro, *a, **k):
            if hasattr(coro, "close"):
                coro.close()
            raise _asyncio.TimeoutError()

        with patch("remote_agent.collectors.database.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)), \
             patch("remote_agent.collectors.database.asyncio.wait_for", side_effect=_wf_timeout):
            out, err, rc = await db._run(["mysql", "--version"])

        assert rc == 1
        assert err == "timeout"

    @pytest.mark.asyncio
    async def test_exception_returns_error_tuple(self):
        with patch(
            "remote_agent.collectors.database.asyncio.create_subprocess_exec",
            AsyncMock(side_effect=OSError("mysql not found")),
        ):
            out, err, rc = await db._run(["mysql", "--version"])

        assert rc == 1
        assert "mysql not found" in err
