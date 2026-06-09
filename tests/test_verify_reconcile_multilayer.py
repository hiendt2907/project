"""Behavioral tests for the multi-layer ground-truth reconciler.

Each infrastructure layer (OS / DB / host-metric / network) must be able to
REFUTE a false claim, CONFIRM a true claim, and return UNVERIFIABLE when no
ground truth is attached (honest gate). We assert on verdicts/behavior, not on
literal evidence strings.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from workers.verify_reconcile import (
    detect_claim_layer,
    reconcile_advisory,
)


def _redis():
    import fakeredis.aioredis

    return fakeredis.aioredis.FakeRedis(decode_responses=True)


def _ctx(*, redis=None, evidence_by_probe=None, tenant="default"):
    return SimpleNamespace(
        redis=redis,
        kafka=None,
        settings=SimpleNamespace(tenant_id=tenant),
        evidence_by_probe=evidence_by_probe,
    )


def _advisory(root_cause: str, affected_workload: str = ""):
    return SimpleNamespace(root_cause=root_cause, affected_workload=affected_workload)


def _probe(probe: str, result: str, fact: dict) -> dict:
    return {"probe": probe, "result": result, "extracted_fact": json.dumps(fact)}


# ---------------------------------------------------------------------------
# Classifier routing
# ---------------------------------------------------------------------------

def test_pod_claim_takes_priority_over_other_layers():
    # mentions "memory" but is a pod OOM claim → must stay on pod layer.
    assert detect_claim_layer("pod nginx OOMKilled exceeds memory limit", "ns/nginx") == "pod"


def test_layer_routing_os_db_host_network():
    assert detect_claim_layer("systemd unit haproxy is down", "host01") == "os"
    assert detect_claim_layer("mysql replication lag on primary", "db01") == "db"
    assert detect_claim_layer("cpu saturation on host", "host01") == "host_metric"
    assert detect_claim_layer("packet loss and high latency on link", "host01") == "network"


# ---------------------------------------------------------------------------
# OS layer
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_os_refute_systemd_passed_contradicts_down_claim():
    ev = {"systemd_units": _probe("systemd_units", "PASSED", {"failed_units": [], "critical_failed_units": []})}
    out = await reconcile_advisory(
        _ctx(evidence_by_probe=ev),
        _advisory("systemd service haproxy is down on host", "host01"),
    )
    assert out.verdict == "refuted"


@pytest.mark.asyncio
async def test_os_confirm_disk_full_failed_probe():
    ev = {"disk_usage": _probe("disk_usage", "FAILED", {"disk_critical_count": 2, "critical_partitions": ["/var"]})}
    out = await reconcile_advisory(
        _ctx(evidence_by_probe=ev),
        _advisory("disk full / no space on /var filesystem", "host01"),
    )
    assert out.verdict == "confirmed"


@pytest.mark.asyncio
async def test_os_unverifiable_no_probe_evidence():
    out = await reconcile_advisory(
        _ctx(evidence_by_probe={}),
        _advisory("systemd unit nginx is down", "host01"),
    )
    assert out.verdict == "unverifiable"


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_db_refute_mysql_passed_contradicts_down_claim():
    ev = {"mysql_health": _probe("mysql_health", "PASSED", {"anomalies": []})}
    out = await reconcile_advisory(
        _ctx(evidence_by_probe=ev),
        _advisory("mysql database down, replication broken", "db01"),
    )
    assert out.verdict == "refuted"


@pytest.mark.asyncio
async def test_db_confirm_proxysql_failed():
    ev = {"proxysql_health": _probe("proxysql_health", "FAILED", {"anomalies": ["too_many_clients"]})}
    out = await reconcile_advisory(
        _ctx(evidence_by_probe=ev),
        _advisory("proxysql connection pool exhausted", "db01"),
    )
    assert out.verdict == "confirmed"


@pytest.mark.asyncio
async def test_db_unverifiable_when_only_unrelated_probe():
    ev = {"disk_usage": _probe("disk_usage", "PASSED", {"disk_critical_count": 0})}
    out = await reconcile_advisory(
        _ctx(evidence_by_probe=ev),
        _advisory("postgresql replication lag exceeds threshold", "db01"),
    )
    assert out.verdict == "unverifiable"


# ---------------------------------------------------------------------------
# Host-metric layer (remote agent + Omni 3σ — NOT Prometheus)
# ---------------------------------------------------------------------------

async def _seed_baseline(redis, tenant, host, suffix, samples):
    from anomaly.three_sigma import ThreeSigmaGate

    gate = ThreeSigmaGate(redis, key_prefix="3sigma:remote:")
    for v in samples:
        await gate.observe(f"{tenant}:{host}:{suffix}", v)


@pytest.mark.asyncio
async def test_host_metric_refute_within_3sigma_baseline():
    redis = _redis()
    host = "guest-web-1"
    # Tight baseline around ~70% CPU; a 72% reading is normal for THIS host.
    await _seed_baseline(redis, "default", host, "cpu", [70, 71, 69, 70, 72, 71, 70, 69, 70, 71])
    ev = {"remote_system_metrics": {
        "probe": "remote_system_metrics", "result": "FAILED",
        "namespace": host, "extracted_fact": json.dumps({"cpu_percent": 72.0}),
    }}
    out = await reconcile_advisory(
        _ctx(redis=redis, evidence_by_probe=ev),
        _advisory("cpu saturation on guest host", host),
    )
    assert out.verdict == "refuted"


@pytest.mark.asyncio
async def test_host_metric_confirm_outside_3sigma_baseline():
    redis = _redis()
    host = "guest-web-2"
    await _seed_baseline(redis, "default", host, "cpu", [10, 11, 9, 10, 12, 11, 10, 9, 10, 11])
    # The CURRENT (newest) sample drives z; push a huge spike as newest.
    from anomaly.three_sigma import ThreeSigmaGate
    gate = ThreeSigmaGate(redis, key_prefix="3sigma:remote:")
    await gate.observe(f"default:{host}:cpu", 99.0)
    ev = {"remote_system_metrics": {
        "probe": "remote_system_metrics", "result": "FAILED",
        "namespace": host, "extracted_fact": json.dumps({"cpu_percent": 99.0}),
    }}
    out = await reconcile_advisory(
        _ctx(redis=redis, evidence_by_probe=ev),
        _advisory("cpu saturation on guest host", host),
    )
    assert out.verdict == "confirmed"


@pytest.mark.asyncio
async def test_host_metric_unverifiable_no_baseline_no_payload():
    redis = _redis()
    out = await reconcile_advisory(
        _ctx(redis=redis, evidence_by_probe={}),
        _advisory("memory saturation on guest host", "guest-x"),
    )
    assert out.verdict == "unverifiable"


# ---------------------------------------------------------------------------
# Network layer
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_network_refute_interfaces_up():
    ev = {"network_interfaces": _probe(
        "network_interfaces", "PASSED", {"down_interfaces": [], "error_interfaces": []}
    )}
    out = await reconcile_advisory(
        _ctx(evidence_by_probe=ev),
        _advisory("network interface down / link down on host", "host01"),
    )
    assert out.verdict == "refuted"


@pytest.mark.asyncio
async def test_network_confirm_tcp_saturation():
    ev = {"tcp_connections": _probe(
        "tcp_connections", "FAILED", {"time_wait_excess": True, "syn_flood_indicator": False}
    )}
    out = await reconcile_advisory(
        _ctx(evidence_by_probe=ev),
        _advisory("tcp connection saturation, time_wait excess", "host01"),
    )
    assert out.verdict == "confirmed"


@pytest.mark.asyncio
async def test_network_unverifiable_when_blind():
    # packet-loss claim but no network probe attached → cannot observe.
    out = await reconcile_advisory(
        _ctx(evidence_by_probe={}),
        _advisory("packet loss and high latency on uplink", "host01"),
    )
    assert out.verdict == "unverifiable"


# ---------------------------------------------------------------------------
# Pod path regression — unchanged behavior, no probe evidence needed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pod_path_unverifiable_when_no_signal():
    out = await reconcile_advisory(_ctx(), _advisory("generic incident", "ns/app"))
    assert out.verdict == "unverifiable"
