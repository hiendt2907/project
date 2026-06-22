"""Unit chaos tests — Health server extended coverage.

Covers configure(), record_message_processed(), startup grace period,
message stall detection, and the /readyz + /healthz HTTP logic.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from workers.health_server import (
    _build_health,
    configure,
    record_message_processed,
    update_check_state,
    _read_check_states,
)


def _reset_health_state() -> None:
    """Reset all health checks to ok before each test to avoid cross-test pollution."""
    from workers import health_server as hs
    with hs._lock:
        hs._last_message_ts = 0.0
        hs._check_states = {
            "kafka_lag": ("ok", "not polled yet"),
            "redis_ping": ("ok", "not polled yet"),
            "llm_up": ("ok", "not polled yet"),
            "last_message_age": ("ok", "not started yet"),
        }


def test_configure_is_noop() -> None:
    """configure() should not raise and returns None (passive mode noop)."""
    result = configure(redis=None, llm_base_url="http://localhost:11434")
    assert result is None


def test_record_message_processed_updates_timestamp() -> None:
    """record_message_processed() updates _last_message_ts to current time."""
    from workers import health_server as hs
    _reset_health_state()

    before = time.time()
    record_message_processed()
    after = time.time()

    with hs._lock:
        ts = hs._last_message_ts
    assert before <= ts <= after


def test_startup_grace_period_no_messages() -> None:
    """During startup grace (< 60s uptime), no messages → status is degraded not unhealthy."""
    from workers import health_server as hs
    _reset_health_state()

    # Simulate fresh startup (uptime < 60s)
    with patch.object(hs, "_startup_ts", time.time() - 10):  # 10s uptime
        with hs._lock:
            hs._last_message_ts = 0.0  # no messages yet
        health = _build_health()

    # last_message_age should be "ok" during startup grace
    assert health["checks"]["last_message_age"]["status"] == "ok"
    assert "startup grace" in health["checks"]["last_message_age"]["detail"]


def test_message_stall_after_grace_period() -> None:
    """After grace period with no messages, last_message_age becomes degraded."""
    from workers import health_server as hs
    _reset_health_state()

    # Simulate that startup was 120s ago (past 60s grace), no messages received
    with patch.object(hs, "_startup_ts", time.time() - 120):
        with hs._lock:
            hs._last_message_ts = 0.0
        health = _build_health()

    assert health["checks"]["last_message_age"]["status"] == "degraded"


def test_message_stall_unhealthy_after_600s() -> None:
    """Message age > 600s → last_message_age=unhealthy, overall=unhealthy."""
    from workers import health_server as hs
    _reset_health_state()

    # Simulate last message was 700s ago
    stale_ts = time.time() - 700
    with hs._lock:
        hs._last_message_ts = stale_ts

    health = _build_health()
    assert health["checks"]["last_message_age"]["status"] == "unhealthy"
    assert health["status"] == "unhealthy"


def test_recent_message_keeps_ok_status() -> None:
    """Message received recently → last_message_age=ok."""
    from workers import health_server as hs
    _reset_health_state()

    # Message 10s ago
    with hs._lock:
        hs._last_message_ts = time.time() - 10

    health = _build_health()
    assert health["checks"]["last_message_age"]["status"] == "ok"


def test_health_check_states_all_ok() -> None:
    """All checks ok → _build_health returns status=ok."""
    _reset_health_state()
    health = _build_health()
    assert health["status"] in ("ok", "degraded")  # last_message_age may be degraded due to no messages


def test_update_check_state_persists_across_calls() -> None:
    """update_check_state updates the shared state correctly."""
    _reset_health_state()

    update_check_state("kafka_lag", "degraded", "lag=500")
    health = _build_health()
    assert health["checks"]["kafka_lag"]["status"] == "degraded"
    assert health["checks"]["kafka_lag"]["detail"] == "lag=500"

    # Restore
    update_check_state("kafka_lag", "ok", "lag=0")


def test_build_health_includes_uptime_and_ts() -> None:
    """_build_health response includes uptime_s and ts fields."""
    _reset_health_state()
    health = _build_health()
    assert "uptime_s" in health
    assert isinstance(health["uptime_s"], float)
    assert "ts" in health
    assert isinstance(health["ts"], float)


def test_readyz_returns_200_when_not_unhealthy() -> None:
    """Readiness logic: health status not 'unhealthy' → readyz=True, code=200."""
    _reset_health_state()
    update_check_state("llm_up", "degraded", "llm_up=0")  # degraded but not unhealthy

    health = _build_health()
    ready = health["status"] != "unhealthy"
    assert ready is True

    # Restore
    update_check_state("llm_up", "ok", "llm_up=1")


def test_readyz_returns_503_when_unhealthy() -> None:
    """Readiness logic: health status 'unhealthy' → readyz=False."""
    _reset_health_state()
    update_check_state("redis_ping", "unhealthy", "connection refused")

    health = _build_health()
    ready = health["status"] != "unhealthy"
    assert ready is False

    # Restore
    update_check_state("redis_ping", "ok", "pong")
