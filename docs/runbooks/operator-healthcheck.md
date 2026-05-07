# Operator Health Check Guide — Omni SRE Platform

Run these checks to verify the system is healthy after deploy or during on-call shifts.

---

## Quick Health Check (2 minutes)

```bash
# 1. All pods running?
kubectl get pods -n multi-agent

# Expected: All Running, 0 Restarts for core workers
# omni-prober-*     1/1   Running
# omni-analyst-*    1/1   Running
# omni-core-*       1/1   Running
# omni-executor-*   1/1   Running
# omni-gateway-*    1/1   Running

# 2. Gateway healthy?
curl -s https://gateway.ai-agent.local/healthz
# Expected: {"status": "ok"}

# 3. LLM reachable?
curl -s http://ollama:11434/api/health
# Expected: {"status": "ok"}
```

---

## Kafka Consumer Lag

```bash
# Check all consumer groups
kubectl exec -n multi-agent deploy/kafka -- \
  /opt/kafka/bin/kafka-consumer-groups.sh \
  --bootstrap-server kafka:9092 \
  --describe --all-groups 2>/dev/null | grep -E "omni-worker|GROUP"

# Expected: LAG column = 0 or small number (<10)
# Red flag: LAG > 100 on omni-diagnostic-evidence
```

---

## CRAT Audit Chain

```bash
# Check chain length
kubectl exec -n multi-agent deploy/omni-analyst -- \
  redis-cli -u $OMNI_REDIS_URL llen audit_chain:blocks
# Expected: > 0 if any advisories have been processed

# Run integrity check
kubectl create job --from=cronjob/crat-integrity-check crat-check-manual -n multi-agent
kubectl wait job/crat-check-manual -n multi-agent --for=condition=complete --timeout=60s
kubectl logs -n multi-agent job/crat-check-manual
# Expected: "CRAT chain OK — N blocks verified"
kubectl delete job crat-check-manual -n multi-agent
```

---

## Grafana Panels to Check

Open Grafana → Dashboard "Omni Ops" and verify:

| Panel | Healthy State |
|-------|--------------|
| Gateway 200 rate (5m) | > 0 if receiving alerts |
| Kafka Consumer Lag | < 10 on omni-diagnostic-evidence |
| Circuit Breaker State | CLOSED (green) |
| LLM Up | 1 |
| CRAT Integrity | OK (green) |
| Telegram Send Timeouts | 0 or near 0 |
| CRAT Write Latency (p99) | < 100ms |

---

## Metrics Spot Check

```bash
# Pull raw metrics from gateway
curl -s https://gateway.ai-agent.local/metrics | grep -E "omni_gateway_requests|omni_circuit_breaker"

# Pull worker metrics (exposed on port 9090 internally)
kubectl port-forward -n multi-agent deploy/omni-analyst 9090:9090 &
curl -s http://localhost:9090/metrics | grep -E "omni_kafka_consumer_lag|omni_llm_up|omni_crat"
kill %1
```

---

## Post-Deploy Verification

After any deploy, run in order:

```bash
# 1. Validate prerequisites
NS=multi-agent make pre-deploy-validate

# 2. Verify Kafka topics
make ensure-kafka-topics

# 3. Check pod readiness
kubectl rollout status deployment/omni-analyst -n multi-agent --timeout=120s

# 4. Smoke test CRAT pipeline
python3 scripts/verify_e2e_crat_pipeline.py --smoke-only

# 5. Manual: send a test alert through gateway and verify Telegram message arrives
bash scripts/e2e_one_alert_full_advisory_path.sh
```

---

## Rollback Procedure

If post-deploy health check fails:

```bash
# Rollback all workers to previous revision
NS=multi-agent make rollback

# Verify rollback succeeded
NS=multi-agent make rollback-verify

# Check which revision is now active
kubectl rollout history deployment/omni-analyst -n multi-agent
```

**Rollback SLA:** Previous revision should be active within 2 minutes.

---

## Secret Expiry Check

```bash
# Check annotation on audit keys
kubectl get secret omni-audit-keys -n multi-agent \
  -o jsonpath='{.metadata.annotations}' | python3 -m json.tool
# Look for omni.io/rotated-at — should be < 90 days ago

# Check gateway secret
kubectl get secret omni-gateway-secret -n multi-agent \
  -o jsonpath='{.metadata.annotations}' | python3 -m json.tool
```

Rotate if > 90 days:
```bash
bash scripts/rotate_audit_key.sh
bash scripts/rotate_gateway_secret.sh
```
