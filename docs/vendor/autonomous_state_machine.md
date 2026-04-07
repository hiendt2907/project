# Autonomous incident state machine (1 page)

Contract: **GIGO** inputs → **RAG** (optional truth) → **LLM SDK-only** on RAG miss → **MACHINE_JSON** (executor) + **HUMAN_SUMMARY** (≤30 words) → **feedback** → **Telegram red escalation** if exhausted.

```mermaid
flowchart TD
  incident[Incident_alert_or_event]
  gigo[GIGO_sanitize_and_probes_SDK_JSON]
  contrast{alert_vs_SDK_state_machine}
  rag{RAG_hit}
  llm_ok[LLM_with_RAG_context]
  llm_miss[LLM_zero_knowledge_SDK_only]
  twoch[Split_MACHINE_JSON_and_HUMAN_SUMMARY]
  contra{Contradicts_SDK}
  esc1[emit_telegram_escalation]
  suggest[omni_actions_SUGGEST]
  mutate[omni_actions_EXECUTE_MUTATE]
  fb[omni_action_feedback]
  replan{Attempts_left}
  esc2[emit_telegram_escalation_max_rounds]

  incident --> gigo
  gigo --> contrast
  contrast -->|inconsistent_clear| suggest
  contrast -->|else| rag
  rag -->|yes| llm_ok
  rag -->|no| llm_miss
  llm_ok --> suggest
  llm_miss --> twoch
  twoch --> contra
  contra -->|yes| esc1
  contra -->|no| verdict{verdict_ESCALATE}
  verdict -->|yes| esc1
  verdict -->|no| suggest
  suggest --> mutate
  mutate --> fb
  fb --> replan
  replan -->|ok| done[Closed_or_hot_cache]
  replan -->|fail| esc2
  esc1 --> end[End]
  esc2 --> end
  done --> end
```

**Notes**

- **Human channel** is capped by `omni_concise_reply_max_words` (default 30). **Machine channel** JSON line is capped (~600 chars ≈ 150 tokens budget).
- **Single red button**: `telegram_escalation.emit_telegram_escalation` — replaces legacy Redis manual approval for `request_approval`.
- **Truth Law**: `rag_truth_law_enforced` no longer stops the flow on RAG miss; SDK-only LLM runs instead of `KNOWLEDGE_UNCERTAIN` token-only return.
