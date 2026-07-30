"""Rolling Z-score gate with Redis — every write uses pipeline + EXPIRE (TTL).

S3.2 additions:
  - Per-workload threshold/window from Redis key omni:sigma:config:{ns}:{dep}
  - Maintenance window suppression via omni:maint:{ns}:{dep}
  - observe_adaptive() method for workload-specific anomaly detection

Tenant-isolation audit (onboarding-ops-agent plan, step 1): ``{namespace}``/
``{deployment}`` here refer to the in-cluster K8s namespace Omni itself
monitors (Lane-1 resource baseline on the lab cluster) — not a customer
tenant boundary, so no ``tenant_id`` belongs in this key. Customer-host
baselines are a separate module (``anomaly/remote_host_baseline.py``,
key ``3sigma:remote:{tenant_id}:{host}:{cpu|mem|disk}``) which is already
tenant-scoped.
"""

from __future__ import annotations

import hashlib
import json
import logging
import statistics
from typing import Any

import redis.asyncio as redis

logger = logging.getLogger(__name__)

DEFAULT_KEY_PREFIX = "3sigma:metric:"
DEFAULT_WINDOW = 100
DEFAULT_TTL_SEC = 3600
MIN_STDDEV = 1e-9
DEFAULT_THRESHOLD = 3.0
# Số mẫu LỊCH SỬ tối thiểu (không kể mẫu đang chấm) trước khi z-score có nghĩa. Trước
# 2026-07-31 gate cold-start là hệ quả TÌNH CỜ của trần √(n-1); nay là điều kiện tường
# minh — dưới ngưỡng này baseline chưa đủ để ước lượng σ đáng tin.
_MIN_BASELINE = 8
# Sàn độ lệch chuẩn tương đối = 1% giá trị trung bình. Chống cả σ=0 (baseline phẳng
# tuyệt đối) lẫn σ cực nhỏ (host im lìm) làm z bung vì nhiễu vụn.
_REL_STD_FLOOR = 0.01

_SIGMA_CONFIG_KEY_FMT = "omni:sigma:config:{namespace}:{deployment}"
_MAINT_KEY_FMT = "omni:maint:{namespace}:{deployment}"


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
        """Record value and return (is_anomaly, z_score or None if not enough data / std=0).

        Anomaly when |z| > DEFAULT_THRESHOLD and sample window has >= 3 points and std > MIN_STDDEV.
        """
        return await self._observe_impl(metric_id, value, threshold=DEFAULT_THRESHOLD, window=self._window)

    async def _observe_impl(
        self,
        metric_id: str,
        value: float,
        threshold: float,
        window: int,
    ) -> tuple[bool, float | None]:
        key = self._key(metric_id)
        pipe = self._r.pipeline()
        pipe.lpush(key, str(value))
        pipe.ltrim(key, 0, window - 1)
        pipe.expire(key, self._ttl)
        await pipe.execute()

        raw = await self._r.lrange(key, 0, -1)
        samples = [float(x) for x in raw]
        # Mẫu mới nhất (LINDEX 0) là mẫu ĐANG chấm. Tính mean/std trên phần LỊCH SỬ còn
        # lại, KHÔNG gồm chính nó. Nếu gồm (lỗi cũ), z bị chặn cứng ở √(n-1) bất kể lệch
        # bao nhiêu — n≤10 thì z không thể >3, và một sự cố giữ mức cao tự nhập vào
        # baseline sau ~6 mẫu rồi hết "bất thường". Đã chứng minh bằng đại số 2026-07-31.
        current = samples[0]
        baseline = samples[1:]
        if len(baseline) < _MIN_BASELINE:
            logger.debug(
                "3sigma: cold_start metric=%s baseline=%d need=%d",
                metric_id, len(baseline), _MIN_BASELINE,
            )
            return False, None

        mean = statistics.fmean(baseline)
        std = statistics.pstdev(baseline)
        # Sàn σ TƯƠNG ĐỐI (2026-07-31). Hai mục đích:
        #  1. Baseline phẳng tuyệt đối (σ=0) không còn trả None câm — một thay đổi thật
        #     vẫn phát hiện được (trước đây MIN_STDDEV=1e-9 chỉ chặn σ đúng bằng 0).
        #  2. Baseline gần phẳng (σ cực nhỏ) không làm z bung vì nhiễu vụn — một host
        #     im lìm không biến mọi dao động 0.1% thành "bất thường".
        # Sàn = 1% của |mean|, tối thiểu MIN_STDDEV. Tầng remote còn chồng sàn BIÊN ĐỘ
        # tuyệt đối (chỉ báo khi giá trị đủ cao) — hai lớp bổ sung nhau.
        std = max(std, abs(mean) * _REL_STD_FLOOR, MIN_STDDEV)

        z = (current - mean) / std
        is_anomaly = abs(z) > threshold
        return is_anomaly, z

    async def observe_adaptive(
        self,
        metric_id: str,
        value: float,
        *,
        namespace: str = "",
        deployment: str = "",
    ) -> tuple[bool, float | None]:
        """S3.2: observe with per-workload config + maintenance window suppression."""
        # Maintenance window check — suppress anomaly detection during planned maintenance.
        if namespace and deployment:
            try:
                maint_key = _MAINT_KEY_FMT.format(namespace=namespace, deployment=deployment)
                is_maint = await self._r.exists(maint_key)
                if is_maint:
                    return False, None
            except Exception as exc:
                logger.warning("observe_adaptive: maint_check failed ns=%s dep=%s err=%r — fail closed (treating as maint active)", namespace, deployment, exc)
                return False, None  # fail closed: Redis error during maint check = suppress anomaly

        # Load per-workload sigma config.
        threshold = DEFAULT_THRESHOLD
        window = self._window
        if namespace and deployment:
            try:
                cfg_key = _SIGMA_CONFIG_KEY_FMT.format(namespace=namespace, deployment=deployment)
                cfg = await self._r.hgetall(cfg_key)
                if cfg:
                    raw_thr = cfg.get(b"threshold") or cfg.get("threshold")
                    raw_win = cfg.get(b"window") or cfg.get("window")
                    if raw_thr:
                        threshold = float(raw_thr)
                    if raw_win:
                        window = max(3, int(raw_win))
            except Exception as exc:
                logger.warning("observe_adaptive: config load failed ns=%s dep=%s err=%r", namespace, deployment, exc)

        return await self._observe_impl(metric_id, value, threshold=threshold, window=window)

    async def get_z_score(self, metric_id: str) -> float | None:
        """Read-only: compute z-score from current rolling window without writing.

        Used as fallback when Prometheus recording rules are absent.
        Returns None if window has < 3 samples or std ≈ 0.
        """
        key = self._key(metric_id)
        raw = await self._r.lrange(key, 0, self._window - 1)
        if not raw:
            return None
        samples = [float(x) for x in raw]
        if len(samples) < 3:
            return None
        mean = statistics.fmean(samples)
        std = statistics.pstdev(samples)
        if std < MIN_STDDEV:
            return None
        return (samples[0] - mean) / std

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
