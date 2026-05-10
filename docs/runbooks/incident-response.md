# Incident Response Runbook — Omni SRE Platform

## Overview

This runbook covers the 5 most common failure scenarios. Each section follows: **Symptoms → Diagnose → Fix → Verify → Escalate**.

---

## Scenario 1: Analyst Not Sending Telegram Advisories

**Symptoms:** Alerts arrive in Alertmanager but no Telegram messages received for >5 minutes.

**Diagnose:**
```bash
# 1. Check analyst pod status
kubectl get pods -n multi-agent -l app=omni-analyst

# 2. Check analyst logs for recent activity
kubectl logs -n multi-agent deploy/omni-analyst --tail=100 | grep -E "event=advisory|ERROR|CRAT"

# 3. Check Kafka consumer lag
kubectl exec -n multi-agent deploy/kafka -- \
  /opt/kafka/bin/kafka-consumer-groups.sh --bootstrap-server kafka:9092 \
  --describe --group omni-worker-analyst

# 4. Check if CRAT write is failing (CRAT fail-closed blocks Telegram)
kubectl logs -n multi-agent deploy/omni-analyst --tail=200 | grep "crat_write"

# 5. Check Telegram bot token
kubectl get secret telegram-bot -n multi-agent -o jsonpath='{.data.TELEGRAM_BOT_TOKEN}' | base64 -d | wc -c
```

**Fix:**
```bash
# If CRAT failing: check Redis connectivity
kubectl exec -n multi-agent deploy/omni-analyst -- redis-cli -u $OMNI_REDIS_URL ping

# If Kafka lag > 1000: restart analyst
kubectl rollout restart deployment/omni-analyst -n multi-agent
kubectl rollout status deployment/omni-analyst -n multi-agent --timeout=120s

# If Telegram token expired: rotate and restart
kubectl rollout restart deployment/omni-analyst -n multi-agent
```

**Verify:** Wait 2 minutes, trigger a test alert via `make e2e-proactive`.

**Escalate:** If not resolved in 15 minutes → page on-call SRE.

---

## Scenario 2: Kafka Consumer Not Processing (Consumer Lag Growing)

**Symptoms:** `omni_kafka_consumer_lag` metric rising in Grafana; `KafkaConsumerLagCritical` alert fires.

**Diagnose:**
```bash
# 1. Check consumer group lag
kubectl exec -n multi-agent deploy/kafka -- \
  /opt/kafka/bin/kafka-consumer-groups.sh --bootstrap-server kafka:9092 \
  --describe --group omni-worker-analyst

# 2. Check if consumer is stuck on one message
kubectl logs -n multi-agent deploy/omni-analyst --tail=50 | grep "evidence_loop"

# 3. Check if LLM is timing out (causing consumer to be slow)
kubectl logs -n multi-agent deploy/omni-analyst --tail=100 | grep -E "LLM_TIMEOUT|TimeoutError"

# 4. Check Ollama health
curl http://ollama:11434/api/health
```

**Fix:**
```bash
# If LLM timeout: check Ollama pod
kubectl get pods -n multi-agent -l app=ollama
# Restart Ollama if unhealthy
kubectl rollout restart deployment/ollama -n multi-agent

# If consumer is stuck on bad message: skip and restart
kubectl rollout restart deployment/omni-analyst -n multi-agent
# Note: auto_offset_reset=earliest means messages are NOT lost on restart

# If Kafka broker issue: check broker pod
kubectl get pods -n multi-agent -l app=kafka
```

**Verify:**
```bash
# Watch lag decrease
watch -n5 'kubectl exec -n multi-agent deploy/kafka -- \
  /opt/kafka/bin/kafka-consumer-groups.sh --bootstrap-server kafka:9092 \
  --describe --group omni-worker-analyst | grep omni-diagnostic'
```

**Escalate:** If broker is down → escalate to infra team.

---

## Scenario 3: CRAT Audit Chain Integrity Failure

**Symptoms:** `CRATIntegrityFailed` alert fires; `omni_crat_integrity_ok == 0` in Grafana.

**Diagnose:**
```bash
# 1. Run integrity check manually
kubectl exec -n multi-agent deploy/omni-analyst -- \
  python3 /app/scripts/crat_integrity_check.py

# 2. Check chain head in Redis
kubectl exec -n multi-agent deploy/omni-analyst -- \
  redis-cli -u $OMNI_REDIS_URL llen audit_chain:blocks

# 3. Check last 3 blocks
kubectl exec -n multi-agent deploy/omni-analyst -- \
  redis-cli -u $OMNI_REDIS_URL lrange audit_chain:blocks -3 -1 | python3 -m json.tool
```

**Fix:**
- **If caused by Redis restart (data loss):** The chain is broken at a known restart point. Document the gap in the audit log manually.
- **If caused by clock skew:** Check node time sync (`timedatectl status`).
- **If suspected tampering:** STOP ALL OPERATIONS. Escalate to security team and compliance officer immediately (SOX §404 protocol).

**Do NOT attempt to repair the chain by rewriting blocks** — this is a security violation.

**Escalate:** CRAT chain break is a P0 security incident. Escalate to security team immediately.

---

## Scenario 4: HITL Approval Timeout (No Operator Response)

**Symptoms:** Actions stuck in `HITL_PENDING` for >30 minutes; `ESCALATE_TO_HUMAN` not resolved.

**Diagnose:**
```bash
# 1. Check HITL dispatcher logs
kubectl logs -n multi-agent deploy/omni-hitl-dispatcher --tail=100 | grep -E "HITL|timeout|approval"

# 2. Check pending actions in Redis
kubectl exec -n multi-agent deploy/omni-analyst -- \
  redis-cli -u $OMNI_REDIS_URL keys "omni:hitl:pending:*"

# 3. Check FinGuard HITL API health
kubectl get pods -n finguard-customer -l app=finguard-hitl-api
```

**Fix:**
```bash
# HITL dispatcher auto-rejects after HITL_APPROVAL_TIMEOUT_SEC (default: 1800s = 30min)
# If operator needs more time: extend timeout via env var and restart
kubectl set env deployment/omni-hitl-dispatcher HITL_APPROVAL_TIMEOUT_SEC=3600 -n multi-agent

# If HITL API is down: restart
kubectl rollout restart deployment/finguard-hitl-api -n finguard-customer
kubectl rollout status deployment/finguard-hitl-api -n finguard-customer --timeout=120s

# Manual reject to unblock (emergency only):
# kubectl exec -n multi-agent deploy/omni-hitl-dispatcher -- \
#   python3 -c "..." # see HITL emergency reject script
```

**Verify:** Check that analyst receives rejection feedback and re-evaluates.

---

## Scenario 5: Gateway Circuit Breaker Open

**Symptoms:** `CircuitBreakerOpen` alert; `omni_circuit_breaker_active == 1`; alerts being dropped.

**Diagnose:**
```bash
# 1. Check circuit breaker state
kubectl exec -n multi-agent deploy/omni-prober -- \
  redis-cli -u $OMNI_REDIS_URL get omni:circuit_breaker:active

# 2. Check what caused it (look in the last 30 mins)
kubectl logs -n multi-agent deploy/omni-gateway --tail=200 | grep "circuit_breaker"

# 3. Check gateway error rate
curl https://gateway.ai-agent.local/metrics | grep omni_gateway
```

**Fix:**
```bash
# Clear circuit breaker manually (only when root cause is resolved)
kubectl exec -n multi-agent deploy/omni-prober -- \
  redis-cli -u $OMNI_REDIS_URL del omni:circuit_breaker:active

# Restart gateway if it's in bad state
kubectl rollout restart deployment/omni-gateway -n multi-agent
```

**Verify:** `curl https://gateway.ai-agent.local/healthz` returns 200.

---

## Escalation Matrix

| Severity | Who | How | SLA |
|----------|-----|-----|-----|
| P0 (CRAT breach, data loss) | Security + Compliance | Phone + PagerDuty | 15 min |
| P1 (No advisories >15min) | On-call SRE | PagerDuty | 30 min |
| P2 (Consumer lag >1000) | SRE team | Slack #omni-alerts | 1 hour |
| P3 (Circuit breaker open) | SRE team | Slack #omni-alerts | 2 hours |
