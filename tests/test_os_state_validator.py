"""Tests for os_state_validator — OS-level state machine contrast check."""
import json
import pytest
from workers.os_state_validator import (
    compare_alert_claim_to_os_state,
    _OS_PROBE_HANDLERS,
    _alert_ctx_summary,
)


# ── helpers ────────────────────────────────────────────────────────────────

def _probe(probe_name: str, result: str, extracted_fact: dict, alert_hint: str = "") -> dict:
    return {
        "probe": probe_name,
        "result": result,
        "extracted_fact": json.dumps(extracted_fact),
        "alert_hint": alert_hint,
    }


def by(probe_name: str, result: str, ef: dict, hint: str = "") -> dict:
    return {probe_name: _probe(probe_name, result, ef, hint)}


# ── systemd_units ─────────────────────────────────────────────────────────

def test_systemd_passed_no_failed_units_returns_contrast():
    ev = by("systemd_units", "PASSED", {"critical_failed_units": [], "failed_units": []},
            "[host1] systemd: all critical units active")
    result = compare_alert_claim_to_os_state(ev)
    assert result is not None
    assert "systemd_units" in result
    assert "PASSED" in result


def test_systemd_failed_with_critical_units_returns_none():
    ev = by("systemd_units", "FAILED", {"critical_failed_units": ["nginx"], "failed_units": ["nginx"]},
            "[host1] CRITICAL: nginx")
    assert compare_alert_claim_to_os_state(ev) is None


def test_systemd_failed_result_returns_none_even_if_ef_empty():
    ev = by("systemd_units", "FAILED", {})
    assert compare_alert_claim_to_os_state(ev) is None


def test_systemd_passed_but_has_critical_units_in_ef_returns_none():
    ev = by("systemd_units", "PASSED", {"critical_failed_units": ["mysql"]})
    assert compare_alert_claim_to_os_state(ev) is None


# ── haproxy ───────────────────────────────────────────────────────────────

def test_haproxy_passed_no_down_backends_returns_contrast():
    ev = by("service_haproxy", "PASSED", {"down_backends": [], "down_backend_count": 0},
            "[host1] HAProxy all backends UP")
    result = compare_alert_claim_to_os_state(ev)
    assert result is not None
    assert "HAProxy" in result


def test_haproxy_prom_passed_returns_contrast():
    ev = by("service_haproxy_prom", "PASSED", {"down_backends": [], "down_backend_count": 0})
    assert compare_alert_claim_to_os_state(ev) is not None


def test_haproxy_with_down_backends_returns_none():
    ev = by("service_haproxy", "FAILED", {"down_backends": ["app/server1"], "down_backend_count": 1})
    assert compare_alert_claim_to_os_state(ev) is None


def test_haproxy_first_failed_second_passed_returns_none():
    """If service_haproxy FAILED, service_haproxy_prom PASSED must not override — fault is confirmed.

    When any registered probe confirms failure, PASSED probe contrasts are suppressed.
    The SYS_HARD_FAIL alert is legitimate (haproxy backends down); the alert is not suspect.
    """
    batch = {
        "service_haproxy": _probe("service_haproxy", "FAILED", {"down_backends": ["app/s1"]}),
        "service_haproxy_prom": _probe("service_haproxy_prom", "PASSED", {"down_backends": []}),
    }
    # service_haproxy FAILED confirms a real fault — no "healthy" contrast should be emitted
    # even though service_haproxy_prom PASSED with no down_backends.
    result = compare_alert_claim_to_os_state(batch)
    assert result is None


# ── disk_usage ────────────────────────────────────────────────────────────

def test_disk_passed_no_critical_returns_contrast():
    ev = by("disk_usage", "PASSED",
            {"critical_partitions": [], "disk_critical_count": 0, "inode_critical": [], "warn_partitions": []},
            "[host1] disk: all partitions OK")
    result = compare_alert_claim_to_os_state(ev)
    assert result is not None
    assert "disk_usage" in result


def test_disk_failed_with_critical_partitions_returns_none():
    ev = by("disk_usage", "FAILED",
            {"critical_partitions": ["/data(98%)"], "disk_critical_count": 1})
    assert compare_alert_claim_to_os_state(ev) is None


def test_disk_warn_result_returns_none():
    ev = by("disk_usage", "WARN",
            {"critical_partitions": [], "disk_critical_count": 0, "warn_partitions": ["/var(85%)"]})
    assert compare_alert_claim_to_os_state(ev) is None


def test_disk_degraded_result_returns_none():
    """DEGRADED/UNKNOWN/ERROR are not PASSED and must block contrast."""
    ev = by("disk_usage", "DEGRADED", {"critical_partitions": [], "disk_critical_count": 0})
    assert compare_alert_claim_to_os_state(ev) is None


def test_disk_passed_but_inode_critical_returns_none():
    ev = by("disk_usage", "PASSED",
            {"critical_partitions": [], "disk_critical_count": 0, "inode_critical": ["/var/spool(100%)"]})
    assert compare_alert_claim_to_os_state(ev) is None


# ── storage_nfs ───────────────────────────────────────────────────────────

def test_nfs_passed_no_errors_returns_contrast():
    ev = by("storage_nfs", "PASSED", {"nfs_error_count": 0, "stale_mounts": []},
            "[host1] NFS: all mounts OK")
    result = compare_alert_claim_to_os_state(ev)
    assert result is not None
    assert "NFS" in result


def test_nfs_failed_with_errors_returns_none():
    ev = by("storage_nfs", "FAILED", {"nfs_error_count": 2, "stale_mounts": ["/mnt/nfs-data"]})
    assert compare_alert_claim_to_os_state(ev) is None


# ── mysql_health ──────────────────────────────────────────────────────────

def test_mysql_passed_returns_contrast():
    ev = by("mysql_health", "PASSED", {"anomalies": [], "threads_connected": 42, "replication_lag_s": 0},
            "[host1] MySQL OK threads=42 repl_lag=0s")
    result = compare_alert_claim_to_os_state(ev)
    assert result is not None
    assert "mysql_health" in result


def test_mysql_failed_returns_none():
    ev = by("mysql_health", "FAILED", {"anomalies": ["replication SQL thread stopped"]})
    assert compare_alert_claim_to_os_state(ev) is None


def test_mysql_passed_but_anomalies_in_ef_returns_none():
    ev = by("mysql_health", "PASSED", {"anomalies": ["threads_connected=1200>500"]})
    assert compare_alert_claim_to_os_state(ev) is None


# ── proxysql_health ───────────────────────────────────────────────────────

def test_proxysql_passed_returns_contrast():
    ev = by("proxysql_health", "PASSED", {"anomalies": [], "clients": 150},
            "[host1] ProxySQL OK clients=150")
    result = compare_alert_claim_to_os_state(ev)
    assert result is not None
    assert "proxysql_health" in result


def test_proxysql_failed_returns_none():
    ev = by("proxysql_health", "FAILED", {"anomalies": ["proxysql_clients=2100>2000"]})
    assert compare_alert_claim_to_os_state(ev) is None


# ── D1 — Process ──────────────────────────────────────────────────────────

def test_cron_jobs_passed_returns_contrast():
    ev = by("cron_jobs", "PASSED", {"failed_cron_count": 0, "failed_jobs": []})
    result = compare_alert_claim_to_os_state(ev)
    assert result is not None
    assert "cron_jobs" in result


def test_cron_jobs_failed_returns_none():
    ev = by("cron_jobs", "FAILED", {"failed_cron_count": 3, "failed_jobs": ["backup-daily"]})
    assert compare_alert_claim_to_os_state(ev) is None


def test_zombie_processes_passed_returns_contrast():
    ev = by("zombie_processes", "PASSED", {"zombie_count": 0})
    assert compare_alert_claim_to_os_state(ev) is not None


def test_zombie_processes_with_zombies_returns_none():
    ev = by("zombie_processes", "PASSED", {"zombie_count": 5})
    assert compare_alert_claim_to_os_state(ev) is None


def test_oom_events_passed_returns_contrast():
    ev = by("oom_events", "PASSED", {"oom_count": 0, "recent_oom_victims": []})
    assert compare_alert_claim_to_os_state(ev) is not None


def test_oom_events_with_oom_returns_none():
    ev = by("oom_events", "PASSED", {"oom_count": 2, "recent_oom_victims": ["java-worker"]})
    assert compare_alert_claim_to_os_state(ev) is None


# ── D2 — Storage (new probes) ─────────────────────────────────────────────

def test_raid_mdadm_passed_returns_contrast():
    ev = by("raid_mdadm", "PASSED", {"degraded_arrays": [], "failed_devices": 0})
    assert compare_alert_claim_to_os_state(ev) is not None


def test_raid_mdadm_degraded_returns_none():
    ev = by("raid_mdadm", "FAILED", {"degraded_arrays": ["md1"], "failed_devices": 1})
    assert compare_alert_claim_to_os_state(ev) is None


def test_lvm_volumes_passed_returns_contrast():
    ev = by("lvm_volumes", "PASSED", {"partial_vgs": [], "failed_pvs": []})
    assert compare_alert_claim_to_os_state(ev) is not None


def test_swap_usage_passed_returns_contrast():
    ev = by("swap_usage", "PASSED", {"swap_used_pct": 20})
    assert compare_alert_claim_to_os_state(ev) is not None


def test_swap_usage_critical_returns_none():
    ev = by("swap_usage", "PASSED", {"swap_used_pct": 95})
    assert compare_alert_claim_to_os_state(ev) is None


# ── D3 — Network ──────────────────────────────────────────────────────────

def test_network_interfaces_passed_returns_contrast():
    ev = by("network_interfaces", "PASSED", {"down_interfaces": [], "error_interfaces": []})
    assert compare_alert_claim_to_os_state(ev) is not None


def test_network_interfaces_down_returns_none():
    ev = by("network_interfaces", "FAILED", {"down_interfaces": ["eth1"], "error_interfaces": []})
    assert compare_alert_claim_to_os_state(ev) is None


def test_dns_resolution_passed_returns_contrast():
    ev = by("dns_resolution", "PASSED", {"failed_lookups": [], "lookup_error_count": 0})
    assert compare_alert_claim_to_os_state(ev) is not None


def test_tcp_connections_passed_returns_contrast():
    ev = by("tcp_connections", "PASSED", {"time_wait_excess": False, "syn_flood_indicator": False})
    assert compare_alert_claim_to_os_state(ev) is not None


# ── D4 — Database (new probes) ────────────────────────────────────────────

def test_postgresql_passed_returns_contrast():
    ev = by("postgresql_health", "PASSED", {"anomalies": [], "replication_lag_s": 0})
    assert compare_alert_claim_to_os_state(ev) is not None


def test_postgresql_high_repl_lag_returns_none():
    ev = by("postgresql_health", "PASSED", {"anomalies": [], "replication_lag_s": 60})
    assert compare_alert_claim_to_os_state(ev) is None


def test_redis_os_passed_returns_contrast():
    ev = by("redis_os_health", "PASSED", {"anomalies": []})
    assert compare_alert_claim_to_os_state(ev) is not None


def test_mongodb_passed_returns_contrast():
    ev = by("mongodb_health", "PASSED", {"anomalies": [], "repl_lag_s": 0})
    assert compare_alert_claim_to_os_state(ev) is not None


# ── D5 — Proxy/LB (new probes) ───────────────────────────────────────────

def test_nginx_passed_returns_contrast():
    ev = by("service_nginx", "PASSED", {"error_rate_pct": 0, "upstream_errors": []})
    assert compare_alert_claim_to_os_state(ev) is not None


def test_nginx_high_error_rate_returns_none():
    ev = by("service_nginx", "PASSED", {"error_rate_pct": 10, "upstream_errors": ["upstream_timeout"]})
    assert compare_alert_claim_to_os_state(ev) is None


def test_keepalived_passed_returns_contrast():
    ev = by("service_keepalived", "PASSED", {"vip_missing": False, "state": "MASTER"})
    assert compare_alert_claim_to_os_state(ev) is not None


def test_keepalived_fault_returns_none():
    ev = by("service_keepalived", "FAILED", {"vip_missing": True, "state": "FAULT"})
    assert compare_alert_claim_to_os_state(ev) is None


# ── D6 — Hardware ─────────────────────────────────────────────────────────

def test_kernel_errors_passed_returns_contrast():
    ev = by("kernel_errors", "PASSED", {"critical_errors": [], "mce_count": 0})
    assert compare_alert_claim_to_os_state(ev) is not None


def test_kernel_errors_with_mce_returns_none():
    ev = by("kernel_errors", "PASSED", {"critical_errors": [], "mce_count": 3})
    assert compare_alert_claim_to_os_state(ev) is None


def test_memory_hw_errors_passed_returns_contrast():
    ev = by("memory_hw_errors", "PASSED", {"correctable_errors": 0, "uncorrectable_errors": 0})
    assert compare_alert_claim_to_os_state(ev) is not None


def test_memory_hw_uncorrectable_returns_none():
    ev = by("memory_hw_errors", "PASSED", {"correctable_errors": 0, "uncorrectable_errors": 1})
    assert compare_alert_claim_to_os_state(ev) is None


# ── D7 — Container ────────────────────────────────────────────────────────

def test_docker_daemon_passed_returns_contrast():
    ev = by("docker_daemon", "PASSED", {"daemon_error": None, "unhealthy_containers": 0})
    assert compare_alert_claim_to_os_state(ev) is not None


def test_docker_daemon_unhealthy_containers_returns_none():
    ev = by("docker_daemon", "PASSED", {"daemon_error": None, "unhealthy_containers": 2})
    assert compare_alert_claim_to_os_state(ev) is None


def test_containerd_state_passed_returns_contrast():
    ev = by("containerd_state", "PASSED", {"daemon_error": None, "plugin_errors": []})
    assert compare_alert_claim_to_os_state(ev) is not None


def test_containerd_plugin_errors_returns_none():
    ev = by("containerd_state", "PASSED", {"daemon_error": None, "plugin_errors": ["snapshotter-error"]})
    assert compare_alert_claim_to_os_state(ev) is None


# ── multi-probe batch ─────────────────────────────────────────────────────

def test_mixed_batch_failed_probe_blocks_passed_contrast():
    """When any registered probe FAILED, PASSED probe contrasts are suppressed.

    mysql_health FAILED confirms a real fault in the system. The SYS_HARD_FAIL alert is
    legitimate — disk_usage being healthy does not mean the alert is a false positive.
    """
    batch = {
        "disk_usage": _probe("disk_usage", "PASSED",
                             {"critical_partitions": [], "disk_critical_count": 0, "inode_critical": []},
                             "disk OK"),
        "mysql_health": _probe("mysql_health", "FAILED", {"anomalies": ["replication SQL thread stopped"]}),
    }
    # mysql FAILED confirms real fault → no "healthy" contrast from disk_usage
    result = compare_alert_claim_to_os_state(batch)
    assert result is None


def test_by_probe_duplicate_prefers_failed():
    """build_by_probe logic: when two items share a probe key, FAILED must win over PASSED."""
    batch = [
        {"probe": "disk_usage", "result": "PASSED",
         "extracted_fact": json.dumps({"critical_partitions": [], "disk_critical_count": 0, "inode_critical": []}),
         "alert_hint": "host1 OK"},
        {"probe": "disk_usage", "result": "FAILED",
         "extracted_fact": json.dumps({"critical_partitions": ["/data(99%)"], "disk_critical_count": 1}),
         "alert_hint": "host2 CRITICAL"},
    ]
    # Simulate the FAILED-wins aggregation from evidence_consumer
    by_probe: dict = {}
    for b in batch:
        key = str(b.get("probe") or "")
        if not key:
            continue
        existing = by_probe.get(key)
        if existing is None:
            by_probe[key] = dict(b)
        else:
            ex_r = str(existing.get("result") or "").upper()
            new_r = str(b.get("result") or "").upper()
            if ex_r == "PASSED" and new_r != "PASSED":
                by_probe[key] = dict(b)
    # After aggregation, FAILED must have won
    assert by_probe["disk_usage"]["result"] == "FAILED"
    assert compare_alert_claim_to_os_state(by_probe) is None


def test_empty_batch_returns_none():
    assert compare_alert_claim_to_os_state({}) is None


def test_unknown_probe_passed_empty_ef_returns_none():
    # Unregistered probe + PASSED + empty extracted_fact {} = unknown state, NOT healthy.
    # Empty dict has no indicators of either health or failure — we cannot trust it.
    # Fixed in chaos-test-lane-business: empty ef from unregistered probe → skip, return None.
    batch = {
        "k8s_clinical_pod_status": {"probe": "k8s_clinical_pod_status", "result": "PASSED",
                                    "extracted_fact": "{}", "alert_hint": ""},
    }
    result = compare_alert_claim_to_os_state(batch)
    assert result is None, (
        "Unregistered probe with empty extracted_fact must not generate contrast — "
        "unknown state is not the same as healthy state."
    )


def test_unknown_probe_failed_returns_none():
    batch = {
        "k8s_clinical_pod_status": {"probe": "k8s_clinical_pod_status", "result": "FAILED",
                                    "extracted_fact": "{}", "alert_hint": ""},
    }
    assert compare_alert_claim_to_os_state(batch) is None


# ── extracted_fact as dict (not JSON string) ──────────────────────────────

def test_ef_as_dict_parsed_correctly():
    ev = {
        "systemd_units": {
            "probe": "systemd_units",
            "result": "PASSED",
            "extracted_fact": {"critical_failed_units": [], "failed_units": []},
            "alert_hint": "all OK",
        }
    }
    assert compare_alert_claim_to_os_state(ev) is not None


def test_ef_as_malformed_json_treated_as_empty():
    ev = {
        "systemd_units": {
            "probe": "systemd_units",
            "result": "PASSED",
            "extracted_fact": "NOT_JSON{{",
            "alert_hint": "all OK",
        }
    }
    # malformed ef → treated as {} → no anomalies found → contrast returned
    result = compare_alert_claim_to_os_state(ev)
    assert result is not None


# ── GIGO sanitizer ────────────────────────────────────────────────────────

def test_alert_hint_truncated_at_500():
    long_hint = "x" * 600
    ev = by("systemd_units", "PASSED", {"critical_failed_units": [], "failed_units": []}, long_hint)
    result = compare_alert_claim_to_os_state(ev)
    assert result is not None
    assert len(long_hint) > 500  # ensure our test input is actually long
    # contrast returned — sanitizer ran without error
    assert "systemd_units" in result


# ── registry coverage ─────────────────────────────────────────────────────

def test_all_expected_probes_registered():
    expected = {
        "systemd_units",
        "cron_jobs", "zombie_processes", "oom_events",
        "disk_usage", "storage_nfs", "raid_mdadm", "lvm_volumes", "swap_usage",
        "network_interfaces", "dns_resolution", "tcp_connections",
        "mysql_health", "proxysql_health", "postgresql_health", "redis_os_health", "mongodb_health",
        "service_haproxy", "service_haproxy_prom", "service_nginx", "service_keepalived",
        "kernel_errors", "memory_hw_errors",
        "docker_daemon", "containerd_state",
    }
    missing = expected - set(_OS_PROBE_HANDLERS.keys())
    assert not missing, f"Missing probe handlers: {missing}"


# ── _alert_ctx_summary ────────────────────────────────────────────────────

def test_alert_ctx_summary_full():
    ctx = {
        "alertname": "DiskFull",
        "namespace": "production",
        "source": "prometheus",
        "labels": {"severity": "critical"},
        "annotations": {"summary": "Disk at 99%"},
    }
    s = _alert_ctx_summary(ctx)
    assert "DiskFull" in s
    assert "sev=critical" in s
    assert "ns=production" in s
    assert "Disk at 99%" in s


def test_alert_ctx_summary_empty():
    assert _alert_ctx_summary({}) == "(no alert context)"


def test_alert_ctx_summary_partial():
    ctx = {"alertname": "MySQLDown"}
    s = _alert_ctx_summary(ctx)
    assert "MySQLDown" in s


# ── alert_ctx propagated through compare_alert_claim_to_os_state ──────────

def test_compare_with_alert_ctx_includes_summary():
    ctx = {
        "alertname": "SystemdFailed",
        "namespace": "infra",
        "labels": {"severity": "warning"},
        "annotations": {"summary": "unit nginx.service failed"},
        "source": "alertmanager",
    }
    ev = by("systemd_units", "PASSED", {"critical_failed_units": [], "failed_units": []})
    result = compare_alert_claim_to_os_state(ev, alert_ctx=ctx)
    assert result is not None
    assert "SystemdFailed" in result


def test_compare_without_alert_ctx_still_works():
    ev = by("disk_usage", "PASSED", {"critical_partitions": [], "disk_critical_count": 0, "inode_critical": []})
    result = compare_alert_claim_to_os_state(ev)
    assert result is not None


# ── Part B — generic fallback for unknown probes ──────────────────────────

def test_unknown_probe_passed_no_anomaly_returns_contrast():
    batch = {"custom_health_check": {
        "probe": "custom_health_check",
        "result": "PASSED",
        "extracted_fact": '{"status": "ok", "latency_ms": 5}',
    }}
    result = compare_alert_claim_to_os_state(batch)
    assert result is not None
    assert "custom_health_check" in result
    assert "PASSED" in result


def test_unknown_probe_passed_with_anomaly_key_returns_none():
    batch = {"custom_health_check": {
        "probe": "custom_health_check",
        "result": "PASSED",
        "extracted_fact": '{"error_count": 3, "status": "ok"}',
    }}
    # "error_count" contains "error" → anomaly detected → no contrast
    assert compare_alert_claim_to_os_state(batch) is None


def test_unknown_probe_failed_returns_none():
    batch = {"custom_health_check": {
        "probe": "custom_health_check",
        "result": "FAILED",
        "extracted_fact": '{"status": "down"}',
    }}
    assert compare_alert_claim_to_os_state(batch) is None


def test_unknown_probe_with_alert_ctx_includes_ctx_in_result():
    ctx = {"alertname": "CustomServiceDown", "namespace": "staging"}
    batch = {"new_service_probe": {
        "probe": "new_service_probe",
        "result": "PASSED",
        "extracted_fact": '{"healthy": true, "connections": 42}',
    }}
    result = compare_alert_claim_to_os_state(batch, alert_ctx=ctx)
    assert result is not None
    assert "new_service_probe" in result
    assert "CustomServiceDown" in result
