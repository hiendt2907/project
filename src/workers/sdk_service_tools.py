"""Service tools: psutil, httpx→Prometheus (PromQL), matplotlib, scapy, asyncpg — không shell/subprocess."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx
import numpy as np
import psutil

from anomaly.forecast import (
    forecast_horizon_steps,
    oom_risk_from_series,
    pandas_trend_forecast,
    series_step_seconds,
)
from anomaly.prophet_forecast import forecast_backend_used, horizons_to_periods, step_to_pandas_freq
from metrics.prometheus_dataframe import fetch_range_dataframe
from rag.pgvector_store import COLLECTION_ERRORS, COLLECTION_SOP
from visualization.chart_bytes import (
    line_chart_history_forecast_ci_png_bytes,
    line_chart_history_forecast_png_bytes,
    line_chart_png_bytes,
)
from workers.analytics_ts import analyze_series, parse_prometheus_matrix_first_series
from workers.promql_presets import (
    build_dynamic_promql,
    build_kube_state_promql,
    build_promql_from_intent,
    resolve_intent_from_keywords,
)
from workers.telegram_ctx import effective_telegram_chat_id, should_send_telegram_chart
from workers.metrics_exporter import inc_promql_placeholder_rejected
from workers.settings import default_prometheus_http_base
from workers.vm_timeseries_helpers import prometheus_timeseries_to_line_chart_png_bytes

parse_vm_matrix_first_series = parse_prometheus_matrix_first_series
vm_timeseries_to_line_chart_png_bytes = prometheus_timeseries_to_line_chart_png_bytes

logger = logging.getLogger(__name__)


def is_placeholder_promql(query: str) -> bool:
    """True nếu query là placeholder từ LLM (metric_value > threshold, ...)."""
    raw = (query or "").strip()
    if not raw:
        return True
    collapsed = "".join(raw.lower().split())
    if "metric_value" in collapsed and "threshold" in collapsed and ">" in raw:
        return True
    if collapsed in ("metric_value>threshold", "metric_value>=threshold"):
        return True
    return False


def _dbg_log(run_id: str, hypothesis_id: str, location: str, message: str, data: dict[str, Any]) -> None:
    # region agent log
    try:
        payload = {
            "sessionId": "3d50e2",
            "runId": run_id,
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data,
            "timestamp": int(time.time() * 1000),
        }
        with open("/Users/hiendang/project/.cursor/debug-3d50e2.log", "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass
    # endregion


class VMHTTPForbidden(Exception):
    """Prometheus / metrics HTTP 403 — không đưa mã trạng thái ra user-facing text."""


def _diagnosis_vm_403() -> str:
    return (
        "Em chưa có quyền đọc Metrics của Node này, đại ca cấp quyền ClusterRole cho em nhé"
    )


def _duration_window_label(duration: str) -> str:
    d = (duration or "1h").strip().lower()
    if d.endswith("h") and len(d) > 1 and d[:-1].replace(".", "").isdigit():
        return f"{d[:-1]} giờ"
    if d.endswith("m") and len(d) > 1 and d[:-1].isdigit():
        return f"{d[:-1]} phút"
    return "1 giờ"


def _diagnosis_vm_empty(args: dict[str, Any], duration: str, *, promql: str) -> str:
    win = _duration_window_label(duration)
    dbg = f"\n[DEBUG] PromQL: {promql}"
    tt = str(args.get("target_type") or "").strip().lower()
    if tt == "host":
        return (
            f"Em đã query nhưng Prometheus báo không có dữ liệu métric **Host/node** trong {win} qua. "
            "Đại ca kiểm tra node_exporter scrape và tên métric."
            f"{dbg}"
        )
    if tt in ("kube_deployment", "kube_state_deployment"):
        dep = str(args.get("deployment") or args.get("deployment_name") or "").strip()
        ns = str(args.get("namespace") or "").strip()
        return (
            f"Em đã query nhưng Prometheus không có dữ liệu **kube-state-metrics** (deployment) "
            f'namespace="{ns}" deployment="{dep}" trong {win} qua. '
            "Kiểm tra Service kube-state-metrics và job scrape `kube_*`."
            f"{dbg}"
        )
    if tt == "kube_namespace":
        ns = str(args.get("namespace") or "").strip()
        return (
            f"Em đã query nhưng Prometheus không có dữ liệu **kube-state-metrics** (namespace) "
            f'namespace="{ns}" trong {win} qua. Kiểm tra scrape kube_pod_status_phase / kube-state.'
            f"{dbg}"
        )
    pod = str(args.get("pod_name") or args.get("pod") or "").strip()
    ns = str(args.get("namespace") or "").strip()
    if ns and pod:
        scope = f'namespace="{ns}" pod="{pod}"'
    elif pod:
        scope = f'pod="{pod}"'
    else:
        scope = "pod/workload (thiếu namespace hoặc pod trong args — kiểm tra session/extract)"
    return (
        f"Em đã query nhưng Prometheus báo không có dữ liệu cho {scope} trong {win} qua. "
        "Kiểm tra: Pod có **/metrics** (exporter) và annotation prometheus.io/scrape; "
        "métric CPU/RAM **container** cần kubelet/cAdvisor scrape vào Prometheus — khác nhau."
        f"{dbg}"
    )


def _vm_user_facing_error(e: Exception) -> str:
    if isinstance(e, VMHTTPForbidden):
        return "[DATA] khong_co_quyen\n[DIAGNOSIS] " + _diagnosis_vm_403()
    if isinstance(e, httpx.HTTPStatusError) and e.response is not None and e.response.status_code == 403:
        return "[DATA] khong_co_quyen\n[DIAGNOSIS] " + _diagnosis_vm_403()
    et = type(e).__name__
    hint = ""
    if isinstance(e, httpx.ConnectError):
        hint = (
            " Thường do DNS/Service không tồn tại (chưa apply stack monitor) hoặc sai OMNI_PROMETHEUS_URL. "
            "Ops: `kubectl apply -f k8s/monitor/prometheus.yaml` (và kube-state nếu cần), "
            "`kubectl get svc -n monitor`. Mặc định in-cluster: "
            f"`{default_prometheus_http_base()}`. "
            "Chạy worker ngoài K8s (local): trỏ `OMNI_PROMETHEUS_URL` tới Prometheus thật (port-forward hoặc IP host)."
        )
    elif isinstance(e, httpx.TimeoutException):
        hint = (
            " Timeout tới Prometheus — kiểm tra Pod `prometheus` (namespace `monitor`) Running và không bị NetworkPolicy chặn."
        )
    elif isinstance(e, httpx.HTTPStatusError) and e.response is not None:
        hint = f" HTTP {e.response.status_code} từ Prometheus."
    return (
        "[DATA] error\n[DIAGNOSIS] Không gọi được Prometheus (mạng hoặc endpoint). "
        f"[DEBUG] exception={et}"
        + (hint if hint else " Đại ca kiểm tra `OMNI_PROMETHEUS_URL` và stack monitor.")
    )


# Gợi ý PromQL — agent chọn metric khớp exporter thật trong cluster.
PROMQL_HINTS_MD = """
## Redis (redis_exporter — tên metric có thể có prefix job/pod)
- `redis_up` — exporter thấy Redis
- `redis_memory_used_bytes` — RAM Redis
- `redis_connected_clients` — client đang kết nối
- `rate(redis_commands_processed_total[5m])` — lệnh/giây (gần đúng)
- `redis_keyspace_hits_total` / `redis_keyspace_misses_total` — cache hit

## Disk / IOPS (node_exporter)
- `rate(node_disk_reads_completed_total[5m])` — đọc ops/s
- `rate(node_disk_writes_completed_total[5m])` — ghi ops/s
- `rate(node_disk_read_bytes_total[5m])` — byte/s đọc

## Hệ thống
- `up` — target scrape sống
- `node_cpu_seconds_total` — CPU (cần rate/irate)

## kube-state-metrics (trạng thái cluster — khác cAdvisor `container_*`)
- `kube_deployment_status_replicas_available` / `kube_deployment_spec_replicas` — replica khả dụng vs desired
- Tỉ lệ: `available / spec` (dự báo lệch capacity)
- `sum(kube_pod_status_phase{phase="Running"})` — số pod Running (theo namespace)
- `sum(kube_pod_status_phase{phase="Pending"})` — backlog Scheduling

Dùng `promql_range` (hoặc `vm_promql_range`) với start/end/step để lấy chuỗi thời gian; `timeseries_analyze` để thống kê + dự đoán tuyến tính.
""".strip()


def _prometheus_base_url(ctx: Any) -> str:
    s = getattr(ctx, "settings", None)
    if s is not None:
        u = getattr(s, "prometheus_url", None)
        if isinstance(u, str) and u.strip():
            return u.strip().rstrip("/")
    return default_prometheus_http_base()


async def _prometheus_get_json(ctx: Any, path: str, params: dict[str, Any]) -> dict[str, Any]:
    base = _prometheus_base_url(ctx)
    url = f"{base}{path}"
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.get(url, params=params)
        if r.status_code == 403:
            raise VMHTTPForbidden()
        r.raise_for_status()
        return r.json()


_vm_base_url = _prometheus_base_url
_vm_get_json = _prometheus_get_json


def _default_namespace(ctx: Any) -> str:
    s = getattr(ctx, "settings", None)
    if s is not None:
        n = getattr(s, "k8s_default_namespace", None)
        if isinstance(n, str) and n.strip():
            return n.strip()
    return "multi-agent"


def resolve_promql_for_args(args: dict[str, Any], ctx: Any) -> tuple[str, str]:
    """Luôn trả về PromQL — không để trống; ghi chú nguồn sinh query."""
    q = str(args.get("query") or "").strip()
    if q:
        return q, "explicit_query"
    intent = str(args.get("intent") or "").strip().lower()
    if not intent:
        hint = str(args.get("user_text") or args.get("hint") or "")
        intent = resolve_intent_from_keywords(hint)
    if not intent:
        intent = "cpu"
    tt = str(args.get("target_type") or "").strip().lower()
    node = args.get("node")
    node_s = str(node).strip() if node is not None and str(node).strip() else None
    if tt == "host":
        built, note, _meta = build_dynamic_promql("host", intent, node=node_s)
        return built, f"auto host {note}"
    if tt in ("kube_deployment", "kube_state_deployment"):
        ns = str(args.get("namespace") or "").strip() or _default_namespace(ctx)
        dep_raw = (
            args.get("deployment")
            if args.get("deployment") is not None
            else args.get("deployment_name")
        )
        dep_s = str(dep_raw).strip() if dep_raw is not None else ""
        if not dep_s:
            raise ValueError(
                "Thiếu deployment — kube_deployment cần namespace + deployment "
                "(deployment hoặc deployment_name)."
            )
        built, note, _meta = build_kube_state_promql(intent, namespace=ns, deployment=dep_s)
        return built, f"auto kube_deployment {note}"
    if tt == "kube_namespace":
        ns = str(args.get("namespace") or "").strip() or _default_namespace(ctx)
        built, note, _meta = build_kube_state_promql(intent, namespace=ns, deployment=None)
        return built, f"auto kube_namespace {note}"
    ns = str(args.get("namespace") or "").strip() or _default_namespace(ctx)
    pod_raw = args.get("pod_name") if args.get("pod_name") is not None else args.get("pod")
    pod_s = str(pod_raw).strip() if pod_raw is not None else ""
    if not pod_s:
        raise ValueError(
            "Thiếu pod_name — pod (cAdvisor) cần namespace + pod; hoặc dùng "
            "target_type=host | kube_deployment | kube_namespace."
        )
    built, note, _meta = build_dynamic_promql(
        "pod",
        intent,
        pod_name=pod_s,
        namespace=ns,
        node=node_s,
    )
    return built, f"auto pod {note} ns={ns}"


async def _vm_instant_scalar(ctx: Any, query: str) -> float | None:
    """Một scalar từ instant query (series đầu, value đầu)."""
    try:
        data = await _prometheus_get_json(ctx, "/api/v1/query", {"query": query})
    except Exception:
        return None
    if data.get("status") != "success":
        return None
    res = (data.get("data") or {}).get("result") or []
    if not res:
        return None
    v = res[0].get("value")
    if v and len(v) >= 2:
        return float(v[1])
    return None


async def tool_system_psutil(ctx: Any, args: dict[str, Any]) -> str:
    """CPU / RAM / disk / network qua psutil (SDK)."""

    def _read() -> str:
        cpu = psutil.cpu_percent(interval=0.25)
        vm = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        net = psutil.net_io_counters()
        lines = [
            f"CPU: {cpu:.1f}%",
            f"RAM: {vm.percent:.1f}% dùng ({vm.used // (1024**3)} / {vm.total // (1024**3)} GiB)",
            f"Disk /: {disk.percent:.1f}% ({disk.used // (1024**3)} / {disk.total // (1024**3)} GiB)",
            f"Net IO: bytes_sent={net.bytes_sent}, bytes_recv={net.bytes_recv}",
        ]
        return "\n".join(lines)

    return await asyncio.to_thread(_read)


async def tool_promql_instant(ctx: Any, args: dict[str, Any]) -> str:
    """Truy vấn PromQL instant qua Prometheus HTTP API (httpx). Thiếu ``query`` → tự sinh (pod cAdvisor / host / kube-state)."""
    # ValueError (thiếu pod/ns) → ném ra để slow_path LLM retry / đổi tool (không trả chuỗi tĩnh).
    query, src = resolve_promql_for_args(args, ctx)
    if is_placeholder_promql(query):
        inc_promql_placeholder_rejected()
        return (
            "[STATUS] error\n"
            "[DIAGNOSIS] PromQL placeholder — dùng metric thật (vd kube_*, node_*, up) hoặc bỏ args.query "
            "để tool tự sinh (target_type/intent/namespace/pod).\n"
            "[HINT] Cấm dùng tên biến metric_value hay threshold từ prompt.\n"
        )
    # region agent log
    _dbg_log(
        run_id="promql-instant",
        hypothesis_id="H5",
        location="sdk_service_tools.py:tool_promql_instant",
        message="promql_query_resolved",
        data={
            "source": src,
            "target_type": str(args.get("target_type") or ""),
            "has_explicit_query": bool(str(args.get("query") or "").strip()),
            "query_preview": query[:180],
        },
    )
    # endregion
    base = _prometheus_base_url(ctx)
    url = f"{base}/api/v1/query"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(url, params={"query": query})
            if r.status_code == 403:
                return "[STATUS] error\n[DATA] khong_co_quyen\n[DIAGNOSIS] " + _diagnosis_vm_403()
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        return "[STATUS] error\n" + _vm_user_facing_error(e)
    if data.get("status") != "success":
        return ("[STATUS] error\n" + f"Prometheus response: {data!s}")[:2000]
    rtype = (data.get("data") or {}).get("resultType", "")
    res = (data.get("data") or {}).get("result") or []
    if not res:
        # region agent log
        _dbg_log(
            run_id="promql-instant",
            hypothesis_id="H5",
            location="sdk_service_tools.py:tool_promql_instant",
            message="promql_empty_result",
            data={"source": src, "result_type": rtype, "query_preview": query[:180]},
        )
        # endregion
        # Contract cho LLM: phân biệt rõ query hợp lệ nhưng không có data.
        return (
            "[STATUS] empty_result\n"
            "Query đúng cú pháp nhưng không có dữ liệu nào khớp với điều kiện. "
            "Sự cố có thể đã tự hết hoặc mày đang query sai Label.\n"
            f"[DETAIL] result_type={rtype}\n"
            f"[DEBUG] PromQL: {query}"
        )
    lines = ["[STATUS] business_hit", f"resultType={rtype}"]
    for it in res[:20]:
        metric = it.get("metric") or {}
        val = it.get("value")
        lines.append(f"- {metric} @ {val}")
    if len(res) > 20:
        lines.append(f"... (+{len(res) - 20} kết quả)")
    lines.append(f"(promql_source={src})")
    return "\n".join(lines)


tool_vm_promql_instant = tool_promql_instant


async def tool_promql_range(ctx: Any, args: dict[str, Any]) -> str:
    """
    PromQL **range** (historical time-series) qua `/api/v1/query_range`.
    args: query (tuỳ chọn — intent/namespace/pod hoặc kube_deployment/kube_namespace), start, end, step.
    """
    query, src = resolve_promql_for_args(args, ctx)
    start = str(args.get("start") or "now-1h").strip()
    end = str(args.get("end") or "now").strip()
    step = str(args.get("step") or "30s").strip()
    params = {"query": query, "start": start, "end": end, "step": step}
    try:
        data = await _prometheus_get_json(ctx, "/api/v1/query_range", params)
    except Exception as e:
        return _vm_user_facing_error(e)
    if data.get("status") != "success":
        return "[DATA] error\n[DIAGNOSIS] Prometheus trả lỗi (đã ẩn chi tiết kỹ thuật)."
    ts, vals, nser = parse_vm_matrix_first_series(data)
    head = (
        f"resultType=matrix, n_chuỗi={nser}, n_points={len(vals)}, "
        f"start={start}, end={end}, step={step}\n"
    )
    if not vals:
        st = str(args.get("start") or "now-1h").strip()
        dur_lbl = st[4:] if st.startswith("now-") else "1h"
        return head + "[DIAGNOSIS] " + _diagnosis_vm_empty(args, dur_lbl, promql=query)
    preview = [f"promql_source={src}"]
    for i in range(min(15, len(vals))):
        preview.append(f"  t={ts[i]} v={vals[i]}")
    if len(vals) > 15:
        preview.append(f"  ... (+{len(vals) - 15} points)")
    return head + "\n".join(preview)


tool_vm_promql_range = tool_promql_range


async def tool_metrics_promql_hints(ctx: Any, args: dict[str, Any]) -> str:
    """Gợi ý PromQL cho Redis/node/disk — không gọi mạng; giúp tránh query sai tên metric."""
    topic = str(args.get("topic") or "all").lower().strip()
    if topic in ("redis", "memory", "cache"):
        return PROMQL_HINTS_MD.split("## Disk")[0].strip()
    if topic in ("disk", "iops", "node"):
        marker = "## Disk / IOPS (node_exporter)"
        idx = PROMQL_HINTS_MD.find(marker)
        if idx >= 0:
            return PROMQL_HINTS_MD[idx:].strip()
    return PROMQL_HINTS_MD


async def tool_timeseries_analyze(ctx: Any, args: dict[str, Any]) -> str:
    """
    Phân tích chuỗi số: thống kê, moving average, dự đoán tuyến tính (numpy).
    args: values (list hoặc CSV), ma_window (int, tuỳ chọn), forecast_steps (int, tuỳ chọn).
    """
    raw = args.get("values") or args.get("y")
    if raw is None:
        return "Thiếu args.values (danh sách số hoặc CSV)."
    if isinstance(raw, str):
        vals = [float(x.strip()) for x in raw.split(",") if x.strip()]
    else:
        vals = [float(x) for x in raw]
    ma_w = int(args.get("ma_window") or 0)
    fc = int(args.get("forecast_steps") or 0)
    out = analyze_series(vals, ma_window=ma_w, forecast_steps=fc)
    return json.dumps(out, ensure_ascii=False, indent=2)


async def tool_viz_vm_range_chart(ctx: Any, args: dict[str, Any]) -> str:
    """
    Lấy range từ Prometheus → vẽ matplotlib (lịch sử + dự đoán tuyến tính tuỳ chọn) → Telegram tuỳ chọn.
    args: query **hoặc** intent/namespace/pod; start, end, step, title?, forecast_steps?, send_telegram?, chat_id?
    """
    query, _src = resolve_promql_for_args(args, ctx)
    start = str(args.get("start") or "now-1h").strip()
    end = str(args.get("end") or "now").strip()
    step = str(args.get("step") or "30s").strip()
    title = str(args.get("title") or "Chart")
    title = title[:120]
    fc_steps = int(args.get("forecast_steps") or 0)
    try:
        data = await _prometheus_get_json(
            ctx,
            "/api/v1/query_range",
            {"query": query, "start": start, "end": end, "step": step},
        )
    except Exception as e:
        return _vm_user_facing_error(e)
    if data.get("status") != "success":
        return "[DATA] error\n[DIAGNOSIS] Prometheus trả lỗi (đã ẩn chi tiết kỹ thuật)."
    ts, vals, nser = parse_vm_matrix_first_series(data)
    if not vals:
        st = str(args.get("start") or "now-1h").strip()
        dur_lbl = st[4:] if st.startswith("now-") else "1h"
        return "[DATA] no_data\n[DIAGNOSIS] " + _diagnosis_vm_empty(args, dur_lbl, promql=query)
    x_hist = list(range(len(vals)))
    x_fore = None
    y_fore = None
    if fc_steps > 0 and len(vals) >= 2:
        x = np.arange(len(vals), dtype=float)
        y = np.asarray(vals, dtype=float)
        a, b = np.polyfit(x, y, 1)
        fx = np.arange(len(vals), len(vals) + fc_steps, dtype=float)
        fy = a * fx + b
        x_fore = [float(v) for v in fx]
        y_fore = [float(v) for v in fy]
    png = line_chart_history_forecast_png_bytes(
        x_hist,
        vals,
        x_fore,
        y_fore,
        title=title,
        xlabel="sample index",
        ylabel="value",
    )
    send = should_send_telegram_chart(ctx, args)
    cid = effective_telegram_chat_id(ctx, args)
    tg = getattr(ctx, "telegram", None)
    if send and tg is not None and cid is not None:
        try:
            await tg.send_photo_bytes(cid, png, caption=title[:200])
            return (
                f"Đã gửi biểu đồ ({len(vals)} điểm, {nser} chuỗi trong phản hồi, "
                f"forecast_steps={fc_steps}) lên Telegram chat_id={cid}. "
                f"flow: Prometheus → matplotlib → Telegram sendPhoto"
            )
        except Exception as e:
            return f"Vẽ OK ({len(png)} bytes) nhưng Telegram lỗi: {e!s}"
    return (
        f"PNG {len(png)} bytes — {len(vals)} điểm, n_chuỗi={nser}, forecast_steps={fc_steps}. "
        "Có chat Telegram → chart tự gửi; không thì chỉ text."
    )


async def tool_system_psutil_diskio(ctx: Any, args: dict[str, Any]) -> str:
    """Đọc I/O đĩa (read/write count & bytes) qua psutil; ước lượng ops/s sau ~0.5s."""

    def _read() -> str:
        interval = float(args.get("sample_sec") or 0.5)
        interval = max(0.2, min(2.0, interval))
        a = psutil.disk_io_counters(perdisk=True)
        time.sleep(interval)
        b = psutil.disk_io_counters(perdisk=True)
        if not a or not b:
            return "disk_io_counters không khả dụng trên platform này."
        lines = [f"Mẫu delta ~{interval:.2f}s — read/write count ≈ IOPS nếu kernel báo ops."]
        keys = sorted(set(a.keys()) | set(b.keys()))
        for name in keys[:16]:
            pa, pb = a.get(name), b.get(name)
            if pa is None or pb is None:
                continue
            dr = pb.read_count - pa.read_count
            dw = pb.write_count - pa.write_count
            dbr = pb.read_bytes - pa.read_bytes
            dbw = pb.write_bytes - pa.write_bytes
            riops = dr / interval
            wiops = dw / interval
            lines.append(
                f"- {name}: read_ops/s≈{riops:.1f}, write_ops/s≈{wiops:.1f}, "
                f"read_B/s={dbr/interval:.0f}, write_B/s={dbw/interval:.0f}"
            )
        if len(keys) > 16:
            lines.append(f"... (+{len(keys) - 16} disk)")
        return "\n".join(lines)

    return await asyncio.to_thread(_read)


def _fmt_slowlog_entry(entry: Any) -> str:
    if isinstance(entry, dict):
        cmd = entry.get("command")
        if isinstance(cmd, (bytes, bytearray)):
            cmd = cmd.decode("utf-8", errors="replace")
        return f"id={entry.get('id')} dur_ms={entry.get('duration')} cmd={cmd!s}"
    dur = getattr(entry, "duration", None)
    eid = getattr(entry, "id", None)
    cmd = getattr(entry, "command", None)
    if dur is not None:
        if isinstance(cmd, (bytes, bytearray)):
            cmd = cmd.decode("utf-8", errors="replace")
        return f"id={eid} dur_ms={dur} cmd={cmd!s}"
    return repr(entry)


async def tool_redis_health(ctx: Any, args: dict[str, Any]) -> str:
    """
    Sức khỏe Redis (redis.asyncio): INFO memory/clients/stats + slowlog + MEMORY MALLOC STATS.
    Chỉ số vận hành — không định nghĩa lý thuyết.
    """
    r = getattr(ctx, "redis", None)
    if r is None:
        return "Không có ctx.redis."
    lines: list[str] = ["=== redis_health (SDK) ==="]
    try:
        mem = await r.info("memory")
        cli = await r.info("clients")
        st = await r.info("stats")
        rep = await r.info("replication")
    except Exception as e:
        return f"Lỗi Redis INFO: {e!s}"
    lines.append(
        f"role={rep.get('role')} used_memory_human={mem.get('used_memory_human')} "
        f"used_memory={mem.get('used_memory')} rss={mem.get('used_memory_rss')} "
        f"frag_ratio={mem.get('mem_fragmentation_ratio')}"
    )
    lines.append(
        f"clients connected={cli.get('connected_clients')} blocked={cli.get('blocked_clients')}"
    )
    lines.append(
        f"ops instantaneous_ops_per_sec={st.get('instantaneous_ops_per_sec')} "
        f"total_commands={st.get('total_commands_processed')}"
    )
    try:
        slow = await r.slowlog_get(10)
        lines.append("slowlog_last10:")
        for ent in slow[:10]:
            lines.append(f"  {_fmt_slowlog_entry(ent)}")
    except Exception as e:
        lines.append(f"slowlog: {e!s}")
    try:
        malloc = await r.execute_command("MEMORY", "MALLOC", "STATS")
        ms = str(malloc)
        if len(ms) > 2000:
            ms = ms[:2000] + "..."
        lines.append(f"memory_malloc_stats: {ms}")
    except Exception as e:
        lines.append(f"memory_malloc_stats: không lấy được ({e!s})")
    return "\n".join(lines)


async def tool_redis_info(ctx: Any, args: dict[str, Any]) -> str:
    """Alias: `redis_health` (INFO đầy đủ một section — tương thích)."""
    if args.get("section"):
        r = getattr(ctx, "redis", None)
        if r is None:
            return "Không có ctx.redis."
        try:
            info = await r.info(str(args["section"]))
            text = json.dumps(info, ensure_ascii=False, default=str, indent=2)
            return text[:6000] + ("\n... (truncated)" if len(text) > 6000 else "")
        except Exception as e:
            return f"Lỗi Redis INFO: {e!s}"
    return await tool_redis_health(ctx, args)


async def tool_pgvector_health(ctx: Any, args: dict[str, Any]) -> str:
    """Kiểm tra bắt buộc `itops_sop_ledger` / `itops_error_ledger` qua Postgres HA."""
    store = getattr(ctx, "vector_store", None)
    if store is None:
        return "Không có ctx.vector_store (Postgres)."
    try:
        async with store._pool.acquire() as conn:
            rows = await conn.fetch("SELECT DISTINCT collection_name FROM rag_documents")
            names = [r["collection_name"] for r in rows]
            
        lines: list[str] = ["=== pgvector_health (Postgres HA) ===", f"collections={names}"]
        for must in (COLLECTION_SOP, COLLECTION_ERRORS):
            if must in names:
                async with store._pool.acquire() as conn:
                    cnt = await conn.fetchval("SELECT count(*) FROM rag_documents WHERE collection_name = $1", must)
                lines.append(f"OK {must}: points_count={cnt}")
            else:
                lines.append(f"MISSING {must} (Postgres partition empty)")
        return "\n".join(lines)
    except Exception as e:
        return f"Lỗi Postgres pgvector: {e!s}"


async def tool_pgvector_status(ctx: Any, args: dict[str, Any]) -> str:
    return await tool_pgvector_health(ctx, args)

async def tool_pgvector_health_audit(ctx: Any, args: dict[str, Any]) -> str:
    return await tool_pgvector_health(ctx, args)


async def tool_query_historical_metrics(ctx: Any, args: dict[str, Any]) -> str:
    """
    Prometheus range (httpx) → tóm tắt ngắn + matplotlib → Telegram (không chữ dài).
    args: query **hoặc** intent/namespace/pod; start (vd now-24h), end (now), step (5m), title?, send_telegram?, chat_id?
    """
    query, _src = resolve_promql_for_args(args, ctx)
    start = str(args.get("start") or "now-24h").strip()
    end = str(args.get("end") or "now").strip()
    step = str(args.get("step") or "5m").strip()
    title = str(args.get("title") or "Chart")[:120]
    try:
        data = await _prometheus_get_json(
            ctx,
            "/api/v1/query_range",
            {"query": query, "start": start, "end": end, "step": step},
        )
    except Exception as e:
        return _vm_user_facing_error(e)
    if data.get("status") != "success":
        return "[DATA] error\n[DIAGNOSIS] Prometheus trả lỗi (đã ẩn chi tiết kỹ thuật)."
    ts, vals, nser = parse_vm_matrix_first_series(data)
    if not vals:
        st = str(args.get("start") or "now-24h").strip()
        dur_lbl = st[4:] if st.startswith("now-") else "24h"
        return "[DATA] no_data\n[DIAGNOSIS] " + _diagnosis_vm_empty(args, dur_lbl, promql=query)
    t0 = ts[0]
    x_hours = [(t - t0) / 3600.0 for t in ts]
    png = line_chart_png_bytes(x_hours, vals, title=title, xlabel="t (giờ từ đầu)", ylabel="giá trị")
    send = should_send_telegram_chart(ctx, args)
    cid = effective_telegram_chat_id(ctx, args)
    tg = getattr(ctx, "telegram", None)
    sent = False
    if send and tg is not None and cid is not None:
        try:
            cap = f"{title} | n={len(vals)} min={min(vals):.4g} max={max(vals):.4g}"
            await tg.send_photo_bytes(cid, png, caption=cap[:200])
            sent = True
        except Exception as e:
            return f"Vẽ OK ({len(png)} B) nhưng Telegram: {e!s}"
    flow = "flow: Prometheus → matplotlib → " + ("Telegram sendPhoto" if sent else "PNG only (text)")
    summary = (
        f"n={len(vals)} n_chuỗi={nser} min={min(vals):.6g} max={max(vals):.6g} "
        f"last={vals[-1]:.6g} chart_telegram={'sent' if sent else 'no'}\n{flow}"
    )
    return summary


def _resolve_promql_or_explicit(args: dict[str, Any], ctx: Any) -> tuple[str, str]:
    q = str(args.get("query") or "").strip()
    if q:
        return q, "explicit_query"
    return resolve_promql_for_args(args, ctx)


async def tool_get_historical_series_dataframe(ctx: Any, args: dict[str, Any]) -> str:
    """
    Prometheus range → **pandas** (nội bộ); trả **JSON** gọn: ``n_points``, ``preview_rows``, min/max/last.
    Khác ``query_historical_metrics`` (chart + Telegram). Dùng làm đầu vào pipeline Prophet.

    Args: ``query`` hoặc intent/namespace/pod như ``promql_range``; ``duration`` (vd ``24h``), ``start``, ``end``, ``step``.
    """
    promql, src = _resolve_promql_or_explicit(args, ctx)
    duration = str(args.get("duration") or "24h").strip()
    start, step = _duration_to_vm_window(duration)
    end = str(args.get("end") or "now").strip()
    if args.get("start"):
        start = str(args["start"]).strip()
    if args.get("step"):
        step = str(args["step"]).strip()
    try:
        df = await fetch_range_dataframe(
            ctx,
            promql=promql,
            start=start,
            end=end,
            step=step,
        )
    except Exception as e:
        return _vm_user_facing_error(e)
    if df.empty:
        return (
            "[DATA] no_data\n[DIAGNOSIS] Prometheus matrix rỗng — kiểm tra PromQL và scrape.\n"
            f"[DEBUG] promql={promql}"
        )
    preview_rows: list[dict[str, Any]] = []
    for i in range(min(5, len(df))):
        row = df.iloc[i]
        ds = row["ds"]
        preview_rows.append(
            {
                "ds": ds.isoformat() if hasattr(ds, "isoformat") else str(ds),
                "y": float(row["y"]),
            }
        )
    payload: dict[str, Any] = {
        "promql_source": src,
        "promql": promql,
        "start": start,
        "end": end,
        "step": step,
        "n_points": len(df),
        "series_note": "first_matrix_series",
        "preview_rows": preview_rows,
        "y_min": float(df["y"].min()),
        "y_max": float(df["y"].max()),
        "y_last": float(df["y"].iloc[-1]),
    }
    return "[DATA]\n" + json.dumps(payload, ensure_ascii=False, indent=2)


async def tool_forecast_metric_prophet(ctx: Any, args: dict[str, Any]) -> str:
    """
    Lấy chuỗi Prometheus → **Prophet** (hoặc fallback tuyến tính) → JSON meta; tuỳ chọn chart (thực tế + dải tin cậy) → Telegram.

    Args: cùng nguồn query như ``get_historical_series_dataframe``; ``periods`` hoặc ``horizon_hours`` (mặc định 1);
    ``send_telegram``, ``chat_id``, ``title``.
    """
    promql, src = _resolve_promql_or_explicit(args, ctx)
    duration = str(args.get("duration") or "24h").strip()
    start, step = _duration_to_vm_window(duration)
    end = str(args.get("end") or "now").strip()
    if args.get("start"):
        start = str(args["start"]).strip()
    if args.get("step"):
        step = str(args["step"]).strip()
    try:
        df = await fetch_range_dataframe(
            ctx,
            promql=promql,
            start=start,
            end=end,
            step=step,
        )
    except Exception as e:
        return _vm_user_facing_error(e)
    if len(df) < 2:
        return "[DATA] no_data\n[DIAGNOSIS] Cần ít nhất 2 điểm để dự báo."

    periods = int(args.get("periods") or 0)
    if periods <= 0:
        hh = float(args.get("horizon_hours") or 1.0)
        periods = horizons_to_periods(hh, step)
    pandas_freq = step_to_pandas_freq(step)
    try:
        fc, backend = forecast_backend_used(df, periods, freq=pandas_freq)
    except Exception as e:
        return f"[DATA] error\n[DIAGNOSIS] forecast_metric: {e!s}"

    yhat_max = float(fc["yhat"].max())
    yhat_last = float(fc["yhat"].iloc[-1])
    out: dict[str, Any] = {
        "promql_source": src,
        "promql": promql,
        "backend": backend,
        "periods": periods,
        "yhat_max": yhat_max,
        "yhat_last": yhat_last,
        "forecast_preview": [
            {
                "ds": r["ds"].isoformat() if hasattr(r["ds"], "isoformat") else str(r["ds"]),
                "yhat": float(r["yhat"]),
                "yhat_lower": float(r["yhat_lower"]),
                "yhat_upper": float(r["yhat_upper"]),
            }
            for _, r in fc.head(min(8, len(fc))).iterrows()
        ],
    }
    title = str(args.get("title") or "Prophet forecast")[:120]
    send = should_send_telegram_chart(ctx, args)
    cid = effective_telegram_chat_id(ctx, args)
    tg = getattr(ctx, "telegram", None)
    chart_note = ""
    if send and tg is not None and cid is not None:
        try:
            png = line_chart_history_forecast_ci_png_bytes(
                df["ds"].tolist(),
                df["y"].astype(float).tolist(),
                fc["ds"].tolist(),
                fc["yhat"].astype(float).tolist(),
                fc["yhat_lower"].astype(float).tolist(),
                fc["yhat_upper"].astype(float).tolist(),
                title=title,
            )
            cap = f"{title} | backend={backend} | n_hist={len(df)} n_fc={len(fc)}"
            await tg.send_photo_bytes(cid, png, caption=cap[:200])
            chart_note = " telegram_sent=true"
        except Exception as e:
            chart_note = f" telegram_error={e!s}"

    return (
        "[DATA]\n"
        + json.dumps(out, ensure_ascii=False, indent=2)
        + f"\n[DIAGNOSIS] Dự báo ({backend}).{chart_note}"
    )


async def tool_forecast_memory_risk_vm(ctx: Any, args: dict[str, Any]) -> str:
    """
    Chuỗi RAM từ Prometheus (24h) + scipy linregress → áp lực/OOM trong horizon_hours (mặc định 6h).
    args: query **hoặc** namespace/pod (tự sinh PromQL RAM pod/cAdvisor), kind=usage|available, start=now-24h, end=now, step=5m,
    horizon_hours=6, total_ram_bytes? (hoặc total_promql), total_ram_gib?

    Ví dụ capacity (kube-state, không phải RAM container): dùng ``query_prometheus_metrics`` với
    ``target_type=kube_deployment``, ``intent=replica_ratio``, ``deployment=...``, ``namespace=...``.
    """
    query = str(args.get("query") or "").strip()
    if not query:
        ns = str(args.get("namespace") or _default_namespace(ctx)).strip()
        pod_raw = args.get("pod_name") if args.get("pod_name") is not None else args.get("pod")
        pod_s = str(pod_raw).strip() if pod_raw is not None else ""
        query = build_promql_from_intent("ram", namespace=ns, pod_name=pod_s or None)
    kind = str(args.get("kind") or "usage").strip().lower()
    if kind not in ("usage", "available"):
        kind = "usage"
    start = str(args.get("start") or "now-24h").strip()
    end = str(args.get("end") or "now").strip()
    step = str(args.get("step") or "5m").strip()
    horizon = float(args.get("horizon_hours") or 6.0)

    try:
        data = await _prometheus_get_json(
            ctx,
            "/api/v1/query_range",
            {"query": query, "start": start, "end": end, "step": step},
        )
    except Exception as e:
        return _vm_user_facing_error(e)
    if data.get("status") != "success":
        return "[DATA] error\n[DIAGNOSIS] Prometheus trả lỗi (đã ẩn chi tiết kỹ thuật)."
    ts, vals, nser = parse_vm_matrix_first_series(data)
    if len(vals) < 3:
        return (
            f"[DATA] no_data\n[DIAGNOSIS] Em đã query nhưng không đủ điểm để phân tích "
            f"(n={len(vals)}). Đại ca check Pod/namespace và Prometheus scrape.\n[DEBUG] PromQL: {query}"
        )

    total_raw = args.get("total_ram_bytes")
    total_gib = args.get("total_ram_gib")
    total_bytes: float | None = None
    if total_raw is not None:
        total_bytes = float(total_raw)
    elif total_gib is not None:
        total_bytes = float(total_gib) * (1024**3)
    else:
        tp = str(args.get("total_promql") or "node_memory_MemTotal_bytes").strip()
        tv = await _vm_instant_scalar(ctx, tp)
        if tv is not None:
            total_bytes = float(tv)

    if total_bytes is None or total_bytes <= 0:
        return (
            "Thiếu tổng RAM: truyền total_ram_gib / total_ram_bytes hoặc total_promql "
            "(instant trả 1 byte MemTotal)."
        )

    step_sec = series_step_seconds(ts)
    out = oom_risk_from_series(
        vals,
        total_ram_bytes=total_bytes,
        step_seconds=step_sec,
        horizon_hours=horizon,
        kind=("usage" if kind == "usage" else "available"),
    )
    return json.dumps(out, ensure_ascii=False, indent=2)


def _duration_to_vm_window(duration: str) -> tuple[str, str]:
    """duration '1h'|'24h'|'30m' → (start, step)."""
    d = duration.strip().lower()
    if d.endswith("h") and len(d) > 1 and d[:-1].replace(".", "").isdigit():
        step = "30s" if float(d[:-1]) <= 6 else "5m"
        return f"now-{d}", step
    if d.endswith("m") and len(d) > 1 and d[:-1].isdigit():
        return f"now-{d}", "15s"
    return "now-1h", "30s"


async def tool_query_prometheus_metrics(ctx: Any, args: dict[str, Any]) -> str:
    """
    **Prometheus range** — luôn có chart; **PromQL là tuỳ chọn** (tự sinh từ intent).

    **Pod (cAdvisor ``container_*``)** — cần ``namespace`` + ``pod_name``:

    ``sum(rate(container_cpu_usage_seconds_total{{namespace="<ns>",pod=~"<workload>.*"}}[5m]))``

    **RAM (working set)**:

    ``sum(container_memory_working_set_bytes{{namespace="<ns>",pod=~"<workload>.*"}})``

    **kube-state-metrics** — ``target_type=kube_deployment``: ``namespace`` + ``deployment`` (tỉ lệ replica, v.v.);
    ``target_type=kube_namespace``: chỉ ``namespace`` (đếm pod Running/Pending).

    Args chính:
      - ``query``: PromQL tùy chỉnh (nếu có thì bỏ qua intent).
      - ``target_type``: ``pod`` | ``host`` | ``kube_deployment`` | ``kube_namespace``.
      - ``intent``: cpu/ram/… hoặc ``replica_ratio`` / ``pods_running`` / ``pods_pending`` (kube).
      - ``namespace``, ``pod_name`` (pod); ``deployment`` (kube deployment).
      - ``duration``: ``1h``, ``24h`` — cửa sổ lịch sử (mặc định 1h).
      - ``forecast`` / ``forecast_next``: ``true`` → dự báo tuyến tính (pandas).
      - ``send_telegram``, ``chat_id``, ``title``.

    **Endpoint:** ``OMNI_PROMETHEUS_URL`` (legacy ``OMNI_VICTORIA_METRICS_URL``) —
    mặc định ``prometheus.monitor.svc.cluster.local:9090`` (tự thêm ``http://`` nếu env thiếu scheme).

    Không trả lời user bằng "thiếu PromQL" — suy ra từ intent và target.
    """
    return await _query_timeseries_impl(ctx, args)


tool_query_victoria_metrics = tool_query_prometheus_metrics


async def tool_query_vm_timeseries(ctx: Any, args: dict[str, Any]) -> str:
    """
    Alias của ``query_prometheus_metrics`` / ``query_victoria_metrics`` — range + chart.
    """
    return await _query_timeseries_impl(ctx, args)


async def _query_timeseries_impl(ctx: Any, args: dict[str, Any]) -> str:
    query, _auto_note = resolve_promql_for_args(args, ctx)
    duration = str(args.get("duration") or "1h").strip()
    start, step = _duration_to_vm_window(duration)
    end = "now"
    title_raw = str(args.get("title") or "").strip()
    tt = str(args.get("target_type") or "").strip().lower()
    if title_raw:
        title = title_raw[:120]
    elif tt == "host":
        title = "Host — métric"
    elif tt in ("kube_deployment", "kube_state_deployment"):
        dep_t = str(args.get("deployment") or args.get("deployment_name") or "").strip()
        ns_t = str(args.get("namespace") or "").strip()
        title = (f"{dep_t}@{ns_t} — kube-state" if dep_t else "kube-state")[:120]
    elif tt == "kube_namespace":
        ns_t = str(args.get("namespace") or "").strip()
        title = (f"{ns_t} — kube pod phase" if ns_t else "kube-namespace")[:120]
    else:
        pod_t = str(args.get("pod_name") or args.get("pod") or "").strip()
        title = (f"{pod_t} — métric" if pod_t else "Prometheus")[:120]
    forecast = bool(args.get("forecast") or args.get("forecast_next") or args.get("predict"))
    fh = str(args.get("forecast_horizon") or args.get("forecast_duration") or "1h").strip()
    try:
        data = await _prometheus_get_json(
            ctx,
            "/api/v1/query_range",
            {"query": query, "start": start, "end": end, "step": step},
        )
    except Exception as e:
        return _vm_user_facing_error(e)
    if data.get("status") != "success":
        return "[DATA] error\n[DIAGNOSIS] Prometheus trả lỗi (đã ẩn chi tiết kỹ thuật)."
    ts, vals, nser = parse_vm_matrix_first_series(data)
    if not vals:
        base = "[DATA] no_data\n[DIAGNOSIS] " + _diagnosis_vm_empty(args, duration, promql=query)
        try:
            from workers.observability_audit import tool_audit_observability_stack

            audit = await tool_audit_observability_stack(
                ctx,
                {
                    "pod_name": args.get("pod_name") or args.get("pod"),
                    "namespace": args.get("namespace"),
                },
            )
            return base + "\n[AUDIT_STACK]\n" + audit
        except Exception as e:
            logger.warning("audit_observability_stack after no_data: %s", e)
            return base

    step_sec = series_step_seconds(ts)
    frame: dict[str, Any] = {
        "n_points": len(vals),
        "n_series_trong_phan_hoi": nser,
        "min": min(vals),
        "max": max(vals),
        "last": vals[-1],
        "start": start,
        "step": step,
    }
    trend_block: dict[str, Any] = {}
    if forecast and len(vals) >= 2:
        h_steps = forecast_horizon_steps(fh, step_sec)
        _pred_y, trend_block = pandas_trend_forecast(vals, horizon_steps=h_steps)
        t0 = ts[0]
        x_hist = [(t - t0) / 3600.0 for t in ts]
        last_t = ts[-1]
        ts_fore = [last_t + (i + 1) * step_sec for i in range(h_steps)]
        x_fore = [(t - t0) / 3600.0 for t in ts_fore]
        y_fore = [float(_pred_y[i]) for i in range(h_steps)]
        png = line_chart_history_forecast_png_bytes(
            x_hist,
            vals,
            x_fore,
            y_fore,
            title=f"{title} (history + forecast)",
            xlabel="t (giờ từ mốc đầu)",
            ylabel="value",
        )
        frame["forecast"] = True
        frame["forecast_horizon"] = fh
        frame["forecast_horizon_steps"] = h_steps
        frame["trend_meta"] = trend_block
    else:
        png = prometheus_timeseries_to_line_chart_png_bytes(ts, vals, title=title)
        frame["forecast"] = False

    send = should_send_telegram_chart(ctx, args)
    cid = effective_telegram_chat_id(ctx, args)
    tg = getattr(ctx, "telegram", None)
    sent = False
    if send and tg is not None and cid is not None:
        try:
            await tg.send_photo_bytes(cid, png, caption=title[:200])
            sent = True
        except Exception as e:
            return f"[DATA] {json.dumps(frame, ensure_ascii=False)}\n[DIAGNOSIS] Chart OK nhưng Telegram: {e!s}"
    if frame.get("forecast"):
        diag = "Đã vẽ lịch sử (nét liền) + dự báo tuyến tính (nét đứt, pandas)."
    else:
        diag = "Chuỗi thời gian ổn; đã vẽ line chart."
    if max(vals) > 0 and min(vals) > 0 and max(vals) / max(min(vals), 1e-9) > 2:
        diag += " Biến động lớn — xem chart."
    flow = "flow: Prometheus query_range → matplotlib → " + ("Telegram sendPhoto" if sent else "PNG trong reply text")
    return (
        "[DATA]\n"
        + json.dumps(frame, ensure_ascii=False)
        + f"\nmau_gia_tri_gan_day={vals[:8]!s}\nchart_png_bytes={len(png)} telegram_sent={sent}\n{flow}\n"
        + "[DIAGNOSIS]\n"
        + diag
    )


async def tool_redis_expert_check(ctx: Any, args: dict[str, Any]) -> str:
    """memory INFO + slowlog 5 + maxmemory + tỉ lệ fragmentation (mem_fragmentation_ratio)."""
    r = getattr(ctx, "redis", None)
    if r is None:
        return "[DATA] no_redis\n[DIAGNOSIS] Không có ctx.redis."
    try:
        mem = await r.info("memory")
        slow = await r.slowlog_get(5)
        cfg = await r.config_get("maxmemory")
        frag = float(mem.get("mem_fragmentation_ratio") or 0.0)
        maxmem = cfg.get("maxmemory") if isinstance(cfg, dict) else str(cfg)
        used = mem.get("used_memory_human")
        rss = mem.get("used_memory_rss")
        dataset = mem.get("used_memory_dataset")
        frag_note = (
            f"fragmentation_ratio={frag:.3f} (RSS vs dataset; >1.5 thường cần active defrag nếu bật)"
        )
        slow_lines = []
        for ent in slow[:5]:
            if isinstance(ent, dict):
                cmd = ent.get("command")
                if isinstance(cmd, (bytes, bytearray)):
                    cmd = cmd.decode("utf-8", errors="replace")
                slow_lines.append(f"id={ent.get('id')} ms={ent.get('duration')} {cmd!s}")
            else:
                slow_lines.append(repr(ent))
        data = {
            "used_memory_human": used,
            "used_memory_rss": rss,
            "used_memory_dataset": dataset,
            "mem_fragmentation_ratio": frag,
            "maxmemory_config": maxmem,
            "slowlog_last5": slow_lines,
        }
        diag = "Redis memory ổn."
        if frag > 1.6:
            diag = "Fragmentation cao — xem cấu hình active defrag / restart plan."
        return "[DATA]\n" + json.dumps(data, ensure_ascii=False, indent=2) + "\n[DIAGNOSIS]\n" + diag
    except Exception as e:
        return f"[DATA] error\n[DIAGNOSIS] {e!s}"


async def tool_predict_resource_exhaustion(ctx: Any, args: dict[str, Any]) -> str:
    """
    24h Prometheus series → xu hướng tuyến tính → thời điểm cắt ngưỡng 90% của max quan sát.
    args: metric_name/query **hoặc** intent/namespace/pod (cAdvisor) — hoặc ``target_type=kube_deployment`` + deployment/namespace
    (tỉ lệ replica từ kube-state), horizon (vd 6h), step=5m
    """
    promql = str(args.get("metric_name") or args.get("query") or "").strip()
    if not promql:
        promql, _src = resolve_promql_for_args(args, ctx)
    horizon_h = float(str(args.get("horizon") or "6h").replace("h", "").strip() or "6")
    start = "now-24h"
    end = "now"
    step = str(args.get("step") or "5m").strip()
    try:
        data = await _prometheus_get_json(
            ctx,
            "/api/v1/query_range",
            {"query": promql, "start": start, "end": end, "step": step},
        )
    except Exception as e:
        return _vm_user_facing_error(e)
    if data.get("status") != "success":
        return "[DATA] error\n[DIAGNOSIS] Prometheus trả lỗi (đã ẩn chi tiết kỹ thuật)."
    _ts, vals, nser = parse_vm_matrix_first_series(data)
    if len(vals) < 4:
        return (
            "[DATA] no_data\n[DIAGNOSIS] Em đã query nhưng không đủ điểm để dự báo tuyến tính. "
            "Đại ca check Pod có hoạt động và Prometheus scrape không.\n[DEBUG] PromQL: "
            f"{promql}"
        )

    y = np.asarray(vals, dtype=float)
    max_y = float(np.max(y))
    target = 0.9 * max_y
    x = np.arange(len(y), dtype=float)
    slope, intercept = np.polyfit(x, y, 1)
    if slope <= 0:
        return (
            "[DATA]\n"
            + json.dumps(
                {"max_observed": max_y, "target_90pct": target, "slope": float(slope)},
                ensure_ascii=False,
            )
            + "\n[DIAGNOSIS]\nXu hướng không tăng — không dự báo cạn kiệt theo mô hình tuyến tính."
        )
    x_cross = (target - intercept) / slope
    step_sec = series_step_seconds(_ts if _ts else [0.0, 300.0])
    hours_to = max(0.0, (x_cross - (len(y) - 1)) * step_sec / 3600.0)
    risk = bool(hours_to <= horizon_h and x_cross > len(y) - 1)
    out = {
        "max_observed": max_y,
        "threshold_90pct_of_max": target,
        "slope_per_step": float(slope),
        "hours_to_threshold_if_linear": round(hours_to, 2),
        "horizon_hours_check": horizon_h,
        "risk_before_horizon": risk,
    }
    diag = (
        f"Dự báo đạt ~90% max quan sát sau ~{hours_to:.1f}h (tuyến tính)."
        if risk
        else f"Trong {horizon_h}h không chạm ngưỡng 90% max theo xu hướng hiện tại."
    )
    return "[DATA]\n" + json.dumps(out, ensure_ascii=False, indent=2) + "\n[DIAGNOSIS]\n" + diag


async def tool_viz_line_chart(ctx: Any, args: dict[str, Any]) -> str:
    """
    Vẽ line chart (matplotlib → RAM) và tuỳ chọn gửi ảnh Telegram.
    args: title, y (list số hoặc CSV), x (tuỳ chọn CSV), send_telegram (bool), chat_id (int).
    """
    title = str(args.get("title") or "Chart")
    ys_raw = args.get("y") or args.get("values")
    if ys_raw is None:
        return "Thiếu args.y (danh sách số)."
    if isinstance(ys_raw, str):
        ys = [float(x.strip()) for x in ys_raw.split(",") if x.strip()]
    else:
        ys = [float(x) for x in ys_raw]
    xs_raw = args.get("x")
    if xs_raw is not None:
        if isinstance(xs_raw, str):
            xs = [float(x.strip()) for x in xs_raw.split(",") if x.strip()]
        else:
            xs = [float(x) for x in xs_raw]
        if len(xs) != len(ys):
            return "args.x và args.y phải cùng độ dài."
    else:
        xs = list(range(len(ys)))

    png = line_chart_png_bytes(xs, ys, title=title)
    send = should_send_telegram_chart(ctx, args)
    cid = effective_telegram_chat_id(ctx, args)
    tg = getattr(ctx, "telegram", None)
    if send and tg is not None and cid is not None:
        try:
            await tg.send_photo_bytes(cid, png, caption=title[:200])
            return (
                f"Đã gửi biểu đồ PNG ({len(png)} bytes) lên Telegram chat_id={cid}. "
                f"flow: viz_line_chart → matplotlib → Telegram sendPhoto"
            )
        except Exception as e:
            return f"Vẽ OK ({len(png)} bytes) nhưng gửi Telegram lỗi: {e!s}"
    return f"Biểu đồ PNG đã tạo ({len(png)} bytes). Có chat_id Telegram → tự gửi khi mặc định."


async def tool_net_scapy_interfaces(ctx: Any, args: dict[str, Any]) -> str:
    """Danh sách interface (scapy); không quét port bằng shell."""
    try:
        from scapy.all import get_if_list  # noqa: PLC0415

        names = get_if_list()
        return "Interfaces (scapy): " + ", ".join(names) if names else "(trống)"
    except Exception as e:
        return f"scapy không khả dụng hoặc cần quyền: {e!s}"


async def tool_postgres_ping(ctx: Any, args: dict[str, Any]) -> str:
    """Thử kết nối Postgres qua asyncpg nếu OMNI_POSTGRES_DSN có."""
    s = getattr(ctx, "settings", None)
    dsn = (getattr(s, "postgres_dsn", None) or "").strip() if s else ""
    if not dsn:
        return "Chưa cấu hình OMNI_POSTGRES_DSN — bỏ qua."
    try:
        import asyncpg  # noqa: PLC0415

        conn = await asyncpg.connect(dsn, timeout=10.0)
        try:
            v = await conn.fetchval("SELECT 1")
            return f"PostgreSQL OK (SELECT 1 → {v})."
        finally:
            await conn.close()
    except Exception as e:
        return f"Lỗi asyncpg: {e!s}"


async def tool_vendor_knowledge_search(ctx: Any, args: dict[str, Any]) -> str:
    """RAG trên ``vendor_knowledge`` (docs đã ingest). Không thay kubectl/SDK."""
    from workers.handlers import _embedding_from_response
    from rag.pgvector_store import COLLECTION_VENDOR_KNOWLEDGE

    q = str(args.get("query") or "").strip()
    if not q:
        return "[ERROR] query required"
    layer = args.get("layer")
    limit = int(args.get("limit") or 5)
    st = args.get("score_threshold")
    score_threshold = float(st) if st is not None else None
    emb_resp = await ctx.llm.embed(
        model=ctx.settings.embed_model,
        input=q[:8000],
    )
    vec = _embedding_from_response(emb_resp)
    pf = None
    if isinstance(layer, str) and layer.strip():
        pf = {"layer": layer.strip()}
    resp = await ctx.vector_store.query_points(
        collection_name=COLLECTION_VENDOR_KNOWLEDGE,
        query=vec,
        limit=limit,
        score_threshold=score_threshold,
        payload_filters=pf,
    )
    lines: list[str] = []
    for p in resp.points or []:
        pay = p.payload or {}
        score = getattr(p, "score", None)
        src = pay.get("source_url") or pay.get("source_path") or ""
        cite = (pay.get("citation_text") or pay.get("embed_text") or "")[:500]
        sc = f"{float(score):.4f}" if score is not None else "?"
        lines.append(f"score={sc} src={src}\n{cite}\n---")
    if not lines:
        return "[DATA] no vendor knowledge hits"
    return "[DATA] vendor_knowledge_search\n" + "\n".join(lines)


async def tool_k8s_expert_search(ctx: Any, args: dict[str, Any]) -> str:
    """Semantic RAG trên collection expert (kubernetes.io ingest + local); ``collection_id`` mặc định từ env."""
    q = str(args.get("query") or "").strip()
    if not q:
        return "[ERROR] query required"
    coll = str(args.get("collection_id") or ctx.settings.pgvector_collection_k8s_expert).strip()
    limit = int(args.get("limit") or 8)
    st = args.get("score_threshold")
    score_threshold = float(st) if st is not None else 0.45
    resp = await ctx.vector_store.similarity_search(
        q,
        coll,
        llm=ctx.llm,
        embed_model=ctx.settings.embed_model,
        limit=limit,
        score_threshold=score_threshold,
    )
    lines: list[str] = []
    for p in resp.points or []:
        pay = dict(p.payload or {})
        meta = pay.get("metadata") if isinstance(pay.get("metadata"), dict) else {}
        url = meta.get("url") or pay.get("url") or ""
        ver = meta.get("version") or ""
        src = meta.get("source") or ""
        typ = meta.get("type") or ""
        cite = (pay.get("text") or pay.get("summary") or "")[:700]
        score = getattr(p, "score", None)
        sc = f"{float(score):.4f}" if score is not None else "?"
        head = f"score={sc} source={src} type={typ} version={ver} url={url}".strip()
        lines.append(f"{head}\n{cite}\n---")
    if not lines:
        return "[DATA] no k8s_expert hits (ingest: python -m training.k8s_official_ingest)"
    return "[DATA] k8s_expert_search\n" + "\n".join(lines)
