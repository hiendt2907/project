#!/usr/bin/env python3
"""Parity test: Python siem_correlation engine vs Go brain-go (live, in-pod).

Chạy TRONG pod omni-fullstack (kubectl cp + kubectl exec) để dùng DNS nội
cluster. Cách ly hoàn toàn với engine Go đang chạy:
  - consumer group riêng (parity-<runid>) đọc cùng ``omni-siem-raw``;
  - state union-find dùng Redis prefix ``pycorr-<runid>:`` (TTL tự dọn);
  - KHÔNG produce gì lên Kafka từ phía Python — output giữ in-memory và so
    với những gì Go THẬT SỰ emit lên ``omni-siem-incidents``/``omni-siem-chains``.

Kịch bản inject (tenant parity-<runid>, không đụng tenant thật):
  - 20 noise events (entity rời rạc, không chain)
  - Attack A: 3 events chung ip+user, stages recon→access→execution → 1 chain
  - 1 event thứ 4 cùng component trong dedup window → KHÔNG chain thứ 2
  - Attack B: 3 events nối bắc cầu user→host (union-find transitive) → 1 chain

So sánh:
  - incidents: envelope Python == envelope Go (khớp từng field, theo id)
  - chains: khớp mọi field trừ chain_id/timestamp_unix (uuid/clock) và
    member_events[].timestamp_unix (arrival clock, chỉ so thứ tự id)

Exit 0 = parity PASS; exit 1 = FAIL (in diff).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

sys.path.insert(0, "/app/src")

from services.siem_correlation.decode import decode_kafka_message, incident_envelope  # noqa: E402
from services.siem_correlation.graph import GraphConfig, GraphCorrelator  # noqa: E402

BOOTSTRAP = os.getenv("PARITY_KAFKA_BOOTSTRAP", "kafka.multi-agent.svc.cluster.local:9092")
REDIS_URL = os.getenv("PARITY_REDIS_URL", "redis://redis.multi-agent.svc.cluster.local:6379/0")
RAW_TOPIC = os.getenv("PARITY_RAW_TOPIC", "omni-siem-raw")
GO_INCIDENTS_TOPIC = os.getenv("PARITY_GO_INCIDENTS_TOPIC", "omni-siem-incidents")
GO_CHAINS_TOPIC = os.getenv("PARITY_GO_CHAINS_TOPIC", "omni-siem-chains")
WAIT_SECONDS = int(os.getenv("PARITY_WAIT_SECONDS", "45"))
EVENT_SPACING_S = float(os.getenv("PARITY_EVENT_SPACING_S", "1.5"))

RUN_ID = uuid.uuid4().hex[:8]
TENANT = f"parity-{RUN_ID}"
ATTACK_IP_A = "203.0.113.77"


def _raw(category: str, severity: str, source_ip: str, desc: str) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "tenant_id": TENANT,
        "severity": severity,
        "source": "agent",
        "category": category,
        "timestamp_unix": int(time.time()),
        "source_ip": source_ip,
        "description": desc,
        "schema_version": "1.0.0",
    }


def build_scenario() -> tuple[list[dict], list[dict]]:
    """Returns (fast_events, spaced_events) — spaced events cần cách nhau
    >1s để arrival-clock ordering deterministic ở cả 2 engine."""
    noise = [
        _raw("network_anomaly", "low", f"10.20.0.{i}", f"user=noise{i} host=nh{i}")
        for i in range(20)
    ]
    attack_a = [
        _raw("port_scan", "medium", ATTACK_IP_A, "port scan user=mallory host=web-01"),
        _raw("auth_failure", "high", ATTACK_IP_A, "auth failure user=mallory host=db-02"),
        _raw("new_process", "high", ATTACK_IP_A, "new process user=mallory host=app-03 process=nc"),
        # 4th event same component within dedup window → must NOT re-emit.
        _raw("malware", "high", ATTACK_IP_A, "malware user=mallory host=app-03"),
    ]
    attack_b = [
        _raw("auth_failure", "medium", "198.51.100.9", "auth failure user=eve"),
        _raw("new_process", "high", "", "new process user=eve host=cache-01"),
        _raw("lateral_movement", "high", "198.51.100.10", "lateral movement host=cache-01"),
    ]
    return noise, attack_a + attack_b


async def run_python_engine(stop: asyncio.Event, outputs: dict) -> None:
    """Consume omni-siem-raw exactly như loop production nhưng giữ output
    in-memory (không produce)."""
    import redis.asyncio as aioredis

    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    env = dict(os.environ)
    env["OMNI_SIEM_CORR_KEY_PREFIX"] = f"pycorr-{RUN_ID}:"
    correlator = GraphCorrelator(r, GraphConfig.from_env(env))

    consumer = AIOKafkaConsumer(
        RAW_TOPIC,
        bootstrap_servers=BOOTSTRAP,
        group_id=f"parity-py-{RUN_ID}",
        auto_offset_reset="latest",
        enable_auto_commit=True,
    )
    await consumer.start()
    try:
        while not stop.is_set():
            batch = await consumer.getmany(timeout_ms=500)
            for msgs in batch.values():
                for msg in msgs:
                    inc = decode_kafka_message(msg.value)
                    if inc is None or inc.tenant_id != TENANT:
                        continue
                    outputs["incidents"].append(incident_envelope(inc))
                    chain = await correlator.process(inc)
                    if chain is not None:
                        outputs["chains"].append(chain)
    finally:
        await consumer.stop()
        await r.aclose()


async def collect_go_outputs(stop: asyncio.Event, outputs: dict) -> None:
    consumer = AIOKafkaConsumer(
        GO_INCIDENTS_TOPIC,
        GO_CHAINS_TOPIC,
        bootstrap_servers=BOOTSTRAP,
        group_id=f"parity-go-collect-{RUN_ID}",
        auto_offset_reset="latest",
        enable_auto_commit=True,
    )
    await consumer.start()
    try:
        while not stop.is_set():
            batch = await consumer.getmany(timeout_ms=500)
            for tp, msgs in batch.items():
                for msg in msgs:
                    try:
                        doc = json.loads(msg.value)
                    except ValueError:
                        continue
                    if doc.get("tenant_id") != TENANT:
                        continue
                    if tp.topic == GO_INCIDENTS_TOPIC:
                        outputs["incidents"].append(doc)
                    else:
                        outputs["chains"].append(doc)
    finally:
        await consumer.stop()


def normalize_chain(chain: dict) -> dict:
    c = dict(chain)
    c.pop("chain_id", None)
    c.pop("timestamp_unix", None)
    c["member_events"] = [
        {k: v for k, v in m.items() if k != "timestamp_unix"}
        for m in (c.get("member_events") or [])
    ]
    return c


def compare(py: dict, go: dict) -> list[str]:
    diffs: list[str] = []

    py_inc = {e["id"]: e for e in py["incidents"]}
    go_inc = {e["id"]: e for e in go["incidents"]}
    if set(py_inc) != set(go_inc):
        diffs.append(
            f"incident id sets differ: only_py={sorted(set(py_inc) - set(go_inc))} "
            f"only_go={sorted(set(go_inc) - set(py_inc))}"
        )
    for id_ in sorted(set(py_inc) & set(go_inc)):
        if py_inc[id_] != go_inc[id_]:
            diffs.append(f"incident {id_} differs:\n  py={py_inc[id_]}\n  go={go_inc[id_]}")

    py_chains = sorted(
        (normalize_chain(c) for c in py["chains"]),
        key=lambda c: c["attack_category"],
    )
    go_chains = sorted(
        (normalize_chain(c) for c in go["chains"]),
        key=lambda c: c["attack_category"],
    )
    if len(py_chains) != len(go_chains):
        diffs.append(f"chain count differs: py={len(py_chains)} go={len(go_chains)}")
    for i, (pc, gc) in enumerate(zip(py_chains, go_chains)):
        if pc != gc:
            for key in sorted(set(pc) | set(gc)):
                if pc.get(key) != gc.get(key):
                    diffs.append(
                        f"chain[{i}] field {key!r} differs:\n  py={pc.get(key)}\n  go={gc.get(key)}"
                    )
    return diffs


async def main() -> int:
    print(f"[parity] run_id={RUN_ID} tenant={TENANT} bootstrap={BOOTSTRAP}")
    stop = asyncio.Event()
    py_out: dict = {"incidents": [], "chains": []}
    go_out: dict = {"incidents": [], "chains": []}

    py_task = asyncio.create_task(run_python_engine(stop, py_out))
    go_task = asyncio.create_task(collect_go_outputs(stop, go_out))
    await asyncio.sleep(5)  # let both consumers join at latest offsets

    producer = AIOKafkaProducer(
        bootstrap_servers=BOOTSTRAP,
        value_serializer=lambda v: json.dumps(v, ensure_ascii=False).encode(),
    )
    await producer.start()
    try:
        fast, spaced = build_scenario()
        for ev in fast:
            await producer.send_and_wait(RAW_TOPIC, ev)
        print(f"[parity] injected {len(fast)} noise events")
        for ev in spaced:
            ev["timestamp_unix"] = int(time.time())
            await producer.send_and_wait(RAW_TOPIC, ev)
            await asyncio.sleep(EVENT_SPACING_S)
        print(f"[parity] injected {len(spaced)} attack events (spaced {EVENT_SPACING_S}s)")
    finally:
        await producer.stop()

    print(f"[parity] waiting {WAIT_SECONDS}s for both engines to drain...")
    await asyncio.sleep(WAIT_SECONDS)
    stop.set()
    await asyncio.gather(py_task, go_task, return_exceptions=True)

    print(
        f"[parity] collected: py incidents={len(py_out['incidents'])} chains={len(py_out['chains'])} | "
        f"go incidents={len(go_out['incidents'])} chains={len(go_out['chains'])}"
    )
    expected_incidents = 27
    problems: list[str] = []
    if len(py_out["incidents"]) != expected_incidents:
        problems.append(f"py engine saw {len(py_out['incidents'])} incidents, expected {expected_incidents}")
    if len(py_out["chains"]) != 2:
        problems.append(f"py engine emitted {len(py_out['chains'])} chains, expected 2 (A + B, dedup on 4th)")

    problems.extend(compare(py_out, go_out))

    if problems:
        print("[parity] FAIL")
        for p in problems:
            print(" -", p)
        return 1
    print("[parity] PASS — Python output == Go output on identical input")
    for c in py_out["chains"]:
        print(f"  chain {c['attack_category']} conf={c['confidence']} members={len(c['member_events'])}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
