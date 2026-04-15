#!/usr/bin/env bash
# Lab: nginx-test volume mount ConfigMap không tồn tại → FailedMount trong events → POST waiting-fault alert.
#
# Bắt buộc E2E_NGINX_POD_AUTO=1 (gateway patch labels.pod từ pod thật). Không dùng E2E_NGINX_POD_AUTO=0
# trừ khi file JSON đã có đúng tên pod (placeholder nginx-test-DYNAMIC sẽ làm probe 404).
#
# Usage: bash scripts/lab_nginx_test_missing_configmap_e2e.sh
# Env:
#   NS=multi-agent
#   SLEEP_SEC — first wait after POST (default 180). Prober/evidence thường <60s; analyst PLAN_EMITTED nhanh.
#   E2E_EXTRA_AGENTIC_SLEEP — second wait (default 240). Cần cho vòng agentic/Ollama nhiều bước sau PLAN_EMITTED
#     (đơn giản chỉ thấy readonly_discovery_redirect step=1 nếu dừng quá sớm).
#   STRICT_ASSERT=0|1
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KUBE="${ROOT}/scripts/with_working_kube.sh"
NS="${NS:-multi-agent}"
SLEEP_SEC="${SLEEP_SEC:-180}"
E2E_EXTRA_AGENTIC_SLEEP="${E2E_EXTRA_AGENTIC_SLEEP:-240}"
STRICT_ASSERT="${STRICT_ASSERT:-0}"
CM_NAME="${CM_NAME:-nginx-test-never-created-cm}"

log() { printf '[%s] %s\n' "$(date -Iseconds)" "$*"; }

log "=== A) Apply clean nginx-test + Service ==="
"$KUBE" apply -f "${ROOT}/scripts/nginx-test-deployment.yaml"
"$KUBE" rollout status "deployment/nginx-test" -n "$NS" --timeout=180s

log "=== B) Patch Deployment: mount non-existent ConfigMap ==="
"$KUBE" patch deployment nginx-test -n "$NS" --type=json -p "[
  {\"op\":\"add\",\"path\":\"/spec/template/spec/volumes\",\"value\":[{\"name\":\"broken-cfg\",\"configMap\":{\"name\":\"${CM_NAME}\"}}]},
  {\"op\":\"add\",\"path\":\"/spec/template/spec/containers/0/volumeMounts\",\"value\":[{\"name\":\"broken-cfg\",\"mountPath\":\"/tmp/broken-cm-ro\"}]}
]" || {
  log "WARN: json patch failed — deployment may already have volumes/volumeMounts"
  exit 1
}

log "=== C) Single replica stuck pod: scale to 0 then 1 (tránh 2 pod RollingUpdate + pod label sai) ==="
"$KUBE" scale deployment nginx-test -n "$NS" --replicas=0
"$KUBE" wait --for=delete pod -l app=nginx-test -n "$NS" --timeout=120s 2>/dev/null || true
"$KUBE" scale deployment nginx-test -n "$NS" --replicas=1
sleep 5
POD="$("$KUBE" get pods -n "$NS" -l app=nginx-test -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")"
log "pod=${POD:-?}"
"$KUBE" describe pod "$POD" -n "$NS" 2>/dev/null | grep -E 'FailedMount|configmap' | head -5 || true

log "=== D) POST gateway (alertmanager_nginx_waiting_fault.json, E2E_NGINX_POD_AUTO=1) ==="
log "SLEEP_SEC=${SLEEP_SEC} E2E_EXTRA_AGENTIC_SLEEP=${E2E_EXTRA_AGENTIC_SLEEP} (agentic — cần đủ để không cắt ngang bước Ollama sau step=1)"
export SLEEP_SEC
export E2E_EXTRA_AGENTIC_SLEEP
export STRICT_ASSERT
export E2E_NGINX_POD_AUTO=1

# Capture exit code without tripping `set -e` so restore (below) always runs.
set +e
bash "${ROOT}/scripts/gateway_alert_loki_verify.sh" "${ROOT}/scripts/alert_payloads/alertmanager_nginx_waiting_fault.json"
GATE_RC=$?
set -e

log "=== E) Restore clean nginx-test ==="
"$KUBE" delete deployment nginx-test -n "$NS" --wait=true --ignore-not-found=true
"$KUBE" apply -f "${ROOT}/scripts/nginx-test-deployment.yaml"
"$KUBE" rollout status "deployment/nginx-test" -n "$NS" --timeout=180s
log "gateway_verify_exit_code=${GATE_RC}"
exit "$GATE_RC"
