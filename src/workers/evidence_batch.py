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

# Khớp `resource_probe_ids` / `kube_pod_state_probe_ids` — gom đủ trước RAG (tránh flush 2/5 probe).
_WORKLOAD_FULL_PROBE_SET = frozenset(
    {
        "k8s_clinical_pod_status",
        "k8s_clinical_pod_metrics",
        "k8s_clinical_pod_log_tail",
        "prom_pod_cpu_cores",
        "prom_pod_memory_wss",
    }
)

REDIS_BATCH = "omni:diag_batch:{trace}"
REDIS_T0 = "omni:diag_batch_t0:{trace}"
REDIS_LOCK = "omni:diag_flush_lock:{trace}"
REDIS_EXPECTED = "omni:diag_expected:{trace}"


def _decode(b: Any) -> str:
    if isinstance(b, bytes):
        return b.decode("utf-8", errors="replace")
    return str(b)


async def register_diag_expected_probes(redis: Any, trace: str, probes: list[str]) -> None:
    """Expected probe ids for this trace (smart dispatcher). TTL matches batch window."""
    try:
        await redis.setex(REDIS_EXPECTED.format(trace=trace), BATCH_TTL, json.dumps(probes))
    except Exception as e:
        logger.warning("event=diag_expected_register_fail trace=%s err=%s", trace, e)


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
    workload = sym in ("workload_resource", "pod_container_state")

    expected_set: frozenset[str] | None = None
    try:
        raw_exp = await redis.get(REDIS_EXPECTED.format(trace=trace))
        if raw_exp:
            exp_list = json.loads(_decode(raw_exp))
            if isinstance(exp_list, list) and exp_list:
                expected_set = frozenset(str(x) for x in exp_list)
    except Exception:
        expected_set = None

    ready = False
    if workload:
        if expected_set is not None:
            ready = expected_set <= keys or elapsed >= AGG_TIMEOUT_SEC
        else:
            ready = _WORKLOAD_FULL_PROBE_SET <= keys or elapsed >= AGG_TIMEOUT_SEC
    else:
        # Security / matrix: khi dispatcher đã register_diag_expected_probes (vd. ['rbac_drift']),
        # flush ngay khi đủ probe — tránh deadlock một message / một probe.
        if expected_set is not None:
            ready = expected_set <= keys or elapsed >= AGG_TIMEOUT_SEC
        else:
            # Không có expected: gom tối đa 3s hoặc ≥2 probe (thường redis+kafka)
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
        for kdel in (hk, tk, lk, REDIS_EXPECTED.format(trace=trace)):
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
            "k8s_clinical_pod_events",
            "k8s_events_probe",
            "k8s_resource_quota_probe",
            "k8s_clinical_pod_log_previous",
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
