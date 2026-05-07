#!/usr/bin/env bash
# Omni dev "death loop": build -> deploy worker+gateway -> [pytest_unit] -> product_e2e
#
# product_e2e (real cluster / runtime proof — not pytest):
#   1) gateway_alert_loki_verify.sh — STRICT_ASSERT default ON (product truth)
#   2) optional proactive_e2e.sh slice (default ON) — NS required by that script
#   3) optional e2e_incident_matrix.sh subset (default ON) — cluster-heavy wave_a1 scenarios;
#      if MVP_API healthz is reachable, also runs phase_b_api_resource,phase_b_api_state.
#      Scenario nginx_waiting_fault is intentionally NOT default (needs MVP + long waits); add via SCENARIOS=...
#
# pytest_unit is a fast, secondary gate — not sold as product validation. Skip with OMNI_DEATH_SKIP_PYTEST=1.
#
# Required env:
#   NS              Kubernetes namespace for E2E (e.g. multi-agent). No default.
#   KUBECONFIG      Optional; standard kubeconfig path (scripts/with_working_kube.sh / kubectl).
#
# Optional env:
#   OMNI_DEATH_SKIP_BUILD=1       Skip `make docker-worker docker-gateway`.
#   OMNI_DEATH_SKIP_PYTEST=1      Skip unit pytest; product_e2e still runs.
#   OMNI_DEATH_LOKI_STRICT=0|1    STRICT_ASSERT for gateway_alert_loki_verify.sh (default 1).
#   OMNI_DEATH_SLEEP_SEC=N        Passed to gateway_alert_loki_verify.sh (default 45; use 30 for faster debug).
#   OMNI_DEATH_PROACTIVE=0|1      After gateway smoke, run proactive_e2e (default 1). Uses --skip-build --skip-restart
#                                 because this script already deployed; set OMNI_DEATH_PROACTIVE_RESTART=1 to omit --skip-restart.
#   OMNI_DEATH_PROACTIVE_RESTART=1  Run proactive_e2e without --skip-restart (re-apply + rollout + metrics wait).
#   OMNI_DEATH_MATRIX=0|1         Run matrix subset (default 1). Set 0 to skip.
#   SCENARIOS                     If set, passed to e2e_incident_matrix.sh as-is. If unset and MATRIX=1, see header.
#   MVP_API_URL                  For matrix reachability probe (default http://localhost:8000).
#
# Exit codes (first failure wins):
#   2   Missing NS, or --help only (exit 0 for --help)
#   10  Docker / make build failed
#   11  make deploy-worker failed
#   12  make deploy-gateway failed
#   20  pytest (unit, non-integration) failed
#   30  scripts/gateway_alert_loki_verify.sh failed
#   31  scripts/e2e_incident_matrix.sh failed
#   32  scripts/proactive_e2e.sh failed
#
# Example:
#   NS=multi-agent bash scripts/omni_dev_death_loop.sh
#   OMNI_DEATH_SKIP_BUILD=1 OMNI_DEATH_SKIP_PYTEST=1 NS=multi-agent bash scripts/omni_dev_death_loop.sh
#   OMNI_DEATH_LOKI_STRICT=0 OMNI_DEATH_SLEEP_SEC=30 NS=multi-agent bash scripts/omni_dev_death_loop.sh
#   OMNI_DEATH_PROACTIVE=0 OMNI_DEATH_MATRIX=0 NS=multi-agent bash scripts/omni_dev_death_loop.sh
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

usage() {
  cat <<'USAGE_EOF'
Omni dev death loop — build, deploy, optional pytest_unit, product_e2e (gateway Loki, proactive, matrix).

Usage:
  NS=<ns> bash scripts/omni_dev_death_loop.sh

See script header comments for OMNI_DEATH_* env vars and exit codes.

Example:
  NS=multi-agent bash scripts/omni_dev_death_loop.sh
USAGE_EOF
}

phase_ok() { echo "[phase] OK: $*"; }

phase_fail() {
  local name="$1" code="$2" hint="$3"
  echo "[phase] FAIL: ${name} (exit ${code})" >&2
  echo "hint: ${hint}" >&2
  exit "${code}"
}

run_phase() {
  local name="$1" code="$2" hint="$3"
  shift 3
  echo "[phase] START: ${name}"
  if "$@"; then
    phase_ok "${name}"
  else
    phase_fail "${name}" "${code}" "${hint}"
  fi
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ -z "${NS:-}" ]]; then
  echo "omni_dev_death_loop.sh: NS is required (no default). Example: NS=multi-agent bash scripts/omni_dev_death_loop.sh" >&2
  exit 2
fi

if [[ "${OMNI_DEATH_SKIP_BUILD:-0}" == "1" ]]; then
  echo "[phase] SKIP: build (OMNI_DEATH_SKIP_BUILD=1)"
else
  run_phase "docker_build" 10 "fix Docker/build context; try: make docker-worker docker-gateway" \
    make docker-worker docker-gateway
fi

run_phase "deploy_worker" 11 "kubectl/worker manifests; try: make deploy-worker (with_working_kube.sh)" \
  make deploy-worker

run_phase "deploy_gateway" 12 "gateway rollout; try: make deploy-gateway after make docker-gateway" \
  make deploy-gateway

if [[ "${OMNI_DEATH_SKIP_PYTEST:-0}" == "1" ]]; then
  echo "[phase] SKIP: pytest_unit (OMNI_DEATH_SKIP_PYTEST=1) — not treating pytest as product proof"
else
  if [[ -x "${ROOT}/.venv/bin/python" ]]; then
    PY="${ROOT}/.venv/bin/python"
  else
    echo "[phase] WARN: ${ROOT}/.venv/bin/python missing; using python3 for pytest (create .venv for pinned deps)" >&2
    PY="python3"
  fi
  run_phase "pytest_unit" 20 "fix failing tests; run: .venv/bin/python -m pytest tests/ -q --ignore=tests/integration --tb=short" \
    "${PY}" -m pytest tests/ -q --ignore=tests/integration
fi

echo "[product_e2e] === cluster smoke + runtime assertions (pytest is not the product gate) ==="

LOKI_STRICT="${OMNI_DEATH_LOKI_STRICT:-1}"
SLEEP_SEC="${OMNI_DEATH_SLEEP_SEC:-45}"
export NS SLEEP_SEC
export STRICT_ASSERT="${LOKI_STRICT}"

run_phase "product_e2e_gateway_loki" 30 "cluster/Loki/gateway; try: NS=${NS} STRICT_ASSERT=${LOKI_STRICT} SLEEP_SEC=${SLEEP_SEC} bash scripts/gateway_alert_loki_verify.sh" \
  bash "${ROOT}/scripts/gateway_alert_loki_verify.sh"

if [[ "${OMNI_DEATH_PROACTIVE:-1}" == "1" ]]; then
  _pro_args=(--skip-build)
  if [[ "${OMNI_DEATH_PROACTIVE_RESTART:-0}" != "1" ]]; then
    _pro_args+=(--skip-restart)
  fi
  run_phase "product_e2e_proactive" 32 "proactive stream / full_system_audit; try: NS=${NS} bash scripts/proactive_e2e.sh ${_pro_args[*]}" \
    env NS="${NS}" bash "${ROOT}/scripts/proactive_e2e.sh" "${_pro_args[@]}"
else
  echo "[phase] SKIP: product_e2e_proactive (OMNI_DEATH_PROACTIVE=0)"
fi

if [[ "${OMNI_DEATH_MATRIX:-1}" == "1" ]]; then
  if [[ -z "${SCENARIOS:-}" ]]; then
    _matrix_base="wave_a1_rbac_manifest,wave_a1_rbac_permissions"
    MVP_PROBE_URL="${MVP_API_URL:-http://localhost:8000}"
    if curl -sf --max-time 3 "${MVP_PROBE_URL}/healthz" >/dev/null 2>&1; then
      export SCENARIOS="${_matrix_base},phase_b_api_resource,phase_b_api_state"
      echo "[product_e2e] MVP_API reachable at ${MVP_PROBE_URL} — matrix includes phase_b_api_resource,phase_b_api_state"
    else
      export SCENARIOS="${_matrix_base}"
      echo "[product_e2e] SKIP: MVP_API not reachable at ${MVP_PROBE_URL}/healthz — matrix uses wave_a1 only (no API scenarios). Export MVP_API_URL or start MVP to widen."
    fi
  fi
  run_phase "product_e2e_incident_matrix" 31 "matrix subset failed; try: NS=${NS} SCENARIOS=${SCENARIOS:-} bash scripts/e2e_incident_matrix.sh" \
    bash "${ROOT}/scripts/e2e_incident_matrix.sh"
else
  echo "[phase] SKIP: product_e2e_incident_matrix (OMNI_DEATH_MATRIX=0)"
fi

echo "[phase] ALL OK — death loop pass (product_e2e + optional pytest_unit)"
exit 0
