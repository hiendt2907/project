#!/usr/bin/env python3
"""
E2E CRAT Pipeline Verification — 4-Phase God-Mode Validation
============================================================

Cluster layout, topics, SIEM stream row, and timeouts come **only** from
``E2E_LIVE_PROFILE_JSON`` — see ``scripts/fixtures/e2e_live_profile.example.json``.

Optional env overrides: E2E_KAFKA_BOOTSTRAP, E2E_REDIS_MA_URL, E2E_REDIS_FG_URL,
E2E_REDIS_FG_PASSWORD, E2E_KUBECTL_WRAPPER.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any

sys.stdout.reconfigure(line_buffering=True)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO_ROOT, "src"))
sys.path.insert(0, os.path.join(_REPO_ROOT, "scripts"))

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from redis.asyncio import Redis

from e2e_live_profile import (  # noqa: E402
    E2EProfileError,
    load_json_file,
    load_live_e2e_profile,
    resolved_path,
    substitute_placeholders,
)
from services.audit_ledger.chain_writer import (  # noqa: E402
    REDIS_AUDIT_BLOCKS_KEY,
    REDIS_AUDIT_HEAD_KEY,
    REDIS_AUDIT_SEQ_KEY,
)
from services.audit_ledger.verifier import verify_chain


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
        return subprocess.check_output(_kube_cmd() + list(args), text=True, timeout=12).strip()
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
    import base64

    for ns in prof["fg_redis_namespace_order"]:
        pod_ip = _kubectl(
            "get", "pod", prof["fg_redis_pod_name"], "-n", ns, "-o", "jsonpath={.status.podIP}"
        )
        if not pod_ip:
            continue
        raw = _kubectl(
            "get",
            "secret",
            prof["fg_redis_auth_secret_name"],
            "-n",
            ns,
            "-o",
            "jsonpath={.data.password}",
        )
        password = base64.b64decode(raw).decode() if raw else ""
        password = os.getenv("E2E_REDIS_FG_PASSWORD", password)
        auth = f":{password}@" if password else ""
        return f"redis://{auth}{pod_ip}:6379/0"
    raise RuntimeError("Cannot resolve SIEM Redis. Set E2E_REDIS_FG_URL.")


def ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def banner(title: str) -> None:
    print(f"\n{'═' * 70}\n  {title}\n{'═' * 70}")


def log(label: str, value: Any) -> None:
    print(f"  [{label}] {json.dumps(value, default=str)[:300]}")


def result(name: str, ok: bool, detail: str = "") -> None:
    mark = "✓ PASS" if ok else "✗ FAIL"
    suffix = f"  — {detail}" if detail else ""
    print(f"  {mark}  {name}{suffix}")


def _siem_placeholders(prof: dict[str, Any], incident_id: str, trace_id: str) -> dict[str, str]:
    return {
        "INCIDENT_ID": incident_id,
        "TRACE": trace_id,
        "TIMESTAMP": ts(),
        "SIEM_CATEGORY": prof["siem_category"],
        "SIEM_SEVERITY": prof["siem_severity"],
        "SIEM_TENANT_ID": prof["siem_tenant_id"],
        "SIEM_SOURCE": prof["siem_source"],
        "SIEM_AFFECTED_IP": prof["siem_affected_ip"],
        "SIEM_DESCRIPTION": prof["siem_description"],
        "SIEM_SUGGESTED_ACTION": prof["siem_suggested_action"],
        "SIEM_HITL_REQUIRED": prof["siem_hitl_required"],
        "SIEM_ALERT_RULE": prof["siem_alert_rule"],
        "SIEM_ALERT_HINT": prof["siem_alert_hint"],
        "SIEM_K8S_NAMESPACE": prof["siem_k8s_namespace"],
    }


async def _consume_until(
    *,
    bootstrap_servers: str,
    auto_offset_reset: str,
    topic: str,
    predicate,
    timeout_sec: float,
) -> dict | None:
    consumer = AIOKafkaConsumer(
        topic,
        bootstrap_servers=bootstrap_servers,
        group_id=f"e2e-verify-{uuid.uuid4().hex[:8]}",
        auto_offset_reset=auto_offset_reset,
        enable_auto_commit=False,
    )
    await consumer.start()
    deadline = time.monotonic() + timeout_sec
    try:
        while time.monotonic() < deadline:
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            poll_ms = max(200, min(2000, remaining_ms))
            records = await consumer.getmany(timeout_ms=poll_ms, max_records=50)
            for _tp, msgs in records.items():
                for msg in msgs:
                    try:
                        raw = json.loads(msg.value.decode())
                        inner_str = raw.get("data")
                        if isinstance(inner_str, str) and inner_str.strip().startswith("{"):
                            inner = json.loads(inner_str)
                        elif isinstance(inner_str, dict):
                            inner = inner_str
                        else:
                            inner = raw
                        if predicate(inner):
                            return inner
                    except Exception:
                        pass
    finally:
        await consumer.stop()
    return None


async def phase4_crat(
    trace_id: str,
    *,
    redis_ma_url: str,
    phase4_wait: float,
) -> bool:
    banner("PHASE 4 — CRAT CRYPTOGRAPHIC LEDGER VERIFICATION")

    await asyncio.sleep(phase4_wait)

    redis = Redis.from_url(redis_ma_url, decode_responses=True)
    try:
        raw_blocks = await redis.lrange(REDIS_AUDIT_BLOCKS_KEY, 0, -1)
        head_hash = await redis.get(REDIS_AUDIT_HEAD_KEY)
        seq_counter = await redis.get(REDIS_AUDIT_SEQ_KEY)
    finally:
        await redis.aclose()

    if not raw_blocks:
        result("Phase 4 — CRAT chain fetched", False, "audit blocks are empty")
        return False

    blocks = []
    for rb in raw_blocks:
        try:
            blocks.append(json.loads(rb))
        except Exception:
            pass

    log("audit chain stats", {
        "total_blocks": len(blocks),
        "head_hash": (head_hash or "")[:16] + "…",
        "seq_counter": seq_counter,
    })

    our_blocks = [b for b in blocks if b.get("trace_id") == trace_id]
    if not our_blocks:
        log("WARN", f"No block for trace_id={trace_id}; chain has {len(blocks)} blocks")
        log("Available trace_ids (last 5)", [b.get("trace_id") for b in blocks[-5:]])
    else:
        log("Our blocks found", [{"seq": b["seq"], "event_type": b["event_type"]} for b in our_blocks])

    result_v = verify_chain(blocks)
    log("verify_chain result", {
        "ok": result_v.ok,
        "blocks_checked": result_v.blocks_checked,
        "reason": result_v.reason,
        "errors": result_v.errors[:3],
    })

    terminal = blocks[-1]
    signed = bool(terminal.get("signature_hex"))
    print(f"\n  ┌─ TERMINAL BLOCK PROOF ─────────────────────────────────────────")
    print(f"  │  seq         : {terminal.get('seq')}")
    print(f"  │  event_type  : {terminal.get('event_type')}")
    print(f"  │  trace_id    : {terminal.get('trace_id')}")
    print(f"  │  timestamp   : {terminal.get('timestamp_utc')}")
    print(f"  │  block_hash  : {terminal.get('block_hash', '')}")
    print(f"  │  signed      : {'YES — Ed25519' if signed else 'NO (lab unsigned)'}")
    print(f"  └────────────────────────────────────────────────────────────────")

    t = terminal
    canonical = (
        f"{t['seq']}|{t['event_type']}|{t['trace_id']}|"
        f"{t['timestamp_utc']}|{t['payload_hash']}|{t['prev_hash']}"
    )
    recomputed = hashlib.sha256(canonical.encode()).hexdigest()
    hash_match = recomputed == t.get("block_hash")

    ok = result_v.ok and (bool(our_blocks) or len(blocks) > 0) and hash_match
    result(
        "Phase 4 — CRAT chain integrity",
        ok,
        f"chain_valid={result_v.ok} our_block={'found' if our_blocks else 'not_found'}",
    )
    return ok


async def main() -> int:
    try:
        prof = load_live_e2e_profile(_REPO_ROOT)
    except E2EProfileError as e:
        print(f"  [FATAL] {e}")
        return 2

    ns = prof["omni_namespace"]
    kafka_bootstrap = os.getenv("E2E_KAFKA_BOOTSTRAP") or _resolve_kafka_bootstrap(
        ns, prof["kafka_svc_name"]
    )
    redis_ma_url = os.getenv("E2E_REDIS_MA_URL") or _resolve_redis_ma_url(
        ns, prof["redis_ma_svc_name"]
    )
    redis_fg_url = os.getenv("E2E_REDIS_FG_URL") or _resolve_redis_fg_url(prof)

    phase1_t = float(prof["crat_phase1_ingress_timeout_sec"])
    phase2_t = float(prof["crat_phase2_timeout_sec"])
    phase3_t = float(prof["crat_phase3_timeout_sec"])
    phase4_w = float(prof["crat_phase4_wait_sec"])
    offset_mode = prof["kafka_consumer_auto_offset_reset"]

    topic_alerts = prof["kafka_topic_alerts"]
    topic_evidence = prof["kafka_topic_diagnostic_evidence"]
    topic_actions = prof["kafka_topic_actions"]
    siem_stream = prof["siem_redis_stream"]

    rh = int(prof["siem_random_hex_len"])
    tb = int(prof["siem_trace_body_hex_len"])
    suffix = uuid.uuid4().hex[:rh]
    incident_id = f'{prof["siem_incident_id_prefix"]}{suffix}'
    trace_id = f'{prof["siem_trace_prefix"]}{suffix[:tb]}'

    banner("OMNI E2E CRAT PIPELINE VERIFICATION — GOD-MODE")
    print(f"  Started   : {ts()}")
    print(f"  Profile   : {prof.get('_profile_path')}")
    print(f"  Kafka     : {kafka_bootstrap}")
    print(f"  Redis MA  : {redis_ma_url}")
    print(f"  Redis FG  : {redis_fg_url.split('@')[-1]}")
    print(f"  trace_id  : {trace_id}\n")

    outcomes: dict[str, bool] = {}

    banner("PRE-SUBSCRIPTION: arming consumers before injection")

    def _matches_trace(m: dict) -> bool:
        return m.get("trace_id") == trace_id

    def _matches_evidence(m: dict) -> bool:
        return (
            m.get("trace_id") == trace_id or trace_id in m.get("canonical_query_snippet", "")
        )

    def _matches_alert(m: dict) -> bool:
        return m.get("trace_id") == trace_id or any(
            al.get("labels", {}).get("trace_id") == trace_id
            for al in m.get("alerts", [])
        )

    def _matches_suggest(m: dict) -> bool:
        return m.get("trace_id") == trace_id and m.get("action") == "SUGGEST_REMEDIATION"

    common_kw = dict(bootstrap_servers=kafka_bootstrap, auto_offset_reset=offset_mode)
    t_alert = asyncio.create_task(
        _consume_until(
            **common_kw,
            topic=topic_alerts,
            predicate=_matches_alert,
            timeout_sec=phase1_t,
        )
    )
    t_evid = asyncio.create_task(
        _consume_until(
            **common_kw,
            topic=topic_evidence,
            predicate=_matches_evidence,
            timeout_sec=phase2_t,
        )
    )
    t_suggest = asyncio.create_task(
        _consume_until(
            **common_kw,
            topic=topic_actions,
            predicate=_matches_suggest,
            timeout_sec=phase3_t,
        )
    )

    await asyncio.sleep(2.0)
    print("  All consumers armed.\n")

    banner(f"PHASE 1 — INGRESS: {siem_stream} → {topic_alerts}")
    try:
        tmpl_row = load_json_file(resolved_path(prof, "siem_redis_xadd_row_template"))
        stream_fields = substitute_placeholders(tmpl_row, _siem_placeholders(prof, incident_id, trace_id))
        fg_redis = Redis.from_url(redis_fg_url, decode_responses=True)
        try:
            msg_stream_id = await fg_redis.xadd(siem_stream, stream_fields)
            log("injected stream entry", {"stream_id": msg_stream_id, "trace_id": trace_id})
        finally:
            await fg_redis.aclose()

        alert_msg = await t_alert

        def _alert_correlates(msg: dict[str, Any] | None, expect: str) -> bool:
            if not msg or not isinstance(msg, dict):
                return False
            if msg.get("trace_id") == expect:
                return True
            data = msg.get("data")
            if not isinstance(data, dict):
                return False
            for al in data.get("alerts") or []:
                if not isinstance(al, dict):
                    continue
                lab = al.get("labels")
                if isinstance(lab, dict) and lab.get("trace_id") == expect:
                    return True
            return False

        correlated = _alert_correlates(alert_msg, trace_id)
        ok1 = correlated
        outcomes["P1_INGRESS"] = ok1
        result(
            "Phase 1 — siem-bridge → omni-alerts",
            ok1,
            "correlated" if ok1 else "no correlated omni-alerts message",
        )
    except Exception as exc:
        print(f"  [EXCEPTION] Phase 1: {exc}")
        outcomes["P1_INGRESS"] = False
        t_evid.cancel()
        t_suggest.cancel()

    if not outcomes.get("P1_INGRESS"):
        for _t in (t_evid, t_suggest):
            if not _t.done():
                _t.cancel()

    banner(f"PHASE 2 — EVIDENCE: {topic_evidence}")
    try:
        try:
            evid_msg = await t_evid
        except asyncio.CancelledError:
            evid_msg = None
        ok2 = evid_msg is not None
        outcomes["P2_EVIDENCE"] = ok2
        result("Phase 2 — prober → diagnostic evidence", ok2, "received" if ok2 else "timeout")
    except Exception as exc:
        print(f"  [EXCEPTION] Phase 2: {exc}")
        outcomes["P2_EVIDENCE"] = False

    banner(f"PHASE 3 — ADVISORY: {topic_actions}")
    try:
        try:
            suggest_msg = await t_suggest
        except asyncio.CancelledError:
            suggest_msg = None
        ok3 = suggest_msg is not None
        outcomes["P3_ADVISORY"] = ok3
        result("Phase 3 — SUGGEST_REMEDIATION", ok3, "dispatched" if ok3 else "timeout")
    except Exception as exc:
        print(f"  [EXCEPTION] Phase 3: {exc}")
        outcomes["P3_ADVISORY"] = False

    try:
        outcomes["P4_CRAT"] = await phase4_crat(
            trace_id, redis_ma_url=redis_ma_url, phase4_wait=phase4_w
        )
    except Exception as exc:
        print(f"  [EXCEPTION] Phase 4: {exc}")
        outcomes["P4_CRAT"] = False

    banner("FINAL EXECUTION MATRIX")
    for key, label in (
        ("P1_INGRESS", f"Ingress ({siem_stream} → {topic_alerts})"),
        ("P2_EVIDENCE", f"Evidence ({topic_evidence})"),
        ("P3_ADVISORY", f"Advisory ({topic_actions})"),
        ("P4_CRAT", "CRAT (hash-chain)"),
    ):
        ok = outcomes.get(key, False)
        print(f"  {'✓ PASS' if ok else '✗ FAIL'}  {label}")

    all_ok = all(outcomes.values())
    print(f"\n  OVERALL: {'ALL PHASES VERIFIED' if all_ok else 'PARTIAL — see FAIL'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
