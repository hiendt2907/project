"""Merge and dequeue RAG / playbook candidates for trace orchestration."""

from __future__ import annotations

from typing import Any

from pkg.trace_orchestrator.state import (
    TraceOrchestratorPhase,
    TraceOrchestratorState,
    save_trace_orchestrator_state,
)


def merge_ranked_candidate_rows(
    rows: list[dict[str, Any]],
    *,
    id_key: str = "id",
    score_key: str = "score",
    source_key: str = "source",
) -> list[str]:
    """Return stable-unique candidate ids, highest score first.

    Each row should include ``id`` (or *id_key*), optional ``score``, optional ``source``.
    Candidate id format: ``{source}:{id}`` when source present, else str(id).
    """
    scored: list[tuple[float, str]] = []
    seen: set[str] = set()
    for row in rows:
        rid = row.get(id_key)
        if rid is None and row.get("payload_id") is not None:
            rid = row.get("payload_id")
        if rid is None:
            continue
        src = str(row.get(source_key) or "rag")
        cid = f"{src}:{rid}"
        if cid in seen:
            continue
        seen.add(cid)
        try:
            sc = float(row.get(score_key) or 0.0)
        except (TypeError, ValueError):
            sc = 0.0
        scored.append((sc, cid))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [c for _, c in scored]


def enqueue_rag_candidate(state: TraceOrchestratorState, candidate_id: str) -> None:
    if candidate_id and candidate_id not in state.rag_candidate_ids:
        state.rag_candidate_ids.append(candidate_id)


def pop_next_untried_candidate(state: TraceOrchestratorState) -> str | None:
    for cid in state.rag_candidate_ids:
        if cid not in state.attempted_rag_ids:
            return cid
    return None


async def record_verify_failure_for_candidate(
    redis: Any,
    state: TraceOrchestratorState,
    candidate_id: str,
    *,
    detail: str = "",
    ttl_sec: int = 86_400,
) -> None:
    if candidate_id and candidate_id not in state.attempted_rag_ids:
        state.attempted_rag_ids.append(candidate_id)
    state.last_verify_ok = False
    if detail:
        state.last_error = detail[:2000]
    return await save_trace_orchestrator_state(redis, state, ttl_sec=ttl_sec)


async def record_verify_success_for_candidate(
    redis: Any,
    state: TraceOrchestratorState,
    candidate_id: str,
    *,
    ttl_sec: int = 86_400,
) -> None:
    if candidate_id and candidate_id not in state.attempted_rag_ids:
        state.attempted_rag_ids.append(candidate_id)
    state.last_verify_ok = True
    state.last_error = ""
    state.phase = TraceOrchestratorPhase.RESOLVED
    return await save_trace_orchestrator_state(redis, state, ttl_sec=ttl_sec)
