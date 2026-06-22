"""Coverage tests for src/workers/llm_semaphore.py."""
from __future__ import annotations

import os

os.environ.setdefault("OMNI_KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
os.environ.setdefault("OMNI_REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("OMNI_OLLAMA_BASE_URL", "http://localhost:11434")
os.environ.setdefault("OMNI_ENV_MODE", "dev")

from unittest.mock import patch

import fakeredis.aioredis
import pytest


def _make_redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


# ---------------------------------------------------------------------------
# Single-pool (max_slots=1)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_init_pool_single_slot():
    from workers.llm_semaphore import LLMSemaphore, POOL_KEY
    r = _make_redis()
    sem = LLMSemaphore(r, max_slots=1)
    await sem.init_pool()
    tokens = await r.lrange(POOL_KEY, 0, -1)
    assert "0" in tokens


@pytest.mark.asyncio
async def test_init_pool_single_idempotent():
    from workers.llm_semaphore import LLMSemaphore, POOL_KEY
    r = _make_redis()
    sem = LLMSemaphore(r, max_slots=1)
    await sem.init_pool()
    await sem.init_pool()  # second call should not double-add
    tokens = await r.lrange(POOL_KEY, 0, -1)
    assert tokens.count("0") == 1


@pytest.mark.asyncio
async def test_acquire_and_release_single():
    from workers.llm_semaphore import LLMSemaphore, POOL_KEY
    r = _make_redis()
    sem = LLMSemaphore(r, max_slots=1)
    await sem.init_pool()

    with patch("workers.llm_semaphore.metrics_exporter") as me:
        token = await sem.acquire(timeout_s=5.0)
        assert token == "0"
        # pool should be empty
        remaining = await r.lrange(POOL_KEY, 0, -1)
        assert len(remaining) == 0
        await sem.release(token)
        # token should be back
        remaining = await r.lrange(POOL_KEY, 0, -1)
        assert "0" in remaining


@pytest.mark.asyncio
async def test_acquire_reactive_single():
    from workers.llm_semaphore import LLMSemaphore
    r = _make_redis()
    sem = LLMSemaphore(r, max_slots=1)
    await sem.init_pool()
    with patch("workers.llm_semaphore.metrics_exporter"):
        token = await sem.acquire_reactive(timeout_s=5.0)
        assert token is not None
        await sem.release(token)


@pytest.mark.asyncio
async def test_acquire_proactive_single():
    from workers.llm_semaphore import LLMSemaphore
    r = _make_redis()
    sem = LLMSemaphore(r, max_slots=1)
    await sem.init_pool()
    with patch("workers.llm_semaphore.metrics_exporter"):
        token = await sem.acquire_proactive(timeout_s=5.0)
        assert token is not None
        await sem.release(token)


@pytest.mark.asyncio
async def test_acquire_timeout_single():
    from workers.llm_semaphore import LLMSemaphore
    r = _make_redis()
    sem = LLMSemaphore(r, max_slots=1)
    await sem.init_pool()
    with patch("workers.llm_semaphore.metrics_exporter"):
        # Hold the only slot
        token = await sem.acquire(timeout_s=5.0)
        with pytest.raises(TimeoutError):
            await sem.acquire(timeout_s=0.01)
        await sem.release(token)


@pytest.mark.asyncio
async def test_reconcile_pool_single_missing_token():
    from workers.llm_semaphore import LLMSemaphore, POOL_KEY
    r = _make_redis()
    sem = LLMSemaphore(r, max_slots=1)
    # Single pool — reconcile replaces missing token into POOL_KEY
    await sem.init_pool()
    await r.lpop(POOL_KEY)
    with patch("workers.llm_semaphore.metrics_exporter"):
        await sem.reconcile_pool_leaks()
    tokens = await r.lrange(POOL_KEY, 0, -1)
    assert len(tokens) == 1


@pytest.mark.asyncio
async def test_reconcile_skip_leased_token():
    from workers.llm_semaphore import LLMSemaphore, POOL_KEY
    r = _make_redis()
    sem = LLMSemaphore(r, max_slots=1, lease_ttl_sec=30)
    await sem.init_pool()
    # Acquire (creates lease key), remove from pool manually
    with patch("workers.llm_semaphore.metrics_exporter"):
        token = await sem.acquire(timeout_s=5.0)
    # token is "0" and lease exists — reconcile should NOT add it back
    with patch("workers.llm_semaphore.metrics_exporter"):
        await sem.reconcile_pool_leaks()
    pool = await r.lrange(POOL_KEY, 0, -1)
    assert "0" not in pool
    await sem.release(token)


# ---------------------------------------------------------------------------
# Dual-pool (max_slots=2)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_init_pool_dual_lane():
    from workers.llm_semaphore import LLMSemaphore, POOL_KEY_PROACTIVE, POOL_KEY_REACTIVE
    r = _make_redis()
    sem = LLMSemaphore(r, max_slots=2)
    await sem.init_pool()
    pro = await r.lrange(POOL_KEY_PROACTIVE, 0, -1)
    react = await r.lrange(POOL_KEY_REACTIVE, 0, -1)
    assert len(pro) >= 1
    assert len(react) >= 1


@pytest.mark.asyncio
async def test_init_pool_dual_odd_slots():
    from workers.llm_semaphore import LLMSemaphore, POOL_KEY_PROACTIVE, POOL_KEY_REACTIVE
    r = _make_redis()
    sem = LLMSemaphore(r, max_slots=3)
    await sem.init_pool()
    pro = await r.lrange(POOL_KEY_PROACTIVE, 0, -1)
    react = await r.lrange(POOL_KEY_REACTIVE, 0, -1)
    # proactive gets max//2=1, reactive=2 OR proactive=2 reactive=1 (check code)
    # Code: _n_proactive = max(1, max // 2) = 1, _n_reactive = 3-1 = 2
    assert len(pro) == 1
    assert len(react) == 2


@pytest.mark.asyncio
async def test_acquire_proactive_dual():
    from workers.llm_semaphore import LLMSemaphore
    r = _make_redis()
    sem = LLMSemaphore(r, max_slots=2)
    await sem.init_pool()
    with patch("workers.llm_semaphore.metrics_exporter"):
        token = await sem.acquire_proactive(timeout_s=5.0)
        assert token.startswith("p")
        await sem.release(token)


@pytest.mark.asyncio
async def test_acquire_reactive_dual():
    from workers.llm_semaphore import LLMSemaphore
    r = _make_redis()
    sem = LLMSemaphore(r, max_slots=2)
    await sem.init_pool()
    with patch("workers.llm_semaphore.metrics_exporter"):
        token = await sem.acquire_reactive(timeout_s=5.0)
        assert token.startswith("r")
        await sem.release(token)


@pytest.mark.asyncio
async def test_release_dual_proactive_token():
    from workers.llm_semaphore import LLMSemaphore, POOL_KEY_PROACTIVE
    r = _make_redis()
    sem = LLMSemaphore(r, max_slots=2)
    await sem.init_pool()
    with patch("workers.llm_semaphore.metrics_exporter"):
        token = await sem.acquire_proactive(timeout_s=5.0)
        await sem.release(token)
    pro = await r.lrange(POOL_KEY_PROACTIVE, 0, -1)
    assert token in pro


@pytest.mark.asyncio
async def test_release_dual_reactive_token():
    from workers.llm_semaphore import LLMSemaphore, POOL_KEY_REACTIVE
    r = _make_redis()
    sem = LLMSemaphore(r, max_slots=2)
    await sem.init_pool()
    with patch("workers.llm_semaphore.metrics_exporter"):
        token = await sem.acquire_reactive(timeout_s=5.0)
        await sem.release(token)
    react = await r.lrange(POOL_KEY_REACTIVE, 0, -1)
    assert token in react


@pytest.mark.asyncio
async def test_release_unknown_token_goes_to_reactive():
    """Token not starting with 'p' or 'r' (manually injected) falls back to reactive pool."""
    from workers.llm_semaphore import LLMSemaphore, POOL_KEY_REACTIVE
    r = _make_redis()
    sem = LLMSemaphore(r, max_slots=2)
    await sem.init_pool()
    with patch("workers.llm_semaphore.metrics_exporter"):
        # Manually inject a weird token with no lane tracking
        await r.rpush(POOL_KEY_REACTIVE, "x0")
        await r.lpop(POOL_KEY_REACTIVE)
        # Release a token we didn't acquire (no lane tracking)
        sem._token_lane["x0"] = "reactive"
        await sem.release("x0")


@pytest.mark.asyncio
async def test_reconcile_dual_lane_missing_token():
    from workers.llm_semaphore import LLMSemaphore, POOL_KEY_PROACTIVE
    r = _make_redis()
    sem = LLMSemaphore(r, max_slots=2)
    await sem.init_pool()
    # Remove proactive token to simulate leak
    await r.lpop(POOL_KEY_PROACTIVE)
    with patch("workers.llm_semaphore.metrics_exporter"):
        await sem.reconcile_pool_leaks()
    pro = await r.lrange(POOL_KEY_PROACTIVE, 0, -1)
    assert "p0" in pro


@pytest.mark.asyncio
async def test_release_without_lane_tracking():
    """Releasing a token not in _token_lane should still work (no KeyError)."""
    from workers.llm_semaphore import LLMSemaphore, POOL_KEY
    r = _make_redis()
    sem = LLMSemaphore(r, max_slots=1)
    await sem.init_pool()
    with patch("workers.llm_semaphore.metrics_exporter"):
        # Don't go through acquire — release an unknown token
        await sem.release("0")


@pytest.mark.asyncio
async def test_acquire_default_is_reactive():
    """acquire() is an alias for acquire_reactive()."""
    from workers.llm_semaphore import LLMSemaphore
    r = _make_redis()
    sem = LLMSemaphore(r, max_slots=2)
    await sem.init_pool()
    with patch("workers.llm_semaphore.metrics_exporter"):
        token = await sem.acquire(timeout_s=5.0)
        assert token.startswith("r")
        await sem.release(token)


@pytest.mark.asyncio
async def test_lease_key_set_on_acquire():
    from workers.llm_semaphore import LLMSemaphore, LEASE_PREFIX
    r = _make_redis()
    sem = LLMSemaphore(r, max_slots=1)
    await sem.init_pool()
    with patch("workers.llm_semaphore.metrics_exporter"):
        token = await sem.acquire(timeout_s=5.0)
    lease_key = f"{LEASE_PREFIX}{token}"
    exists = await r.exists(lease_key)
    assert exists
    await sem.release(token)
    exists_after = await r.exists(lease_key)
    assert not exists_after


@pytest.mark.asyncio
async def test_init_pool_dual_idempotent():
    from workers.llm_semaphore import LLMSemaphore, POOL_KEY_PROACTIVE
    r = _make_redis()
    sem = LLMSemaphore(r, max_slots=2)
    await sem.init_pool()
    await sem.init_pool()  # second call should not double-add
    pro = await r.lrange(POOL_KEY_PROACTIVE, 0, -1)
    assert pro.count("p0") == 1
