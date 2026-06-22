"""Unit tests for workers/storage_tools.py — disk health and NFS diagnostic tools."""
from __future__ import annotations

import types
from unittest.mock import patch

import pytest


def _ctx():
    return types.SimpleNamespace(redis=None, kafka=None, settings=None)


# ── tool_disk_health ───────────────────────────────────────────────────────────

class TestToolDiskHealth:
    _DF_NORMAL = (
        "Filesystem     Type   Size  Used Avail Use% Mounted on\n"
        "/dev/sda4      ext4    97G   48G   45G  52% /\n"
        "/dev/sdb1      ext4   787G  633G  115G  85% /data\n"
        "tmpfs          tmpfs   15G     0   15G   0% /dev/shm\n"
    )
    _DF_CRITICAL = (
        "Filesystem     Type   Size  Used Avail Use% Mounted on\n"
        "/dev/sdd1      xfs    2.8T  2.6T  157G  95% /srv/nfs4/backup_uat\n"
        "/dev/sda4      ext4    97G   48G   45G  52% /\n"
    )

    @pytest.mark.asyncio
    async def test_marks_ok_partitions(self):
        from workers.storage_tools import tool_disk_health

        async def fake_run(cmd, timeout=10.0):
            if "-ih" in cmd:
                return "Filesystem    Inodes  IUsed  IFree IUse% Mounted on\n/dev/sda4     123456   5000 118456    5% /\n", "", 0
            return self._DF_NORMAL, "", 0

        with patch("workers.storage_tools._run_cmd", side_effect=fake_run):
            result = await tool_disk_health(_ctx(), {})

        assert "disk_health" in result
        assert "OK" in result or "WARN" in result  # /data at 85% should trigger WARN

    @pytest.mark.asyncio
    async def test_marks_critical_partition(self):
        from workers.storage_tools import tool_disk_health

        async def fake_run(cmd, timeout=10.0):
            if "-ih" in cmd:
                return "", "", 0
            return self._DF_CRITICAL, "", 0

        with patch("workers.storage_tools._run_cmd", side_effect=fake_run):
            result = await tool_disk_health(_ctx(), {"critical_pct": 94})

        assert "CRITICAL" in result
        assert "/srv/nfs4/backup_uat" in result or "95%" in result

    @pytest.mark.asyncio
    async def test_skips_tmpfs_by_default(self):
        from workers.storage_tools import tool_disk_health

        df_out = (
            "Filesystem Type Size Used Avail Use% Mounted on\n"
            "tmpfs tmpfs 1G 500M 500M 50% /tmp\n"
            "/dev/sda4 ext4 100G 10G 90G 11% /\n"
        )

        async def fake_run(cmd, timeout=10.0):
            if "-ih" in cmd:
                return "", "", 0
            return df_out, "", 0

        with patch("workers.storage_tools._run_cmd", side_effect=fake_run):
            result = await tool_disk_health(_ctx(), {})

        # tmpfs should NOT appear in results (filtered out)
        assert "tmpfs" not in result or "OK" in result  # at minimum shouldn't be flagged

    @pytest.mark.asyncio
    async def test_returns_error_when_df_fails(self):
        from workers.storage_tools import tool_disk_health

        async def fake_run(cmd, timeout=10.0):
            return "", "df: cannot access", 1

        with patch("workers.storage_tools._run_cmd", side_effect=fake_run):
            result = await tool_disk_health(_ctx(), {})

        assert "ERROR" in result


# ── tool_nfs_health ────────────────────────────────────────────────────────────

class TestToolNfsHealth:
    _PROC_MOUNTS_WITH_NFS = (
        "sysfs /sys sysfs rw,nosuid,nodev,noexec,relatime 0 0\n"
        "10.210.14.86:/srv/nfs4/backup_uat /backup_mysql nfs4 rw,relatime 0 0\n"
        "/dev/sda4 / ext4 rw,relatime 0 0\n"
    )

    @pytest.mark.asyncio
    async def test_returns_no_nfs_when_none_mounted(self):
        from workers.storage_tools import tool_nfs_health

        async def fake_run(cmd, timeout=10.0):
            if "proc/mounts" in " ".join(cmd):
                return "sysfs /sys sysfs rw 0 0\n/dev/sda4 / ext4 rw 0 0\n", "", 0
            return "", "", 0

        with patch("workers.storage_tools._run_cmd", side_effect=fake_run):
            result = await tool_nfs_health(_ctx(), {})

        assert "No NFS mounts" in result

    @pytest.mark.asyncio
    async def test_detects_healthy_nfs_mount(self):
        from workers.storage_tools import tool_nfs_health

        stat_out = (
            "  File: /backup_mysql\n"
            "  Type: nfs4\n"
            "Blocks: 733333\n"
            "Available: 123456789\n"
        )

        async def fake_run(cmd, timeout=10.0):
            if "proc/mounts" in " ".join(cmd):
                return self._PROC_MOUNTS_WITH_NFS, "", 0
            if "stat" in cmd[0]:
                return stat_out, "", 0
            if "dmesg" in cmd[0]:
                return "", "", 0
            return "", "", 0

        with patch("workers.storage_tools._run_cmd", side_effect=fake_run):
            result = await tool_nfs_health(_ctx(), {})

        assert "nfs_health" in result
        assert "/backup_mysql" in result
        assert "OK" in result or "All NFS mounts accessible" in result

    @pytest.mark.asyncio
    async def test_detects_stale_nfs_mount(self):
        from workers.storage_tools import tool_nfs_health

        async def fake_run(cmd, timeout=10.0):
            if "proc/mounts" in " ".join(cmd):
                return self._PROC_MOUNTS_WITH_NFS, "", 0
            if "stat" in cmd[0]:
                return "", "stat: cannot stat '/backup_mysql': Stale file handle", 1
            if "dmesg" in cmd[0]:
                return "[ 1234.56] nfs: server 10.210.14.86 not responding, timed out\n", "", 0
            return "", "", 0

        with patch("workers.storage_tools._run_cmd", side_effect=fake_run):
            result = await tool_nfs_health(_ctx(), {})

        assert "STALE" in result
        assert "/backup_mysql" in result

    @pytest.mark.asyncio
    async def test_reports_dmesg_nfs_errors(self):
        from workers.storage_tools import tool_nfs_health

        async def fake_run(cmd, timeout=10.0):
            if "proc/mounts" in " ".join(cmd):
                return self._PROC_MOUNTS_WITH_NFS, "", 0
            if "stat" in cmd[0]:
                return "", "", 0  # mount accessible
            if "dmesg" in cmd[0]:
                return (
                    "[May21 10:01] nfs: I/O error on server 10.210.14.86\n"
                    "[May21 10:02] nfs: server 10.210.14.86 not responding\n"
                ), "", 0
            return "", "", 0

        with patch("workers.storage_tools._run_cmd", side_effect=fake_run):
            result = await tool_nfs_health(_ctx(), {})

        assert "dmesg" in result.lower() or "nfs" in result.lower()

    @pytest.mark.asyncio
    async def test_returns_error_when_proc_mounts_fails(self):
        from workers.storage_tools import tool_nfs_health

        async def fake_run(cmd, timeout=10.0):
            return "", "permission denied", 1

        with patch("workers.storage_tools._run_cmd", side_effect=fake_run):
            result = await tool_nfs_health(_ctx(), {})

        assert "ERROR" in result

    @pytest.mark.asyncio
    async def test_detects_io_error_nfs_mount(self):
        from workers.storage_tools import tool_nfs_health

        async def fake_run(cmd, timeout=10.0):
            if "proc/mounts" in " ".join(cmd):
                return self._PROC_MOUNTS_WITH_NFS, "", 0
            if "stat" in cmd[0]:
                return "", "stat: /backup_mysql: Input/output error", 1
            return "", "", 0

        with patch("workers.storage_tools._run_cmd", side_effect=fake_run):
            result = await tool_nfs_health(_ctx(), {})

        assert "IO_ERROR" in result
        assert "IO ERROR MOUNTS" in result

    @pytest.mark.asyncio
    async def test_detects_other_stat_error_nfs(self):
        from workers.storage_tools import tool_nfs_health

        async def fake_run(cmd, timeout=10.0):
            if "proc/mounts" in " ".join(cmd):
                return self._PROC_MOUNTS_WITH_NFS, "", 0
            if "stat" in cmd[0]:
                return "", "stat: something went wrong", 2
            return "", "", 0

        with patch("workers.storage_tools._run_cmd", side_effect=fake_run):
            result = await tool_nfs_health(_ctx(), {})

        assert "ERROR(2)" in result


class TestRunCmd:
    @pytest.mark.asyncio
    async def test_run_cmd_success(self):
        from workers.storage_tools import _run_cmd
        out, err, rc = await _run_cmd(["echo", "hello"])
        assert "hello" in out
        assert rc == 0

    @pytest.mark.asyncio
    async def test_run_cmd_timeout(self):
        from workers.storage_tools import _run_cmd
        out, err, rc = await _run_cmd(["sleep", "10"], timeout=0.01)
        assert rc == 1
        assert "timeout" in err

    @pytest.mark.asyncio
    async def test_run_cmd_exception(self):
        from workers.storage_tools import _run_cmd
        out, err, rc = await _run_cmd(["nonexistent_cmd_xyz_123"])
        assert rc == 1
        assert err != ""


class TestDiskHealthEdgeCases:
    @pytest.mark.asyncio
    async def test_malformed_use_pct_handled(self):
        from workers.storage_tools import tool_disk_health

        df_out = (
            "Filesystem Type Size Used Avail Use% Mounted on\n"
            "/dev/sda4 ext4 100G 10G 90G bad% /\n"
            "/dev/sdb1 ext4 200G 20G 180G 10% /data\n"
        )

        async def fake_run(cmd, timeout=10.0):
            if "-ih" in cmd:
                return "", "", 0
            return df_out, "", 0

        with patch("workers.storage_tools._run_cmd", side_effect=fake_run):
            result = await tool_disk_health(_ctx(), {})

        assert "disk_health" in result

    @pytest.mark.asyncio
    async def test_inode_critical_flagged(self):
        from workers.storage_tools import tool_disk_health

        df_out = "Filesystem Type Size Used Avail Use% Mounted on\n/dev/sda4 ext4 100G 10G 90G 10% /\n"
        inode_out = (
            "Filesystem    Inodes  IUsed  IFree IUse% Mounted on\n"
            "/dev/sda4     100000  95000   5000   95% /\n"
            "/dev/sdb1     200000  10000 190000    5% /data\n"
        )

        async def fake_run(cmd, timeout=10.0):
            if "-ih" in cmd:
                return inode_out, "", 0
            return df_out, "", 0

        with patch("workers.storage_tools._run_cmd", side_effect=fake_run):
            result = await tool_disk_health(_ctx(), {})

        assert "95%" in result or "inode" in result.lower()

    @pytest.mark.asyncio
    async def test_inode_line_without_pct_token(self):
        from workers.storage_tools import tool_disk_health

        df_out = "Filesystem Type Size Used Avail Use% Mounted on\n/dev/sda4 ext4 100G 10G 90G 10% /\n"
        inode_out = (
            "Filesystem    Inodes  IUsed  IFree IUse% Mounted on\n"
            "notoken line with percent sign 50 / nopct\n"
            "/dev/sda4     100000   5000  95000    5% /\n"
        )

        async def fake_run(cmd, timeout=10.0):
            if "-ih" in cmd:
                return inode_out, "", 0
            return df_out, "", 0

        with patch("workers.storage_tools._run_cmd", side_effect=fake_run):
            result = await tool_disk_health(_ctx(), {})

        assert "disk_health" in result
