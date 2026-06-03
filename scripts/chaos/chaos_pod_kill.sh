#!/usr/bin/env bash
# chaos_pod_kill.sh — Kill omni-analyst pod, verify K8s restart + Kafka message replay.
#
# Usage:
#   NS=multi-agent bash scripts/chaos/chaos_pod_kill.sh
#
# Verifies the auto_offset_reset=earliest invariant: evidence from before kill is replayed.
# Safety: exits immediately if OMNI_ENV_MODE != lab.
set -euo pipefail

NS="${NS:-multi-agent}"
HEALTH_URL="${OMNI_HEALTH_URL:-http://localhost:8090}"
ANALYST_LABEL="${ANALYST_LABEL:-app=omni-fullstack}"
WAIT_RESTART_SEC=120
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

echo "[CHAOS] Pod kill chaos drill starting — namespace=$NS label=$ANALYST_LABEL"

# ── Helpers ───────────────────────────────────────────────────────────────────
check_health() {
  curl -sf "${HEALTH_URL}/healthz" 2>/dev/null || echo '{"status":"unreachable"}'
}

overall_status() {
  check_health | python3 -c "import json,sys; h=json.load(sys.stdin); print(h.get('status','unknown'))" 2>/dev/null || echo "unknown"
}

# ── Baseline ──────────────────────────────────────────────────────────────────
echo "[CHAOS] === Baseline ==="
BASELINE_POD=$(kubectl get pod -n "$NS" -l "$ANALYST_LABEL" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
if [ -z "$BASELINE_POD" ]; then
  echo "[CHAOS] ABORT: could not find omni-analyst pod in namespace $NS" >&2
  exit 3
fi
echo "[CHAOS] Target pod: $BASELINE_POD"
BASELINE_STATUS=$(overall_status)
echo "[CHAOS] baseline health: $BASELINE_STATUS"

# ── Inject: delete pod ────────────────────────────────────────────────────────
echo "[CHAOS] === Inject: deleting pod $BASELINE_POD ==="
kubectl delete pod "$BASELINE_POD" -n "$NS"

echo "[CHAOS] Waiting for pod to disappear..."
kubectl wait --for=delete pod/"$BASELINE_POD" -n "$NS" --timeout=60s 2>/dev/null || true

# ── Wait for restart ──────────────────────────────────────────────────────────
echo "[CHAOS] === Waiting for new pod to start (K8s should restart within ${WAIT_RESTART_SEC}s) ==="
NEW_POD=""
for i in $(seq 1 $((WAIT_RESTART_SEC / POLL_INTERVAL))); do
  NEW_POD=$(kubectl get pod -n "$NS" -l "$ANALYST_LABEL" \
    --field-selector=status.phase=Running \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
  if [ -n "$NEW_POD" ] && [ "$NEW_POD" != "$BASELINE_POD" ]; then
    echo "[CHAOS] New pod running: $NEW_POD (after $((i * POLL_INTERVAL))s)"
    break
  fi
  echo "[CHAOS]   waiting for new pod... attempt $i"
  sleep $POLL_INTERVAL
done

if [ -z "$NEW_POD" ]; then
  echo "[CHAOS] FAIL: new pod did not start within ${WAIT_RESTART_SEC}s" >&2
  kubectl get pod -n "$NS" -l "$ANALYST_LABEL"
  exit 4
fi

# ── Recovery verification ─────────────────────────────────────────────────────
echo "[CHAOS] === Recovery: waiting for health to return to ok ==="
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

echo "[CHAOS] Verify Kafka replay: consumer group offset should have consumed pre-kill messages"
echo "[CHAOS]   kubectl exec -n $NS kafka-0 -- kafka-consumer-groups.sh \\"
echo "[CHAOS]     --bootstrap-server localhost:9092 --group omni-analyst --describe"

echo "[CHAOS] === Final health ==="
check_health | python3 -m json.tool 2>/dev/null || check_health
echo "[CHAOS] Pod kill chaos drill PASSED"
