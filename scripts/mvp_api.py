"""
MVP FastAPI: 3-Lane Universal Reasoner → HighLevelRemediationPlan.

Sprint 1 — Canonical lane names (resource / state / app_log), fail-closed Loki,
           is_api_web_workload guard.
Sprint 2 — resolve_proof_lane replaces hard-coded classifier (matrix + heuristics).
Sprint 3 — evaluate_diagnostic_invariants (INV_*) gate before kubectl.
Sprint 4 — RAG enrichment: Ollama embed + pgvector action_experience (fail-open).
Sprint 5 — Redis shadow write-back after successful execution (TTL 24 h).
Sprint 6 — pytest suite: tests/test_mvp_api.py.

Pipeline:
  phase1_parse    — Dispatcher via resolve_proof_lane
  phase2_transform — Lane-aware enrichment + RAG + prompt construction
  phase3_output   — Ollama → HighLevelRemediationPlan
  phase4_execute  — Invariant gate → kubectl (lab) → Shadow write-back

Lane RESOURCE  — CPU/RAM/Network → ThreeSigmaGate (3-sigma statistical gate)
Lane STATE     — OOMKilled/CrashLoop/NodeDown → immediate Reasoner (no sigma)
Lane APP_LOG   — api_web workloads only → Loki log-surge; fail-closed on unavailable

Usage:
    uvicorn scripts.mvp_api:app --reload
    OMNI_ENV_MODE=lab OMNI_REDIS_URL=redis://localhost:6379 uvicorn scripts.mvp_api:app --reload

Test (Lane STATE — OOMKilled):
    curl -s -X POST http://localhost:8000/alert \\
      -H "Content-Type: application/json" \\
      -d '{"alertname":"KubePodOOMKilled","namespace":"production",
           "pod":"api-server-7d9f8b6c4-xk2pq","container":"api-server",
           "severity":"critical","memory_limit":"512Mi"}' | python -m json.tool

Test (Lane RESOURCE — high CPU):
    curl -s -X POST http://localhost:8000/alert \\
      -H "Content-Type: application/json" \\
      -d '{"alertname":"HighCPUUsage","namespace":"production",
           "pod":"api-server-7d9f8b6c4-xk2pq","container":"api-server",
           "severity":"warning","memory_limit":"512Mi"}' | python -m json.tool

Test (Lane APP_LOG — api_web 5xx):
    curl -s -X POST http://localhost:8000/alert \\
      -H "Content-Type: application/json" \\
      -d '{"alertname":"HttpErrorRate5xx","namespace":"production",
           "pod":"api-server-7d9f8b6c4-xk2pq","container":"api-server",
           "severity":"critical","message":"sustained 503 errors on /checkout"}' | python -m json.tool
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from enum import Enum
from types import SimpleNamespace
from typing import Any

# Ensure src/ is on the path so Omni modules are importable when running from project root.
_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import httpx
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from anomaly.three_sigma import ThreeSigmaGate
from workers.k8s_cluster_tools import (
    ApplyRbacLeastPrivilegeArgs,
    CreateOrPatchConfigMapArgs,
    PatchResourceArgs,
    RolloutRestartArgs,
    get_resource_owner,
    tool_k8s_apply_rbac_least_privilege,
    tool_k8s_create_or_patch_configmap,
    tool_k8s_patch_resource,
    tool_k8s_rollout_restart,
)
from pkg.autonomy.llm_contract import (
    STRICT_REMEDIATION_JSON_SCHEMA,
    ActionRecord,
    HighLevelRemediationPlan,
    ObservationRecord,
    OutcomeRecord,
    RemediationContext,
    map_high_level_plan_to_mutate,
    parse_high_level_plan_json,
)
from pkg.reasoning.diagnostic_policy import evaluate_diagnostic_invariants
from pkg.reasoning.incident_matrix_profile import (
    is_api_web_workload,
    resolve_proof_lane,
)
from workers.k8s_tools import _load_k8s_config
from workers.log_surge_probe import evaluate_log_surge_sigma_bypass

from kubernetes_asyncio import client as k8s_client

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

def _default_vllm_url() -> str:
    """
    Resolve LLM base URL.
    Priority: VLLM_BASE_URL env → OLLAMA_BASE_URL env (backwards-compat) →
              in-cluster: Ollama on macOS host via OrbStack DNS → localhost.
    """
    explicit = os.getenv("VLLM_BASE_URL") or os.getenv("OLLAMA_BASE_URL")
    if explicit:
        return explicit.rstrip("/")
    if os.getenv("KUBERNETES_SERVICE_HOST"):
        return "http://host.orb.internal:11434/v1"
    return "http://localhost:11434/v1"


VLLM_BASE_URL = _default_vllm_url()
# Model name as registered in Ollama (ollama pull qwen3.6).
VLLM_MODEL = os.getenv("VLLM_MODEL", os.getenv("OLLAMA_MODEL", "qwen3.6"))

MAX_LOOP_ITERATIONS = int(os.getenv("OMNI_MAX_LOOP_ITERATIONS", "3"))
VERIFY_BACKOFF_SECONDS = float(os.getenv("OMNI_VERIFY_BACKOFF_SECONDS", "5"))

def _default_embedder_url() -> str:
    """
    Resolve embedder base URL (same Ollama instance as chat).
    Priority: VLLM_EMBEDDER_URL env → in-cluster: OrbStack DNS → localhost.
    """
    explicit = os.getenv("VLLM_EMBEDDER_URL")
    if explicit:
        return explicit.rstrip("/")
    if os.getenv("KUBERNETES_SERVICE_HOST"):
        return "http://host.orb.internal:11434/v1"
    return "http://localhost:11434/v1"

VLLM_EMBEDDER_URL = _default_embedder_url()
VLLM_EMBED_MODEL = os.getenv("VLLM_EMBED_MODEL", "nomic-embed-text")
LOKI_BASE_URL = os.getenv("LOKI_BASE_URL", "")
POSTGRES_RAG_DSN = os.getenv("POSTGRES_RAG_DSN", "")

@asynccontextmanager
async def _lifespan(application: FastAPI):  # noqa: ARG001
    """
    Wave A1 — Startup privilege audit.

    Detects if the process is running under a cluster-admin ServiceAccount and
    emits a SECURITY warning.  Does not block execution — the invariant gates
    (INV_NAMESPACE_ISOLATION) enforce the namespace boundary at mutation time.

    Only runs when kubectl is available and we appear to be inside a cluster
    (KUBERNETES_SERVICE_HOST set).  Skips silently in local dev.
    """
    if os.getenv("KUBERNETES_SERVICE_HOST"):
        try:
            result = subprocess.run(
                ["kubectl", "auth", "can-i", "*", "*", "--all-namespaces"],
                capture_output=True, text=True, timeout=5,
            )
            if result.stdout.strip() == "yes":
                log.warning(
                    "SECURITY [Wave A1]: executor ServiceAccount has cluster-wide wildcard "
                    "permissions (cluster-admin or equivalent). This violates least-privilege "
                    "policy. Apply k8s/deployments/executor-rbac.yaml and set "
                    "serviceAccountName: omni-executor in omni-executor.yaml."
                )
            else:
                log.info("SECURITY [Wave A1]: executor SA privilege check passed — no wildcard cluster access.")
        except FileNotFoundError:
            log.debug("kubectl not found — skipping privilege audit (expected in local dev).")
        except Exception as exc:
            log.debug("Privilege audit skipped: %s", exc)
    yield


app = FastAPI(title="Omni MVP — 3-Lane Universal Reasoner", version="0.3.0", lifespan=_lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness probe — used by scripts/e2e_incident_matrix.sh reachability check (curl /healthz)."""

    return {"status": "ok"}


# Error code returned when Loki is unavailable in the app_log lane.
ERR_REA_LOG_SOURCE_UNAVAILABLE = "ERR_REA_LOG_SOURCE_UNAVAILABLE"

# Minimal WorkerSettings proxy — evaluate_diagnostic_invariants only needs
# autonomous_allowed_namespaces (read by namespace_allowed()).
_WS = SimpleNamespace(
    env_mode=os.getenv("OMNI_ENV_MODE", "lab"),
    autonomous_allowed_namespaces=os.getenv(
        "OMNI_AUTONOMOUS_ALLOWED_NAMESPACES",
        "default,production,staging,multi-agent",
    ),
)


# ---------------------------------------------------------------------------
# Lane taxonomy (canonical names — matches proof_lane contract)
# ---------------------------------------------------------------------------

class Lane(str, Enum):
    RESOURCE = "resource"   # CPU/RAM/Network → 3-sigma rolling gate
    STATE    = "state"      # OOMKilled/CrashLoop/NodeDown → physical proof
    APP_LOG  = "app_log"    # api_web workloads, Loki log-surge; fail-closed


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class AlertInput(BaseModel):
    """Raw alert payload from Alertmanager or curl."""

    alertname: str
    namespace: str
    pod: str
    container: str
    severity: str = "critical"
    memory_limit: str = ""
    message: str = ""


@dataclass
class LanedAlert:
    """Normalised alert with lane classification. lane_meta populated in phase2."""

    alertname: str
    namespace: str
    pod: str
    container: str
    severity: str
    memory_limit: str
    message: str
    deployment_name: str
    trace_id: str
    lane: Lane
    lane_source: str = "default"        # source from resolve_proof_lane
    lane_meta: dict[str, Any] = field(default_factory=dict)
    # Minimal evidence batch — shared by lane resolver + invariant checker.
    batch: list[dict[str, Any]] = field(default_factory=list)


class ExecutionResponse(BaseModel):
    """API response: lane classification + remediation plan + execution status."""

    trace_id: str
    lane: str
    lane_source: str = "default"
    lane_meta: dict[str, Any]
    plan: HighLevelRemediationPlan
    executed: bool = False
    iterations: int = 1
    converged: bool = False
    resolution_state: str = "incomplete"
    # Values: "converged" | "sigma_gate" | "loki_unavailable" | "blocked" | "exec_skipped" | "exhausted" | "incomplete"


# ---------------------------------------------------------------------------
# Memory parser helper
# ---------------------------------------------------------------------------

# Kubernetes memory suffixes → MiB (longest first to avoid prefix collision).
_MEM_UNIT_TO_MIB: list[tuple[str, float]] = [
    ("Ti", 1024.0 * 1024.0),
    ("Gi", 1024.0),
    ("Mi", 1.0),
    ("Ki", 1.0 / 1024.0),
    ("T", 1024.0 * 1024.0),
    ("G", 1024.0),
    ("M", 1.0),
    ("K", 1.0 / 1024.0),
]


def _parse_memory_mib(s: str) -> float:
    """Parse Kubernetes memory string to MiB. '512Mi' → 512.0, '1Gi' → 1024.0."""
    s = s.strip()
    for suffix, factor in _MEM_UNIT_TO_MIB:
        if s.endswith(suffix):
            try:
                return float(s[: -len(suffix)]) * factor
            except ValueError:
                return 0.0
    try:
        return float(s) / (1024 * 1024)  # bare number assumed bytes
    except ValueError:
        return 0.0


def _build_minimal_batch(raw: AlertInput) -> list[dict[str, Any]]:
    """
    Build minimal evidence batch compatible with resolve_proof_lane and
    evaluate_diagnostic_invariants.

    Encodes alertname + message into alert_hint so state_lane_heuristic()
    can match OOMKilled/CrashLoopBackOff patterns. Labels go in
    canonical_query_snippet so alertname_from_batch() can extract them.
    """
    hint = f"{raw.alertname} {raw.message}".strip()
    labels: dict[str, str] = {
        "alertname": raw.alertname,
        "namespace": raw.namespace,
        "pod": raw.pod,
    }
    return [{
        "alert_rule": raw.alertname,
        "alert_hint": hint,
        "canonical_query_snippet": json.dumps({"labels": labels}),
    }]


# ---------------------------------------------------------------------------
# Phase 1: Dispatcher via resolve_proof_lane
# ---------------------------------------------------------------------------

def phase1_parse(raw: AlertInput) -> LanedAlert:
    """
    Phase 1 — Dispatcher.

    Uses resolve_proof_lane (annotation > matrix > heuristics > default) to
    assign one of the three canonical lanes.

    APP_LOG guard: if lane resolved to app_log but is_api_web_workload() is False,
    downgrade to resource.  The Loki log-surge bypass is only valid for API/Web
    workloads; a non-api_web workload would never satisfy the sigma bypass guard.
    """
    parts = raw.pod.split("-")
    deployment_name = "-".join(parts[:-2]) if len(parts) > 2 else raw.pod
    trace_id = str(uuid.uuid4())

    batch = _build_minimal_batch(raw)
    lane_str, lane_source = resolve_proof_lane(batch)

    if lane_str == "app_log" and not is_api_web_workload(batch):
        log.info(
            "[%s] app_log lane resolved but is_api_web_workload=False — downgrade to resource",
            trace_id,
        )
        lane_str = "resource"
        lane_source = "api_web_guard"

    lane = Lane(lane_str)
    log.info(
        "[%s] lane=%s source=%s alertname=%s ns=%s",
        trace_id, lane.value, lane_source, raw.alertname, raw.namespace,
    )
    return LanedAlert(
        alertname=raw.alertname,
        namespace=raw.namespace,
        pod=raw.pod,
        container=raw.container,
        severity=raw.severity,
        memory_limit=raw.memory_limit,
        message=raw.message,
        deployment_name=deployment_name,
        trace_id=trace_id,
        lane=lane,
        lane_source=lane_source,
        batch=batch,
    )


# ---------------------------------------------------------------------------
# Phase 2a: Lane-aware enrichment
# ---------------------------------------------------------------------------

async def _enrich_resource(laned: LanedAlert) -> dict[str, Any]:
    """
    Lane RESOURCE enricher — ThreeSigmaGate on memory_limit metric.

    gate_blocked=True when |z| <= 3 (not anomalous → caller short-circuits to noop).
    gate_skipped=True when Redis is unavailable or metric is unparseable.
    """
    meta: dict[str, Any] = {"gate": "3sigma"}

    mem_mib = _parse_memory_mib(laned.memory_limit)
    meta["metric_value_mib"] = mem_mib
    if mem_mib <= 0.0:
        log.warning("[%s] memory_limit %r unparseable — 3-sigma gate skipped", laned.trace_id, laned.memory_limit)
        meta["gate_skipped"] = True
        return meta

    redis_url = os.getenv("OMNI_REDIS_URL") or os.getenv("REDIS_URL")
    if not redis_url:
        log.warning("[%s] OMNI_REDIS_URL not set — 3-sigma gate skipped", laned.trace_id)
        meta["gate_skipped"] = True
        return meta

    try:
        import redis.asyncio as aioredis

        r = aioredis.from_url(redis_url, decode_responses=True)
        gate = ThreeSigmaGate(r)
        metric_id = f"mem:{laned.namespace}/{laned.deployment_name}"
        is_anomaly, z_score = await gate.observe(metric_id, mem_mib)
        await r.aclose()

        meta["is_anomaly"] = is_anomaly
        meta["z_score"] = z_score
        log.info(
            "[%s] 3sigma metric=%s value_mib=%.1f is_anomaly=%s z=%.3f",
            laned.trace_id, metric_id, mem_mib, is_anomaly, z_score or 0.0,
        )
        if not is_anomaly:
            meta["gate_blocked"] = True

    except Exception as exc:
        log.warning("[%s] 3-sigma gate error — proceeding: %s", laned.trace_id, exc)
        meta["gate_error"] = str(exc)[:200]

    return meta


async def _enrich_state(laned: LanedAlert) -> dict[str, Any]:
    """Lane STATE enricher — no statistical gate; inject pod identity context."""
    return {
        "alertname": laned.alertname,
        "namespace": laned.namespace,
        "pod": laned.pod,
        "container": laned.container,
        "deployment": laned.deployment_name,
        "message": laned.message,
    }


async def _enrich_app_log(laned: LanedAlert) -> dict[str, Any]:
    """
    Lane APP_LOG enricher — Loki log-surge analysis. Fail-closed.

    Sets meta["loki_unavailable"] = True and meta["error_code"] =
    ERR_REA_LOG_SOURCE_UNAVAILABLE when:
      - LOKI_BASE_URL is not set
      - Loki is unreachable or returns escalate_log_unavailable=True
      - Any exception during Loki query

    The caller (phase4_execute) must check loki_unavailable and return a noop
    without calling the LLM.  No mutation is allowed without log evidence.
    """
    meta: dict[str, Any] = {"gate": "loki_log_surge"}

    if not LOKI_BASE_URL:
        log.warning("[%s] LOKI_BASE_URL not set — %s", laned.trace_id, ERR_REA_LOG_SOURCE_UNAVAILABLE)
        meta["loki_unavailable"] = True
        meta["error_code"] = ERR_REA_LOG_SOURCE_UNAVAILABLE
        return meta

    try:
        result = await evaluate_log_surge_sigma_bypass(
            loki_base_url=LOKI_BASE_URL,
            namespace=laned.namespace,
            pod_name=laned.pod,
            window_sec=300,
            min_lines=5,
            min_ratio=0.3,
            line_limit=200,
            timeout_sec=10.0,
        )
        meta["log_surge_ok"] = result.ok
        meta["log_surge_reason"] = result.reason
        meta.update(result.meta)

        if result.escalate_log_unavailable:
            log.warning(
                "[%s] Loki escalate_log_unavailable — %s (fail-closed, no mutate)",
                laned.trace_id, ERR_REA_LOG_SOURCE_UNAVAILABLE,
            )
            meta["loki_unavailable"] = True
            meta["error_code"] = ERR_REA_LOG_SOURCE_UNAVAILABLE

    except Exception as exc:
        log.warning("[%s] Loki error — %s: %s", laned.trace_id, ERR_REA_LOG_SOURCE_UNAVAILABLE, exc)
        meta["loki_unavailable"] = True
        meta["error_code"] = ERR_REA_LOG_SOURCE_UNAVAILABLE
        meta["gate_error"] = str(exc)[:200]

    return meta


_ENRICHERS: dict[Lane, Any] = {
    Lane.RESOURCE: _enrich_resource,
    Lane.STATE:    _enrich_state,
    Lane.APP_LOG:  _enrich_app_log,
}


# ---------------------------------------------------------------------------
# Phase 2b: RAG enrichment (Sprint 4)
# ---------------------------------------------------------------------------

async def _rag_enrich(query_text: str, trace_id: str) -> str:
    """
    Sprint 4 — lightweight RAG enrichment (fail-open).

    1. Embeds query_text via vLLM /v1/embeddings (nomic-embed-text-v1.5, 768 dims).
    2. Queries rag_documents table (collection_name='action_experience') for top-1
       cosine-similar past remediation.
    3. Returns formatted context string or empty string on any failure.

    Requires POSTGRES_RAG_DSN env var; skipped silently when absent.
    """
    if not POSTGRES_RAG_DSN:
        return ""

    # Step 1: embed via vLLM OpenAI-compatible /v1/embeddings
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{VLLM_EMBEDDER_URL}/v1/embeddings",
                json={"model": VLLM_EMBED_MODEL, "input": query_text[:2000]},
            )
            resp.raise_for_status()
            data = resp.json().get("data", [])
            emb = data[0].get("embedding", []) if data else []
        if not emb:
            return ""
    except Exception as exc:
        log.debug("[%s] RAG embed failed (skipping): %s", trace_id, exc)
        return ""

    # Step 2: pgvector cosine search
    try:
        import asyncpg
        from pgvector.asyncpg import register_vector

        async def _init_conn(conn: Any) -> None:
            await register_vector(conn)

        conn = await asyncpg.connect(POSTGRES_RAG_DSN, init=_init_conn)
        try:
            rows = await conn.fetch(
                """
                SELECT payload, 1 - (embedding <=> $1::vector) AS score
                FROM rag_documents
                WHERE collection_name = 'action_experience'
                ORDER BY embedding <=> $1::vector
                LIMIT 1
                """,
                emb,
            )
        finally:
            await conn.close()

        if not rows:
            return ""

        score = float(rows[0]["score"])
        if score < 0.70:
            return ""

        raw_payload = rows[0]["payload"]
        payload: dict[str, Any] = (
            json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
        )
        lesson = str(payload.get("lesson") or payload.get("playbook") or "").strip()
        if not lesson:
            return ""

        alertname_hint = str(payload.get("alertname") or "").strip()
        log.info("[%s] RAG hit score=%.3f alertname=%s", trace_id, score, alertname_hint)
        return f"[PAST_EXPERIENCE score={score:.3f}] {lesson[:500]}"

    except Exception as exc:
        log.debug("[%s] RAG query failed (skipping): %s", trace_id, exc)
        return ""


# ---------------------------------------------------------------------------
# Phase 2: Prompt construction
# ---------------------------------------------------------------------------

_SCHEMA_BLOCK = json.dumps(STRICT_REMEDIATION_JSON_SCHEMA, indent=2)

_SYSTEM_BASE = f"""\
You are an SRE automation agent for Kubernetes. Respond with ONLY a JSON object — \
no markdown, no explanation, no code fences.

The JSON must match this exact schema:
{_SCHEMA_BLOCK}

Allowed actions: noop | rollout_restart | patch_deployment_resource | \
patch_configmap_key | apply_rbac_least_privilege.
"""

_LANE_INSTRUCTIONS: dict[Lane, str] = {
    Lane.RESOURCE: (
        "LANE: RESOURCE (CPU/RAM/Network usage alert).\n"
        "A 3-sigma statistical analysis has been run against the rolling metric window. "
        "The lane_context contains the z_score and is_anomaly flag.\n"
        "If is_anomaly is true: choose patch_deployment_resource to adjust resource limits.\n"
        "If is_anomaly is false or gate was skipped: prefer noop with a brief reasoning."
    ),
    Lane.STATE: (
        "LANE: STATE (infrastructure failure — OOMKilled / CrashLoop / NodeDown / missing dependency).\n"
        "Act immediately — no statistical gate applies (physical K8s proof lane).\n"
        "OOMKilled → patch_deployment_resource to increase memory limits by 50%.\n"
        "CrashLoopBackOff → rollout_restart (only if no broken spec evidence in message).\n"
        "NodeDown / NodeNotReady → noop (node recovery is outside workload scope).\n"
        "CreateContainerConfigError / FailedMount / missing dependency resource → patch_configmap_key.\n"
        "  Set target_ref=<resource-name-from-message>, namespace=<from alert>, "
        "configmap_key='placeholder', configmap_value='omni-auto-created'.\n"
        "  The invariant INV_NO_RESTART_ON_BROKEN_SPEC blocks rollout_restart for broken-spec faults; "
        "recreating the missing dependency resource is the correct action."
    ),
    Lane.APP_LOG: (
        "LANE: APP_LOG (application error / log surge on api_web workload).\n"
        "The lane_context contains Loki log-surge evidence (log_surge_ok, log_surge_reason).\n"
        "If log_surge_ok is true: rollout_restart is a reasonable first action.\n"
        "If log_surge_ok is false: noop pending further investigation."
    ),
}


async def phase2_transform(laned: LanedAlert) -> tuple[str, str]:
    """
    Phase 2 — Lane-aware enrichment + RAG enrichment + prompt construction.

    Calls the enricher for the resolved lane, then optionally prepends a
    past_experience context block from the action_experience RAG collection.
    """
    laned.lane_meta = await _ENRICHERS[laned.lane](laned)

    rag_query = f"{laned.alertname} {laned.namespace} {laned.message}".strip()
    rag_context = await _rag_enrich(rag_query, laned.trace_id)

    system_prompt = f"{_SYSTEM_BASE}\n{_LANE_INSTRUCTIONS[laned.lane]}"
    payload: dict[str, Any] = {
        "trace_id": laned.trace_id,
        "alertname": laned.alertname,
        "lane": laned.lane.value,
        "namespace": laned.namespace,
        "pod": laned.pod,
        "container": laned.container,
        "deployment_name": laned.deployment_name,
        "severity": laned.severity,
        "memory_limit": laned.memory_limit,
        "message": laned.message,
        "lane_context": laned.lane_meta,
    }
    if rag_context:
        payload["past_experience"] = rag_context

    return system_prompt, json.dumps(payload, indent=2)


# ---------------------------------------------------------------------------
# Phase 3: Universal Reasoner
# ---------------------------------------------------------------------------

async def phase3_output(system_prompt: str, user_message: str) -> HighLevelRemediationPlan:
    """
    Phase 3 — Universal Reasoner.

    Calls vLLM via the OpenAI-compatible /v1/chat/completions endpoint.
    Parses the response into HighLevelRemediationPlan (handles markdown fences,
    partial JSON).  Raises HTTPException(502) on vLLM failure or schema mismatch.
    """
    vllm_payload = {
        "model": VLLM_MODEL,
        "stream": False,
        "temperature": 0,
        "max_tokens": 4096,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{VLLM_BASE_URL}/v1/chat/completions",
                json=vllm_payload,
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            raw = resp.json()["choices"][0]["message"]["content"]
    except httpx.ConnectError:
        log.error("vLLM unreachable at %s", VLLM_BASE_URL)
        raise HTTPException(502, detail=f"vLLM unreachable at {VLLM_BASE_URL}")
    except httpx.HTTPStatusError as exc:
        log.error("vLLM HTTP error %s: %s", exc.response.status_code, exc.response.text)
        raise HTTPException(502, detail=f"vLLM error: {exc.response.status_code}")

    plan = parse_high_level_plan_json(raw)
    if plan is None:
        log.error("[Reasoner] LLM output did not parse: %s", raw[:400])
        raise HTTPException(502, detail="LLM output did not match HighLevelRemediationPlan schema")

    log.info("[Reasoner] action=%s target=%s ns=%s", plan.action, plan.target_ref, plan.namespace)
    return plan


# ---------------------------------------------------------------------------
# Phase 4: Execute
# ---------------------------------------------------------------------------

def _apply_invariants(
    laned: LanedAlert,
    plan: HighLevelRemediationPlan,
) -> tuple[bool, str | None]:
    """
    Sprint 3 — Evaluate INV_* invariants before any mutation.

    Returns (ok, reason_code_or_none).  When not ok, the caller overrides the
    plan with a noop and records the reason code in lane_meta.

    Invariants evaluated:
      INV_NAMESPACE_ISOLATION      — target namespace must be in allowed list
      INV_NO_RESTART_ON_BROKEN_SPEC — block rollout_restart when evidence shows
                                      missing ConfigMap/Secret (restart won't fix it)
      INV_READ_BEFORE_MUTATE       — require prior readonly evidence for state-lane
                                      hard faults before allowing mutation
    """
    tool_call = map_high_level_plan_to_mutate(plan)
    if tool_call is None:
        return True, None  # noop — nothing to check

    ok, reason_code, inv_meta = evaluate_diagnostic_invariants(
        _WS,
        tool_name=tool_call["tool_name"],
        args=tool_call["args"],
        batch=laned.batch,
        discovery_tool_names=[],
        proof_lane=laned.lane.value,
    )
    if not ok:
        log.warning(
            "[%s] invariant blocked tool=%s reason=%s meta=%s",
            laned.trace_id, tool_call["tool_name"], reason_code, inv_meta,
        )
        laned.lane_meta["invariant_blocked"] = reason_code
        laned.lane_meta["invariant_meta"] = inv_meta
    return ok, reason_code


async def _shadow_writeback(laned: LanedAlert, plan: HighLevelRemediationPlan) -> None:
    """
    Sprint 5 — Write shadow record to Redis after successful execution.

    Key: omni:selflearn:shadow:{trace_id}  TTL: 24 h
    Value: JSON snapshot for human-approval ingest into action_experience.
    Non-blocking: any Redis failure is logged and ignored.
    """
    redis_url = os.getenv("OMNI_REDIS_URL") or os.getenv("REDIS_URL")
    if not redis_url:
        return
    try:
        import redis.asyncio as aioredis

        r = aioredis.from_url(redis_url, decode_responses=True)
        key = f"omni:selflearn:shadow:{laned.trace_id}"
        value = json.dumps({
            "trace_id": laned.trace_id,
            "alertname": laned.alertname,
            "namespace": laned.namespace,
            "lane": laned.lane.value,
            "action": plan.action,
            "target_ref": plan.target_ref,
            "reasoning": plan.reasoning,
        })
        await r.set(key, value, ex=86400)  # 24 h
        await r.aclose()
        log.info("[%s] shadow write-back recorded key=%s TTL=24h", laned.trace_id, key)
    except Exception as exc:
        log.warning("[%s] shadow write-back failed (non-blocking): %s", laned.trace_id, exc)


# ---------------------------------------------------------------------------
# Phase 5: SDK-based health verification (generic — no workload-type hardcoding)
# ---------------------------------------------------------------------------

async def phase5_verify(laned: LanedAlert, plan: HighLevelRemediationPlan) -> tuple[bool, str]:
    """
    Phase 5 — SDK-based health verification.

    Determines the true owner of the alerted Pod via OwnerReference traversal
    (Task 3 — `get_resource_owner` in k8s_cluster_tools), then checks the
    owner's health using the appropriate K8s API.

    Owner resolution order:
      1. `get_resource_owner(pod, namespace)` — Pod→RS→Deployment/StatefulSet (authoritative).
      2. `laned.deployment_name` pod-name heuristic fallback.
      3. `plan.target_ref` as last resort.

    Supported owner kinds:  Deployment, StatefulSet.
    Returns (healthy, human_readable_summary).
    Fails open (healthy=False) when K8s is unreachable so the loop terminates
    at MAX_LOOP_ITERATIONS rather than blocking.
    """
    if plan.action == "noop":
        return True, "noop — no verification needed"

    namespace = laned.namespace or plan.namespace.strip()
    if not namespace:
        return False, "cannot verify — namespace unknown"

    # --- Step 1: OwnerReference resolution (Task 3) ---
    owner_kind: str = "Deployment"
    owner_name: str | None = None
    try:
        result = await get_resource_owner(laned.pod, namespace)
        if result:
            owner_kind, owner_name = result
            log.debug(
                "[%s] phase5_verify: owner resolved %s → %s/%s",
                laned.trace_id, laned.pod, owner_kind, owner_name,
            )
    except Exception as exc:
        log.warning(
            "[%s] phase5_verify: owner resolution failed (%s) — falling back to heuristic",
            laned.trace_id, exc,
        )

    if not owner_name:
        fallback = laned.deployment_name or plan.target_ref.strip()
        if fallback:
            log.warning(
                "[%s] phase5_verify: OwnerRef traversal returned no result — "
                "falling back to heuristic name=%s (may be inaccurate)",
                laned.trace_id, fallback,
            )
        owner_name = fallback

    if not owner_name:
        return False, "cannot verify — owner name could not be resolved"

    # --- Step 2: Kind-aware health check ---
    try:
        await _load_k8s_config()
        apps = k8s_client.AppsV1Api()
        try:
            if owner_kind == "StatefulSet":
                obj = await apps.read_namespaced_stateful_set(owner_name, namespace)
                desired = obj.spec.replicas or 0
                ready = obj.status.ready_replicas or 0
                available = ready  # StatefulSet exposes readyReplicas as the primary health signal
            elif owner_kind == "Deployment":
                obj = await apps.read_namespaced_deployment(owner_name, namespace)
                desired = obj.spec.replicas or 0
                available = obj.status.available_replicas or 0
                ready = obj.status.ready_replicas or 0
            else:
                return False, f"owner kind={owner_kind} not supported for health verification"

            summary = (
                f"{owner_kind} {owner_name}/{namespace}: "
                f"desired={desired} available={available} ready={ready}"
            )
            healthy = desired > 0 and available == desired and ready == desired
            log.info("[%s] phase5_verify healthy=%s %s", laned.trace_id, healthy, summary)
            return healthy, summary
        finally:
            await apps.api_client.close()

    except Exception as exc:
        summary = f"verification error (k8s unreachable?): {str(exc)[:200]}"
        log.warning("[%s] phase5_verify failed: %s", laned.trace_id, exc)
        return False, summary


# Namespaces permitted for autonomous mutation (Wave A1 scope).
_ALLOWED_MUTATE_NAMESPACES: frozenset[str] = frozenset({"multi-agent", "lab-test"})


def _sec_audit_sa_scope(trace_id: str, target_namespace: str) -> None:
    """
    Wave A1 — Runtime SA scope check before any mutation.

    Detects cluster-admin or out-of-scope namespace and emits SEC_AUDIT_CRITICAL.
    Blocks execution when OMNI_ENV_MODE != lab (fail-closed in non-lab environments).

    Raises HTTPException(403) when running outside lab with a privilege violation.
    In lab mode, logs the warning and allows execution to continue so developers
    can observe the gap without a hard block.
    """
    env_mode = os.getenv("OMNI_ENV_MODE", "")
    violations: list[str] = []

    # Check 1: target namespace must be in the Wave A1 allowed set.
    if target_namespace not in _ALLOWED_MUTATE_NAMESPACES:
        violations.append(
            f"namespace '{target_namespace}' not in allowed set {sorted(_ALLOWED_MUTATE_NAMESPACES)}"
        )

    # Check 2: SA must not have wildcard cluster-admin.
    try:
        result = subprocess.run(
            ["kubectl", "auth", "can-i", "*", "*", "--all-namespaces"],
            capture_output=True, text=True, timeout=5,
        )
        if result.stdout.strip() == "yes":
            violations.append("ServiceAccount has cluster-wide wildcard permissions (cluster-admin)")
    except Exception:
        pass  # kubectl unavailable — skip cluster-admin check

    if not violations:
        return

    msg = "SEC_AUDIT_CRITICAL [Wave A1] [%s]: %s. Apply k8s/rbac-executor-least-privilege.yaml."
    joined = "; ".join(violations)
    if env_mode != "lab":
        log.error(msg, trace_id, joined)
        raise HTTPException(
            403,
            detail=f"SEC_AUDIT_CRITICAL: SA scope violation blocks mutation outside lab — {joined}",
        )
    log.warning(msg, trace_id, joined)


# ---------------------------------------------------------------------------
# Library tool dispatch table — maps tool_name → (async_fn, ArgsModel).
# All mutations route through kubernetes-asyncio library calls; no subprocess.
# ---------------------------------------------------------------------------

_TOOL_DISPATCH: dict[str, tuple[Any, type]] = {
    "k8s_rollout_restart": (tool_k8s_rollout_restart, RolloutRestartArgs),
    "k8s_patch_resource": (tool_k8s_patch_resource, PatchResourceArgs),
    "k8s_create_or_patch_configmap": (tool_k8s_create_or_patch_configmap, CreateOrPatchConfigMapArgs),
    "k8s_apply_rbac_least_privilege": (tool_k8s_apply_rbac_least_privilege, ApplyRbacLeastPrivilegeArgs),
}


async def _execute_library_tool(laned: LanedAlert, plan: HighLevelRemediationPlan) -> bool:
    """
    Wave A1 — Execute plan via kubernetes-asyncio library tools (no subprocess).

    Routing: map_high_level_plan_to_mutate → tool_name → _TOOL_DISPATCH.
    Safety guard: only executes when OMNI_ENV_MODE=lab.
    Raises HTTPException(500) on tool error; returns False for noop or skip.
    """
    if os.getenv("OMNI_ENV_MODE") != "lab":
        log.warning("[%s] OMNI_ENV_MODE != lab — skipping execution", laned.trace_id)
        return False

    tool_call = map_high_level_plan_to_mutate(plan)
    if tool_call is None:
        log.info("[%s] plan.action=noop — no mutation", laned.trace_id)
        return False

    # Wave A1: SA scope check before any cluster mutation.
    _sec_audit_sa_scope(laned.trace_id, laned.namespace)

    tool_name = tool_call["tool_name"]
    args_dict = tool_call["args"]

    entry = _TOOL_DISPATCH.get(tool_name)
    if entry is None:
        log.warning("[%s] No library dispatch for tool=%s", laned.trace_id, tool_name)
        return False

    tool_fn, ArgsModel = entry
    try:
        args_obj = ArgsModel(**args_dict)
    except Exception as exc:
        log.error("[%s] args validation failed for tool=%s: %s", laned.trace_id, tool_name, exc)
        raise HTTPException(500, detail=f"tool args invalid: {exc}")

    log.info("[%s] dispatching library tool=%s args=%s", laned.trace_id, tool_name, args_dict)
    result = await tool_fn(ctx=None, args=args_obj)

    if "[DATA] api_error" in result:
        log.error("[%s] tool=%s returned error: %s", laned.trace_id, tool_name, result)
        raise HTTPException(500, detail=f"k8s tool error: {result}")

    log.info("[%s] tool=%s result: %s", laned.trace_id, tool_name, result)
    return True


@app.post("/alert", response_model=ExecutionResponse)
async def phase4_execute(raw: AlertInput) -> ExecutionResponse:
    """
    Phase 4 — Stateful Closed-Loop endpoint (Sprint 7).

    Runs up to MAX_LOOP_ITERATIONS passes of:
      Probe → LLM Reason → Invariant gate → Execute → Phase 5 Verify → Decide.

    RemediationContext is injected into the LLM system prompt on iterations > 1
    so the model can reason from history (what was tried, what the K8s state is)
    rather than repeating the same action blindly.

    Short-circuits:
      Lane RESOURCE: gate_blocked=True → noop (no LLM call, no loop)
      Lane APP_LOG:  loki_unavailable=True → noop (fail-closed, no loop)
    """
    laned = phase1_parse(raw)

    context = RemediationContext(
        trace_id=laned.trace_id,
        alertname=laned.alertname,
        namespace=laned.namespace,
    )

    final_plan = HighLevelRemediationPlan(action="noop", reasoning="no action taken")
    executed = False

    def _noop_response(reason: str, iteration: int = 1, resolution_state: str = "incomplete") -> ExecutionResponse:
        return ExecutionResponse(
            trace_id=laned.trace_id,
            lane=laned.lane.value,
            lane_source=laned.lane_source,
            lane_meta=laned.lane_meta,
            plan=HighLevelRemediationPlan(action="noop", reasoning=reason),
            executed=False,
            iterations=iteration,
            converged=False,
            resolution_state=resolution_state,
        )

    for iteration in range(1, MAX_LOOP_ITERATIONS + 1):
        context.iterations = iteration
        log.info("[%s] === Closed-loop iteration %d/%d ===", laned.trace_id, iteration, MAX_LOOP_ITERATIONS)

        # Record probe observation for this iteration.
        if iteration == 1:
            obs_summary = f"Initial alert: {laned.alertname} in {laned.namespace}/{laned.pod}. {laned.message}".strip()
        else:
            prev_out = next((o for o in context.outcomes if o.iteration == iteration - 1), None)
            obs_summary = (
                f"Re-probe after iteration {iteration - 1}. "
                f"Previous outcome: {prev_out.summary if prev_out else 'unknown'}"
            )
        context.observations.append(ObservationRecord(iteration=iteration, summary=obs_summary))

        # Phase 2: lane enrichment + prompt construction.
        system_prompt, user_message = await phase2_transform(laned)

        # Inject remediation history into system prompt for iterations > 1.
        if iteration > 1:
            ctx_block = context.to_prompt_block()
            system_prompt = f"{system_prompt}\n\n{ctx_block}"

        # Lane RESOURCE: 3-sigma gate not anomalous → short-circuit (no loop benefit).
        if laned.lane is Lane.RESOURCE and laned.lane_meta.get("gate_blocked"):
            z = laned.lane_meta.get("z_score")
            z_str = f"{z:.3f}" if z is not None else "N/A"
            log.info("[%s] 3-sigma gate blocked (z=%s) — noop", laned.trace_id, z_str)
            return _noop_response(
                f"3-sigma gate: z={z_str} does not exceed |3.0| threshold — metric within normal range.",
                iteration,
                resolution_state="sigma_gate",
            )

        # Lane APP_LOG: fail-closed — no LLM call without Loki evidence.
        if laned.lane is Lane.APP_LOG and laned.lane_meta.get("loki_unavailable"):
            log.warning("[%s] %s — fail-closed noop", laned.trace_id, ERR_REA_LOG_SOURCE_UNAVAILABLE)
            return _noop_response(
                f"{ERR_REA_LOG_SOURCE_UNAVAILABLE}: Loki unavailable — no log evidence for mutation.",
                iteration,
                resolution_state="loki_unavailable",
            )

        # Phase 3: LLM reasoning.
        plan = await phase3_output(system_prompt, user_message)
        final_plan = plan

        context.actions_taken.append(ActionRecord(
            iteration=iteration,
            action=plan.action,
            target_ref=plan.target_ref,
            namespace=plan.namespace,
            reasoning=plan.reasoning,
        ))

        # INV_* invariant gate — must pass before any mutation.
        ok, reason_code = _apply_invariants(laned, plan)
        if not ok:
            context.outcomes.append(OutcomeRecord(
                iteration=iteration,
                healthy=False,
                summary=f"Invariant {reason_code} blocked mutation — noop applied.",
            ))
            final_plan = HighLevelRemediationPlan(
                action="noop",
                reasoning=f"Invariant {reason_code} blocked mutation — noop applied for safety.",
            )
            context.converged = False
            context.resolution_state = "blocked"
            break  # invariant block is terminal — no value in retrying

        # LLM chose noop → the model assessed workload is healthy or unactionable.
        # converged=False: we did NOT verify via SDK — the assertion is unconfirmed.
        if plan.action == "noop":
            context.outcomes.append(OutcomeRecord(
                iteration=iteration,
                healthy=False,
                summary="LLM chose noop — workload not independently verified; treated as incomplete.",
            ))
            context.converged = False
            context.resolution_state = "incomplete"
            break

        # Phase 4 (execute) → backoff → Phase 5 (verify).
        executed = await _execute_library_tool(laned, plan)

        if not executed:
            # OMNI_ENV_MODE != lab: execution skipped — cannot verify; terminate loop.
            context.outcomes.append(OutcomeRecord(
                iteration=iteration,
                healthy=False,
                summary="Execution skipped (OMNI_ENV_MODE != lab) — state unchanged.",
            ))
            context.converged = False
            context.resolution_state = "exec_skipped"
            break

        # Task 2 — Execution backoff: allow K8s controllers and Kubelet time to
        # process the mutation before the SDK health check runs.
        # K8s is eventually consistent; immediate verification yields false negatives.
        if VERIFY_BACKOFF_SECONDS > 0:
            log.info(
                "[%s] Backoff %.1fs before phase5_verify (OMNI_VERIFY_BACKOFF_SECONDS)",
                laned.trace_id, VERIFY_BACKOFF_SECONDS,
            )
            await asyncio.sleep(VERIFY_BACKOFF_SECONDS)

        healthy, verify_summary = await phase5_verify(laned, plan)
        context.outcomes.append(OutcomeRecord(
            iteration=iteration,
            healthy=healthy,
            summary=verify_summary,
        ))

        if healthy:
            context.converged = True
            context.resolution_state = "converged"
            await _shadow_writeback(laned, plan)
            log.info("[%s] Converged on iteration %d — %s", laned.trace_id, iteration, verify_summary)
            break

        if iteration == MAX_LOOP_ITERATIONS:
            context.resolution_state = "exhausted"

        log.info(
            "[%s] Iteration %d UNHEALTHY — %s. %s",
            laned.trace_id, iteration, verify_summary,
            "Continuing loop." if iteration < MAX_LOOP_ITERATIONS else "Max iterations reached.",
        )

    return ExecutionResponse(
        trace_id=laned.trace_id,
        lane=laned.lane.value,
        lane_source=laned.lane_source,
        lane_meta=laned.lane_meta,
        plan=final_plan,
        executed=executed,
        iterations=context.iterations,
        converged=context.converged,
        resolution_state=context.resolution_state,
    )
