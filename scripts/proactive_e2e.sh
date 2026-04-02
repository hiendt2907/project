#!/usr/bin/env bash
# Build image, apply worker manifests, restart, chạy full_system_audit (luồng proactive).
# Yêu cầu: cluster kubectl (OrbStack), namespace multi-agent, Redis/Gateway/worker đã có.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
KUBE="${ROOT}/scripts/with_working_kube.sh"
PY="${ROOT}/.venv/bin/python"
DURATION_SEC="${DURATION_SEC:-90}"
INTERVAL_SEC="${INTERVAL_SEC:-10}"

usage() {
  cat <<'EOF'
Usage: scripts/proactive_e2e.sh [options]

Environment:
  DURATION_SEC   Audit simulation window (default 90)
  INTERVAL_SEC   Seconds between gateway + XADD ticks (default 10)

Steps:
  1. docker build -t multi-agent-system:latest
  2. kubectl apply omni-worker ConfigMap / RBAC / Deployment
  3. kubectl rollout restart deployment/omni-worker -n multi-agent
  4. python scripts/full_system_audit.py --strict --min-action-experience 0

Options:
  --skip-build     Bỏ bước docker build
  --skip-restart   Bỏ apply + rollout restart (chỉ audit)
  -h, --help
EOF
}

SKIP_BUILD=0
SKIP_RESTART=0
while (($#)); do
  case "$1" in
    --skip-build) SKIP_BUILD=1 ;;
    --skip-restart) SKIP_RESTART=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown: $1" >&2; usage; exit 1 ;;
  esac
  shift
done

if [[ "${SKIP_BUILD}" -eq 0 ]]; then
  echo "[proactive_e2e] docker build ..."
  docker build -t multi-agent-system:latest -f "${ROOT}/Dockerfile" "${ROOT}"
fi

if [[ "${SKIP_RESTART}" -eq 0 ]]; then
  echo "[proactive_e2e] kubectl apply + rollout (legacy omni-worker and/or Master Plan V3 split) ..."
  "${KUBE}" apply -f "${ROOT}/k8s/deployments/omni-worker-configmap.yaml"
  "${KUBE}" apply -f "${ROOT}/k8s/deployments/omni-worker-rbac.yaml"
  "${KUBE}" apply -f "${ROOT}/k8s/deployments/prober-rbac.yaml"
  "${KUBE}" apply -f "${ROOT}/k8s/deployments/analyst-rbac.yaml"
  "${KUBE}" apply -f "${ROOT}/k8s/deployments/omni-worker.yaml"
  "${KUBE}" apply -f "${ROOT}/k8s/deployments/omni-prober.yaml"
  "${KUBE}" apply -f "${ROOT}/k8s/deployments/omni-analyst.yaml"
  "${KUBE}" apply -f "${ROOT}/k8s/deployments/omni-core.yaml"
  "${KUBE}" rollout restart deployment/omni-worker deployment/omni-prober deployment/omni-analyst deployment/omni-core -n multi-agent
  "${KUBE}" rollout status deployment/omni-prober -n multi-agent --timeout=180s || true
  "${KUBE}" rollout status deployment/omni-analyst -n multi-agent --timeout=180s || true
  "${KUBE}" rollout status deployment/omni-core -n multi-agent --timeout=180s || true
  "${KUBE}" rollout status deployment/omni-worker -n multi-agent --timeout=60s || true
  METRICS_DEPLOY="omni-prober"
  if replicas="$("${KUBE}" get deploy omni-worker -n multi-agent -o jsonpath='{.spec.replicas}' 2>/dev/null)" && [[ "${replicas:-0}" != "0" ]]; then
    METRICS_DEPLOY="omni-worker"
  fi
  echo "[proactive_e2e] waiting for metrics on deploy/${METRICS_DEPLOY} (:9090) ..."
  for _ in $(seq 1 30); do
    if code="$("${KUBE}" exec -n multi-agent "deploy/${METRICS_DEPLOY}" -- sh -lc \
      'curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:9090/metrics' 2>/dev/null)" && [[ "${code}" == "200" ]]; then
      echo "[proactive_e2e] metrics OK (${METRICS_DEPLOY})"
      break
    fi
    sleep 2
  done
fi

echo "[proactive_e2e] full_system_audit (${DURATION_SEC}s, interval ${INTERVAL_SEC}s) ..."
exec "${PY}" "${ROOT}/scripts/full_system_audit.py" \
  --duration-sec "${DURATION_SEC}" \
  --interval-sec "${INTERVAL_SEC}" \
  --strict \
  --min-action-experience 0
