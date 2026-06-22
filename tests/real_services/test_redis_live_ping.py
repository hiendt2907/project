"""Live Redis checks — ``redis.asyncio`` only; no mocks or in-memory fakes.

Set ``OMNI_REDIS_URL`` to a reachable Redis (e.g. ``redis://127.0.0.1:6379/0``).
Tests FAIL explicitly when the URL is unset — zero-skip policy.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_redis_ping_live() -> None:
    url = (os.environ.get("OMNI_REDIS_URL") or "").strip()
    if not url:
        pytest.fail(
            "OMNI_REDIS_URL unset — set it to a reachable Redis URL "
            "(e.g. redis://127.0.0.1:6379/0) before running live integration tests"
        )

    import redis.asyncio as redis

    client = redis.from_url(url)
    try:
        pong = await client.ping()
    finally:
        await client.aclose()

    assert pong is True
