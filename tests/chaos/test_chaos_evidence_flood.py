"""Unit chaos tests — Evidence flood and sigma gate defense.

Verifies that ThreeSigmaGate correctly blocks normal-z traffic under flood
conditions, and only flags true anomalies (|z| > 3.0).
"""

from __future__ import annotations

import fakeredis.aioredis
import pytest

from anomaly.three_sigma import ThreeSigmaGate


async def test_sigma_gate_blocks_normal_flood() -> None:
    """100 normal CPU values (20-25%) → gate blocks all; no false positives."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    gate = ThreeSigmaGate(redis, window_size=50)

    # Simulate 100 normal CPU readings (20-25% range with slight noise)
    normal_values = [
        20.1, 21.5, 19.8, 22.3, 20.7, 21.1, 19.5, 22.8, 20.4, 21.9,
        20.6, 21.2, 19.7, 22.1, 20.9, 21.4, 19.6, 22.5, 20.3, 21.8,
        20.8, 21.0, 19.9, 22.4, 20.2, 21.6, 19.4, 22.7, 20.5, 21.3,
        20.1, 21.5, 19.8, 22.3, 20.7, 21.1, 19.5, 22.8, 20.4, 21.9,
        20.6, 21.2, 19.7, 22.1, 20.9, 21.4, 19.6, 22.5, 20.3, 21.8,
        20.8, 21.0, 19.9, 22.4, 20.2, 21.6, 19.4, 22.7, 20.5, 21.3,
        20.1, 21.5, 19.8, 22.3, 20.7, 21.1, 19.5, 22.8, 20.4, 21.9,
        20.6, 21.2, 19.7, 22.1, 20.9, 21.4, 19.6, 22.5, 20.3, 21.8,
        20.8, 21.0, 19.9, 22.4, 20.2, 21.6, 19.4, 22.7, 20.5, 21.3,
        20.1, 21.5, 19.8, 22.3, 20.7, 21.1, 19.5, 22.8, 20.4, 21.9,
    ]
    false_positives = []
    for v in normal_values:
        anomaly, z = await gate.observe("cpu_flood_test", v)
        if anomaly:
            false_positives.append((v, z))

    assert len(false_positives) == 0, f"False positives detected: {false_positives}"


async def test_sigma_gate_detects_spike_in_flood() -> None:
    """After 50 normal CPU values, a spike at 999% CPU is detected as anomaly."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    gate = ThreeSigmaGate(redis, window_size=50)

    # Warm up gate with stable baseline
    for i in range(50):
        v = 20.0 + (i % 5) * 0.5  # values 20.0-22.0
        await gate.observe("cpu_spike_test", v)

    # Inject a CPU spike — should be flagged
    anomaly, z = await gate.observe("cpu_spike_test", 999.0)

    assert anomaly is True, "Expected CPU spike to be detected as anomaly"
    assert z is not None and abs(z) > 3.0, f"Expected |z| > 3.0, got z={z}"


async def test_sigma_gate_requires_min_3_samples() -> None:
    """Gate returns (False, None) until at least 3 samples are collected."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    gate = ThreeSigmaGate(redis, window_size=10)

    # First 2 samples — insufficient data
    a1, z1 = await gate.observe("min_samples_test", 100.0)
    assert a1 is False and z1 is None

    a2, z2 = await gate.observe("min_samples_test", 200.0)
    assert a2 is False and z2 is None

    # Third sample — now enough data
    a3, z3 = await gate.observe("min_samples_test", 150.0)
    # Not an anomaly (all in similar range), but z is now computed
    assert a3 is False  # not anomalous — these are just test values


async def test_sigma_gate_isolates_per_metric_id() -> None:
    """Different metric IDs do not share state — one flood does not affect another."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    gate = ThreeSigmaGate(redis, window_size=20)

    # Warm up metric A with stable values
    for _ in range(20):
        await gate.observe("metric_a", 50.0)

    # Metric B is fresh (no warmup) — should not flag even on first sample
    a_fresh, _ = await gate.observe("metric_b", 50.0)
    assert a_fresh is False  # insufficient samples for metric_b

    # Metric A should still work independently
    anomaly_a, z_a = await gate.observe("metric_a", 9999.0)
    assert anomaly_a is True and z_a is not None


async def test_sigma_gate_handles_zero_variance() -> None:
    """Constant values produce std=0 → gate returns (False, None) safely, no ZeroDivisionError."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    gate = ThreeSigmaGate(redis, window_size=10)

    # All identical values
    for _ in range(10):
        await gate.observe("const_test", 42.0)

    # Now push the same value again — std=0, gate must not crash
    anomaly, z = await gate.observe("const_test", 42.0)
    # Sàn σ tương đối 2026-07-31: giá trị y hệt baseline ⇒ z=0.0 (không lệch), không
    # còn None mơ hồ. Điểm chốt: KHÔNG bất thường, và không chia-cho-0.
    assert anomaly is False
    assert z in (None, 0.0)


async def test_flood_100_envelopes_sigma_gate_blocks_normal() -> None:
    """Simulate 100 fake evidence envelopes with normal z-scores — verify gate blocks all."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    gate = ThreeSigmaGate(redis, window_size=100)

    # Simulate 100 evidence arrivals with CPU values in normal range
    blocked = 0
    for i in range(100):
        cpu_pct = 15.0 + (i % 10) * 0.8  # 15.0-22.2%, normal operating range
        anomaly, _ = await gate.observe("flood_cpu", cpu_pct)
        if not anomaly:
            blocked += 1

    # All 100 normal-range events should be blocked (not flagged as anomaly)
    assert blocked == 100, f"Expected 100 blocked, got {blocked}"
