"""Unit tests for workers/database_tools.py — MySQL + ProxySQL diagnostic tools."""
from __future__ import annotations

import asyncio
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _ctx():
    return types.SimpleNamespace(redis=None, kafka=None, settings=None)


# ── tool_mysql_health ──────────────────────────────────────────────────────────

class TestToolMysqlHealth:
    @pytest.mark.asyncio
    async def test_returns_status_block_on_success(self):
        from workers.database_tools import tool_mysql_health

        mock_status_out = (
            "Aborted_connects\t5\n"
            "Threads_connected\t12\n"
            "Uptime\t86400\n"
            "Slow_queries\t3\n"
        )
        mock_repl_out = ""  # standalone

        async def fake_run(cmd, timeout=10.0):
            if "global_status" in " ".join(cmd):
                return mock_status_out, "", 0
            # SHOW REPLICA STATUS — empty means primary
            return mock_repl_out, "", 0

        with patch("workers.database_tools._run_cmd", side_effect=fake_run):
            result = await tool_mysql_health(_ctx(), {"host": "127.0.0.1", "port": 3306})

        assert "mysql_health" in result
        assert "host=127.0.0.1:3306" in result
        assert "primary" in result or "not configured" in result

    @pytest.mark.asyncio
    async def test_returns_error_on_connection_failure(self):
        from workers.database_tools import tool_mysql_health

        async def fake_run(cmd, timeout=10.0):
            return "", "Can't connect to MySQL server", 1

        with patch("workers.database_tools._run_cmd", side_effect=fake_run):
            result = await tool_mysql_health(_ctx(), {"host": "10.0.0.1"})

        assert "ERROR" in result
        assert "Can't connect" in result

    @pytest.mark.asyncio
    async def test_shows_replication_lag_when_replica(self):
        from workers.database_tools import tool_mysql_health

        repl_out = (
            "*************************** 1. row ***************************\n"
            "         Source_Host: 10.0.0.1\n"
            "      Replica_IO_Running: Yes\n"
            "     Replica_SQL_Running: Yes\n"
            "Seconds_Behind_Source: 45\n"
            "              Last_Error: \n"
        )

        async def fake_run(cmd, timeout=10.0):
            if "global_status" in " ".join(cmd):
                return "Uptime\t1234\n", "", 0
            if "REPLICA" in " ".join(cmd) or "SLAVE" in " ".join(cmd):
                return repl_out, "", 0
            return "", "", 0

        with patch("workers.database_tools._run_cmd", side_effect=fake_run):
            result = await tool_mysql_health(_ctx(), {})

        assert "replication" in result.lower()
        assert "45" in result  # lag value visible


# ── tool_proxysql_stats ────────────────────────────────────────────────────────

class TestToolProxysqlStats:
    @pytest.mark.asyncio
    async def test_returns_all_sections_on_success(self):
        from workers.database_tools import tool_proxysql_stats

        async def fake_run(cmd, timeout=10.0):
            if "stats_mysql_global" in " ".join(cmd):
                return "Active_Transactions\t3\nClient_Connections_connected\t150\n", "", 0
            if "connection_pool" in " ".join(cmd):
                return "1\t10.0.0.2\t3306\tONLINE\t5\t10\t1000\t0\t5000\n", "", 0
            if "runtime_mysql_servers" in " ".join(cmd):
                return "1\t10.0.0.2\t3306\tONLINE\t1\n", "", 0
            return "", "", 0

        with patch("workers.database_tools._run_cmd", side_effect=fake_run):
            result = await tool_proxysql_stats(_ctx(), {"host": "127.0.0.1"})

        assert "proxysql_stats" in result
        assert "global_stats" in result
        assert "connection_pool" in result
        assert "runtime_servers" in result

    @pytest.mark.asyncio
    async def test_handles_each_section_error_independently(self):
        from workers.database_tools import tool_proxysql_stats

        call_count = [0]

        async def fake_run(cmd, timeout=10.0):
            call_count[0] += 1
            # First call fails (global_stats), rest succeed
            if call_count[0] == 1:
                return "", "Access denied", 1
            return "ok\t1\n", "", 0

        with patch("workers.database_tools._run_cmd", side_effect=fake_run):
            result = await tool_proxysql_stats(_ctx(), {})

        assert "ERROR" in result  # first section failed
        # Should not raise — other sections still attempted


# ── tool_database_replication_lag ─────────────────────────────────────────────

class TestToolDatabaseReplicationLag:
    @pytest.mark.asyncio
    async def test_reports_lag_per_host(self):
        from workers.database_tools import tool_database_replication_lag

        repl_out = (
            "Replica_IO_Running: Yes\n"
            "Replica_SQL_Running: Yes\n"
            "Seconds_Behind_Source: 120\n"
        )

        async def fake_run(cmd, timeout=10.0):
            return repl_out, "", 0

        with patch("workers.database_tools._run_cmd", side_effect=fake_run):
            result = await tool_database_replication_lag(
                _ctx(), {"hosts": ["10.0.0.2:3306", "10.0.0.3:3306"]}
            )

        assert "database_replication_lag" in result
        assert "120" in result

    @pytest.mark.asyncio
    async def test_primary_detected_when_no_replica_status(self):
        from workers.database_tools import tool_database_replication_lag

        async def fake_run(cmd, timeout=10.0):
            return "", "", 0  # empty = primary

        with patch("workers.database_tools._run_cmd", side_effect=fake_run):
            result = await tool_database_replication_lag(_ctx(), {"hosts": ["127.0.0.1:3306"]})

        assert "primary" in result.lower() or "not configured" in result.lower()

    @pytest.mark.asyncio
    async def test_handles_connection_error(self):
        from workers.database_tools import tool_database_replication_lag

        async def fake_run(cmd, timeout=10.0):
            return "", "connection refused", 1

        with patch("workers.database_tools._run_cmd", side_effect=fake_run):
            result = await tool_database_replication_lag(_ctx(), {"hosts": ["10.99.0.1:3306"]})

        assert "ERROR" in result
