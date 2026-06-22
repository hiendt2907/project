"""Tests for domain_signals.py — domain detection and severity assessment."""

from __future__ import annotations

import pytest

from pkg.reasoning.domain_signals import (
    DOMAIN_APPLICATION,
    DOMAIN_CONTAINER,
    DOMAIN_DATABASE,
    DOMAIN_NETWORK,
    DOMAIN_OS,
    DOMAIN_SECURITY,
    DOMAIN_SERVICES,
    DOMAIN_STORAGE,
    DOMAIN_UNKNOWN,
    assess_domain_severity,
    detect_domain,
)


# ── detect_domain: probe-based ─────────────────────────────────────────────

class TestDetectDomainByProbe:
    def test_remote_system_metrics(self):
        assert detect_domain("remote_system_metrics", "", "", "SYS_RESOURCE") == DOMAIN_OS

    def test_remote_log_errors(self):
        assert detect_domain("remote_log_errors", "", "", "SYS_HARD_FAIL") == DOMAIN_CONTAINER

    def test_mysql_probe(self):
        assert detect_domain("mysql_status", "", "", "SYS_HARD_FAIL") == DOMAIN_DATABASE

    def test_postgres_probe(self):
        assert detect_domain("postgres_connections", "", "", "SYS_RESOURCE") == DOMAIN_DATABASE

    def test_redis_db_probe(self):
        assert detect_domain("redis_db_info", "", "", "SYS_RESOURCE") == DOMAIN_DATABASE

    def test_dns_probe(self):
        assert detect_domain("dns_check", "", "", "SYS_HARD_FAIL") == DOMAIN_NETWORK

    def test_network_probe(self):
        assert detect_domain("network_latency", "", "", "SYS_RESOURCE") == DOMAIN_NETWORK

    def test_disk_probe(self):
        assert detect_domain("disk_usage", "", "", "SYS_RESOURCE") == DOMAIN_STORAGE

    def test_systemd_probe(self):
        assert detect_domain("systemd_unit_check", "", "", "SYS_HARD_FAIL") == DOMAIN_SERVICES

    def test_container_log_probe(self):
        assert detect_domain("container_log_nginx", "", "", "APP_HTTP") == DOMAIN_CONTAINER

    def test_security_probe(self):
        assert detect_domain("auth_failures", "", "", "SIEM_SECURITY") == DOMAIN_SECURITY

    def test_http_probe(self):
        assert detect_domain("http_status_check", "", "", "APP_HTTP") == DOMAIN_APPLICATION


# ── detect_domain: content-based fallback ─────────────────────────────────

class TestDetectDomainByContent:
    def test_mysql_keyword_in_hint(self):
        assert detect_domain("unknown_probe", "mysql deadlock found", "", "SYS_RESOURCE") == DOMAIN_DATABASE

    def test_dns_in_hint(self):
        assert detect_domain("unknown_probe", "DNS resolution failed", "", "SYS_HARD_FAIL") == DOMAIN_NETWORK

    def test_oom_kill_in_hint(self):
        assert detect_domain("unknown_probe", "oom kill process 1234", "", "SYS_RESOURCE") == DOMAIN_OS

    def test_disk_full_in_hint(self):
        assert detect_domain("unknown_probe", "no space left on device", "", "SYS_RESOURCE") == DOMAIN_STORAGE

    def test_auth_failure_in_hint(self):
        assert detect_domain("unknown_probe", "brute force detected ssh", "", "SIEM_SECURITY") == DOMAIN_SECURITY

    def test_panic_in_raw(self):
        assert detect_domain("app_log", "", "panic: runtime error index out of range", "SYS_HARD_FAIL") == DOMAIN_CONTAINER

    def test_lane_fallback_siem(self):
        assert detect_domain("unknown_probe", "", "", "SIEM_SECURITY") == DOMAIN_SECURITY

    def test_lane_fallback_app_http(self):
        assert detect_domain("unknown_probe", "", "", "APP_HTTP") == DOMAIN_APPLICATION

    def test_unknown_probe_no_content(self):
        assert detect_domain("totally_unknown", "", "", "UNKNOWN_LANE") == DOMAIN_UNKNOWN

    def test_no_lane_no_signal_returns_unknown(self):
        assert detect_domain("unknown", "", "", "") == DOMAIN_UNKNOWN


# ── detect_domain: label-based (alertname rules) ──────────────────────────

class TestDetectDomainByLabels:
    def test_kubepod_crashloop(self):
        labels = {"alertname": "KubePodCrashLooping"}
        assert detect_domain("unknown", "", "", "", labels=labels) == DOMAIN_CONTAINER

    def test_kubedeployment_unavailable(self):
        labels = {"alertname": "KubeDeploymentReplicasMismatch"}
        assert detect_domain("unknown", "", "", "", labels=labels) == DOMAIN_CONTAINER

    def test_kube_oom_kill(self):
        labels = {"alertname": "KubeOOMKill"}
        assert detect_domain("unknown", "", "", "", labels=labels) == DOMAIN_CONTAINER

    def test_kube_memory_pressure(self):
        labels = {"alertname": "KubeMemoryPressure"}
        assert detect_domain("unknown", "", "", "", labels=labels) == DOMAIN_OS

    def test_kube_cpu_throttle(self):
        labels = {"alertname": "KubeCPUThrottlingHigh"}
        assert detect_domain("unknown", "", "", "", labels=labels) == DOMAIN_OS

    def test_kube_network_policy(self):
        labels = {"alertname": "KubeNetworkPolicyBlocking"}
        assert detect_domain("unknown", "", "", "", labels=labels) == DOMAIN_NETWORK

    def test_disk_full_alertname(self):
        labels = {"alertname": "DiskSpaceFull"}
        assert detect_domain("unknown", "", "", "", labels=labels) == DOMAIN_STORAGE

    def test_postgres_alertname(self):
        labels = {"alertname": "PostgresReplicationLag"}
        assert detect_domain("unknown", "", "", "", labels=labels) == DOMAIN_DATABASE

    def test_http_error_alertname(self):
        labels = {"alertname": "HTTPErrorRateHigh"}
        assert detect_domain("unknown", "", "", "", labels=labels) == DOMAIN_APPLICATION

    def test_auth_alertname(self):
        labels = {"alertname": "AuthFailureSpike"}
        assert detect_domain("unknown", "", "", "", labels=labels) == DOMAIN_SECURITY

    def test_probe_prefix_beats_label(self):
        # probe prefix has higher priority than alertname label
        labels = {"alertname": "PostgresReplicationLag"}
        assert detect_domain("network_latency", "", "", "", labels=labels) == DOMAIN_NETWORK

    def test_label_beats_content_kw(self):
        # alertname label wins over content keywords when probe unknown
        labels = {"alertname": "KubePodCrashLooping"}
        assert detect_domain("unknown", "mysql deadlock found", "", "", labels=labels) == DOMAIN_CONTAINER

    def test_empty_labels_falls_back_to_content(self):
        labels = {"alertname": ""}
        assert detect_domain("unknown", "oom kill process", "", "SYS_RESOURCE", labels=labels) == DOMAIN_OS

    def test_none_labels_ignored(self):
        assert detect_domain("unknown", "oom kill process", "", "SYS_RESOURCE", labels=None) == DOMAIN_OS

    def test_label_case_insensitive(self):
        labels = {"alertname": "kubepodcrashlooping"}
        assert detect_domain("unknown", "", "", "", labels=labels) == DOMAIN_CONTAINER


# ── assess_domain_severity: OS ─────────────────────────────────────────────

class TestAssessSeverityOS:
    def test_oom_kill_critical(self):
        sev = assess_domain_severity(DOMAIN_OS, "oom kill: mysqld process", "", {})
        assert sev == "critical"

    def test_kernel_panic_critical(self):
        sev = assess_domain_severity(DOMAIN_OS, "kernel panic - not syncing", "", {})
        assert sev == "critical"

    def test_cpu_throttle_critical(self):
        sev = assess_domain_severity(DOMAIN_OS, "cpu throttling detected cgroup", "", {})
        assert sev == "critical"

    def test_disk_full_high(self):
        sev = assess_domain_severity(DOMAIN_OS, "high iowait observed", "", {})
        assert sev == "medium"

    def test_baseline_metrics(self):
        sev = assess_domain_severity(DOMAIN_OS, "", "", {"cpu_pct": 12.3, "mem_pct": 45.2})
        assert sev == "baseline"

    def test_numeric_critical_cpu(self):
        sev = assess_domain_severity(DOMAIN_OS, "", "", {"cpu_pct": 97.5})
        assert sev == "critical"

    def test_numeric_high_mem(self):
        sev = assess_domain_severity(DOMAIN_OS, "", "", {"mem_pct": 88.0})
        assert sev == "high"

    def test_numeric_oom_count(self):
        sev = assess_domain_severity(DOMAIN_OS, "", "", {"oom_count": 3})
        assert sev == "critical"


# ── assess_domain_severity: Network ────────────────────────────────────────

class TestAssessSeverityNetwork:
    def test_dns_fail_critical(self):
        sev = assess_domain_severity(DOMAIN_NETWORK, "DNS resolution failed for api.internal", "", {})
        assert sev == "critical"

    def test_connection_refused_high(self):
        sev = assess_domain_severity(DOMAIN_NETWORK, "connection refused 10.0.0.5:3306", "", {})
        assert sev == "high"

    def test_packet_loss_high(self):
        sev = assess_domain_severity(DOMAIN_NETWORK, "packet loss 45% detected", "", {})
        assert sev == "high"

    def test_baseline_latency(self):
        sev = assess_domain_severity(DOMAIN_NETWORK, "", "", {"latency_ms": 5.2})
        assert sev == "baseline"


# ── assess_domain_severity: Database ───────────────────────────────────────

class TestAssessSeverityDatabase:
    def test_deadlock_high(self):
        sev = assess_domain_severity(DOMAIN_DATABASE, "Deadlock found when trying to get lock", "", {})
        assert sev in ("critical", "high")

    def test_max_connections_critical(self):
        sev = assess_domain_severity(DOMAIN_DATABASE, "too many connections, max_connections reached", "", {})
        assert sev == "critical"

    def test_replication_lag_critical_from_fact(self):
        sev = assess_domain_severity(DOMAIN_DATABASE, "", "", {"replication_lag_s": 400})
        assert sev == "critical"

    def test_replication_lag_high_from_fact(self):
        sev = assess_domain_severity(DOMAIN_DATABASE, "", "", {"replication_lag_s": 120})
        assert sev == "high"

    def test_redis_oom(self):
        sev = assess_domain_severity(DOMAIN_DATABASE, "OOM command not allowed maxmemory", "", {})
        assert sev == "critical"

    def test_baseline_queries(self):
        sev = assess_domain_severity(DOMAIN_DATABASE, "", "", {"queries_per_sec": 1234})
        assert sev == "baseline"


# ── assess_domain_severity: Security ───────────────────────────────────────

class TestAssessSeveritySecurity:
    def test_brute_force_critical(self):
        sev = assess_domain_severity(DOMAIN_SECURITY, "brute force detected on ssh port", "", {})
        assert sev == "critical"

    def test_auth_failure_high(self):
        sev = assess_domain_severity(DOMAIN_SECURITY, "multiple authentication failures from 10.0.0.1", "", {})
        assert sev == "high"

    def test_baseline_auth_success(self):
        sev = assess_domain_severity(DOMAIN_SECURITY, "", "", {"auth_success_count": 50})
        assert sev == "baseline"


# ── assess_domain_severity: Application ────────────────────────────────────

class TestAssessSeverityApplication:
    def test_5xx_rate_critical(self):
        sev = assess_domain_severity(DOMAIN_APPLICATION, "5xx rate 75% above threshold", "", {})
        assert sev == "critical"

    def test_http_500_high(self):
        sev = assess_domain_severity(DOMAIN_APPLICATION, "internal server error http 500", "", {})
        assert sev == "high"

    def test_error_rate_critical_from_fact(self):
        sev = assess_domain_severity(DOMAIN_APPLICATION, "", "", {"error_rate": 55.0})
        assert sev == "critical"

    def test_p99_high_from_fact(self):
        sev = assess_domain_severity(DOMAIN_APPLICATION, "", "", {"latency_p99_ms": 2500})
        assert sev == "high"

    def test_baseline_request_rate(self):
        sev = assess_domain_severity(DOMAIN_APPLICATION, "", "", {"request_rate": 500})
        assert sev == "baseline"


# ── assess_domain_severity: Container ──────────────────────────────────────

class TestAssessSeverityContainer:
    def test_panic_critical(self):
        sev = assess_domain_severity(DOMAIN_CONTAINER, "panic: runtime error: index out of range", "", {})
        assert sev == "critical"

    def test_oom_kill_in_container(self):
        sev = assess_domain_severity(DOMAIN_CONTAINER, "oom killer activated: out of memory", "", {})
        assert sev == "critical"

    def test_connection_refused_high(self):
        sev = assess_domain_severity(DOMAIN_CONTAINER, "connection refused to redis:6379", "", {})
        assert sev == "high"

    def test_baseline_started(self):
        sev = assess_domain_severity(DOMAIN_CONTAINER, "server started listening on :8080", "", {})
        assert sev == "baseline"


# ── assess_domain_severity: Services ───────────────────────────────────────

class TestAssessSeverityServices:
    def test_oom_kill_service_critical(self):
        sev = assess_domain_severity(DOMAIN_SERVICES, "result: oom-kill (failed)", "", {})
        assert sev == "critical"

    def test_start_limit_critical(self):
        sev = assess_domain_severity(DOMAIN_SERVICES, "start request repeated too quickly", "", {})
        assert sev == "critical"

    def test_service_failed_high(self):
        sev = assess_domain_severity(DOMAIN_SERVICES, "service failed (result: exit-code)", "", {})
        assert sev == "high"

    def test_baseline_running(self):
        sev = assess_domain_severity(DOMAIN_SERVICES, "active (running) since", "", {})
        assert sev == "baseline"


# ── assess_domain_severity: Storage ────────────────────────────────────────

class TestAssessSeverityStorage:
    def test_no_space_left_critical(self):
        sev = assess_domain_severity(DOMAIN_STORAGE, "no space left on device /var/lib/mysql", "", {})
        assert sev == "critical"

    def test_raid_degraded_critical(self):
        sev = assess_domain_severity(DOMAIN_STORAGE, "raid degraded: disk md0 failed", "", {})
        assert sev == "critical"

    def test_disk_90_high_from_fact(self):
        sev = assess_domain_severity(DOMAIN_STORAGE, "", "", {"disk_used_pct": 92.0})
        assert sev == "high"

    def test_baseline_disk(self):
        sev = assess_domain_severity(DOMAIN_STORAGE, "", "", {"disk_used_pct": 45.0})
        assert sev == "baseline"
