"""Loop định kỳ cho capacity advisory + báo cáo SRE (G4).

Đọc chuỗi baseline ``3sigma:remote:{tenant}:{host}:{metric}`` do remote agent ghi, chạy
`analyze_capacity`, và publish kết quả vào Redis cho gateway/portal đọc. Báo cáo tenant
được dựng cùng lượt để portal không phải tự tổng hợp lại.

INVARIANT: loop này CHỈ đọc và publish văn bản đề xuất. Nó không gọi executor, không
mutate gì. Mọi thay đổi dung lượng vẫn phải do người duyệt và đi qua executor + tier gate.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

_BASELINE_PATTERN = "3sigma:remote:*"
_ADVICE_KEY = "omni:capacity:advice:{tenant}"
_REPORT_KEY = "omni:report:sre:{tenant}"
_TTL_SEC = 86400 * 2
_INTERVAL_SEC = 3600
_REPORT_PERIOD_DAYS = 7
_MAX_KEYS = 2000  # chặn quét vô hạn nếu key nở ra bất thường


def _parse_baseline_key(key: str) -> tuple[str, str, str] | None:
    """``3sigma:remote:{tenant}:{host}:{metric}`` → (tenant, host, metric)."""
    parts = key.split(":", 4)
    if len(parts) != 5 or parts[0] != "3sigma" or parts[1] != "remote":
        return None
    _, _, tenant, host, metric = parts
    if not (tenant and host and metric):
        return None
    return tenant, host, metric


async def collect_capacity_advice(redis: Any) -> dict[str, list[Any]]:
    """Gom đề xuất capacity theo tenant. Trả ``{tenant: [CapacityAdvice, ...]}``."""
    from pkg.reasoning.capacity_advisor import analyze_capacity

    by_tenant: dict[str, list[Any]] = {}
    seen = 0
    async for key in redis.scan_iter(_BASELINE_PATTERN, count=200):
        seen += 1
        if seen > _MAX_KEYS:
            logger.warning("capacity loop: vượt %d key baseline — cắt bớt", _MAX_KEYS)
            break
        parsed = _parse_baseline_key(key)
        if parsed is None:
            continue
        tenant, host, metric = parsed
        try:
            samples = await redis.lrange(key, 0, -1)
        except Exception as exc:  # noqa: BLE001 — 1 key hỏng không được giết cả lượt
            logger.warning("capacity loop: đọc %s lỗi: %s", key, exc)
            continue
        by_tenant.setdefault(tenant, []).append(
            analyze_capacity(samples=samples, metric=metric, host=host, tenant_id=tenant)
        )
    return by_tenant


async def _count_topology_facts(redis: Any) -> int:
    try:
        res = await redis.execute_command(
            "FT.SEARCH", "idx:infra_topology", "*", "LIMIT", "0", "0"
        )
    except Exception:  # noqa: BLE001
        return 0
    if isinstance(res, dict):
        return int(res.get("total_results") or 0)
    try:
        return int(res[0])
    except (TypeError, ValueError, IndexError):
        return 0


async def capacity_report_loop(ctx: Any, stop: asyncio.Event) -> None:
    """Mỗi giờ: tính capacity advice + dựng báo cáo SRE cho từng tenant có dữ liệu."""
    if not bool(getattr(ctx.settings, "omni_capacity_advisor_enabled", True)):
        logger.info("capacity_report loop disabled by flag")
        return

    from pkg.reasoning.sre_report import build_sre_report
    from workers.kpi_metrics import read_outcome_rates

    repo = None
    pool = getattr(ctx, "admin_pool", None)
    if pool is not None:
        from services.admin_config import AdminConfigRepo

        repo = AdminConfigRepo(pool, redis=ctx.redis)

    while not stop.is_set():
        try:
            by_tenant = await collect_capacity_advice(ctx.redis)
            for tenant, advice in by_tenant.items():
                await ctx.redis.set(
                    _ADVICE_KEY.format(tenant=tenant),
                    json.dumps([a.__dict__ for a in advice], ensure_ascii=False, default=str),
                    ex=_TTL_SEC,
                )

                rates = await read_outcome_rates(ctx.redis, tenant_id=tenant)
                # CỐ Ý đọc CẢ HAI track: báo cáo cho khách phải kể cả kinh nghiệm học từ
                # phán quyết người (advisory), không chỉ playbook đã chạy. Nhưng mỗi hàng
                # mang cột `track` và `build_sre_report` in nhãn "Học từ" — gộp mà KHÔNG
                # nhãn chính là cái làm người đọc tưởng Omni đã tự xử lý được.
                # Khác với tier_loops: ở đó là hạn mức QUYỀN, chỉ đếm track=playbook.
                grads = []
                if repo is not None:
                    try:
                        grads = await repo.list_playbook_graduations(tenant)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("capacity loop: đọc graduation lỗi: %s", exc)

                report = build_sre_report(
                    tenant_id=tenant,
                    period_days=_REPORT_PERIOD_DAYS,
                    rates=rates,
                    graduations=grads,
                    capacity=advice,
                    topology_facts=await _count_topology_facts(ctx.redis),
                )
                await ctx.redis.set(
                    _REPORT_KEY.format(tenant=tenant), report, ex=_TTL_SEC
                )
            logger.info(
                "event=capacity_report_published tenants=%d", len(by_tenant)
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("capacity_report loop error: %s", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=_INTERVAL_SEC)
        except asyncio.TimeoutError:
            pass
