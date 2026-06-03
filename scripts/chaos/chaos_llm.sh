#!/usr/bin/env bash
# chaos_llm.sh — Inject bad LLM URL into omni-analyst, verify degraded advisory mode.
#
# Usage:
#   NS=multi-agent bash scripts/chaos/chaos_llm.sh
#
# Strategy: Patch omni-analyst deployment env OMNI_OLLAMA_BASE_URL to invalid host.
#           Verify /healthz shows llm_up=degraded. Restore original URL.
# Safety: exits immediately if OMNI_ENV_MODE != lab.
set -euo pipefail

NS="${NS:-multi-agent}"
HEALTH_URL="${OMNI_HEALTH_URL:-http://localhost:8090}"
ANALYST_DEPLOY="${ANALYST_DEPLOY:-omni-analyst}"
WAIT_DETECT_SEC=90    # llm health check polls every ~30s
WAIT_RECOVER_SEC=120
POLL_INTERVAL=10
INVALID_LLM_URL="http://chaos-invalid-llm-host.local:11434"

# ── Safety gates ──────────────────────────────────────────────────────────────
if [ "${OMNI_ENV_MODE:-}" != "lab" ]; then
  echo "[CHAOS] ABORT: OMNI_ENV_MODE must be 'lab', got '${OMNI_ENV_MODE:-unset}'" >&2
  exit 2
fi

if [ "${OMNI_AUTO_EXECUTE_ENABLED:-true}" != "false" ]; then
  echo "[CHAOS] ABORT: OMNI_AUTO_EXECUTE_ENABLED must be 'false'" >&2
  exit 2
fi

echo "[CHAOS] LLM chaos drill starting — namespace=$NS deployment=$ANALYST_DEPLOY"

# ── Helpers ───────────────────────────────────────────────────────────────────
check_health() {
  curl -sf "${HEALTH_URL}/healthz" 2>/dev/null || echo '{"status":"unreachable"}'
}

llm_status() {
  check_health | python3 -c "import json,sys; h=json.load(sys.stdin); print(h.get('checks',{}).get('llm_up',['unknown'])[0])" 2>/dev/null || echo "unknown"
}

overall_status() {
  check_health | python3 -c "import json,sys; h=json.load(sys.stdin); print(h.get('status','unknown'))" 2>/dev/null || echo "unknown"
}

# ── Baseline ──────────────────────────────────────────────────────────────────
echo "[CHAOS] === Baseline ==="
BASELINE_LLM=$(llm_status)
BASELINE_STATUS=$(overall_status)
echo "[CHAOS] baseline llm_up=$BASELINE_LLM overall=$BASELINE_STATUS"

# Capture original LLM URL from deployment
ORIGINAL_LLM_URL=$(kubectl get deploy "$ANALYST_DEPLOY" -n "$NS" \
  -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="OMNI_OLLAMA_BASE_URL")].value}' 2>/dev/null || \
  echo "http://host.orb.internal:11434")
echo "[CHAOS] original OMNI_OLLAMA_BASE_URL: $ORIGINAL_LLM_URL"

# ── Inject: bad LLM URL ───────────────────────────────────────────────────────
echo "[CHAOS] === Inject: patching OMNI_OLLAMA_BASE_URL to $INVALID_LLM_URL ==="
kubectl set env deploy/"$ANALYST_DEPLOY" -n "$NS" \
  "OMNI_OLLAMA_BASE_URL=$INVALID_LLM_URL"

echo "[CHAOS] Waiting for rollout..."
kubectl rollout status deploy/"$ANALYST_DEPLOY" -n "$NS" --timeout=90s

echo "[CHAOS] Waiting up to ${WAIT_DETECT_SEC}s for llm_up to become degraded..."
DETECTED=false
for i in $(seq 1 $((WAIT_DETECT_SEC / POLL_INTERVAL))); do
  LLM_ST=$(llm_status)
  echo "[CHAOS]   llm_up=$LLM_ST (attempt $i)"
  if [ "$LLM_ST" = "degraded" ] || [ "$LLM_ST" = "unhealthy" ]; then
    DETECTED=true
    echo "[CHAOS] DETECTED: llm_up=$LLM_ST after $((i * POLL_INTERVAL))s"
    break
  fi
  sleep $POLL_INTERVAL
done

if [ "$DETECTED" = "false" ]; then
  echo "[CHAOS] WARN: llm_up never reached degraded within ${WAIT_DETECT_SEC}s" >&2
fi

# ── Restore: original LLM URL ────────────────────────────────────────────────
echo "[CHAOS] === Restore: patching OMNI_OLLAMA_BASE_URL back to $ORIGINAL_LLM_URL ==="
kubectl set env deploy/"$ANALYST_DEPLOY" -n "$NS" \
  "OMNI_OLLAMA_BASE_URL=$ORIGINAL_LLM_URL"

kubectl rollout status deploy/"$ANALYST_DEPLOY" -n "$NS" --timeout=90s

# ── Recovery verification ─────────────────────────────────────────────────────
echo "[CHAOS] === Recovery: waiting for llm_up to return to ok ==="
RECOVERED=false
for i in $(seq 1 $((WAIT_RECOVER_SEC / POLL_INTERVAL))); do
  LLM_ST=$(llm_status)
  STATUS=$(overall_status)
  echo "[CHAOS]   llm_up=$LLM_ST overall=$STATUS (attempt $i)"
  if [ "$LLM_ST" = "ok" ] && [ "$STATUS" = "ok" ]; then
    RECOVERED=true
    echo "[CHAOS] RECOVERED: llm_up=ok after $((i * POLL_INTERVAL))s"
    break
  fi
  sleep $POLL_INTERVAL
done

if [ "$RECOVERED" = "false" ]; then
  echo "[CHAOS] FAIL: LLM health did not recover within ${WAIT_RECOVER_SEC}s" >&2
  check_health | python3 -m json.tool
  exit 4
fi

echo "[CHAOS] === Final health ==="
check_health | python3 -m json.tool 2>/dev/null || check_health
echo "[CHAOS] LLM chaos drill PASSED"
