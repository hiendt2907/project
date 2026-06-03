#!/usr/bin/env bash
# deploy_and_test_advisory.sh — Phase 5 Advisory Mode: Deploy + E2E Smoke Test
#
# What this script does (fully automated, no manual steps):
#   1. Applies the advisory-mode ConfigMap (OMNI_AUTO_EXECUTE_ENABLED=false, OMNI_SIEM_SUGGEST_ONLY=true)
#   2. Rollout-restarts all workers that consume omni-worker-config
#   3. Waits for all rollouts to stabilise
#   4. Injects a realistic "Disk Space Exhaustion" critical SIEM incident into
#      the finguard-customer Redis stream:actionable_incidents
#   5. Tails siem-bridge + omni-analyst logs so advisory report generation is
#      visible in real-time (Ctrl-C to stop tailing)
#
# Prerequisites:
#   - kubectl context pointing to OrbStack cluster (orbstack or docker-desktop)
#   - RBAC access to namespaces: multi-agent, finguard-customer
#   - Secret finguard-redis-bridge-secret present in multi-agent namespace
#
# Usage:
#   bash scripts/deploy_and_test_advisory.sh
#   SKIP_ROLLOUT=1 bash scripts/deploy_and_test_advisory.sh   # skip apply+restart, only inject
#   SKIP_INJECT=1  bash scripts/deploy_and_test_advisory.sh   # apply+restart only, skip injection

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NS_OMNI="multi-agent"
NS_SIEM="finguard-customer"
REDIS_POD="redis-0"
SIEM_STREAM="stream:actionable_incidents"
SKIP_ROLLOUT="${SKIP_ROLLOUT:-0}"
SKIP_INJECT="${SKIP_INJECT:-0}"
ROLLOUT_TIMEOUT="${ROLLOUT_TIMEOUT:-120s}"

_log()  { echo "[advisory-e2e] $*"; }
_pass() { echo "[advisory-e2e] PASS: $*"; }
_fail() { echo "[advisory-e2e] FAIL: $*" >&2; }
_die()  { _fail "$*"; exit 1; }

# ---------------------------------------------------------------------------
# Prereqs
# ---------------------------------------------------------------------------

_check_prereqs() {
  command -v kubectl >/dev/null 2>&1 || _die "kubectl not found in PATH"
  kubectl cluster-info --request-timeout=5s >/dev/null 2>&1 \
    || _die "No cluster reachable. Check kubectl context."
  _pass "prereqs: kubectl + cluster reachable"
}

# ---------------------------------------------------------------------------
# Step 1 & 2: Apply ConfigMap + rollout-restart affected workers
# ---------------------------------------------------------------------------

_apply_configmap_and_restart() {
  _log "Applying advisory-mode ConfigMap..."
  kubectl apply -f "${ROOT}/k8s/deployments/omni-worker-configmap.yaml" -n "${NS_OMNI}"
  _pass "ConfigMap omni-worker-config applied"

  # Verify the kill-switch values are live in the cluster
  local auto_exec
  auto_exec="$(kubectl get configmap omni-worker-config -n "${NS_OMNI}" \
    -o jsonpath='{.data.OMNI_AUTO_EXECUTE_ENABLED}')"
  if [[ "${auto_exec}" == "false" ]]; then
    _pass "ConfigMap guard: OMNI_AUTO_EXECUTE_ENABLED=false (Advisory Mode enforced)"
  else
    _die "ConfigMap guard failed: OMNI_AUTO_EXECUTE_ENABLED=${auto_exec} — expected 'false'"
  fi

  local siem_only
  siem_only="$(kubectl get configmap omni-worker-config -n "${NS_OMNI}" \
    -o jsonpath='{.data.OMNI_SIEM_SUGGEST_ONLY}')"
  if [[ "${siem_only}" == "true" ]]; then
    _pass "ConfigMap guard: OMNI_SIEM_SUGGEST_ONLY=true (SIEM suggestions only)"
  else
    _die "ConfigMap guard failed: OMNI_SIEM_SUGGEST_ONLY=${siem_only} — expected 'true'"
  fi

  # Rollout-restart all workers that consume this ConfigMap
  local deployments=(omni-analyst omni-core omni-executor omni-prober)
  _log "Rolling out restarts: ${deployments[*]}"
  for dep in "${deployments[@]}"; do
    if kubectl get deployment "${dep}" -n "${NS_OMNI}" >/dev/null 2>&1; then
      kubectl rollout restart "deployment/${dep}" -n "${NS_OMNI}"
      _log "  restarted: ${dep}"
    else
      _log "  skip: ${dep} not found (may be scaled-down)"
    fi
  done
}

# ---------------------------------------------------------------------------
# Step 3: Wait for rollouts
# ---------------------------------------------------------------------------

_wait_rollouts() {
  local deployments=(omni-analyst omni-core omni-executor omni-prober)
  _log "Waiting for rollouts (timeout=${ROLLOUT_TIMEOUT} each)..."
  local rc=0
  for dep in "${deployments[@]}"; do
    if kubectl get deployment "${dep}" -n "${NS_OMNI}" >/dev/null 2>&1; then
      if kubectl rollout status "deployment/${dep}" -n "${NS_OMNI}" \
           --timeout="${ROLLOUT_TIMEOUT}" >/dev/null 2>&1; then
        _pass "rollout: ${dep} stable"
      else
        _fail "rollout: ${dep} did not stabilise within ${ROLLOUT_TIMEOUT}"
        rc=1
      fi
    fi
  done
  return "${rc}"
}

# ---------------------------------------------------------------------------
# Step 4: Inject fake "Disk Space Exhaustion" critical SIEM incident
# ---------------------------------------------------------------------------

_get_redis_password() {
  kubectl get secret finguard-redis-bridge-secret -n "${NS_OMNI}" \
    -o jsonpath='{.data.password}' 2>/dev/null \
    | base64 --decode 2>/dev/null \
    || echo ""
}

_inject_disk_exhaustion_incident() {
  _log "Fetching Redis password from secret finguard-redis-bridge-secret..."
  local redis_pw
  redis_pw="$(_get_redis_password)"

  if [[ -z "${redis_pw}" ]]; then
    _fail "Could not retrieve Redis password — skipping injection"
    _fail "  Ensure: kubectl create secret generic finguard-redis-bridge-secret"
    _fail "          --from-literal=password=<pw> -n ${NS_OMNI}"
    return 1
  fi
  _pass "Redis password retrieved"

  # Verify the Redis pod is reachable
  if ! kubectl get pod "${REDIS_POD}" -n "${NS_SIEM}" >/dev/null 2>&1; then
    _die "Redis pod ${REDIS_POD} not found in namespace ${NS_SIEM}"
  fi

  local incident_id="inc-disk-$(date +%s)"
  local ts
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

  _log "Injecting Disk Space Exhaustion incident (id=${incident_id}) into ${SIEM_STREAM}..."

  # RED TEAM NOTE: Each XADD field-value pair is a separate argv element after `--`.
  # Bash double-quotes expand variables but keep the value as one word. redis-cli
  # receives them as distinct positional args — no shell within the container is
  # invoked, so no secondary escaping is needed.
  local msg_id
  msg_id="$(kubectl exec -n "${NS_SIEM}" "${REDIS_POD}" -- \
    redis-cli -a "${redis_pw}" --no-auth-warning \
    XADD "${SIEM_STREAM}" '*' \
      id "${incident_id}" \
      severity "critical" \
      category "disk_exhaustion" \
      tenant_id "finguard-corp" \
      description "CRITICAL: Disk space exhaustion detected on /var/data partition. Used: 98%. Node: prod-node-3. All write operations will fail if not remediated within 15 minutes." \
      suggested_action "Scale PVC capacity or archive and purge log files immediately" \
      affected_ip "10.0.1.45" \
      source "smart-siem" \
      timestamp "${ts}" \
      hitl_required "false" \
  )"

  if [[ -n "${msg_id}" ]]; then
    _pass "Incident injected — Redis stream msg_id: ${msg_id}"
    _pass "  incident_id  : ${incident_id}"
    _pass "  category     : disk_exhaustion"
    _pass "  severity     : critical"
    _pass "  timestamp    : ${ts}"
  else
    _die "XADD returned empty — check Redis connectivity and password"
  fi

  # Quick sanity: stream should have at least 1 pending entry
  local stream_len
  stream_len="$(kubectl exec -n "${NS_SIEM}" "${REDIS_POD}" -- \
    redis-cli -a "${redis_pw}" --no-auth-warning \
    XLEN "${SIEM_STREAM}" 2>/dev/null || echo "0")"
  _log "Stream length after injection: ${stream_len}"
}

# ---------------------------------------------------------------------------
# Step 5: Tail logs in real-time
# ---------------------------------------------------------------------------

_tail_advisory_logs() {
  _log "───────────────────────────────────────────────────────────────"
  _log "Tailing live logs — press Ctrl-C to stop"
  _log "  siem-bridge  : translates FinGuard stream → Kafka omni-alerts"
  _log "  omni-analyst : diagnoses, runs kill-switch, emits suggestion"
  _log "───────────────────────────────────────────────────────────────"
  echo ""

  # Clean up background log tail jobs on exit
  _cleanup_jobs() {
    local job
    for job in "${LOG_PIDS[@]+"${LOG_PIDS[@]}"}"; do
      kill "${job}" 2>/dev/null || true
    done
  }
  LOG_PIDS=()
  trap _cleanup_jobs EXIT

  # siem-bridge: watch for translate/publish events
  kubectl logs -f -n "${NS_OMNI}" \
    -l app=omni-siem-bridge \
    --container siem-bridge \
    --tail=0 \
    --prefix 2>/dev/null &
  LOG_PIDS+=($!)

  # omni-analyst: watch for SUGGEST_REMEDIATION, kill_switch_blocked, advisory events
  kubectl logs -f -n "${NS_OMNI}" \
    -l app=omni-fullstack \
    --tail=0 \
    --prefix 2>/dev/null &
  LOG_PIDS+=($!)

  # Wait for Ctrl-C
  wait "${LOG_PIDS[@]}" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

main() {
  _log "Phase 5 Advisory Mode — Deploy + E2E Smoke Test"
  _log "Namespace (Omni)  : ${NS_OMNI}"
  _log "Namespace (SIEM)  : ${NS_SIEM}"
  _log "Redis pod         : ${REDIS_POD}"
  _log "SKIP_ROLLOUT      : ${SKIP_ROLLOUT}"
  _log "SKIP_INJECT       : ${SKIP_INJECT}"
  echo ""

  _check_prereqs

  if [[ "${SKIP_ROLLOUT}" != "1" ]]; then
    _apply_configmap_and_restart
    _wait_rollouts
    echo ""
    _log "Pausing 5s for pods to begin log streaming before injection..."
    sleep 5
  else
    _log "SKIP_ROLLOUT=1 — skipping ConfigMap apply and rollout restart"
  fi

  if [[ "${SKIP_INJECT}" != "1" ]]; then
    _inject_disk_exhaustion_incident
    echo ""
    _log "Incident injected. Waiting 3s before tailing logs..."
    sleep 3
  else
    _log "SKIP_INJECT=1 — skipping incident injection"
  fi

  _tail_advisory_logs
}

main "$@"
