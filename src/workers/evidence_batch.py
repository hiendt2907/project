"""Gom evidence theo trace_id trên Redis — một lần LLM / một lần ping."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

AGG_TIMEOUT_SEC = 3.0
BATCH_TTL = 120
FLUSH_LOCK_TTL = 30

REDIS_BATCH = "omni:diag_batch:{trace}"
REDIS_T0 = "omni:diag_batch_t0:{trace}"
REDIS_LOCK = "omni:diag_flush_lock:{trace}"


def _decode(b: Any) -> str:
    if isinstance(b, bytes):
        return b.decode("utf-8", errors="replace")
    return str(b)


async def append_evidence_and_take_flush_batch(
    redis: Any,
    trace: str,
    ev_doc: dict[str, Any],
) -> list[dict[str, Any]] | None:
    """
    Thêm một probe vào batch. Trả về **list** các evidence dict khi **caller** là người flush
    (đủ điều kiện + giữ lock); **None** nếu còn chờ probe khác hoặc worker khác đang flush.
    """
    probe = str(ev_doc.get("probe") or "unknown")
    raw_json = json.dumps(ev_doc, ensure_ascii=False)
    hk = REDIS_BATCH.format(trace=trace)
    tk = REDIS_T0.format(trace=trace)
    lk = REDIS_LOCK.format(trace=trace)

    await redis.hset(hk, probe, raw_json)
    await redis.expire(hk, BATCH_TTL)

    t0s = await redis.get(tk)
    if not t0s:
        await redis.set(tk, str(time.time()), ex=BATCH_TTL, nx=True)
        t0s = await redis.get(tk)
    try:
        t0 = float(_decode(t0s) if t0s else time.time())
    except (TypeError, ValueError):
        t0 = time.time()

    elapsed = time.time() - t0
    keys_raw = await redis.hkeys(hk)
    keys = {_decode(x) for x in (keys_raw or [])}

    sym = str(ev_doc.get("symptom_group") or "").strip()
    workload = sym == "workload_resource"

    ready = False
    if workload:
        ready = (
            "k8s_clinical_pod_status" in keys and "k8s_clinical_pod_metrics" in keys
        ) or elapsed >= AGG_TIMEOUT_SEC
    else:
        # Matrix / khác: gom tối đa 3s hoặc ≥2 probe (thường redis+kafka)
        ready = elapsed >= AGG_TIMEOUT_SEC or len(keys) >= 2

    if not ready:
        return None

    got_lock = await redis.set(lk, "1", nx=True, ex=FLUSH_LOCK_TTL)
    if not got_lock:
        logger.info(
            "event=diag_batch_flush_wait trace_id_in_ctx skip=other_worker_flushing probes=%s",
            sorted(keys),
        )
        return None

    try:
        blob = await redis.hgetall(hk)
        for kdel in (hk, tk, lk):
            try:
                await redis.delete(kdel)
            except Exception:
                pass

        out: list[dict[str, Any]] = []
        for _k, val in (blob or {}).items():
            try:
                out.append(json.loads(_decode(val)))
            except Exception:
                continue

        # Thứ tự ổn định theo probe
        order = [
            "k8s_clinical_pod_status",
            "k8s_clinical_pod_metrics",
            "k8s_clinical_pod_log_tail",
            "prom_pod_cpu_cores",
            "prom_pod_memory_wss",
        ]

        def _sort_key(d: dict[str, Any]) -> int:
            p = str(d.get("probe") or "")
            try:
                return order.index(p)
            except ValueError:
                return 99

        out.sort(key=_sort_key)
        return out
    except Exception:
        try:
            await redis.delete(lk)
        except Exception:
            pass
        raise
