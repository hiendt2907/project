"""Unit chaos tests — LLM failure modes.

Tests that when the LLM is unavailable, the health server reports
llm_up=degraded and overall status=degraded.
"""

from __future__ import annotations

import pytest

from workers.health_server import _build_health, update_check_state


async def test_llm_down_sets_degraded_status() -> None:
    """update_check_state("llm_up", "degraded") → healthz reports status=degraded."""
    # Inject LLM failure (mirrors what observability_metrics_loop does when llm_up=0)
    update_check_state("llm_up", "degraded", "llm_up=0")

    health = _build_health()
    assert health["checks"]["llm_up"]["status"] == "degraded", health["checks"]["llm_up"]
    # Overall status must degrade when any check is degraded
    assert health["status"] in ("degraded", "unhealthy"), health["status"]


async def test_llm_recovery_sets_ok_status() -> None:
    """After LLM comes back, update_check_state("llm_up", "ok") → status recovers."""
    # First simulate failure
    update_check_state("llm_up", "degraded", "llm_up=0")
    degraded = _build_health()
    assert degraded["checks"]["llm_up"]["status"] == "degraded"

    # Then simulate recovery
    update_check_state("llm_up", "ok", "llm_up=1")
    recovered = _build_health()
    assert recovered["checks"]["llm_up"]["status"] == "ok", recovered["checks"]["llm_up"]


async def test_redis_down_sets_unhealthy() -> None:
    """Redis ping failure → healthz reports redis_ping=unhealthy and status=unhealthy."""
    update_check_state("redis_ping", "unhealthy", "Connection refused")

    health = _build_health()
    assert health["checks"]["redis_ping"]["status"] == "unhealthy", health["checks"]["redis_ping"]
    assert health["status"] == "unhealthy", health["status"]

    # Restore for other tests
    update_check_state("redis_ping", "ok", "pong")


async def test_multiple_failures_compound_to_unhealthy() -> None:
    """Both LLM degraded and Redis unhealthy → overall status=unhealthy (worst wins)."""
    update_check_state("llm_up", "degraded", "llm_up=0")
    update_check_state("redis_ping", "unhealthy", "timeout")

    health = _build_health()
    assert health["status"] == "unhealthy", health["status"]

    # Restore
    update_check_state("redis_ping", "ok", "pong")
    update_check_state("llm_up", "ok", "llm_up=1")


async def test_kafka_lag_high_sets_unhealthy() -> None:
    """Kafka lag > 1000 → kafka_lag=unhealthy."""
    update_check_state("kafka_lag", "unhealthy", "lag=5000")

    health = _build_health()
    assert health["checks"]["kafka_lag"]["status"] == "unhealthy", health["checks"]["kafka_lag"]

    # Restore
    update_check_state("kafka_lag", "ok", "lag=0")
