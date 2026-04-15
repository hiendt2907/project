"""Redis LIST pool + lease keys — matches ``llm_num_parallel`` / OMNI_LLM_NUM_PARALLEL.

When ``max_slots > 1``, tokens are split into **proactive** and **reactive** lanes
(half each, rounded: proactive gets the extra slot when odd) so incident traffic
does not starve behind chat FIFO.
"""

from __future__ import annotations

import asyncio
import logging
import time

import redis.asyncio as redis

from workers import metrics_exporter

logger = logging.getLogger(__name__)

POOL_KEY = "omni:semaphore:{llm}:pool"
POOL_KEY_PROACTIVE = "omni:semaphore:{llm}:pool:proactive"
POOL_KEY_REACTIVE = "omni:semaphore:{llm}:pool:reactive"
LEASE_PREFIX = "omni:semaphore:{llm}:lease:"


class LLMSemaphore:
    """LPOP / RPUSH token pool; lease key TTL khi giữ slot (crash → reconcile bổ sung)."""

    def __init__(
        self,
        r: redis.Redis,
        *,
        max_slots: int,
        lease_ttl_sec: int = 120,
    ) -> None:
        self._r = r
        self._max = max(1, max_slots)
        self._lease_ttl = lease_ttl_sec
        self._split = self._max > 1
        self._token_lane: dict[str, str] = {}
        if self._split:
            self._n_proactive = max(1, self._max // 2)
            self._n_reactive = self._max - self._n_proactive

    def _lease_key(self, token: str) -> str:
        return f"{LEASE_PREFIX}{token}"

    async def init_pool(self) -> None:
        """Đảm bảo đủ token trong pool (idempotent)."""
        if not self._split:
            pool = set(await self._r.lrange(POOL_KEY, 0, -1))
            for i in range(self._max):
                t = str(i)
                if t not in pool and not await self._r.exists(self._lease_key(t)):
                    await self._r.rpush(POOL_KEY, t)
            return
        await self._init_lane(POOL_KEY_PROACTIVE, "p", self._n_proactive)
        await self._init_lane(POOL_KEY_REACTIVE, "r", self._n_reactive)
        logger.info(
            "llm_semaphore dual_lane init proactive_slots=%s reactive_slots=%s total=%s",
            self._n_proactive,
            self._n_reactive,
            self._max,
        )

    async def _init_lane(self, pool_key: str, prefix: str, n: int) -> None:
        pool = set(await self._r.lrange(pool_key, 0, -1))
        for i in range(n):
            t = f"{prefix}{i}"
            if t not in pool and not await self._r.exists(self._lease_key(t)):
                await self._r.rpush(pool_key, t)

    async def reconcile_pool_leaks(self) -> None:
        """Bổ sung token thiếu khi pool + lease < max."""
        if not self._split:
            pool = await self._r.lrange(POOL_KEY, 0, -1)
            pool_set = {x.decode() if isinstance(x, bytes) else x for x in pool}
            for i in range(self._max):
                t = str(i)
                if t in pool_set:
                    continue
                if await self._r.exists(self._lease_key(t)):
                    continue
                await self._r.rpush(POOL_KEY, t)
                logger.debug("reconciled missing token %s into pool", t)
            return
        await self._reconcile_lane(POOL_KEY_PROACTIVE, "p", self._n_proactive)
        await self._reconcile_lane(POOL_KEY_REACTIVE, "r", self._n_reactive)

    async def _reconcile_lane(self, pool_key: str, prefix: str, n: int) -> None:
        pool = await self._r.lrange(pool_key, 0, -1)
        pool_set = {x.decode() if isinstance(x, bytes) else x for x in pool}
        for i in range(n):
            t = f"{prefix}{i}"
            if t in pool_set:
                continue
            if await self._r.exists(self._lease_key(t)):
                continue
            await self._r.rpush(pool_key, t)
            logger.debug("reconciled missing token %s into pool %s", t, pool_key)

    async def acquire(self, timeout_s: float = 120.0) -> str:
        """Reactive/chat lane (default)."""
        return await self.acquire_reactive(timeout_s)

    async def acquire_reactive(self, timeout_s: float = 120.0) -> str:
        if not self._split:
            return await self._acquire_single_pool(timeout_s, "reactive")
        return await self._acquire_from_pool(POOL_KEY_REACTIVE, timeout_s, "reactive")

    async def acquire_proactive(self, timeout_s: float = 120.0) -> str:
        if not self._split:
            return await self._acquire_single_pool(timeout_s, "proactive")
        return await self._acquire_from_pool(POOL_KEY_PROACTIVE, timeout_s, "proactive")

    async def _acquire_single_pool(self, timeout_s: float, lane: str) -> str:
        deadline = time.monotonic() + timeout_s
        while True:
            await self.reconcile_pool_leaks()
            raw = await self._r.lpop(POOL_KEY)
            if raw is not None:
                token = raw.decode() if isinstance(raw, bytes) else raw
                await self._r.set(self._lease_key(token), "1", ex=self._lease_ttl)
                self._token_lane[token] = lane
                metrics_exporter.ollama_semaphore_inc(lane)
                return token
            if time.monotonic() >= deadline:
                raise TimeoutError("llm semaphore acquire timeout")
            await asyncio.sleep(0.05)

    async def _acquire_from_pool(self, pool_key: str, timeout_s: float, lane: str) -> str:
        deadline = time.monotonic() + timeout_s
        while True:
            await self.reconcile_pool_leaks()
            raw = await self._r.lpop(pool_key)
            if raw is not None:
                token = raw.decode() if isinstance(raw, bytes) else raw
                await self._r.set(self._lease_key(token), "1", ex=self._lease_ttl)
                self._token_lane[token] = lane
                metrics_exporter.ollama_semaphore_inc(lane)
                return token
            if time.monotonic() >= deadline:
                raise TimeoutError("llm semaphore acquire timeout")
            await asyncio.sleep(0.05)

    async def release(self, token: str) -> None:
        lane = self._token_lane.pop(token, None)
        if lane:
            metrics_exporter.ollama_semaphore_dec(lane)
        try:
            await self._r.delete(self._lease_key(token))
            if not self._split:
                await self._r.rpush(POOL_KEY, token)
            elif token.startswith("p"):
                await self._r.rpush(POOL_KEY_PROACTIVE, token)
            elif token.startswith("r"):
                await self._r.rpush(POOL_KEY_REACTIVE, token)
            else:
                await self._r.rpush(POOL_KEY_REACTIVE, token)
        finally:
            pass
