# Lane 1 — SYS_RESOURCE (Time-Series Anomaly)

## Architecture Flow

```
baseline_snapshot_loop (core role)
    ↓ every tick
ThreeSigmaGate.observe_adaptive()  ← src/anomaly/three_sigma.py:96
    ↓ writes window to Redis LIST: 3sigma:metric:{id}
_sigma_dr(z_cpu, z_mem, threshold)  ← src/workers/baseline_snapshot.py:274
    ↓
Redis SET: omni:baseline:snapshot (JSON, TTL 24h)
    ↓
kafka: omni-diagnostic-evidence (lane=resource)
    ↓
reason_from_diagnostic_evidence()  ← src/workers/evidence_consumer.py
    ↓
_proof_of_fault_gate()  ← src/workers/evidence_consumer.py:665
    ↓ sigma_ok = bool(dr or z_hit)
    ├─ sigma_ok=False → ERR_REA_SIGMA_GATE_BLOCKED (advisory skipped)
    └─ sigma_ok=True  → injects "3-SIGMA RESOURCE BASELINE" block into evidence_text
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
| `src/anomaly/three_sigma.py` | `ThreeSigmaGate`: rolling z-score computation, Redis LIST management |
| `src/workers/baseline_snapshot.py` | `baseline_sync_loop`: Prometheus query + z-score write to Redis. Constants: `REDIS_KEY_SNAPSHOT = "omni:baseline:snapshot"` (line 30), `REDIS_KEY_TS = "omni:baseline:ts"` (line 31) |
| `src/workers/evidence_consumer.py:665` | `_proof_of_fault_gate()`: reads snapshot, computes `sigma_ok`, injects `SIGMA_RESOURCE_EVIDENCE_BASELINE_MARKER` |
| `src/anomaly/forecast.py` | `linear_forecast_horizon()` + `oom_risk_from_series()` — 5 horizons (1h/3h/6h/12h/24h) |
| `src/workers/advisory_analyst_handler.py:176` | `run_advisory_analyst()` — LLM advisory invocation |
| `src/services/audit_ledger/chain_writer.py` | `write_audit_block()` — CRAT fail-closed gate |

## Alert Conditions

| Parameter | Value | Source |
|-----------|-------|--------|
| Anomaly threshold | `\|z\| > 3.0` | `src/anomaly/three_sigma.py:25` (`DEFAULT_THRESHOLD`) |
| Window size | 100 samples | `src/anomaly/three_sigma.py:22` (`DEFAULT_WINDOW`) |
| TTL per metric key | 3600s | `src/anomaly/three_sigma.py:23` (`DEFAULT_TTL_SEC`) |
| Redis key pattern | `3sigma:metric:{metric_id}` | Redis LIST, newest value at index 0 |
| Snapshot key | `omni:baseline:snapshot` | JSON: `{dr, z_cpu, z_mem, threshold}` |
| Gate bypass | `omni_sigma_log_bypass_enabled=True` | Settings flag; skips gate for log-surge events |
| Proof lane enabled | `omni_proof_lane_enabled=True` (default) | Settings flag; enables sigma gate check |

## Gate Logic (`_proof_of_fault_gate`, line 665)

```python
sigma_ok = bool(dr or z_hit)
# dr = snapshot["dr"] (bool: at least one z-score > threshold)
# z_hit = any(|z| > threshold for z in [z_cpu, z_mem])
```

- `sigma_ok=True` → injects `"3-SIGMA RESOURCE BASELINE"` block containing z_cpu, z_mem, and per-z ANOMALY/NORMAL label
- `sigma_ok=False` → returns `ERR_REA_SIGMA_GATE_BLOCKED` (constant in `src/workers/error_codes.py`) — advisory NOT called

## CRAT Fail-Closed Invariant

`write_audit_block()` (from `services.audit_ledger.chain_writer`) is called before any Telegram emit or Kafka dispatch. If it raises `AuditLedgerError`, the entire advisory is aborted — no message reaches `omni-actions` or Telegram. This is an intentional SOX/PCI-DSS invariant.

## Failure Modes

| Mode | Behavior | Source |
|------|----------|--------|
| Snapshot key absent | `sigma_ok=False` if `dr=False` in missing snap; gate may block | `_proof_of_fault_gate:693` |
| Window < 3 samples | `observe()` returns `(False, None)` — gate passes through without z-score | `three_sigma.py:60-65` |
| std ≈ 0 (all identical values) | `observe()` returns `(False, None)` — degenerate distribution protection | `three_sigma.py:84-86` |
| `write_audit_block()` fails | Advisory aborted, `ERR_CRAT_WRITE_FAILED` logged, no Kafka dispatch | `evidence_consumer.py` CRAT block |
| Prometheus z-score unavailable | Falls back to `ThreeSigmaGate.get_z_score("cluster_cpu/mem")` | `baseline_snapshot.py:428-437` |

## Run Locally

```bash
# Unit/integration tests for Lane 1
.venv/bin/python -m pytest tests/e2e/test_lane1_resource.py -v

# All E2E tests
.venv/bin/python -m pytest tests/e2e/ -v

# Observe ThreeSigmaGate state in Redis (lab)
redis-cli -p 16379 LRANGE "3sigma:metric:cpu" 0 -1
redis-cli -p 16379 GET "omni:baseline:snapshot"
```

## Related Post-Mortems

- `docs/post-mortems/tr-verified.md` — HighCPU alert resolved via `k8s_rollout_restart`
- `docs/post-mortems/hot-cache-single-001.md` — NginxTestContainerWaitingFaultLab, single-trace verified
- `docs/post-mortems/trace1.md`, `trace2.md`, `trace3.md` — multi-trace parallel verification runs
