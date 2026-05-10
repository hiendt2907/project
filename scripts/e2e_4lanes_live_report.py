#!/usr/bin/env python3
"""
Live E2E Report — 4 Diagnostic Lanes
====================================
Cluster names, topics, incidents, and log expectations are loaded **only** from
``E2E_LIVE_PROFILE_JSON`` (see ``scripts/fixtures/e2e_live_profile.example.json``).

Aligned with docs/runbooks/demo-four-streams-telegram.md (suggest-only, no mutate).

Required env:
  E2E_LIVE_PROFILE_JSON — path to profile JSON (relative to repo root ok)

Optional env (bypass discovery):
  E2E_KAFKA_BOOTSTRAP, E2E_REDIS_MA_URL, E2E_REDIS_FG_URL, E2E_GATEWAY_URL
  E2E_LLM_TIMEOUT
  E2E_KUBECTL_WRAPPER
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import textwrap
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

sys.stdout.reconfigure(line_buffering=True)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO_ROOT, "src"))
sys.path.insert(0, os.path.join(_REPO_ROOT, "scripts"))

from aiokafka import AIOKafkaProducer
from redis.asyncio import Redis
from workers.baseline_snapshot import REDIS_KEY_SNAPSHOT  # noqa: E402

from e2e_live_profile import (  # noqa: E402
    E2EProfileError,
    load_json_file,
    load_live_e2e_profile,
    resolved_path,
    resolve_lane1_anchors,
    substitute_placeholders,
)

# ── kubectl ──────────────────────────────────────────────────────────────────


def _kube_cmd() -> list[str]:
    wrap = (os.environ.get("E2E_KUBECTL_WRAPPER") or "").strip()
    if wrap:
        return [wrap, "kubectl"]
    wk = os.path.join(_REPO_ROOT, "scripts", "with_working_kube.sh")
    if os.path.isfile(wk):
        return [wk, "kubectl"]
    return ["kubectl"]


def _kubectl(*args: str) -> str:
    try:
        return subprocess.check_output(
            _kube_cmd() + list(args), text=True, timeout=12
        ).strip()
    except Exception:
        return ""


def _svc_cluster_ip(ns: str, svc: str) -> str:
    return _kubectl("get", "svc", svc, "-n", ns, "-o", "jsonpath={.spec.clusterIP}")


def _resolve_kafka_bootstrap(ns: str, kafka_svc: str) -> str:
    ip = _svc_cluster_ip(ns, kafka_svc)
    if not ip or ip == "None":
        raise RuntimeError("Cannot resolve Kafka ClusterIP. Set E2E_KAFKA_BOOTSTRAP.")
    return f"{ip}:9092"


def _resolve_redis_ma_url(ns: str, redis_svc: str) -> str:
    ip = _svc_cluster_ip(ns, redis_svc)
    if not ip or ip == "None":
        raise RuntimeError("Cannot resolve Redis MA ClusterIP. Set E2E_REDIS_MA_URL.")
    return f"redis://{ip}:6379/0"


def _resolve_redis_fg_url(prof: dict[str, Any]) -> str:
    ns_order = prof["fg_redis_namespace_order"]
    pod = prof["fg_redis_pod_name"]
    secret = prof["fg_redis_auth_secret_name"]
    pod_ip = ""
    password = ""
    for ns in ns_order:
        pod_ip = _kubectl("get", "pod", pod, "-n", ns, "-o", "jsonpath={.status.podIP}")
        if pod_ip:
            raw = _kubectl("get", "secret", secret, "-n", ns, "-o", "jsonpath={.data.password}")
            if raw:
                password = base64.b64decode(raw).decode()
            break
    if not pod_ip:
        raise RuntimeError(
            f"Cannot resolve SIEM Redis pod {pod} in {ns_order}. Set E2E_REDIS_FG_URL."
        )
    password = os.getenv("E2E_REDIS_FG_PASSWORD", password)
    auth = f":{password}@" if password else ""
    return f"redis://{auth}{pod_ip}:6379/0"


def _resolve_gateway_url(ns: str, gateway_svc: str, webhook_path: str) -> str:
    ip = _svc_cluster_ip(ns, gateway_svc)
    if not ip or ip == "None":
        return ""
    return f"http://{ip}{webhook_path}"


# ── console helpers ───────────────────────────────────────────────────────────


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def section(title: str) -> None:
    bar = "═" * 72
    print(f"\n{bar}\n  {title}\n{bar}")


def ok(msg: str) -> None:
    print(f"  [PASS] {msg}")


def fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")


def info(msg: str) -> None:
    print(f"  [INFO] {msg}")


# ── log polling ───────────────────────────────────────────────────────────────


@dataclass
class E2EConnections:
    prof: dict[str, Any]
    ns: str
    kafka_bootstrap: str
    redis_ma_url: str
    redis_fg_url: str
    gateway_url: str
    topic_diagnostic_evidence: str
    siem_stream: str
    llm_timeout: float
    analyst_deploy: str
    siem_bridge_deploy: str


def _analyst_logs(ctx: E2EConnections, since_min: int = 15) -> str:
    out = _kubectl(
        "logs",
        "-n",
        ctx.ns,
        f"deploy/{ctx.analyst_deploy}",
        f"--since={since_min}m",
        "--tail=50000",
    )
    return out or ""


def _grep_trace(logs: str, trace: str) -> list[str]:
    return [ln for ln in logs.splitlines() if trace in ln]


def _wait_for_marker(
    ctx: E2EConnections,
    trace: str,
    markers: list[str],
    timeout: float,
    since_min: int = 15,
) -> tuple[bool, list[str]]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        logs = _analyst_logs(ctx, since_min=since_min)
        trace_lines = _grep_trace(logs, trace)
        for marker in markers:
            if any(marker in ln for ln in trace_lines):
                return True, trace_lines
        remaining = deadline - time.monotonic()
        time.sleep(min(5.0, remaining) if remaining > 0 else 0.0)
    return False, _grep_trace(_analyst_logs(ctx, since_min=since_min), trace)


def _extract_advisory_text(trace_lines: list[str]) -> str:
    for ln in trace_lines:
        for key in ("advisory_text=", "diagnosis=", "root_cause=", "affected_workload="):
            if key in ln:
                idx = ln.index(key) + len(key)
                return ln[idx : idx + 600].strip()
    return ""


def _runbook_five_w_h(blob: str, who_hints: list[str]) -> dict[str, bool]:
    b = blob.lower()
    who_extra = tuple(x.lower() for x in who_hints if x)
    return {
        "runbook field What (root cause / summary)": any(
            p in b
            for p in (
                "**what:",
                "what:",
                "root_cause",
                '"root_cause"',
                "summary",
            )
        ),
        "runbook field When (time)": any(
            p in b for p in ("**when:", "when:", "startsat", "timestamp", "ts=", "first_seen")
        ),
        "runbook field Who (scope)": any(
            p in b
            for p in (
                "**who:",
                "who:",
                "affected_workload",
                "namespace",
                "deployment",
                "tenant",
            )
            + who_extra
        ),
        "runbook field Why (evidence)": any(
            p in b
            for p in ("**why:", "why:", "verification", "rationale", "evidence", "sigma", "diag")
        ),
        "runbook field How to (remediation / read-only)": any(
            p in b
            for p in (
                "**how to:",
                "how to:",
                "how-to",
                "proposed_remediation",
                "remediation",
                "kubectl",
                "suggest",
                "ticket",
            )
        ),
    }


async def _publish_evidence(
    producer: AIOKafkaProducer, topic: str, trace: str, batch: list[dict[str, Any]]
) -> None:
    for doc in batch:
        inner = {**doc, "trace_id": trace, "kind": "diagnostic_evidence"}
        envelope = {"data": json.dumps(inner, ensure_ascii=False)}
        msg = json.dumps(envelope, ensure_ascii=False).encode()
        await producer.send(topic, msg)
    info(f"Published {len(batch)} evidence doc(s) → {topic} (trace={trace})")


async def _post_gateway(gateway_url: str, payload: dict) -> tuple[str, str]:
    import httpx

    if not gateway_url:
        return "", "no_gateway_url"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(gateway_url, json=payload)
        body = (
            resp.json()
            if resp.headers.get("content-type", "").startswith("application/json")
            else {}
        )
        trace = str(body.get("trace_id") or "")
        return trace, str(resp.status_code)


def _build_ctx(prof: dict[str, Any]) -> E2EConnections:
    ns = prof["omni_namespace"]
    kb = os.getenv("E2E_KAFKA_BOOTSTRAP") or _resolve_kafka_bootstrap(ns, prof["kafka_svc_name"])
    rma = os.getenv("E2E_REDIS_MA_URL") or _resolve_redis_ma_url(ns, prof["redis_ma_svc_name"])
    rfg = os.getenv("E2E_REDIS_FG_URL") or _resolve_redis_fg_url(prof)
    gw = os.getenv("E2E_GATEWAY_URL") or _resolve_gateway_url(
        ns, prof["gateway_svc_name"], prof["gateway_webhook_path"]
    )
    return E2EConnections(
        prof=prof,
        ns=ns,
        kafka_bootstrap=kb,
        redis_ma_url=rma,
        redis_fg_url=rfg,
        gateway_url=gw,
        topic_diagnostic_evidence=prof["kafka_topic_diagnostic_evidence"],
        siem_stream=prof["siem_redis_stream"],
        llm_timeout=float(os.getenv("E2E_LLM_TIMEOUT", "240")),
        analyst_deploy=prof["analyst_deploy_name"],
        siem_bridge_deploy=prof["siem_bridge_deploy_name"],
    )


# ── Lanes ─────────────────────────────────────────────────────────────────────


async def lane1_resource(ctx: E2EConnections, redis_ma: Redis) -> dict:
    section("LANE 1 — SYS_RESOURCE (3-sigma z-score via gateway)")
    p = ctx.prof
    snap_path = resolved_path(p, "baseline_snapshot")
    snap = dict(load_json_file(snap_path))
    snap["ts"] = now_iso()
    await redis_ma.set(REDIS_KEY_SNAPSHOT, json.dumps(snap))
    await redis_ma.expire(REDIS_KEY_SNAPSHOT, int(p["baseline_snapshot_ttl_sec"]))
    info(f"Set baseline snapshot Redis key from worker module + fixture file ({REDIS_KEY_SNAPSHOT})")

    tmpl = load_json_file(resolved_path(p, "lane1_gateway_alert_template"))
    mapping = {
        "GATEWAY_RECEIVER": p["gateway_receiver_name"],
        "OMNI_NAMESPACE": p["omni_namespace"],
        "LANE1_DEPLOYMENT": p["lane1_deployment"],
        "LANE1_SUMMARY": p["lane1_summary"],
        "LANE1_DESCRIPTION": p["lane1_description"],
        "STAMP": now_iso(),
    }
    payload = substitute_placeholders(tmpl, mapping)
    trace, status = await _post_gateway(ctx.gateway_url, payload)
    info(f"Gateway POST → {status}  trace_id={trace}")
    if not trace:
        trace = p["trace_fallback_lane1"]

    markers = list(p["log_markers_lane1"])
    info(f"Waiting up to {ctx.llm_timeout:.0f}s for LLM advisory (prober → analyst)…")
    found, trace_lines = _wait_for_marker(ctx, trace, markers, ctx.llm_timeout, since_min=20)

    combined = "\n".join(trace_lines)
    result = {
        "lane": 1,
        "name": "System resource / 3-sigma (SYS_RESOURCE)",
        "trace": trace,
        "found_marker": found,
        "trace_line_count": len(trace_lines),
        "checks": {},
        "llm_excerpt": "",
        "stream_tag": p["stream_tags_by_lane"]["1"],
    }
    anchors = resolve_lane1_anchors(p["lane1_log_anchors"], p)
    result["checks"] = {label: (sub in combined) for label, sub in anchors.items()}
    result["llm_excerpt"] = _extract_advisory_text(trace_lines)
    blob = combined + "\n" + result["llm_excerpt"]
    result["checks"].update(_runbook_five_w_h(blob, p["runbook_who_hints"]))
    tag = p["stream_tags_by_lane"]["1"]
    result["checks"][f"stream_tag is {tag} (profile)"] = result["stream_tag"] == tag

    for label, passed in result["checks"].items():
        (ok if passed else fail)(label)
    if not found:
        fail("Timeout: no advisory markers found within LLM_TIMEOUT")
    return result


async def lane2_system_errors(ctx: E2EConnections) -> dict:
    section("LANE 2 — SYS_HARD_FAIL (AnalystAdvisory via gateway)")
    p = ctx.prof
    tmpl = load_json_file(resolved_path(p, "lane2_gateway_alert_template"))
    mapping = {
        "GATEWAY_RECEIVER": p["gateway_receiver_name"],
        "OMNI_NAMESPACE": p["omni_namespace"],
        "LANE2_DEPLOYMENT": p["lane2_deployment"],
        "LANE2_POD": p["lane2_pod"],
        "LANE2_SUMMARY": p["lane2_summary"],
        "LANE2_DESCRIPTION": p["lane2_description"],
        "STAMP": now_iso(),
    }
    payload = substitute_placeholders(tmpl, mapping)
    trace, status = await _post_gateway(ctx.gateway_url, payload)
    info(f"Gateway POST → {status}  trace_id={trace}")
    if not trace:
        trace = p["trace_fallback_lane2"]

    found, trace_lines = _wait_for_marker(
        ctx, trace, list(p["log_markers_lane2"]), ctx.llm_timeout, since_min=20
    )
    combined = "\n".join(trace_lines)
    result = {
        "lane": 2,
        "name": "System hard fail / AnalystAdvisory (SYS_HARD_FAIL)",
        "trace": trace,
        "found_marker": found,
        "trace_line_count": len(trace_lines),
        "checks": {},
        "llm_excerpt": "",
        "stream_tag": p["stream_tags_by_lane"]["2"],
    }
    result["checks"] = {
        label: (sub in combined) for label, sub in p["lane2_log_anchors"].items()
    }
    result["llm_excerpt"] = _extract_advisory_text(trace_lines)
    blob = combined + "\n" + result["llm_excerpt"]
    result["checks"].update(_runbook_five_w_h(blob, p["runbook_who_hints"]))
    tag = p["stream_tags_by_lane"]["2"]
    result["checks"][f"stream_tag is {tag} (profile)"] = result["stream_tag"] == tag
    cl = combined.lower()
    sig_kw = [k.lower() for k in p["lane2_multi_signal_keywords"]]
    sig_hits = sum(1 for k in sig_kw if k in cl)
    result["checks"]["multi-signal hints (profile keywords)"] = sig_hits >= min(2, len(sig_kw))

    for label, passed in result["checks"].items():
        (ok if passed else fail)(label)
    if not found:
        fail("Timeout: no advisory markers found within LLM_TIMEOUT")
    return result


async def lane3_app_http(ctx: E2EConnections, producer: AIOKafkaProducer) -> dict:
    section("LANE 3 — APP_HTTP (HTTP / log surge sigma-bypass)")
    p = ctx.prof
    trace = f'e2e-lane3-{uuid.uuid4().hex[: int(p["siem_random_hex_len"])]}'
    info(f"trace_id={trace}")

    tmpl = load_json_file(resolved_path(p, "lane3_evidence_batch_template"))
    l3labels = json.dumps({"labels": p["lane3_evidence_labels"]}, ensure_ascii=False)
    mapping = {
        "TRACE": trace,
        "LANE3_CANONICAL_JSON": l3labels,
        "LANE3_SYMPTOM_GROUP": p["lane3_symptom_group"],
    }
    batch = substitute_placeholders(tmpl, mapping)
    if not isinstance(batch, list):
        raise E2EProfileError("lane3_evidence_batch_template must be a JSON array")
    await _publish_evidence(producer, ctx.topic_diagnostic_evidence, trace, batch)

    found, trace_lines = _wait_for_marker(ctx, trace, list(p["log_markers_lane3"]), ctx.llm_timeout)
    combined = "\n".join(trace_lines)
    result = {
        "lane": 3,
        "name": "Application HTTP / sigma-bypass (APP_HTTP)",
        "trace": trace,
        "found_marker": found,
        "trace_line_count": len(trace_lines),
        "checks": {},
        "llm_excerpt": "",
        "bypass_report": {},
        "stream_tag": p["stream_tags_by_lane"]["3"],
    }
    from workers.log_surge_probe import classify_http_status

    for row in p["lane3_http_classify_expectations"]:
        st = int(row["status"])
        expected_class = row["expected_class"]
        note = row["note"]
        cls = classify_http_status(st)
        class_ok = cls == expected_class
        (ok if class_ok else fail)(
            f"HTTP {st} → classify={cls} (expected={expected_class}) — {note}"
        )
        result["bypass_report"][st] = {
            "class": cls,
            "expected": expected_class,
            "ok": class_ok,
            "bypass": row.get("bypass"),
            "note": note,
        }
    result["checks"] = {
        label: (sub in combined) for label, sub in p["lane3_log_anchors"].items()
    }
    result["llm_excerpt"] = _extract_advisory_text(trace_lines)
    blob = combined + "\n" + result["llm_excerpt"]
    result["checks"].update(_runbook_five_w_h(blob, p["runbook_who_hints"]))
    tag = p["stream_tags_by_lane"]["3"]
    result["checks"][f"stream_tag is {tag} (profile)"] = result["stream_tag"] == tag
    result["checks"]["HTTP surge evidence in trace (429/rate_limit)"] = (
        "429" in combined or "rate_limit" in combined.lower()
    )

    for label, passed in result["checks"].items():
        (ok if passed else fail)(label)
    if not found:
        fail("Timeout: no advisory markers found within LLM_TIMEOUT")
    return result


async def lane4_siem(ctx: E2EConnections, redis_fg: Redis, pre_injected: dict | None) -> dict:
    section("LANE 4 — SIEM_SECURITY (WHAT/WHO/WHY/HOW-TO + kill-chain forecast)")
    p = ctx.prof
    if pre_injected and pre_injected.get("incident_id"):
        incident_id = pre_injected["incident_id"]
        trace = pre_injected["trace"]
        fields = pre_injected.get("fields")
        if not fields:
            raise E2EProfileError("pre_injected missing fields dict")
        info(f"Using pre-injected: incident_id={incident_id}  trace={trace}")
    else:
        rh = int(p["siem_random_hex_len"])
        tb = int(p["siem_trace_body_hex_len"])
        suffix = uuid.uuid4().hex[:rh]
        incident_id = f'{p["siem_incident_id_prefix"]}{suffix}'
        trace = f'{p["siem_trace_prefix"]}{suffix[:tb]}'
        tmpl_row = load_json_file(resolved_path(p, "siem_redis_xadd_row_template"))
        mapping = _siem_mapping(p, incident_id, trace)
        fields = substitute_placeholders(tmpl_row, mapping)
        await redis_fg.xadd(ctx.siem_stream, fields)
        info(f"Injected SIEM row incident_id={incident_id} trace={trace}")

    markers = list(p["log_markers_lane4"]) + [incident_id, trace]
    found, trace_lines = _wait_for_marker(ctx, trace, markers, timeout=30.0, since_min=45)
    combined = "\n".join(trace_lines)

    from workers.evidence_consumer import (
        _format_siem_forecast_text,
        _siem_diagnosis_from_batch,
        _siem_forecast_timeline,
    )

    tmpl_batch = load_json_file(
        resolved_path(p, "lane4_analyst_siem_evidence_batch_template")
    )
    if not isinstance(tmpl_batch, list) or len(tmpl_batch) != 1:
        raise E2EProfileError("lane4_analyst template must be a one-element array")
    lane4_labels = {
        "siem_source": p["siem_source"],
        "siem_category": p["siem_category"],
        "siem_incident_id": incident_id,
        "severity": p["siem_severity"],
        "namespace": p["siem_k8s_namespace"],
    }
    mapping = _siem_mapping(p, incident_id, trace)
    mapping["LANE4_CANONICAL_JSON"] = json.dumps({"labels": lane4_labels}, ensure_ascii=False)
    siem_evidence_batch = substitute_placeholders(tmpl_batch, mapping)
    siem_labels = dict(lane4_labels)
    diag = _siem_diagnosis_from_batch(siem_evidence_batch, siem_labels, "")
    forecast = _siem_forecast_timeline(p["siem_category"], p["siem_severity"])
    forecast_text = _format_siem_forecast_text(forecast)

    result = {
        "lane": 4,
        "name": "Smart-SIEM / kill-chain forecast (SIEM_SECURITY)",
        "trace": trace,
        "incident_id": incident_id,
        "found_marker": found,
        "trace_line_count": len(trace_lines),
        "checks": {},
        "siem_diag_excerpt": diag[:1200],
        "siem_forecast_text": forecast_text[:600],
        "stream_tag": p["stream_tags_by_lane"]["4"],
    }
    section_checks: dict[str, bool] = {}
    for needle in p["lane4_diag_substrings_required"]:
        section_checks[f"diag contains ({needle})"] = needle in diag
    blob_f = diag + "\n" + forecast_text
    for needle in p["lane4_forecast_blob_substrings_required"]:
        section_checks[f"diag+forecast contains ({needle})"] = needle in blob_f
    if p["lane4_require_incident_id_in_diag"]:
        section_checks["incident_id in diag"] = incident_id in diag
    tag = p["stream_tags_by_lane"]["4"]
    section_checks[f"stream_tag is {tag} (profile)"] = result["stream_tag"] == tag
    result["checks"] = section_checks
    blob_runbook = (diag + "\n" + forecast_text + "\n" + combined).lower()
    result["checks"].update(_runbook_five_w_h(blob_runbook, p["runbook_who_hints"]))

    for label, passed in result["checks"].items():
        (ok if passed else fail)(label)

    cluster_checks = {
        f"cluster log ({s})": s in combined for s in p["lane4_cluster_log_substrings"]
    }
    result["cluster_checks"] = cluster_checks
    for lbl, passed in cluster_checks.items():
        (ok if passed else fail)(f"[cluster] {lbl}")

    if not found:
        fail("Timeout / bridge did not forward incident within LLM_TIMEOUT")
        info("Check siem-bridge pod logs:")
        bridge_logs = _kubectl(
            "logs",
            "-n",
            ctx.ns,
            f"deploy/{ctx.siem_bridge_deploy}",
            "--since=10m",
            "--tail=100",
        )
        for ln in (bridge_logs or "").splitlines()[-15:]:
            print(f"    {ln[:200]}")
    return result


def _siem_mapping(p: dict[str, Any], incident_id: str, trace: str) -> dict[str, str]:
    return {
        "INCIDENT_ID": incident_id,
        "TRACE": trace,
        "TIMESTAMP": now_iso(),
        "SIEM_CATEGORY": p["siem_category"],
        "SIEM_SEVERITY": p["siem_severity"],
        "SIEM_TENANT_ID": p["siem_tenant_id"],
        "SIEM_SOURCE": p["siem_source"],
        "SIEM_AFFECTED_IP": p["siem_affected_ip"],
        "SIEM_DESCRIPTION": p["siem_description"],
        "SIEM_SUGGESTED_ACTION": p["siem_suggested_action"],
        "SIEM_HITL_REQUIRED": p["siem_hitl_required"],
        "SIEM_ALERT_RULE": p["siem_alert_rule"],
        "SIEM_ALERT_HINT": p["siem_alert_hint"],
        "SIEM_K8S_NAMESPACE": p["siem_k8s_namespace"],
    }


# ── report ────────────────────────────────────────────────────────────────────


def print_report(results: list[dict]) -> None:
    section("FINAL REPORT — 4 Diagnostic Lanes Live E2E")
    lanes_ok = 0
    for r in results:
        checks = dict(r.get("checks", {}))
        if r.get("cluster_checks"):
            checks.update(r["cluster_checks"])
        all_pass = all(checks.values()) if checks else False
        cluster_ok = r.get("found_marker", False)
        lane_ok = all_pass and cluster_ok
        if lane_ok:
            lanes_ok += 1
        status = "PASS" if lane_ok else "FAIL"
        print(f'\n  Lane {r["lane"]} [{status}] — {r["name"]}')
        print(
            f'    stream_tag={r.get("stream_tag", "n/a")}  trace={r.get("trace", "")}  '
            f"cluster_marker={'found' if cluster_ok else 'MISSING'}"
        )
        check_results = dict(r.get("checks", {}))
        if r.get("cluster_checks"):
            check_results = {
                **check_results,
                **{f"[cluster] {k}": v for k, v in r["cluster_checks"].items()},
            }
        for label, passed in check_results.items():
            print(f'    {"✓" if passed else "✗"} {label}')
        if r.get("bypass_report"):
            print("    Sigma-bypass per status code:")
            for status_code, info_d in r["bypass_report"].items():
                print(
                    f'      {"✓" if info_d["ok"] else "✗"} HTTP {status_code} → '
                    f'{info_d["class"]} | bypass={info_d["bypass"]} | {info_d["note"]}'
                )
        if r.get("llm_excerpt"):
            print("\n    LLM advisory excerpt:")
            for line in textwrap.wrap(r["llm_excerpt"], width=80):
                print(f"      {line}")
        if r.get("siem_diag_excerpt"):
            print("\n    SIEM diagnosis (generated):")
            for line in r["siem_diag_excerpt"].splitlines()[:20]:
                print(f"      {line}")
        if r.get("siem_forecast_text"):
            print("\n    Kill-chain forecast:")
            for line in r["siem_forecast_text"].splitlines()[:8]:
                print(f"      {line}")
    bar = "═" * 72
    print(f"\n{bar}\n  RESULT: {lanes_ok}/{len(results)} lanes passed\n{bar}\n")


def print_findings(results: list[dict]) -> None:
    section("FINDINGS & PROPOSED NEXT STEPS")
    failures: list[tuple[int, str, str]] = []
    for r in results:
        checks = {**r.get("checks", {}), **r.get("cluster_checks", {})}
        for label, passed in checks.items():
            if not passed:
                failures.append((r["lane"], r["name"], label))
        if not r.get("found_marker", True):
            failures.append(
                (r["lane"], r["name"], "cluster marker not found (timeout/pipeline broken)")
            )
    if not failures:
        print("\n  No failures detected. All 4 lanes operational (per profile).\n")
    else:
        print(f"\n  {len(failures)} check(s) failed:\n")
        for lane_n, name, label in failures:
            print(f"  Lane {lane_n} ({name}): FAIL — {label}")


async def main() -> None:
    print(f"\n{'═' * 72}\n  Omni — 4 Lanes Live E2E Report\n  {now_iso()}\n{'═' * 72}")
    try:
        prof = load_live_e2e_profile(_REPO_ROOT)
    except E2EProfileError as e:
        print(f"  [FATAL] {e}")
        sys.exit(2)

    ctx = _build_ctx(prof)
    info(f"Profile: {prof.get('_profile_path')}")
    info(f"Kafka  : {ctx.kafka_bootstrap}")
    info(f"Redis MA: {ctx.redis_ma_url}")
    info(f"Redis FG: {ctx.redis_fg_url.split('@')[-1]}")
    info(f"LLM timeout per lane: {ctx.llm_timeout:.0f}s")

    redis_ma = Redis.from_url(ctx.redis_ma_url, decode_responses=True)
    redis_fg = Redis.from_url(ctx.redis_fg_url, decode_responses=True)
    producer = AIOKafkaProducer(bootstrap_servers=ctx.kafka_bootstrap)
    await producer.start()

    results: list[dict[str, Any]] = []
    siem_pre: dict = {}
    try:
        rh = int(prof["siem_random_hex_len"])
        tb = int(prof["siem_trace_body_hex_len"])
        suffix = uuid.uuid4().hex[:rh]
        siem_incident_id = f'{prof["siem_incident_id_prefix"]}{suffix}'
        siem_trace = f'{prof["siem_trace_prefix"]}{suffix[:tb]}'
        tmpl_row = load_json_file(resolved_path(prof, "siem_redis_xadd_row_template"))
        mapping = _siem_mapping(prof, siem_incident_id, siem_trace)
        siem_fields = substitute_placeholders(tmpl_row, mapping)
        msg_id = await redis_fg.xadd(ctx.siem_stream, siem_fields)
        section("LANE 4 pre-inject — Smart-SIEM (before other lanes)")
        info(f"incident_id={siem_incident_id}  trace={siem_trace}  msg_id={msg_id}")
        siem_pre = {"incident_id": siem_incident_id, "trace": siem_trace, "fields": siem_fields}
        await asyncio.sleep(float(prof["lane_pre_inject_sleep_sec"]))
    except Exception as e:
        info(f"SIEM pre-inject failed: {e}")

    try:
        results.append(await lane1_resource(ctx, redis_ma))
        results.append(await lane2_system_errors(ctx))
        results.append(await lane3_app_http(ctx, producer))
        results.append(await lane4_siem(ctx, redis_fg, siem_pre if siem_pre else None))
    finally:
        await producer.stop()
        await redis_ma.aclose()
        await redis_fg.aclose()

    print_report(results)
    print_findings(results)
    lanes_ok = 0
    for r in results:
        m = dict(r.get("checks", {}))
        if r.get("cluster_checks"):
            m.update(r["cluster_checks"])
        if r.get("found_marker") and m and all(m.values()):
            lanes_ok += 1
    sys.exit(0 if lanes_ok == len(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
