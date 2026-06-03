"""Trace-scoped orchestration: RAG trial queue, phases, verify outcomes.

Redis key: ``omni:trace_orchestrator:{trace_id}`` (JSON blob, TTL managed by saver).

This module does not replace analyst/executor; it records per-trace progression so
multiple RAG candidates can be tried before falling through to full LLM tool loops.
"""

from __future__ import annotations

from pkg.trace_orchestrator.candidates import (
    enqueue_rag_candidate,
    merge_ranked_candidate_rows,
    pop_next_untried_candidate,
    record_verify_failure_for_candidate,
    record_verify_success_for_candidate,
)
from pkg.trace_orchestrator.learning import on_verified_resolve_hook
from pkg.trace_orchestrator.state import (
    REDIS_KEY_TRACE_ORCHESTRATOR_PREFIX,
    TraceOrchestratorPhase,
    TraceOrchestratorState,
    load_trace_orchestrator_state,
    mark_trace_orchestrator_resolved_verified,
    redis_key_trace_orchestrator,
    save_trace_orchestrator_state,
)

__all__ = [
    "REDIS_KEY_TRACE_ORCHESTRATOR_PREFIX",
    "TraceOrchestratorPhase",
    "TraceOrchestratorState",
    "enqueue_rag_candidate",
    "load_trace_orchestrator_state",
    "mark_trace_orchestrator_resolved_verified",
    "merge_ranked_candidate_rows",
    "on_verified_resolve_hook",
    "pop_next_untried_candidate",
    "record_verify_failure_for_candidate",
    "record_verify_success_for_candidate",
    "redis_key_trace_orchestrator",
    "save_trace_orchestrator_state",
]
