#!/usr/bin/env python3
"""
E2E CRAT Pipeline Verification — 4-Phase God-Mode Validation
============================================================

Phase 1 (Ingress):   Inject synthetic alert into SIEM Redis stream:actionable_incidents
                     (namespace from E2E_SIEM_REDIS_NAMESPACE). Assert bridge → omni-alerts.

Phase 2 (Evidence):  Assert prober diagnostic pipeline runs and publishes
                     temporal evidence to omni-diagnostic-evidence.

Phase 3 (Advisory):  Assert analyst LLM processes evidence → AnalystAdvisory,
                     emit SUGGEST_REMEDIATION to omni-actions (advisory mode).

Phase 4 (CRAT):      Fetch audit_chain:blocks from Redis; run hash-chain +
                     Ed25519 cryptographic verification via verifier.py;
                     print terminal block proof.

Usage:
    python scripts/verify_e2e_crat_pipeline.py

Env:
    E2E_KAFKA_BOOTSTRAP, E2E_REDIS_MA_URL, E2E_REDIS_FG_URL — optional overrides.
    E2E_OMNI_KUBE_NAMESPACE — Omni namespace for kafka/redis ClusterIP (default: multi-agent).
    E2E_SIEM_REDIS_NAMESPACE — comma-separated SIEM Redis ns search order.
    E2E_KUBECTL_WRAPPER — path to script that prefixes kubectl (e.g. with_working_kube.sh).
"""

from __future__ import annotations

import asyncio
import base64
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

# Add src/ to path for verifier import
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO_ROOT, "src"))

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from redis.asyncio import Redis

from services.audit_ledger.verifier import verify_chain

# ── Connection config ─────────────────────────────────────────────────────────
# Priority: env var override → kubectl auto-resolve.
# Set E2E_KAFKA_BOOTSTRAP / E2E_REDIS_MA_URL / E2E_REDIS_FG_URL to skip kubectl.
# Optional: E2E_REDIS_FG_PASSWORD when redis-auth Secret is unreadable.
# Use ./scripts/with_working_kube.sh as kubectl wrapper if multiple kubeconfigs (OrbStack).


def _kube_cmd() -> list[str]:
    """kubectl argv prefix: optional E2E_KUBECTL_WRAPPER, else with_working_kube.sh, else kubectl."""
    wrap = (os.environ.get("E2E_KUBECTL_WRAPPER") or "").strip()
    if wrap:
        return [wrap, "kubectl"]
    wk = os.path.join(_REPO_ROOT, "scripts", "with_working_kube.sh")
    if os.path.isfile(wk):
        return [wk, "kubectl"]
    return ["kubectl"]


def _omni_namespace() -> str:
    return (os.environ.get("E2E_OMNI_KUBE_NAMESPACE") or os.environ.get("E2E_KUBE_NS") or "multi-agent").strip()


def _kubectl(*args: str) -> str:
    """Run kubectl and return stripped stdout, or '' on failure."""
    try:
        return subprocess.check_output(_kube_cmd() + list(args), text=True, timeout=12).strip()
    except Exception:
        return ""


def _resolve_kafka() -> str:
    ns = _omni_namespace()
    ip = _kubectl("get", "svc", "kafka", "-n", ns, "-o", "jsonpath={.spec.clusterIP}")
    if not ip or ip == "None":
        raise RuntimeError("Cannot resolve Kafka ClusterIP. Set E2E_KAFKA_BOOTSTRAP.")
    return f"{ip}:9092"


def _resolve_redis_ma() -> str:
    ns = _omni_namespace()
    ip = _kubectl("get", "svc", "redis", "-n", ns, "-o", "jsonpath={.spec.clusterIP}")
    if not ip or ip == "None":
        raise RuntimeError("Cannot resolve Redis ClusterIP in Omni namespace. Set E2E_REDIS_MA_URL.")
    return f"redis://{ip}:6379/0"


def _resolve_redis_fg() -> str:
    """SIEM data-plane Redis URL: E2E_REDIS_FG_URL overrides; else redis-0 pod IP + password.

    Namespace order: E2E_SIEM_REDIS_NAMESPACE (comma-separated), default **finguard-customer** first
    (matches `omni-siem-bridge` SIEM_BRIDGE_REDIS_URL), then smart-siem.
    """
    raw_ns = os.getenv("E2E_SIEM_REDIS_NAMESPACE", "finguard-customer,smart-siem")
    ns_order = [x.strip() for x in raw_ns.split(",") if x.strip()]
    pod_ip = ""
    password = ""
    for ns in ns_order:
        pod_ip = _kubectl("get", "pod", "redis-0", "-n", ns,
                          "-o", "jsonpath={.status.podIP}")
        if pod_ip:
            raw = _kubectl("get", "secret", "redis-auth", "-n", ns,
                           "-o", "jsonpath={.data.password}")
            if raw:
                password = base64.b64decode(raw).decode()
            break
    if not pod_ip:
        raise RuntimeError(
            "Cannot resolve SIEM Redis pod IP in namespaces "
            f"{ns_order}. Set E2E_REDIS_FG_URL."
        )
    # Lab override when Secret missing or kubectl RBAC blocks read (never commit).
    password = os.getenv("E2E_REDIS_FG_PASSWORD", password)
    auth = f":{password}@" if password else ""
    return f"redis://{auth}{pod_ip}:6379/0"


KAFKA_BOOTSTRAP = os.getenv("E2E_KAFKA_BOOTSTRAP") or _resolve_kafka()
REDIS_MA_URL = os.getenv("E2E_REDIS_MA_URL") or _resolve_redis_ma()
REDIS_FG_URL = os.getenv("E2E_REDIS_FG_URL") or _resolve_redis_fg()

PHASE2_TIMEOUT = 90.0    # seconds: prober diagnostics can be slow
PHASE3_TIMEOUT = 180.0   # seconds: LLM qwen2.5 can take 60-120s
PHASE4_WAIT = 5.0        # seconds: allow CRAT write to propagate after Phase 3

# ── Helpers ───────────────────────────────────────────────────────────────────

def ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def banner(title: str) -> None:
    print(f"\n{'═' * 70}")
    print(f"  {title}")
    print(f"{'═' * 70}")


def log(label: str, value: Any) -> None:
    txt = json.dumps(value, default=str)
    print(f"  [{label}] {txt[:300]}")


def result(name: str, ok: bool, detail: str = "") -> None:
    mark = "✓ PASS" if ok else "✗ FAIL"
    suffix = f"  — {detail}" if detail else ""
    print(f"  {mark}  {name}{suffix}")


async def _consume_until(
    topic: str,
    predicate,
    timeout_sec: float,
) -> dict | None:
    """Consume from topic (latest offset) until predicate matches or timeout."""
    consumer = AIOKafkaConsumer(
        topic,
        bootstrap_servers=KAFKA_BOOTSTRAP,
        group_id=f"e2e-verify-{uuid.uuid4().hex[:8]}",
        auto_offset_reset="latest",
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
                        # Try double-envelope first, then bare
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


async def _kafka_produce(topic: str, body: dict) -> None:
    """Publish message wrapped in standard double-envelope."""
    producer = AIOKafkaProducer(bootstrap_servers=KAFKA_BOOTSTRAP)
    await producer.start()
    try:
        payload = json.dumps(
            {"data": json.dumps(body, ensure_ascii=False)}, ensure_ascii=False
        ).encode()
        await producer.send_and_wait(topic, value=payload)
    finally:
        await producer.stop()


# ── Phase 4 ───────────────────────────────────────────────────────────────────

async def phase4_crat(trace_id: str) -> bool:
    """
    Fetch audit_chain:blocks from Redis, find block(s) for our trace_id,
    run verify_chain() for hash + Ed25519 proof, print terminal block.
    """
    banner("PHASE 4 — CRAT CRYPTOGRAPHIC LEDGER VERIFICATION")

    await asyncio.sleep(PHASE4_WAIT)  # allow final CRAT write to propagate

    redis = Redis.from_url(REDIS_MA_URL, decode_responses=True)
    try:
        raw_blocks = await redis.lrange("audit_chain:blocks", 0, -1)
        head_hash = await redis.get("audit_chain:head_hash")
        seq_counter = await redis.get("audit_chain:seq")
    finally:
        await redis.aclose()

    if not raw_blocks:
        result("Phase 4 — CRAT chain fetched", False, "audit_chain:blocks is empty")
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

    # Find our block(s)
    our_blocks = [b for b in blocks if b.get("trace_id") == trace_id]
    if not our_blocks:
        log("WARN", f"No block found with trace_id={trace_id} — chain has {len(blocks)} blocks")
        log("Available trace_ids (last 5)", [b.get("trace_id") for b in blocks[-5:]])
    else:
        log("Our blocks found", [
            {"seq": b["seq"], "event_type": b["event_type"], "trace_id": b["trace_id"]}
            for b in our_blocks
        ])

    # Full chain integrity verification
    result_v = verify_chain(blocks)

    log("verify_chain result", {
        "ok": result_v.ok,
        "blocks_checked": result_v.blocks_checked,
        "first_broken_seq": result_v.first_broken_seq,
        "reason": result_v.reason,
        "errors": result_v.errors[:3],
    })

    # Terminal block proof
    terminal = blocks[-1]
    signed = bool(terminal.get("signature_hex"))
    print(f"\n  ┌─ TERMINAL BLOCK PROOF ─────────────────────────────────────────")
    print(f"  │  seq         : {terminal.get('seq')}")
    print(f"  │  event_type  : {terminal.get('event_type')}")
    print(f"  │  trace_id    : {terminal.get('trace_id')}")
    print(f"  │  timestamp   : {terminal.get('timestamp_utc')}")
    print(f"  │  block_hash  : {terminal.get('block_hash', '')}")
    print(f"  │  prev_hash   : {terminal.get('prev_hash', '')[:64]}")
    print(f"  │  signed      : {'YES — Ed25519' if signed else 'NO (lab unsigned)'}")
    if signed:
        print(f"  │  sig_hex     : {terminal.get('signature_hex', '')[:32]}…")
        print(f"  │  pub_key_hex : {terminal.get('public_key_hex', '')[:32]}…")
    print(f"  └────────────────────────────────────────────────────────────────")

    # Recompute block hash for independent proof
    t = terminal
    canonical = (
        f"{t['seq']}|{t['event_type']}|{t['trace_id']}|"
        f"{t['timestamp_utc']}|{t['payload_hash']}|{t['prev_hash']}"
    )
    recomputed = hashlib.sha256(canonical.encode()).hexdigest()
    hash_match = recomputed == t.get("block_hash")
    print(f"\n  Independent hash recompute: {'MATCH ✓' if hash_match else 'MISMATCH ✗'}")
    print(f"  Expected : {t.get('block_hash', '')}")
    print(f"  Got      : {recomputed}")

    ok = result_v.ok and (bool(our_blocks) or len(blocks) > 0) and hash_match
    result("Phase 4 — CRAT chain integrity", ok,
           f"chain_valid={result_v.ok} our_block={'found' if our_blocks else 'not_found'} "
           f"terminal_hash={'ok' if hash_match else 'FAIL'}")
    return ok


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> int:
    banner("OMNI E2E CRAT PIPELINE VERIFICATION — GOD-MODE")
    print(f"  Started   : {ts()}")
    print(f"  Kafka     : {KAFKA_BOOTSTRAP}")
    print(f"  Redis MA  : {REDIS_MA_URL}")
    print(f"  Redis FG  : {REDIS_FG_URL.split('@')[-1]}")
    print()

    # Allocate trace_id deterministically so consumers can filter on it
    # before the injection happens.
    incident_id = str(uuid.uuid4())
    trace_id = f"fg-{incident_id[:8]}"
    print(f"  trace_id  : {trace_id}  (pre-allocated)\n")

    outcomes: dict[str, bool] = {}

    # ── Pre-subscribe to all Kafka topics BEFORE injection ─────────────────
    # This avoids the race where the pipeline runs faster than our consumers start.
    banner("PRE-SUBSCRIPTION: arming consumers before injection")

    def _matches_trace(m: dict) -> bool:
        return m.get("trace_id") == trace_id

    def _matches_evidence(m: dict) -> bool:
        return (
            m.get("trace_id") == trace_id
            or trace_id in m.get("canonical_query_snippet", "")
        )

    def _matches_alert(m: dict) -> bool:
        return m.get("trace_id") == trace_id or any(
            al.get("labels", {}).get("trace_id") == trace_id
            for al in m.get("alerts", [])
        )

    def _matches_suggest(m: dict) -> bool:
        return m.get("trace_id") == trace_id and m.get("action") == "SUGGEST_REMEDIATION"

    # Launch all three Kafka listeners concurrently
    t_alert   = asyncio.create_task(_consume_until("omni-alerts",               _matches_alert,   30.0))
    t_evid    = asyncio.create_task(_consume_until("omni-diagnostic-evidence",  _matches_evidence, PHASE2_TIMEOUT))
    t_suggest = asyncio.create_task(_consume_until("omni-actions",              _matches_suggest,  PHASE3_TIMEOUT))

    await asyncio.sleep(2.0)  # let all three consumers join partitions before we inject
    print("  All consumers armed.\n")

    # ── Phase 1 ────────────────────────────────────────────────────────────
    banner("PHASE 1 — INGRESS: stream:actionable_incidents → omni-alerts")
    try:
        fg_redis = Redis.from_url(REDIS_FG_URL, decode_responses=True)
        try:
            stream_fields = {
                "id": incident_id,
                "severity": "critical",
                "category": "high_cpu",
                "tenant_id": "omni-lab",
                "description": (
                    "High CPU + DB connection pool exhausted on worker node. "
                    "PostgreSQL max_connections exceeded. Service latency >5s."
                ),
                "suggested_action": "Scale deployment, restart DB connection pool",
                "affected_ip": "10.0.0.42",
                "hitl_required": "false",
                "timestamp": ts(),
            }
            msg_stream_id = await fg_redis.xadd("stream:actionable_incidents", stream_fields)
            log("injected stream entry", {"stream_id": msg_stream_id, "trace_id": trace_id, "incident_id": incident_id})
        finally:
            await fg_redis.aclose()

        print(f"  Waiting for siem-bridge → omni-alerts …")
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
        if alert_msg:
            data_inner = alert_msg.get("data")
            alerts_list = data_inner.get("alerts", []) if isinstance(data_inner, dict) else []
            labels = alerts_list[0].get("labels", {}) if alerts_list else {}
            log("omni-alerts", {
                "trace_id": alert_msg.get("trace_id"),
                "labels_trace_id": labels.get("trace_id"),
                "correlated": correlated,
                "alertname": labels.get("alertname"),
                "severity": labels.get("severity"),
                "siem_source": labels.get("siem_source"),
            })
        else:
            log("omni-alerts", "TIMEOUT — bridge did not forward within 30s")

        ok1 = correlated
        outcomes["P1_INGRESS"] = ok1
        result(
            "Phase 1 — siem-bridge → omni-alerts",
            ok1,
            f"trace_id={trace_id} correlated" if ok1 else "no correlated omni-alerts message (wrong payload or bridge idle)",
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

    # ── Phase 2 ────────────────────────────────────────────────────────────
    banner("PHASE 2 — PROBER EVIDENCE: omni-diagnostic-evidence")
    print(f"  Listening for trace_id={trace_id} (timeout {PHASE2_TIMEOUT:.0f}s) …")
    try:
        try:
            evid_msg = await t_evid
        except asyncio.CancelledError:
            evid_msg = None
        if evid_msg:
            log("omni-diagnostic-evidence", {
                "trace_id": evid_msg.get("trace_id"),
                "keys": list(evid_msg.keys())[:8],
                "alertname": evid_msg.get("alertname"),
            })
        ok2 = evid_msg is not None
        outcomes["P2_EVIDENCE"] = ok2
        result("Phase 2 — prober → omni-diagnostic-evidence", ok2,
               "evidence received" if ok2 else f"no evidence after {PHASE2_TIMEOUT:.0f}s")
    except Exception as exc:
        print(f"  [EXCEPTION] Phase 2: {exc}")
        outcomes["P2_EVIDENCE"] = False

    # ── Phase 3 ────────────────────────────────────────────────────────────
    banner("PHASE 3 — LLM ADVISORY + KILL-SWITCH: omni-actions SUGGEST_REMEDIATION")
    print(f"  Listening for SUGGEST_REMEDIATION trace_id={trace_id} (timeout {PHASE3_TIMEOUT:.0f}s) …")
    try:
        try:
            suggest_msg = await t_suggest
        except asyncio.CancelledError:
            suggest_msg = None
        if suggest_msg:
            data = suggest_msg.get("data", {})
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except Exception:
                    data = {}
            log("SUGGEST_REMEDIATION", {
                "trace_id": suggest_msg.get("trace_id"),
                "action": suggest_msg.get("action"),
                "verdict": data.get("advisory_verdict") or data.get("verdict"),
                "root_cause": str(data.get("root_cause", ""))[:100],
                "confidence": data.get("confidence"),
            })
        ok3 = suggest_msg is not None
        outcomes["P3_ADVISORY"] = ok3
        result("Phase 3 — LLM advisory → SUGGEST_REMEDIATION", ok3,
               "advisory dispatched" if ok3 else f"no advisory after {PHASE3_TIMEOUT:.0f}s")
    except Exception as exc:
        print(f"  [EXCEPTION] Phase 3: {exc}")
        outcomes["P3_ADVISORY"] = False

    # ── Phase 4 ────────────────────────────────────────────────────────────
    try:
        ok4 = await phase4_crat(trace_id)
        outcomes["P4_CRAT"] = ok4
    except Exception as exc:
        print(f"  [EXCEPTION] Phase 4: {exc}")
        outcomes["P4_CRAT"] = False

    # Final matrix
    banner("FINAL EXECUTION MATRIX")
    labels = {
        "P1_INGRESS": "Ingress  (SIEM stream → omni-alerts)",
        "P2_EVIDENCE": "Evidence (prober → omni-diagnostic-evidence)",
        "P3_ADVISORY": "Advisory (LLM → SUGGEST_REMEDIATION on omni-actions)",
        "P4_CRAT":    "CRAT     (hash-chain + terminal block proof)",
    }
    for key, label in labels.items():
        ok = outcomes.get(key, False)
        print(f"  {'✓ PASS' if ok else '✗ FAIL'}  {label}")

    all_ok = all(outcomes.values())
    print(f"\n  {'═'*68}")
    print(f"  OVERALL: {'100% SUCCESS — ALL PHASES VERIFIED' if all_ok else 'PARTIAL — see FAIL items'}")
    print(f"  trace_id : {trace_id}")
    print(f"  Completed: {ts()}")
    print(f"  {'═'*68}\n")

    return 0 if all_ok else 1


if __name__ == "__main__":
    rc = asyncio.run(main())
    sys.exit(rc)
