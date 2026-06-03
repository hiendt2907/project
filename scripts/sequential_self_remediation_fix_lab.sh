#!/usr/bin/env bash
# Sequential inject (rbac → configmap → oom): prepare drift, inject, wait up to 10m each.
# Success = cluster state matches remediation (NOT raw exit_code=0 — rollout_restart also logs exit_code=0).
# Lab-only: reapplies ClusterRoleBinding drift, sets OMNI_GOD_MODE=true for configmap probe, enables OOM deterministic.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KUBE="${ROOT}/scripts/with_working_kube.sh"
NS="${NS:-multi-agent}"
EXEC_DEPLOY="${E2E_EXEC_DEPLOY:-omni-fullstack}"
WAIT_MAX="${WAIT_MAX_SEC:-600}"
POLL_SEC="${POLL_SEC:-5}"
# Must match preflight ConfigMap OMNI_OOM_PATCH_MEMORY
OOM_TARGET_MEM="${OOM_TARGET_MEM:-512Mi}"
OOM_DRIFT_MEM="${OOM_DRIFT_MEM:-64Mi}"

log() { printf '%s %s\n' "$(date -Iseconds)" "$*"; }

on_timeout_dump() {
  local trace="${1:-}"
  local step="${2:-}"
  log "Debug dump for ${step} trace=${trace:-?}"
  if [[ -n "${trace}" ]]; then
    log "Executor (trace):"
    "$KUBE" logs -n "$NS" deploy/omni-fullstack --since=45m --tail=12000 2>/dev/null | grep -F "$trace" | tail -40 || true
    log "Analyst (trace):"
    "$KUBE" logs -n "$NS" deploy/omni-fullstack --since=45m --tail=12000 2>/dev/null | grep -F "$trace" | tail -40 || true
  fi
}

# Wait until command succeeds (exit 0). Args: description, then command...
wait_until() {
  local desc="$1"
  shift
  local deadline=$(( $(date +%s) + WAIT_MAX ))
  log "wait (max ${WAIT_MAX}s): $desc"
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if "$@"; then
      log "OK: $desc"
      return 0
    fi
    sleep "$POLL_SEC"
  done
  log "TIMEOUT: $desc"
  return 1
}

check_rbac_binding_absent() {
  ! "$KUBE" get clusterrolebinding omni-worker-cluster-admin &>/dev/null
}

check_god_mode_false() {
  local v
  v="$("$KUBE" get cm omni-worker-config -n "$NS" -o jsonpath='{.data.OMNI_GOD_MODE}' 2>/dev/null || echo "?")"
  [[ "$v" == "false" ]]
}

check_oom_memory_patched() {
  local v
  v="$("$KUBE" get deploy nginx-load -n "$NS" -o jsonpath='{.spec.template.spec.containers[?(@.name=="load")].resources.limits.memory}' 2>/dev/null || echo "")"
  [[ "$v" == "$OOM_TARGET_MEM" ]]
}

ensure_nginx_load_deployment() {
  if "$KUBE" get deploy nginx-load -n "$NS" &>/dev/null; then
    log "Deployment nginx-load already present"
    return 0
  fi
  log "Creating lab Deployment nginx-load (container load, idle sleep)"
  "$KUBE" apply -f - <<'EOF'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: nginx-load
  namespace: multi-agent
  labels:
    app: nginx-load
spec:
  replicas: 1
  selector:
    matchLabels:
      app: nginx-load
  template:
    metadata:
      labels:
        app: nginx-load
    spec:
      containers:
        - name: load
          image: curlimages/curl:8.5.0
          command: ["/bin/sh", "-c", "sleep 3600"]
          resources:
            requests:
              cpu: "10m"
              memory: 64Mi
            limits:
              memory: 64Mi
              cpu: "100m"
EOF
  "$KUBE" rollout status "deployment/nginx-load" -n "$NS" --timeout=180s
  "$KUBE" wait --for=condition=Ready pod -n "$NS" -l app=nginx-load --timeout=180s
}

log "=== 0) Lab preflight: drift + flags ==="
# Re-apply consolidated worker RBAC (lab setup for RBAC probe)
"$KUBE" apply -f "${ROOT}/k8s/deployments/omni-fullstack-rbac.yaml" >/dev/null
log "Applied omni-fullstack-rbac.yaml (consolidated worker RBAC)"

# ConfigMap drift: god mode in prod + OOM deterministic flags (probe reads live ConfigMap via API)
"$KUBE" patch configmap omni-worker-config -n "$NS" --type merge -p "{\"data\":{\"OMNI_GOD_MODE\":\"true\",\"OMNI_ENV_MODE\":\"prod\",\"OMNI_OOM_DETERMINISTIC_REMEDIATE_ENABLED\":\"true\",\"OMNI_OOM_PATCH_CONTAINER\":\"load\",\"OMNI_OOM_PATCH_MEMORY\":\"${OOM_TARGET_MEM}\"}}" >/dev/null
log "Patched omni-worker-config: GOD_MODE=true, OOM deterministic on, container=load, target=${OOM_TARGET_MEM}"

# Analyst + executor reload env for OOM flags
for d in omni-fullstack; do
  if "$KUBE" get deploy "$d" -n "$NS" &>/dev/null; then
    "$KUBE" rollout restart "deployment/$d" -n "$NS" >/dev/null
    "$KUBE" rollout status "deployment/$d" -n "$NS" --timeout=180s
    log "Rollout done: $d"
  fi
done

run_inject() {
  local only="$1"
  shift || true
  "$KUBE" exec -n "$NS" "deploy/${EXEC_DEPLOY}" -- \
    python3 /app/scripts/inject_self_remediation_alerts.py --only "$only" "$@" 2>&1
}

extract_trace() {
  grep -oE 'trace_id=[^ ]+' | head -1 | sed 's/trace_id=//'
}

# --- RBAC ---
log "=== 1) INJECT rbac ==="
OUT1=$(run_inject rbac || true)
echo "$OUT1"
T1=$(echo "$OUT1" | extract_trace || true)
if [[ -z "${T1:-}" ]]; then log "FAIL: no trace rbac"; exit 2; fi
if ! wait_until "clusterrolebinding omni-worker-cluster-admin removed" check_rbac_binding_absent; then
  on_timeout_dump "$T1" rbac
  exit 3
fi

# --- ConfigMap (least-privilege Role from step 1 must allow cm patch) ---
log "=== 2) INJECT configmap ==="
OUT2=$(run_inject configmap || true)
echo "$OUT2"
T2=$(echo "$OUT2" | extract_trace || true)
if [[ -z "${T2:-}" ]]; then log "FAIL: no trace configmap"; exit 4; fi
if ! wait_until "ConfigMap OMNI_GOD_MODE=false" check_god_mode_false; then
  on_timeout_dump "$T2" configmap
  exit 5
fi

# --- OOM: need Deployment nginx-load + memory drift, then inject ---
log "=== 2b) Ensure nginx-load Deployment + drift memory before OOM inject ==="
ensure_nginx_load_deployment
log "Setting nginx-load/load limits to ${OOM_DRIFT_MEM} (drift; remediation targets ${OOM_TARGET_MEM})"
"$KUBE" set resources deployment nginx-load -n "$NS" -c=load --limits=memory="${OOM_DRIFT_MEM}" >/dev/null
"$KUBE" rollout status "deployment/nginx-load" -n "$NS" --timeout=180s

OOM_POD="$("$KUBE" get pods -n "$NS" -l app=nginx-load -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")"
if [[ -z "${OOM_POD}" ]]; then
  log "FAIL: no pod for app=nginx-load"
  exit 8
fi
log "OOM pod label for inject: ${OOM_POD}"

log "=== 3) INJECT oom ==="
OUT3=$(run_inject oom --oom-pod "$OOM_POD" || true)
echo "$OUT3"
T3=$(echo "$OUT3" | extract_trace || true)
if [[ -z "${T3:-}" ]]; then log "FAIL: no trace oom"; exit 6; fi
if ! wait_until "Deployment nginx-load container load memory=${OOM_TARGET_MEM}" check_oom_memory_patched; then
  on_timeout_dump "$T3" oom
  exit 7
fi

log "=== DONE: all three paths verified on cluster state ==="
log "Traces: rbac=$T1 configmap=$T2 oom=$T3"
log "Optional: re-apply k8s/deployments/omni-fullstack-rbac.yaml to restore worker RBAC."
