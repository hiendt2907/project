# Full Code Review — 2026-05-19
**Branch:** main (`6cda27e` + uncommitted)  
**Scope:** 38 source files (src/ only)  
**Method:** 4 parallel specialist agents — Correctness, Security, Python/Async, Performance

---

## Executive Summary

| Domain | CRITICAL | HIGH | MEDIUM | LOW |
|--------|----------|------|--------|-----|
| Security | 3 | 5 | 4 | 2 |
| Correctness | 2 | 4 | 4 | 2 |
| Python/Async | 2 | 8 | 5 | 1 |
| Performance | 3 | 8 | 4 | 4 |
| **Total (deduplicated)** | **8** | **19** | **13** | **7** |

**Verdict: BLOCK.** 8 CRITICAL findings, several of which violate core project invariants (CRAT fail-closed, executor-only mutations, master kill-switch, ingest runtime crashes).

---

## CRITICAL — Must Fix Before Merge

### SEC-C1 — API Key Bypass: All Routes Unauthenticated When Env Var Unset
**File:** `src/gateway/api.py:166–176`

When `OMNI_GATEWAY_API_KEY` is empty (unset env var), `_require_api_key` returns immediately without checking credentials. No guard verifies `OMNI_ENV_MODE != "prod"` before bypassing. A production pod with a missing K8s Secret silently becomes fully unauthenticated across all `/kpi/*`, `/siem/*`, `/agents/*`, `/autonomy/*`, `/compliance/*` routes.

```python
# Fix: add prod-mode guard
async def _require_api_key(...) -> None:
    key = os.getenv("OMNI_GATEWAY_API_KEY", "").strip()
    if not key:
        if os.getenv("OMNI_ENV_MODE", "prod").strip().lower() == "prod":
            raise HTTPException(status_code=503, detail="Gateway API key not configured")
        return  # lab only
    ...
```

---

### SEC-C2 — `omni_unrestricted_tool_execution` Bypasses Entire Mutate Allowlist
**File:** `src/workers/autonomous_execute.py:149–162`

`OMNI_UNRESTRICTED_TOOL_EXECUTION=true` skips `K8S_SDK_MUTATING_TOOL_NAMES` entirely and can invoke any tool in `TOOL_REGISTRY`, including shell wrappers. The `_god_mode_implies_lab` `model_validator` in `settings.py` zeroes out `god_mode` in prod but does NOT cover `omni_unrestricted_tool_execution`. A crafted Kafka message could execute arbitrary commands when this flag is set.

Fix: Add to `_god_mode_implies_lab` validator:
```python
if self.env_mode == "prod":
    self.omni_unrestricted_tool_execution = False
```
Even in lab mode, restrict execution to `MUTATE_TOOL_ALLOWLIST`.

---

### SEC-C3 — `dev_mode` Overrides Master Kill-Switch and Rate Limiter
**File:** `src/workers/kafka_actions_consumer.py:187, 236`

```python
auto = bool(getattr(ws, "omni_auto_execute_enabled", False) or dev_mode)
if not dev_mode and await _is_rate_limited(...):
```

`OMNI_ENV_MODE=dev` silently overrides `OMNI_AUTO_EXECUTE_ENABLED=false` (the master kill-switch documented in CLAUDE.md as fail-closed invariant) AND disables per-action rate limiting. If `OMNI_ENV_MODE` is accidentally set to `dev` in a production cluster, the kill-switch and all rate limits evaporate.

Fix: Remove `or dev_mode` from the kill-switch check. Use a separate `OMNI_DEV_AUTO_EXECUTE=true` flag.

---

### COR-C1 — `emit_hitl_pending` Dispatches to Kafka Without Prior CRAT Write
**File:** `src/workers/evidence_mutate_emit.py:336–412`

`emit_hitl_pending` sends to `omni-hitl-pending` at line 398 without calling `write_audit_block` first. The CRAT invariant requires an audit write BEFORE any action dispatch. `emit_execute_mutate` at lines 219–226 does this correctly; `emit_hitl_pending` does not.

Fix: Add before `k.send_dict(topic, ...)`:
```python
await write_audit_block(
    event_type=CRAT_EVENT_MUTATION_ENQUEUED,
    trace_id=trace_id,
    payload={...},
    redis=redis, kafka=k, kafka_topic=audit_topic,
)
```

---

### COR-C2 — `autonomous_decider` Executes Mutating Tools Directly — Bypasses Executor + CRAT
**File:** `src/workers/autonomous_decider.py:344–347, 461–468`

`_tick_legacy` and `_tick_react` call `await fn(ctx, dict(call.args))` and `await _run_tool(ctx, tname, targs)` directly on `_MUTATING_TOOLS` (e.g., `k8s_rollout_restart`, `k8s_scale_deployment`) inside the `core` role, without routing through the executor Kafka pipeline or writing any CRAT block. This violates: *"Mutations only via executor; analyst is read-only."*

Fix: Mutating tool calls must emit to `omni-actions` via `emit_execute_mutate` instead of direct invocation. Read-only tools can continue to execute in place.

---

### PY-C1 — `NameError: 'ollama'` in `ingest_main.py` — Crashes on First Call
**File:** `src/knowledge/ingest_main.py:33`

The `embed_fn` closure captures `ollama`, but the variable is named `llm` (line 29). Raises `NameError` the first time any knowledge ingest runs.

```python
# Fix: replace ollama with llm
return await _embed_batch(llm, model=ws.embed_model, texts=texts, keep_alive=None)
```

---

### PY-C2 — `TypeError: missing 'keep_alive'` in `sop_ingest.py` — Crashes on First Run
**File:** `src/training/sop_ingest.py:119–124`

`_embed_batch` has `keep_alive: str | None` as a required keyword-only parameter (no default). The call at line 119 omits it, raising `TypeError` on every SOP ingest run.

```python
# Fix:
vecs = await _embed_batch(llm, model=settings.embed_model, texts=texts, keep_alive=None)
```

---

### PERF-C1 — `httpx.AsyncClient` Created Per Health-Probe Call
**File:** `src/workers/metrics_exporter.py:484`

`probe_llm_up()` opens a new `httpx.AsyncClient` context manager on every 15-second observability tick, allocating and tearing down a full connection pool each time. ~5–10ms overhead per probe, no keepalive.

```python
# Fix: module-level shared client
_llm_probe_client: httpx.AsyncClient | None = None

async def probe_llm_up(base_url: str) -> None:
    global _llm_probe_client
    if _llm_probe_client is None:
        _llm_probe_client = httpx.AsyncClient(timeout=5.0)
    ...
```

---

## HIGH

### Security

**SEC-H1 — Unvalidated `agent_id` Used as Redis Key — Key Traversal on DELETE**
`src/gateway/routes/agents.py:177, 211` — No length cap or character allowlist on `agent_id` path param. An attacker with API key can supply `agent_id` values like `../../../omni:kpi:z:accepted` to delete arbitrary Redis keys.
```python
_AGENT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")
if not _AGENT_ID_RE.fullmatch(agent_id):
    raise HTTPException(status_code=422)
```

**SEC-H2 — No Schema/Length Validation on Kafka Payload Deserialization**
`src/workers/kafka_actions_consumer.py:112–114` — Kafka `omni-actions` payload is `json.loads`-ed with no byte-length cap or schema enforcement before passing `tool_name`/`args` to the executor. Rogue Kafka messages can cause deeply-nested JSON hash-flooding or oversized memory allocation. Apply a 1 MB payload cap and validate `tool_name` against the allowlist immediately after deserialization.

**SEC-H3 — SSRF: HITL API URL and Slack Webhook Not Allowlisted at Runtime**
`src/workers/hitl_dispatcher.py:76, 91` — Both `HITL_API_BASE_URL` and `OMNI_HITL_FALLBACK_WEBHOOK_URL` are used directly without URL validation. ConfigMap write access lets an insider point them to cloud metadata endpoints.

**SEC-H4 — Log Injection via Unsanitized Kafka-Sourced Strings**
`src/workers/kafka_actions_consumer.py:136–137` — `action_raw`, `trace`, `tool_name` from Kafka are logged without newline stripping. Can forge structured log entries.
```python
def _safe_log(s: str, max_len: int = 200) -> str:
    return (s or "")[:max_len].replace("\n", " ").replace("\r", " ")
```

**SEC-H5 — `chaos_pg_app_password` Stored as Plain `str` — Leaks in Debug Logs**
`src/workers/settings.py:441` — Field has no `repr=False`. `WorkerSettings` printed during startup debug exposes the password in plaintext. Use `SecretStr`.

---

### Correctness

**COR-H1 — `kafka_action_feedback_loop` Missing `auto_offset_reset="earliest"`**
`src/workers/autonomous_feedback_loop.py:1726–1733` — CLAUDE.md invariant: `auto_offset_reset="earliest"` MUST be set so analyst recovers messages during rebalance. This consumer for `omni-action-feedback` omits it.

**COR-H2 — `kafka_actions_loop` Missing `auto_offset_reset="earliest"`**
`src/workers/kafka_actions_consumer.py:99–105` — Same issue for `omni-actions` consumer in the executor. Pod restart can silently skip in-flight `EXECUTE_MUTATE` messages.

**COR-H3 — `_attempt_auto_rollback` CRAT Failure Not Propagated — Telegram Still Fires**
`src/workers/autonomous_feedback_loop.py:97–121` — `write_audit_block` exceptions are caught internally at line 106. Callers at lines 1250–1256 call `emit_telegram_escalation` without knowing if the CRAT write succeeded. Violates fail-closed: Telegram fires even when audit block was not written.
Fix: Propagate `AuditLedgerError` (or return `False`) from `_attempt_auto_rollback` and gate the Telegram emit on it.

**COR-H4 — `handlers.py` Direct Mutation Tool Calls — Bypasses Executor + CRAT**
`src/workers/handlers.py:46–51, 1275–1302` — Telegram confirmation path calls `execute_rollout_restart_from_pending`, `execute_write_pending_from_redis` directly from the analyst/prober role, bypassing executor Kafka pipeline and CRAT audit instrumentation. Same invariant violation as COR-C2.

---

### Python/Async

**PY-H1 — 48-Second Blocking Poll Inside Kafka Consumer Handler**
`src/workers/autonomous_feedback_loop.py:862–866` — `handle_action_feedback_envelope` contains a `for _attempt in range(24): ... await asyncio.sleep(2.0)` loop that monopolizes the consumer for up to 48 seconds, spiking consumer-group lag.

**PY-H2 — Swallowed Exceptions Without Logging in `observe_adaptive`**
`src/anomaly/three_sigma.py:108–109, 125–126` — Redis connection errors and config load failures silently `pass`, making anomaly detection proceed on incorrect default thresholds with no observable signal.

**PY-H3 — `decode_responses` Mismatch: Ingest Scripts vs Production Workers**
`src/anomaly/three_sigma.py:119–120` — Production uses `decode_responses=True`; ingest scripts use `decode_responses=False`. The dual `cfg.get(b"threshold") or cfg.get("threshold")` workaround in `observe_adaptive` masks the inconsistency. Standardize ingest scripts to `decode_responses=True`.

**PY-H4 — Return Type Annotation Mismatch: `-> None` Functions Return `bool`**
`src/pkg/trace_orchestrator/candidates.py:67, 81` — `record_verify_failure_for_candidate` and `record_verify_success_for_candidate` are annotated `-> None` but return `bool`. Fix: change annotations to `-> bool`.

**PY-H5 — Blocking Synchronous File I/O in Async Handler**
`src/workers/archivist.py:94` (called from `autonomous_feedback_loop.py:349, 455`) — `write_incident_postmortem` does blocking `os.makedirs + open(..., "w")` inside an async feedback handler. Use `await asyncio.to_thread(write_incident_postmortem, ...)`.

**PY-H6 — Pydantic v2 `model_validator` Mutates `self` Directly**
`src/workers/settings.py:42–57` — Direct `self.field = ...` in `mode="after"` validator is fragile if the model is ever made frozen (violating the project's immutability mandate). Use `self.model_copy(update={...})` and return the copy.

**PY-H7 — `keep_alive` Also Missing in `knowledge/ingest_main.py` embed call**
`src/knowledge/ingest_main.py:31–37` — Even after fixing the `ollama` NameError, the `_embed_batch` call still omits the required `keep_alive` argument. Add `keep_alive=None`.

**PY-H8 — `VLLMClient` `PrivateAttr` Resources Not Closed on Exception in `model_post_init`**
`src/llm/vllm_client.py:90–101` — If the third `httpx.AsyncClient` constructor raises, `_chat_client` and `_embed_client` are leaked (open but never closed). Wrap with try/except that closes already-created clients before re-raising.

---

### Performance

**PERF-H1 — Extra `LRANGE` Round-Trip Outside Pipeline in `observe_adaptive`**
`src/anomaly/three_sigma.py:72–79` — `LRANGE` is issued separately after the `LPUSH+LTRIM+EXPIRE` pipeline. Add `LRANGE` into the same pipeline, read result from `results[3]`.

**PERF-H2 — Sequential `EXISTS` + `HGETALL` in `observe_adaptive` — Should Be Pipelined**
`src/anomaly/three_sigma.py:104–124` — Two independent Redis reads issued sequentially. Use `pipeline(transaction=False)` to run them in parallel.

**PERF-H3 — Post-Pipeline Sequential `SADD`/`EXPIRE`/`SCARD` in Learning Pattern Stats**
`src/workers/proactive_observer.py:406–417` — 6 extra round-trips per event that could be included in the existing pipeline.

**PERF-H4 — Tight 2s Poll Loop When Feature Disabled**
`src/workers/proactive_observer.py:1058–1063` — `asyncio.sleep(2)` loop when `proactive_enabled=False` burns cycles checking a flag that never changes without restart. Use `asyncio.wait_for(stop.wait(), timeout=30)`.

**PERF-H5 — LLM Called Without RAG Recall Check in `_llm_replan_after_feedback`**
`src/workers/autonomous_feedback_loop.py:556–625` — RAG-first policy (skip LLM if recall score ≥ 0.75) not enforced in the replan path. Add `recall_playbook_advisory()` call before `llm.chat()`.

**PERF-H6 — Unbounded Prometheus Label Cardinality in `inc_slow_path_exhausted`**
`src/workers/metrics_exporter.py:453–458` — `bucket` label accepts raw error signatures (up to 64 chars) derived from alert payloads. Can create thousands of unique label combinations → Prometheus memory growth. Restrict to a small pre-approved enum.

**PERF-H7 — `redis.keys("omni:proactive:elevated:*")` — O(N) Blocking Keyspace Scan**
`src/workers/proactive_observer.py:1134` — `KEYS` is explicitly unsafe in production Redis. Use `SCAN` with a cursor or maintain a dedicated sorted set.

**PERF-H8 — Double Embed for Same Text in Proactive Observer**
`src/workers/proactive_observer.py:238, 295` — `_resolve_from_action_experience` computes an embedding, then `_save_proactive_learning_record` embeds the same canonical text again. Pass the already-computed vector as an optional parameter to the save function.

---

## MEDIUM

### Security
| # | File | Issue |
|---|------|-------|
| SEC-M1 | `gateway/api.py:85` | `generatorURL` in `PrometheusAlert` model not validated — downstream SSRF/redirect |
| SEC-M2 | `workers/settings.py:1434` | `ingest_secrets_raw=true` not blocked in prod by model validator |
| SEC-M3 | `pkg/reasoning/sanitize.py:13` | Prompt injection patterns missing Unicode homoglyphs and markdown fence variants |
| SEC-M4 | `workers/autonomous_decider.py:510` | LLM `thought` field logged raw without `sanitize_for_llm` |

### Correctness
| # | File | Issue |
|---|------|-------|
| COR-M1 | `evidence_mutate_emit.py:372` | Redundant `None` guard — dead code after advisory-mode early-return |
| COR-M2 | `autonomous_feedback_loop.py:423` | Legacy success path skips `mark_trace_orchestrator_resolved_verified` |
| COR-M3 | `autonomous_feedback_loop.py:17` | `from rag.pgvector_store import ...` — CLAUDE.md states Postgres removed |
| COR-M4 | `workers/hitl_dispatcher.py:590` | Malformed Kafka messages silently dropped (no dead-letter write) |

### Python/Async
| # | File | Issue |
|---|------|-------|
| PY-M1 | `anomaly/three_sigma.py:119` | `cfg.get(b"threshold") or cfg.get("threshold")` — design smell masking decode_responses mismatch |
| PY-M2 | `workers/analyst_agentic_loop.py:884` | Missing `asyncio.sleep(0)` yield in tight step loop when all LLM calls fail |
| PY-M3 | `workers/advisory_analyst_handler.py:329` | Bare `except Exception` swallows JSON parse error type — use `json.JSONDecodeError` |
| PY-M4 | `src/llm/vllm_client.py:323` | Parameter `input` shadows Python builtin — rename to `text_input` |
| PY-M5 | `workers/settings.py:577` | `env_mode: Literal["prod", "dev"]` conflicts with CLAUDE.md documenting `"lab"` — `OMNI_ENV_MODE=lab` causes startup validation error |

### Performance
| # | File | Issue |
|---|------|-------|
| PERF-M1 | `rag/redis_vector_store.py:659` | Per-key `HGET` inside SCAN loop — batch with pipeline per SCAN page |
| PERF-M2 | `workers/proactive_observer.py:336` | No back-off in LLM retry loop — floods LLM when degraded |
| PERF-M3 | `workers/proactive_observer.py:83` | Synchronous blocking file write inside async handler + hardcoded dev path |
| PERF-M4 | `workers/autonomous_feedback_loop.py:785` | Multiple sequential `ctx.redis.get`/`setex` calls that could be pipelined |

---

## LOW

| # | Sev | Domain | File | Issue |
|---|-----|--------|------|-------|
| L-1 | LOW | Security | `gateway/api.py:348` | TOCTOU race in token bucket rate limiter — use `asyncio.wait_for(acquire(), timeout=0)` |
| L-2 | LOW | Security | `hitl_dispatcher.py:76` | Hardcoded `HITL_API_BASE_URL` default — namespace squatting risk; require explicit env var |
| L-3 | LOW | Correctness | `omni_worker.py:128,378,623` | `assert ctx.kafka is not None` stripped in `-O` builds — use explicit `RuntimeError` |
| L-4 | LOW | Correctness | `autonomous_decider.py:340` | `ctx.inbound_*` not fully restored in `_tick_legacy` exception path |
| L-5 | LOW | Python | `training/sop_ingest.py:47` | `llm: VLLMClient` annotation without import — breaks static analysis |
| L-6 | LOW | Performance | `vllm_client.py:101` | Single `timeout_s` for both embed and chat — embed timeout fires on long advisory calls |
| L-7 | LOW | Performance | `analyst_advisory_schema.py:208` | `datetime.utcnow()` deprecated since Python 3.12 — use `datetime.now(UTC)` |

---

## Priority Fix Order

**P0 — Fix immediately (runtime crashes + security invariant violations):**
1. PY-C1: `NameError: 'ollama'` in `ingest_main.py`
2. PY-C2: `TypeError: missing 'keep_alive'` in `sop_ingest.py`
3. SEC-C3: `dev_mode` overrides master kill-switch
4. COR-C1: `emit_hitl_pending` no CRAT write before Kafka dispatch
5. COR-C2: `autonomous_decider` direct mutation — no executor + CRAT
6. SEC-C2: `omni_unrestricted_tool_execution` bypasses allowlist
7. SEC-C1: API key bypass in gateway when env var unset

**P1 — Fix before production deploy:**
8. COR-H1/H2: `auto_offset_reset` missing on feedback + actions consumers
9. PY-H5: Blocking file I/O without `asyncio.to_thread`
10. PY-H8: `VLLMClient` resource leak on init exception
11. SEC-H1: `agent_id` Redis key traversal on DELETE
12. PERF-H7: `redis.keys()` O(N) scan → `SCAN`

**P2 — Fix in next sprint (quality + performance):**
- PERF-H1/H2: Pipeline Redis reads in `three_sigma.py`
- PERF-H5: RAG-first check missing in replan path
- PERF-C1: `httpx.AsyncClient` per health probe
- PERF-H8: Double embed in proactive observer
- SEC-H3: SSRF — HITL URL allowlist
- PY-M5: `Literal["prod","dev"]` vs CLAUDE.md `"lab"` — startup breakage
- SEC-M3: Prompt injection Unicode/markdown patterns
- PERF-M3: Debug `_dbg_log` hardcoded path — remove before prod
