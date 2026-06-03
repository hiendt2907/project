#!/usr/bin/env bash
# Build image, apply worker manifests, restart, chạy full_system_audit (luồng proactive).
# Yêu cầu: cluster kubectl (OrbStack), NS set tới namespace Omni (lab: multi-agent), Redis/Gateway/worker đã có.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
KUBE="${ROOT}/scripts/with_working_kube.sh"
PY="${ROOT}/.venv/bin/python"
if [[ -z "${NS:-}" ]]; then
  echo "proactive_e2e.sh: set NS to the Kubernetes namespace (no default)." >&2
  exit 2
fi
DURATION_SEC="${DURATION_SEC:-90}"
INTERVAL_SEC="${INTERVAL_SEC:-10}"
STRICT_ROLLOUT="${STRICT_ROLLOUT:-1}"

usage() {
  cat <<'EOF'
Usage: scripts/proactive_e2e.sh [options]

Environment:
  NS             **required** — Kubernetes namespace for rollouts (lab: multi-agent)
  DURATION_SEC   Audit simulation window (default 90)
  INTERVAL_SEC   Seconds between gateway + XADD ticks (default 10)
  E2E_INJECT_PROACTIVE  Set to 1 to pass --inject-proactive to full_system_audit (Kafka proactive inject)

Steps:
  1. docker build worker + gateway (`multi-agent-system:latest`, `omni-gateway:latest`)
  2. kubectl apply omni-worker ConfigMap / RBAC / Deployment
  3. kubectl rollout restart … -n "${NS}"
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
  echo "[proactive_e2e] docker build (worker + gateway) ..."
  docker build -t multi-agent-system:latest -f "${ROOT}/Dockerfile" "${ROOT}"
  docker build -t omni-gateway:latest -f "${ROOT}/Dockerfile.gateway" "${ROOT}"
fi

if [[ "${SKIP_RESTART}" -eq 0 ]]; then
  echo "[proactive_e2e] kubectl apply + rollout (omni-fullstack consolidated) ..."
  "${KUBE}" apply -f "${ROOT}/k8s/deployments/omni-worker-configmap.yaml"
  "${KUBE}" apply -f "${ROOT}/k8s/deployments/omni-fullstack-rbac.yaml"
  "${KUBE}" apply -f "${ROOT}/k8s/deployments/omni-fullstack.yaml"
  "${KUBE}" apply -f "${ROOT}/k8s/deployments/omni-gateway.yaml"
  "${KUBE}" rollout restart deployment/omni-fullstack deployment/omni-gateway -n "${NS}"
  if [[ "${STRICT_ROLLOUT}" == "1" ]]; then
    "${KUBE}" rollout status deployment/omni-fullstack -n "${NS}" --timeout=300s
    "${KUBE}" rollout status deployment/omni-gateway -n "${NS}" --timeout=300s
  else
    "${KUBE}" rollout status deployment/omni-fullstack -n "${NS}" --timeout=300s || true
    "${KUBE}" rollout status deployment/omni-gateway -n "${NS}" --timeout=300s || true
  fi
  METRICS_DEPLOY="omni-fullstack"
  echo "[proactive_e2e] waiting for metrics on deploy/${METRICS_DEPLOY} (:9090) ..."
  for _ in $(seq 1 30); do
    if code="$("${KUBE}" exec -n "${NS}" "deploy/${METRICS_DEPLOY}" -- sh -lc \
      'curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:9090/metrics' 2>/dev/null)" && [[ "${code}" == "200" ]]; then
      echo "[proactive_e2e] metrics OK (${METRICS_DEPLOY})"
      break
    fi
    sleep 2
  done
fi

echo "[proactive_e2e] full_system_audit (${DURATION_SEC}s, interval ${INTERVAL_SEC}s) ..."
if [[ "${E2E_INJECT_PROACTIVE:-0}" == "1" ]]; then
  exec "${PY}" "${ROOT}/scripts/full_system_audit.py" \
    --duration-sec "${DURATION_SEC}" \
    --interval-sec "${INTERVAL_SEC}" \
    --strict \
    --min-action-experience 0 \
    --inject-proactive
else
  exec "${PY}" "${ROOT}/scripts/full_system_audit.py" \
    --duration-sec "${DURATION_SEC}" \
    --interval-sec "${INTERVAL_SEC}" \
    --strict \
    --min-action-experience 0 \
    --sigma-min-hits "${SIGMA_MIN_HITS:-0}"
fi
