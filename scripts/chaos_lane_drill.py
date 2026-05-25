#!/usr/bin/env python3
"""
Chaos drill runner for Omni 4-lane diagnostic system.
Injects faults per lane and verifies auto-remediation within SLO.

The system monitors infrastructure beyond K8s:
  - Bare metal OS (systemd, disk, swap, OOM, kernel errors)
  - Databases (MySQL, ProxySQL, PostgreSQL, MongoDB, Redis)
  - Network (DNS, NFS, TCP, interfaces)
  - Load balancers / services (HAProxy, nginx, keepalived)
  - K8s workloads (CrashLoop, OOMKilled, ImagePullBackOff)

Usage:
  python scripts/chaos_lane_drill.py --lane all
  python scripts/chaos_lane_drill.py --lane resource
  python scripts/chaos_lane_drill.py --lane hardfail-systemd
  python scripts/chaos_lane_drill.py --lane hardfail-mysql
  python scripts/chaos_lane_drill.py --lane all-infra   # all non-K8s scenarios
  python scripts/chaos_lane_drill.py --lane siem --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("chaos_lane_drill")

# ── Constants ────────────────────────────────────────────────────────────────

GATEWAY_URL = os.getenv("OMNI_GATEWAY_URL", "http://localhost:8000")
GATEWAY_API_KEY = os.getenv("OMNI_GATEWAY_API_KEY", "")

_SLO_SECONDS: dict[str, int] = {
    # K8s workload lanes
    "resource": 120,
    "hardfail": 120,
    "http": 120,
    "siem": 300,
    # Bare metal OS lanes
    "resource-baremetal": 120,
    "hardfail-systemd": 120,
    "hardfail-disk": 120,
    "hardfail-swap": 120,
    "hardfail-oom": 120,
    # Database lanes
    "hardfail-mysql": 120,
    "hardfail-proxysql": 120,
    "hardfail-postgresql": 120,
    "hardfail-mongodb": 120,
    # Network / storage lanes
    "hardfail-nfs": 120,
    "hardfail-dns": 120,
    "hardfail-haproxy": 120,
}

_POLL_INTERVAL = 5  # seconds between CRAT/advisory checks


# ── Drill result dataclass ────────────────────────────────────────────────────

@dataclass
class DrillResult:
    lane: str
    dry_run: bool
    injected_at: float = 0.0
    detected_at: float | None = None
    advisory_verdict: str = ""
    crat_written: bool = False
    action_type: str = ""
    within_slo: bool = False
    error: str = ""
    notes: list[str] = field(default_factory=list)

    def elapsed(self) -> float:
        if self.detected_at and self.injected_at:
            return round(self.detected_at - self.injected_at, 1)
        return -1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "lane": self.lane,
            "dry_run": self.dry_run,
            "injected_at_iso": _iso(self.injected_at),
            "detected_at_iso": _iso(self.detected_at) if self.detected_at else None,
            "elapsed_seconds": self.elapsed(),
            "advisory_verdict": self.advisory_verdict,
            "crat_written": self.crat_written,
            "action_type": self.action_type,
            "within_slo": self.within_slo,
            "slo_budget_seconds": _SLO_SECONDS.get(self.lane, 120),
            "error": self.error,
            "notes": self.notes,
        }


def _iso(ts: float | None) -> str:
    if not ts:
        return ""
    import datetime
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Fault payloads ────────────────────────────────────────────────────────────

def _resource_payload(trace_id: str) -> dict:
    """Lane 1 — resource anomaly: CPU spike alert."""
    return {
        "receiver": "omni-webhook",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "ChaosDrillCPUSpike",
                    "severity": "warning",
                    "namespace": "multi-agent",
                    "pod": f"chaos-target-{trace_id[-12:]}",
                    "deployment": f"chaos-target-{trace_id[-12:]}",
                    "container": "chaos-target",
                    "chaos_drill": "true",
                    "chaos_lane": "resource",
                    "trace_id": trace_id,
                },
                "annotations": {
                    "summary": "[CHAOS DRILL] CPU spike injected for lane-1 resource test",
                    "description": (
                        "Chaos drill: container chaos-target CPU throttling 95% for 5m. "
                        "z_cpu=4.5 (3-sigma breach). This is a synthetic drill — no real workload affected."
                    ),
                },
                "startsAt": _iso(time.time()),
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": "http://prometheus.monitor.svc.cluster.local:9090",
            }
        ],
        "groupLabels": {"alertname": "ChaosDrillCPUSpike"},
        "commonLabels": {},
        "commonAnnotations": {},
        "externalURL": "http://alertmanager:9093",
    }


def _hardfail_payload(trace_id: str) -> dict:
    """Lane 2 — hard fail: CrashLoopBackOff synthetic alert."""
    return {
        "receiver": "omni-webhook",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "ChaosDrillCrashLoop",
                    "severity": "critical",
                    "namespace": "multi-agent",
                    "pod": f"chaos-hardfail-{trace_id[-12:]}-abc12",
                    "deployment": f"chaos-hardfail-{trace_id[-12:]}",
                    "reason": "CrashLoopBackOff",
                    "chaos_drill": "true",
                    "chaos_lane": "hardfail",
                    "trace_id": trace_id,
                },
                "annotations": {
                    "summary": "[CHAOS DRILL] CrashLoopBackOff injected for lane-2 hardfail test",
                    "description": (
                        "Chaos drill: pod chaos-hardfail in CrashLoopBackOff. "
                        "Exit code 137 (OOMKilled). Restart count: 5. "
                        "This is a synthetic drill — no real workload affected."
                    ),
                },
                "startsAt": _iso(time.time()),
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": "http://prometheus.monitor.svc.cluster.local:9090",
            }
        ],
        "groupLabels": {"alertname": "ChaosDrillCrashLoop"},
        "commonLabels": {},
        "commonAnnotations": {},
        "externalURL": "http://alertmanager:9093",
    }


def _http_payload(trace_id: str) -> dict:
    """Lane 3 — business HTTP errors: 5xx surge alert."""
    return {
        "receiver": "omni-webhook",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "ChaosDrillHTTP5xxSurge",
                    "severity": "warning",
                    "namespace": "multi-agent",
                    "pod": f"chaos-http-{trace_id[:8]}",
                    "deployment": "chaos-http",
                    "chaos_drill": "true",
                    "chaos_lane": "http",
                    "trace_id": trace_id,
                },
                "annotations": {
                    "summary": "[CHAOS DRILL] 5xx surge injected for lane-3 HTTP error test",
                    "description": (
                        "Chaos drill: 503 error rate 85% over 5m window (250/300 requests). "
                        "Loki access log shows sustained HTTP 503 from chaos-http service. "
                        "This is a synthetic drill — no real workload affected."
                    ),
                },
                "startsAt": _iso(time.time()),
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": "http://prometheus.monitor.svc.cluster.local:9090",
            }
        ],
        "groupLabels": {"alertname": "ChaosDrillHTTP5xxSurge"},
        "commonLabels": {},
        "commonAnnotations": {},
        "externalURL": "http://alertmanager:9093",
    }


def _siem_payload(trace_id: str) -> dict:
    """Lane 4 — SIEM: synthetic DDoS incident alert."""
    return {
        "receiver": "omni-webhook",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "ChaosDrillSIEMDDoS",
                    "severity": "critical",
                    "namespace": "multi-agent",
                    "siem_source": "finguard",
                    "siem_category": "ddos",
                    "siem_incident_id": f"chaos-{trace_id[:12]}",
                    "source_ip": "198.51.100.1",
                    "tenant": "chaos-drill-tenant",
                    "chaos_drill": "true",
                    "chaos_lane": "siem",
                    "trace_id": trace_id,
                },
                "annotations": {
                    "summary": "[CHAOS DRILL] DDoS incident injected for lane-4 SIEM test",
                    "description": (
                        "Chaos drill: DDoS detected — 50,000 req/min from 198.51.100.1 "
                        "targeting multi-agent/api-gateway. Packet rate 2.5M pps. "
                        "SIEM incident_id: chaos-" + trace_id[:12] + ". "
                        "This is a synthetic drill — no real attack."
                    ),
                },
                "startsAt": _iso(time.time()),
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": "http://prometheus.monitor.svc.cluster.local:9090",
            }
        ],
        "groupLabels": {"alertname": "ChaosDrillSIEMDDoS"},
        "commonLabels": {},
        "commonAnnotations": {},
        "externalURL": "http://alertmanager:9093",
    }


# ── Non-K8s payload builders ──────────────────────────────────────────────────

def _resource_baremetal_payload(trace_id: str) -> dict:
    """Lane 1 — resource anomaly on bare metal server: CPU spike (not K8s)."""
    return {
        "receiver": "omni-webhook",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "ChaosDrillBareMetalCPUHigh",
                    "severity": "warning",
                    "job": "node",
                    "instance": f"db-primary-01:9100",
                    "host": "db-primary-01",
                    "env": "prod",
                    "chaos_drill": "true",
                    "chaos_lane": "resource-baremetal",
                    "trace_id": trace_id,
                },
                "annotations": {
                    "summary": "[CHAOS DRILL] CPU sustained high on bare metal node db-primary-01",
                    "description": (
                        "Chaos drill: node db-primary-01 CPU utilization at 94% for 8m. "
                        "z_cpu=4.2 (3-sigma breach vs 7-day rolling baseline). "
                        "Top processes: mysqld 78%, ksoftirqd 9%. "
                        "This is a synthetic drill — no real host affected."
                    ),
                },
                "startsAt": _iso(time.time()),
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": "http://prometheus.monitor.svc.cluster.local:9090",
            }
        ],
        "groupLabels": {"alertname": "ChaosDrillBareMetalCPUHigh"},
        "commonLabels": {},
        "commonAnnotations": {},
        "externalURL": "http://alertmanager:9093",
    }


def _hardfail_systemd_payload(trace_id: str) -> dict:
    """Lane 2 — systemd critical service failure on bare metal."""
    return {
        "receiver": "omni-webhook",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "SysHardFailServiceDown",
                    "severity": "critical",
                    "job": "node",
                    "instance": "app-server-03:9100",
                    "host": "app-server-03",
                    "service": "nginx.service",
                    "chaos_drill": "true",
                    "chaos_lane": "hardfail-systemd",
                    "trace_id": trace_id,
                },
                "annotations": {
                    "summary": "[CHAOS DRILL] nginx.service in failed state on app-server-03",
                    "description": (
                        "Chaos drill: nginx.service entered failed state 12m ago. "
                        "systemctl status: ActiveState=failed, Result=exit-code. "
                        "Last journal: bind() to 0.0.0.0:80 failed (98: Address already in use). "
                        "Service restart limit reached (3/30s). "
                        "This is a synthetic drill — no real service affected."
                    ),
                },
                "startsAt": _iso(time.time()),
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": "http://prometheus.monitor.svc.cluster.local:9090",
            }
        ],
        "groupLabels": {"alertname": "SysHardFailServiceDown"},
        "commonLabels": {},
        "commonAnnotations": {},
        "externalURL": "http://alertmanager:9093",
    }


def _hardfail_disk_payload(trace_id: str) -> dict:
    """Lane 2 — disk critical on database server."""
    return {
        "receiver": "omni-webhook",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "SysHardFailDiskCritical",
                    "severity": "critical",
                    "job": "node",
                    "instance": "db-primary-01:9100",
                    "host": "db-primary-01",
                    "mountpoint": "/var/lib/mysql",
                    "device": "/dev/sdb1",
                    "chaos_drill": "true",
                    "chaos_lane": "hardfail-disk",
                    "trace_id": trace_id,
                },
                "annotations": {
                    "summary": "[CHAOS DRILL] Disk /var/lib/mysql at 97.3% on db-primary-01",
                    "description": (
                        "Chaos drill: filesystem /var/lib/mysql (device /dev/sdb1) at 97.3% usage. "
                        "Used: 184.2GB / 190GB. Free: 1.8GB remaining. "
                        "InnoDB tablespace writes failing with errno=28 (No space left on device). "
                        "MySQL binary logs consuming 12GB — purge required. "
                        "This is a synthetic drill — no real disk affected."
                    ),
                },
                "startsAt": _iso(time.time()),
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": "http://prometheus.monitor.svc.cluster.local:9090",
            }
        ],
        "groupLabels": {"alertname": "SysHardFailDiskCritical"},
        "commonLabels": {},
        "commonAnnotations": {},
        "externalURL": "http://alertmanager:9093",
    }


def _hardfail_swap_payload(trace_id: str) -> dict:
    """Lane 2 — swap exhaustion on application VM."""
    return {
        "receiver": "omni-webhook",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "SysHardFailSwapExhausted",
                    "severity": "critical",
                    "job": "node",
                    "instance": "worker-vm-07:9100",
                    "host": "worker-vm-07",
                    "chaos_drill": "true",
                    "chaos_lane": "hardfail-swap",
                    "trace_id": trace_id,
                },
                "annotations": {
                    "summary": "[CHAOS DRILL] Swap 100% exhausted on worker-vm-07",
                    "description": (
                        "Chaos drill: swap fully exhausted on worker-vm-07. "
                        "swap_used_pct=100% (8GB / 8GB). Physical RAM at 98% (30.9GB / 32GB). "
                        "kswapd0 pegged at 99% CPU. OOM killer candidates queued. "
                        "Application response time degraded 40x (p99=18s). "
                        "This is a synthetic drill — no real VM affected."
                    ),
                },
                "startsAt": _iso(time.time()),
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": "http://prometheus.monitor.svc.cluster.local:9090",
            }
        ],
        "groupLabels": {"alertname": "SysHardFailSwapExhausted"},
        "commonLabels": {},
        "commonAnnotations": {},
        "externalURL": "http://alertmanager:9093",
    }


def _hardfail_oom_payload(trace_id: str) -> dict:
    """Lane 2 — kernel OOM kills on application host."""
    return {
        "receiver": "omni-webhook",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "SysHardFailOOMKill",
                    "severity": "critical",
                    "job": "node",
                    "instance": "app-server-01:9100",
                    "host": "app-server-01",
                    "chaos_drill": "true",
                    "chaos_lane": "hardfail-oom",
                    "trace_id": trace_id,
                },
                "annotations": {
                    "summary": "[CHAOS DRILL] Kernel OOM kills on app-server-01 (4 in 10m)",
                    "description": (
                        "Chaos drill: 4 OOM kill events in 10-minute window on app-server-01. "
                        "Victims: java (pid 28341, 12.8GB), java (pid 29012, 11.2GB). "
                        "dmesg: Out of memory: Kill process 28341 (java) score 892. "
                        "Available memory at OOM: 42MB / 64GB. "
                        "This is a synthetic drill — no real host affected."
                    ),
                },
                "startsAt": _iso(time.time()),
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": "http://prometheus.monitor.svc.cluster.local:9090",
            }
        ],
        "groupLabels": {"alertname": "SysHardFailOOMKill"},
        "commonLabels": {},
        "commonAnnotations": {},
        "externalURL": "http://alertmanager:9093",
    }


def _hardfail_mysql_payload(trace_id: str) -> dict:
    """Lane 2 — MySQL hard failure on database host."""
    return {
        "receiver": "omni-webhook",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "SysHardFailMySQLDown",
                    "severity": "critical",
                    "job": "mysql",
                    "instance": "db-primary-01:3306",
                    "host": "db-primary-01",
                    "chaos_drill": "true",
                    "chaos_lane": "hardfail-mysql",
                    "trace_id": trace_id,
                },
                "annotations": {
                    "summary": "[CHAOS DRILL] MySQL unreachable on db-primary-01:3306",
                    "description": (
                        "Chaos drill: MySQL server on db-primary-01 not accepting connections. "
                        "errno=2002 (Can't connect to MySQL server on localhost). "
                        "mysqld process absent from ps aux. "
                        "Last 3 connection attempts failed over 30s window. "
                        "InnoDB crash recovery log found: innodb_force_recovery=0 failed. "
                        "This is a synthetic drill — no real MySQL affected."
                    ),
                },
                "startsAt": _iso(time.time()),
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": "http://prometheus.monitor.svc.cluster.local:9090",
            }
        ],
        "groupLabels": {"alertname": "SysHardFailMySQLDown"},
        "commonLabels": {},
        "commonAnnotations": {},
        "externalURL": "http://alertmanager:9093",
    }


def _hardfail_proxysql_payload(trace_id: str) -> dict:
    """Lane 2 — ProxySQL failure affecting all backend routes."""
    return {
        "receiver": "omni-webhook",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "SysHardFailProxySQLDown",
                    "severity": "critical",
                    "job": "proxysql",
                    "instance": "lb-db-01:6032",
                    "host": "lb-db-01",
                    "chaos_drill": "true",
                    "chaos_lane": "hardfail-proxysql",
                    "trace_id": trace_id,
                },
                "annotations": {
                    "summary": "[CHAOS DRILL] ProxySQL all backends OFFLINE on lb-db-01",
                    "description": (
                        "Chaos drill: ProxySQL on lb-db-01 reporting all MySQL backends OFFLINE. "
                        "stats_mysql_backend_groups: hostgroup 10 — 0/3 servers ONLINE. "
                        "Connection errors: 1847 in last 60s. "
                        "Client queue depth: 3200 pending connections. "
                        "ProxySQL process running but monitor thread stalled. "
                        "This is a synthetic drill — no real ProxySQL affected."
                    ),
                },
                "startsAt": _iso(time.time()),
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": "http://prometheus.monitor.svc.cluster.local:9090",
            }
        ],
        "groupLabels": {"alertname": "SysHardFailProxySQLDown"},
        "commonLabels": {},
        "commonAnnotations": {},
        "externalURL": "http://alertmanager:9093",
    }


def _hardfail_postgresql_payload(trace_id: str) -> dict:
    """Lane 2 — PostgreSQL replication lag / connection failure."""
    return {
        "receiver": "omni-webhook",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "SysHardFailPostgreSQLReplicationLag",
                    "severity": "critical",
                    "job": "postgres",
                    "instance": "db-pg-replica-01:5432",
                    "host": "db-pg-replica-01",
                    "chaos_drill": "true",
                    "chaos_lane": "hardfail-postgresql",
                    "trace_id": trace_id,
                },
                "annotations": {
                    "summary": "[CHAOS DRILL] PostgreSQL replication lag 480s on db-pg-replica-01",
                    "description": (
                        "Chaos drill: PostgreSQL replica db-pg-replica-01 streaming replication stalled. "
                        "pg_stat_replication: replay_lag=480s, write_lag=482s. "
                        "WAL receiver process exited (signal 9). "
                        "Primary LSN: 0/A5001B8, Replica LSN: 0/9F200000. "
                        "This is a synthetic drill — no real PostgreSQL affected."
                    ),
                },
                "startsAt": _iso(time.time()),
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": "http://prometheus.monitor.svc.cluster.local:9090",
            }
        ],
        "groupLabels": {"alertname": "SysHardFailPostgreSQLReplicationLag"},
        "commonLabels": {},
        "commonAnnotations": {},
        "externalURL": "http://alertmanager:9093",
    }


def _hardfail_mongodb_payload(trace_id: str) -> dict:
    """Lane 2 — MongoDB replica set member failure."""
    return {
        "receiver": "omni-webhook",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "SysHardFailMongoDBReplicaSetDegraded",
                    "severity": "critical",
                    "job": "mongodb",
                    "instance": "db-mongo-02:27017",
                    "host": "db-mongo-02",
                    "chaos_drill": "true",
                    "chaos_lane": "hardfail-mongodb",
                    "trace_id": trace_id,
                },
                "annotations": {
                    "summary": "[CHAOS DRILL] MongoDB replica set PRIMARY unreachable — election in progress",
                    "description": (
                        "Chaos drill: MongoDB replica set rs0 lost PRIMARY member db-mongo-01. "
                        "replSetGetStatus: PRIMARY member health=0, stateStr=DOWN. "
                        "Election in progress — 2 secondaries visible, quorum: 2/3. "
                        "Replication lag on remaining secondaries: 85s. "
                        "Write operations blocked until new PRIMARY elected. "
                        "This is a synthetic drill — no real MongoDB affected."
                    ),
                },
                "startsAt": _iso(time.time()),
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": "http://prometheus.monitor.svc.cluster.local:9090",
            }
        ],
        "groupLabels": {"alertname": "SysHardFailMongoDBReplicaSetDegraded"},
        "commonLabels": {},
        "commonAnnotations": {},
        "externalURL": "http://alertmanager:9093",
    }


def _hardfail_nfs_payload(trace_id: str) -> dict:
    """Lane 2 — NFS mount stale / unavailable on application servers."""
    return {
        "receiver": "omni-webhook",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "SysHardFailNFSStale",
                    "severity": "critical",
                    "job": "node",
                    "instance": "app-server-02:9100",
                    "host": "app-server-02",
                    "mountpoint": "/mnt/shared-logs",
                    "nfs_server": "nas-01.internal",
                    "chaos_drill": "true",
                    "chaos_lane": "hardfail-nfs",
                    "trace_id": trace_id,
                },
                "annotations": {
                    "summary": "[CHAOS DRILL] NFS /mnt/shared-logs stale on app-server-02",
                    "description": (
                        "Chaos drill: NFS mount /mnt/shared-logs (nas-01.internal:/exports/logs) "
                        "returning ESTALE (Stale file handle) on app-server-02. "
                        "df -h hangs on /mnt/shared-logs (NFS server unreachable). "
                        "Application log writes failing with 'Stale file handle' errno=116. "
                        "nas-01.internal not responding to ping since 08:32 UTC. "
                        "This is a synthetic drill — no real NFS affected."
                    ),
                },
                "startsAt": _iso(time.time()),
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": "http://prometheus.monitor.svc.cluster.local:9090",
            }
        ],
        "groupLabels": {"alertname": "SysHardFailNFSStale"},
        "commonLabels": {},
        "commonAnnotations": {},
        "externalURL": "http://alertmanager:9093",
    }


def _hardfail_dns_payload(trace_id: str) -> dict:
    """Lane 2 — DNS resolution failure across multiple application nodes."""
    return {
        "receiver": "omni-webhook",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "SysHardFailDNSResolutionFailure",
                    "severity": "critical",
                    "job": "blackbox",
                    "instance": "dns-probe-prod",
                    "target_host": "internal-api.svc.cluster.local",
                    "chaos_drill": "true",
                    "chaos_lane": "hardfail-dns",
                    "trace_id": trace_id,
                },
                "annotations": {
                    "summary": "[CHAOS DRILL] DNS resolution failing for internal-api.svc.cluster.local",
                    "description": (
                        "Chaos drill: DNS resolution for internal-api.svc.cluster.local timing out. "
                        "dig +short internal-api.svc.cluster.local — no response after 5s. "
                        "Nameserver 10.96.0.10 (kube-dns) not responding to queries. "
                        "3 app nodes affected: app-01, app-02, app-03. "
                        "Service discovery broken — new connections failing with NXDOMAIN. "
                        "This is a synthetic drill — no real DNS affected."
                    ),
                },
                "startsAt": _iso(time.time()),
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": "http://prometheus.monitor.svc.cluster.local:9090",
            }
        ],
        "groupLabels": {"alertname": "SysHardFailDNSResolutionFailure"},
        "commonLabels": {},
        "commonAnnotations": {},
        "externalURL": "http://alertmanager:9093",
    }


def _hardfail_haproxy_payload(trace_id: str) -> dict:
    """Lane 2 — HAProxy all backends down, traffic blackholed."""
    return {
        "receiver": "omni-webhook",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "SysHardFailHAProxyBackendsDown",
                    "severity": "critical",
                    "job": "haproxy",
                    "instance": "lb-01:8404",
                    "host": "lb-01",
                    "backend": "web-backend",
                    "chaos_drill": "true",
                    "chaos_lane": "hardfail-haproxy",
                    "trace_id": trace_id,
                },
                "annotations": {
                    "summary": "[CHAOS DRILL] HAProxy backend web-backend: 0/5 servers UP on lb-01",
                    "description": (
                        "Chaos drill: HAProxy backend 'web-backend' on lb-01 has 0 of 5 servers UP. "
                        "HAProxy stats: backend web-backend status=DOWN, downtime=8m. "
                        "All 5 health check targets failing: "
                        "app-01:8080, app-02:8080, app-03:8080, app-04:8080, app-05:8080. "
                        "Health check: HTTP 000 (connection refused) after 2s timeout. "
                        "503 error rate: 100% across all incoming traffic. "
                        "This is a synthetic drill — no real HAProxy affected."
                    ),
                },
                "startsAt": _iso(time.time()),
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": "http://prometheus.monitor.svc.cluster.local:9090",
            }
        ],
        "groupLabels": {"alertname": "SysHardFailHAProxyBackendsDown"},
        "commonLabels": {},
        "commonAnnotations": {},
        "externalURL": "http://alertmanager:9093",
    }


_PAYLOAD_BUILDERS = {
    # K8s workload lanes (original)
    "resource": _resource_payload,
    "hardfail": _hardfail_payload,
    "http": _http_payload,
    "siem": _siem_payload,
    # Bare metal OS lanes
    "resource-baremetal": _resource_baremetal_payload,
    "hardfail-systemd": _hardfail_systemd_payload,
    "hardfail-disk": _hardfail_disk_payload,
    "hardfail-swap": _hardfail_swap_payload,
    "hardfail-oom": _hardfail_oom_payload,
    # Database lanes
    "hardfail-mysql": _hardfail_mysql_payload,
    "hardfail-proxysql": _hardfail_proxysql_payload,
    "hardfail-postgresql": _hardfail_postgresql_payload,
    "hardfail-mongodb": _hardfail_mongodb_payload,
    # Network / storage lanes
    "hardfail-nfs": _hardfail_nfs_payload,
    "hardfail-dns": _hardfail_dns_payload,
    "hardfail-haproxy": _hardfail_haproxy_payload,
}


# ── Gateway HTTP helpers ──────────────────────────────────────────────────────

def _headers() -> dict[str, str]:
    h: dict[str, str] = {"Content-Type": "application/json"}
    if GATEWAY_API_KEY:
        h["Authorization"] = f"Bearer {GATEWAY_API_KEY}"
    return h


async def _post_alert(client: httpx.AsyncClient, payload: dict, trace_id: str = "") -> tuple[bool, str]:
    """POST payload to /webhook/prometheus. Returns (success, error)."""
    url = f"{GATEWAY_URL}/webhook/prometheus"
    h = dict(_headers())
    if trace_id:
        h["X-Omni-Trace-Id"] = trace_id
    try:
        resp = await client.post(url, json=payload, headers=h, timeout=10.0)
        if resp.status_code < 300:
            return True, ""
        return False, f"http_{resp.status_code}: {resp.text[:200]}"
    except Exception as exc:
        return False, str(exc)[:200]


async def _check_crat(client: httpx.AsyncClient, trace_id: str) -> bool:
    """Check /crat/export (CSV) for CRAT block with this trace_id."""
    try:
        resp = await client.get(
            f"{GATEWAY_URL}/crat/export",
            headers=_headers(),
            timeout=5.0,
            params={"limit": 50},
        )
        if resp.status_code == 200:
            return trace_id in resp.text
    except Exception:
        pass
    return False


async def _check_advisory(client: httpx.AsyncClient, trace_id: str) -> tuple[str, str]:
    """Poll /crat/export for ADVISORY_DISPATCHED with this trace_id.
    Returns (verdict, action_type) — SUGGEST_REMEDIATION is the expected action when
    OMNI_AUTO_EXECUTE_ENABLED=false.
    """
    try:
        resp = await client.get(
            f"{GATEWAY_URL}/crat/export",
            headers=_headers(),
            timeout=5.0,
            params={"limit": 50},
        )
        if resp.status_code == 200:
            text = resp.text
            if trace_id in text and "ADVISORY_DISPATCHED" in text:
                # Confirmed advisory dispatched — auto-execute=false means suggest
                return "SUGGEST_REMEDIATION", "SUGGEST_REMEDIATION"
            if trace_id in text and "ADVISORY_DECISION" in text:
                # Decision written but not dispatched yet
                return "ADVISORY_DECISION", "SUGGEST_REMEDIATION"
    except Exception:
        pass
    return "", ""


# ── Redis state helpers ───────────────────────────────────────────────────────

_REDIS_SNAPSHOT_KEY = "omni:baseline:snapshot"
_REDIS_SNAPSHOT_TS_KEY = "omni:baseline:ts"


def _redis_exec(cmd: list[str]) -> str:
    """Run redis-cli via kubectl exec in the multi-agent Redis pod."""
    import subprocess
    full = [
        "kubectl", "exec", "-n", "multi-agent", "redis-0", "--",
        "redis-cli",
    ] + cmd
    try:
        result = subprocess.run(full, capture_output=True, text=True, timeout=10)
        return (result.stdout or "").strip()
    except Exception as exc:
        logger.warning("redis_exec failed cmd=%s err=%s", cmd, exc)
        return ""


def _inject_synthetic_z_scores(z_cpu: float = 4.5) -> str | None:
    """Write synthetic z-scores to baseline snapshot. Returns original snapshot or None."""
    original = _redis_exec(["GET", _REDIS_SNAPSHOT_KEY])
    snap = json.dumps({"z_cpu": z_cpu, "z_mem": 0.5, "dr": False})
    _redis_exec(["SET", _REDIS_SNAPSHOT_KEY, snap])
    _redis_exec(["SET", _REDIS_SNAPSHOT_TS_KEY, str(time.time())])
    logger.info("chaos_drill: injected synthetic z_cpu=%.1f into baseline snapshot", z_cpu)
    return original or None


def _restore_z_scores(original: str | None) -> None:
    """Restore baseline snapshot to original value."""
    if original:
        _redis_exec(["SET", _REDIS_SNAPSHOT_KEY, original])
    else:
        _redis_exec(["DEL", _REDIS_SNAPSHOT_KEY])
    _redis_exec(["SET", _REDIS_SNAPSHOT_TS_KEY, str(time.time())])
    logger.info("chaos_drill: restored baseline snapshot")


# ── Drill runner ──────────────────────────────────────────────────────────────

async def run_drill(lane: str, dry_run: bool) -> DrillResult:
    """Run one lane drill. Returns DrillResult."""
    import uuid

    trace_id = f"chaos-drill-{lane}-{uuid.uuid4().hex[:12]}"
    slo_budget = _SLO_SECONDS.get(lane, 120)
    result = DrillResult(lane=lane, dry_run=dry_run)

    builder = _PAYLOAD_BUILDERS.get(lane)
    if not builder:
        result.error = f"Unknown lane: {lane}"
        return result

    payload = builder(trace_id)

    if dry_run:
        result.notes.append(f"[DRY-RUN] Would POST to {GATEWAY_URL}/webhook/prometheus")
        result.notes.append(f"[DRY-RUN] trace_id={trace_id}")
        result.notes.append(f"[DRY-RUN] slo_budget={slo_budget}s")
        result.notes.append(f"[DRY-RUN] payload alertname={payload['alerts'][0]['labels']['alertname']}")
        result.within_slo = True
        return result

    result.injected_at = time.time()
    logger.info("lane=%s injecting fault trace_id=%s", lane, trace_id)

    # Resource lane requires real sigma anomaly in Redis — inject synthetic z-scores.
    _original_snapshot: str | None = None
    if lane in ("resource", "resource-baremetal"):
        _original_snapshot = _inject_synthetic_z_scores(z_cpu=4.5)
        result.notes.append("Injected synthetic z_cpu=4.5 into Redis baseline snapshot")

    async with httpx.AsyncClient() as client:
        ok, err = await _post_alert(client, payload, trace_id=trace_id)
        if not ok:
            result.error = f"injection_failed: {err}"
            result.notes.append(f"Gateway unreachable or rejected: {err}")
            return result

        result.notes.append(f"Injected at {_iso(result.injected_at)} trace_id={trace_id}")
        logger.info("lane=%s injection accepted, polling for advisory...", lane)

        # Poll for advisory detection
        deadline = result.injected_at + slo_budget
        while time.time() < deadline:
            await asyncio.sleep(_POLL_INTERVAL)

            verdict, action = await _check_advisory(client, trace_id)
            if verdict:
                result.detected_at = time.time()
                result.advisory_verdict = verdict
                result.action_type = action
                elapsed = result.detected_at - result.injected_at
                result.within_slo = elapsed <= slo_budget
                result.notes.append(
                    f"Advisory detected after {elapsed:.1f}s: verdict={verdict} action={action}"
                )
                break

            crat_ok = await _check_crat(client, trace_id)
            if crat_ok:
                result.crat_written = True
                result.notes.append("CRAT block found in audit chain")

        if not result.advisory_verdict:
            result.error = f"No advisory detected within {slo_budget}s SLO budget"
            result.notes.append("Timeout: pipeline may be down or trace not propagated")

        # Final CRAT check
        if not result.crat_written:
            result.crat_written = await _check_crat(client, trace_id)

    if _original_snapshot is not None or lane in ("resource", "resource-baremetal"):
        _restore_z_scores(_original_snapshot)
        result.notes.append("Restored Redis baseline snapshot")

    return result


# ── Entry point ───────────────────────────────────────────────────────────────

_K8S_LANES = ["resource", "hardfail", "http", "siem"]
_INFRA_LANES = [
    "resource-baremetal",
    "hardfail-systemd",
    "hardfail-disk",
    "hardfail-swap",
    "hardfail-oom",
    "hardfail-mysql",
    "hardfail-proxysql",
    "hardfail-postgresql",
    "hardfail-mongodb",
    "hardfail-nfs",
    "hardfail-dns",
    "hardfail-haproxy",
]
ALL_LANES = _K8S_LANES + _INFRA_LANES


async def main_async(lanes: list[str], dry_run: bool) -> int:
    results: list[DrillResult] = []

    for lane in lanes:
        print(f"\n{'='*60}")
        print(f"[CHAOS DRILL] lane={lane} dry_run={dry_run}")
        print(f"  Gateway: {GATEWAY_URL}")
        print(f"  SLO budget: {_SLO_SECONDS.get(lane, 120)}s")
        print(f"{'='*60}")

        result = await run_drill(lane, dry_run)
        results.append(result)

        if dry_run:
            print(f"  [DRY-RUN] No injection performed.")
            for note in result.notes:
                print(f"  {note}")
        else:
            status = "PASS" if result.within_slo and not result.error else "FAIL"
            print(f"  Status:         {status}")
            print(f"  Verdict:        {result.advisory_verdict or '(none)'}")
            print(f"  CRAT written:   {result.crat_written}")
            print(f"  Action type:    {result.action_type or '(none)'}")
            print(f"  Elapsed:        {result.elapsed()}s")
            print(f"  Within SLO:     {result.within_slo}")
            if result.error:
                print(f"  Error:          {result.error}")
            for note in result.notes:
                print(f"  Note: {note}")

    # Save results
    ts = time.strftime("%Y%m%d_%H%M%S")
    out_path = Path(f"chaos_drill_results_{ts}.json")
    summary = {
        "timestamp": _iso(time.time()),
        "dry_run": dry_run,
        "gateway_url": GATEWAY_URL,
        "lanes_tested": lanes,
        "results": [r.to_dict() for r in results],
        "overall_pass": all(
            r.within_slo and not r.error for r in results
        ),
    }
    if not dry_run:
        out_path.write_text(json.dumps(summary, indent=2))
        print(f"\nResults saved: {out_path}")

    # Human-readable summary
    print(f"\n{'='*60}")
    print("CHAOS DRILL SUMMARY")
    print(f"{'='*60}")
    all_pass = True
    for r in results:
        ok = r.within_slo and not r.error
        flag = "PASS" if ok else "FAIL"
        print(f"  [{flag}] lane={r.lane} elapsed={r.elapsed()}s verdict={r.advisory_verdict or 'N/A'}")
        if not ok:
            all_pass = False

    print(f"\nOverall: {'PASS' if all_pass else 'FAIL'}")
    return 0 if all_pass else 1


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Chaos drill runner for Omni 4-lane diagnostic system",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    all_lane_names = list(_PAYLOAD_BUILDERS.keys())
    parser.add_argument(
        "--lane",
        choices=["all", "all-infra", "all-k8s"] + all_lane_names,
        default="all",
        help=(
            "Lane to drill. 'all'=all lanes, 'all-infra'=non-K8s only, "
            "'all-k8s'=K8s workload only. (default: all)"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be injected without actually doing it",
    )
    args = parser.parse_args()

    if args.lane == "all":
        lanes = ALL_LANES
    elif args.lane == "all-infra":
        lanes = _INFRA_LANES
    elif args.lane == "all-k8s":
        lanes = _K8S_LANES
    else:
        lanes = [args.lane]
    return asyncio.run(main_async(lanes, args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
