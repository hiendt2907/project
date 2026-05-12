# Trace orchestrator (RAG trials → LLM → verify)

## Purpose

Per-`trace_id` state tracks **which RAG / playbook candidates** have been tried and whether post-action verification succeeded. This supports the product flow: try memory-backed remediation serially before escalating to a full LLM tool loop, then record verified outcomes for learning.

## Redis

- Key: `omni:trace_orchestrator:{trace_id}`
- Value: JSON from `TraceOrchestratorState` (`src/pkg/trace_orchestrator/state.py`)
- Default TTL: 24h (configurable in `save_trace_orchestrator_state`)

## Phases (`TraceOrchestratorPhase`)

- `collect` — evidence gathering (informational; analyst/prober-owned)
- `rag_trials` — dequeue RAG / playbook candidates
- `llm_tools` — structured LLM + executor loop
- `verify` — post-mutate / SDK checks
- `resolved` / `escalated` — terminal

## Code entry points

- **Init + playbook enqueue**: [`src/workers/evidence_consumer.py`](../../src/workers/evidence_consumer.py) after a diagnostic batch flushes and optional `PlaybookMatcher` hit.
- **Helpers**: [`src/pkg/trace_orchestrator/candidates.py`](../../src/pkg/trace_orchestrator/candidates.py) — `merge_ranked_candidate_rows`, `pop_next_untried_candidate`, `record_verify_failure_for_candidate`, `record_verify_success_for_candidate`.
- **Learning hook stub**: [`src/pkg/trace_orchestrator/learning.py`](../../src/pkg/trace_orchestrator/learning.py) — extend to call archivist / `action_experience` upserts with existing governance (`OMNI_EXPERIENCE_REQUIRES_SDK_VERIFY`, CRAT ordering).

## Invariants

- Mutations only via **executor**; analyst remains read-only.
- **CRAT** `write_audit_block()` before Telegram/action per `CLAUDE.md`.
- Do not change `kafka_evidence_loop` `auto_offset_reset`.

## Next steps (incremental)

1. On verify failure after a RAG-sourced action, call `record_verify_failure_for_candidate` and retry the next candidate.
2. Merge vectors from `evaluate_rag_gate` + `recall_playbook_advisory` into `rag_candidate_ids` before planner runs.
3. On verified resolve, implement `on_verified_resolve_hook` body using existing proactive/archivist writers.
