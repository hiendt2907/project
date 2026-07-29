"""Validator lệnh chẩn đoán — MỘT bản duy nhất, dùng chung gateway + remote agent.

Thiết kế: `plans/unify-domain-and-diagnostic-catalog-2026-07-30.md`.

## Vì sao module này tồn tại (chứ không để mỗi bên tự kiểm)

Trước đây danh sách lệnh cho phép sống ở BA chỗ:
  1. `remote_agent/command_executor.py` — frozenset 24 lệnh
  2. `gateway/routes/agent_commands.py` — **bản sao thứ hai**, kèm comment
     *"Must stay identical"* vì `Dockerfile.gateway` không COPY `src/remote_agent/`
  3. `remote_agent/collectors/*.py` — `create_subprocess_exec` trực tiếp, KHÔNG qua
     validator nào (đang chạy `cat`, chính lệnh nằm trong `_CONTENT_READ_BLOCKED`)

Hai bản sao đồng bộ bằng tay là nợ chờ nổ; chỗ thứ ba thì đã lệch sẵn. Đặt validator ở
`src/pkg/` vì gateway KHÔNG được import `workers/` hay `remote_agent/` (INVARIANT), mà
`remote_agent` cũng phải dùng đúng logic đó — chung một hàm thì không thể drift.

## Phân vai

- **Catalogue (`config/diagnostic_commands.yaml`)** khai *lệnh nào, subcommand nào,
  đọc được đường dẫn nào, động từ SQL nào*. Dữ liệu, sửa không cần deploy code.
- **Module này** cưỡng chế catalogue + những tính chất an toàn mà một file YAML
  **không diễn đạt được**: cú pháp cluster-flag của `ps`/`dpkg`/`rpm`, ngữ pháp
  chạy-nhiều-lệnh của `mysqladmin`, `ip` là tool object+action, và hàng rào
  `WRITE_VERBS` độc lập với catalogue.

## Fail-closed

Catalogue load LỖI ⇒ **từ chối MỌI lệnh**. Không rơi về whitelist cũ: một fallback
"an toàn" chính là cách một cấu hình vỡ biến thành một chính sách khác âm thầm.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from pkg.diagnostics.command_catalog import (
    WRITE_VERBS,
    Catalog,
    CatalogError,
    CommandSpec,
    is_local_target,
    is_path_readable,
    load_catalog,
)

# Ký tự metachar của shell. `create_subprocess_exec` không đi qua shell nên đây là
# phòng thủ theo lớp (chống argument smuggling khi payload bị nối chuỗi ở đâu đó),
# nhưng vẫn giữ vì nó đã chặn thật nhiều lần trong lịch sử module này.
_SHELL_INJECTION_RE = re.compile(r"[|;&`$><]|\$\(|\$\{")

# Cờ mang theo một CÂU LỆNH DB. Giá trị của nó được MIỄN kiểm metachar (SQL hợp lệ
# có `;`, `>`), nhưng bị kiểm bằng luật SQL riêng bên dưới — chặt hơn, không lỏng hơn.
_STATEMENT_FLAGS = frozenset({"-e", "--execute", "-c", "--command", "--eval"})

# `SELECT` chỉ được chạm schema hệ thống. `SELECT * FROM customers` là rút dữ liệu
# khách hàng, dù nó "read-only" về mặt SQL.
_SYSTEM_SCHEMA_PREFIXES: tuple[str, ...] = (
    "information_schema.", "performance_schema.", "mysql.", "sys.", "stats.",
    "pg_catalog.", "monitor.", "runtime_", "global_variables",
)
_SYSTEM_TABLE_PREFIXES: tuple[str, ...] = ("pg_", "stats_", "information_schema", "performance_schema")

# ── Quirk guard: tính chất an toàn mà YAML không diễn đạt được ────────────────

# find: cờ biến một liệt kê metadata thành exec tuỳ ý / đọc nội dung.
_FIND_DANGEROUS_FLAGS = frozenset({
    "-exec", "-execdir", "-delete", "-fprint", "-fprintf", "-fls", "-ok", "-okdir", "-cat",
})

# ps BSD `e` (cluster không gạch đầu) in ENVIRONMENT của mọi process — rò rỉ secret
# xuyên process. Một số build procps-ng còn hiểu `-aux` như `aux`, nên dấu gạch KHÔNG
# được là giấy thông hành cho cluster nhiều chữ; chỉ POSIX `-e` (một chữ, nghĩa
# "select all") được miễn.
_PS_ENV_LONGFLAGS = frozenset({"--environ", "--environment"})
_PS_BSD_FLAG_LETTERS = frozenset({"a", "u", "x", "w", "e"})

# dpkg: allowlist cờ truy vấn. Allowlist (không blocklist) để một cờ mới của dpkg
# phiên bản sau mặc định BỊ CHẶN, chứ không âm thầm được phép.
_DPKG_SAFE_FLAGS = frozenset({
    "-l", "-s", "-L", "-p", "-S",
    "--status", "--listfiles", "--print-avail", "--search", "--list", "--get-selections",
})

_RPM_DESTRUCTIVE_LONGFLAGS = frozenset({
    "--install", "--erase", "--upgrade", "--freshen", "--reinstall",
    "--force", "--nodeps", "--replacepkgs", "--justdb",
})
_RPM_DESTRUCTIVE_SHORTLETTERS = frozenset({"i", "e", "U", "F"})

# mysqladmin chạy MỌI token không phải cờ như một lệnh riêng, tuần tự
# (`mysqladmin status shutdown` chạy cả hai), và `-h`/`-u` có thể ăn token kế tiếp làm
# giá trị — cả hai tính chất đều phá một scanner "token không-cờ đầu tiên". Nên: không
# cờ nào, đúng một arg, và arg đó phải là động từ read-only.
_MYSQLADMIN_READONLY = frozenset({
    "status", "extended-status", "ping", "processlist", "version", "variables",
})

# ip là tool object+action (`ip route add`, `ip link set eth0 down`); action đứng ở
# slot positional nào cũng được nên chặn theo sự hiện diện, không theo vị trí.
_IP_MUTATING_SUBCOMMANDS = frozenset({
    "add", "del", "delete", "change", "replace", "set", "flush", "append", "prepend",
})

# Đường dẫn tuyệt đối tới catalogue trên máy KHÁCH. Bundle agent cài package vào
# site-packages nên layout repo (`<root>/config/…`) không còn đúng ở đó.
_ENV_CATALOG_FILE = "OMNI_DIAG_CATALOG_FILE"

_CATALOG: Catalog | None = None
_CATALOG_ERROR: str = ""


def reset_catalog_cache() -> None:
    """Xoá cache (test, và reload có chủ đích sau khi sửa catalogue)."""
    global _CATALOG, _CATALOG_ERROR
    _CATALOG = None
    _CATALOG_ERROR = ""


def get_catalog() -> Catalog:
    """Catalogue đã cache ở mức module.

    Cache vì validator chạy trên host KHÁCH: đọc file mỗi lệnh là tự tạo I/O trên hạ
    tầng của khách hàng. Cache cả LỖI để một catalogue vỡ không biến thành một vòng
    lặp stat() mỗi lần poll.
    """
    global _CATALOG, _CATALOG_ERROR
    if _CATALOG is not None:
        return _CATALOG
    if _CATALOG_ERROR:
        raise CatalogError(_CATALOG_ERROR)
    override = (os.environ.get(_ENV_CATALOG_FILE) or "").strip()
    paths = [Path(p) for p in override.split(":") if p.strip()] if override else None
    try:
        _CATALOG = load_catalog(paths=paths)
    except CatalogError as exc:
        _CATALOG_ERROR = str(exc)
        raise
    return _CATALOG


def basename(command: str) -> str:
    return (command or "").strip().lstrip("/").split("/")[-1]


def _looks_like_path(arg: str) -> bool:
    return not arg.startswith("-") and ("/" in arg)


def _statement_args(args: list[str]) -> tuple[list[str], set[int]]:
    """Trả về (các câu lệnh DB, index các arg là câu lệnh đó).

    Cần index để MIỄN đúng những arg này khỏi scan metachar — chứ không miễn cả dòng.
    """
    stmts: list[str] = []
    idx: set[int] = set()
    skip_next = False
    for i, arg in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        low = arg.lower()
        if low in _STATEMENT_FLAGS:
            if i + 1 < len(args):
                stmts.append(args[i + 1])
                idx.add(i + 1)
                skip_next = True
            continue
        if "=" in arg and low.split("=", 1)[0] in _STATEMENT_FLAGS:
            stmts.append(arg.split("=", 1)[1])
            idx.add(i)
    return stmts, idx


def _check_statement(stmt: str, spec: CommandSpec) -> tuple[bool, str]:
    """Câu lệnh DB chỉ được là MỘT câu, động từ đầu nằm trong `statement_verbs`."""
    body = stmt.strip().rstrip(";").strip()
    if not body:
        return False, "statement_empty"
    if ";" in body:
        # Nhiều câu lệnh trong một arg — `SHOW STATUS; DROP TABLE t` chỉ có câu đầu
        # được kiểm nếu ta chỉ đọc động từ đầu tiên.
        return False, "statement_multiple_not_allowed"
    if "--" in body or "/*" in body:
        return False, "statement_comment_not_allowed"
    verb = body.split(None, 1)[0].lower().lstrip("(")
    allowed = {v.lower() for v in spec.statement_verbs}
    if verb not in allowed:
        return False, f"statement_verb_not_allowed:{verb}"
    if verb == "select":
        return _check_select_scope(body)
    return True, ""


def _check_select_scope(body: str) -> tuple[bool, str]:
    """`SELECT` chỉ trên schema hệ thống — bảng nghiệp vụ là dữ liệu khách hàng."""
    tokens = re.split(r"[\s(),]+", body.lower())
    try:
        target = tokens[tokens.index("from") + 1]
    except (ValueError, IndexError):
        # Không có FROM (`SELECT VERSION()`, `SELECT 1`) — không chạm bảng nào.
        return True, ""
    target = target.strip('`"')
    if target.startswith(_SYSTEM_SCHEMA_PREFIXES) or target.startswith(_SYSTEM_TABLE_PREFIXES):
        return True, ""
    return False, f"select_outside_system_schema:{target}"


def _quirk_guard(base: str, args: list[str]) -> tuple[bool, str]:
    """Ngữ pháp CLI mà catalogue không diễn đạt được. Chạy TRƯỚC kiểm catalogue."""
    if base == "find":
        for flag in args:
            if flag.split("=", 1)[0] in _FIND_DANGEROUS_FLAGS:
                return False, f"find_dangerous_flag_blocked: {flag}"

    if base == "ps":
        for arg in args:
            if arg.lower() in _PS_ENV_LONGFLAGS:
                return False, f"ps_environment_flag_blocked: {arg}"
            if "=" in arg:
                continue
            letters = arg[1:] if arg.startswith("-") else arg
            if arg.startswith("-") and len(letters) == 1:
                continue
            if letters and letters.isalpha() and "e" in letters and all(
                ch in _PS_BSD_FLAG_LETTERS for ch in letters
            ):
                return False, f"ps_environment_flag_blocked: {arg}"

    if base == "mysqladmin":
        if any(a.startswith("-") for a in args):
            return False, f"mysqladmin_flags_not_allowed: {' '.join(args)}"
        if len(args) != 1 or args[0].lower() not in _MYSQLADMIN_READONLY:
            return False, f"mysqladmin_subcommand_not_allowed: {args[0] if args else ''}"

    if base == "dpkg":
        for a in args:
            if a.startswith("-") and a not in _DPKG_SAFE_FLAGS:
                return False, f"dpkg_flag_not_allowed: {a}"

    if base == "rpm":
        has_query = False
        for a in args:
            if a in ("-q", "--query"):
                has_query = True
                continue
            if a.lower() in _RPM_DESTRUCTIVE_LONGFLAGS:
                return False, f"rpm_destructive_flag_blocked: {a}"
            if a.startswith("-") and not a.startswith("--"):
                letters = a[1:]
                if "q" in letters:
                    # Cluster đã có -q: i/e ở đây nghĩa info/etc, không phải
                    # install/erase.
                    has_query = True
                    continue
                if any(ch in _RPM_DESTRUCTIVE_SHORTLETTERS for ch in letters):
                    return False, f"rpm_destructive_flag_blocked: {a}"
        if not has_query:
            return False, "rpm_query_mode_required"

    if base == "ip":
        for a in args:
            if not a.startswith("-") and a.lower() in _IP_MUTATING_SUBCOMMANDS:
                return False, f"ip_mutating_subcommand_blocked: {a}"

    return True, ""


def _first_positional(args: list[str], *, skip: set[int]) -> str | None:
    for i, arg in enumerate(args):
        if i in skip:
            continue
        if not arg.startswith("-"):
            return arg
    return None


def validate_command(
    command: str, args: list[str] | None = None, *, catalog: Catalog | None = None
) -> tuple[bool, str]:
    """Một câu trả lời duy nhất cho "lệnh này có được chạy không".

    Gateway gọi để fail-fast trước khi enqueue; agent gọi để cưỡng chế lần cuối trước
    khi exec; collectors gọi trước mỗi `create_subprocess_exec`. Cùng hàm ⇒ không thể
    lệch nhau (`tests/test_diagnostic_catalog_unification.py` pin tính chất này).
    """
    argv = list(args or [])
    base = basename(command)

    try:
        cat = catalog if catalog is not None else get_catalog()
    except CatalogError as exc:
        # FAIL-CLOSED. Không rơi về whitelist cũ.
        return False, f"catalog_unavailable: {exc}"

    spec = cat.get(base)
    if spec is None:
        return False, f"command_not_whitelisted: {base}"

    stmts, stmt_idx = _statement_args(argv)

    # Metachar: bỏ qua arg là câu lệnh DB (SQL hợp lệ có `;`/`>`), phần còn lại kiểm hết.
    scanned = " ".join([base] + [a for i, a in enumerate(argv) if i not in stmt_idx])
    if _SHELL_INJECTION_RE.search(scanned):
        return False, f"shell_injection_detected in: {scanned[:80]}"

    ok, reason = _quirk_guard(base, argv)
    if not ok:
        return False, reason

    # Đích mạng: lệnh khai `local_targets_only` chỉ được nhắm host cục bộ / mạng riêng.
    # `curl` chặn được `-o`/`-d`/`-X` nhưng KHÔNG có cờ nào ngăn trỏ tới host Internet,
    # mà một GET kèm query string đã là kênh đẩy dữ liệu ra — và cũng là đường quét
    # dịch vụ nội bộ từ trong mạng khách. Giá trị chẩn đoán thật là "endpoint của CHÍNH
    # host này trả 5xx", nên giới hạn này không mất năng lực nào.
    if spec.local_targets_only:
        targets = [
            a for i, a in enumerate(argv)
            if i not in stmt_idx and not a.startswith("-") and ("://" in a or "." in a or a.isalnum())
        ]
        if not targets:
            return False, "local_targets_only_but_no_target"
        for t in targets:
            ok_t, why_t = is_local_target(t)
            if not ok_t:
                return False, why_t

    # Hàng rào độc lập catalogue: bất kỳ arg nào là động từ GHI ⇒ chặn. Giữ lại kể cả
    # khi catalogue khai lỏng — `systemctl restart nginx` phải chết ở đây.
    for arg in argv:
        if arg.lstrip("-").lower() in WRITE_VERBS:
            return False, f"write_verb_blocked: {arg}"

    for arg in argv:
        flag = arg.split("=", 1)[0]
        if flag in spec.deny_flags or arg in spec.deny_flags:
            return False, f"flag_denied: {arg}"

    for arg in argv:
        if arg.lstrip("-").lower() in {d.lower() for d in spec.deny_subcommands}:
            return False, f"subcommand_denied:{arg}"

    # Client DB lấy hành động từ câu lệnh, không từ positional (`-e` ăn token kế
    # tiếp), nên kiểm subcommand ở đó là kiểm sai chỗ.
    if not spec.statement_verbs:
        sub = _first_positional(argv, skip=stmt_idx)
        if sub is not None and spec.subcommands:
            ok, reason = spec.allows_subcommand(sub)
            if not ok:
                return False, reason

    if spec.reads_content:
        # INV_DIAG_SCOPE_BOUNDED: đọc được, nhưng chỉ trong phạm vi đã khai.
        for i, arg in enumerate(argv):
            if i in stmt_idx or not _looks_like_path(arg):
                continue
            ok, reason = is_path_readable(arg, spec)
            if not ok:
                return False, reason

    if spec.statement_verbs and not stmts:
        # `redis-cli info memory`, `mysqladmin`-style: một số client nhận câu lệnh ở
        # positional chứ không qua `-e`. Không suy ra được thì positional phải chịu
        # ĐÚNG luật statement — nếu bỏ qua, cả `redis-cli get <key>` sẽ lọt.
        positional = " ".join(a for a in argv if not a.startswith("-"))
        if positional.strip():
            stmts = [positional]
        # Không có gì cả ⇒ client mở shell tương tác; không tty nên vô hại.

    for stmt in stmts:
        ok, reason = _check_statement(stmt, spec)
        if not ok:
            return False, reason

    return True, ""


__all__ = [
    "basename",
    "get_catalog",
    "reset_catalog_cache",
    "validate_command",
]
