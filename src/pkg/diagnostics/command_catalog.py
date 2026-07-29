"""Catalogue lệnh chẩn đoán — KHAI BÁO, dùng chung gateway + remote agent.

Thiết kế: `plans/unify-domain-and-diagnostic-catalog-2026-07-30.md`.

## Vì sao tồn tại

Trước module này, danh sách lệnh cho phép bị hardcode ở BA chỗ:
  1. `remote_agent/command_executor.py:83` — frozenset 24 lệnh
  2. `gateway/routes/agent_commands.py:83` — **bản sao thứ hai**, kèm comment
     *"Must stay identical"* vì `Dockerfile.gateway` không COPY `src/remote_agent/`
  3. `remote_agent/collectors/*.py` — gọi `create_subprocess_exec` trực tiếp, KHÔNG
     qua validator nào (đang chạy `cat`, chính lệnh nằm trong `_CONTENT_READ_BLOCKED`)

Hai bản sao đồng bộ bằng tay là nợ chờ nổ; chỗ thứ ba thì đã lệch sẵn.

## Bất biến

- **Catalogue KHÔNG cấp quyền mutate cho bất kỳ domain nào.** Mutation chỉ đi qua K8s
  SDK (`MUTATE_TOOL_ALLOWLIST`) và capability có kiểu (`aoip/capabilities/`).
- **Fail-closed ở tầng LOAD, không ở tầng gọi.** Entry nào khai một subcommand mang
  nghĩa ghi thì `load_catalog()` **ném lỗi**, không cảnh báo. Người sửa YAML không thể
  vô tình mở đường mutate; và lỗi bật ra lúc khởi động chứ không lúc ai đó gọi lệnh.
- `INV_DIAG_SCOPE_BOUNDED` thay `INV_NO_DATA_EXFIL`: đọc nội dung ĐƯỢC, nhưng chỉ
  trong phạm vi đường dẫn vận hành đã khai. Chặn theo tên lệnh (`cat`, `tail`, `grep`)
  làm Omni không bao giờ đọc được log ứng dụng — tức không thể chẩn đoán tầng app.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from pkg.domain.taxonomy import require_domain

# Động từ mang nghĩa GHI. Xuất hiện trong `subcommands` của bất kỳ entry nào ⇒ load lỗi.
# Danh sách này là hàng rào cuối giữa "catalogue chẩn đoán" và "đường mutate lậu".
WRITE_VERBS: frozenset[str] = frozenset({
    "apply", "create", "delete", "remove", "rm", "patch", "edit", "replace", "set",
    "scale", "drain", "cordon", "uncordon", "taint", "annotate", "label",
    "start", "stop", "restart", "reload", "enable", "disable", "mask", "unmask",
    "kill", "terminate", "signal", "reset-failed", "daemon-reload",
    "install", "upgrade", "uninstall", "purge", "update",
    "exec", "attach", "cp", "port-forward", "proxy", "run", "expose",
    "mkfs", "fsck", "mount", "umount", "dd", "chmod", "chown", "truncate",
    "flushall", "flushdb", "shutdown", "failover", "migrate",
    "insert", "update-row", "drop", "alter", "truncate-table", "grant", "revoke",
    "vacuum", "reindex", "cluster",
})

# Đường dẫn đọc được mặc định — hạ tầng vận hành, không phải dữ liệu nghiệp vụ.
DEFAULT_READ_ALLOW: tuple[str, ...] = ("/proc", "/sys", "/etc", "/var/log", "/run", "/dev/shm")

# Chặn cứng, không entry nào override được: dữ liệu khách hàng và bí mật.
# Đây là phần CÒN LẠI của INV_NO_DATA_EXFIL sau khi nới theo phạm vi.
HARD_READ_DENY: tuple[str, ...] = (
    "/var/lib/mysql", "/var/lib/postgresql", "/var/lib/mongodb", "/var/lib/redis",
    "/var/lib/clickhouse", "/var/lib/elasticsearch", "/var/backups", "/backup",
    "/home", "/root", "/srv", "/media", "/mnt",
)

# Hai họ mẫu, cố ý tách rời vì neo khác nhau:
#
#  - _SECRET_NAME: khớp theo TÊN file/thư mục, neo `(^|/)`.
#  - _SECRET_EXT: khớp theo ĐUÔI file, neo `$`.
#
# Gộp cả hai vào một mẫu neo `(^|/)` là lỗi đã từng tồn tại ở đây: `\.key` chỉ khớp file
# tên đúng là `.key`, nên `/etc/ssl/private/server.key` và `/etc/pki/tls/cert.pem` đọc
# được bình thường qua `cat` (vì read_allow có `/etc`). Khoá riêng của TLS là thứ đắt
# nhất trên một host — mất nó là mất luôn mọi phiên đã ghi lại được.
_SECRET_NAME = re.compile(
    r"(^|/)(\.env(\.|$)|\.git(/|$)|id_rsa|id_ecdsa|id_ed25519|authorized_keys|"
    r"shadow|gshadow|htpasswd|credentials|\.aws(/|$)|\.ssh(/|$)|"
    r"\.docker(/|$)|\.pgpass|\.my\.cnf|\.netrc|\.npmrc|kubeconfig)",
    re.IGNORECASE,
)
# Đuôi file khoá, VÀ hậu tố `_key` không có đuôi (OpenSSH đặt tên
# `ssh_host_rsa_key` — không đuôi, nên mẫu theo đuôi một mình sẽ bỏ lọt).
_SECRET_EXT = re.compile(
    r"(\.(pem|key|p12|pfx|jks|keystore|truststore|ppk|gpg|asc|kdbx)$|_key$)",
    re.IGNORECASE,
)
# Thư mục chỉ chứa khoá riêng — chặn cả cây, không xét tên.
#
# Cố ý KHÔNG chặn cả `/etc/ssh`: `sshd_config` có giá trị chẩn đoán thật (cấu hình
# auth, port, cipher) và chặn nó là mất năng lực mà chẳng bảo vệ được gì — khoá riêng
# ở đó tên `ssh_host_*_key`, đã bị `_SECRET_EXT` bắt qua hậu tố `_key`. Chặn quá rộng
# cũng là một dạng hỏng: nó đẩy người vận hành đi tìm đường lách.
_SECRET_DIRS: tuple[str, ...] = (
    "/etc/ssl/private", "/etc/pki/tls/private", "/etc/pki/CA/private",
    "/root/.ssh", "/var/lib/kubelet/pki", "/etc/kubernetes/pki",
)


def _looks_secret(path: str) -> bool:
    if _SECRET_NAME.search(path) or _SECRET_EXT.search(path):
        return True
    return any(path == d or path.startswith(d.rstrip("/") + "/") for d in _SECRET_DIRS)

_ENV_EXTRA_CATALOG = "OMNI_DIAG_COMMAND_CATALOG"
_DEFAULT_CATALOG = Path(__file__).resolve().parents[3] / "config" / "diagnostic_commands.yaml"


class CatalogError(ValueError):
    """Catalogue không hợp lệ. Ném lúc LOAD để lỗi bật ra khi khởi động."""


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """Một lệnh chẩn đoán được phép, kèm biên của nó."""

    command: str
    domain: str
    subcommands: frozenset[str] = frozenset()
    deny_subcommands: frozenset[str] = frozenset()
    deny_flags: frozenset[str] = frozenset()
    # Lệnh này có đọc nội dung file/DB không. True ⇒ phải qua kiểm phạm vi đường dẫn.
    reads_content: bool = False
    read_allow: tuple[str, ...] = DEFAULT_READ_ALLOW
    # Câu lệnh DB chỉ được là các động từ chẩn đoán (SHOW/EXPLAIN/...); rỗng = không phải DB.
    statement_verbs: frozenset[str] = frozenset()
    # Lệnh có khả năng gửi request ra ngoài (curl, ping, dig, traceroute...) chỉ được
    # nhắm vào host cục bộ / mạng riêng. Không có cờ nào của curl chặn được việc trỏ
    # tới một host Internet, mà một GET kèm query string đã là kênh đẩy dữ liệu ra —
    # và cũng là đường quét dịch vụ nội bộ. Giá trị chẩn đoán thật của curl là chứng
    # minh "endpoint của CHÍNH host này trả 5xx", nên giới hạn này không mất gì.
    local_targets_only: bool = False
    timeout_s: float = 20.0
    note: str = ""

    def allows_subcommand(self, sub: str) -> tuple[bool, str]:
        s = (sub or "").strip().lstrip("-").lower()
        if s in {d.lower() for d in self.deny_subcommands}:
            return False, f"subcommand_denied:{s}"
        if not self.subcommands:
            return True, ""
        if s in {a.lower() for a in self.subcommands}:
            return True, ""
        return False, f"subcommand_not_in_catalog:{s}"


@dataclass(frozen=True, slots=True)
class Catalog:
    specs: dict[str, CommandSpec] = field(default_factory=dict)
    source_files: tuple[str, ...] = ()

    def get(self, command: str) -> CommandSpec | None:
        return self.specs.get((command or "").strip().lower())

    def commands(self) -> tuple[str, ...]:
        return tuple(sorted(self.specs))

    def by_domain(self, domain: str) -> tuple[CommandSpec, ...]:
        d = require_domain(domain)
        return tuple(s for s in self.specs.values() if s.domain == d)

    def domains(self) -> tuple[str, ...]:
        return tuple(sorted({s.domain for s in self.specs.values()}))


def _as_frozenset(value: Any, *, where: str) -> frozenset[str]:
    if value is None:
        return frozenset()
    if not isinstance(value, (list, tuple, set)):
        raise CatalogError(f"{where}: phai la danh sach, nhan duoc {type(value).__name__}")
    return frozenset(str(v).strip() for v in value if str(v).strip())


def _spec_from_dict(raw: dict[str, Any]) -> CommandSpec:
    cmd = str(raw.get("command") or "").strip().lower()
    if not cmd:
        raise CatalogError("entry thieu 'command'")
    if "/" in cmd or cmd.startswith("."):
        # Tên lệnh phải là basename — đường dẫn tuyệt đối trong catalogue là đường
        # vòng để chỉ tới một binary không nằm trong PATH tin cậy.
        raise CatalogError(f"{cmd}: 'command' phai la ten lenh, khong phai duong dan")

    try:
        domain = require_domain(raw.get("domain"))
    except ValueError as exc:
        raise CatalogError(f"{cmd}: {exc}") from exc

    subs = _as_frozenset(raw.get("subcommands"), where=f"{cmd}.subcommands")

    # HÀNG RÀO CHÍNH: không entry nào được khai động từ ghi. Kiểm ở đây nghĩa là một
    # PR sửa YAML sẽ làm test/khởi động vỡ, chứ không âm thầm cấp quyền mutate.
    offending = sorted(s for s in subs if s.strip().lower() in WRITE_VERBS)
    if offending:
        raise CatalogError(
            f"{cmd}: subcommand mang nghia GHI khong duoc phep trong catalogue chan doan: "
            f"{', '.join(offending)} — mutation chi di qua K8s SDK hoac aoip/capabilities"
        )

    reads = bool(raw.get("reads_content", False))
    allow_raw = raw.get("read_allow")
    read_allow = tuple(str(p) for p in allow_raw) if allow_raw else DEFAULT_READ_ALLOW
    bad = [p for p in read_allow if any(p.rstrip("/").startswith(d) for d in HARD_READ_DENY)]
    if bad:
        raise CatalogError(
            f"{cmd}.read_allow chua duong dan bi chan cung: {', '.join(bad)} — "
            f"du lieu khach hang khong duoc doc du catalogue co khai"
        )

    return CommandSpec(
        command=cmd,
        domain=domain,
        subcommands=subs,
        deny_subcommands=_as_frozenset(raw.get("deny_subcommands"), where=f"{cmd}.deny_subcommands"),
        deny_flags=_as_frozenset(raw.get("deny_flags"), where=f"{cmd}.deny_flags"),
        reads_content=reads,
        read_allow=read_allow,
        statement_verbs=_as_frozenset(raw.get("statement_verbs"), where=f"{cmd}.statement_verbs"),
        local_targets_only=bool(raw.get("local_targets_only", False)),
        timeout_s=float(raw.get("timeout_s", 20.0)),
        note=str(raw.get("note") or ""),
    )


def _parse(text: str, *, origin: str) -> list[dict[str, Any]]:
    """Đọc YAML nếu có PyYAML, ngược lại thử JSON.

    Agent chạy trên máy khách với bundle tối giản — không chắc có PyYAML. Nên
    catalogue phải đọc được cả khi chỉ có stdlib, thay vì thêm một dependency bắt
    buộc vào tiến trình chạy trên hạ tầng của khách.
    """
    try:
        import yaml  # type: ignore[import-untyped]

        data = yaml.safe_load(text)
    except ImportError:
        import json

        try:
            data = json.loads(text)
        except ValueError as exc:
            raise CatalogError(
                f"{origin}: khong co PyYAML va noi dung khong phai JSON — "
                f"dung dinh dang JSON cho ban dong goi khong co yaml ({exc})"
            ) from exc
    except Exception as exc:  # noqa: BLE001
        raise CatalogError(f"{origin}: YAML khong hop le: {exc}") from exc

    if isinstance(data, dict):
        data = data.get("commands")
    if not isinstance(data, list):
        raise CatalogError(f"{origin}: mong doi danh sach entry (hoac khoa 'commands')")
    return [d for d in data if isinstance(d, dict)]


def load_catalog(
    *, paths: list[Path] | None = None, env: dict[str, str] | None = None
) -> Catalog:
    """Nạp catalogue. Ném ``CatalogError`` nếu bất kỳ entry nào không hợp lệ.

    Mở rộng KHÔNG cần sửa code: `OMNI_DIAG_COMMAND_CATALOG` trỏ tới file bổ sung
    (nhiều file cách nhau bằng `:`), merge theo `command` — file sau ghi đè file trước.
    """
    src = os.environ if env is None else env
    files: list[Path] = list(paths) if paths else [_DEFAULT_CATALOG]
    extra = (src.get(_ENV_EXTRA_CATALOG) or "").strip()
    if extra:
        files += [Path(p) for p in extra.split(":") if p.strip()]

    specs: dict[str, CommandSpec] = {}
    used: list[str] = []
    for f in files:
        if not f.exists():
            if paths is None and f == _DEFAULT_CATALOG:
                raise CatalogError(f"khong tim thay catalogue mac dinh: {f}")
            raise CatalogError(f"catalogue khong ton tai: {f}")
        for raw in _parse(f.read_text(encoding="utf-8"), origin=str(f)):
            spec = _spec_from_dict(raw)
            prev = specs.get(spec.command)
            # Merge: file sau chỉ được SIẾT thêm, không nới. deny_* hợp lại.
            if prev is not None:
                spec = replace(
                    spec,
                    deny_subcommands=prev.deny_subcommands | spec.deny_subcommands,
                    deny_flags=prev.deny_flags | spec.deny_flags,
                )
            specs[spec.command] = spec
        used.append(str(f))

    if not specs:
        raise CatalogError("catalogue rong — fail-closed, khong chay lenh nao")
    return Catalog(specs=specs, source_files=tuple(used))


def is_path_readable(path: str, spec: CommandSpec) -> tuple[bool, str]:
    """`INV_DIAG_SCOPE_BOUNDED` — đọc được, nhưng chỉ trong phạm vi đã khai.

    Chuẩn hoá đường dẫn TRƯỚC khi so: `..` và symlink-style traversal là cách cổ điển
    để biến `/var/log/../../home/khach/.ssh/id_rsa` thành một đường dẫn "hợp lệ".
    """
    raw = (path or "").strip()
    if not raw:
        return False, "empty_path"
    p = os.path.normpath(raw if raw.startswith("/") else "/" + raw)
    if _looks_secret(p):
        return False, f"secret_like_path:{p}"
    for deny in HARD_READ_DENY:
        if p == deny or p.startswith(deny.rstrip("/") + "/"):
            return False, f"hard_denied_path:{p}"
    for allow in spec.read_allow:
        a = allow.rstrip("/")
        if p == a or p.startswith(a + "/"):
            return True, ""
    return False, f"path_out_of_scope:{p}"


__all__ = [
    "Catalog",
    "CatalogError",
    "CommandSpec",
    "DEFAULT_READ_ALLOW",
    "HARD_READ_DENY",
    "WRITE_VERBS",
    "is_local_target",
    "is_path_readable",
    "load_catalog",
]


# ---------------------------------------------------------------------------
# Giới hạn đích mạng — cho lệnh khai `local_targets_only`
# ---------------------------------------------------------------------------

_LOCAL_HOSTNAMES: frozenset[str] = frozenset({
    "localhost", "localhost.localdomain", "ip6-localhost", "127.0.0.1", "::1", "[::1]",
})


def is_local_target(target: str) -> tuple[bool, str]:
    """``target`` (URL hoặc host[:port]) có trỏ vào host cục bộ / mạng riêng không.

    Vì sao cần: `curl` không có cờ nào ngăn trỏ tới một host Internet, mà một GET kèm
    query string đã là kênh đẩy dữ liệu ra ngoài — và cũng là đường quét dịch vụ nội
    bộ từ trong mạng khách. Chặn theo cờ (`-o`, `-d`, `-X`) bịt được việc ghi file và
    gửi body, nhưng KHÔNG bịt được đích.
    """
    import ipaddress
    from urllib.parse import urlsplit

    t = (target or "").strip()
    if not t:
        return False, "empty_target"
    if "://" in t:
        parts = urlsplit(t)
        if parts.scheme.lower() not in ("http", "https"):
            return False, f"scheme_not_allowed:{parts.scheme}"
        host = parts.hostname or ""
    else:
        host = t.split("/", 1)[0].rsplit(":", 1)[0] if t.count(":") == 1 else t.split("/", 1)[0]
    host = host.strip("[]").lower()
    if not host:
        return False, "no_host"
    if host in _LOCAL_HOSTNAMES:
        return True, ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # Tên miền: KHÔNG resolve ở đây. Resolve rồi tin kết quả là mở cửa cho
        # DNS rebinding — bản ghi trả IP riêng lúc kiểm rồi trả IP công lúc gọi.
        # Chỉ nhận hostname không có dấu chấm (tên máy trong mạng nội bộ).
        if "." not in host:
            return True, ""
        return False, f"remote_host_not_allowed:{host}"
    if ip.is_loopback or ip.is_private or ip.is_link_local:
        return True, ""
    return False, f"public_ip_not_allowed:{host}"
