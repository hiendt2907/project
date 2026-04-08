# Technical Analysis: End-to-End Alert Flow & Autonomous Reasoning

> **Legacy / snapshot:** Default lab today is **MPV3 split** (prober / analyst / core / executor). This narrative may describe monolith or Redis stream paths; verify against [docs/vendor/OMNI_PROJECT_CANONICAL.md](docs/vendor/OMNI_PROJECT_CANONICAL.md).

This document outlines the precise journey of a metric alert through the Omni system, from ingestion to autonomous remediation and self-learning.

## 1. Phase 1: Ingestion & Sentinel (The Gateway)

When Prometheus/Alertmanager detects a firing alert:

1.  **Transport**: It sends a POST request with a JSON alert body to the `omni-gateway`.
2.  **Sentinel Checks**:
    *   **Rate Limiting**: Uses a **Token Bucket** (1000 TPS by default) to prevent floods.
    *   **Circuit Breaker**: Queries Redis for `omni:circuit_breaker:active`. If the worker queue is saturated, it drops the alert and returns `503`.
3.  **Persistence**:
    *   Generates a unique `trace_id` (e.g., `gw-prom-5f3a...`).
    *   Performs an `XADD` to the **Redis Cluster** stream `events:inbound`.
    *   **Ack**: Returns `200 OK` with the `trace_id` to Prometheus.

## 2. Phase 2: Orchestration (The Worker)

The `omni-worker` consumes the stream using `XREADGROUP`:

1.  **Trace Initialization**: Sets the thread-local `current_trace_id` for logging uniformity across systems.
2.  **FastPath Check (RAG Retrieval)**:
    *   Embeds the alert summary using `nomic-embed-text`.
    *   Queries **PGVector** (`doc_sop` and `action_experience`).
    *   **Decision**: If a match is found with a score > `rag_fast_path_score` (e.g., 0.85), it executes the remediation tool **immediately** without calling the LLM for reasoning.

## 3. Phase 3: Reasoning (The SlowPath ReAct Loop)

If no "Experience" is found, the **SlowPath** is activated:

1.  **Semaphore Lock**: Acquires a slot from `RedisOllamaSemaphore`. This ensures only N LLM requests run concurrently (Dual-lane: Alert-priority or Proactive-priority).
2.  **Reasoning (The Planner)**:
    *   Sends the alert context to a **Reasoning Model** (e.g., DeepSeek-r1).
    *   **Output**: A step-by-step **Plan** (no tools yet).
3.  **Execution (The Executor)**:
    *   The **Heavy Lifter** (e.g., gemma3:27b) receives the Plan + Alert + Cluster Topology (from Deep Scout).
    *   Calls a **Tool** (e.g., `inspect_pod_deep` or `redis_expert_check`).
4.  **Action/Observation**:
    *   The tool executes on the cluster.
    *   Resulting logs/metrics are **sanitized** (PII removed).
    *   Feed back to the LLM. Repeat until "Diagnosis" is complete or "Action" (Rollout) is triggered.

## 4. Phase 4: Self-Learning (Cognitive Feedback)

The system "learns" from every successful or failed attempt:

*   **Learning from Success**:
    *   When an LLM sequence leads to a fix (Status=OK), `record_routing_from_success` generates an embedding of the *original problem*.
    *   Saves the *winning tool/args* into `action_experience`.
    *   **Result**: The next encounter with this problem follows the **FastPath** (Zero-Reasoning).
*   **Unique Counting & Metrics**:
    *   Uses **Prometheus Counters** (`inc_messages_processed`) to track global throughput.
    *   Tracks `inc_slow_path_exhausted` with an **Error Signature**. The signature is a hash of the error type + component, allowing SREs to see "Unique Failures" vs. "Repeat Failures".
*   **Knowledge Updates**:
    *   `deep_scout_autonomous` periodically updates the **Infrastructure Topology** in the Vector DB, ensuring the RAG layer is aware of new pods/services without manual input.

## 5. Output & Remediation Summary

1.  **User Notification**: Final result is formatted and sent to **Telegram** (if `chat_id` was provided).
2.  **Audit Trail**: The entire trace is stored in the `rag_documents` table under `itops_error_ledger` for post-mortem analysis.
3.  **Autonomous Handover**: If a remediation is critical (e.g., `rollout restart`), it follows a **Hybrid Policy**:
    *   If `OMNI_GOD_MODE=true`: It executes.
    *   Otherwise: It sends a `[CONFIRM_REQUIRED]` message to Telegram for a human "OK".
