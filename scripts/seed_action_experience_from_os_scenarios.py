"""Seed COLLECTION_ACTION_EXPERIENCE from the 250 OS-hard-fail scenarios.

ingest_os_hard_fail_rag.py already wrote 1000 entry/mid/terminal pairs into
COLLECTION_OS_HARD_FAIL_DIAGNOSTIC, but that collection is never queried by
recall_playbook_advisory() (the function remote_triage.py / evidence_consumer.py
actually call before deciding to invoke the LLM). This script reuses the same
authored scenario content (root_cause + fix per domain) and writes it into
COLLECTION_ACTION_EXPERIENCE with the payload schema remote_diagnostic_archiver.py
uses, and with symptom_text built the same way remote_triage._build_symptom_text
builds its query text — so live triage actually gets a RAG hit.

Run:
  PYTHONPATH=src .venv/bin/python scripts/seed_action_experience_from_os_scenarios.py \\
      --redis-url redis://localhost:16379/0
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

import redis.asyncio as aioredis

from ingest_os_hard_fail_rag import ALL_SCENARIOS, _pad_vec, _vecs_from_embed_response
from llm.factory import build_llm_client
from rag.redis_vector_store import (
    COLLECTION_ACTION_EXPERIENCE,
    PointStruct,
    RedisVectorStore,
)
from workers.settings import WorkerSettings

logger = logging.getLogger(__name__)

BATCH_SIZE = 32

_DOMAIN_TO_LANE = {
    "D0_systemd": "SYS_HARD_FAIL",
    "D1_process": "SYS_HARD_FAIL",
    "D2_storage": "SYS_HARD_FAIL",
    "D3_network": "SYS_HARD_FAIL",
    "D4_database": "SYS_HARD_FAIL",
    "D5_proxy_lb": "SYS_HARD_FAIL",
    "D6_hardware": "SYS_HARD_FAIL",
    "D7_container": "SYS_HARD_FAIL",
}

# Map the scenario-authoring taxonomy (D0_systemd, D2_storage, ...) to the
# canonical domain names pkg.reasoning.domain_signals actually emits at
# runtime — symptom_text must use the same vocabulary as the live query
# text for embeddings to land close in vector space.
_DOMAIN_TO_CANONICAL = {
    "D0_systemd": "services",
    "D1_process": "os_system",
    "D2_storage": "storage",
    "D3_network": "network",
    "D4_database": "database",
    "D5_proxy_lb": "services",
    "D6_hardware": "os_system",
    "D7_container": "container_logs",
}

_RE_ALERT = re.compile(r"alert=(\S+)")
_RE_STEP_FAILED = re.compile(r"step2: probe=(\S+) result=FAILED")


def _build_symptom_text(domain: str, probe: str, lane: str, alertname: str, raw: str) -> str:
    """Mirror workers.remote_triage._build_symptom_text exactly."""
    parts = [f"domain={domain}", f"probe={probe}", f"lane={lane}"]
    if alertname:
        parts.append(f"alert: {alertname}")
    if raw:
        parts.append(f"raw: {raw[:300]}")
    parts.append("failed_count=1")
    return " ".join(parts)


def _extract_terminal_cases() -> list[dict]:
    """Pull (domain, probe, alertname, root_cause, fix, confidence) per scenario."""
    cases: list[dict] = []
    for scenario in ALL_SCENARIOS:
        terminal = scenario[-1]
        if terminal.get("pair_type") != "terminal":
            continue
        domain = terminal["domain"]
        root_cause = terminal.get("root_cause", "")
        fix = terminal.get("fix", "")
        if not root_cause or not fix:
            continue
        m_alert = _RE_ALERT.search(terminal["text"])
        m_probe = _RE_STEP_FAILED.search(terminal["text"])
        alertname = m_alert.group(1) if m_alert else ""
        probe = m_probe.group(1) if m_probe else domain
        cases.append(
            {
                "domain": _DOMAIN_TO_CANONICAL.get(domain, domain),
                "scenario_tag": domain,
                "probe": probe,
                "lane": _DOMAIN_TO_LANE.get(domain, "SYS_HARD_FAIL"),
                "alertname": alertname,
                "root_cause": root_cause,
                "fix": fix,
                "confidence": terminal.get("confidence", 0.9),
            }
        )
    return cases


def _point_id(text: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, text))


async def seed(redis_url: str, ollama_url: str | None, tenant_id: str) -> None:
    ws = WorkerSettings()
    embed_model = getattr(ws, "embed_model", "nomic-embed-text:latest")
    ollama_base = ollama_url or getattr(ws, "vllm_base_url", "http://localhost:11434")
    embed_url = ollama_url or getattr(ws, "vllm_embed_url", ollama_base)

    llm = build_llm_client(
        base_url=ollama_base,
        embed_url=embed_url,
        timeout_s=float(getattr(ws, "llm_chat_timeout_sec", 120.0)),
    )
    r = await aioredis.from_url(redis_url, decode_responses=False)
    store = RedisVectorStore(r)

    cases = _extract_terminal_cases()
    total = len(cases)
    logger.info("seed_action_experience: total_cases=%d collection=%s", total, COLLECTION_ACTION_EXPERIENCE)

    seeded = 0
    for i in range(0, total, BATCH_SIZE):
        batch = cases[i : i + BATCH_SIZE]
        texts = [
            _build_symptom_text(c["domain"], c["probe"], c["lane"], c["alertname"], c["root_cause"])
            for c in batch
        ]
        try:
            resp = await llm.embed(model=embed_model, input=texts, keep_alive="10m")
            vecs = [_pad_vec(v) for v in _vecs_from_embed_response(resp)]
        except Exception as exc:
            logger.error("embed batch failed i=%d err=%r", i, exc)
            raise

        points = []
        for text, vector, case in zip(texts, vecs, batch):
            lesson_text = (
                f"[seed] domain={case['domain']} root_cause={case['root_cause']} "
                f"fix={case['fix']}"
            )
            payload = {
                "memory_kind": "remote_diagnostic",
                "symptom_text": text,
                "workload_fingerprint": f"{case['domain']}:seed",
                "lesson": lesson_text,
                "advisory_verdict": "DIAGNOSE",
                "advisory_root_cause": case["root_cause"],
                "advisory_confidence": case["confidence"],
                "domain": case["domain"],
                "scenario_tag": case["scenario_tag"],
                "lane": case["lane"],
                "probe": case["probe"],
                "fingerprint": f"{case['probe']}:seed",
                "occurrence_count": 1,
                "tool": case["fix"][:120],
                "exec_outcome": "advisory_only",
                "biz_outcome": "seed_knowledge",
                "trace_id": "",
                "ts": "0",
                "text": lesson_text,
                "summary": case["root_cause"][:300],
            }
            points.append(PointStruct(id=_point_id(text), vector=vector, payload=payload))

        await store.upsert(COLLECTION_ACTION_EXPERIENCE, points, tenant_id=tenant_id)
        seeded += len(points)
        logger.info("progress: %d/%d", seeded, total)

    logger.info("seed_action_experience: done seeded=%d", seeded)
    await r.aclose()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Seed COLLECTION_ACTION_EXPERIENCE from OS scenarios")
    parser.add_argument("--redis-url", default="redis://localhost:16379/0")
    parser.add_argument("--ollama-url", default=None)
    parser.add_argument("--tenant-id", default="default")
    args = parser.parse_args()
    asyncio.run(seed(args.redis_url, args.ollama_url, args.tenant_id))


if __name__ == "__main__":
    main()
