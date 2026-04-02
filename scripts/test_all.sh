#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KUBE_WRAPPER="${ROOT_DIR}/scripts/with_working_kube.sh"
PYTHON_BIN="${PYTHON_BIN:-${ROOT_DIR}/.venv/bin/python}"
PYTEST_BIN="${PYTEST_BIN:-${ROOT_DIR}/.venv/bin/pytest}"

RUN_UNIT=1
RUN_K8S=1
RUN_AUDIT=1
AUDIT_STRICT=1
AUDIT_DURATION_SEC="${AUDIT_DURATION_SEC:-60}"
AUDIT_INTERVAL_SEC="${AUDIT_INTERVAL_SEC:-10}"

usage() {
  cat <<'EOF'
Usage: scripts/test_all.sh [options]

Options:
  --skip-unit            Skip pytest logic/business tests
  --skip-k8s             Skip Kubernetes infrastructure checks
  --skip-audit           Skip live dataflow/system audit
  --no-audit-strict      Do not fail script when full_system_audit reports failed checks
  --audit-duration SEC   Override audit duration (default: 60)
  --audit-interval SEC   Override audit interval (default: 10)
  -h, --help             Show help

Examples:
  scripts/test_all.sh
  scripts/test_all.sh --skip-audit
  scripts/test_all.sh --audit-duration 120 --audit-interval 15
EOF
}

log() {
  printf '\n[%s] %s\n' "$(date '+%H:%M:%S')" "$*"
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

while (($#)); do
  case "$1" in
    --skip-unit) RUN_UNIT=0 ;;
    --skip-k8s) RUN_K8S=0 ;;
    --skip-audit) RUN_AUDIT=0 ;;
    --no-audit-strict) AUDIT_STRICT=0 ;;
    --audit-duration)
      AUDIT_DURATION_SEC="${2:-}"
      shift
      ;;
    --audit-interval)
      AUDIT_INTERVAL_SEC="${2:-}"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 1
      ;;
  esac
  shift
done

if [[ "${RUN_UNIT}" -eq 1 && ! -x "${PYTEST_BIN}" ]]; then
  require_cmd pytest
  PYTEST_BIN="$(command -v pytest)"
fi
if [[ "${RUN_K8S}" -eq 1 || "${RUN_AUDIT}" -eq 1 ]]; then
  require_cmd kubectl
fi
if [[ "${RUN_AUDIT}" -eq 1 ]]; then
  require_cmd docker
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
  PYTHON_BIN="python3"
fi

cd "${ROOT_DIR}"
log "Starting full verification suite"

if [[ "${RUN_UNIT}" -eq 1 ]]; then
  log "Layer 1/4 - Logic & business tests (pytest; gồm proactive SLO/grounding)"
  "${PYTEST_BIN}" tests/ -q
fi

if [[ "${RUN_K8S}" -eq 1 ]]; then
  log "Layer 2/4 - Infrastructure checks (Kubernetes API + workloads)"
  "${ROOT_DIR}/scripts/check_kube.sh"
  "${KUBE_WRAPPER}" get ns multi-agent monitor >/dev/null
  "${KUBE_WRAPPER}" get deploy -n multi-agent
  "${KUBE_WRAPPER}" get pods -n multi-agent
fi

if [[ "${RUN_AUDIT}" -eq 1 ]]; then
  log "Layer 3/4 - Dataflow & system audit (live proactive/gateway paths)"
  AUDIT_ARGS=(
    "${ROOT_DIR}/scripts/full_system_audit.py"
    "--duration-sec" "${AUDIT_DURATION_SEC}"
    "--interval-sec" "${AUDIT_INTERVAL_SEC}"
  )
  if [[ "${AUDIT_STRICT}" -eq 1 ]]; then
    AUDIT_ARGS+=("--strict")
  fi
  "${PYTHON_BIN}" "${AUDIT_ARGS[@]}"
fi

log "Layer 4/4 - Deployment manifest sanity"
for mf in "${ROOT_DIR}"/k8s/deployments/*.yaml; do
  base="$(basename "${mf}")"
  if [[ "${base}" == "omni-prom-rules.yaml" ]]; then
    log "Skipping ${base} (requires PrometheusRule CRD)"
    continue
  fi
  "${KUBE_WRAPPER}" apply --dry-run=client -f "${mf}"
done
"${KUBE_WRAPPER}" apply --dry-run=client -f "${ROOT_DIR}/k8s/monitor/"

log "All requested checks completed successfully."
