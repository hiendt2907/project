# Omni playbook / memory index (pointers only)

**Canonical (kiến trúc + vận hành — đọc trước):** [vendor/OMNI_PROJECT_CANONICAL.md](vendor/OMNI_PROJECT_CANONICAL.md) · **Chỉ mục doc:** [DOCUMENTATION_INDEX.md](DOCUMENTATION_INDEX.md)

This document is an **index**: it points to where knowledge lives. It does **not** duplicate SOP text or long runbooks.

**Bookmark cũ:** [vendor/golden_path_split.md](vendor/golden_path_split.md) → redirect tới canonical.

## Corpus contract (where to write what)

| Kind of update | Document |
|----------------|----------|
| **Single source — kiến trúc + Kafka + RAG + verify (bám code)** | [OMNI_PROJECT_CANONICAL.md](vendor/OMNI_PROJECT_CANONICAL.md) |
| Invariants, guardrails, failure patterns after a **behavior change** | [project-memory.md](../reports/project-memory.md) |
| Diagnostic policy (INV_*), `reasoning_chain`, ReAct/blind lane | [diagnostic-policy-spec.md](../reports/diagnostic-policy-spec.md) + [project-memory.md](../reports/project-memory.md) |
| **Symptom → fix** from a real incident or debug session | [knownbase.md](knownbase.md) |
| MPV3 / architecture review | [master_plan_v3_review_report.md](master_plan_v3_review_report.md) |
| New long-lived technical notes | Prefer `docs/vendor/`; phase reports in `docs/reports/` |

## Retrieval surfaces (code)


| Surface           | Settings / code                                                                                                                                            | pgvector / store key |
| ----------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------- |
| Expert RAG (gate) | `pgvector_collection_k8s_expert` → default collection name `k8s_expert` ([`COLLECTION_K8S_EXPERT`](../src/rag/pgvector_store.py)) | Collection `k8s_expert` in `rag_documents` |
| SOP RAG           | `proactive_sop_collection` ([`settings.py`](../src/workers/settings.py)); `resolve_remediation_from_memory` in [`handlers.py`](../src/workers/handlers.py) | `itops_sop_ledger` / `itops_sop_ledger_v2` (`COLLECTION_SOP` / `COLLECTION_SOP_V2`) |
| Action experience | Feedback loop upsert + `_resolve_from_action_experience` in [`proactive_observer.py`](../src/workers/proactive_observer.py) | `action_experience` (`COLLECTION_ACTION_EXPERIENCE`); also written from [`autonomous_feedback_loop.py`](../src/workers/autonomous_feedback_loop.py) on successful mutate |
| Error / vendor / topology | Optional collections | `itops_error_ledger`, `vendor_knowledge`, `infra_topology` — see [`pgvector_store.py`](../src/rag/pgvector_store.py) `COLLECTION_*` |
| ReAct scratchpad  | Redis `omni:proactive:react_mem:{trace_id}`                                                                                                                | Short per-iteration observations for the current incident only |
| Negative patterns | Redis `omni:learning:negative:proactive:*`                                                                                                                       | Avoid repeating failed playbooks                               |

**Self-learning (shadow):** Redis `omni:selflearn:shadow:*` — **not** auto-ingested into pgvector; export via chaos-rag runbooks. See [OMNI_PROJECT_CANONICAL.md](vendor/OMNI_PROJECT_CANONICAL.md) (shadow / learning policy).


## Context priority (for LLM calls)

When building proactive fallback prompts, prefer this order (highest signal first):

1. Rule / incident header: `rule_name`, `metric_value`, `threshold`, `canonical_query`, `trigger_promql`, `error_hint`, `baseline_promql`
2. Phase and allowed tools for the current phase
3. **Tail** of `react_memory` (recent iterations only), truncated by `proactive_react_memory_max_chars` / related settings

Full SOP bodies are **not** inlined in the system prompt; they are retrieved via similarity search when the SOP path runs.

## Rule / symptom → where to look


| Trigger                  | Typical `rule_name` / hint                                  | First retrieval                                                                                         |
| ------------------------ | ----------------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| Default proactive tick   | `PrometheusProactiveThreshold` (see `DEFAULT_RULE` in code) | `proactive_sop_collection` + `canonical_query` from `[normalize.py](../src/observability/normalize.py)` |
| Crash loop style metrics | `error_hint` from `infer_error_hint_from_promql`            | Same + action experience by `canonical_query` / pattern key                                             |


Extend this table as new `rule_name` values or taxonomies appear — keep rows **short** (pointers only).

## Related docs

- `[vendor/OMNI_PROJECT_CANONICAL.md](vendor/OMNI_PROJECT_CANONICAL.md)` — **canonical** kiến trúc + Kafka + RAG + verify
- `[docs/proactive_state_machine.md](proactive_state_machine.md)` — phases, audit, metrics
- `[docs/reports/chaos-rag-selflearn-runbook.md](reports/chaos-rag-selflearn-runbook.md)` — chaos/matrix + shadow self-learning lab, Registry, Learning Delta, Redis export (no auto-ingest)

