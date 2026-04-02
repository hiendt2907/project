# Omni playbook / memory index (pointers only)

This document is an **index**: it points to where knowledge lives. It does **not** duplicate SOP text or long runbooks.

## Retrieval surfaces (code)

| Surface | Settings / code | What it stores |
|---------|-----------------|----------------|
| SOP RAG | `proactive_sop_collection` ([`settings.py`](../src/workers/settings.py)); `resolve_remediation_from_memory` in [`handlers.py`](../src/workers/handlers.py) | pgvector collection for IT ops SOP snippets |
| Action experience | `action_experience_*`; `_resolve_from_action_experience` in [`proactive_observer.py`](../src/workers/proactive_observer.py) | Learned tool patterns linked to incidents |
| ReAct scratchpad | Redis `omni:proactive:react_mem:{trace_id}` | Short per-iteration observations for the current incident only |
| Negative patterns | `omni:learning:negative:proactive:*` | Avoid repeating failed playbooks |

## Context priority (for LLM calls)

When building proactive fallback prompts, prefer this order (highest signal first):

1. Rule / incident header: `rule_name`, `metric_value`, `threshold`, `canonical_query`, `trigger_promql`, `error_hint`, `baseline_promql`
2. Phase and allowed tools for the current phase
3. **Tail** of `react_memory` (recent iterations only), truncated by `proactive_react_memory_max_chars` / related settings

Full SOP bodies are **not** inlined in the system prompt; they are retrieved via similarity search when the SOP path runs.

## Rule / symptom → where to look

| Trigger | Typical `rule_name` / hint | First retrieval |
|---------|----------------------------|-----------------|
| Default proactive tick | `PrometheusProactiveThreshold` (see `DEFAULT_RULE` in code) | `proactive_sop_collection` + `canonical_query` from [`normalize.py`](../src/observability/normalize.py) |
| Crash loop style metrics | `error_hint` from `infer_error_hint_from_promql` | Same + action experience by `canonical_query` / pattern key |

Extend this table as new `rule_name` values or taxonomies appear — keep rows **short** (pointers only).

## Related docs

- [`docs/proactive_state_machine.md`](proactive_state_machine.md) — phases, audit, metrics
