"""Catalogue lệnh chẩn đoán — MỘT nguồn sự thật cho gateway, agent và collectors.

Trước đợt này danh sách lệnh cho phép sống ở BA chỗ: frozenset trong
`remote_agent/command_executor.py`, một BẢN SAO trong `gateway/routes/agent_commands.py`
(kèm comment "Must stay identical", vì Dockerfile.gateway không COPY src/remote_agent/),
và `remote_agent/collectors/*.py` gọi subprocess trực tiếp không qua validator nào —
chỗ thứ ba đã lệch sẵn (đang chạy `cat`, lệnh nằm trong blocklist của chỗ thứ nhất).

Test ở đây pin đúng cái không được vỡ lại:
  1. hành vi cụ thể trên đường mới (systemctl restart / kubectl get / cat / mysql)
  2. catalogue load lỗi ⇒ TỪ CHỐI MỌI LỆNH, không rơi về whitelist cũ
  3. gateway và agent trả CÙNG câu trả lời trên cùng input (chống drift trở lại)
"""

from __future__ import annotations

import json

import pytest

from gateway.routes import agent_commands as gw
from pkg.diagnostics import validator as val
from remote_agent import command_executor
from remote_agent.command_executor import _is_command_allowed


@pytest.fixture(autouse=True)
def _fresh_catalog():
    """Catalogue cache ở module level (tránh I/O mỗi lệnh trên host khách), nên test
    phải tự dọn — nếu không, test đầu tiên set môi trường lỗi sẽ đầu độc các test sau."""
    val.reset_catalog_cache()
    yield
    val.reset_catalog_cache()


class TestRequiredBehaviour:
    def test_systemctl_restart_blocked(self):
        allowed, reason = _is_command_allowed("systemctl", ["restart", "nginx"])
        assert allowed is False
        assert reason

    def test_kubectl_get_pods_allowed(self):
        """Trước đây kubectl KHÔNG có trong whitelist nào — Omni không đọc được state
        cluster qua đường agent, dù đó là domain lõi của nó."""
        allowed, reason = _is_command_allowed("kubectl", ["get", "pods", "-n", "multi-agent"])
        assert allowed is True, reason

    def test_kubectl_mutation_still_blocked(self):
        for args in (["delete", "pod", "x"], ["apply", "-f", "x.yaml"], ["exec", "pod", "--", "sh"]):
            allowed, reason = _is_command_allowed("kubectl", args)
            assert allowed is False, args
            assert reason

    def test_cat_etc_hosts_allowed_but_db_datafile_blocked(self):
        ok, reason = _is_command_allowed("cat", ["/etc/hosts"])
        assert ok is True, reason
        ok, reason = _is_command_allowed("cat", ["/var/lib/mysql/x.ibd"])
        assert ok is False
        assert "hard_denied" in reason

    def test_mysql_drop_blocked_show_slave_status_allowed(self):
        ok, reason = _is_command_allowed("mysql", ["-e", "DROP TABLE t"])
        assert ok is False
        assert "statement_verb_not_allowed" in reason
        ok, reason = _is_command_allowed("mysql", ["-e", "SHOW SLAVE STATUS"])
        assert ok is True, reason

    def test_mysql_second_statement_cannot_ride_along(self):
        """Chỉ đọc động từ ĐẦU TIÊN là bỏ lọt `SHOW STATUS; DROP TABLE t`."""
        ok, reason = _is_command_allowed("mysql", ["-e", "SHOW STATUS; DROP TABLE t"])
        assert ok is False
        assert reason


class TestFailClosed:
    def test_catalog_load_failure_refuses_every_command(self, monkeypatch):
        """Catalogue vỡ ⇒ không lệnh nào chạy. KHÔNG có fallback về danh sách cũ: một
        fallback "an toàn" chính là cách một cấu hình vỡ biến thành chính sách khác."""
        monkeypatch.setenv("OMNI_DIAG_CATALOG_FILE", "/nonexistent/catalog-xyz.yaml")
        val.reset_catalog_cache()
        for cmd, args in [("df", ["-h"]), ("ps", ["-ef"]), ("kubectl", ["get", "pods"]),
                          ("cat", ["/proc/meminfo"]), ("uname", ["-a"])]:
            ok, reason = _is_command_allowed(cmd, args)
            assert ok is False, f"{cmd} phai bi tu choi khi catalogue load loi"
            assert "catalog_unavailable" in reason

    @pytest.mark.asyncio
    async def test_execute_command_does_not_spawn_when_catalog_broken(self, monkeypatch):
        monkeypatch.setenv("OMNI_DIAG_CATALOG_FILE", "/nonexistent/catalog-xyz.yaml")
        val.reset_catalog_cache()
        from unittest.mock import patch

        with patch("remote_agent.command_executor.asyncio.create_subprocess_exec") as spawn:
            result = await command_executor.execute_command("c1", "df", ["-h"])
        spawn.assert_not_called()
        assert result["blocked"] is True
        assert "catalog_unavailable" in result["block_reason"]

    def test_empty_catalog_is_a_load_error_not_an_empty_allowlist(self, tmp_path):
        from pkg.diagnostics.command_catalog import CatalogError, load_catalog

        empty = tmp_path / "empty.json"
        empty.write_text(json.dumps({"commands": []}), encoding="utf-8")
        with pytest.raises(CatalogError):
            load_catalog(paths=[empty])


class TestGatewayAgentParity:
    """Chống drift TRỞ LẠI: hai đầu phải là cùng một hàm, không phải hai bản sao."""

    _CASES = [
        ("systemctl", ["restart", "nginx"]),
        ("systemctl", ["status", "nginx"]),
        ("systemctl", ["frobnicate", "nginx"]),
        ("kubectl", ["get", "pods"]),
        ("kubectl", ["delete", "pod", "x"]),
        ("cat", ["/etc/hosts"]),
        ("cat", ["/var/lib/mysql/x.ibd"]),
        ("cat", ["/etc/shadow"]),
        ("mysql", ["-e", "SHOW SLAVE STATUS"]),
        ("mysql", ["-e", "DROP TABLE t"]),
        ("ps", ["auxe"]),
        ("ps", ["-eo", "comm"]),
        ("mysqladmin", ["status", "shutdown"]),
        ("dpkg", ["--purge", "nginx"]),
        ("rpm", ["-a"]),
        ("ip", ["route", "add", "1.2.3.4"]),
        ("find", ["/tmp", "-delete"]),
        ("rm", ["-rf", "/"]),
        ("ls", ["foo;rm"]),
        ("/tmp/x/ps", ["-ef"]),
    ]

    @pytest.mark.parametrize("cmd,args", _CASES)
    def test_same_verdict_both_sides(self, cmd, args):
        assert gw.validate_command(cmd, args) == _is_command_allowed(cmd, args)

    def test_gateway_holds_no_private_command_list(self):
        """Bản sao thứ hai đã bị xoá — nếu ai đó thêm lại, test này vỡ."""
        for attr in ("_COMMAND_WHITELIST", "_MYSQLADMIN_READONLY", "_DPKG_SAFE_FLAGS",
                     "_PS_BSD_FLAG_LETTERS", "_IP_MUTATING_SUBCOMMANDS",
                     "_RPM_DESTRUCTIVE_LONGFLAGS", "_command_args_allowed"):
            assert not hasattr(gw, attr), f"gateway lai co ban sao rieng: {attr}"

    def test_gateway_does_not_import_workers_or_remote_agent(self):
        import inspect

        src = inspect.getsource(gw)
        assert "from workers" not in src and "import workers" not in src
        # Redis key prefix `omni:remote_agent:` là dữ liệu, không phải import.
        assert "from remote_agent" not in src and "import remote_agent" not in src


class TestCollectorsGoThroughValidator:
    """Chỗ thứ ba: collectors từng gọi create_subprocess_exec trực tiếp."""

    @pytest.mark.asyncio
    async def test_blocked_collector_command_never_spawns(self):
        from unittest.mock import patch

        from remote_agent.collectors import storage

        with patch("remote_agent.collectors.storage.asyncio.create_subprocess_exec") as spawn:
            out, err, rc = await storage._run(["rm", "-rf", "/var/log"])
        spawn.assert_not_called()
        assert rc == 1
        assert "blocked" in err

    @pytest.mark.asyncio
    async def test_real_collector_commands_pass_the_guard(self):
        """Các lệnh collectors THẬT đang chạy phải qua được — nếu không, siết catalogue
        đã làm mù chính đường thu chứng cứ (im lặng, vì mọi `_run` đều never-raise)."""
        from remote_agent import exec_guard

        for cmd in (
            ["ps", "-eo", "comm"],
            ["ss", "-tlnp"],
            ["netstat", "-tlnp"],
            ["systemctl", "list-units", "--type=service", "--no-legend", "--no-pager", "--plain"],
            ["systemctl", "is-enabled", "nginx.service"],
            ["systemctl", "show", "nginx.service", "-p", "FragmentPath", "--value"],
            ["df", "-h", "--output=source,fstype,size,used,avail,pcent,target"],
            ["df", "-i", "--output=source,ipcent,target"],
            ["cat", "/proc/mounts"],
            ["cat", "/etc/os-release"],
            ["stat", "--file-system", "/"],
            ["dmesg", "-T", "--level=err,crit", "--notime"],
            ["dpkg", "-S", "/lib/systemd/system/nginx.service"],
            ["dpkg", "-l"],
            ["rpm", "-qf", "/lib/systemd/system/nginx.service"],
            ["find", "/var/log", "-maxdepth", "3", "-name", "*.log", "-not", "-empty"],
            ["mysql", "--host=127.0.0.1", "--batch", "-e", "SHOW REPLICA STATUS"],
        ):
            assert exec_guard.check(cmd) == "", cmd
