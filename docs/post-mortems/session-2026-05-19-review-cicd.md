# Session Report — Code Review + CI/CD Pipeline
**Date:** 2026-05-19  
**Branch:** main (`6cda27e`)  
**Scope:** Local uncommitted changes (46 source files, ~2000 net insertions)

---

## 1. Code Review

### Changeset Summary

| Domain | Files | Net Lines |
|---|---|---|
| Python workers | 31 files | +1 740 |
| Python pkg/anomaly/llm | 8 files | +220 |
| TypeScript UI | 6 files | +203 |
| Config/Makefile/K8s | remaining | misc |

**Tests before review:** 4 214 passed.

---

### Findings

#### HIGH — 1 issue

**`src/workers/autonomous_feedback_loop.py:110-114` — `write_audit_block` called with wrong signature**

```python
# BEFORE (broken — TypeError at runtime, silently caught)
await write_audit_block(
    CRAT_EVENT_ROLLBACK_EXECUTED,
    {"trace_id": trace, "reason_code": reason_code, "rollback_msg": msg},
    redis=ctx.redis, kafka=kafka, topic=audit_topic,
)

# AFTER (correct)
await write_audit_block(
    event_type=CRAT_EVENT_ROLLBACK_EXECUTED,
    trace_id=trace,
    payload={"trace_id": trace, "reason_code": reason_code, "rollback_msg": msg},
    redis=ctx.redis,
    kafka=kafka,
    kafka_topic=audit_topic,
)
```

`write_audit_block` is keyword-only (`*`). The old call used 2 positional args and `topic=` instead of `kafka_topic=`. At runtime this always raised `TypeError`, caught by the outer `try/except Exception`, so CRAT `ROLLBACK_EXECUTED` audit blocks **silently never wrote** — violating fail-closed CRAT invariant for the auto-rollback path.

---

#### MEDIUM — 4 issues

**M1 — `src/workers/autonomous_feedback_loop.py` — `ImportError` in SOP promo logged at DEBUG**

Missing modules (`execution.memory_normalize`, `services.learning_promoter`) silently swallowed at the same DEBUG level as normal "no fingerprint" skips. Deployment errors masked.

```python
# BEFORE
except Exception as _promo_err:
    logger.debug("event=sop_promo_skip trace=%s err=%s", trace, _promo_err)

# AFTER
except Exception as _promo_err:
    if isinstance(_promo_err, ImportError):
        logger.warning("event=sop_promo_import_error trace=%s err=%s", trace, _promo_err)
    else:
        logger.debug("event=sop_promo_skip trace=%s err=%s", trace, _promo_err)
```

---

**M2 — `src/llm/vllm_client.py` — New `httpx.AsyncClient` per `think=False` call (no connection pooling)**

`_chat_ollama_native()` opened a fresh `httpx.AsyncClient` context manager on every call. Every advisory invocation with `think=False` opened and closed a TCP connection to Ollama (~1–10 ms overhead, no keepalive reuse).

```python
# BEFORE
async with httpx.AsyncClient(timeout=self.timeout_s) as client:
    resp = await client.post(url, json=body)

# AFTER — shared client stored on instance
# In model_post_init:
self._native_client = httpx.AsyncClient(timeout=self.timeout_s)

# In aclose:
await self._native_client.aclose()

# In _chat_ollama_native:
resp = await self._native_client.post(url, json=body)
```

---

**M3 — `src/workers/hitl_dispatcher.py` — Module-level env reads bypass `WorkerSettings` Pydantic bounds**

`_ESCALATION_TIMEOUT_SEC`, `_FALLBACK_CHANNEL`, `_FALLBACK_WEBHOOK_URL`, `_DEAD_LETTER_TTL_SEC` were read from `os.environ` at import time without the bounds validation added to `WorkerSettings` (ge/le). Risk: a misconfigured `OMNI_HITL_ESCALATION_TIMEOUT_SEC=0` would evaluate to `max(60,0)=60` — but `_ESCALATION_TIMEOUT_SEC` could exceed `_APPROVAL_TIMEOUT_SEC`, making escalation trigger after the window closes.

```python
# AFTER — bounds mirror WorkerSettings validators; escalation strictly < approval window
_ESCALATION_TIMEOUT_SEC: Final[int] = min(
    _APPROVAL_TIMEOUT_SEC - 1,
    max(60, int(os.environ.get("OMNI_HITL_ESCALATION_TIMEOUT_SEC", "900"))),
)
_DEAD_LETTER_TTL_SEC: Final[int] = min(
    604800,  # 7 days max (WorkerSettings le=604800)
    max(3600, int(os.environ.get("OMNI_HITL_DEAD_LETTER_TTL_SEC", "86400"))),
)
```

---

**M4 — `src/workers/forecast_autonomous_loop.py` — `statefulset` label missing from `_DEP_RE` regex**

`_infer_labels_from_promql` matched `deployment|workload` but not `statefulset`, inconsistent with `omni_worker.py:_alert_fingerprint` which checks all three. Forecast proactive integration for StatefulSet workloads emitted with empty `dep=`, making elevated-watch Redis keys useless for StatefulSet-backed services.

```python
# BEFORE
_DEP_RE = re.compile(r'(?:deployment|workload)\s*=\s*"([^"]+)"')

# AFTER
_DEP_RE = re.compile(r'(?:deployment|workload|statefulset)\s*=\s*"([^"]+)"')
```

---

#### LOW — 2 notes (no fix required)

**L1 — `advisory_analyst_handler.py` — `temperature` changed 0.2 → 0.0**  
Combined with `think=False` and `format="json"`, advisory output is maximally deterministic. Monitor benchmark score distribution for edge-case regression.

**L2 — `three_sigma.py:observe_adaptive` — bytes key path untested**  
`hgetall` returns `bytes` keys in production (`decode_responses=False`). Code handles both paths correctly. `FakeAsyncRedis(decode_responses=True)` in tests only exercises the string path. Not a bug, coverage gap.

---

### Tests after code review fixes

```
4 214 passed, 113 warnings
```

---

## 2. CI/CD Pipeline

### Phase 1 — BUILD ✓

```
docker build omni-worker     → multi-agent-system:latest    OK
docker build omni-gateway    → omni-gateway:latest          OK
docker build finguard-hitl   → finguard-hitl-api:lab        OK
```

---

### Phase 2 — GATES ✓

All 11 gates passed on first run:

```
asyncio-lint                 OK
secret-gate                  OK  (no leaks found, 36.9s scan)
env-mode-gate                OK
mutate-only-gate             OK
auto-execute-gate            10/10 passed
classifier-regression-gate   OK
phase-docs-gate              OK
nonimpact-guards-gate        OK
learning-loop-gate           OK
unit-tests (90% coverage)    4 214 passed
hitl-gate                    14/14 passed
full_system_audit            PASS (90s, strict, 9 gateway traces)
```

Also ran the new CI step added in `.github/workflows/ci.yml`:
```
advisory-schema-gate         120/120 passed
```

---

### Phase 3 — DEPLOY

#### Worker / SIEM Stack ✓
```
ensure-kafka-topics    OK (omni-audit-chain compacted)
deploy-worker          OK (prober, analyst, core, executor all rolled out)
deploy-siem-stack      OK (siem-bridge, hitl-dispatcher, evidence-adapter)
```

#### Gateway — 2 fixes required

**Fix 1: `Dockerfile.gateway` missing `pkg/reasoning` + `pkg/rag`**

Gateway crashed on startup:
```
ModuleNotFoundError: No module named 'pkg.reasoning'
```

`src/gateway/routes/agent_webhook.py` (new in this changeset) imports `pkg.reasoning.domain_signals`, `pkg.reasoning.evidence_fingerprint`, and `pkg.reasoning.sanitize`. These modules were not copied into the gateway image.

Added to `Dockerfile.gateway`:
```dockerfile
COPY src/pkg/reasoning/ /app/src/pkg/reasoning/
COPY src/pkg/rag/ /app/src/pkg/rag/
```

**Fix 2: `src/pkg/rag/__init__.py` auto-import cascade**

After adding `pkg/rag/`, gateway still crashed:
```
ModuleNotFoundError: No module named 'rag'
```

Chain: `pkg/rag/__init__.py` auto-imported `pkg.rag.gate` → `rag.pgvector_store` (top-level `rag/` module, not in gateway image).

Verified: no code anywhere does `from pkg.rag import RagGateOutcome` (using `__init__` auto-import). All callers use `from pkg.rag.gate import ...` directly.

```python
# BEFORE (src/pkg/rag/__init__.py)
from pkg.rag.gate import RagGateOutcome, evaluate_rag_gate, normalize_rag_query

# AFTER — removed auto-import; __all__ preserved for documentation
__all__ = ["RagGateOutcome", "evaluate_rag_gate", "normalize_rag_query"]
```

Gateway deployed successfully after these two fixes.

---

### Phase 4 — E2E

#### Proactive E2E ✓
```
make e2e-proactive    PASS  (full_system_audit summary.pass=true)
```

#### Incident Matrix — 1 fix required

**Fix: `RBAC_NEGATIVE_NAMESPACE` not set**

```
[e2e] FAIL: Set RBAC_NEGATIVE_NAMESPACE or OUT_OF_SCOPE_TEST_NAMESPACE
```

The RBAC test verifies that `omni-executor` SA cannot patch deployments in an out-of-scope namespace. `executor-rbac.yaml` intentionally grants access to `multi-agent`, `production`, `staging`, and `default` — all four are "allowed". `kube-system` is present in all clusters and the SA has no binding there.

```bash
# BEFORE (Makefile)
NS=multi-agent bash scripts/e2e_incident_matrix.sh

# AFTER (Makefile + omni_dev_death_loop.sh)
NS=multi-agent RBAC_NEGATIVE_NAMESPACE=kube-system bash scripts/e2e_incident_matrix.sh
```

Also patched `scripts/omni_dev_death_loop.sh` (which calls the script directly, bypassing the Makefile):
```bash
export RBAC_NEGATIVE_NAMESPACE="${RBAC_NEGATIVE_NAMESPACE:-kube-system}"
```

Result after fix:
```
═══════════════════════════════════════════════
  Wave A1 + Phase B — E2E Results
  Total  : 9   Passed : 9   Failed : 0   OK : YES
  ✓ wave_a1_rbac_manifest
  ✓ wave_a1_rbac_permissions
  ✓ phase_b_pytest
  ✓ phase_b_unit_full
  ✓ phase_b_api_resource / api_state / api_app_log_fc
  ✓ phase_b_sec_audit
  ✓ nginx_waiting_fault
═══════════════════════════════════════════════
```

---

## 3. Full Fix List

| # | Severity | File | Description |
|---|---|---|---|
| 1 | HIGH | `src/workers/autonomous_feedback_loop.py` | `write_audit_block` positional→keyword args, `topic=`→`kafka_topic=`, added `trace_id=` |
| 2 | MEDIUM | `src/workers/autonomous_feedback_loop.py` | SOP promo `ImportError` logs at WARNING not DEBUG |
| 3 | MEDIUM | `src/llm/vllm_client.py` | Shared `_native_client: httpx.AsyncClient` on instance; `aclose()` updated |
| 4 | MEDIUM | `src/workers/hitl_dispatcher.py` | `_ESCALATION_TIMEOUT_SEC` clamped to `< _APPROVAL_TIMEOUT_SEC`; `_DEAD_LETTER_TTL_SEC` capped at 604800 |
| 5 | MEDIUM | `src/workers/forecast_autonomous_loop.py` | `_DEP_RE` regex: added `statefulset` label |
| 6 | DEPLOY | `Dockerfile.gateway` | Added `COPY src/pkg/reasoning/` and `COPY src/pkg/rag/` |
| 7 | DEPLOY | `src/pkg/rag/__init__.py` | Removed auto-import cascade (`pkg.rag.gate` → `rag.pgvector_store`) |
| 8 | E2E | `Makefile` | `e2e-incident-matrix`: set `RBAC_NEGATIVE_NAMESPACE=kube-system` |
| 9 | E2E | `scripts/omni_dev_death_loop.sh` | Export `RBAC_NEGATIVE_NAMESPACE=kube-system` before matrix run |

---

## 4. Final State

```
Unit tests:          4 214 passed ✓
Advisory schema:       120 passed ✓
Autonomy gate:     11/11 gates   ✓
Gateway deploy:          healthy ✓
E2E incident matrix:  9/9 passed ✓
E2E proactive:             pass  ✓
```
