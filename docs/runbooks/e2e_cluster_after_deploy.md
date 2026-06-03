# E2E cluster checklist (after deploy)

Run after `make deploy-worker` or equivalent rollout to verify the pipeline is healthy.

## Step 1 — CI-friendly unit/E2E tests (no live cluster required)

```bash
# Lane 1 (SYS_RESOURCE) + Lane 2 (SYS_HARD_FAIL) + system-wide pipeline
.venv/bin/python -m pytest tests/e2e/ -v

# Lane 3 (APP_HTTP) + Lane 4 (SIEM_SECURITY) — must not regress
.venv/bin/python -m pytest tests/test_e2e_diagnostic_lanes.py -v
```

All tests are CI-friendly (FakeRedis + KafkaCapture, no live Kafka/LLM required).

| Test file | Lane coverage |
|-----------|--------------|
| `tests/e2e/test_lane1_resource.py` | SYS_RESOURCE: ThreeSigmaGate, baseline_snapshot, CRAT fail-closed |
| `tests/e2e/test_lane2_sys_hard_fail.py` | SYS_HARD_FAIL: os_state_validator, coerce_evidence_dict, OS loop guard |
| `tests/e2e/test_pipeline_system.py` | Health server, KPI rolling window, tenant isolation |
| `tests/test_e2e_diagnostic_lanes.py` | APP_HTTP (Lane 3), SIEM_SECURITY (Lane 4) |

## Step 2 — Preflight: ConfigMap YAML

If `make deploy-worker` fails with `error converting YAML to JSON` or `did not find expected key` on `k8s/deployments/omni-worker-configmap.yaml`: every value in `data:` must be on **one line**. Common failure: URL value wraps to a new line without a key (often around `OMNI_VLLM_EMBED_URL`).

Smoke check before rollout:
```bash
./scripts/with_working_kube.sh apply -f k8s/deployments/omni-worker-configmap.yaml --dry-run=client -o yaml >/dev/null && echo OK
```

## Step 3 — Verify pods

```bash
kubectl get pods -n multi-agent -l 'app in (omni-analyst,omni-prober,omni-core,omni-executor)'
kubectl get pods -n multi-agent -l app=omni-gateway
```

## Step 4 — Live cluster E2E (requires cluster)

| Scenario | Command |
|----------|---------|
| Smoke contrast + suggest (HighCPU default) | `NS=multi-agent bash scripts/gateway_alert_loki_verify.sh` |
| Full advisory LLM + CRAT + Telegram | `NS=multi-agent bash scripts/e2e_one_alert_full_advisory_path.sh` |
| CRAT pipeline integrity | `python scripts/verify_e2e_crat_pipeline.py` |

## Step 5 — Live CRAT pipeline verification (optional, requires cluster)

```bash
python scripts/verify_e2e_crat_pipeline.py
# Auto-resolves OrbStack ClusterIPs via kubectl
# Override: E2E_KAFKA_BOOTSTRAP / E2E_REDIS_MA_URL / E2E_REDIS_FG_URL
```

## Pass / Fail

- **Pass:** All `tests/e2e/` pass; at least one live trace end-to-end with expected lane transitions; CRAT blocks written (`signed=True` in logs).
- **Fail:** Record `trace_id`, transition mismatch, or cluster blocker — do not merge until post-mortem is filed.

## Lane Architecture Reference

- Lane 1 (SYS_RESOURCE): `docs/lanes/lane1_resource.md`
- Lane 2 (SYS_HARD_FAIL): `docs/lanes/lane2_sys_hard_fail.md`
- Full flow evidence checklist: `docs/runbooks/e2e_full_flow_evidence_checklist.md`
