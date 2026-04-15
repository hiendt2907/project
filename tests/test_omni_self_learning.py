"""
tests/test_omni_self_learning.py

E2E Self-Learning Validation — stateful three-phase loop:

  Phase 1 (First Encounter / Learning):
    Mock a missing-ConfigMap fault.  Send two consecutive successful action-feedback
    events (Iter-1: k8s_create_or_patch_configmap, Iter-2: k8s_rollout_restart).
    After the second VERIFIED_SUCCESS, _upsert_action_experience_on_success must
    have been called with the successful sequence → vector_store has ≥2 experience
    points in COLLECTION_ACTION_EXPERIENCE.

  Phase 2 (Persistence):
    omni:autonomous:hot:{trace} exists in Redis with closed=True, tied to the
    workload fingerprint extracted from the evidence context.

  Phase 3 (Second Encounter / Knowledge):
    fetch_action_experience_context() is called with the identical symptom text.
    It must return a non-empty "[CONTEXT: action_experience]" block, proving the
    Analyst can resolve the fault in ONE iteration using the stored lesson.

No real Postgres, Kafka, Ollama, or K8s connection is required.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Inline vector-store stub (avoids asyncpg / pgvector deps in unit test)
# ---------------------------------------------------------------------------

from rag.pgvector_store import (
    COLLECTION_ACTION_EXPERIENCE,
    EMBED_DIM,
    PointStruct,
    QueryResponse,
)


class _InMemVectorStore:
    """Minimal in-memory stand-in for PGVectorStore.  Records all upserts."""

    def __init__(self) -> None:
        self._store: dict[str, list[PointStruct]] = {}
        self.upsert_calls: list[tuple[str, list[PointStruct]]] = []

    async def upsert(self, *, collection_name: str, points: list[PointStruct]) -> None:
        coll = self._store.setdefault(collection_name, [])
        existing_ids = {p.id for p in coll}
        for pt in points:
            if pt.id in existing_ids:
                self._store[collection_name] = [
                    pt if p.id == pt.id else p for p in coll
                ]
            else:
                coll.append(pt)
        self.upsert_calls.append((collection_name, list(points)))

    async def query_points(
        self,
        *,
        collection_name: str,
        query: list[float],
        limit: int,
        score_threshold: float = 0.0,
        with_payload: bool = True,
    ) -> QueryResponse:
        pts = self._store.get(collection_name, [])
        results: list[PointStruct] = []
        for p in pts:
            if len(results) >= limit:
                break
            results.append(PointStruct(id=p.id, vector=p.vector, payload=p.payload, score=1.0))
        return QueryResponse(points=results)

    def collection_points(self, name: str) -> list[PointStruct]:
        return list(self._store.get(name, []))


# ---------------------------------------------------------------------------
# Fake semaphore — acquire/release no-ops backed by FakeRedis tokens
# ---------------------------------------------------------------------------

class _DirectSemaphore:
    """Immediate token semaphore — no Redis pool needed for tests."""

    async def acquire(self, timeout_s: float = 120.0) -> str:  # noqa: ARG002
        return "t0"

    async def release(self, token: str) -> None:  # noqa: ARG002
        pass

    async def acquire_reactive(self, timeout_s: float = 120.0) -> str:
        return "t0"

    async def acquire_proactive(self, timeout_s: float = 120.0) -> str:
        return "t0"


# ---------------------------------------------------------------------------
# Fixed embedding helper
# ---------------------------------------------------------------------------

_FIXED_VEC = [0.42] * EMBED_DIM  # stable unit-like vector for all embeds


def _make_llm_mock() -> AsyncMock:
    llm = AsyncMock()
    llm.embed = AsyncMock(return_value={"embeddings": [_FIXED_VEC]})
    llm.chat = AsyncMock(return_value={"message": {"content": "{}"}})
    return llm


# ---------------------------------------------------------------------------
# Context factory
# ---------------------------------------------------------------------------

def _make_ctx(redis_client: Any, vector_store: _InMemVectorStore) -> Any:
    ws = SimpleNamespace(
        # learning flags
        action_experience_enabled=True,
        routing_experience_enabled=True,
        # embedding
        embed_model="nomic-ai/nomic-embed-text-v1.5",
        embed_model_fallback=None,
        # hot-cache TTL
        rag_hot_cache_ttl_sec=3600,
        # proof gate / mutate attempts
        autonomous_execute_max_attempts=3,
        autonomous_verify_max_rounds=3,
        omni_post_mutate_sdk_verify_enabled=False,
        omni_post_mutate_verify_planner_enabled=False,
        # Legacy finalize path in this harness — allow RAG upsert without SDK verify (lab self-learning).
        omni_experience_requires_sdk_verify=False,
        # RAG retrieval threshold (0 so all stored points are returned)
        action_experience_score_threshold=0.0,
        # memory canonicalization
        memory_canonical_strip_pods=True,
        routing_experience_max_chars=2000,
        lesson_max_chars=400,
        # kafka (not actually sent — emit_transition is try-except safe)
        kafka_topic_audit_agent="omni-audit",
        kafka_topic_action_feedback="omni-action-feedback",
        # telegram
        telegram_admin_chat_id=None,
        # model helpers
        diag_evidence_llm_model="qwen2.5-coder-3b",
        model_helper="qwen2.5-coder-3b",
    )

    kafka_mock = MagicMock()
    kafka_mock.send_dict = AsyncMock()

    from workers.handler_context import WorkerHandlerContext

    return WorkerHandlerContext(
        settings=ws,
        redis=redis_client,
        llm=_make_llm_mock(),
        vector_store=vector_store,
        ledger=MagicMock(),
        semaphore=_DirectSemaphore(),
        telegram=None,
        kafka=kafka_mock,
    )


# ---------------------------------------------------------------------------
# Helpers for building feedback envelopes
# ---------------------------------------------------------------------------

def _feedback_envelope(
    trace: str,
    tool_name: str,
    mutate_args: dict[str, Any],
    exit_code: int = 0,
    stdout: str = "ok",
) -> dict[str, str]:
    body = {
        "trace_id": trace,
        "tool_name": tool_name,
        "mutate_args": mutate_args,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": "",
        "skipped_reason": "",
    }
    return {"trace_id": trace, "data": json.dumps(body)}


# ===========================================================================
# Phase 1 + 2 + 3 wrapped in one async test
# ===========================================================================

TRACE = "test-selflearn-001"
NAMESPACE = "multi-agent"
CM_NAME = "nginx-test-never-created-cm"
DEPLOY = "nginx-test"

# Context stored by evidence_consumer before feedback arrives
_CTX_OBJ = {
    "sanitized_text": (
        f'configmap "{CM_NAME}" not found namespace={NAMESPACE} '
        f"deployment={DEPLOY} FailedMount CreateContainerConfigError"
    ),
    "alertname": "NginxTestContainerWaitingFaultLab",
    "drift_type": "FailedMount",
    "omni_verify_required": False,  # skip SDK verify in this test
    "verify_probe_ids": [],          # ensures legacy fast-path
}


@pytest.mark.asyncio
async def test_self_learning_three_phases():
    """
    Full stateful self-learning loop — learning → persistence → recall in 3 phases.
    """
    import fakeredis.aioredis

    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    vector_store = _InMemVectorStore()

    # Seed context for the trace (simulates what evidence_consumer writes)
    await redis_client.set(
        f"omni:autonomous:ctx:{TRACE}",
        json.dumps(_CTX_OBJ, ensure_ascii=False),
    )

    ctx = _make_ctx(redis_client, vector_store)

    # -----------------------------------------------------------------------
    # PHASE 1 — First Encounter (Learning)
    # -----------------------------------------------------------------------

    from workers.autonomous_feedback_loop import handle_action_feedback_envelope

    # Iteration 1: create the missing ConfigMap
    iter1 = _feedback_envelope(
        TRACE,
        tool_name="k8s_create_or_patch_configmap",
        mutate_args={
            "namespace": NAMESPACE,
            "name": CM_NAME,
            "key": "placeholder",
            "value": "created-by-omni",
        },
        stdout="configmap created ok",
    )
    await handle_action_feedback_envelope(ctx, iter1)

    # After iteration 1 the trace is already closed (VERIFIED_SUCCESS).
    # For the scenario to include a second tool we use a fresh trace for iter 2.
    TRACE2 = "test-selflearn-001-iter2"
    await redis_client.set(
        f"omni:autonomous:ctx:{TRACE2}",
        json.dumps({**_CTX_OBJ, "sanitized_text": _CTX_OBJ["sanitized_text"] + " rolling-restart"}, ensure_ascii=False),
    )
    ctx2 = _make_ctx(redis_client, vector_store)

    # Iteration 2: rollout restart to pick up the new ConfigMap
    iter2 = _feedback_envelope(
        TRACE2,
        tool_name="k8s_rollout_restart",
        mutate_args={"namespace": NAMESPACE, "deployment": DEPLOY},
        stdout="restarted deployment ok",
    )
    await handle_action_feedback_envelope(ctx2, iter2)

    # Both iterations must have triggered upsert to vector_store
    upserted_collections = [c for c, _ in vector_store.upsert_calls]
    assert upserted_collections.count(COLLECTION_ACTION_EXPERIENCE) >= 2, (
        "Expected ≥2 upserts to action_experience (one per successful feedback iteration); "
        f"got upsert_calls={vector_store.upsert_calls!r}"
    )

    # -----------------------------------------------------------------------
    # PHASE 2 — Persistence
    # -----------------------------------------------------------------------

    # hot cache must exist for the first trace
    hot_raw = await redis_client.get(f"omni:autonomous:hot:{TRACE}")
    assert hot_raw is not None, "Hot-cache key must exist after VERIFIED_SUCCESS"
    hot = json.loads(hot_raw)
    assert hot.get("closed") is True, f"Expected closed=True, got {hot!r}"
    assert hot.get("trace_id") == TRACE

    # At least one stored point must reference the correct tool
    experience_points = vector_store.collection_points(COLLECTION_ACTION_EXPERIENCE)
    assert len(experience_points) >= 2, (
        f"vector_store must have ≥2 experience points, got {len(experience_points)}"
    )
    tools_stored = [p.payload.get("tool") for p in experience_points]
    assert "k8s_create_or_patch_configmap" in tools_stored, (
        f"ConfigMap creation tool must be in stored experience; tools_stored={tools_stored!r}"
    )
    assert "k8s_rollout_restart" in tools_stored, (
        f"Rollout restart tool must be in stored experience; tools_stored={tools_stored!r}"
    )

    # Workload fingerprint must be populated
    for pt in experience_points:
        assert pt.payload.get("workload_fingerprint") is not None, (
            f"workload_fingerprint missing from payload {pt.payload!r}"
        )

    # -----------------------------------------------------------------------
    # PHASE 3 — Second Encounter (Knowledge / Recall)
    # -----------------------------------------------------------------------

    from execution.experience import fetch_action_experience_context

    # Identical symptom text triggers the same embed → same fixed vector → score=1.0
    symptom_text = _CTX_OBJ["sanitized_text"]

    # Use a fresh ctx sharing the same vector_store (simulates second Analyst loop)
    ctx_recall = _make_ctx(redis_client, vector_store)

    result = await fetch_action_experience_context(ctx_recall, symptom_text)

    assert result, (
        "fetch_action_experience_context must return non-empty context on second encounter"
    )
    assert "[CONTEXT: action_experience]" in result, (
        f"Result must contain action_experience context block; got:\n{result!r}"
    )

    # The returned context must surface a lesson so the Analyst can act in one step
    assert "k8s_create_or_patch_configmap" in result or "k8s_rollout_restart" in result, (
        f"Recalled context must mention at least one stored tool; got:\n{result!r}"
    )


# ===========================================================================
# Additional isolated contract tests
# ===========================================================================

@pytest.mark.asyncio
async def test_hot_cache_written_once_per_trace():
    """Each successful feedback must write exactly one hot-cache entry."""
    import fakeredis.aioredis

    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    vector_store = _InMemVectorStore()
    trace = "hot-cache-single-001"

    await redis_client.set(
        f"omni:autonomous:ctx:{trace}",
        json.dumps(_CTX_OBJ, ensure_ascii=False),
    )
    ctx = _make_ctx(redis_client, vector_store)

    from workers.autonomous_feedback_loop import handle_action_feedback_envelope

    env = _feedback_envelope(trace, "k8s_rollout_restart", {"namespace": NAMESPACE, "deployment": DEPLOY})
    await handle_action_feedback_envelope(ctx, env)

    hot_raw = await redis_client.get(f"omni:autonomous:hot:{trace}")
    assert hot_raw is not None
    hot = json.loads(hot_raw)
    assert hot["closed"] is True
    assert hot["trace_id"] == trace


@pytest.mark.asyncio
async def test_state_key_deleted_on_success():
    """After VERIFIED_SUCCESS the state key must be cleared (prevents ghost retries)."""
    import fakeredis.aioredis

    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    trace = "state-clear-001"
    state_key = f"omni:autonomous:state:{trace}"

    # Pre-seed a non-zero attempt count
    await redis_client.set(state_key, json.dumps({"last_attempt_count": 1, "feedback_failures": 0, "sdk_verify_round": 0}))
    await redis_client.set(f"omni:autonomous:ctx:{trace}", json.dumps(_CTX_OBJ))

    vector_store = _InMemVectorStore()
    ctx = _make_ctx(redis_client, vector_store)

    from workers.autonomous_feedback_loop import handle_action_feedback_envelope

    env = _feedback_envelope(trace, "k8s_rollout_restart", {"namespace": NAMESPACE, "deployment": DEPLOY})
    await handle_action_feedback_envelope(ctx, env)

    assert await redis_client.get(state_key) is None, (
        "state key must be deleted after VERIFIED_SUCCESS to prevent ghost retries"
    )


@pytest.mark.asyncio
async def test_recall_suppressed_when_disabled():
    """action_experience_enabled=False must suppress RETRIEVAL (fetch returns empty).

    Note: _upsert_action_experience_on_success in autonomous_feedback_loop always writes
    (no flag check there — feedback upsert is unconditional on success).  The flag only
    gates fetch_action_experience_context in execution/experience.py.
    """
    import fakeredis.aioredis

    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    vector_store = _InMemVectorStore()
    trace = "disabled-recall-001"

    await redis_client.set(f"omni:autonomous:ctx:{trace}", json.dumps(_CTX_OBJ))

    # First write an experience (enabled)
    ctx_write = _make_ctx(redis_client, vector_store)
    from workers.autonomous_feedback_loop import handle_action_feedback_envelope
    env = _feedback_envelope(trace, "k8s_rollout_restart", {"namespace": NAMESPACE, "deployment": DEPLOY})
    await handle_action_feedback_envelope(ctx_write, env)

    assert vector_store.upsert_calls, "Upsert must have happened (enabled path)"

    # Now fetch with flag disabled — must return empty
    ctx_recall = _make_ctx(redis_client, vector_store)
    ctx_recall.settings.action_experience_enabled = False

    from execution.experience import fetch_action_experience_context
    result = await fetch_action_experience_context(ctx_recall, _CTX_OBJ["sanitized_text"])
    assert result == "", (
        f"fetch_action_experience_context must return '' when action_experience_enabled=False; got {result!r}"
    )
    # hot cache is still written independently
    hot_raw = await redis_client.get(f"omni:autonomous:hot:{trace}")
    assert hot_raw is not None


@pytest.mark.asyncio
async def test_recall_returns_empty_on_cold_store():
    """Empty vector store must return empty string (no crash, graceful miss)."""
    import fakeredis.aioredis

    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    vector_store = _InMemVectorStore()  # empty
    ctx = _make_ctx(redis_client, vector_store)

    from execution.experience import fetch_action_experience_context

    result = await fetch_action_experience_context(ctx, "missing configmap nginx-config namespace=lab")
    assert result == "", f"Expected empty string on cold store, got {result!r}"
