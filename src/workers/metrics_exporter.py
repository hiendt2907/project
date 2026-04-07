"""Prometheus /metrics — thread HTTP server (không chặn asyncio loop)."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_build_info: Any = None
_messages: Any = None
_scout_ts: Any = None
_exhausted: Any = None
_kill_switch: Any = None
_ollama_up: Any = None
_semaphore: Any = None
_anomaly: Any = None
_baseline_z_cpu: Any = None
_baseline_z_mem: Any = None
_baseline_dr: Any = None
_baseline_chs: Any = None
_baseline_remediation_silent: Any = None
# V6.3 Resilience Metrics
_circuit_breaker: Any = None
_lag_size: Any = None
_error_rate: Any = None
_latency: Any = None
_learning_upserts: Any = None
_learning_unique_patterns: Any = None
_proactive_fallback: Any = None
_proactive_verify: Any = None
_learning_governance: Any = None
_proactive_events: Any = None
_llm_requests: Any = None
_fastpath_hits: Any = None
_experience_saved: Any = None
_agent_sessions: Any = None
_agent_premature_escalate_blocked: Any = None
_proactive_requires_human: Any = None
_proactive_freeze: Any = None
_proactive_event_timeout: Any = None
_proactive_tombstone_no_k8s: Any = None
_proactive_lease_conflict: Any = None
_proactive_skip_frozen: Any = None
_wilson_confidence_score: Any = None
_redis_stream_backlog: Any = None
_proactive_outcome: Any = None
_proactive_incident_duration: Any = None
_promql_placeholder_rejected: Any = None
_evidence_llm_contradiction: Any = None
_started = False


def _ensure_metrics() -> None:
    global _build_info, _messages, _scout_ts, _exhausted
    global _kill_switch, _ollama_up, _semaphore, _anomaly
    global _baseline_z_cpu, _baseline_z_mem, _baseline_dr
    global _baseline_chs, _baseline_remediation_silent
    global _circuit_breaker, _lag_size, _error_rate, _latency
    global _learning_upserts, _learning_unique_patterns, _proactive_fallback, _proactive_verify, _learning_governance
    global _proactive_events, _llm_requests, _fastpath_hits, _experience_saved, _agent_sessions, _agent_premature_escalate_blocked
    global _proactive_requires_human, _proactive_freeze, _proactive_event_timeout
    global _proactive_tombstone_no_k8s, _proactive_lease_conflict, _proactive_skip_frozen
    global _wilson_confidence_score, _redis_stream_backlog
    global _proactive_outcome, _proactive_incident_duration, _promql_placeholder_rejected
    global _evidence_llm_contradiction
    if _build_info is not None:
        return
    from prometheus_client import Counter, Gauge, Histogram, Info

    _build_info = Info("omni_worker_build", "Omni-worker build metadata")
    _build_info.info({"version": "1", "component": "omni-worker"})
    _messages = Counter(
        "omni_worker_messages_processed_total",
        "Inbound messages handled",
        ["source"],
    )
    _scout_ts = Gauge("omni_worker_last_scout_timestamp", "Unix time of last successful deep_scout")
    _exhausted = Counter(
        "omni_slow_path_exhausted_total",
        "Slow-path exhausted (max attempts or stale signature)",
        ["reason", "bucket"],
    )
    # Command Center — Principal SRE
    _kill_switch = Gauge(
        "omni_proactive_kill_switch",
        "Proactive kill switch from Redis: 0=Active running 1=Bypassed",
    )
    _ollama_up = Gauge(
        "omni_ollama_up",
        "Ollama HTTP /api/tags reachable (1=yes 0=no)",
    )
    _semaphore = Gauge(
        "omni_ollama_semaphore_in_use",
        "Ollama semaphore slots currently held",
        ["lane"],
    )
    _anomaly = Counter(
        "omni_anomaly_events_total",
        "Proactive anomaly incidents enqueued to incidents:proactive stream",
    )
    _baseline_z_cpu = Gauge(
        "omni_baseline_z_cpu",
        "Last baseline manifest z_cpu from Redis sync (parity with omni:node_cpu:z)",
    )
    _baseline_z_mem = Gauge(
        "omni_baseline_z_mem",
        "Last baseline manifest z_mem from Redis sync",
    )
    _baseline_dr = Gauge(
        "omni_baseline_dr",
        "Last baseline manifest dr (1=true 0=false)",
    )
    _baseline_chs = Gauge(
        "omni_baseline_chs",
        "Last baseline manifest CHS (0 if unset)",
    )
    _baseline_remediation_silent = Gauge(
        "omni_baseline_remediation_silent",
        "Last baseline manifest remediation_silent (1=true)",
    )
    _semaphore.labels(lane="proactive").set(0)
    _semaphore.labels(lane="reactive").set(0)
    # V6.3 Resilience Metrics
    _circuit_breaker = Gauge(
        "omni_circuit_breaker_active",
        "1 khi Delayed Queue vượt ngưỡng (OOM Protection active), 0 khi bình thường.",
    )
    _circuit_breaker.set(0)
    _lag_size = Gauge(
        "omni_worker_lag_size",
        "Số tin nhắn hiện tồn động trong Delayed Queue chưa xử lý.",
    )
    _error_rate = Counter(
        "omni_worker_error_rate_total",
        "Đếm lỗi phân loại theo component.",
        ["component", "error_type"],
    )
    _latency = Histogram(
        "omni_worker_latency_seconds",
        "Latency từ Inbound XREADGROUP đến XACK thành công.",
        buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0],
    )
    _learning_upserts = Counter(
        "omni_learning_upserts_total",
        "Learning records written to action_experience",
        ["source", "outcome"],
    )
    _learning_unique_patterns = Gauge(
        "omni_learning_unique_patterns",
        "Approx unique learned pattern keys by source/outcome",
        ["source", "outcome"],
    )
    _proactive_fallback = Counter(
        "omni_proactive_fallback_total",
        "Proactive SOP miss fallback attempts and outcomes",
        ["outcome"],
    )
    _proactive_verify = Counter(
        "omni_proactive_verify_total",
        "Post-check verification outcome for proactive actions",
        ["outcome"],
    )
    _learning_governance = Counter(
        "omni_learning_governance_decision_total",
        "Governance decisions for auto-execution",
        ["decision"],
    )
    _proactive_events = Counter(
        "omni_proactive_events_total",
        "Proactive events triggered and enqueued",
    )
    _llm_requests = Counter(
        "omni_llm_requests_total",
        "LLM requests from slow-path reasoning",
    )
    _fastpath_hits = Counter(
        "omni_fastpath_hits_total",
        "Fast-path SOP direct hits with high similarity",
    )
    _experience_saved = Counter(
        "omni_experience_saved_total",
        "Experience records saved into action_experience",
    )
    _agent_sessions = Counter(
        "omni_agent_sessions_total",
        "Agentic slow-path sessions completed with omni_mark_resolved",
    )
    _agent_premature_escalate_blocked = Counter(
        "omni_agent_premature_escalate_blocked_total",
        "Unattended agentic: escalate rejected until at least one observation tool",
    )
    _proactive_requires_human = Counter(
        "omni_proactive_requires_human_total",
        "Proactive path escalated to human intervention",
        ["reason"],
    )
    _proactive_freeze = Counter(
        "omni_proactive_freeze_total",
        "Redis freeze keys set after proactive failure",
        ["scope"],
    )
    _proactive_event_timeout = Counter(
        "omni_proactive_event_timeout_total",
        "Entire proactive AnomalyEvent exceeded proactive_event_timeout_sec",
    )
    _proactive_tombstone_no_k8s = Counter(
        "omni_proactive_tombstone_without_k8s_state_total",
        "Tombstone emitted without usable k8s_state snapshot",
    )
    _proactive_lease_conflict = Counter(
        "omni_proactive_lease_conflict_total",
        "Proactive mutate skipped due to resource lease held",
    )
    _proactive_skip_frozen = Counter(
        "omni_proactive_skip_frozen_total",
        "Proactive skipped because resource/namespace freeze active",
        ["layer"],
    )
    _wilson_confidence_score = Gauge(
        "omni_wilson_confidence_score",
        "Wilson lower-bound confidence score from learning governance",
    )
    _redis_stream_backlog = Gauge(
        "omni_redis_stream_backlog",
        "Redis Streams backlog length by stream key",
        ["stream"],
    )
    _proactive_outcome = Counter(
        "omni_proactive_outcome_total",
        "Proactive pipeline terminal outcomes (SLO / control tower)",
        ["outcome"],
    )
    _proactive_incident_duration = Histogram(
        "omni_proactive_incident_duration_seconds",
        "Wall time for one proactive incident after proactive semaphore acquire until handler returns (excludes kill_switch / bad payload skips).",
        buckets=[0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0],
    )
    _promql_placeholder_rejected = Counter(
        "omni_promql_placeholder_rejected_total",
        "PromQL queries rejected as placeholders (metric_value/threshold etc.)",
    )
    _evidence_llm_contradiction = Counter(
        "omni_evidence_llm_contradiction_total",
        "Diagnostic analyst LLM output contradicted SDK evidence batch",
    )
    _wilson_confidence_score.set(0.0)
    _proactive_events.inc(0)
    _llm_requests.inc(0)
    _fastpath_hits.inc(0)
    _experience_saved.inc(0)
    _agent_sessions.inc(0)
    _agent_premature_escalate_blocked.inc(0)
    # Always expose outcome=fail|success per source so PromQL sum(increase(...{outcome="fail"}))
    # never returns empty / Missingseries (flapped Grafana "Normal" vs Error on fresh pods).
    for _lsrc in ("proactive_sop", "proactive_learning_hit", "proactive_fallback"):
        for _lout in ("success", "fail"):
            _learning_upserts.labels(source=_lsrc, outcome=_lout).inc(0)
    for _po in (
        "sop_success",
        "learning_resolved",
        "learning_observe",
        "learning_verify_fail",
        "react_resolved",
        "react_escalated",
        "governance_deny",
    ):
        _proactive_outcome.labels(outcome=_po).inc(0)
    _promql_placeholder_rejected.inc(0)
    _evidence_llm_contradiction.inc(0)


def start_prometheus_server(host: str, port: int) -> None:
    global _started
    if _started:
        return
    _ensure_metrics()
    from prometheus_client import start_http_server

    def _run() -> None:
        try:
            start_http_server(port, addr=host)
            logger.info("prometheus metrics listening on %s:%s", host, port)
        except Exception as e:
            logger.warning("prometheus metrics server failed: %s", e)

    t = threading.Thread(target=_run, name="prom-metrics", daemon=True)
    t.start()
    _started = True


def inc_messages_processed(source: str) -> None:
    _ensure_metrics()
    src = (source or "unknown").lower()[:32]
    if src not in ("telegram", "stream", "http"):
        src = "other"
    _messages.labels(source=src).inc()


def set_last_scout_timestamp(ts: float | None = None) -> None:
    _ensure_metrics()
    _scout_ts.set(ts if ts is not None else time.time())


def inc_slow_path_exhausted(reason: str, bucket: str) -> None:
    """reason: max_attempts | stale_signature | loop_exit; bucket: error_signature hoặc mixed."""
    _ensure_metrics()
    r = (reason or "unknown").lower()[:48]
    b = (bucket or "unknown").lower()[:64]
    _exhausted.labels(reason=r, bucket=b).inc()


def set_proactive_kill_switch(value: float) -> None:
    """0 = proactive Active; 1 = Bypassed."""
    _ensure_metrics()
    _kill_switch.set(float(value))


async def sync_proactive_kill_switch_metric(r: Any, key: str) -> None:
    """Đọc Redis key; 1 → gauge 1, else 0."""
    _ensure_metrics()
    try:
        v = await r.get(key)
    except Exception:
        _kill_switch.set(0.0)
        return
    engaged = v is not None and str(v).strip() == "1"
    _kill_switch.set(1.0 if engaged else 0.0)


async def probe_ollama_up(base_url: str) -> None:
    """GET {base}/api/tags — set omni_ollama_up 1/0."""
    _ensure_metrics()
    url = (base_url or "").rstrip("/") + "/api/tags"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(url)
            _ollama_up.set(1.0 if r.status_code < 500 else 0.0)
    except Exception:
        _ollama_up.set(0.0)


def ollama_semaphore_inc(lane: str) -> None:
    _ensure_metrics()
    ln = lane if lane in ("proactive", "reactive") else "reactive"
    _semaphore.labels(lane=ln).inc()


def ollama_semaphore_dec(lane: str) -> None:
    _ensure_metrics()
    ln = lane if lane in ("proactive", "reactive") else "reactive"
    _semaphore.labels(lane=ln).dec()


def inc_anomaly_events() -> None:
    _ensure_metrics()
    _anomaly.inc()


def set_baseline_snapshot_gauges(
    *,
    z_cpu: float | None,
    z_mem: float | None,
    z_disk: float | None = None,
    z_iops: float | None = None,
    dr: bool,
    chs: float | None = None,
    remediation_silent: bool = False,
) -> None:
    """Cập nhật sau mỗi lần ghi Redis baseline snapshot (Grafana /metrics)."""
    _ensure_metrics()
    _baseline_z_cpu.set(float(z_cpu) if z_cpu is not None else 0.0)
    _baseline_z_mem.set(float(z_mem) if z_mem is not None else 0.0)
    # create ad-hoc gauges if not instantiated statically to save tokens:
    if "omni_baseline_z_disk" not in globals():
        global _baseline_z_disk, _baseline_z_iops
        _baseline_z_disk = Gauge("omni_baseline_z_disk", "Disk Z-Score")
        _baseline_z_iops = Gauge("omni_baseline_z_iops", "IOPS Z-Score")
    
    _baseline_z_disk.set(float(z_disk) if z_disk is not None else 0.0)
    _baseline_z_iops.set(float(z_iops) if z_iops is not None else 0.0)

    _baseline_dr.set(1.0 if dr else 0.0)
    _baseline_chs.set(float(chs) if chs is not None else 0.0)
    _baseline_remediation_silent.set(1.0 if remediation_silent else 0.0)


def set_circuit_breaker_active(value: int) -> None:
    """0 = bình thường; 1 = Circuit Breaker đang ngắt mạch."""
    _ensure_metrics()
    _circuit_breaker.set(float(value))


def set_lag_size(size: int) -> None:
    """Cập nhật kích thước Delayed Queue hiện tại."""
    _ensure_metrics()
    _lag_size.set(float(size))


def inc_error_rate(component: str, error_type: str) -> None:
    """Tăng counter lỗi cho component + error_type."""
    _ensure_metrics()
    _error_rate.labels(
        component=(component or "unknown")[:48],
        error_type=(error_type or "unknown")[:64],
    ).inc()


def observe_latency(seconds: float) -> None:
    """Ghi nhận Latency xử lý tin nhắn (giây)."""
    _ensure_metrics()
    _latency.observe(seconds)


def inc_learning_upsert(source: str, outcome: str) -> None:
    _ensure_metrics()
    s = (source or "unknown")[:32]
    o = (outcome or "unknown")[:32]
    _learning_upserts.labels(source=s, outcome=o).inc()


def set_learning_unique_patterns(source: str, outcome: str, value: float) -> None:
    _ensure_metrics()
    s = (source or "unknown")[:32]
    o = (outcome or "all")[:32]
    _learning_unique_patterns.labels(source=s, outcome=o).set(float(value))


def inc_proactive_fallback(outcome: str) -> None:
    _ensure_metrics()
    o = (outcome or "unknown")[:32]
    _proactive_fallback.labels(outcome=o).inc()


def inc_proactive_verify(outcome: str) -> None:
    _ensure_metrics()
    o = (outcome or "unknown")[:32]
    _proactive_verify.labels(outcome=o).inc()


def inc_learning_governance(decision: str) -> None:
    _ensure_metrics()
    d = (decision or "unknown")[:32]
    _learning_governance.labels(decision=d).inc()


def inc_proactive_events() -> None:
    _ensure_metrics()
    _proactive_events.inc()


def inc_llm_requests() -> None:
    _ensure_metrics()
    _llm_requests.inc()


def inc_evidence_llm_contradiction() -> None:
    _ensure_metrics()
    _evidence_llm_contradiction.inc()


def inc_fastpath_hits() -> None:
    _ensure_metrics()
    _fastpath_hits.inc()


def inc_experience_saved() -> None:
    _ensure_metrics()
    _experience_saved.inc()


def inc_agent_sessions_total() -> None:
    _ensure_metrics()
    _agent_sessions.inc()


def inc_agent_premature_escalate_blocked() -> None:
    _ensure_metrics()
    _agent_premature_escalate_blocked.inc()


def inc_proactive_requires_human(reason: str) -> None:
    _ensure_metrics()
    _proactive_requires_human.labels(reason=(reason or "unknown")[:48]).inc()


def inc_proactive_freeze(scope: str) -> None:
    _ensure_metrics()
    _proactive_freeze.labels(scope=(scope or "resource")[:32]).inc()


def inc_proactive_event_timeout() -> None:
    _ensure_metrics()
    _proactive_event_timeout.inc()


def inc_proactive_tombstone_no_k8s() -> None:
    _ensure_metrics()
    _proactive_tombstone_no_k8s.inc()


def inc_proactive_lease_conflict() -> None:
    _ensure_metrics()
    _proactive_lease_conflict.inc()


def inc_proactive_skip_frozen(layer: str) -> None:
    _ensure_metrics()
    _proactive_skip_frozen.labels(layer=(layer or "resource")[:32]).inc()


def set_wilson_confidence_score(value: float) -> None:
    _ensure_metrics()
    _wilson_confidence_score.set(float(value))


def set_redis_stream_backlog(stream: str, value: float) -> None:
    _ensure_metrics()
    _redis_stream_backlog.labels(stream=(stream or "unknown")[:64]).set(float(value))


def inc_proactive_outcome(outcome: str) -> None:
    """SLO-oriented terminal bucket: sop_success | learning_* | react_* | governance_deny."""
    _ensure_metrics()
    o = (outcome or "unknown")[:48]
    _proactive_outcome.labels(outcome=o).inc()


def observe_proactive_incident_duration(seconds: float) -> None:
    """End-to-end handler duration for incidents that acquired the proactive semaphore."""
    _ensure_metrics()
    _proactive_incident_duration.observe(max(0.0, float(seconds)))


def inc_promql_placeholder_rejected() -> None:
    _ensure_metrics()
    _promql_placeholder_rejected.inc()


async def observability_metrics_loop(
    *,
    redis: Any,
    kill_switch_key: str,
    ollama_base_url: str,
    stop: asyncio.Event,
    stream_keys: tuple[str, ...] = (),
    interval_sec: float = 15.0,
) -> None:
    """15s: sync kill switch + ping Ollama /api/tags + lag_size từ Redis."""
    while not stop.is_set():
        try:
            await sync_proactive_kill_switch_metric(redis, kill_switch_key)
            await probe_ollama_up(ollama_base_url)
            # Sync lag_size từ Redis Delayed Queue
            try:
                lag = await redis.zcard("omni:delayed_queue")
                set_lag_size(int(lag or 0))
            except Exception:
                pass
            for stream_key in stream_keys:
                if not stream_key:
                    continue
                try:
                    size = await redis.xlen(stream_key)
                    set_redis_stream_backlog(stream_key, float(size or 0))
                except Exception:
                    set_redis_stream_backlog(stream_key, 0.0)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("observability_metrics_loop: %s", e)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_sec)
        except asyncio.TimeoutError:
            pass
