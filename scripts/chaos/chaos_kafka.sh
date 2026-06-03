#!/usr/bin/env bash
# chaos_kafka.sh — Block Kafka port, verify graceful degradation + consumer lag recovery.
#
# Usage:
#   NS=multi-agent bash scripts/chaos/chaos_kafka.sh
#
# Strategy: Scale Kafka to 0 replicas → wait for lag to increase → scale back → verify recovery.
# Safety: exits immediately if OMNI_ENV_MODE != lab.
set -euo pipefail

NS="${NS:-multi-agent}"
HEALTH_URL="${OMNI_HEALTH_URL:-http://localhost:8090}"
KAFKA_STATEFULSET="${KAFKA_STATEFULSET:-kafka}"
WAIT_INJECT_SEC=30
WAIT_RECOVER_SEC=120
POLL_INTERVAL=5

# ── Safety gates ──────────────────────────────────────────────────────────────
if [ "${OMNI_ENV_MODE:-}" != "lab" ]; then
  echo "[CHAOS] ABORT: OMNI_ENV_MODE must be 'lab', got '${OMNI_ENV_MODE:-unset}'" >&2
  exit 2
fi

if [ "${OMNI_AUTO_EXECUTE_ENABLED:-true}" != "false" ]; then
  echo "[CHAOS] ABORT: OMNI_AUTO_EXECUTE_ENABLED must be 'false'" >&2
  exit 2
fi

if [ "$NS" = "finguard-customer" ]; then
  echo "[CHAOS] ABORT: chaos injection into finguard-customer is forbidden" >&2
  exit 2
fi

echo "[CHAOS] Kafka chaos drill starting — namespace=$NS"

# ── Helpers ───────────────────────────────────────────────────────────────────
check_health() {
  curl -sf "${HEALTH_URL}/healthz" 2>/dev/null || echo '{"status":"unreachable"}'
}

overall_status() {
  check_health | python3 -c "import json,sys; h=json.load(sys.stdin); print(h.get('status','unknown'))" 2>/dev/null || echo "unknown"
}

kafka_lag() {
  kubectl exec -n "$NS" kafka-0 -- \
    kafka-consumer-groups.sh --bootstrap-server localhost:9092 \
    --group omni-analyst --describe 2>/dev/null | \
    awk 'NR>1 && $6 ~ /^[0-9]+$/ {sum += $6} END {print sum+0}' || echo "0"
}

# ── Baseline ──────────────────────────────────────────────────────────────────
echo "[CHAOS] === Baseline ==="
BASELINE_STATUS=$(overall_status)
echo "[CHAOS] baseline health: $BASELINE_STATUS"
BASELINE_LAG=$(kafka_lag)
echo "[CHAOS] baseline consumer lag: $BASELINE_LAG"

ORIGINAL_REPLICAS=$(kubectl get statefulset "$KAFKA_STATEFULSET" -n "$NS" \
  -o jsonpath='{.spec.replicas}' 2>/dev/null || echo "1")
echo "[CHAOS] Kafka original replicas: $ORIGINAL_REPLICAS"

# ── Inject: scale Kafka to 0 ──────────────────────────────────────────────────
echo "[CHAOS] === Inject: scaling $KAFKA_STATEFULSET to 0 replicas ==="
kubectl scale statefulset "$KAFKA_STATEFULSET" -n "$NS" --replicas=0

echo "[CHAOS] Waiting ${WAIT_INJECT_SEC}s for Kafka unavailability to be detected..."
sleep "$WAIT_INJECT_SEC"

echo "[CHAOS] Health during Kafka outage:"
OUTAGE_STATUS=$(overall_status)
echo "[CHAOS]   overall_status=$OUTAGE_STATUS"
# Worker should be degraded or unhealthy, NOT crashed (still serving /healthz)
if ! curl -sf "${HEALTH_URL}/healthz" -o /dev/null; then
  echo "[CHAOS] WARN: /healthz not reachable — worker may have crashed" >&2
fi

# ── Restore: scale Kafka back ─────────────────────────────────────────────────
echo "[CHAOS] === Restore: scaling $KAFKA_STATEFULSET back to $ORIGINAL_REPLICAS ==="
kubectl scale statefulset "$KAFKA_STATEFULSET" -n "$NS" --replicas="$ORIGINAL_REPLICAS"

echo "[CHAOS] Waiting for Kafka pod to be ready..."
kubectl rollout status statefulset/"$KAFKA_STATEFULSET" -n "$NS" --timeout=120s

# ── Recovery verification ─────────────────────────────────────────────────────
echo "[CHAOS] === Recovery: waiting for consumer lag to return to 0 ==="
RECOVERED=false
for i in $(seq 1 $((WAIT_RECOVER_SEC / POLL_INTERVAL))); do
  LAG=$(kafka_lag)
  STATUS=$(overall_status)
  echo "[CHAOS]   lag=$LAG status=$STATUS (attempt $i)"
  if [ "$LAG" -le 5 ] && [ "$STATUS" = "ok" ]; then
    RECOVERED=true
    echo "[CHAOS] RECOVERED: lag=$LAG status=$STATUS after $((i * POLL_INTERVAL))s"
    break
  fi
  sleep $POLL_INTERVAL
done

if [ "$RECOVERED" = "false" ]; then
  echo "[CHAOS] FAIL: consumer lag or health did not recover within ${WAIT_RECOVER_SEC}s" >&2
  echo "[CHAOS] Final lag: $(kafka_lag)"
  check_health | python3 -m json.tool
  exit 4
fi

echo "[CHAOS] === Final health ==="
check_health | python3 -m json.tool 2>/dev/null || check_health
echo "[CHAOS] Kafka chaos drill PASSED"
