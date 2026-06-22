"""Unit chaos tests — Extended ThreeSigmaGate coverage.

Covers observe_adaptive (maintenance window + per-workload config),
get_z_score, ttl_for, key_count_estimate, fingerprint_key_samples,
and constructor validation.

Marker legend:
  @pytest.mark.inverted_logic   — synthetic condition, tests logic path only
  @pytest.mark.real_condition   — real failure injected via FakeRedis
  @pytest.mark.business_logic_only — kiểm tra logic, không phải infra path
"""

from __future__ import annotations

import fakeredis.aioredis
import pytest

from anomaly.three_sigma import ThreeSigmaGate, fingerprint_key_samples


async def _warm_gate(gate: ThreeSigmaGate, metric_id: str, n: int = 20, base: float = 50.0) -> None:
    for i in range(n):
        await gate.observe(metric_id, base + (i % 5) * 0.3)


async def test_observe_adaptive_maintenance_window_suppresses() -> None:
    """observe_adaptive returns (False, None) during maintenance window."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    gate = ThreeSigmaGate(redis, window_size=20)

    await _warm_gate(gate, "cpu_maint", n=20)

    # Set maintenance window key
    await redis.set("omni:maint:multi-agent:nginx-lab", "1")

    # Even a huge spike is suppressed during maintenance
    anomaly, z = await gate.observe_adaptive(
        "cpu_maint",
        9999.0,
        namespace="multi-agent",
        deployment="nginx-lab",
    )
    assert anomaly is False
    assert z is None


async def test_observe_adaptive_custom_threshold_from_config() -> None:
    """observe_adaptive reads per-workload threshold from Redis config key."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    gate = ThreeSigmaGate(redis, window_size=20)

    await _warm_gate(gate, "cpu_cfg", n=20, base=50.0)

    # Set a tighter threshold: 1.0 sigma (will flag 2-sigma deviations)
    cfg_key = "omni:sigma:config:multi-agent:nginx-lab"
    await redis.hset(cfg_key, mapping={"threshold": "1.0", "window": "20"})

    # A moderate deviation (2-sigma) should be flagged with threshold=1.0
    # Inject a value clearly beyond 1 sigma from mean
    anomaly, z = await gate.observe_adaptive(
        "cpu_cfg",
        100.0,  # far from 50.0 baseline
        namespace="multi-agent",
        deployment="nginx-lab",
    )
    assert anomaly is True
    assert z is not None and abs(z) > 1.0


async def test_observe_adaptive_missing_config_uses_default() -> None:
    """observe_adaptive with no config key falls back to default threshold (3.0)."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    gate = ThreeSigmaGate(redis, window_size=20)

    await _warm_gate(gate, "cpu_noconfig", n=20)

    # No config key set — default 3.0 threshold applies
    # A moderate spike (<3 sigma) should NOT be flagged
    anomaly, z = await gate.observe_adaptive(
        "cpu_noconfig",
        51.5,  # tiny deviation within 3 sigma
        namespace="multi-agent",
        deployment="nginx-lab",
    )
    assert anomaly is False


async def test_observe_adaptive_without_namespace_skips_config_check() -> None:
    """observe_adaptive with no namespace/deployment skips both maint + config checks."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    gate = ThreeSigmaGate(redis, window_size=20)

    await _warm_gate(gate, "cpu_nons", n=20)

    # Should behave same as observe()
    anomaly, z = await gate.observe_adaptive("cpu_nons", 9999.0)
    assert anomaly is True


async def test_get_z_score_returns_none_on_empty() -> None:
    """get_z_score returns None when no data is stored."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    gate = ThreeSigmaGate(redis, window_size=10)

    z = await gate.get_z_score("fresh_metric")
    assert z is None


async def test_get_z_score_returns_score_after_warmup() -> None:
    """get_z_score returns float after enough observations."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    gate = ThreeSigmaGate(redis, window_size=20)

    # All identical values → std=0 → returns None.
    # Use two distinct values so std > 0 but range is small.
    for _ in range(10):
        await gate.observe("stable_cpu", 50.0)
    for _ in range(10):
        await gate.observe("stable_cpu", 50.1)

    z = await gate.get_z_score("stable_cpu")
    # With near-constant values, z is computed and must be a float
    assert z is not None
    assert isinstance(z, float)


async def test_ttl_for_returns_positive_after_observe() -> None:
    """ttl_for returns a positive TTL after at least one observation."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    gate = ThreeSigmaGate(redis, window_size=10, ttl_sec=3600)

    await gate.observe("ttl_test_metric", 42.0)

    ttl = await gate.ttl_for("ttl_test_metric")
    assert ttl > 0, f"Expected positive TTL, got {ttl}"
    assert ttl <= 3600


async def test_ttl_for_returns_negative_for_missing_key() -> None:
    """ttl_for returns -2 (key doesn't exist) for unknown metric."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    gate = ThreeSigmaGate(redis, window_size=10)

    ttl = await gate.ttl_for("nonexistent_metric")
    assert ttl == -2  # Redis convention: -2 = key doesn't exist


async def test_key_count_estimate_empty() -> None:
    """key_count_estimate returns 0 on fresh Redis."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    gate = ThreeSigmaGate(redis, window_size=10)

    count = await gate.key_count_estimate()
    assert count == 0


async def test_key_count_estimate_after_observations() -> None:
    """key_count_estimate counts metrics that have been observed."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    gate = ThreeSigmaGate(redis, window_size=10)

    await gate.observe("metric_alpha", 1.0)
    await gate.observe("metric_beta", 2.0)
    await gate.observe("metric_gamma", 3.0)

    count = await gate.key_count_estimate()
    assert count == 3


def test_fingerprint_key_samples_deterministic() -> None:
    """fingerprint_key_samples produces the same result for the same input."""
    samples = [1.0, 2.0, 3.0]
    h1 = fingerprint_key_samples("cpu", samples)
    h2 = fingerprint_key_samples("cpu", samples)
    assert h1 == h2
    assert len(h1) == 16


def test_fingerprint_key_samples_different_for_different_inputs() -> None:
    """fingerprint_key_samples produces different hashes for different inputs."""
    h1 = fingerprint_key_samples("cpu", [1.0, 2.0, 3.0])
    h2 = fingerprint_key_samples("cpu", [1.0, 2.0, 4.0])
    h3 = fingerprint_key_samples("mem", [1.0, 2.0, 3.0])
    assert h1 != h2
    assert h1 != h3


def test_three_sigma_gate_rejects_small_window() -> None:
    """ThreeSigmaGate raises ValueError for window_size < 3."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    with pytest.raises(ValueError, match="window_size"):
        ThreeSigmaGate(redis, window_size=2)


def test_three_sigma_gate_rejects_zero_ttl() -> None:
    """ThreeSigmaGate raises ValueError for ttl_sec <= 0."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    with pytest.raises(ValueError, match="ttl_sec"):
        ThreeSigmaGate(redis, window_size=10, ttl_sec=0)
