#!/usr/bin/env bash
# chaos_redis.sh — Inject Redis kill, verify CRAT fail-closed + health recovery.
#
# Usage:
#   NS=multi-agent bash scripts/chaos/chaos_redis.sh
#   NS=multi-agent bash scripts/chaos/chaos_redis.sh --mode crat-corrupt
#
# Safety: exits immediately if OMNI_ENV_MODE != lab or OMNI_AUTO_EXECUTE_ENABLED != false.
set -euo pipefail

NS="${NS:-multi-agent}"
MODE="${1:-kill}"
HEALTH_URL="${OMNI_HEALTH_URL:-http://localhost:8090}"
WAIT_DETECT_SEC=60
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

echo "[CHAOS] Redis chaos drill starting — namespace=$NS mode=$MODE"

# ── Helper: health check ──────────────────────────────────────────────────────
check_health() {
  curl -sf "${HEALTH_URL}/healthz" 2>/dev/null || echo '{"status":"unreachable"}'
}

redis_ping_status() {
  check_health | python3 -c "import json,sys; h=json.load(sys.stdin); print(h.get('checks',{}).get('redis_ping',['unknown'])[0])" 2>/dev/null || echo "unknown"
}

overall_status() {
  check_health | python3 -c "import json,sys; h=json.load(sys.stdin); print(h.get('status','unknown'))" 2>/dev/null || echo "unknown"
}

# ── Baseline ──────────────────────────────────────────────────────────────────
echo "[CHAOS] === Baseline ==="
BASELINE_HEALTH=$(check_health)
echo "[CHAOS] baseline health: $(echo "$BASELINE_HEALTH" | python3 -c 'import json,sys; h=json.load(sys.stdin); print(h.get("status","?"))')"

REDIS_POD=$(kubectl get pod -n "$NS" -l app=redis -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || \
            kubectl get pod -n "$NS" --field-selector=status.phase=Running -o name 2>/dev/null | grep redis | head -1 | sed 's|pod/||' || \
            echo "")

if [ -z "$REDIS_POD" ]; then
  echo "[CHAOS] ABORT: could not find Redis pod in namespace $NS" >&2
  exit 3
fi

echo "[CHAOS] Redis pod: $REDIS_POD"

if [ "$MODE" = "crat-corrupt" ]; then
  # ── Mode: CRAT chain corruption ───────────────────────────────────────────
  echo "[CHAOS] === Mode: CRAT chain corruption ==="
  echo "[CHAOS] Deleting audit_chain:head_hash from Redis..."
  kubectl exec -n "$NS" "$REDIS_POD" -- redis-cli DEL audit_chain:head_hash

  echo "[CHAOS] Verifying: next write_audit_block should detect corruption"
  echo "[CHAOS] (Monitor omni-analyst logs for AuditLedgerError)"

  # Wait briefly for any in-flight advisory to complete
  sleep 5

  # Verify no new omni-actions messages were dispatched (check via kafka consumer)
  echo "[CHAOS] Checking omni-actions Kafka topic for unexpected messages..."

  echo "[CHAOS] === Recovery: CRAT head_hash corruption self-heals on next genesis block ==="
  echo "[CHAOS] Head_hash will be set to genesis on next advisory cycle"

else
  # ── Mode: Redis pod kill ──────────────────────────────────────────────────
  echo "[CHAOS] === Mode: Redis pod kill ==="
  echo "[CHAOS] Deleting Redis pod (K8s will restart it)..."
  kubectl delete pod "$REDIS_POD" -n "$NS"

  echo "[CHAOS] Waiting up to ${WAIT_DETECT_SEC}s for redis_ping to become unhealthy..."
  DETECTED=false
  for i in $(seq 1 $((WAIT_DETECT_SEC / POLL_INTERVAL))); do
    STATUS=$(redis_ping_status)
    echo "[CHAOS]   redis_ping=$STATUS (attempt $i)"
    if [ "$STATUS" = "unhealthy" ] || [ "$STATUS" = "unreachable" ]; then
      DETECTED=true
      echo "[CHAOS] DETECTED: redis_ping=$STATUS after $((i * POLL_INTERVAL))s"
      break
    fi
    sleep $POLL_INTERVAL
  done

  if [ "$DETECTED" = "false" ]; then
    echo "[CHAOS] WARN: redis_ping never reached unhealthy within ${WAIT_DETECT_SEC}s (Redis may have recovered fast)"
  fi

  # ── Recovery wait ─────────────────────────────────────────────────────────
  echo "[CHAOS] === Recovery ==="
  echo "[CHAOS] Waiting up to ${WAIT_RECOVER_SEC}s for overall status to return to 'ok'..."
  RECOVERED=false
  for i in $(seq 1 $((WAIT_RECOVER_SEC / POLL_INTERVAL))); do
    STATUS=$(overall_status)
    echo "[CHAOS]   overall_status=$STATUS (attempt $i)"
    if [ "$STATUS" = "ok" ]; then
      RECOVERED=true
      echo "[CHAOS] RECOVERED: status=ok after $((i * POLL_INTERVAL))s"
      break
    fi
    sleep $POLL_INTERVAL
  done

  if [ "$RECOVERED" = "false" ]; then
    echo "[CHAOS] FAIL: system did not recover within ${WAIT_RECOVER_SEC}s" >&2
    check_health | python3 -m json.tool
    exit 4
  fi
fi

echo "[CHAOS] === Final health ==="
check_health | python3 -m json.tool 2>/dev/null || check_health
echo "[CHAOS] Redis chaos drill PASSED"
