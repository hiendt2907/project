# Lane 2 — SYS_HARD_FAIL (OS State Validation)

## Architecture Flow

```
kafka: omni-diagnostic-evidence (symptom_group=sys_hard_fail, layer=os_baremetal)
    ↓
reason_from_diagnostic_evidence()  ← src/workers/evidence_consumer.py
    ↓
compare_alert_claim_to_sdk_state()  ← sync, SDK-level contrast check
    ↓ (if no SDK contrast)
resolve_proof_lane(batch)  ← src/pkg/reasoning/incident_matrix_profile.py
    ├─ lane != "state" → run_os_diagnostic_loop() SKIPPED (early-return guard, line 2072)
    └─ lane == "state" →
        run_os_diagnostic_loop()  ← src/workers/os_diagnostic_loop.py (async)
            ↓
        compare_alert_claim_to_os_state(evidence_by_probe)  ← src/workers/os_state_validator.py:529
            ├─ probe PASSED + alert claims SYS_HARD_FAIL → contrast string returned
            │    └─ _emit_suggest_remediation() called; advisory exits early
            └─ probe FAILED or no contrast → None returned
                ↓
        recall_playbook_advisory()  ← src/workers/archivist.py:129
            ├─ score >= 0.85 → priority prefix injected, LLM skipped
            ├─ 0.70 <= score < 0.85 → hint prepended, LLM still invoked
            └─ score < 0.70 → LLM invoked fresh
                ↓
        run_advisory_analyst()  ← src/workers/advisory_analyst_handler.py:176
            ↓
        write_audit_block()  ← CRAT fail-closed
            ↓
        kafka: omni-actions (SUGGEST_REMEDIATION)
```

## Key Files

| File | Purpose |
|------|---------|
| `src/workers/os_state_validator.py` | `compare_alert_claim_to_os_state()` (line 529): sync function, takes `evidence_by_probe: dict[str, dict]` — does NOT read Redis directly |
| `src/workers/os_diagnostic_loop.py` | `run_os_diagnostic_loop()`: async, aggregates probe results, calls os_state_validator |
| `src/workers/evidence_consumer.py:2072` | Early-return guard: `if _pre_lane == "state": await run_os_diagnostic_loop(...)` |
| `src/pkg/reasoning/schema.py:44` | `coerce_evidence_dict()`: normalizes evidence dicts, guarantees `trace_id` field |
| `src/workers/archivist.py:129` | `recall_playbook_advisory()`: RAG recall before LLM |
| `src/workers/advisory_analyst_handler.py:176` | `run_advisory_analyst()`: LLM advisory invocation |

## Probe Inventory (`os_state_validator.py`)

| Probe Key | Handler Line | What It Checks |
|-----------|-------------|----------------|
| `systemd_units` | 85 | `systemctl list-units` — critical services active |
| `disk_usage` | 162 | `df -h / df -i` — partition usage + inodes |
| `storage_nfs` | 179 | NFS mount responsiveness |
| `mysql_health` | 306 | MySQL process + replication |
| `proxysql_health` | 324 | ProxySQL process + backend pool |
| `service_haproxy` / `service_haproxy_prom` | 397 | HAProxy process + stats socket |
| Unregistered probes | 529+ fallback | Generic: PASSED + no anomaly keywords → contrast |

Probe evidence format expected by `compare_alert_claim_to_os_state`:
```python
evidence_by_probe = {
    "systemd_units": {
        "probe": "systemd_units",
        "result": "PASSED",           # "PASSED" | "FAILED"
        "extracted_fact": '{"critical_failed_units": []}',
        "alert_hint": "all units active",
    }
}
```

## Recall Thresholds (archivist.py)

| Threshold | Value | Behavior |
|-----------|-------|----------|
| Include threshold | `0.70` (`_RECALL_SCORE_THRESHOLD`, line 48) | Score >= 0.70: recalled advisory included as hint |
| Strong threshold | `0.85` (`_RECALL_STRONG_THRESHOLD`, line 50) | Score >= 0.85: recalled advisory used as priority prefix; LLM invocation skipped |
| Below include | < 0.70 | RAG miss; LLM invoked fresh with no recall context |

## Early-Return Guard (evidence_consumer.py:2072)

```python
_pre_lane, _ = resolve_proof_lane(batch)
os_contrast = (
    await run_os_diagnostic_loop(ctx, batch, by_probe, _alert_ctx, trace)
    if _pre_lane == "state" else None
)
```

Only `lane == "state"` (SYS_HARD_FAIL) reaches `run_os_diagnostic_loop`. Resource, app_log, and SIEM lanes return `None` immediately — K8s/OS probe logic never runs.

## CRAT Fail-Closed Invariant

Same as Lane 1: `write_audit_block()` must succeed before any Telegram or Kafka dispatch. `AuditLedgerError` aborts the advisory — no message reaches `omni-actions`.

## Failure Modes

| Mode | Behavior |
|------|----------|
| `evidence_by_probe` empty | `compare_alert_claim_to_os_state` returns `None`; pipeline continues normally |
| Probe PASSED, no anomaly keywords | Contrast string returned; advisory exits early via `_emit_suggest_remediation()` |
| Probe FAILED | `None` returned; confirms real fault; LLM path proceeds |
| `coerce_evidence_dict()` given missing `trace_id` | Function adds empty string `""` as fallback; always safe |
| Unregistered probe with anomaly keywords in extracted_fact | Fallback handler returns `None` to prevent false contrast |

## RAG Training Data

- File: `data/rag_training/sys_hard_fail_os_advisory_pairs.jsonl` — 40 pairs
- Coverage: systemd (15), disk/NFS (8), MySQL/ProxySQL (4), HAProxy (3), OOM/kernel (3), network (3), node-level K8s (4)
- Ingest: `PYTHONPATH=src .venv/bin/python src/training/advisory_ingest.py --path data/rag_training/sys_hard_fail_os_advisory_pairs.jsonl --redis-url redis://localhost:16379/0`

## Run Locally

```bash
# Unit/integration tests for Lane 2
.venv/bin/python -m pytest tests/e2e/test_lane2_sys_hard_fail.py -v

# All E2E tests
.venv/bin/python -m pytest tests/e2e/ -v

# Verify RAG training data loaded
redis-cli -p 16379 HLEN "omni:rag:sop"  # should be >= 1000
```

## Related Post-Mortems

- `docs/post-mortems/e2e-parallel-death-loop-001.md` — Lane 3/4 failures in parallel harness (historical; Lane 2 passed in that run)
- `docs/post-mortems/test-selflearn-001.md` — NginxTestContainerWaitingFaultLab resolved via `k8s_create_or_patch_configmap` (missing ConfigMap → SYS_HARD_FAIL path)
