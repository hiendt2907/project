"""Controlled Recovery — vòng phục hồi có kiểm soát, bằng chứng, trách nhiệm.

Vì sao tồn tại (ranh giới quan trọng nhất của AOIP): không chỉ HIỂU vì sao hỏng mà
PHỤC HỒI có kiểm soát. Đây là phần khách trả tiền nhất, nhưng cũng nguy hiểm nhất —
nên mọi bước đều fail-closed, có gate, có audit.

Nguyên tắc kiến trúc cốt lõi (reviewer):
  - Recovery theo (FAILURE_MODE + SUBSTRATE), KHÔNG theo product name. Cùng một
    operator (process_down + systemd) phục hồi redis-server, mariadb, nginx — KHÔNG
    module riêng mỗi service.
  - Planner sinh ``Action`` (ontology đã có); KHÔNG tự chạy shell. Executor mới chạy,
    và CHỈ chạy Action đã APPROVED tường minh (INV_HUMAN_ACCOUNTABILITY).
  - Ngay trước khi execute: validate capability/authority/risk/scope/current-state.
  - Capture before-state + bằng chứng TRƯỚC mutation. Execute action nhỏ nhất, đảo
    được. Verify cả service lẫn dependents. KHÔNG retry vô hạn. Verify fail → dừng,
    giữ bằng chứng, escalate (rollback KHÔNG nhất thiết đối xứng — không giả vờ).
  - OMNI_AUTO_EXECUTE_ENABLED=false vẫn fail-closed: KHÔNG có path nào execute mà
    thiếu approval. Toàn bộ ghi audit hash-chain.

KHÔNG noun ontology mới: dùng Action/Decision/Finding. RecoveryOperator/RecoveryGate/
RecoveryRequest/Approval/RecoveryOutcome là Derived (policy/runtime value, không persist).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from aoip import audit
from aoip.objects import Action, ActionState
from aoip.verification import VerificationResult

# Substrate đã hỗ trợ (mở rộng = thêm operator, KHÔNG sửa executor).
SUBSTRATE_SYSTEMD = "systemd"


# ── Operator: cách phục hồi một (failure_mode, substrate) — KHÔNG biết service ───
@dataclass(frozen=True)
class RecoveryOperator:
    """Tập thao tác phục hồi cho một cơ chế hỏng trên một substrate.

    Mỗi callable nhận (transport, unit, port) và async. Cùng operator dùng cho mọi
    service chạy trên substrate đó — đó là điểm khiến redis/mariadb/nginx chung 1 path.
    """

    failure_mode: str
    substrate: str
    action_verb: str                      # smallest reversible action, vd "restart"
    is_broken: Callable[..., Awaitable[bool]]      # state hiện tại còn hỏng?
    capture_before: Callable[..., Awaitable[dict]]  # before-state (bằng chứng)
    apply: Callable[..., Awaitable[tuple[str, int]]]  # mutation nhỏ nhất
    health: Callable[..., Awaitable[bool]]          # service khỏe lại?


# ── systemd / process_down operator (cặp đầu tiên) ───────────────────────────
async def _sd_is_broken(t, unit, port) -> bool:
    out, _ = await t.run(["systemctl", "is-active", unit])
    return out.strip().lower() in ("inactive", "failed", "deactivating")


async def _sd_capture(t, unit, port) -> dict:
    state, _ = await t.run(["systemctl", "is-active", unit])
    since, _ = await t.run(["systemctl", "show", "-p", "ActiveEnterTimestamp", "--value", unit])
    return {"unit": unit, "active_state": state.strip(), "active_since": since.strip()}


async def _sd_apply(t, unit, port) -> tuple[str, int]:
    # Action nhỏ nhất khôi phục tiến trình: restart unit (reversible qua systemd state).
    return await t.run(["sudo", "systemctl", "restart", unit], timeout=30.0)


async def _sd_health(t, unit, port) -> bool:
    state, _ = await t.run(["systemctl", "is-active", unit])
    if state.strip().lower() != "active":
        return False
    if port is None:
        return True
    out, _ = await t.run(
        ["bash", "-c",
         f'timeout 1 bash -c "exec 3<>/dev/tcp/127.0.0.1/{port}" && echo OPEN'], timeout=5.0)
    return "OPEN" in out


_SYSTEMD_PROCESS_DOWN = RecoveryOperator(
    failure_mode="process_down", substrate=SUBSTRATE_SYSTEMD, action_verb="restart",
    is_broken=_sd_is_broken, capture_before=_sd_capture, apply=_sd_apply, health=_sd_health,
)


# ── systemd / failed_state_stale operator (capability #2: reset-failed) ──────
# Dọn một unit còn kẹt ActiveState=failed (vd hit StartLimitBurst) SAU KHI có
# bằng chứng khác cho thấy vấn đề gốc đã hết. CỐ Ý không dùng chung operator
# với process_down: apply() ở đây chỉ dọn failed bookkeeping
# (`systemctl reset-failed`) — KHÔNG bao giờ start/stop/restart unit, nên
# không có rủi ro downtime (khác _sd_apply — restart thật). is_broken/health
# đều dựa vào `is-failed` (không phải `is-active`) vì đó chính xác là state
# operator này tác động.
async def _sd_is_failed(t, unit, port) -> bool:
    out, _ = await t.run(["systemctl", "is-failed", unit])
    return out.strip().lower() == "failed"


async def _sd_reset_failed_capture(t, unit, port) -> dict:
    is_failed, _ = await t.run(["systemctl", "is-failed", unit])
    active_state, _ = await t.run(["systemctl", "is-active", unit])
    return {"unit": unit, "is_failed_state": is_failed.strip(), "active_state": active_state.strip()}


async def _sd_reset_failed_apply(t, unit, port) -> tuple[str, int]:
    # Action nhỏ nhất: chỉ dọn failed bookkeeping — KHÔNG start/stop/restart unit.
    return await t.run(["sudo", "systemctl", "reset-failed", unit], timeout=15.0)


async def _sd_reset_failed_health(t, unit, port) -> bool:
    out, _ = await t.run(["systemctl", "is-failed", unit])
    return out.strip().lower() != "failed"


_SYSTEMD_FAILED_STATE_STALE = RecoveryOperator(
    failure_mode="failed_state_stale", substrate=SUBSTRATE_SYSTEMD, action_verb="reset-failed",
    is_broken=_sd_is_failed, capture_before=_sd_reset_failed_capture,
    apply=_sd_reset_failed_apply, health=_sd_reset_failed_health,
)


# ── systemd / disk_pressure_journal operator (capability #3: journal_vacuum) ─
# Fixes SYS_RESOURCE lane's missing auto-remediation gap: journal disk usage
# growing unbounded fills the root/var partition. Target is CHÍNH unit
# "systemd-journald.service" (real on every systemd host) — journal vacuum is
# fundamentally an operation on journald's OWN retained data, so it fits the
# existing unit-scoped model (canonical_scope, lease, allowlist all assume a
# single unit target) without generalizing them. apply() ONLY calls the
# official `journalctl --vacuum-size=<target>` — NEVER a raw rm/find -delete
# against /var/log/journal — so the blast radius is bounded to what journald
# itself considers safe to rotate out.
#
# Threshold (disk-pressure trigger) and target (post-vacuum retained size) are
# BOTH env-configurable (see _journal_vacuum_threshold_bytes/_journal_vacuum_
# target_size below) — never hardcoded, since what counts as "too much
# journal" depends on the host's partition size, which this module cannot see.
_ENV_JOURNAL_VACUUM_THRESHOLD_BYTES = "AOIP_JOURNAL_VACUUM_THRESHOLD_BYTES"
# Default: 2 GiB of retained journal is already excessive on a typical lab VM
# (small root partitions, 10-20GB) and gives headroom to act BEFORE a Lane 1
# (SYS_RESOURCE, 3-sigma disk) alert would fire from overall filesystem usage.
# Journal disk-usage is orthogonal to overall filesystem %, so this is an
# absolute byte threshold, not a percentage.
_DEFAULT_JOURNAL_VACUUM_THRESHOLD_BYTES = 2 * 1024**3  # 2 GiB

_ENV_JOURNAL_VACUUM_TARGET_SIZE = "AOIP_JOURNAL_VACUUM_TARGET_SIZE"
# Default: retain 200M after vacuuming — well below the 2 GiB trigger, so one
# vacuum run buys meaningful headroom before the operator would fire again.
# Same string format `journalctl --vacuum-size=` itself accepts (K/M/G/T).
_DEFAULT_JOURNAL_VACUUM_TARGET_SIZE = "200M"

# Parses `journalctl --disk-usage` output, e.g. "Archived and active journals
# take up 4.0G in the file system." → 4.0 * 1024**3 bytes. Accepts an optional
# trailing "B" (some systemd versions/locales append it) and is unit-suffix
# case-insensitive; bare bytes (no K/M/G/T letter) parse as-is.
_DISK_USAGE_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([KMGTPE]?)B?", re.IGNORECASE)
_DISK_USAGE_MULTIPLIERS = {
    "": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5, "E": 1024**6,
}


def _journal_vacuum_threshold_bytes(env: dict | None = None) -> int:
    """Disk-pressure trigger threshold (bytes) — đọc từ env, KHÔNG hardcode.
    Thiếu/không parse được/không dương → default an toàn (xem hằng số phía trên)."""
    env = os.environ if env is None else env
    raw = (env.get(_ENV_JOURNAL_VACUUM_THRESHOLD_BYTES) or "").strip()
    if not raw:
        return _DEFAULT_JOURNAL_VACUUM_THRESHOLD_BYTES
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_JOURNAL_VACUUM_THRESHOLD_BYTES
    return value if value > 0 else _DEFAULT_JOURNAL_VACUUM_THRESHOLD_BYTES


def _journal_vacuum_target_size(env: dict | None = None) -> str:
    """Post-vacuum retained size passed to `journalctl --vacuum-size=` — đọc
    từ env, KHÔNG hardcode. Thiếu/rỗng → default an toàn (xem hằng số trên)."""
    env = os.environ if env is None else env
    raw = (env.get(_ENV_JOURNAL_VACUUM_TARGET_SIZE) or "").strip()
    return raw or _DEFAULT_JOURNAL_VACUUM_TARGET_SIZE


def _parse_disk_usage_bytes(text: str) -> int | None:
    """Parse `journalctl --disk-usage` human-readable output → bytes.

    Trả None nếu không parse được — caller PHẢI coi đây là "không đủ bằng
    chứng disk pressure" (fail-closed KHÔNG mutate), KHÔNG tự suy diễn."""
    m = _DISK_USAGE_SIZE_RE.search(text)
    if not m:
        return None
    value = float(m.group(1))
    unit = m.group(2).upper()
    return int(value * _DISK_USAGE_MULTIPLIERS.get(unit, 1))


async def _jv_is_broken(t, unit, port) -> bool:
    out, _ = await t.run(["journalctl", "--disk-usage"])
    used = _parse_disk_usage_bytes(out)
    if used is None:
        return False  # fail-closed: không đo được → KHÔNG coi là hỏng, KHÔNG mutate
    return used >= _journal_vacuum_threshold_bytes()


async def _jv_capture_before(t, unit, port) -> dict:
    out, _ = await t.run(["journalctl", "--disk-usage"])
    used = _parse_disk_usage_bytes(out)
    return {"unit": unit, "disk_usage_raw": out.strip(), "disk_usage_bytes": used,
            "threshold_bytes": _journal_vacuum_threshold_bytes()}


async def _jv_apply(t, unit, port) -> tuple[str, int]:
    # Action nhỏ nhất: CHỈ dùng journalctl chính thức để vacuum theo size —
    # KHÔNG bao giờ tự xoá file trong /var/log/journal (không rm/find -delete).
    target = _journal_vacuum_target_size()
    return await t.run(["sudo", "journalctl", f"--vacuum-size={target}"], timeout=60.0)


async def _jv_health(t, unit, port) -> bool:
    out, _ = await t.run(["journalctl", "--disk-usage"])
    used = _parse_disk_usage_bytes(out)
    if used is None:
        return False  # verification uncertainty must be explicit — never assume healthy
    return used < _journal_vacuum_threshold_bytes()


_SYSTEMD_JOURNAL_VACUUM = RecoveryOperator(
    failure_mode="disk_pressure_journal", substrate=SUBSTRATE_SYSTEMD, action_verb="vacuum",
    is_broken=_jv_is_broken, capture_before=_jv_capture_before,
    apply=_jv_apply, health=_jv_health,
)

# ── systemd / resource_runaway operator (capability #4: kill_unit) ───────────
# Phase 4 (docs/plans/omni-close-autonomous-sre-gaps-2026-07-23.md) — remote-host/
# VM action library expansion. Fixes the gap where a unit's process is pinned by
# evidence (Lane 1, 3-sigma memory/CPU) as a runaway consumer but is NOT itself
# `failed`/`inactive` (reset_failed/restart's current-state gates would both
# no-op: `is-active` says active, `is-failed` says not-failed). apply() sends
# ONLY `systemctl kill --signal=SIGTERM <unit>` (systemd's own supervised signal
# delivery, NEVER `kill -9 <pid>` against a raw PID) — the smallest reversible
# action for a stuck/runaway process: it relies on the unit's OWN configured
# Restart= policy to bring it back (same self-healing contract systemd already
# gives every managed service), so this capability never itself calls
# start/restart. Threshold is env-configurable (mirrors journal_vacuum's
# pattern) since "how much memory is runaway" depends on the host's RAM size,
# which this module cannot see.
_ENV_KILL_UNIT_MEMORY_THRESHOLD_BYTES = "AOIP_KILL_UNIT_MEMORY_THRESHOLD_BYTES"
# Default: 1 GiB resident for a single unit is already high pressure on a
# typical lab VM (small RAM, 1-2GB) — gives headroom to act before an OOM-kill
# (uncontrolled, not chosen by Omni) would happen instead.
_DEFAULT_KILL_UNIT_MEMORY_THRESHOLD_BYTES = 1 * 1024**3  # 1 GiB


def _kill_unit_memory_threshold_bytes(env: dict | None = None) -> int:
    """Memory threshold (bytes) đọc từ env, KHÔNG hardcode. Thiếu/không parse
    được/không dương → default an toàn (xem hằng số phía trên)."""
    env = os.environ if env is None else env
    raw = (env.get(_ENV_KILL_UNIT_MEMORY_THRESHOLD_BYTES) or "").strip()
    if not raw:
        return _DEFAULT_KILL_UNIT_MEMORY_THRESHOLD_BYTES
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_KILL_UNIT_MEMORY_THRESHOLD_BYTES
    return value if value > 0 else _DEFAULT_KILL_UNIT_MEMORY_THRESHOLD_BYTES


def _parse_memory_current_bytes(text: str) -> int | None:
    """Parse `systemctl show -p MemoryCurrent --value` output → bytes.

    `[not set]`/empty/non-numeric → None (fail-closed: caller PHẢI coi đây là
    "không đủ bằng chứng resource pressure", KHÔNG tự suy diễn/mutate)."""
    raw = text.strip()
    if not raw or not raw.isdigit():
        return None
    return int(raw)


async def _ku_is_broken(t, unit, port) -> bool:
    out, _ = await t.run(["systemctl", "show", "-p", "MemoryCurrent", "--value", unit])
    used = _parse_memory_current_bytes(out)
    if used is None:
        return False  # fail-closed: không đo được → KHÔNG coi là runaway, KHÔNG mutate
    return used >= _kill_unit_memory_threshold_bytes()


async def _ku_capture_before(t, unit, port) -> dict:
    mem_out, _ = await t.run(["systemctl", "show", "-p", "MemoryCurrent", "--value", unit])
    active_state, _ = await t.run(["systemctl", "is-active", unit])
    return {"unit": unit, "memory_current_raw": mem_out.strip(),
            "memory_current_bytes": _parse_memory_current_bytes(mem_out),
            "active_state": active_state.strip(),
            "threshold_bytes": _kill_unit_memory_threshold_bytes()}


async def _ku_apply(t, unit, port) -> tuple[str, int]:
    # Action nhỏ nhất: gửi SIGTERM qua đúng systemd (KHÔNG kill -9 PID trực
    # tiếp) — hồi phục phụ thuộc HOÀN TOÀN vào Restart= policy sẵn có của
    # unit, capability này KHÔNG tự start/restart.
    return await t.run(["sudo", "systemctl", "kill", "--signal=SIGTERM", unit], timeout=15.0)


async def _ku_health(t, unit, port) -> bool:
    out, _ = await t.run(["systemctl", "show", "-p", "MemoryCurrent", "--value", unit])
    used = _parse_memory_current_bytes(out)
    if used is None:
        return False  # verification uncertainty must be explicit — never assume healthy
    return used < _kill_unit_memory_threshold_bytes()


_SYSTEMD_KILL_UNIT = RecoveryOperator(
    failure_mode="resource_runaway", substrate=SUBSTRATE_SYSTEMD, action_verb="kill",
    is_broken=_ku_is_broken, capture_before=_ku_capture_before,
    apply=_ku_apply, health=_ku_health,
)


# ── systemd / disk_pressure_tmp operator (capability #5: disk_cleanup) ───────
# Second SYS_RESOURCE capability (after journal_vacuum), different target: root/
# tmp filesystem usage rather than journald's own retained data. TARGET UNIT
# CỐ ĐỊNH: chính unit thật `systemd-tmpfiles-clean.service` (oneshot có sẵn
# trên mọi host systemd hiện đại) — apply() CHỈ `systemctl start` unit đó, tức
# là chạy `systemd-tmpfiles --clean` CHÍNH THỨC theo rule đã cấu hình trong
# /etc/tmpfiles.d (age-based, disposable data only) — KHÔNG BAO GIỜ raw
# rm/find -delete tự viết. Cùng lý do với journal_vacuum: khớp tự nhiên với mô
# hình unit-scoped hiện có (canonical_scope, lease, allowlist), KHÔNG cần tổng
# quát hoá lại cho khái niệm "target không phải unit".
_ENV_DISK_CLEANUP_THRESHOLD_PCT = "AOIP_DISK_CLEANUP_THRESHOLD_PCT"
# Default: 85% root filesystem usage is already high pressure on a typical lab
# VM (small root partitions, 10-20GB) — headroom to act BEFORE a Lane 1
# (SYS_RESOURCE, 3-sigma disk) alert would fire from overall filesystem usage.
_DEFAULT_DISK_CLEANUP_THRESHOLD_PCT = 85.0

_ENV_DISK_CLEANUP_PATH = "AOIP_DISK_CLEANUP_PATH"
_DEFAULT_DISK_CLEANUP_PATH = "/"

_DISK_USAGE_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%?")


def _disk_cleanup_threshold_pct(env: dict | None = None) -> float:
    """Ngưỡng %-usage kích hoạt cleanup — đọc từ env, KHÔNG hardcode. Thiếu/
    không parse được/ngoài (0,100] → default an toàn (hằng số phía trên)."""
    env = os.environ if env is None else env
    raw = (env.get(_ENV_DISK_CLEANUP_THRESHOLD_PCT) or "").strip()
    if not raw:
        return _DEFAULT_DISK_CLEANUP_THRESHOLD_PCT
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_DISK_CLEANUP_THRESHOLD_PCT
    return value if 0.0 < value <= 100.0 else _DEFAULT_DISK_CLEANUP_THRESHOLD_PCT


def _disk_cleanup_path(env: dict | None = None) -> str:
    """Filesystem path bị đo %-usage — đọc từ env, KHÔNG hardcode. Rỗng →
    default an toàn (root filesystem)."""
    env = os.environ if env is None else env
    raw = (env.get(_ENV_DISK_CLEANUP_PATH) or "").strip()
    return raw or _DEFAULT_DISK_CLEANUP_PATH


def _parse_disk_usage_pct(text: str) -> float | None:
    """Parse `df --output=pcent <path>` output (header line + data line, vd
    "Use%\\n 87%") → float percent. Trả None nếu không parse được — caller
    PHẢI coi đây là "không đủ bằng chứng disk pressure" (fail-closed)."""
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    data_line = lines[-1] if lines else ""
    m = _DISK_USAGE_PCT_RE.search(data_line)
    if not m:
        return None
    return float(m.group(1))


async def _dc_is_broken(t, unit, port) -> bool:
    out, _ = await t.run(["df", "--output=pcent", _disk_cleanup_path()])
    pct = _parse_disk_usage_pct(out)
    if pct is None:
        return False  # fail-closed: không đo được → KHÔNG coi là hỏng, KHÔNG mutate
    return pct >= _disk_cleanup_threshold_pct()


async def _dc_capture_before(t, unit, port) -> dict:
    out, _ = await t.run(["df", "--output=pcent", _disk_cleanup_path()])
    return {"unit": unit, "path": _disk_cleanup_path(), "disk_usage_raw": out.strip(),
            "disk_usage_pct": _parse_disk_usage_pct(out),
            "threshold_pct": _disk_cleanup_threshold_pct()}


async def _dc_apply(t, unit, port) -> tuple[str, int]:
    # Action nhỏ nhất: khởi động unit oneshot CHÍNH THỨC — KHÔNG tự rm/find.
    return await t.run(["sudo", "systemctl", "start", unit], timeout=60.0)


async def _dc_health(t, unit, port) -> bool:
    out, _ = await t.run(["df", "--output=pcent", _disk_cleanup_path()])
    pct = _parse_disk_usage_pct(out)
    if pct is None:
        return False  # verification uncertainty must be explicit — never assume healthy
    return pct < _disk_cleanup_threshold_pct()


_SYSTEMD_DISK_CLEANUP_TMP = RecoveryOperator(
    failure_mode="disk_pressure_tmp", substrate=SUBSTRATE_SYSTEMD, action_verb="tmpfiles-clean",
    is_broken=_dc_is_broken, capture_before=_dc_capture_before,
    apply=_dc_apply, health=_dc_health,
)


# ── systemd / config_drifted operator (capability #6: config_rollback) ──────
# Restores a unit's config file from its last-known-good sibling backup
# (`<path>.aoip-backup`, fixed naming convention only — NEVER attacker/
# payload-controlled) and restarts the owning unit. The config path itself is
# resolved from `unit` via an env-configured mapping
# (`AOIP_CONFIG_ROLLBACK_PATHS="unit1:/etc/x.conf,unit2:/etc/y.conf"`, SAME
# env-driven-mapping pattern as the allowlist thresholds above) — NOT passed
# through the operator's generic `port: int | None` slot, which would silently
# repurpose a typed field for an unrelated string and is exactly the kind of
# signature-widening `INV_MINIMAL_PRIMITIVES` guards against. A unit missing
# from the mapping has NO known config path → fail-closed (`is_broken=False`,
# same as "no backup found").
#
# Reversibility proof (required by Phase 4 exit criteria before a capability
# may be added): BEFORE overwriting the live config, capture_before snapshots
# the CURRENT (about-to-be-replaced) file to `<path>.pre_rollback_snapshot` —
# if the rollback target itself turns out to be wrong, an operator can
# manually restore that snapshot. This is the one capability in this batch
# with real downtime risk (restarts a unit), so its risk is registered
# alongside `systemd.restart_unit`, not below it.
_ENV_CONFIG_ROLLBACK_PATHS = "AOIP_CONFIG_ROLLBACK_PATHS"


def config_rollback_path_for_unit(unit: str, env: dict | None = None) -> str | None:
    """Resolve `unit` → its allowlisted config path via
    ``AOIP_CONFIG_ROLLBACK_PATHS``. Returns None if the unit is not mapped
    (fail-closed — no path means no known-good backup to roll back to)."""
    env = os.environ if env is None else env
    raw = (env.get(_ENV_CONFIG_ROLLBACK_PATHS) or "").strip()
    mapping: dict[str, str] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or ":" not in entry:
            continue
        mapped_unit, _, path = entry.partition(":")
        mapped_unit, path = mapped_unit.strip(), path.strip()
        if mapped_unit and path:
            mapping[mapped_unit] = path
    return mapping.get(unit)


async def _cr_checksum(t, path) -> str | None:
    out, rc = await t.run(["sha256sum", path])
    if rc != 0 or not out.strip():
        return None
    return out.strip().split()[0]


async def _cr_is_broken(t, unit, port) -> bool:
    path = config_rollback_path_for_unit(unit)
    if path is None:
        return False  # fail-closed: unit không có config path đã map → không mutate
    current = await _cr_checksum(t, path)
    backup = await _cr_checksum(t, f"{path}.aoip-backup")
    if backup is None:
        return False  # fail-closed: no backup to roll back to → not "broken" here
    return current != backup


async def _cr_capture_before(t, unit, port) -> dict:
    path = config_rollback_path_for_unit(unit)
    if path is None:
        return {"unit": unit, "path": None, "current_sha256": None, "backup_sha256": None}
    current = await _cr_checksum(t, path)
    backup = await _cr_checksum(t, f"{path}.aoip-backup")
    # Reversibility snapshot — see operator docstring above.
    await t.run(["sudo", "cp", "-p", path, f"{path}.pre_rollback_snapshot"], timeout=15.0)
    return {"unit": unit, "path": path, "current_sha256": current, "backup_sha256": backup}


async def _cr_apply(t, unit, port) -> tuple[str, int]:
    path = config_rollback_path_for_unit(unit)
    if path is None:
        return "no config path mapped for unit", 1
    out, rc = await t.run(["sudo", "cp", "-p", f"{path}.aoip-backup", path], timeout=15.0)
    if rc != 0:
        return out, rc
    return await t.run(["sudo", "systemctl", "restart", unit], timeout=30.0)


async def _cr_health(t, unit, port) -> bool:
    path = config_rollback_path_for_unit(unit)
    if path is None:
        return False  # verification uncertainty must be explicit — never assume healthy
    current = await _cr_checksum(t, path)
    backup = await _cr_checksum(t, f"{path}.aoip-backup")
    if current is None or backup is None:
        return False
    if current != backup:
        return False
    active_state, _ = await t.run(["systemctl", "is-active", unit])
    return active_state.strip().lower() == "active"


_SYSTEMD_CONFIG_ROLLBACK = RecoveryOperator(
    failure_mode="config_drifted", substrate=SUBSTRATE_SYSTEMD, action_verb="config-rollback",
    is_broken=_cr_is_broken, capture_before=_cr_capture_before,
    apply=_cr_apply, health=_cr_health,
)

# Registry: (failure_mode, substrate) → operator. Thêm cặp mới = 1 entry, KHÔNG sửa loop.
OPERATORS: dict[tuple[str, str], RecoveryOperator] = {
    ("process_down", SUBSTRATE_SYSTEMD): _SYSTEMD_PROCESS_DOWN,
    ("failed_state_stale", SUBSTRATE_SYSTEMD): _SYSTEMD_FAILED_STATE_STALE,
    ("disk_pressure_journal", SUBSTRATE_SYSTEMD): _SYSTEMD_JOURNAL_VACUUM,
    ("resource_runaway", SUBSTRATE_SYSTEMD): _SYSTEMD_KILL_UNIT,
    ("disk_pressure_tmp", SUBSTRATE_SYSTEMD): _SYSTEMD_DISK_CLEANUP_TMP,
    ("config_drifted", SUBSTRATE_SYSTEMD): _SYSTEMD_CONFIG_ROLLBACK,
}


def operator_for(failure_mode: str, substrate: str) -> RecoveryOperator | None:
    return OPERATORS.get((failure_mode, substrate))


# ── Policy / runtime values (Derived) ────────────────────────────────────────
@dataclass(frozen=True)
class RecoveryGate:
    """Thẩm quyền + ngưỡng của agent (capability/authority/risk/scope/freshness)."""

    allowed_failure_modes: frozenset[str]
    allowed_substrates: frozenset[str]
    max_risk: float
    scope_prefix: str                  # node được phép tác động (vd "svc:")
    min_diagnosis_confidence: float
    max_diagnosis_age_s: float
    # Target-level allowlist (unit name for systemd today; the concept
    # generalizes to other substrates later). Fail-closed like
    # capabilities.systemd_restart.SystemdRestartPolicy.allowed_units: rỗng =
    # KHÔNG restart gì, KHÔNG phải wildcard-allow. ADR-005: trước bản vá này,
    # daemon production (operations.build_recovery_executor) chỉ có
    # scope_prefix thô (node namespace) — không allowlist theo unit cụ thể —
    # trong khi operator cấu hình AOIP_ALLOWED_SYSTEMD_UNITS tưởng nó có hiệu
    # lực toàn hệ thống.
    allowed_targets: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Approval:
    """Phê duyệt người, RÀNG BUỘC tenant + scope + Decision + Action + hạn (HITL).

    Living Operations Runtime: approval không được "chung chung". Nó chỉ hợp lệ cho
    ĐÚNG (tenant, action_scope, decision_goal) và HẾT HẠN sau expires_at — quá hạn /
    sai tenant / sai scope / sai decision → ZERO mutation. Mặc định backward-compatible
    cho call cũ (tenant/decision rỗng = bỏ qua ràng buộc đó, expires_at=∞)."""

    approved: bool
    approver: str
    action_scope: str
    tenant: str = ""
    decision_goal: str = ""
    expires_at: float = float("inf")
    action_id: str = ""            # ràng buộc đúng Action cụ thể (immutable identity)
    canonical_scope: str = ""      # ràng buộc đúng target đã canonical (tenant-embedded)
    issued_at: float = 0.0         # thời điểm phát hành (approval không được "từ tương lai")

    @classmethod
    def issue(cls, *, approver: str, tenant: str, canonical_scope: str, decision_goal: str,
              action_id: str, action_scope: str, issued_at: float, expires_at: float) -> "Approval":
        """Production path: fail-closed. Thiếu bất kỳ binding nào → raise (KHÔNG tạo được).

        Approval hợp lệ PHẢI ràng buộc: approver, tenant, canonical scope, Decision, Action,
        thời điểm phát hành, hạn. Không default rỗng, không hạn vô cực trên production.
        """
        missing = [n for n, v in (("approver", approver), ("tenant", tenant),
                                  ("canonical_scope", canonical_scope), ("decision_goal", decision_goal),
                                  ("action_id", action_id), ("action_scope", action_scope)) if not v]
        if missing:
            raise ValueError(f"bounded approval thiếu binding: {missing}")
        if not (issued_at < expires_at < float("inf")):
            raise ValueError("approval phải có issued_at < expires_at hữu hạn")
        return cls(approved=True, approver=approver, action_scope=action_scope, tenant=tenant,
                   decision_goal=decision_goal, expires_at=expires_at, action_id=action_id,
                   canonical_scope=canonical_scope, issued_at=issued_at)


@dataclass(frozen=True)
class RecoveryRequest:
    failed_node: str
    failure_mode: str
    substrate: str
    unit: str
    port: int | None
    action: Action
    risk: float
    diagnosed_at: float
    dependents: tuple[str, ...] = ()
    tenant: str = "default"
    mission_id: str = ""
    incident_id: str = ""
    decision_id: str = ""
    action_id: str = ""
    command_id: str = ""
    trace_id: str = ""


@dataclass(frozen=True)
class RecoveryOutcome:
    action: Action
    status: str           # "recovered" | "escalated" | "aborted"
    reason: str
    evidence: tuple[str, ...] = ()
    verification: VerificationResult = field(default_factory=lambda: VerificationResult.unknown(
        expected_state="verification.not_run", reason="recovery did not reach verification",
        evidence_refs=("verification:not_run",)))


def plan_recovery(
    *, failed_node: str, failure_mode: str, substrate: str, unit: str, port: int | None, risk: float,
) -> Action:
    """Sinh Action phục hồi (PLANNED) từ failure_mode+substrate — KHÔNG chạy gì.

    Action mang đủ ngữ cảnh để executor ràng buộc operator; smallest reversible verb.
    """
    op = operator_for(failure_mode, substrate)
    verb = op.action_verb if op else "escalate"
    return Action(
        decision_goal=f"recover:{failure_mode}",
        scope=f"recover_service:{failed_node}",
        plan=f"{verb} {unit} ({substrate}) để khôi phục {failure_mode} trên {failed_node}",
        state=ActionState.PLANNED,
        result={"failure_mode": failure_mode, "substrate": substrate, "unit": unit,
                "port": port, "verb": verb, "risk": risk},
    )


def _gate_checks(ctx, req: RecoveryRequest, gate: RecoveryGate, approval: Approval, now: float):
    """Trả list (name, ok, reason) — kiểm NGAY TRƯỚC execute. Bất kỳ fail → zero mutation."""
    # "DOWN" là cách diễn đạt lịch sử của failure_mode process_down; tổng quát
    # hoá theo req.failure_mode (thay vì chỉ "DOWN" cứng) để capability mới
    # (vd failed_state_stale) tự diễn đạt claim riêng mà vẫn qua được gate,
    # KHÔNG hardcode logic riêng cho từng loại failure_mode ở đây.
    incident_verified = any(
        f.verdict and ("DOWN" in f.claim or req.failure_mode in f.claim) for f in ctx.findings
    )
    diag = ctx.diagnosis_confidence
    positive_root = any(f.verdict and req.failure_mode in f.claim for f in ctx.findings) \
        or (diag is not None and diag >= gate.min_diagnosis_confidence)
    age = now - req.diagnosed_at
    return [
        ("incident_verified", incident_verified, "sự cố chưa được verify DOWN"),
        ("diagnosis_positive", (diag is not None and diag >= gate.min_diagnosis_confidence
                                and positive_root),
         f"diagnosis score {diag} < ngưỡng {gate.min_diagnosis_confidence} hoặc thiếu positive evidence"),
        ("diagnosis_fresh", age <= gate.max_diagnosis_age_s,
         f"diagnosis stale ({age:.0f}s > {gate.max_diagnosis_age_s:.0f}s)"),
        ("explicit_approval", approval.approved and approval.action_scope == req.action.scope,
         "thiếu approval tường minh cho đúng action scope"),
        ("approval_not_expired", now <= approval.expires_at,
         f"approval hết hạn (now {now:.0f} > expires_at {approval.expires_at:.0f})"),
        ("approval_tenant_bound", not approval.tenant or approval.tenant == req.tenant,
         f"approval sai tenant ({approval.tenant!r} ≠ {req.tenant!r})"),
        ("approval_decision_bound",
         not approval.decision_goal or approval.decision_goal == req.action.decision_goal,
         f"approval sai decision ({approval.decision_goal!r} ≠ {req.action.decision_goal!r})"),
        ("action_approved_state", req.action.state == ActionState.APPROVED,
         f"action state {req.action.state.value} ≠ approved"),
        ("capability_authorized",
         req.failure_mode in gate.allowed_failure_modes and req.substrate in gate.allowed_substrates,
         "failure_mode/substrate ngoài capability agent"),
        ("risk_within_gate", req.risk <= gate.max_risk,
         f"risk {req.risk} > max {gate.max_risk}"),
        ("scope_in_authority", req.failed_node.startswith(gate.scope_prefix),
         f"node {req.failed_node} ngoài scope {gate.scope_prefix!r}"),
        ("target_allowlisted", req.unit in gate.allowed_targets,
         f"unit {req.unit!r} không nằm trong allowed_targets (rỗng = KHÔNG restart gì)"),
    ]


async def execute_recovery(
    ctx, *, req: RecoveryRequest, transport, audit_log: audit.FileAuditLog,
    gate: RecoveryGate, approval: Approval, env_auto_execute: bool, now: float,
    probe_dependent: Callable[[str], Awaitable[bool]] | None = None,
    phase_hook: Callable[[str, dict], Awaitable[None]] | None = None,
) -> RecoveryOutcome:
    """Vòng phục hồi có kiểm soát. Trả RecoveryOutcome; ghi audit từng bước.

    Trình tự: gate → capture before → execute smallest action → verify service +
    dependents → complete | escalate. KHÔNG retry. Fail-closed tuyệt đối.
    """
    trace = req.trace_id or req.action.scope
    audit_context = {
        "tenant_id": req.tenant,
        "mission_id": req.mission_id,
        "incident_id": req.incident_id,
        "decision_id": req.decision_id,
        "action_id": req.action_id,
        "command_id": req.command_id,
    }
    op = operator_for(req.failure_mode, req.substrate)
    audit_log.append(audit.EV_RECOVERY_PLANNED, {
        **audit_context,
        "node": req.failed_node, "failure_mode": req.failure_mode, "substrate": req.substrate,
        "unit": req.unit, "verb": req.action.result.get("verb"), "risk": req.risk,
        "env_auto_execute": env_auto_execute,
    }, trace_id=trace)

    # ── GATE: mọi điều kiện phải đạt; bất kỳ fail → ZERO mutation ──────────────
    checks = _gate_checks(ctx, req, gate, approval, now)
    if op is None:
        checks.append(("operator_exists", False,
                       f"không có operator cho ({req.failure_mode},{req.substrate})"))
    blocked = [(n, r) for (n, ok, r) in checks if not ok]
    if blocked:
        reason = "; ".join(f"{n}: {r}" for n, r in blocked)
        audit_log.append(audit.EV_RECOVERY_GATE_BLOCKED,
                         {**audit_context, "node": req.failed_node, "blocked": [n for n, _ in blocked],
                          "reason": reason}, trace_id=trace)
        ctx.log("Recover", f"GATE chặn — KHÔNG mutation: {reason}")
        return RecoveryOutcome(action=req.action.at(ActionState.ABORTED, reason=reason),
                               status="aborted", reason=reason)

    # ── CURRENT-STATE GATE (ngay trước mutation): service còn hỏng thật không? ──
    if not await op.is_broken(transport, req.unit, req.port):
        reason = "service đang HEALTHY ngay trước execute — không tác động (zero mutation)"
        audit_log.append(audit.EV_RECOVERY_GATE_BLOCKED,
                         {**audit_context, "node": req.failed_node, "blocked": ["current_state_broken"],
                          "reason": reason}, trace_id=trace)
        ctx.log("Recover", reason)
        return RecoveryOutcome(action=req.action.at(ActionState.ABORTED, reason=reason),
                               status="aborted", reason=reason)

    # ── CAPTURE BEFORE-STATE (bằng chứng trước mutation) ──────────────────────
    before = await op.capture_before(transport, req.unit, req.port)
    audit_log.append(audit.EV_RECOVERY_BEFORE_STATE,
                     {**audit_context, "node": req.failed_node, "before": before}, trace_id=trace)

    # ── EXECUTE smallest reversible action (mutation thật) ────────────────────
    action = req.action.at(ActionState.EXECUTING)
    if phase_hook is not None:
        # This write is a fail-closed precondition: if it cannot be persisted,
        # do not dispatch the host mutation with an unknown execution phase.
        await phase_hook("mutation_started", {"before": before, "unit": req.unit})
    out, rc = await op.apply(transport, req.unit, req.port)
    audit_log.append(audit.EV_RECOVERY_EXECUTED,
                     {**audit_context, "node": req.failed_node, "verb": op.action_verb, "rc": rc,
                      "stdout": out[:200], "approver": approval.approver}, trace_id=trace)
    ctx.log("Recover", f"executed {op.action_verb} {req.unit} (rc={rc}) bởi {approval.approver}")

    # ── VERIFY: service khỏe lại + dependents hết ảnh hưởng ───────────────────
    if phase_hook is not None:
        await phase_hook("verifying", {"rc": rc, "unit": req.unit})
    try:
        service_ok = await op.health(transport, req.unit, req.port)
    except Exception as exc:  # noqa: BLE001 — verification uncertainty must be explicit
        evidence = (f"before={before.get('active_state')}",)
        verification = VerificationResult.unknown(
            expected_state="service.active_and_dependents.healthy",
            evidence_refs=evidence,
            reason=f"verification transport error: {type(exc).__name__}: {exc}",
        )
        reason = "verification UNKNOWN → escalate (transport error, no retry)"
        audit_log.append(audit.EV_RECOVERY_VERIFICATION_FAILED,
                         {**audit_context, "node": req.failed_node,
                          "evidence": list(evidence), "verification": verification.to_dict()},
                         trace_id=trace)
        audit_log.append(audit.EV_RECOVERY_ESCALATED,
                         {**audit_context, "node": req.failed_node, "reason": reason},
                         trace_id=trace)
        ctx.log("Recover", f"{reason}: {exc}")
        return RecoveryOutcome(
            action=action.at(ActionState.FAILED, verified=False), status="escalated",
            reason=reason, evidence=evidence, verification=verification,
        )
    dep_results: dict[str, bool] = {}
    if probe_dependent is not None:
        for dep in req.dependents:
            try:
                dep_results[dep] = await probe_dependent(dep)
            except Exception:
                dep_results[dep] = False
    dependents_ok = all(dep_results.values()) if dep_results else True
    evidence = (f"before={before.get('active_state')}",
                f"service_health={'ok' if service_ok else 'fail'}",
                f"dependents={dep_results or 'n/a'}")
    verification = (
        VerificationResult.pass_(
            expected_state="service.active_and_dependents.healthy",
            evidence_refs=evidence,
            checks={"service": service_ok, "dependents": dep_results or True},
            confidence=1.0,
        )
        if service_ok and dependents_ok else
        VerificationResult.fail(
            expected_state="service.active_and_dependents.healthy",
            evidence_refs=evidence,
            checks={"service": service_ok, "dependents": dep_results or True},
            reason="service or dependent verification failed",
            confidence=1.0,
        )
    )

    if service_ok and dependents_ok:
        final = action.at(ActionState.COMPLETED, verified=True, dependents=dep_results)
        audit_log.append(audit.EV_RECOVERY_COMPLETED,
                         {**audit_context, "node": req.failed_node, "evidence": list(evidence),
                          "verification": verification.to_dict()}, trace_id=trace)
        ctx.log("Recover", f"VERIFIED khỏe lại → COMPLETED ({', '.join(evidence)})")
        return RecoveryOutcome(action=final, status="recovered",
                               reason="service + dependents verified", evidence=evidence,
                               verification=verification)

    # ── VERIFY FAIL: dừng, KHÔNG retry, giữ bằng chứng, escalate ──────────────
    final = action.at(ActionState.FAILED, verified=False, dependents=dep_results)
    audit_log.append(audit.EV_RECOVERY_VERIFICATION_FAILED,
                     {**audit_context, "node": req.failed_node, "evidence": list(evidence),
                      "verification": verification.to_dict()}, trace_id=trace)
    audit_log.append(audit.EV_RECOVERY_ESCALATED,
                     {**audit_context, "node": req.failed_node,
                      "reason": "verification failed — no retry, human escalation"}, trace_id=trace)
    ctx.log("Recover", f"VERIFY FAIL → KHÔNG retry, ESCALATE ({', '.join(evidence)})")
    return RecoveryOutcome(action=final, status="escalated",
                           reason="verification failed → escalate (no infinite retry)",
                           evidence=evidence, verification=verification)
