"""Regression test for the gateway admin-pool connect retry (2026-08-03).

Root cause confirmed live: a single failed `create_admin_pool()` attempt at
gateway startup (Postgres not ready yet — same race class as the documented
Kafka producer race for this pod) left `app.state.admin_repo` permanently
`None` for the pod's whole lifetime, causing per-agent-credential auth
(`_resolve_agent_credential`) to 401 every request regardless of how correct
the credential was. `_connect_admin_pool_with_retry` bounds that with retry
+ backoff so a transient startup race self-heals within the same process.
"""
from __future__ import annotations

import pytest

from gateway.api import _connect_admin_pool_with_retry


@pytest.mark.asyncio
async def test_succeeds_immediately_when_first_attempt_works():
    calls = []

    async def _create_pool(settings):
        calls.append(settings)
        return "pool-object"

    result = await _connect_admin_pool_with_retry(
        "postgres://x", backoff_start=0, backoff_max=0, _create_pool=_create_pool,
    )

    assert result == "pool-object"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_retries_past_transient_connection_refused_then_succeeds():
    attempts = {"n": 0}

    async def _create_pool(settings):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionRefusedError("[Errno 111] Connection refused")
        return "pool-object"

    result = await _connect_admin_pool_with_retry(
        "postgres://x", max_attempts=5, backoff_start=0, backoff_max=0, _create_pool=_create_pool,
    )

    assert result == "pool-object"
    assert attempts["n"] == 3


@pytest.mark.asyncio
async def test_gives_up_after_max_attempts_returns_none_without_raising():
    attempts = {"n": 0}

    async def _create_pool(settings):
        attempts["n"] += 1
        raise ConnectionRefusedError("[Errno 111] Connection refused")

    result = await _connect_admin_pool_with_retry(
        "postgres://x", max_attempts=3, backoff_start=0, backoff_max=0, _create_pool=_create_pool,
    )

    assert result is None
    assert attempts["n"] == 3


@pytest.mark.asyncio
async def test_treats_none_return_as_failure_and_retries():
    """create_admin_pool can itself return None (e.g. bad DSN) rather than raise."""
    attempts = {"n": 0}

    async def _create_pool(settings):
        attempts["n"] += 1
        if attempts["n"] < 2:
            return None
        return "pool-object"

    result = await _connect_admin_pool_with_retry(
        "postgres://x", max_attempts=5, backoff_start=0, backoff_max=0, _create_pool=_create_pool,
    )

    assert result == "pool-object"
    assert attempts["n"] == 2
