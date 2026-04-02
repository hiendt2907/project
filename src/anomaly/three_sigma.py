"""Rolling Z-score gate with Redis — every write uses pipeline + EXPIRE (TTL)."""

from __future__ import annotations

import hashlib
import statistics

import redis.asyncio as redis

DEFAULT_KEY_PREFIX = "3sigma:metric:"
DEFAULT_WINDOW = 100
DEFAULT_TTL_SEC = 3600
MIN_STDDEV = 1e-9


def _sanitize_metric_id(metric_id: str) -> str:
    """Avoid Redis key injection / odd chars."""
    safe = "".join(c if c.isalnum() or c in "._-:" else "_" for c in metric_id.strip())
    return safe[:200] or "unknown"


class ThreeSigmaGate:
    """LPUSH + LTRIM + EXPIRE in one pipeline per observation (no orphan keys)."""

    def __init__(
        self,
        r: redis.Redis,
        *,
        window_size: int = DEFAULT_WINDOW,
        ttl_sec: int = DEFAULT_TTL_SEC,
        key_prefix: str = DEFAULT_KEY_PREFIX,
    ) -> None:
        if window_size < 3:
            raise ValueError("window_size must be >= 3 for meaningful stddev")
        if ttl_sec <= 0:
            raise ValueError("ttl_sec must be positive")
        self._r = r
        self._window = window_size
        self._ttl = ttl_sec
        self._prefix = key_prefix

    def _key(self, metric_id: str) -> str:
        return f"{self._prefix}{_sanitize_metric_id(metric_id)}"

    async def observe(self, metric_id: str, value: float) -> tuple[bool, float | None]:
        """
        Record value and return (is_anomaly, z_score or None if not enough data / std=0).
        Anomaly when |z| > 3 and sample window has >= 3 points and std > MIN_STDDEV.
        """
        key = self._key(metric_id)
        pipe = self._r.pipeline()
        pipe.lpush(key, str(value))
        pipe.ltrim(key, 0, self._window - 1)
        pipe.expire(key, self._ttl)
        await pipe.execute()

        raw = await self._r.lrange(key, 0, -1)
        samples = [float(x) for x in raw]
        if len(samples) < 3:
            return False, None

        mean = statistics.fmean(samples)
        # sample std for window (use pstdev for population of window — same list)
        std = statistics.pstdev(samples)
        if std < MIN_STDDEV:
            return False, None

        z = (samples[0] - mean) / std  # newest is LINDEX 0
        is_anomaly = abs(z) > 3.0
        return is_anomaly, z

    async def ttl_for(self, metric_id: str) -> int:
        """TTL seconds remaining (-2 missing, -1 no expire)."""
        return await self._r.ttl(self._key(metric_id))

    async def key_count_estimate(self) -> int:
        """Count keys under prefix (test / debug; SCAN on real Redis)."""
        pattern = f"{self._prefix}*"
        n = 0
        async for _ in self._r.scan_iter(match=pattern, count=100):
            n += 1
        return n


def fingerprint_key_samples(metric_id: str, samples: list[float]) -> str:
    """Stable id for tests (RAM leak / key churn)."""
    h = hashlib.sha256(_sanitize_metric_id(metric_id).encode())
    for s in samples:
        h.update(f"{s:.16f}".encode())
    return h.hexdigest()[:16]
