"""Simulator route — inject a REAL synthetic alert per lane and trace it live.

This is NOT a mock. Each lane builds a payload shaped exactly like the production
ingress for that lane and produces it onto the same Kafka topic the real pipeline
consumes, so the full worker flow runs end-to-end (evidence → RAG → LLM advisory →
kill-switch → CRAT → dispatch → Telegram). The UI follows the resulting trace via
``/trace/stream`` (SSE) + ``/trace/{id}/pipeline``.

Lane → ingress path (mirrors scripts/e2e_4lanes_live_report.py):
  - sys_resource  → omni-alerts (Prometheus webhook envelope, CPU saturation)
  - sys_hard_fail → omni-alerts (memory/limit critical alert)
  - app_http      → omni-diagnostic-evidence (log-surge access errors)
  - siem_security → omni-diagnostic-evidence (SIEM incident context)

Gateway must NOT import workers; this uses pkg.observability.pipeline_stages
(already packaged in the gateway image) for the INGEST stage mark.
"""
from __future__ import annotations

import json
import logging
import secrets
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from pkg.domain.taxonomy import lane_to_domain
from pkg.observability.pipeline_stages import mark_stage

log = logging.getLogger(__name__)

router = APIRouter(prefix="/simulate", tags=["simulate"])

# Canonical lane keys exposed to the UI (one button each).
LANE_KEYS: tuple[str, ...] = ("sys_resource", "sys_hard_fail", "app_http", "siem_security")

_LANE_LABEL: dict[str, str] = {
    "sys_resource": "SYS_RESOURCE",
    "sys_hard_fail": "SYS_HARD_FAIL",
    "app_http": "APP_HTTP",
    "siem_security": "SIEM_SECURITY",
}

# Lanes ingested as Prometheus alerts (omni-alerts) vs evidence batches.
_ALERT_LANES = frozenset({"sys_resource", "sys_hard_fail"})


def _get_redis(request: Request) -> Any:
    r = getattr(request.app.state, "redis", None)
    if r is None:
        raise HTTPException(status_code=503, detail="Redis not available")
    return r


def _get_kafka(request: Request) -> Any:
    k = getattr(request.app.state, "kafka", None)
    if k is None:
        raise HTTPException(status_code=503, detail="Kafka not available")
    return k


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _new_trace(lane: str) -> str:
    return f"sim-{lane}-{secrets.token_hex(6)}"


def _alert_topic(request: Request) -> str:
    return getattr(request.app.state, "kafka_topic_alerts", "omni-alerts")


def _evidence_topic(request: Request) -> str:
    return getattr(request.app.state, "kafka_topic_evidence", "omni-diagnostic-evidence")


def _build_prometheus_alert(
    lane: str,
    trace_id: str,
    *,
    pod: str | None = None,
    namespace: str | None = None,
    drop_pod: bool = False,
) -> dict[str, Any]:
    """Build a firing Prometheus/Alertmanager webhook body for an alert-lane.

    Optional overrides let an operator exercise edge cases (lab-only):
      - ``pod`` / ``namespace``: target a different (e.g. non-existent) workload so
        the VERIFY ground-truth reconciler can be tested (ghost pod ⇒ refuted).
      - ``drop_pod``: omit the ``pod`` label entirely to test alert-completeness
        handling (does the pipeline reject or hallucinate a pod?).
    """
    stamp = _now_iso()
    _ns = (namespace or "multi-agent").strip() or "multi-agent"
    _pod = (pod or "nginx-test").strip() or "nginx-test"
    if lane == "sys_resource":
        alert = {
            "status": "firing",
            "labels": {
                "alertname": "HighCPUUsage",
                "severity": "warning",
                "namespace": _ns,
                "pod": _pod,
                "deployment": _pod,
                "container": "nginx",
            },
            "annotations": {
                "summary": "[SIMULATOR] nginx-test CPU ~92% saturation for 5m",
                "description": "Container nginx ~92% CPU vs limit; 3-sigma resource baseline anomaly expected.",
            },
            "startsAt": stamp,
            "endsAt": "0001-01-01T00:00:00Z",
        }
    else:  # sys_hard_fail
        alert = {
            "status": "firing",
            "labels": {
                "alertname": "PodMemoryWorkingSetVsLimitHigh",
                "severity": "critical",
                "namespace": _ns,
                "pod": _pod,
                "container": "nginx",
                "domain": "pod",
                "signal": "memory",
            },
            "annotations": {
                "summary": "[SIMULATOR] Pod memory 95% of limit — OOM eviction imminent",
                "description": "container_memory_working_set_bytes/limit=0.95 sustained 5m; OOMKilled risk.",
            },
            "startsAt": stamp,
            "endsAt": "0001-01-01T00:00:00Z",
        }
    if drop_pod:
        # Alert-completeness edge case: a critical alert with no pod label.
        alert["labels"].pop("pod", None)
        alert["labels"].pop("deployment", None)
    return {
        "receiver": "omni-webhook",
        "status": "firing",
        "alerts": [alert],
        "groupLabels": {},
        "commonLabels": {},
        "commonAnnotations": {},
        "externalURL": "http://simulator",
    }


def _base_envelope(trace_id: str, lane_label: str, probe: str, now_ts: str) -> dict[str, Any]:
    # `domain` là trục sự thật mới; `lane` giữ lại vì simulator phải bơm payload
    # GIỐNG HỆT đường vào production, và production còn agent bản cũ chỉ gửi lane.
    # Simulator mà đi trước fleet thì nó không còn mô phỏng thật.
    return {
        "trace_id": trace_id,
        "probe": probe,
        "domain": lane_to_domain(lane_label),
        "lane": lane_label,
        "stream_tags": [lane_label],
        "namespace": "multi-agent",
        "ts": now_ts,
        "kind": "diagnostic_evidence",
        "canonical_query_snippet": json.dumps({"labels": {"probe": probe}}),
    }


def _build_evidence_envelopes(
    lane: str, trace_id: str, *, siem_ip: str | None = None
) -> list[dict[str, Any]]:
    """Build the omni-diagnostic-evidence batch for an evidence-lane.

    Returns TWO probes per lane: a primary diagnostic probe plus a companion
    context probe. The aggregator (`append_evidence_and_take_flush_batch`) only
    flushes a batch when ``len(keys) >= 2`` (or the smart-dispatcher's expected
    probe set is satisfied). A single probe would sit unflushed until another
    message arrives — so the simulator always emits two distinct probes to make
    the batch flush immediately and run the full advisory pipeline.
    """
    now_ts = str(int(time.time()))
    lane_label = _LANE_LABEL[lane]

    if lane == "app_http":
        primary = {
            **_base_envelope(trace_id, lane_label, "log_surge_access_errors", now_ts),
            "alert_rule": "HighErrorRateSurge",
            "alert_hint": (
                "[SIMULATOR] Access log surge: 429×42, 499×8, 5xx×5 in 300s "
                "(rate_limit dominant — sigma bypass eligible)"
            ),
            "result": "PASS",
            "extracted_fact": {
                "total_lines": 100,
                "count_429": 42,
                "count_499": 8,
                "count_401": 3,
                "count_5xx": 5,
                "dominant_class": "rate_limit",
                "sigma_bypass_eligible": True,
                "sigma_bypass_reason": "429 rate_limit surge (42% of requests)",
            },
            "raw": "GET /api 429 ... POST /login 429 ... GET /img 499",
            "symptom_group": "http_error_surge",
            "evidence_source": "Simulator",
            "layer": "prometheus",
        }
        companion = {
            **_base_envelope(trace_id, lane_label, "remote_log_errors", now_ts),
            "alert_rule": "HighErrorRateSurge",
            "alert_hint": "[SIMULATOR] upstream 502/504 spikes correlate with 429 surge",
            "result": "FAILED",
            "extracted_fact": {"count_502": 6, "count_504": 3, "window_sec": 300},
            "raw": "upstream timed out (110: Connection timed out) ... 502 Bad Gateway",
            "symptom_group": "http_error_surge",
            "evidence_source": "Simulator",
            "layer": "prometheus",
        }
        return [primary, companion]

    # siem_security
    incident_id = f"sim-siem-{secrets.token_hex(4)}"
    # Optional source-IP override lets the operator exercise the principle engine
    # with a public IP (distributed/external framing) vs the default RFC1918 IP
    # (single-internal framing). Default preserves the original internal scenario.
    _ip = (siem_ip or "10.0.0.42").strip() or "10.0.0.42"
    # SIEM batch detection (_siem_alert_labels) keys off
    # canonical_query_snippet.labels.siem_source == "finguard".
    siem_snip = json.dumps({
        "labels": {
            "siem_source": "finguard",
            "siem_category": "ddos",
            "siem_severity": "critical",
            "siem_incident_id": incident_id,
            "stream_tags": lane_label,
        }
    })
    primary = {
        **_base_envelope(trace_id, lane_label, "siem_incident_context", now_ts),
        "canonical_query_snippet": siem_snip,
        "alert_rule": "DDoSFloodDetected",
        "alert_hint": "[SIMULATOR] High-rate SYN flood from single source IP",
        "result": "FAILED",
        "extracted_fact": {
            "category": "ddos",
            "severity": "critical",
            "incident_id": incident_id,
            "tenant": "default",
            "description": f"Simulator: DDoS flood from {_ip} against multi-agent namespace",
            "suggested_action": "Block source IP at firewall and escalate to SOC",
            "affected_ip": _ip,
            "namespace": "multi-agent",
        },
        "raw": f"syn_flood rate=80000pps src={_ip}",
        "symptom_group": "siem_ddos",
        "evidence_source": "SIEM",
    }
    companion = {
        **_base_envelope(trace_id, lane_label, "siem_network_flow", now_ts),
        "alert_rule": "DDoSFloodDetected",
        "alert_hint": f"[SIMULATOR] inbound pps 80k from {_ip} — conntrack table filling",
        "result": "FAILED",
        "extracted_fact": {
            "category": "ddos",
            "incident_id": incident_id,
            "pps": 80000,
            "src_ip": _ip,
            "conntrack_pct": 92,
        },
        "raw": "conntrack: table full, dropping packet",
        "symptom_group": "siem_ddos",
        "evidence_source": "SIEM",
    }
    return [primary, companion]


def _build_remote_agent_envelopes(
    lane: str, trace_id: str, *, tenant_id: str, agent_id: str, hostname: str
) -> list[dict[str, Any]]:
    """Build a RemoteAgent evidence batch for a tenant's host.

    These set ``evidence_source="RemoteAgent"`` so the worker routes them to
    ``handle_remote_agent_evidence`` (Tenant/Remote-Agent path) instead of the
    in-cluster advisory flow. result=FAILED + high-severity keywords push triage
    to critical urgency so the multi-turn diagnosis loop runs and a session is
    stored — that is what surfaces the deep-check turns on the UI.

    Two distinct probes per lane so the evidence aggregator flushes immediately.
    """
    now_ts = str(int(time.time()))
    lane_label = _LANE_LABEL[lane]

    def base(probe: str, alert_hint: str, raw: str, fact: dict[str, Any]) -> dict[str, Any]:
        return {
            "trace_id": trace_id,
            "probe": probe,
            "alert_rule": f"RemoteAgent_{lane_label}",
            "alert_hint": f"[SIM/{hostname}] {alert_hint}",
            "result": "FAILED",
            # result also inside extracted_fact: assess_domain_severity Priority 1 reads
            # extracted_fact.result == "FAILED" → critical/high urgency → diagnosis loop runs.
            "extracted_fact": {**fact, "result": "FAILED", "agent_id": agent_id, "hostname": hostname},
            # Nonce in raw → unique fingerprint per run, so each simulation is a fresh
            # incident (no stale cached cluster/representative, no spurious RAG self-hit).
            "raw": f"{raw}\n# sim-run {trace_id}",
            "symptom_group": f"remote_{lane}",
            "domain": lane_to_domain(lane_label),
            "lane": lane_label,
            "stream_tags": [lane_label],
            "namespace": hostname,
            "tenant_id": tenant_id,
            "ts": now_ts,
            "evidence_source": "RemoteAgent",
            "canonical_query_snippet": json.dumps(
                {"labels": {"agent_id": agent_id, "hostname": hostname, "probe": probe}}
            ),
            "kind": "diagnostic_evidence",
        }

    # One probe per lane: the remote-agent pipeline processes each evidence message
    # individually (no batch aggregation), so a single probe = one clean trace.
    presets: dict[str, dict[str, Any]] = {
        "sys_resource": base(
            "remote_system_metrics", "CPU 96% sustained 5m — critical saturation",
            "load avg 18.2; top: pid 4112 99%cpu", {"cpu_percent": 96.0, "severity": "critical"}),
        "sys_hard_fail": base(
            "remote_systemd_units", "mysql.service failed (Result: exit-code) — critical OOM",
            "● mysql.service failed; active(exited) status=1/FAILURE", {"failed_unit": "mysql.service", "severity": "critical"}),
        "app_http": base(
            "remote_log_errors", "5xx surge 502×140/504×60 in 300s — critical upstream failure",
            "upstream timed out (110); 502 Bad Gateway x140", {"count_5xx": 200, "severity": "critical"}),
        "siem_security": base(
            "remote_security_event", "SYN flood 80kpps from 10.0.0.42 — critical DDoS",
            "syn_flood rate=80000pps src=10.0.0.42", {"category": "ddos", "severity": "critical", "src_ip": "10.0.0.42"}),
    }
    return [presets[lane]]


@router.get("/lanes")
async def list_lanes() -> JSONResponse:
    """Expose the lane catalog so the UI renders one button per lane."""
    return JSONResponse(
        {
            "lanes": [
                {"key": k, "label": _LANE_LABEL[k], "ingress": "alert" if k in _ALERT_LANES else "evidence"}
                for k in LANE_KEYS
            ]
        }
    )


@router.post("/{lane}")
async def simulate_lane(lane: str, request: Request) -> JSONResponse:
    """Inject one real synthetic alert for ``lane`` and return its trace_id."""
    if lane not in LANE_KEYS:
        raise HTTPException(status_code=400, detail=f"unknown lane '{lane}'; valid: {', '.join(LANE_KEYS)}")

    # Optional JSON body: {target: "omni"|"remote", tenant_id, agent_id}.
    # Absent/invalid body → omni target (back-compat with the no-body callers/tests).
    try:
        body_in = await request.json()
        if not isinstance(body_in, dict):
            body_in = {}
    except Exception:
        body_in = {}
    target = str(body_in.get("target") or "omni").strip().lower()
    tenant_id = str(body_in.get("tenant_id") or "default").strip()[:128] or "default"
    agent_id = str(body_in.get("agent_id") or "").strip()[:128]

    redis = _get_redis(request)
    kafka = _get_kafka(request)
    trace_id = _new_trace(lane)
    lane_label = _LANE_LABEL[lane]

    if target == "remote":
        # Tenant / Remote-Agent path → omni-diagnostic-evidence as RemoteAgent evidence.
        if not agent_id:
            agent_id = f"sim-agent-{secrets.token_hex(3)}"
        hostname = agent_id
        topic = _evidence_topic(request)
        messages = [
            json.dumps({"data": json.dumps(env, ensure_ascii=False)}, ensure_ascii=False).encode("utf-8")
            for env in _build_remote_agent_envelopes(
                lane, trace_id, tenant_id=tenant_id, agent_id=agent_id, hostname=hostname
            )
        ]
        ingress = "remote_agent"
    elif lane in _ALERT_LANES:
        topic = _alert_topic(request)
        _ov_pod = str(body_in.get("pod") or "").strip()[:128] or None
        _ov_ns = str(body_in.get("namespace") or "").strip()[:128] or None
        _drop_pod = bool(body_in.get("drop_pod") or False)
        body = _build_prometheus_alert(
            lane, trace_id, pod=_ov_pod, namespace=_ov_ns, drop_pod=_drop_pod
        )
        # source MUST be "prometheus" so build_anomaly_event_from_alert_payload runs the
        # full label-extraction path (namespace/pod/deployment). "simulator" falls through
        # to the GenericAlert fallback → no namespace/pod → every resource/state probe SKIPs.
        # The sim-* trace_id prefix + "[SIMULATOR]" annotation retain simulator identity.
        payload = {"source": "prometheus", "trace_id": trace_id, "received_at": time.time(), "data": body}
        messages = [json.dumps({"data": json.dumps(payload, ensure_ascii=False)}, ensure_ascii=False).encode("utf-8")]
        ingress = "alert"
    else:
        topic = _evidence_topic(request)
        # Two probes so the evidence aggregator flushes immediately (len(keys) >= 2).
        _siem_ip = str(body_in.get("siem_ip") or "").strip()[:64] or None
        messages = [
            json.dumps({"data": json.dumps(envelope, ensure_ascii=False)}, ensure_ascii=False).encode("utf-8")
            for envelope in _build_evidence_envelopes(lane, trace_id, siem_ip=_siem_ip)
        ]
        ingress = "evidence"

    try:
        for env in messages:
            await kafka.send_and_wait(topic, value=env)
    except Exception as exc:
        log.error("[simulate] kafka send failed lane=%s trace=%s err=%s", lane, trace_id, exc)
        raise HTTPException(status_code=502, detail="kafka send failed") from exc

    # Mark INGEST so the trace shows up immediately on the dashboard, same trace_id
    # the workers will use downstream.
    _ingest_detail = f"simulator target={target} lane={lane_label} topic={topic}"
    if target == "remote":
        _ingest_detail += f" tenant={tenant_id} agent={agent_id}"
    await mark_stage(redis, trace_id, "INGEST", "ok", detail=_ingest_detail, lane=lane_label)

    log.info("[simulate] injected target=%s lane=%s trace=%s topic=%s tenant=%s agent=%s",
             target, lane, trace_id, topic, tenant_id, agent_id or "-")
    return JSONResponse(
        {
            "status": "injected",
            "target": target,
            "lane": lane,
            "lane_label": lane_label,
            "trace_id": trace_id,
            "topic": topic,
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "ingress": ingress,
        }
    )
