#!/usr/bin/env bash
# Run one phase of omni_dev_death_loop.sh in isolation, with optional dependency wait.
# Used when orchestrating parallel workers (each waits on marker files).
#
# Usage:
#   RUN_ID=myrun NS=multi-agent bash scripts/omni_death_loop_single_phase.sh <phase>
#
# phase: docker_build | deploy_worker | deploy_gateway | pytest_unit |
#        gateway_loki | proactive | incident_matrix
#
# Env:
#   RUN_ID          Required — unique id for marker directory (e.g. $$ or uuid).
#   NS              Required for cluster phases.
#   MARKER_DIR      Optional; default reports/death-loop-runs/${RUN_ID}
#   OMNI_DEATH_SKIP_BUILD, OMNI_DEATH_SKIP_PYTEST, OMNI_DEATH_LOKI_STRICT,
#   OMNI_DEATH_SLEEP_SEC, OMNI_DEATH_PROACTIVE, OMNI_DEATH_PROACTIVE_RESTART,
#   OMNI_DEATH_MATRIX, SCENARIOS, OMNI_GATEWAY_URL — same as omni_dev_death_loop.sh
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PHASE="${1:?usage: $0 <phase>}"
RUN_ID="${RUN_ID:?set RUN_ID (e.g. RUN_ID=$$)}"
MARKER_DIR="${MARKER_DIR:-${ROOT}/reports/death-loop-runs/${RUN_ID}}"
mkdir -p "${MARKER_DIR}"

wait_marker() {
  local name="$1" max="${2:-120}"
  local i=0
  while [[ ! -f "${MARKER_DIR}/${name}.ok" ]]; do
    i=$((i + 1))
    if [[ "${i}" -gt "${max}" ]]; then
      echo "timeout waiting for ${name}.ok in ${MARKER_DIR}" >&2
      exit 98
    fi
    sleep 5
  done
}

touch_ok() { touch "${MARKER_DIR}/${1}.ok"; }

skip_build() { [[ "${OMNI_DEATH_SKIP_BUILD:-0}" == "1" ]]; }

echo "[single-phase] RUN_ID=${RUN_ID} PHASE=${PHASE} MARKER_DIR=${MARKER_DIR}"

case "${PHASE}" in
  docker_build)
    if skip_build; then
      echo "[single-phase] SKIP docker_build (OMNI_DEATH_SKIP_BUILD=1)"
      touch_ok docker_build
      exit 0
    fi
    make docker-worker docker-gateway
    touch_ok docker_build
    ;;

  deploy_worker)
    if ! skip_build; then wait_marker docker_build 144; fi
    make deploy-worker
    touch_ok deploy_worker
    ;;

  deploy_gateway)
    wait_marker deploy_worker 144
    make deploy-gateway
    touch_ok deploy_gateway
    ;;

  pytest_unit)
    wait_marker deploy_gateway 144
    if [[ "${OMNI_DEATH_SKIP_PYTEST:-0}" == "1" ]]; then
      echo "[single-phase] SKIP pytest_unit"
      touch_ok pytest_unit
      exit 0
    fi
    if [[ -x "${ROOT}/.venv/bin/python" ]]; then PY="${ROOT}/.venv/bin/python"; else PY="python3"; fi
    "${PY}" -m pytest tests/ -q --ignore=tests/integration
    touch_ok pytest_unit
    ;;

  gateway_loki)
    wait_marker deploy_gateway 144
    if [[ -z "${NS:-}" ]]; then echo "NS required" >&2; exit 2; fi
    LOKI_STRICT="${OMNI_DEATH_LOKI_STRICT:-1}"
    SLEEP_SEC="${OMNI_DEATH_SLEEP_SEC:-45}"
    export NS SLEEP_SEC STRICT_ASSERT="${LOKI_STRICT}"
    bash "${ROOT}/scripts/gateway_alert_loki_verify.sh"
    touch_ok gateway_loki
    ;;

  proactive)
    wait_marker gateway_loki 200
    if [[ -z "${NS:-}" ]]; then echo "NS required" >&2; exit 2; fi
    if [[ "${OMNI_DEATH_PROACTIVE:-1}" != "1" ]]; then
      echo "[single-phase] SKIP proactive (OMNI_DEATH_PROACTIVE=0)"
      touch_ok proactive
      exit 0
    fi
    _pro_args=(--skip-build)
    if [[ "${OMNI_DEATH_PROACTIVE_RESTART:-0}" != "1" ]]; then
      _pro_args+=(--skip-restart)
    fi
    env NS="${NS}" bash "${ROOT}/scripts/proactive_e2e.sh" "${_pro_args[@]}"
    touch_ok proactive
    ;;

  incident_matrix)
    wait_marker proactive 200
    if [[ -z "${NS:-}" ]]; then echo "NS required" >&2; exit 2; fi
    if [[ "${OMNI_DEATH_MATRIX:-1}" != "1" ]]; then
      echo "[single-phase] SKIP incident_matrix (OMNI_DEATH_MATRIX=0)"
      touch_ok incident_matrix
      exit 0
    fi
    if [[ -z "${SCENARIOS:-}" ]]; then
      export SCENARIOS="wave_a1_rbac_manifest,wave_a1_rbac_permissions,phase_b_pytest,phase_b_unit_full,nginx_waiting_fault"
    fi
    bash "${ROOT}/scripts/e2e_incident_matrix.sh"
    touch_ok incident_matrix
    ;;

  *)
    echo "unknown phase: ${PHASE}" >&2
    exit 99
    ;;
esac

echo "[single-phase] OK ${PHASE}"
