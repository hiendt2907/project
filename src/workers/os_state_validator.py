"""Validate SYS_HARD_FAIL alert claims against OS/service probe state.

Mirror of alert_sdk_truth_compare.py — covers OS commands (systemd, df, NFS)
and service-layer probes (HAProxy, MySQL, ProxySQL, Nginx, etc.) instead of K8s SDK.

Returns a contrast string when probe result=PASSED contradicts an alert that
classified the batch as SYS_HARD_FAIL (alert is suspect / stale).
Returns None when probe confirms failure (real incident) or data is insufficient.

Handler registration: use @register_os_probe("probe_name", domain=...). Unknown probes
are logged at DEBUG level and skipped — they never fall through to LLM silently.

``domain`` là domain canonical của `pkg.domain.taxonomy`, KHÔNG phải lane trục A. Nhờ
nó, `compare_alert_claim_to_os_state(..., domain=...)` kiểm probe cùng lĩnh vực với sự
cố TRƯỚC — bằng chứng cùng lĩnh vực là bằng chứng mạnh hơn.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from pkg.domain import taxonomy

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_OS_PROBE_HANDLERS: dict[str, Callable[[dict[str, Any], dict[str, Any]], str | None]] = {}

# probe_name → domain canonical. Song song với `_OS_PROBE_HANDLERS`, không thay nó:
# handler trả lời "đọc bằng chứng này thế nào", còn map này trả lời "bằng chứng này
# thuộc lĩnh vực nào" — dùng để xếp thứ tự kiểm theo domain của sự cố thay vì theo
# thứ tự dict ngẫu nhiên của batch.
_OS_PROBE_DOMAINS: dict[str, str] = {}


def register_os_probe(*probe_names: str, domain: str = taxonomy.OS_HOST) -> Callable:
    """Đăng ký handler cho một/nhiều probe.

    ``domain`` mặc định `os_host` cho tương thích ngược, nhưng MỌI handler trong file
    này đều khai tường minh — mặc định chỉ tồn tại để một handler mới không làm vỡ
    import trước khi ai đó kịp phân loại nó.
    """
    d = taxonomy.require_domain(domain)

    def decorator(fn: Callable) -> Callable:
        for name in probe_names:
            _OS_PROBE_HANDLERS[name] = fn
            _OS_PROBE_DOMAINS[name] = d
        return fn
    return decorator


def os_probes_for_domain(domain: str) -> tuple[str, ...]:
    """Tên probe OS-layer thuộc ``domain`` (sort — thứ tự phải xác định)."""
    d = taxonomy.normalize_domain(domain)
    return tuple(sorted(p for p, pd in _OS_PROBE_DOMAINS.items() if pd == d))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_ef(raw: Any, probe: str = "") -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    s = str(raw).strip()
    if s.startswith("{"):
        try:
            o = json.loads(s)
            return o if isinstance(o, dict) else {}
        except Exception as exc:
            logger.debug("_parse_ef malformed json probe=%s err=%r", probe, exc)
    return {}


def _probe_result(ev: dict[str, Any]) -> str:
    return str(ev.get("result") or "").upper().strip()


def _sanitize_probe_ev(ev: dict[str, Any]) -> dict[str, Any]:
    """Return a sanitized immutable copy of a probe evidence dict."""
    out = dict(ev)
    probe_output = str(out.get("probe_output") or "")
    if len(probe_output) > 2000:
        out["probe_output"] = probe_output[:2000]
    return out


def _alert_ctx_summary(alert_ctx: dict[str, Any]) -> str:
    an = alert_ctx.get("alertname", "")
    sev = (alert_ctx.get("labels") or {}).get("severity", "")
    ns = alert_ctx.get("namespace", "")
    summary = (alert_ctx.get("annotations") or {}).get("summary", "")[:120]
    src = alert_ctx.get("source", "")
    parts = [x for x in (an, sev and f"sev={sev}", ns and f"ns={ns}", summary, src) if x]
    return " | ".join(parts) or "(no alert context)"


# ---------------------------------------------------------------------------
# D0 — SystemD / OS Services
# ---------------------------------------------------------------------------

@register_os_probe("systemd_units", domain=taxonomy.SERVICE)
def _check_systemd_units(ev: dict[str, Any], alert_ctx: dict[str, Any]) -> str | None:
    if _probe_result(ev) != "PASSED":
        return None
    ef = _parse_ef(ev.get("extracted_fact"), "systemd_units")
    if ef.get("critical_failed_units") or ef.get("failed_units"):
        return None
    ctx_summary = _alert_ctx_summary(alert_ctx)
    return (
        "OS probe systemd_units (systemctl list-units) reports all critical services healthy "
        "(result=PASSED, critical_failed_units=[], failed_units=[]). "
        "Alert claiming service failure is suspect — likely stale Prometheus series, "
        "mis-scoped alert rule, or transient restart that already recovered. "
        f"Alert: {ctx_summary}"
    )


# ---------------------------------------------------------------------------
# D1 — Process Health
# ---------------------------------------------------------------------------

@register_os_probe("cron_jobs", domain=taxonomy.SERVICE)
def _check_cron_jobs(ev: dict[str, Any], alert_ctx: dict[str, Any]) -> str | None:
    if _probe_result(ev) != "PASSED":
        return None
    ef = _parse_ef(ev.get("extracted_fact"), "cron_jobs")
    if ef.get("failed_cron_count") is None:
        return None  # null count = unknown, not healthy
    if ef.get("failed_cron_count", 0) or ef.get("failed_jobs"):
        return None
    ctx_summary = _alert_ctx_summary(alert_ctx)
    return (
        "OS probe cron_jobs reports all scheduled jobs completed successfully "
        "(result=PASSED, failed_cron_count=0). "
        "Alert claiming cron/job failure is suspect — verify with `journalctl -u cron` "
        "and check if the alert rule targets a stale job history entry. "
        f"Alert: {ctx_summary}"
    )


@register_os_probe("zombie_processes", domain=taxonomy.OS_HOST)
def _check_zombie_processes(ev: dict[str, Any], alert_ctx: dict[str, Any]) -> str | None:
    if _probe_result(ev) != "PASSED":
        return None
    ef = _parse_ef(ev.get("extracted_fact"), "zombie_processes")
    zombie_count = ef.get("zombie_count", 0)
    if zombie_count is None:
        return None  # null count = unknown, not healthy
    if zombie_count and int(zombie_count) > 0:
        return None
    ctx_summary = _alert_ctx_summary(alert_ctx)
    return (
        "OS probe zombie_processes reports no zombie processes "
        f"(result=PASSED, zombie_count=0). "
        "Alert claiming zombie accumulation is suspect — process table may have been "
        "cleaned between scrape intervals. "
        f"Alert: {ctx_summary}"
    )


@register_os_probe("oom_events", domain=taxonomy.OS_HOST)
def _check_oom_events(ev: dict[str, Any], alert_ctx: dict[str, Any]) -> str | None:
    if _probe_result(ev) != "PASSED":
        return None
    ef = _parse_ef(ev.get("extracted_fact"), "oom_events")
    if ef.get("oom_count") is None:
        return None  # null count = unknown, not healthy
    if ef.get("oom_count", 0) or ef.get("recent_oom_victims"):
        return None
    ctx_summary = _alert_ctx_summary(alert_ctx)
    return (
        "OS probe oom_events reports no OOM kills in recent window "
        "(result=PASSED, oom_count=0). "
        "Alert claiming OOM pressure is suspect — check `dmesg | grep -i oom` directly; "
        "alert may be firing on a transient memory spike that self-resolved. "
        f"Alert: {ctx_summary}"
    )


# ---------------------------------------------------------------------------
# D2 — Storage
# ---------------------------------------------------------------------------

@register_os_probe("disk_usage", domain=taxonomy.STORAGE)
def _check_disk(ev: dict[str, Any], alert_ctx: dict[str, Any]) -> str | None:
    if _probe_result(ev) != "PASSED":
        return None
    ef = _parse_ef(ev.get("extracted_fact"), "disk_usage")
    if ef.get("disk_critical_count") is None:
        return None  # null count = unknown, not healthy
    if ef.get("disk_critical_count", 0) or ef.get("inode_critical") or ef.get("critical_partitions"):
        return None
    ctx_summary = _alert_ctx_summary(alert_ctx)
    return (
        "OS probe disk_usage (df -h / df -i) reports all partitions within healthy range "
        "(result=PASSED, disk_critical_count=0, inode_critical=[]). "
        "Alert claiming disk-full or inode-exhausted is suspect — possible stale Prometheus "
        "disk-usage metric from a decommissioned mount or ephemeral spike already resolved. "
        f"Alert: {ctx_summary}"
    )


@register_os_probe("storage_nfs", domain=taxonomy.STORAGE)
def _check_nfs(ev: dict[str, Any], alert_ctx: dict[str, Any]) -> str | None:
    if _probe_result(ev) != "PASSED":
        return None
    ef = _parse_ef(ev.get("extracted_fact"), "storage_nfs")
    if ef.get("nfs_error_count") is None:
        return None  # null = unknown, not healthy
    if ef.get("nfs_error_count", 0) > 0:
        return None
    ctx_summary = _alert_ctx_summary(alert_ctx)
    return (
        "OS probe storage_nfs reports NFS mounts responsive "
        "(result=PASSED, nfs_error_count=0). "
        "Alert claiming NFS unavailable is suspect — verify with `mount | grep nfs` and "
        "`df -h` on the client; NFS may have auto-recovered or the alert rule targets "
        "a decommissioned endpoint. "
        f"Alert: {ctx_summary}"
    )


@register_os_probe("raid_mdadm", domain=taxonomy.STORAGE)
def _check_raid_mdadm(ev: dict[str, Any], alert_ctx: dict[str, Any]) -> str | None:
    if _probe_result(ev) != "PASSED":
        return None
    ef = _parse_ef(ev.get("extracted_fact"), "raid_mdadm")
    if ef.get("failed_devices") is None:
        return None  # null = unknown, not healthy
    if ef.get("degraded_arrays") or ef.get("failed_devices", 0):
        return None
    ctx_summary = _alert_ctx_summary(alert_ctx)
    return (
        "OS probe raid_mdadm reports all RAID arrays clean "
        "(result=PASSED, degraded_arrays=[], failed_devices=0). "
        "Alert claiming RAID degradation is suspect — verify with `cat /proc/mdstat` directly. "
        f"Alert: {ctx_summary}"
    )


@register_os_probe("lvm_volumes", domain=taxonomy.STORAGE)
def _check_lvm_volumes(ev: dict[str, Any], alert_ctx: dict[str, Any]) -> str | None:
    if _probe_result(ev) != "PASSED":
        return None
    ef = _parse_ef(ev.get("extracted_fact"), "lvm_volumes")
    if ef.get("partial_vgs") is None or ef.get("failed_pvs") is None:
        return None  # null = unknown, not healthy
    if ef.get("partial_vgs") or ef.get("failed_pvs"):
        return None
    ctx_summary = _alert_ctx_summary(alert_ctx)
    return (
        "OS probe lvm_volumes reports LVM physical volumes and volume groups healthy "
        "(result=PASSED, partial_vgs=[], failed_pvs=[]). "
        "Alert claiming LVM failure is suspect — run `pvs` / `vgs` / `lvs` to confirm. "
        f"Alert: {ctx_summary}"
    )


@register_os_probe("swap_usage", domain=taxonomy.OS_HOST)
def _check_swap_usage(ev: dict[str, Any], alert_ctx: dict[str, Any]) -> str | None:
    if _probe_result(ev) != "PASSED":
        return None
    ef = _parse_ef(ev.get("extracted_fact"), "swap_usage")
    swap_pct = ef.get("swap_used_pct", 0)
    if swap_pct is None:
        return None  # missing data = unknown state, not healthy
    if swap_pct and float(swap_pct) > 80:
        return None
    ctx_summary = _alert_ctx_summary(alert_ctx)
    return (
        f"OS probe swap_usage reports swap within healthy range "
        f"(result=PASSED, swap_used_pct={swap_pct}%). "
        "Alert claiming swap exhaustion is suspect — check `free -m` and vmstat for "
        "actual current state. "
        f"Alert: {ctx_summary}"
    )


# ---------------------------------------------------------------------------
# D3 — Network
# ---------------------------------------------------------------------------

@register_os_probe("network_interfaces", domain=taxonomy.NETWORK)
def _check_network_interfaces(ev: dict[str, Any], alert_ctx: dict[str, Any]) -> str | None:
    if _probe_result(ev) != "PASSED":
        return None
    ef = _parse_ef(ev.get("extracted_fact"), "network_interfaces")
    if ef.get("down_interfaces") is None or ef.get("error_interfaces") is None:
        return None  # null = unknown, not healthy
    if ef.get("down_interfaces") or ef.get("error_interfaces"):
        return None
    ctx_summary = _alert_ctx_summary(alert_ctx)
    return (
        "OS probe network_interfaces reports all interfaces UP and no error counts "
        "(result=PASSED, down_interfaces=[], error_interfaces=[]). "
        "Alert claiming network interface failure is suspect — verify with `ip link` and "
        "`ethtool <iface>`; alert may target a decommissioned or renamed interface. "
        f"Alert: {ctx_summary}"
    )


@register_os_probe("dns_resolution", domain=taxonomy.NETWORK)
def _check_dns_resolution(ev: dict[str, Any], alert_ctx: dict[str, Any]) -> str | None:
    if _probe_result(ev) != "PASSED":
        return None
    ef = _parse_ef(ev.get("extracted_fact"), "dns_resolution")
    if ef.get("failed_lookups") is None or ef.get("lookup_error_count") is None:
        return None  # null = unknown, not healthy
    if ef.get("failed_lookups") or ef.get("lookup_error_count", 0):
        return None
    ctx_summary = _alert_ctx_summary(alert_ctx)
    return (
        "OS probe dns_resolution reports all DNS lookups resolving correctly "
        "(result=PASSED, failed_lookups=[], lookup_error_count=0). "
        "Alert claiming DNS failure is suspect — run `dig +short <domain>` from the host; "
        "resolver may have recovered. "
        f"Alert: {ctx_summary}"
    )


@register_os_probe("tcp_connections", domain=taxonomy.NETWORK)
def _check_tcp_connections(ev: dict[str, Any], alert_ctx: dict[str, Any]) -> str | None:
    if _probe_result(ev) != "PASSED":
        return None
    ef = _parse_ef(ev.get("extracted_fact"), "tcp_connections")
    if ef.get("time_wait_excess") is None or ef.get("syn_flood_indicator") is None:
        return None  # null = unknown, not healthy
    if ef.get("time_wait_excess") or ef.get("syn_flood_indicator"):
        return None
    ctx_summary = _alert_ctx_summary(alert_ctx)
    return (
        "OS probe tcp_connections reports TCP connection table healthy "
        "(result=PASSED, no TIME_WAIT excess or SYN-flood indicators). "
        "Alert claiming connection saturation is suspect — verify with `ss -s` and "
        "`netstat -an | awk '{print $6}' | sort | uniq -c`. "
        f"Alert: {ctx_summary}"
    )


# ---------------------------------------------------------------------------
# D4 — Database
# ---------------------------------------------------------------------------

@register_os_probe("mysql_health", domain=taxonomy.DATABASE)
def _check_mysql(ev: dict[str, Any], alert_ctx: dict[str, Any]) -> str | None:
    if _probe_result(ev) != "PASSED":
        return None
    ef = _parse_ef(ev.get("extracted_fact"), "mysql_health")
    if ef.get("anomalies") is None:
        return None  # null = unknown, not healthy
    if ef.get("anomalies"):
        return None
    ctx_summary = _alert_ctx_summary(alert_ctx)
    return (
        "OS probe mysql_health reports MySQL healthy "
        "(result=PASSED, no anomalies: replication running, connections within limit, slow-queries normal). "
        "Alert claiming MySQL down/critical is suspect — "
        "check alert rule expression vs current SHOW STATUS; possible scrape lag or "
        "alert label mismatch against this instance. "
        f"Alert: {ctx_summary}"
    )


@register_os_probe("proxysql_health", domain=taxonomy.DATABASE)
def _check_proxysql(ev: dict[str, Any], alert_ctx: dict[str, Any]) -> str | None:
    if _probe_result(ev) != "PASSED":
        return None
    ef = _parse_ef(ev.get("extracted_fact"), "proxysql_health")
    if ef.get("anomalies") is None:
        return None  # null = unknown, not healthy
    if ef.get("anomalies"):
        return None
    ctx_summary = _alert_ctx_summary(alert_ctx)
    return (
        "OS probe proxysql_health reports ProxySQL healthy "
        "(result=PASSED, clients within limit, active_transactions normal). "
        "Alert claiming ProxySQL overload is suspect — "
        "cross-check `SELECT * FROM stats_mysql_global` on ProxySQL admin; "
        "alert may be firing on a stale connection-count metric. "
        f"Alert: {ctx_summary}"
    )


@register_os_probe("postgresql_health", domain=taxonomy.DATABASE)
def _check_postgresql(ev: dict[str, Any], alert_ctx: dict[str, Any]) -> str | None:
    if _probe_result(ev) != "PASSED":
        return None
    ef = _parse_ef(ev.get("extracted_fact"), "postgresql_health")
    if ef.get("anomalies") is None or ef.get("replication_lag_s") is None:
        return None  # null = unknown, not healthy
    if ef.get("anomalies") or ef.get("replication_lag_s", 0) > 30:
        return None
    ctx_summary = _alert_ctx_summary(alert_ctx)
    return (
        "OS probe postgresql_health reports PostgreSQL healthy "
        "(result=PASSED, no anomalies, replication lag within threshold). "
        "Alert claiming PostgreSQL failure is suspect — "
        "verify with `SELECT * FROM pg_stat_replication` and `pg_stat_activity`. "
        f"Alert: {ctx_summary}"
    )


@register_os_probe("redis_os_health", domain=taxonomy.DATABASE)
def _check_redis_os(ev: dict[str, Any], alert_ctx: dict[str, Any]) -> str | None:
    if _probe_result(ev) != "PASSED":
        return None
    ef = _parse_ef(ev.get("extracted_fact"), "redis_os_health")
    if ef.get("anomalies"):
        return None
    if ef.get("rdb_last_bgsave_status") == "err":
        return None  # broken RDB persistence regardless of AOF state
    ctx_summary = _alert_ctx_summary(alert_ctx)
    return (
        "OS probe redis_os_health reports Redis instance healthy "
        "(result=PASSED, no anomalies detected via redis-cli INFO). "
        "Alert claiming Redis failure is suspect — "
        "verify with `redis-cli ping` and `redis-cli INFO replication`. "
        f"Alert: {ctx_summary}"
    )


@register_os_probe("mongodb_health", domain=taxonomy.DATABASE)
def _check_mongodb(ev: dict[str, Any], alert_ctx: dict[str, Any]) -> str | None:
    if _probe_result(ev) != "PASSED":
        return None
    ef = _parse_ef(ev.get("extracted_fact"), "mongodb_health")
    if ef.get("anomalies") is None or ef.get("repl_lag_s") is None:
        return None  # null = unknown, not healthy
    if ef.get("anomalies") or ef.get("repl_lag_s", 0) > 30:
        return None
    ctx_summary = _alert_ctx_summary(alert_ctx)
    return (
        "OS probe mongodb_health reports MongoDB healthy "
        "(result=PASSED, no anomalies, replica set lag within threshold). "
        "Alert claiming MongoDB failure is suspect — "
        "verify with `db.adminCommand('replSetGetStatus')`. "
        f"Alert: {ctx_summary}"
    )


# ---------------------------------------------------------------------------
# D5 — Proxy / Load Balancer
# ---------------------------------------------------------------------------

@register_os_probe("service_haproxy", "service_haproxy_prom", domain=taxonomy.SERVICE)
def _check_haproxy(ev: dict[str, Any], alert_ctx: dict[str, Any]) -> str | None:
    probe_key = ev.get("probe", "service_haproxy")
    if _probe_result(ev) != "PASSED":
        return None
    ef = _parse_ef(ev.get("extracted_fact"), probe_key)
    if ef.get("down_backends") is None:
        return None  # null = unknown, not healthy
    if ef.get("down_backends"):
        return None
    ctx_summary = _alert_ctx_summary(alert_ctx)
    return (
        f"OS probe {probe_key} reports HAProxy backend state healthy "
        "(result=PASSED, down_backends=[]). "
        "Alert claiming backend-down is suspect — verify HAProxy stats page directly "
        "and cross-check Prometheus `haproxy_backend_status` time series for scrape lag. "
        f"Alert: {ctx_summary}"
    )


@register_os_probe("service_nginx", domain=taxonomy.SERVICE)
def _check_nginx(ev: dict[str, Any], alert_ctx: dict[str, Any]) -> str | None:
    if _probe_result(ev) != "PASSED":
        return None
    ef = _parse_ef(ev.get("extracted_fact"), "service_nginx")
    if ef.get("error_rate_pct") is None or ef.get("upstream_errors") is None:
        return None  # null = unknown, not healthy
    if ef.get("error_rate_pct", 0) > 5 or ef.get("upstream_errors"):
        return None
    ctx_summary = _alert_ctx_summary(alert_ctx)
    return (
        "OS probe service_nginx reports Nginx healthy "
        "(result=PASSED, error_rate within threshold, no upstream errors). "
        "Alert claiming Nginx failure is suspect — "
        "verify with `nginx -t` and check access/error logs. "
        f"Alert: {ctx_summary}"
    )


@register_os_probe("service_keepalived", domain=taxonomy.SERVICE)
def _check_keepalived(ev: dict[str, Any], alert_ctx: dict[str, Any]) -> str | None:
    if _probe_result(ev) != "PASSED":
        return None
    ef = _parse_ef(ev.get("extracted_fact"), "service_keepalived")
    if ef.get("vip_missing") is None:
        return None  # null = unknown, not healthy
    if ef.get("vip_missing") or ef.get("state") == "FAULT":
        return None
    ctx_summary = _alert_ctx_summary(alert_ctx)
    return (
        "OS probe service_keepalived reports VRRP/keepalived healthy "
        "(result=PASSED, VIP assigned, state=MASTER or BACKUP). "
        "Alert claiming VIP failover is suspect — "
        "verify with `ip addr show` and `journalctl -u keepalived`. "
        f"Alert: {ctx_summary}"
    )


# ---------------------------------------------------------------------------
# D6 — Hardware
# ---------------------------------------------------------------------------

@register_os_probe("kernel_errors", domain=taxonomy.OS_HOST)
def _check_kernel_errors(ev: dict[str, Any], alert_ctx: dict[str, Any]) -> str | None:
    if _probe_result(ev) != "PASSED":
        return None
    ef = _parse_ef(ev.get("extracted_fact"), "kernel_errors")
    if ef.get("critical_errors") is None or ef.get("mce_count") is None:
        return None  # null = unknown, not healthy
    if ef.get("critical_errors") or ef.get("mce_count", 0):
        return None
    ctx_summary = _alert_ctx_summary(alert_ctx)
    return (
        "OS probe kernel_errors reports no critical kernel errors in dmesg "
        "(result=PASSED, critical_errors=[], mce_count=0). "
        "Alert claiming hardware/kernel error is suspect — "
        "verify with `dmesg -T | grep -i error` and `mcelog`. "
        f"Alert: {ctx_summary}"
    )


@register_os_probe("memory_hw_errors", domain=taxonomy.HARDWARE)
def _check_memory_hw_errors(ev: dict[str, Any], alert_ctx: dict[str, Any]) -> str | None:
    if _probe_result(ev) != "PASSED":
        return None
    ef = _parse_ef(ev.get("extracted_fact"), "memory_hw_errors")
    if ef.get("correctable_errors") is None or ef.get("uncorrectable_errors") is None:
        return None  # null = unknown, not healthy
    if ef.get("correctable_errors", 0) > 0 or ef.get("uncorrectable_errors", 0) > 0:
        return None
    ctx_summary = _alert_ctx_summary(alert_ctx)
    return (
        "OS probe memory_hw_errors reports no ECC memory errors "
        "(result=PASSED, correctable_errors=0, uncorrectable_errors=0). "
        "Alert claiming memory hardware failure is suspect — "
        "verify with `edac-util -s` or `ipmitool sel list`. "
        f"Alert: {ctx_summary}"
    )


# ---------------------------------------------------------------------------
# D7 — Container Runtime
# ---------------------------------------------------------------------------

@register_os_probe("docker_daemon", domain=taxonomy.KUBERNETES)
def _check_docker_daemon(ev: dict[str, Any], alert_ctx: dict[str, Any]) -> str | None:
    if _probe_result(ev) != "PASSED":
        return None
    ef = _parse_ef(ev.get("extracted_fact"), "docker_daemon")
    if ef.get("unhealthy_containers") is None:
        return None  # null container count = unknown, not healthy
    if ef.get("daemon_error") or ef.get("unhealthy_containers", 0):
        return None
    ctx_summary = _alert_ctx_summary(alert_ctx)
    return (
        "OS probe docker_daemon reports Docker daemon healthy "
        "(result=PASSED, daemon responsive, no unhealthy containers). "
        "Alert claiming container runtime failure is suspect — "
        "verify with `docker info` and `docker ps --filter health=unhealthy`. "
        f"Alert: {ctx_summary}"
    )


@register_os_probe("containerd_state", domain=taxonomy.KUBERNETES)
def _check_containerd(ev: dict[str, Any], alert_ctx: dict[str, Any]) -> str | None:
    if _probe_result(ev) != "PASSED":
        return None
    ef = _parse_ef(ev.get("extracted_fact"), "containerd_state")
    if ef.get("plugin_errors") is None:
        return None  # null plugin_errors = unknown, not healthy
    if ef.get("daemon_error") or ef.get("plugin_errors"):
        return None
    ctx_summary = _alert_ctx_summary(alert_ctx)
    return (
        "OS probe containerd_state reports containerd runtime healthy "
        "(result=PASSED, daemon responsive, no plugin errors). "
        "Alert claiming containerd failure is suspect — "
        "verify with `ctr version` and `systemctl status containerd`. "
        f"Alert: {ctx_summary}"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _probe_order(
    evidence_by_probe: dict[str, dict[str, Any]],
    domain: str,
) -> list[str]:
    """Thứ tự kiểm: probe CÙNG domain với sự cố trước, phần còn lại giữ nguyên thứ tự.

    Không LỌC BỎ probe khác domain: một sự cố `database` vẫn có thể được giải thích
    bởi `disk_usage`, và bỏ qua bằng chứng đó là tự bịt mắt. Chỉ đổi thứ tự — hàm này
    trả contrast ĐẦU TIÊN tìm được, nên thứ tự chính là mức ưu tiên.
    """
    names = [p for p in evidence_by_probe if p]
    d = taxonomy.normalize_domain(domain)
    if d == taxonomy.UNKNOWN:
        return names
    same = [p for p in names if _OS_PROBE_DOMAINS.get(p) == d]
    return same + [p for p in names if p not in set(same)]


def compare_alert_claim_to_os_state(
    evidence_by_probe: dict[str, dict[str, Any]],
    alert_ctx: dict[str, Any] | None = None,
    *,
    domain: str | None = None,
    lane: str | None = None,
) -> str | None:
    """Run OS-layer probe checks against the alert claim.

    ``domain`` (canonical) hoặc ``lane`` trục A — chỉ dùng để XẾP THỨ TỰ kiểm, không
    lọc. Cả hai để trống ⇒ hành vi y như trước.

    Returns a contrast string when at least one OS probe reports PASSED (healthy)
    while the batch was classified SYS_HARD_FAIL — alert is suspect.

    Returns None when any checked probe confirms failure or no relevant probes
    are present.
    """
    alert_ctx = alert_ctx or {}

    # Check if any registered probe in the batch confirmed FAILED.
    # When true, unregistered probes cannot be trusted to generate contrast —
    # a real failure is present and the alert is not suspect.
    registered_confirmed_failure = any(
        _probe_result(ev) != "PASSED"
        for pn, ev in evidence_by_probe.items()
        if pn and pn in _OS_PROBE_HANDLERS
    )

    resolved_domain = taxonomy.normalize_domain(domain)
    if resolved_domain == taxonomy.UNKNOWN:
        resolved_domain = taxonomy.lane_to_domain(lane)

    for probe_name in _probe_order(evidence_by_probe, resolved_domain):
        ev = evidence_by_probe[probe_name]
        handler = _OS_PROBE_HANDLERS.get(probe_name)
        if handler is None:
            logger.info("os_state_validator: unregistered probe=%s (add handler)", probe_name)
            if registered_confirmed_failure:
                # Registered probe confirmed real failure — skip unregistered probe contrast.
                continue
            if _probe_result(ev) == "PASSED":
                ef = _parse_ef(ev.get("extracted_fact"), probe_name)
                if not ef:
                    # Empty or non-parseable extracted_fact = unknown state, not healthy.
                    logger.debug("os_state_validator: unregistered probe=%s has empty fact — skipping", probe_name)
                    continue
                anomaly_words = ("error", "fail", "critical", "down", "missing", "issue")
                anomaly_values = [
                    ef[k] for k in ef
                    if any(w in k.lower() for w in anomaly_words) and ef[k]
                ]
                if not anomaly_values:
                    return (
                        f"OS probe {probe_name} reports PASSED, no anomaly indicators. "
                        f"{_alert_ctx_summary(alert_ctx)}"
                    )
            continue
        if registered_confirmed_failure and _probe_result(ev) == "PASSED":
            # Another registered probe already confirmed a real fault in this batch.
            # Emitting a 'healthy' contrast from this PASSED probe would be misleading.
            logger.debug("os_state_validator: registered failure present — skipping contrast probe=%s", probe_name)
            continue
        sanitized = _sanitize_probe_ev(ev)
        try:
            result = handler(sanitized, alert_ctx)
        except Exception as exc:
            logger.warning("os_state_validator: handler=%s raised err=%r", probe_name, exc)
            result = None
        if result is not None:
            logger.info("event=os_state_contrast_found probe=%s", probe_name)
            return result
    return None
