"""Domain-aware signal detection for remote agent evidence.

Từ vựng domain là của ``pkg.domain.taxonomy`` (canonical). Module này CHỈ phân loại;
nó không được sở hữu tên domain. Trước 2026-07-30 nó tự khai 8 tên riêng
(``os_system``/``services``/``container_logs``) không cầu nối với hai từ vựng khác,
nên không thể trả lời "Omni làm được gì trên domain X" mà không đọc cả ba file.

ĐỔI GIÁ TRỊ, KHÔNG ĐỔI HÀNH VI: mỗi hằng ``DOMAIN_*`` giữ nguyên vai trò trong cascade
phân loại, chỉ trả về tên canonical. Ánh xạ 1-1 nên không có hai nhánh nào bị trộn:
os_system→os_host, services→service, container_logs→kubernetes, còn lại giữ nguyên tên.

Each domain defines keyword sets per severity tier (critical/high/medium/baseline).
All keyword matching is case-insensitive against alert_hint + raw content.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pkg.domain import taxonomy

# ---------------------------------------------------------------------------
# Domain taxonomy
# ---------------------------------------------------------------------------

# Alias sang canonical — KHÔNG khai chuỗi mới ở đây. `DOMAIN_CONTAINER` trỏ
# `kubernetes` vì log container/pod là bằng chứng tầng K8s; `DOMAIN_SERVICES` trỏ
# `service` (số ít) — cùng khái niệm, chỉ khác cách viết cũ.
DOMAIN_OS = taxonomy.OS_HOST
DOMAIN_NETWORK = taxonomy.NETWORK
DOMAIN_STORAGE = taxonomy.STORAGE
DOMAIN_SERVICES = taxonomy.SERVICE
DOMAIN_CONTAINER = taxonomy.KUBERNETES
DOMAIN_DATABASE = taxonomy.DATABASE
DOMAIN_APPLICATION = taxonomy.APPLICATION
DOMAIN_SECURITY = taxonomy.SECURITY
DOMAIN_UNKNOWN = taxonomy.UNKNOWN

# 8 domain module này phân loại được — KHÁC `taxonomy.CANONICAL_DOMAINS` (9, có
# `hardware`): không có probe nào của agent phát tín hiệu hardware, nên khai ở đây là
# khai một năng lực không tồn tại.
ALL_DOMAINS = (
    DOMAIN_OS, DOMAIN_NETWORK, DOMAIN_STORAGE, DOMAIN_SERVICES,
    DOMAIN_CONTAINER, DOMAIN_DATABASE, DOMAIN_APPLICATION, DOMAIN_SECURITY,
)

if len(set(ALL_DOMAINS)) != len(ALL_DOMAINS):  # pragma: no cover — invariant
    raise RuntimeError("DOMAIN_* alias bi trung nhau: hai nhanh phan loai se bi tron")
if set(ALL_DOMAINS) - set(taxonomy.CANONICAL_DOMAINS):  # pragma: no cover — invariant
    raise RuntimeError("DOMAIN_* khong phai canonical: them alias vao pkg.domain.taxonomy")

# ---------------------------------------------------------------------------
# Probe-prefix → domain mapping (checked before content-based fallback)
# ---------------------------------------------------------------------------

_PROBE_DOMAIN_PREFIXES: list[tuple[tuple[str, ...], str]] = [
    (("remote_system_metrics", "os_proc_", "kernel_", "syslog_", "proc_"), DOMAIN_OS),
    (("network_", "dns_", "connectivity_", "ping_", "port_", "traceroute_"), DOMAIN_NETWORK),
    (("disk_", "storage_", "mount_", "fs_", "io_", "raid_", "volume_"), DOMAIN_STORAGE),
    (("systemd_", "service_", "daemon_", "process_", "unit_"), DOMAIN_SERVICES),
    (("container_log_", "pod_log_", "docker_log_", "remote_log_errors",
      "remote_log_", "app_log_"), DOMAIN_CONTAINER),
    (("db_", "mysql_", "mysql", "proxysql_", "proxysql", "postgres_", "redis_db_",
      "mongo_", "elasticsearch_", "mariadb_", "pgsql_"), DOMAIN_DATABASE),
    (("app_", "http_", "api_", "web_", "grpc_", "rpc_", "log_errors"), DOMAIN_APPLICATION),
    (("security_", "auth_", "siem_", "audit_", "firewall_", "iptables_",
      "selinux_", "fail2ban_"), DOMAIN_SECURITY),
]

# ---------------------------------------------------------------------------
# Signal keyword tables — (keywords, severity)
# Order matters: checked top-to-bottom, first match wins per domain.
# ---------------------------------------------------------------------------

_OS_SIGNALS: list[tuple[list[str], str]] = [
    (["oom kill", "oom-kill", "oom_kill", "out of memory: kill", "kernel panic",
      "segfault", "segmentation fault", "general protection fault",
      "swap exhausted", "swap full", "cpu throttl", "cgroup limit",
      "cpuset throttl", "millicore throttl"], "critical"),
    (["memory pressure", "kswapd", "memory reclaim", "thrashing",
      "high load average", "cpu steal", "no space left on device",
      "disk full", "inode exhausted", "read-only filesystem",
      "i/o error on device", "bad sector", "smart failure"], "high"),
    (["zombie process", "defunct", "cannot reap", "high iowait",
      "load average > 10", "memory usage > 85", "swap > 80",
      "disk > 90"], "medium"),
    (["cpu_pct", "mem_pct", "disk_pct", "load_avg", "uptime",
      "swap_pct", "io_wait_pct", "mem_used_mb", "cpu_used_cores",
      "memory_usage_bytes", "disk_read_bytes", "disk_write_bytes"], "baseline"),
]

_NETWORK_SIGNALS: list[tuple[list[str], str]] = [
    (["network unreachable", "no route to host", "all interfaces down",
      "dns resolution failed", "nxdomain", "servfail", "dns timeout",
      "dns lookup fail", "dns server fail"], "critical"),
    (["packet loss", "tcp reset", "connection reset by peer",
      "certificate expired", "ssl handshake fail", "tls verify fail",
      "tls handshake", "connection refused", "port unreachable",
      "host unreachable", "network timeout"], "high"),
    (["latency spike", "latency > 200ms", "high rtt", "jitter",
      "intermittent packet loss", "dns slow", "dns > 500ms"], "medium"),
    (["ping ok", "ping latency", "dns resolve time", "port open",
      "tcp connect ok", "connectivity check passed",
      "latency_ms", "rtt_ms", "packet_loss_pct"], "baseline"),
]

_STORAGE_SIGNALS: list[tuple[list[str], str]] = [
    (["no space left", "disk full", "filesystem full", "disk_pct > 95",
      "filesystem corrupt", "fsck error", "dirty bit", "journal abort",
      "raid degraded", "mdadm degraded", "disk failed", "read-only filesystem",
      "i/o error", "smart failure"], "critical"),
    (["disk > 90", "disk_pct > 90", "inode > 90", "high io wait",
      "io wait > 50", "iowait", "disk slow", "await ms > 200",
      "write latency high", "read latency high"], "high"),
    (["disk > 80", "disk_pct > 80", "inode > 80", "io wait > 20",
      "disk usage growing"], "medium"),
    (["disk_used_pct", "inode_used_pct", "io_wait_pct", "read_throughput",
      "write_throughput", "disk_read_iops", "disk_write_iops"], "baseline"),
]

_SERVICES_SIGNALS: list[tuple[list[str], str]] = [
    (["result: oom-kill", "killed by sigkill",
      "start request repeated too quickly", "start-limit-hit",
      "start-limit exceeded"], "critical"),
    (["service failed", "failed (result:", "failed (result: exit-code)",
      "failed (result: signal)", "core dump", "watchdog timeout",
      "service inactive", "stopped unexpectedly",
      "activating (auto-restart)"], "high"),
    (["service restarted", "restart count > 3", "backing off",
      "high restart count", "service degraded", "service slow to start"], "medium"),
    (["active (running)", "service enabled", "started successfully",
      "loaded active running", "service_state", "restart_count",
      "active_time_seconds"], "baseline"),
]

_CONTAINER_SIGNALS: list[tuple[list[str], str]] = [
    (["panic:", "fatal error:", "fatal:", "sigsegv", "heap corruption",
      "stack overflow", "double free", "out of memory", "oom killer",
      "killed process", "exit status 137", "exit code 137"], "critical"),
    (["exception:", "traceback (most recent", "unhandled exception",
      "java.lang.exception", "java.lang.error", "error: unhandled",
      "connection refused", "connection reset", "failed to connect",
      "dial tcp", "cannot connect", "tls: no supported", "refused stream"], "high"),
    (["warning:", "warn:", "deprecated", "retry attempt",
      "backoff", "error:", "failed:", "unable to"], "medium"),
    (["listening on", "started successfully", "ready", "initialized",
      "server started", "application started", "healthy",
      "container_start_time", "restart_count"], "baseline"),
]

# Database: split by sub-engine then generic
_DB_MYSQL_CRITICAL = ["replication stopped", "slave sql thread stopped",
                       "gtid error", "max_connections", "too many connections",
                       "host is blocked", "table corrupt", "tablespace error",
                       "innodb: unable to lock"]
_DB_MYSQL_HIGH = ["deadlock found", "lock wait timeout exceeded",
                   "slow query", "query took > 1"]
_DB_POSTGRES_CRITICAL = ["checkpoint distance exceeded",
                           "autovacuum worker failed", "too many clients",
                           "remaining connection slots reserved",
                           "database system was shut down"]
_DB_POSTGRES_HIGH = ["replication lag > 300", "standby too far behind",
                      "autovacuum: found orphan", "checkpoint warning"]
_DB_REDIS_CRITICAL = ["oom command not allowed", "maxmemory-policy",
                       "eviction rate", "master link down", "replication error",
                       "rdb: error saving"]
_DB_REDIS_HIGH = ["rdb save failed", "background saving terminated",
                   "connection to master lost", "aof rewrite failed"]
_DB_MONGO_CRITICAL = ["oplog window too short", "replica set state changed",
                       "too many open files", "election timeout"]
_DB_GENERIC = ["deadlock", "connection pool exhausted", "replication lag",
                "max connections reached", "table scan", "index missing",
                "query timeout", "database down", "db connection failed"]

_DATABASE_SIGNALS: list[tuple[list[str], str]] = [
    (_DB_MYSQL_CRITICAL + _DB_POSTGRES_CRITICAL + _DB_REDIS_CRITICAL
     + _DB_MONGO_CRITICAL + _DB_GENERIC, "critical"),
    (_DB_MYSQL_HIGH + _DB_POSTGRES_HIGH + _DB_REDIS_HIGH + _DB_MONGO_HIGH
     if False else _DB_MYSQL_HIGH + _DB_POSTGRES_HIGH + _DB_REDIS_HIGH
     + ["slow query", "high connections", "cache miss > 50",
        "replication lag > 60"], "high"),
    (["slow query", "high connections", "cache miss", "query > 500ms",
      "connection > 80%", "query plan degraded"], "medium"),
    (["queries_per_sec", "connections_current", "cache_hit_rate",
      "replication_lag_s", "slow_queries_total", "db_connections",
      "select_rate", "insert_rate", "update_rate"], "baseline"),
]

_APPLICATION_SIGNALS: list[tuple[list[str], str]] = [
    (["5xx rate", "circuit breaker open",
      "outofmemoryerror", "java.lang.outofmemory", "gc overhead limit",
      "heap space", "metaspace oom", "thread pool exhausted",
      "goroutine leak detected"], "critical"),
    (["internal server error", "service unavailable", "bad gateway",
      "http 500", "http 502", "http 503", "5xx error",
      "error_rate > 10", "exception_rate > 5", "p99 > 1s",
      "memory leak", "connection pool timeout", "socket timeout"], "high"),
    (["http 4xx spike", "timeout", "retry storm", "rate limited",
      "backoff", "latency degraded", "p95 > 500ms",
      "error_rate > 1"], "medium"),
    (["http_2xx_rate", "request_rate", "latency_p50", "latency_p99",
      "error_rate", "active_connections", "requests_per_sec",
      "gc_pause_ms", "heap_used_mb"], "baseline"),
]

_SECURITY_SIGNALS: list[tuple[list[str], str]] = [
    (["privilege escalation", "unauthorized root access", "rootkit",
      "malware detected", "data exfiltration", "unusual outbound traffic",
      "brute force", "too many auth fail", "ssh: invalid user",
      "authentication failure burst", "ransomware"], "critical"),
    (["multiple authentication failures", "failed login", "login attempts",
      "suspicious process", "unexpected binary", "file integrity violation",
      "port scan detected", "nmap scan", "unusual network connection",
      "unknown process", "setuid abuse"], "high"),
    (["auth failure", "permission denied", "unusual user activity",
      "connection from unknown ip", "failed sudo", "invalid certificate"], "medium"),
    (["authentication success", "login from known ip", "firewall allow",
      "auth_success_count", "auth_fail_count", "login_count"], "baseline"),
]

_DOMAIN_SIGNAL_MAP: dict[str, list[tuple[list[str], str]]] = {
    DOMAIN_OS: _OS_SIGNALS,
    DOMAIN_NETWORK: _NETWORK_SIGNALS,
    DOMAIN_STORAGE: _STORAGE_SIGNALS,
    DOMAIN_SERVICES: _SERVICES_SIGNALS,
    DOMAIN_CONTAINER: _CONTAINER_SIGNALS,
    DOMAIN_DATABASE: _DATABASE_SIGNALS,
    DOMAIN_APPLICATION: _APPLICATION_SIGNALS,
    DOMAIN_SECURITY: _SECURITY_SIGNALS,
}

# Content keywords for domain fallback detection
_CONTENT_DOMAIN_KEYWORDS: list[tuple[list[str], str]] = [
    (["mysql", "postgres", "postgresql", "redis", "mongodb", "mongo",
      "elasticsearch", "deadlock", "replication lag", "max_connections"], DOMAIN_DATABASE),
    (["connection refused", "no route to host", "dns", "packet loss",
      "network unreachable", "ssl handshake", "certificate"], DOMAIN_NETWORK),
    (["privilege escalation", "auth fail", "brute force", "unauthorized",
      "malware", "port scan", "firewall", "ssh: invalid"], DOMAIN_SECURITY),
    (["oom kill", "kernel panic", "cpu throttl", "zombie",
      "segfault", "cgroup", "swap exhausted"], DOMAIN_OS),
    (["disk full", "no space left", "i/o error", "filesystem corrupt",
      "raid degraded", "inode exhausted"], DOMAIN_STORAGE),
    (["service failed", "systemd", "watchdog timeout", "start-limit",
      "unit failed", "daemon", "activating (auto-restart)"], DOMAIN_SERVICES),
    (["panic:", "traceback", "exception:", "stack overflow",
      "container", "docker", "goroutine"], DOMAIN_CONTAINER),
    (["http 5", "5xx", "circuit breaker", "request timeout",
      "heap space", "outofmemory", "gc pause"], DOMAIN_APPLICATION),
]

# Hàng chót của cascade, theo lane TRỤC A — DEPRECATED, sẽ rỗng khi mọi collector
# khai `domain`.
#
# ⚠️ Cố ý KHÁC `taxonomy.lane_to_domain`: ở đây `SYS_HARD_FAIL` → `os_host`, còn
# `lane_to_domain` trả `unknown`. Không phải bất nhất mà là hai mục đích khác nhau:
# đây là bước cuối của một cascade PHÂN LOẠI (đã thử probe/label/content, đoán
# `os_host` cho một hard-fail là hợp lý để có nhãn hiển thị), còn `lane_to_domain` là
# đường dữ liệu LỊCH SỬ mà kết quả có thể đi cấp quyền — ở đó thừa nhận `unknown`
# an toàn hơn đoán. Đừng "hợp nhất cho gọn".
_LANE_DEFAULT: dict[str, str] = {
    "SIEM_SECURITY": DOMAIN_SECURITY,
    "APP_HTTP": DOMAIN_APPLICATION,
    "SYS_HARD_FAIL": DOMAIN_OS,
    "SYS_RESOURCE": DOMAIN_OS,
}

# alertname substring patterns → domain (case-insensitive).
# Checked after probe-prefix, before content keywords.
_ALERTNAME_LABEL_RULES: list[tuple[list[str], str]] = [
    (["KubePod", "KubeDeployment", "KubeNode", "KubeDaemon", "ImagePull", "CrashLoop", "OOMKill"], DOMAIN_CONTAINER),
    (["KubeMemory", "MemoryPressure", "oom_kill", "OutOfMemory"], DOMAIN_OS),
    (["KubeCPU", "Throttl", "CPULimit", "HighLoad"], DOMAIN_OS),
    (["KubeNetwork", "NetworkPolicy", "DNSError", "dns_fail", "dns_timeout"], DOMAIN_NETWORK),
    (["Disk", "Storage", "PersistentVolume", "inode", "FilesystemFull"], DOMAIN_STORAGE),
    (["MySQL", "Postgres", "Redis", "Mongo", "Database", "DeadLock", "ReplicationLag"], DOMAIN_DATABASE),
    (["HTTPError", "ServiceUnavailable", "CircuitBreaker", "HighErrorRate", "5xx", "http_error"], DOMAIN_APPLICATION),
    (["Auth", "Firewall", "siem_", "BruteForce", "UnauthorizedAccess", "AnomalyAuth"], DOMAIN_SECURITY),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_domain(
    probe: str,
    alert_hint: str,
    raw: str,
    lane: str,
    labels: dict[str, str] | None = None,
    *,
    domain_hint: str | None = None,
) -> str:
    """Return the most likely domain for this evidence item.

    Cascade:
      0. ``domain_hint`` — domain do NGUỒN tự khai (collector/gateway). Thắng mọi
         suy đoán: collector biết nó đang đo cái gì, cascade dưới đây chỉ đoán.
      1. Probe prefix  — highest signal, zero-ambiguity
      2. Alertname/label rules — Prometheus alertname substring matching
      3. Content keywords — free-text fallback
      4. Lane default  — last resort; returns DOMAIN_UNKNOWN when lane unknown

    ``lane`` là lane TRỤC A (`envelope.lane`) và chỉ còn là hàng chót — không phải
    `proof_lane` (trục B) hay lane semaphore (trục C). Xem `pkg.domain.taxonomy`.
    """
    if domain_hint is not None:
        declared = taxonomy.normalize_domain(domain_hint)
        if declared != DOMAIN_UNKNOWN:
            return declared

    probe_lower = probe.lower().strip()

    for prefixes, domain in _PROBE_DOMAIN_PREFIXES:
        for p in prefixes:
            if probe_lower == p.rstrip("_") or probe_lower.startswith(p):
                return domain

    alertname = ((labels or {}).get("alertname") or alert_hint or "").strip()
    if alertname:
        alertname_lower = alertname.lower()
        for patterns, domain in _ALERTNAME_LABEL_RULES:
            if any(p.lower() in alertname_lower for p in patterns):
                return domain

    text = (alert_hint + " " + raw).lower()
    for keywords, domain in _CONTENT_DOMAIN_KEYWORDS:
        if any(kw in text for kw in keywords):
            return domain

    return _LANE_DEFAULT.get(lane, DOMAIN_UNKNOWN)


def assess_domain_severity(
    domain: str,
    alert_hint: str,
    raw: str,
    extracted_fact: dict[str, Any] | None,
) -> str:
    """
    Return severity tier: "critical" | "high" | "medium" | "baseline" | "none".

    Priority:
      1. extracted_fact.result == "FAILED" → domain-specific severity floor
      2. Domain signal keyword tables (alert_hint + raw content)
      3. Numeric thresholds from extracted_fact
      4. Baseline key presence fallback
    """
    fact = extracted_fact or {}

    # ── Priority 1: explicit FAILED result (structured, not string-matched) ──
    if fact.get("result") == "FAILED":
        if domain in (DOMAIN_DATABASE,):
            return "critical"
        if domain in (DOMAIN_SERVICES, DOMAIN_OS, DOMAIN_STORAGE, DOMAIN_NETWORK):
            return "high"
        return "high"

    # ── Priority 2: keyword matching on text content ──────────────────────
    signals = _DOMAIN_SIGNAL_MAP.get(domain, [])
    text = (alert_hint + " " + raw).lower()

    for keywords, severity in signals:
        if any(kw.lower() in text for kw in keywords):
            return severity

    # ── Priority 3: numeric thresholds ────────────────────────────────────
    if fact:
        sev = _check_numeric_thresholds(domain, fact)
        if sev:
            return sev

        # Has domain-recognized metric keys but values are in normal range → baseline
        if _has_baseline_keys(domain, fact):
            return "baseline"

    return "none"


# Alias từ vựng metric: producer thật (`remote_agent/collectors/system.py`) phát
# `cpu_percent`/`mem_percent`/`disk_percent`, còn bảng ngưỡng dưới đây viết `cpu_pct`/…
# Hai bộ song song ⇒ lưới an toàn severity đọc `cpu_pct` LUÔN None, mọi nhánh là code
# chết (đã trả giá 2026-07-31). `_num` nay thử mọi bí danh của cùng một đại lượng.
_METRIC_ALIASES: dict[str, tuple[str, ...]] = {
    "cpu_pct": ("cpu_pct", "cpu_percent"),
    "mem_pct": ("mem_pct", "mem_percent"),
    "disk_pct": ("disk_pct", "disk_percent"),
    "disk_used_pct": ("disk_used_pct", "disk_percent"),
}


def _check_numeric_thresholds(domain: str, fact: dict[str, Any]) -> str | None:
    """Check extracted_fact numeric values against known thresholds."""
    def _num(key: str) -> float | None:
        for k in _METRIC_ALIASES.get(key, (key,)):
            v = fact.get(k)
            if v is None:
                continue
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
        return None

    if domain == DOMAIN_OS:
        cpu = _num("cpu_pct")
        mem = _num("mem_pct")
        oom = _num("oom_count")
        if oom is not None and oom > 0:
            return "critical"
        if cpu is not None and cpu > 95:
            return "critical"
        if mem is not None and mem > 95:
            return "critical"
        if cpu is not None and cpu > 85:
            return "high"
        if mem is not None and mem > 85:
            return "high"
        if cpu is not None and cpu > 70:
            return "medium"

    elif domain == DOMAIN_STORAGE:
        disk = _num("disk_pct") or _num("disk_used_pct")
        inode = _num("inode_used_pct")
        if disk is not None and disk > 95:
            return "critical"
        if inode is not None and inode > 95:
            return "critical"
        if disk is not None and disk > 90:
            return "high"
        if disk is not None and disk > 80:
            return "medium"

    elif domain == DOMAIN_DATABASE:
        error_count = _num("error_count") or _num("deadlock_count")
        repl_lag = _num("replication_lag_s") or _num("replica_lag_seconds")
        if error_count is not None and error_count > 0:
            return "high"
        if repl_lag is not None and repl_lag > 300:
            return "critical"
        if repl_lag is not None and repl_lag > 60:
            return "high"

    elif domain == DOMAIN_APPLICATION:
        error_rate = _num("error_rate") or _num("error_rate_pct")
        p99 = _num("latency_p99_ms") or _num("p99_ms")
        if error_rate is not None and error_rate > 50:
            return "critical"
        if error_rate is not None and error_rate > 10:
            return "high"
        if p99 is not None and p99 > 5000:
            return "critical"
        if p99 is not None and p99 > 1000:
            return "high"

    return None


# Known baseline metric key sets per domain — presence alone signals this is a metrics probe
_BASELINE_KEYS: dict[str, set[str]] = {
    DOMAIN_OS: {"cpu_pct", "cpu_percent", "mem_pct", "mem_percent", "disk_pct",
                "disk_percent", "load_avg", "load_avg_1m", "uptime",
                "swap_pct", "io_wait_pct", "mem_used_mb", "cpu_used_cores",
                "memory_usage_bytes", "disk_read_bytes", "disk_write_bytes"},
    DOMAIN_STORAGE: {"disk_used_pct", "disk_percent", "inode_used_pct", "io_wait_pct",
                     "read_throughput", "write_throughput", "disk_read_iops",
                     "disk_write_iops"},
    DOMAIN_NETWORK: {"latency_ms", "rtt_ms", "packet_loss_pct", "dns_resolve_ms",
                     "bandwidth_mbps"},
    DOMAIN_DATABASE: {"queries_per_sec", "connections_current", "cache_hit_rate",
                      "replication_lag_s", "slow_queries_total", "db_connections",
                      "select_rate", "insert_rate", "update_rate"},
    DOMAIN_APPLICATION: {"http_2xx_rate", "request_rate", "latency_p50", "latency_p99",
                         "error_rate", "active_connections", "requests_per_sec",
                         "gc_pause_ms", "heap_used_mb"},
    DOMAIN_SECURITY: {"auth_success_count", "auth_fail_count", "login_count",
                      "firewall_allow_count"},
    DOMAIN_SERVICES: {"service_state", "restart_count", "active_time_seconds"},
    DOMAIN_CONTAINER: {"container_start_time", "restart_count", "cpu_usage_cores",
                       "mem_usage_mb"},
}


def _has_baseline_keys(domain: str, fact: dict[str, Any]) -> bool:
    """Return True if extracted_fact contains any recognized baseline metric keys for domain."""
    known = _BASELINE_KEYS.get(domain, set())
    return bool(known.intersection(fact.keys()))
