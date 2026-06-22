"""Unit tests for workers/services_tools.py — HAProxy + systemd diagnostic tools."""
from __future__ import annotations

import types
from unittest.mock import patch

import pytest


def _ctx():
    return types.SimpleNamespace(redis=None, kafka=None, settings=None)


# ── tool_haproxy_stats ─────────────────────────────────────────────────────────

class TestToolHaproxyStats:
    _CSV_HEADER = "# pxname,svname,qcur,qmax,scur,smax,slim,stot,bin,bout,dreq,dresp,ereq,econ,eresp,wretr,wredis,status,weight,act,bck,chkfail,chkdown,lastchg,downtime,qlimit,pid,iid,sid,throttle,lbtot,tracked,type,rate,rate_lim,rate_max,check_status,check_code,check_duration,hrsp_1xx,hrsp_2xx,hrsp_3xx,hrsp_4xx,hrsp_5xx,hrsp_other,hanafail,req_rate,req_rate_max,req_tot,cli_abrt,srv_abrt,comp_in,comp_out,comp_byp,comp_rsp,lastsess,last_chk,last_agt,qtime,ctime,rtime,ttime,agent_status,agent_code,agent_duration,check_desc,agent_desc,check_rise,check_fall,check_health,check_state,check_weight,check_purgefailures,addr,cookie,mode,algo,conn_rate,conn_rate_max,conn_tot,intercepted,dcon,dses,wrew,connect,reuse,cache_lookups,cache_hits,srv_icur,src_ilim,\n"
    _CSV_ROW_UP = "frontend,FRONTEND,,,5,10,2000,100000,5000000,6000000,0,0,0,,,,,OPEN,,,,,,,,,1,1,0,,,,0,10,2000,10,,,,,,,,0,0,,0,10,100,,,,,,,,,,,,\n"
    _CSV_ROW_DOWN = "backend,server1,0,0,0,3,100,50,100000,200000,0,0,,0,5,0,0,DOWN,1,1,0,10,1,300,300,,1,2,1,,50,,2,,,3,L4CON,,,0,0,0,0,0,0,0,,,,0,0,0,0,0,0,200,,,0,0,5,50,,,,,,,,,,,,10.0.0.2:3306,,tcp,,,,,,0,0,0,0,0,,,,\n"

    @pytest.mark.asyncio
    async def test_returns_csv_stats_on_socket_success(self):
        from workers.services_tools import tool_haproxy_stats

        async def fake_run(cmd, stdin=None, timeout=10.0):
            if "socat" in cmd[0]:
                return self._CSV_HEADER + self._CSV_ROW_UP, "", 0
            return "", "", 0

        with patch("workers.services_tools._run_cmd", side_effect=fake_run):
            result = await tool_haproxy_stats(_ctx(), {})

        assert "haproxy_stats" in result
        assert "show stat" in result.lower() or "CSV" in result or "source:" in result

    @pytest.mark.asyncio
    async def test_falls_back_to_http_when_socket_fails(self):
        from workers.services_tools import tool_haproxy_stats

        async def fake_run(cmd, stdin=None, timeout=10.0):
            if "socat" in cmd[0]:
                return "", "No such file", 1
            if "curl" in cmd[0]:
                return "haproxy_server_up{proxy=\"backend\",server=\"s1\"} 0\n", "", 0
            return "", "", 0

        with patch("workers.services_tools._run_cmd", side_effect=fake_run):
            result = await tool_haproxy_stats(_ctx(), {})

        assert "http" in result.lower() or "metrics" in result.lower()

    @pytest.mark.asyncio
    async def test_returns_error_when_both_unavailable(self):
        from workers.services_tools import tool_haproxy_stats

        async def fake_run(cmd, stdin=None, timeout=10.0):
            return "", "connection refused", 1

        with patch("workers.services_tools._run_cmd", side_effect=fake_run):
            result = await tool_haproxy_stats(_ctx(), {})

        assert "ERROR" in result


# ── tool_systemd_service_health ────────────────────────────────────────────────

class TestToolSystemdServiceHealth:
    @pytest.mark.asyncio
    async def test_reports_active_service(self):
        from workers.services_tools import tool_systemd_service_health

        async def fake_run(cmd, stdin=None, timeout=10.0):
            return (
                "LoadState=loaded\n"
                "ActiveState=active\n"
                "SubState=running\n"
                "Result=success\n"
                "ActiveEnterTimestamp=Thu 2026-05-01 10:00:00 UTC\n"
            ), "", 0

        with patch("workers.services_tools._run_cmd", side_effect=fake_run):
            result = await tool_systemd_service_health(_ctx(), {"services": ["haproxy"]})

        assert "haproxy" in result
        assert "active=active" in result
        assert "running" in result

    @pytest.mark.asyncio
    async def test_fetches_journal_for_failed_service(self):
        from workers.services_tools import tool_systemd_service_health

        call_count = [0]

        async def fake_run(cmd, stdin=None, timeout=10.0):
            call_count[0] += 1
            if "show" in cmd:
                return (
                    "LoadState=loaded\nActiveState=failed\nSubState=failed\n"
                    "Result=exit-code\nActiveEnterTimestamp=\n"
                ), "", 0
            if "journalctl" in cmd[0]:
                return "May 21 10:00:01 host proxysql[1234]: Fatal error: cannot bind\n", "", 0
            return "", "", 0

        with patch("workers.services_tools._run_cmd", side_effect=fake_run):
            result = await tool_systemd_service_health(_ctx(), {"services": ["proxysql"], "journal_lines": 5})

        assert "failed" in result
        assert "journal" in result.lower() or "journalctl" in result.lower() or "Fatal" in result

    @pytest.mark.asyncio
    async def test_handles_multiple_services(self):
        from workers.services_tools import tool_systemd_service_health

        async def fake_run(cmd, stdin=None, timeout=10.0):
            return "LoadState=loaded\nActiveState=active\nSubState=running\nResult=success\nActiveEnterTimestamp=\n", "", 0

        with patch("workers.services_tools._run_cmd", side_effect=fake_run):
            result = await tool_systemd_service_health(
                _ctx(), {"services": ["haproxy", "mysql", "proxysql"]}
            )

        assert "haproxy" in result
        assert "mysql" in result
        assert "proxysql" in result

    @pytest.mark.asyncio
    async def test_services_as_comma_string(self):
        from workers.services_tools import tool_systemd_service_health

        async def fake_run(cmd, stdin=None, timeout=10.0):
            return "LoadState=loaded\nActiveState=active\nSubState=running\nResult=success\nActiveEnterTimestamp=\n", "", 0

        with patch("workers.services_tools._run_cmd", side_effect=fake_run):
            result = await tool_systemd_service_health(_ctx(), {"services": "nginx,redis"})

        assert "nginx" in result
        assert "redis" in result

    @pytest.mark.asyncio
    async def test_systemctl_error_shows_error_line(self):
        from workers.services_tools import tool_systemd_service_health

        async def fake_run(cmd, stdin=None, timeout=10.0):
            return "", "No such unit", 1

        with patch("workers.services_tools._run_cmd", side_effect=fake_run):
            result = await tool_systemd_service_health(_ctx(), {"services": ["unknown-svc"]})

        assert "ERROR" in result


class TestToolHaproxyStatsBranches:
    _MANY_ROWS = "\n".join(
        f"backend,srv{i},0,0,1,5,100,50,100000,200000,0,0,,0,0,0,0,UP,1,1,0,0,0,100,0,,1,2,{i},,50,,2,,,3,,,,0,0,0,0,0,0,0,,,,0,0,0,0,0,0,100,,,0,0,5,50"
        for i in range(35)
    )

    @pytest.mark.asyncio
    async def test_truncates_csv_when_more_than_30_rows(self):
        from workers.services_tools import tool_haproxy_stats

        async def fake_run(cmd, stdin=None, timeout=10.0):
            if "socat" in cmd[0]:
                return self._MANY_ROWS, "", 0
            return "", "", 0

        with patch("workers.services_tools._run_cmd", side_effect=fake_run):
            result = await tool_haproxy_stats(_ctx(), {})

        assert "more rows" in result

    @pytest.mark.asyncio
    async def test_show_info_flag(self):
        from workers.services_tools import tool_haproxy_stats

        call_count = [0]

        async def fake_run(cmd, stdin=None, timeout=10.0):
            call_count[0] += 1
            if stdin and "show info" in stdin:
                return "Process_num: 1\nUptime: 100\n", "", 0
            return "# csv header\nfrontend,FRONTEND\n", "", 0

        with patch("workers.services_tools._run_cmd", side_effect=fake_run):
            result = await tool_haproxy_stats(_ctx(), {"show_info": True})

        assert call_count[0] >= 2
        assert "show info" in result.lower() or "Process_num" in result

    @pytest.mark.asyncio
    async def test_http_fallback_no_haproxy_server_lines(self):
        from workers.services_tools import tool_haproxy_stats

        async def fake_run(cmd, stdin=None, timeout=10.0):
            if "socat" in cmd[0]:
                return "", "conn refused", 1
            if "curl" in cmd[0]:
                return "# just comments\n# nothing useful\n", "", 0
            return "", "", 0

        with patch("workers.services_tools._run_cmd", side_effect=fake_run):
            result = await tool_haproxy_stats(_ctx(), {})

        assert "http" in result.lower() or "metrics" in result.lower()


# ── _run_cmd ──────────────────────────────────────────────────────────────────

class TestRunCmd:
    @pytest.mark.asyncio
    async def test_success_captures_stdout(self):
        from workers.services_tools import _run_cmd

        out, err, rc = await _run_cmd(["echo", "hello"])
        assert "hello" in out
        assert rc == 0

    @pytest.mark.asyncio
    async def test_timeout_returns_sentinel(self):
        import asyncio
        from workers.services_tools import _run_cmd

        with patch("workers.services_tools.asyncio.wait_for", side_effect=asyncio.TimeoutError):
            out, err, rc = await _run_cmd(["echo", "x"])

        assert err == "timeout"
        assert rc == 1

    @pytest.mark.asyncio
    async def test_exception_returns_error_string(self):
        from workers.services_tools import _run_cmd

        with patch("workers.services_tools.asyncio.create_subprocess_exec", side_effect=OSError("no such file")):
            out, err, rc = await _run_cmd(["nonexistent_binary"])

        assert "no such file" in err
        assert rc == 1
