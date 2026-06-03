"""System Health Manifest: Prometheus + K8s Warning events → Redis (slow-path context)."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from collections import OrderedDict, defaultdict
from typing import Any

from kubernetes_asyncio import client

from anomaly.three_sigma import ThreeSigmaGate
from workers.sdk_service_tools import _prometheus_get_json

logger = logging.getLogger(__name__)

_SIGMA_GATE: ThreeSigmaGate | None = None


def _get_sigma_gate(redis: Any) -> ThreeSigmaGate:
    global _SIGMA_GATE
    if _SIGMA_GATE is None or _SIGMA_GATE._r is not redis:
        _SIGMA_GATE = ThreeSigmaGate(redis)
    return _SIGMA_GATE

REDIS_KEY_SNAPSHOT = "omni:baseline:snapshot"
REDIS_KEY_TS = "omni:baseline:ts"

# Hint legend (keys: t cpu mem z_* net dsk rp evt dr chs golden remediation_silent)
BASELINE_HINT_LEGEND = (
    "[baseline] t=epoch; cpu=busy(0-1) mem=avail_ratio(0-1); "
    "z_cpu/z_mem/z_disk=Prom Z vs 24h; seasonal_drift_z=WoW (fallback trong Prom); "
    "net.rx/net.tx=B/s; dsk.u=fs_used_ratio dsk.rt/wt=disk_B/s dsk.ri/wi=iops; "
    "rp.c/rp.m=usage/req(ns); evt=k8s_Warning[k]; "
    "dr=true iff |z|>thr (3-Sigma default) hoặc legacy cpu drift nếu bật; "
    "chs/wide_incident khi OMNI_CHS_WEIGHTS; golden.latency_p99_ms; "
    "remediation_silent=true khi latency đo được và < OMNI_LATENCY_THRESHOLD_MS | "
)

# --- Built-in PromQL (node_exporter + cAdvisor + kube-state-metrics) ---


def _prom_cpu_busy() -> str:
    return 'avg(rate(node_cpu_seconds_total{mode!="idle"}[5m]))'


def _prom_mem_avail_ratio() -> str:
    return "avg(node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)"


def _prom_net_rx() -> str:
    return "sum(rate(node_network_receive_bytes_total[5m]))"


def _prom_net_tx() -> str:
    return "sum(rate(node_network_transmit_bytes_total[5m]))"


def _prom_dsk_usage_ratio() -> str:
    return (
        "avg(1 - (node_filesystem_avail_bytes{fstype!~\"tmpfs|squashfs|overlay\"} / "
        "node_filesystem_size_bytes{fstype!~\"tmpfs|squashfs|overlay\"}))"
    )


def _prom_dsk_read_bps() -> str:
    return "sum(rate(node_disk_read_bytes_total[5m]))"


def _prom_dsk_write_bps() -> str:
    return "sum(rate(node_disk_written_bytes_total[5m]))"


def _prom_dsk_read_iops() -> str:
    return "sum(rate(node_disk_reads_completed_total[5m]))"


def _prom_dsk_write_iops() -> str:
    return "sum(rate(node_disk_writes_completed_total[5m]))"


def _prom_cpu_usage_ns(ns: str) -> str:
    return (
        f'sum(rate(container_cpu_usage_seconds_total{{namespace="{ns}",container!="POD",container!=""}}[5m]))'
    )


def _prom_cpu_requests_ns(ns: str) -> str:
    return f'sum(kube_pod_container_resource_requests{{namespace="{ns}",resource="cpu"}})'


def _prom_mem_working_ns(ns: str) -> str:
    return (
        f'sum(container_memory_working_set_bytes{{namespace="{ns}",container!="POD",container!=""}})'
    )


def _prom_mem_requests_ns(ns: str) -> str:
    return f'sum(kube_pod_container_resource_requests{{namespace="{ns}",resource="memory"}})'


def _compact_instant(data: dict[str, Any]) -> dict[str, Any]:
    """Vector instant → cấu trúc nhỏ (tests / legacy)."""
    res = (data.get("data") or {}).get("result") or []
    samples: list[dict[str, Any]] = []
    for r in res[:40]:
        metric = dict(r.get("metric") or {})
        name = metric.pop("__name__", "")
        val = r.get("value") or []
        v = val[1] if len(val) >= 2 else ""
        samples.append({"metric": name or "?", "labels": metric, "v": str(v)})
    return {"n": len(res), "top": samples[:25]}


def parse_baseline_promql_lines(raw: str) -> list[tuple[str, str]]:
    """Mỗi dòng `name|instant_promql`; bỏ comment và dòng trống."""
    out: list[tuple[str, str]] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "|" not in line:
            continue
        name, q = line.split("|", 1)
        name, q = name.strip(), q.strip()
        if name and q:
            out.append((name, q))
    return out


def _instant_to_scalar_str(data: dict[str, Any]) -> str | None:
    res = (data.get("data") or {}).get("result") or []
    if not res:
        return None
    if len(res) == 1:
        v = res[0].get("value") or []
        return str(v[1]) if len(v) >= 2 else None
    vals: list[str] = []
    for r in res[:5]:
        v = r.get("value") or []
        vals.append(str(v[1]) if len(v) >= 2 else "?")
    return json.dumps(vals, ensure_ascii=False)


async def _query_scalar_str(ctx: Any, promql: str) -> str | None:
    try:
        data = await _prometheus_get_json(ctx, "/api/v1/query", {"query": promql})
        if data.get("status") != "success":
            return None
        return _instant_to_scalar_str(data)
    except Exception as e:
        logger.debug("baseline promql: %s", e)
        return None


async def _query_float(ctx: Any, promql: str) -> float | None:
    s = await _query_scalar_str(ctx, promql)
    if s is None:
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _round_num(x: float | None, nd: int = 4) -> float | None:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return None
    return round(float(x), nd)


def _infer_latency_ms_from_prom_value(v: float) -> float | None:
    """Prometheus thường trả latency tính bằng giây; số lớn coi là đã ms."""
    if math.isnan(v) or math.isinf(v):
        return None
    x = float(v)
    if abs(x) <= 120.0:
        return round(x * 1000.0, 4)
    return round(x, 4)


def _parse_chs_weights_json(raw: str) -> dict[str, float]:
    s = (raw or "").strip()
    if not s:
        return {}
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        logger.warning("OMNI_CHS_WEIGHTS: invalid JSON, skipping CHS")
        return {}
    if not isinstance(obj, dict):
        return {}
    out: dict[str, float] = {}
    for k, v in obj.items():
        kk = str(k).lower().strip()
        if kk and isinstance(v, (int, float)) and not (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
            out[kk] = float(v)
    return out


def _compute_chs(
    weights: dict[str, float],
    z_cpu: float | None,
    z_mem: float | None,
    z_disk: float | None,
    z_net: float | None,
) -> float | None:
    if not weights:
        return None
    zmap = {
        "cpu": z_cpu,
        "mem": z_mem,
        "disk": z_disk,
        "net": z_net,
    }
    total = 0.0
    for key, w in weights.items():
        zv = zmap.get(key)
        if zv is None:
            zi = 0.0
        else:
            try:
                fv = float(zv)
            except (TypeError, ValueError):
                zi = 0.0
            else:
                if math.isnan(fv) or math.isinf(fv):
                    zi = 0.0
                else:
                    zi = abs(fv)
        total += float(w) * zi
    return _round_num(total, nd=4)


def _extract_cpu_from_old_snapshot(raw: Any) -> float | None:
    if not raw:
        return None
    s = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        return None
    if isinstance(obj.get("cpu"), (int, float)):
        return float(obj["cpu"])
    q = obj.get("queries") or {}
    if isinstance(q.get("cpu_busy"), str):
        try:
            return float(q["cpu_busy"])
        except ValueError:
            pass
    return None


def _cpu_drift(
    old_cpu: float | None,
    new_cpu: float | None,
    *,
    threshold: float,
    eps: float = 1e-9,
) -> bool:
    if old_cpu is None or new_cpu is None:
        return False
    delta = abs(new_cpu - old_cpu)
    if abs(old_cpu) < eps:
        return delta > threshold
    return (delta / abs(old_cpu)) > threshold


def _sigma_dr(
    z_cpu: float | None,
    z_mem: float | None,
    *,
    threshold: float,
) -> bool:
    if z_cpu is not None and abs(float(z_cpu)) > threshold:
        return True
    if z_mem is not None and abs(float(z_mem)) > threshold:
        return True
    return False


async def _kube_warning_events(ws: Any) -> list[dict[str, Any]]:
    """5 Warning events gần nhất (toàn cụm), field gọn: ns, r, m, o."""
    from init.deep_scout import _kube_load

    max_e = int(getattr(ws, "baseline_warning_events_max", 5) or 5)
    fetch_lim = int(getattr(ws, "baseline_warning_events_fetch_limit", 400) or 400)
    timeout_s = float(getattr(ws, "baseline_k8s_events_timeout_sec", 20.0) or 20.0)

    async def _run() -> list[dict[str, Any]]:
        await _kube_load()
        v1 = client.CoreV1Api()
        try:
            evl = await v1.list_event_for_all_namespaces(
                field_selector="type=Warning",
                limit=fetch_lim,
            )
            items = list(evl.items or [])

            def _ts(ev: Any) -> float:
                lt = getattr(ev, "last_timestamp", None)
                if lt is not None and hasattr(lt, "timestamp"):
                    return float(lt.timestamp())
                et = getattr(ev, "event_time", None)
                if et is not None and hasattr(et, "timestamp"):
                    return float(et.timestamp())
                ct = getattr(ev, "metadata", None) and getattr(ev.metadata, "creation_timestamp", None)
                if ct is not None and hasattr(ct, "timestamp"):
                    return float(ct.timestamp())
                return 0.0

            items.sort(key=_ts, reverse=True)
            out: list[dict[str, Any]] = []
            for ev in items[:max_e]:
                md = getattr(ev, "metadata", None)
                ns = (md.namespace if md else None) or ""
                reason = str(getattr(ev, "reason", "") or "")[:64]
                msg = str(getattr(ev, "message", "") or "").replace("\n", " ")[:100]
                inv = getattr(ev, "involved_object", None)
                kind = str(getattr(inv, "kind", "") or "") if inv else ""
                name = str(getattr(inv, "name", "") or "") if inv else ""
                o = f"{kind}/{name}"[:80] if kind or name else "?"
                out.append({"ns": ns, "r": reason, "m": msg, "o": o, "ts": int(_ts(ev))})
            return out
        finally:
            await v1.api_client.close()

    try:
        return await asyncio.wait_for(_run(), timeout=timeout_s)
    except Exception as e:
        logger.warning("baseline k8s events: %s", e)
        return []


async def _supplemental_queries(ctx: Any, raw: str) -> dict[str, Any]:
    pairs = parse_baseline_promql_lines(raw)
    if not pairs:
        return {}
    out: dict[str, Any] = {}
    for name, q in pairs[:8]:
        s = await _query_scalar_str(ctx, q)
        key = name[:12] if len(name) > 12 else name
        out[key] = s if s is not None else None
    return out


async def build_health_manifest_dict(ctx: Any, ws: Any, old_raw: Any) -> dict[str, Any]:
    """Một tick manifest (chưa enforce byte budget)."""
    ns = (getattr(ws, "k8s_default_namespace", None) or "multi-agent").strip() or "multi-agent"
    thr = float(getattr(ws, "baseline_cpu_drift_threshold", 0.15) or 0.15)

    old_cpu = _extract_cpu_from_old_snapshot(old_raw)

    prom_tasks = [
        _query_float(ctx, _prom_cpu_busy()),
        _query_float(ctx, _prom_mem_avail_ratio()),
        _query_float(ctx, _prom_net_rx()),
        _query_float(ctx, _prom_net_tx()),
        _query_float(ctx, _prom_dsk_usage_ratio()),
        _query_float(ctx, _prom_dsk_read_bps()),
        _query_float(ctx, _prom_dsk_write_bps()),
        _query_float(ctx, _prom_dsk_read_iops()),
        _query_float(ctx, _prom_dsk_write_iops()),
        _query_float(ctx, _prom_cpu_usage_ns(ns)),
        _query_float(ctx, _prom_cpu_requests_ns(ns)),
        _query_float(ctx, _prom_mem_working_ns(ns)),
        _query_float(ctx, _prom_mem_requests_ns(ns)),
    ]

    ev_task = asyncio.create_task(_kube_warning_events(ws))
    prom_res = await asyncio.gather(*prom_tasks, return_exceptions=True)

    def _safe_float(i: int) -> float | None:
        if i >= len(prom_res):
            return None
        r = prom_res[i]
        if isinstance(r, Exception):
            logger.debug("baseline prom task %s: %s", i, r)
            return None
        return r if isinstance(r, (int, float)) else None

    cpu = _round_num(_safe_float(0))
    mem = _round_num(_safe_float(1))
    net_rx = _round_num(_safe_float(2))
    net_tx = _round_num(_safe_float(3))
    dsk_u = _round_num(_safe_float(4))
    dsk_rt = _round_num(_safe_float(5))
    dsk_wt = _round_num(_safe_float(6))
    dsk_ri = _round_num(_safe_float(7))
    dsk_wi = _round_num(_safe_float(8))

    cpu_use = _safe_float(9)
    cpu_req = _safe_float(10)
    mem_use = _safe_float(11)
    mem_req = _safe_float(12)

    rp_c: float | None = None
    if cpu_use is not None and cpu_req is not None and abs(cpu_req) > 1e-12:
        rp_c = _round_num(cpu_use / cpu_req)
    rp_m: float | None = None
    if mem_use is not None and mem_req is not None and abs(mem_req) > 1e-12:
        rp_m = _round_num(mem_use / mem_req)

    evt: list[dict[str, Any]] = []
    try:
        evt = await ev_task
    except Exception as e:
        logger.warning("baseline evt task: %s", e)
        evt = []

    new_cpu_f = float(cpu) if cpu is not None else None
    old_cpu_f = old_cpu

    zq_cpu = (getattr(ws, "baseline_promql_z_cpu", None) or "omni:node_cpu:z").strip()
    zq_mem = (getattr(ws, "baseline_promql_z_mem", None) or "omni:mem:z").strip()
    z_thr = float(getattr(ws, "baseline_dr_z_threshold", 3.0) or 3.0)
    z_cpu_f, z_mem_f = await asyncio.gather(
        _query_float(ctx, zq_cpu),
        _query_float(ctx, zq_mem),
    )
    # Fallback: when Prometheus recording rules absent, derive z from ThreeSigmaGate rolling window.
    # The gate is fed with cluster_cpu/cluster_mem raw values each tick in baseline_sync_loop.
    _ctx_redis = getattr(ctx, "redis", None)
    if _ctx_redis is not None and (z_cpu_f is None or z_mem_f is None):
        try:
            _gate = _get_sigma_gate(_ctx_redis)
            if z_cpu_f is None:
                z_cpu_f = await _gate.get_z_score("cluster_cpu")
                if z_cpu_f is not None:
                    logger.debug("baseline z_cpu fallback from ThreeSigmaGate: %.3f", z_cpu_f)
            if z_mem_f is None:
                z_mem_f = await _gate.get_z_score("cluster_mem")
                if z_mem_f is not None:
                    logger.debug("baseline z_mem fallback from ThreeSigmaGate: %.3f", z_mem_f)
        except Exception as _zfe:
            logger.debug("baseline z_score gate fallback: %s", _zfe)
    z_cpu = _round_num(z_cpu_f)
    z_mem = _round_num(z_mem_f)

    zq_disk = (getattr(ws, "baseline_promql_z_disk", None) or "omni:node_disk:z").strip()
    zq_iops = (getattr(ws, "baseline_promql_z_iops", None) or "omni:node_iops:z").strip()
    zq_net = (getattr(ws, "baseline_promql_z_net", None) or "").strip()
    zq_seasonal = (getattr(ws, "baseline_promql_seasonal_cpu", None) or "").strip()
    gold_q = (getattr(ws, "golden_latency_promql", None) or "").strip()

    async def _opt_float(q: str) -> float | None:
        qq = (q or "").strip()
        if not qq:
            return None
        return await _query_float(ctx, qq)

    zd_f, zi_f, zn_f, z_seasonal_f, gold_lat_f = await asyncio.gather(
        _opt_float(zq_disk),
        _opt_float(zq_iops),
        _opt_float(zq_net),
        _opt_float(zq_seasonal),
        _opt_float(gold_q),
    )
    z_disk = _round_num(zd_f)
    z_iops = _round_num(zi_f)
    z_net = _round_num(zn_f) if zq_net else None
    seasonal_drift_z = _round_num(z_seasonal_f) if zq_seasonal else None

    latency_ms: float | None = None
    if gold_lat_f is not None:
        latency_ms = _infer_latency_ms_from_prom_value(float(gold_lat_f))

    lat_thr = getattr(ws, "latency_threshold_ms", None)
    remediation_silent = False
    if latency_ms is not None and lat_thr is not None:
        try:
            if float(latency_ms) < float(lat_thr):
                remediation_silent = True
        except (TypeError, ValueError):
            pass

    chs_weights = _parse_chs_weights_json(getattr(ws, "chs_weights", "") or "")
    chs_thr = float(getattr(ws, "chs_threshold", 10.0) or 10.0)
    chs_val = _compute_chs(chs_weights, z_cpu_f, z_mem_f, zd_f, zn_f if zq_net else None)
    wide_incident = False
    if chs_val is not None:
        wide_incident = float(chs_val) > chs_thr

    dr_sigma = _sigma_dr(z_cpu, z_mem, threshold=z_thr)
    dr_legacy = False
    if bool(getattr(ws, "baseline_legacy_cpu_drift_for_dr", False)):
        dr_legacy = _cpu_drift(old_cpu_f, new_cpu_f, threshold=thr)
    dr = dr_sigma or dr_legacy

    manifest: dict[str, Any] = {
        "t": int(time.time()),
        "cpu": cpu,
        "mem": mem,
        "z_cpu": z_cpu,
        "z_mem": z_mem,
        "z_disk": z_disk,
        "z_iops": z_iops,
        "net": {"rx": net_rx, "tx": net_tx},
        "dsk": {
            "u": dsk_u,
            "rt": dsk_rt,
            "wt": dsk_wt,
            "ri": dsk_ri,
            "wi": dsk_wi,
        },
        "rp": {"c": rp_c, "m": rp_m},
        "evt": evt,
        "dr": dr,
        "remediation_silent": remediation_silent,
    }

    if zq_net:
        manifest["z_net"] = z_net
    if zq_seasonal:
        manifest["seasonal_drift_z"] = seasonal_drift_z
    if gold_q:
        manifest["golden"] = {
            "latency_p99_ms": latency_ms,
            "source": "promql",
        }
    if chs_weights and chs_val is not None:
        manifest["chs"] = chs_val
        manifest["chs_thr"] = chs_thr
        manifest["w"] = chs_weights
        manifest["wide_incident"] = wide_incident

    extra = (getattr(ws, "baseline_promql", None) or "").strip()
    if extra:
        q_extra = await _supplemental_queries(ctx, extra)
        if q_extra:
            manifest["q"] = q_extra

    return manifest


def _smart_trim_manifest(
    manifest: dict[str, Any],
    max_chars: int,
    *,
    window_min: int = 10,
    keywords: str = "OOMKilled|Evicted|Failed|Timeout",
) -> str:
    """Smart trim with sliding window, keyword-protected events, and deduplication.

    1. Filter events older than window_min minutes (ts=0 → keep always).
    2. Deduplicate: same (reason, object) → [Count: X] prefix.
    3. Trim budget: drop non-critical events first, then optional fields, then truncate critical msg to keyword context.
    """
    m: dict[str, Any] = json.loads(json.dumps(manifest, ensure_ascii=False))
    kw_re = re.compile(keywords, re.IGNORECASE) if keywords else None
    now_t = int(m.get("t") or time.time())
    cutoff = now_t - window_min * 60

    # Step 1: sliding window — drop stale events (ts=0 means unknown → keep)
    evts: list[dict[str, Any]] = m.get("evt") or []
    if evts:
        fresh = [e for e in evts if int(e.get("ts") or 0) == 0 or int(e.get("ts", 0)) >= cutoff]
        m["evt"] = fresh if fresh else evts  # always keep at least original if all stale

    # Step 2: deduplicate by (reason, object)
    evts = m.get("evt") or []
    if evts:
        counts: dict[tuple[str, str], int] = defaultdict(int)
        first: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
        for e in evts:
            key = (str(e.get("r") or ""), str(e.get("o") or ""))
            counts[key] += 1
            if key not in first:
                first[key] = dict(e)
        deduped: list[dict[str, Any]] = []
        for key, ev in first.items():
            c = counts[key]
            if c > 1:
                ev = dict(ev)
                ev["m"] = f"[Count:{c}] {ev.get('m', '')}"
            deduped.append(ev)
        m["evt"] = deduped

    def _is_critical(e: dict[str, Any]) -> bool:
        if kw_re is None:
            return False
        return bool(kw_re.search(str(e.get("m") or "") + str(e.get("r") or "")))

    def dumps() -> str:
        return json.dumps(m, ensure_ascii=False, separators=(",", ":"))

    def _trim_one() -> bool:
        evts = m.get("evt") or []
        non_crit = [e for e in evts if not _is_critical(e)]
        crit = [e for e in evts if _is_critical(e)]
        # Drop non-critical events from the tail first
        if non_crit:
            m["evt"] = crit + non_crit[:-1]
            return True
        # Shorten critical messages to keyword + ~50 char context (±3 lines approximation)
        if crit:
            last = dict(crit[-1])
            msg = str(last.get("m") or "")
            if len(msg) > 60:
                match = kw_re.search(msg) if kw_re else None
                if match:
                    start = max(0, match.start() - 15)
                    end = min(len(msg), match.end() + 50)
                    last["m"] = msg[start:end]
                else:
                    last["m"] = msg[:60]
                m["evt"] = crit[:-1] + [last]
                return True
        # Drop optional enrichment fields in order of expendability
        for field in ("q", "w", "golden", "chs", "chs_thr", "wide_incident"):
            if field in m:
                del m[field]
                return True
        dsk = m.get("dsk")
        if isinstance(dsk, dict):
            for k in ("wi", "ri", "wt", "rt"):
                if k in dsk:
                    del dsk[k]
                    return True
        return False

    for _ in range(32):
        if len(dumps()) <= max_chars:
            break
        if not _trim_one():
            break

    s = dumps()
    if len(s) > max_chars:
        return s[: max_chars - 1] + "…"
    return s


def _manifest_json_under_budget(manifest: dict[str, Any], max_chars: int) -> str:
    """Backward-compat shim → delegates to _smart_trim_manifest."""
    return _smart_trim_manifest(manifest, max_chars)


async def baseline_sync_loop(ctx: Any, stop: asyncio.Event) -> None:
    ws = ctx.settings
    if not ws.baseline_snapshot_enabled:
        logger.info("baseline_sync_loop disabled")
        return
    await ctx.scout_ready.wait()
    interval = float(ws.baseline_snapshot_interval_sec)
    ttl = int(ws.baseline_snapshot_redis_ttl_sec)
    max_chars = int(getattr(ws, "baseline_manifest_max_chars", 1400) or 1400)
    trim_window_min = int(getattr(ws, "baseline_smart_trim_window_min", 10) or 10)
    trim_keywords = str(getattr(ws, "baseline_smart_trim_keywords", "OOMKilled|Evicted|Failed|Timeout") or "OOMKilled|Evicted|Failed|Timeout")

    logger.info(
        "baseline_sync_loop start interval_sec=%s ttl=%s manifest_max=%s trim_window_min=%s",
        interval,
        ttl,
        max_chars,
        trim_window_min,
    )

    while not stop.is_set():
        try:
            old_raw = await ctx.redis.get(REDIS_KEY_SNAPSHOT)
            manifest = await build_health_manifest_dict(ctx, ws, old_raw)
            payload = _smart_trim_manifest(
                manifest, max_chars, window_min=trim_window_min, keywords=trim_keywords
            )
            try:
                from workers.metrics_exporter import set_baseline_snapshot_gauges

                zc = manifest.get("z_cpu")
                zm = manifest.get("z_mem")
                zd = manifest.get("z_disk")
                zi = manifest.get("z_iops")
                set_baseline_snapshot_gauges(
                    z_cpu=float(zc) if isinstance(zc, (int, float)) else None,
                    z_mem=float(zm) if isinstance(zm, (int, float)) else None,
                    z_disk=float(zd) if isinstance(zd, (int, float)) else None,
                    z_iops=float(zi) if isinstance(zi, (int, float)) else None,
                    dr=bool(manifest.get("dr")),
                    chs=float(manifest["chs"])
                    if isinstance(manifest.get("chs"), (int, float))
                    else None,
                    remediation_silent=bool(manifest.get("remediation_silent")),
                )
            except Exception as e:
                logger.debug("baseline snapshot gauges: %s", e)
            # S3.2: feed cluster-level CPU/mem into ThreeSigmaGate rolling window (Redis-backed).
            # Provides fallback z-scores when Prometheus recording rules are absent.
            _zc_raw = manifest.get("cpu")
            _zm_raw = manifest.get("mem")
            if _zc_raw is not None or _zm_raw is not None:
                try:
                    gate = _get_sigma_gate(ctx.redis)
                    if _zc_raw is not None:
                        await gate.observe_adaptive("cluster_cpu", float(_zc_raw))
                    if _zm_raw is not None:
                        await gate.observe_adaptive("cluster_mem", float(_zm_raw))
                except Exception as _sg_err:
                    logger.debug("baseline sigma_gate.observe_adaptive: %s", _sg_err)

            ts = int(time.time())
            await ctx.redis.setex(REDIS_KEY_SNAPSHOT, ttl, payload)
            await ctx.redis.setex(REDIS_KEY_TS, ttl, str(ts))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("baseline_sync_loop tick: %s", e)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def baseline_snapshot_loop(ctx: Any, stop: asyncio.Event) -> None:
    await baseline_sync_loop(ctx, stop)


async def fetch_baseline_system_prompt(redis: Any, max_chars: int) -> str:
    header = "[SYSTEM BASELINE CONTEXT (LAST 5 MINS)]: "
    try:
        raw = await redis.get(REDIS_KEY_SNAPSHOT)
    except Exception:
        return ""
    if not raw:
        return ""
    s = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
    budget = max(0, max_chars - len(header))
    if len(s) > budget:
        s = s[: max(0, budget - 1)] + "…"
    return header + s


async def fetch_baseline_snapshot_hint(redis: Any, max_chars: int) -> str:
    """Legend + snapshot JSON (truncate)."""
    try:
        raw = await redis.get(REDIS_KEY_SNAPSHOT)
        ts_raw = await redis.get(REDIS_KEY_TS)
    except Exception:
        return ""
    if not raw:
        return ""
    s = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
    ts = ""
    if ts_raw:
        ts = ts_raw if isinstance(ts_raw, str) else ts_raw.decode("utf-8", errors="replace")
    head = f"ts={ts} " if ts else ""
    body = BASELINE_HINT_LEGEND + head + s
    if len(body) > max_chars:
        return body[: max_chars - 1] + "…"
    return body
