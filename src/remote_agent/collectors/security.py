"""Remote agent collector — security signals (auth failures, privilege escalation).

Probes:
  security_auth_failures         → domain=security. `lastb` (btmp) — failed login
                                    attempts, the earliest brute-force SSH signal.
  security_privilege_escalation  → domain=security. `journalctl _COMM=sudo` —
                                    sudo failures / unusual `su` activity.

Part of the FinGuard→Smart SIEM internal merge
(plans/finguard-to-smart-siem-merge-2026-08-04.md, phase S1) — this is the first real
data source for the `security` domain (previously ❌, no collector existed at all).

Ngưỡng — lệch có chủ đích so với thiết kế gốc trong plan: plan đề nghị agent gửi
`result="OBSERVED"` để Omni tự học baseline (mẫu `os_host`), vì ngưỡng "bao nhiêu lần
đăng nhập sai là tấn công" phụ thuộc từng host. Nhưng cơ chế baseline hiện tại
(`src/anomaly/remote_host_baseline.py`) hardcode đúng 3 metric (cpu/mem/disk_percent,
xem `_METRIC_DOMAIN`/`_BASELINE_METRICS`) — mở rộng nó là việc riêng, ngoài phạm vi
"viết 1 collector". Dùng tạm STATIC_GUARD (agent tự tính verdict, ngưỡng tĩnh) giống 5
domain khác (database/storage/service/network/application) để có đường end-to-end thật
ngay — chuyển sang OBSERVED/baseline là cải tiến để lại cho lượt sau.

INV_DATA_RESIDENCY (S1.2): KHÔNG bao giờ gửi dòng log thô. Chuẩn hoá NGAY TRÊN HOST thành
chuỗi `user=<x> host=<y>` khớp allowlist của `siem_correlation/entities.py` (user=/host=),
đặt vào `extracted_fact.normalized_entities` — CHỈ trường này, KHÔNG BAO GIỜ đặt vào `raw`
(luôn để `raw=""` khi gọi `build_envelope`).

All commands are read-only (whitelisted trong config/diagnostic_commands.yaml, domain
security/service — `lastb`, `journalctl`); no mutations.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from typing import Any

from pkg.domain.taxonomy import SECURITY
from remote_agent import exec_guard
from remote_agent.evidence import build_envelope

logger = logging.getLogger(__name__)

# Ngưỡng tĩnh (STATIC_GUARD) — xem docstring module về lý do chưa dùng baseline Omni.
_AUTH_FAILURE_WARN = 5
_AUTH_FAILURE_CRITICAL = 20
_PRIV_ESC_WARN = 3
_PRIV_ESC_CRITICAL = 10

# entities.py::_ALLOWLIST — chỉ 2 key này thật sự cần cho 2 probe ở đây.
_ENTITY_VALUE_RE = r"[A-Za-z0-9][A-Za-z0-9._:@/\-]*"
_MAX_ENTITIES = 16


def _safe_entity(raw: str) -> str | None:
    """Giữ lại phần khớp pattern an toàn của entities.py; bỏ qua nếu rỗng/không khớp."""
    raw = (raw or "").strip()
    if not raw:
        return None
    m = re.match(_ENTITY_VALUE_RE, raw)
    return m.group(0)[:128] if m else None


async def _run(cmd: list[str], timeout: float = 15.0) -> tuple[str, str, int]:
    """Run subprocess read-only. Never raises. Cùng exec_guard với mọi collector khác."""
    reason = exec_guard.check(cmd)
    if reason:
        return "", f"blocked: {reason}", 1
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return out.decode(errors="replace"), err.decode(errors="replace"), proc.returncode or 0
    except asyncio.TimeoutError:
        return "", "timeout", 1
    except Exception as exc:
        return "", str(exc), 1


def _parse_lastb_lines(out: str) -> list[tuple[str, str]]:
    """Mỗi dòng `lastb`: `<user> <tty> <host_or_ip> <ngày giờ...>`. Trả (user, host)."""
    pairs: list[tuple[str, str]] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        user, host = _safe_entity(parts[0]), _safe_entity(parts[2])
        if user:
            pairs.append((user, host or "unknown"))
    return pairs


async def collect_auth_failures(hostname: str) -> dict[str, Any] | None:
    """Failed login attempts via `lastb` (btmp) — earliest brute-force SSH signal."""
    out, err, rc = await _run(["lastb", "-n", "200"])
    if rc != 0:
        # `lastb` trả rc≠0 khi btmp rỗng/không tồn tại trên host chưa từng có login lỗi —
        # KHÔNG phải outage, không log warning ồn ào cho trường hợp bình thường này.
        if "no such file" in err.lower() or "btmp" in err.lower():
            logger.debug("[collector.security] lastb: no btmp yet (no failed logins)")
            return None
        logger.warning("[collector.security] lastb unavailable: %s", err[:200])
        return None

    pairs = _parse_lastb_lines(out)
    total = len(pairs)
    by_user = Counter(u for u, _ in pairs)
    entities = sorted({f"user={u} host={h}" for u, h in pairs})[:_MAX_ENTITIES]

    if total >= _AUTH_FAILURE_CRITICAL:
        result = "FAILED"
    elif total >= _AUTH_FAILURE_WARN:
        result = "FAILED"
    else:
        result = "PASSED"

    fact: dict[str, Any] = {
        "failed_login_count": total,
        "distinct_users": len(by_user),
        "top_users": [u for u, _ in by_user.most_common(5)],
        "normalized_entities": " ".join(entities),
    }
    hint = (
        f"[{hostname}] auth failures: {total} lần đăng nhập sai ({len(by_user)} user khác nhau)"
        if total else f"[{hostname}] auth failures: sạch, không lần nào"
    )

    return build_envelope(
        probe="security_auth_failures",
        lane="SIEM_SECURITY",
        domain=SECURITY,
        result=result,
        extracted_fact=fact,
        raw="",  # INV_DATA_RESIDENCY — không bao giờ mang dòng lastb thô lên Omni
        alert_rule="SecurityAuthFailureBurst" if result == "FAILED" else "SecurityAuthHealthy",
        alert_hint=hint,
        symptom_group="security_auth",
        namespace=hostname,
    )


_SUDO_USER_RE = re.compile(r"^\s*(\S+)\s*:")


def _parse_sudo_lines(out: str) -> list[str]:
    """journalctl _COMM=sudo — dòng "user : ..." hoặc "user : command not allowed ;
    ...". Trả list user đã chuẩn hoá."""
    users: list[str] = []
    for line in out.splitlines():
        # Bỏ phần "Aug 10 10:00:00 hostname sudo[123]: " (journalctl prefix) trước khi
        # tìm "<user> :" — chỉ quan tâm nội dung sau tên tiến trình.
        idx = line.find("sudo")
        tail = line[idx:] if idx >= 0 else line
        tail = tail.split(":", 1)[1] if ":" in tail else tail
        m = _SUDO_USER_RE.match(tail)
        if m:
            u = _safe_entity(m.group(1))
            if u:
                users.append(u)
    return users


async def collect_privilege_escalation(hostname: str) -> dict[str, Any] | None:
    """sudo/su anomalies via `journalctl _COMM=sudo` — privilege escalation signal."""
    out, err, rc = await _run([
        "journalctl", "_COMM=sudo", "--since", "-15min", "--no-pager", "-o", "short",
    ])
    if rc != 0:
        logger.warning("[collector.security] journalctl sudo unavailable: %s", err[:200])
        return None

    fail_lines = [l for l in out.splitlines() if "not allowed" in l.lower() or "incorrect password" in l.lower() or "authentication failure" in l.lower()]
    users = _parse_sudo_lines("\n".join(fail_lines))
    total = len(fail_lines)
    entities = sorted({f"user={u} process=sudo" for u in users})[:_MAX_ENTITIES]

    result = "FAILED" if total >= _PRIV_ESC_WARN else "PASSED"

    fact: dict[str, Any] = {
        "sudo_failure_count": total,
        "distinct_users": len(set(users)),
        "normalized_entities": " ".join(entities),
    }
    hint = (
        f"[{hostname}] privilege escalation: {total} lần sudo thất bại/bất thường trong 15 phút qua"
        if total else f"[{hostname}] privilege escalation: sạch, không lần nào"
    )

    return build_envelope(
        probe="security_privilege_escalation",
        lane="SIEM_SECURITY",
        domain=SECURITY,
        result=result,
        extracted_fact=fact,
        raw="",  # INV_DATA_RESIDENCY
        alert_rule="SecurityPrivilegeEscalation" if result == "FAILED" else "SecurityPrivEscHealthy",
        alert_hint=hint,
        symptom_group="security_priv_esc",
        namespace=hostname,
    )
