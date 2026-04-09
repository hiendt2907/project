#!/usr/bin/env bash
# E2E incident matrix (real faults, not only CPU/RAM/DISK).
# Scenarios:
#   - nginx_waiting_fault: induce CreateContainerConfigError on nginx-test, then verify gateway trace path.
#   - redis_probe_fault: reuse redis_exporter_probe_lab.sh (break readiness probe -> verify -> restore).
#   - nginx_cpu_overlap: optional CPU overlap scenario for comparison.
#   - gateway_payload: synthetic Alertmanager payloads from config (Prometheus label contract).
#
# Usage:
#   bash scripts/e2e_incident_matrix.sh
# Env:
#   NS=multi-agent
#   SCENARIOS=nginx_waiting_fault,redis_probe_fault,nginx_cpu_overlap
#   MATRIX_PATHS=path:path  (merged scenario lists; default = training matrix + prometheus_firing_simulation)
#   SLEEP_SEC=35                    # raise to 120–200 if STRICT_ASSERT + gateway E2E needs analyst/Ollama logs (planner slow)
#   STRICT_ASSERT=1
#   E2E_ASSERT_DIAGNOSTIC_POLICY=1  # nginx_waiting_fault defaults to 1: require INV_*/DIAGNOSTIC_INVARIANT_GATE markers in worker logs
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KUBE="${ROOT}/scripts/with_working_kube.sh"
if [[ -x "${ROOT}/.venv/bin/python" ]]; then
  PYTHON_BIN="${PYTHON_BIN:-${ROOT}/.venv/bin/python}"
else
  PYTHON_BIN="${PYTHON_BIN:-python3}"
fi
NS="${NS:-multi-agent}"
SCENARIOS="${SCENARIOS:-}"
SLEEP_SEC="${SLEEP_SEC:-35}"
STRICT_ASSERT="${STRICT_ASSERT:-1}"
REPORT_JSON="${REPORT_JSON:-${ROOT}/reports/incident-matrix/latest.json}"
MATRIX_PATHS="${MATRIX_PATHS:-${ROOT}/config/incident_training_matrix.yaml:${ROOT}/config/prometheus_firing_simulation.yaml}"
export MATRIX_PATHS

NGINX_CFG_CM="${NGINX_CFG_CM:-non-existent-config}"
NGINX_CM_CREATED=0
REPORT_ENTRIES="$(mktemp)"
START_TS="$(date +%s)"
FAIL_COUNT=0
TOTAL_COUNT=0

_log() { echo "[incident-matrix] $*"; }

_append_report_entry() {
  local scenario="$1"
  local status="$2"
  local duration_sec="$3"
  local trace_id="$4"
  local note="$5"
  "${PYTHON_BIN}" - "$REPORT_ENTRIES" "$scenario" "$status" "$duration_sec" "$trace_id" "$note" <<'PY'
import json
import sys
from pathlib import Path

f = Path(sys.argv[1])
obj = {
    "scenario": sys.argv[2],
    "status": sys.argv[3],
    "duration_sec": int(sys.argv[4]),
    "trace_id": sys.argv[5],
    "note": sys.argv[6],
}
with f.open("a", encoding="utf-8") as fp:
    fp.write(json.dumps(obj, ensure_ascii=False) + "\n")
PY
}

_matrix_runner_for() {
  local sid="$1"
  MATRIX_PATHS="${MATRIX_PATHS}" "${PYTHON_BIN}" "${ROOT}/scripts/matrix_lookup.py" runner "${sid}"
}

_scenario_exists_in_matrix() {
  local sid="$1"
  local out
  out="$(_matrix_runner_for "$sid")"
  [[ -n "${out}" ]]
}

_matrix_all_scenarios_csv() {
  MATRIX_PATHS="${MATRIX_PATHS}" "${PYTHON_BIN}" "${ROOT}/scripts/matrix_lookup.py" all-ids
}

_extract_trace_id() {
  local log_file="$1"
  "${PYTHON_BIN}" - "$log_file" <<'PY'
import re
import sys
from pathlib import Path

p = Path(sys.argv[1])
if not p.exists():
    print("")
    raise SystemExit(0)
last = ""
for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
    m = re.search(r"trace_id=([A-Za-z0-9._:-]+)", ln)
    if m:
        last = m.group(1)
print(last)
PY
}

_run_scenario() {
  local scenario="$1"
  local fn="$2"
  local t0 t1 rc duration trace tmp
  t0="$(date +%s)"
  tmp="$(mktemp)"
  _log "scenario=${scenario} start"
  set +e
  "${fn}" > >(tee "${tmp}") 2>&1
  rc=$?
  set -e
  t1="$(date +%s)"
  duration=$((t1 - t0))
  trace="$(_extract_trace_id "${tmp}")"
  TOTAL_COUNT=$((TOTAL_COUNT + 1))
  if [[ "${rc}" -eq 0 ]]; then
    _append_report_entry "${scenario}" "passed" "${duration}" "${trace}" ""
    _log "scenario=${scenario} done"
  else
    FAIL_COUNT=$((FAIL_COUNT + 1))
    _append_report_entry "${scenario}" "failed" "${duration}" "${trace}" "exit_code=${rc}"
    _log "scenario=${scenario} failed rc=${rc}"
  fi
  rm -f "${tmp}"
}

_ensure_cm() {
  if "${KUBE}" get configmap "${NGINX_CFG_CM}" -n "${NS}" >/dev/null 2>&1; then
    return 0
  fi
  _log "create configmap/${NGINX_CFG_CM} for nginx-test recovery"
  "${KUBE}" create configmap "${NGINX_CFG_CM}" -n "${NS}" --from-literal=DUMMY=1 >/dev/null
  NGINX_CM_CREATED=1
}

_delete_cm_for_fault() {
  if "${KUBE}" get configmap "${NGINX_CFG_CM}" -n "${NS}" >/dev/null 2>&1; then
    _log "delete configmap/${NGINX_CFG_CM} to induce CreateContainerConfigError"
    "${KUBE}" delete configmap "${NGINX_CFG_CM}" -n "${NS}" >/dev/null
  fi
}

_restore_nginx() {
  _ensure_cm
  _log "rollout restart deployment/nginx-test for recovery"
  "${KUBE}" rollout restart deployment/nginx-test -n "${NS}" >/dev/null
  "${KUBE}" rollout status deployment/nginx-test -n "${NS}" --timeout=180s >/dev/null
}

_induce_nginx_waiting_fault() {
  _log "apply nginx-test deployment/service"
  "${KUBE}" apply -f "${ROOT}/scripts/nginx-test-deployment.yaml" >/dev/null
  _ensure_cm
  _delete_cm_for_fault
  _log "rollout restart deployment/nginx-test to create waiting fault pod"
  "${KUBE}" rollout restart deployment/nginx-test -n "${NS}" >/dev/null
  sleep 8
  "${KUBE}" get pods -n "${NS}" -l app=nginx-test -o wide || true
}

_run_nginx_waiting_fault() {
  _induce_nginx_waiting_fault
  local payload="${ROOT}/scripts/alert_payloads/alertmanager_nginx_waiting_fault.json"
  local rc=0
  # Quality gate: not trace-only — require diagnostic policy / invariant markers (see gateway_alert_loki_verify.sh 3c).
  NS="${NS}" STRICT_ASSERT="${STRICT_ASSERT}" SLEEP_SEC="${SLEEP_SEC}" \
    E2E_ASSERT_DIAGNOSTIC_POLICY="${E2E_ASSERT_DIAGNOSTIC_POLICY:-1}" \
    bash "${ROOT}/scripts/gateway_alert_loki_verify.sh" "${payload}" || rc=$?
  _restore_nginx
  return "${rc}"
}

_run_redis_probe_fault() {
  KUBE_NS="${NS}" STRICT_ASSERT="${STRICT_ASSERT}" SLEEP_SEC="${SLEEP_SEC}" \
    bash "${ROOT}/scripts/redis_exporter_probe_lab.sh"
}

_run_nginx_cpu_overlap() {
  NS="${NS}" STRICT_ASSERT="${STRICT_ASSERT}" SLEEP_SEC=45 STRESS_OVERLAP_ALERT=1 WARMUP_SEC=15 OVERLAP_STRESS_SEC=120 WAIT_PROM_CPU=1 \
    bash "${ROOT}/scripts/nginx_test_cpu_alert_lab.sh"
}

_run_gateway_payload_scenario() {
  local sid="${1:-${CURRENT_SCENARIO:-}}"
  if [[ -z "${sid}" ]]; then
    echo "missing scenario id for gateway payload runner" >&2
    return 2
  fi
  local payload
  payload="$(mktemp)"
  MATRIX_PATHS="${MATRIX_PATHS}" NS="${NS}" \
    "${PYTHON_BIN}" "${ROOT}/scripts/incident_matrix_payload_from_config.py" \
    --scenario-id "${sid}" \
    --namespace "${NS}" \
    --out "${payload}" >/dev/null
  local rc=0
  NS="${NS}" STRICT_ASSERT="${STRICT_ASSERT}" SLEEP_SEC="${SLEEP_SEC}" \
    bash "${ROOT}/scripts/gateway_alert_loki_verify.sh" "${payload}" || rc=$?
  rm -f "${payload}"
  return "${rc}"
}

_dispatch_scenario() {
  local sc="$1"
  case "${sc}" in
    nginx_waiting_fault) _run_scenario "${sc}" _run_nginx_waiting_fault ;;
    redis_probe_fault) _run_scenario "${sc}" _run_redis_probe_fault ;;
    nginx_cpu_overlap) _run_scenario "${sc}" _run_nginx_cpu_overlap ;;
    *)
      local r
      r="$(_matrix_runner_for "${sc}")"
      if [[ "${r}" == "gateway_payload" ]]; then
        CURRENT_SCENARIO="${sc}" _run_scenario "${sc}" _run_gateway_payload_scenario
      else
        echo "Unknown scenario or runner: ${sc} (runner=${r})" >&2
        TOTAL_COUNT=$((TOTAL_COUNT + 1))
        FAIL_COUNT=$((FAIL_COUNT + 1))
        _append_report_entry "${sc}" "failed" "0" "" "unknown_scenario"
      fi
      ;;
  esac
}

_emit_report() {
  local end_ts report_dir meta_git meta_cfg
  end_ts="$(date +%s)"
  report_dir="$(dirname "${REPORT_JSON}")"
  mkdir -p "${report_dir}"
  meta_git="$(cd "${ROOT}" && git rev-parse HEAD 2>/dev/null || echo "")"
  meta_cfg=""
  if [[ -f "${ROOT}/config/incident_training_matrix.yaml" ]]; then
    if command -v sha256sum >/dev/null 2>&1; then
      meta_cfg="$(sha256sum "${ROOT}/config/incident_training_matrix.yaml" 2>/dev/null | awk '{print $1}')"
    elif command -v shasum >/dev/null 2>&1; then
      meta_cfg="$(shasum -a 256 "${ROOT}/config/incident_training_matrix.yaml" 2>/dev/null | awk '{print $1}')"
    fi
  fi
  "${PYTHON_BIN}" - "$REPORT_ENTRIES" "$REPORT_JSON" "$START_TS" "$end_ts" "$meta_git" "$meta_cfg" "$MATRIX_PATHS" <<'PY'
import json
import sys
from pathlib import Path

entries_file = Path(sys.argv[1])
out_file = Path(sys.argv[2])
start_ts = int(sys.argv[3])
end_ts = int(sys.argv[4])
git_sha = sys.argv[5] if len(sys.argv) > 5 else ""
config_sha = sys.argv[6] if len(sys.argv) > 6 else ""
matrix_paths = sys.argv[7] if len(sys.argv) > 7 else ""

entries = []
if entries_file.exists():
    for ln in entries_file.read_text(encoding="utf-8", errors="replace").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        entries.append(json.loads(ln))

passed = sum(1 for e in entries if e.get("status") == "passed")
failed = sum(1 for e in entries if e.get("status") != "passed")
report = {
    "schema_version": "1",
    "started_at": start_ts,
    "ended_at": end_ts,
    "duration_sec": max(0, end_ts - start_ts),
    "git_sha": git_sha,
    "config_sha256_primary_matrix": config_sha,
    "matrix_paths": matrix_paths,
    "summary": {
        "total": len(entries),
        "passed": passed,
        "failed": failed,
        "ok": failed == 0,
    },
    "scenarios": entries,
}
out_file.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(str(out_file))
PY
}

_cleanup() {
  # Keep environment safe for next runs.
  _ensure_cm || true
  if [[ "${NGINX_CM_CREATED}" == "1" ]]; then
    _log "cleanup: configmap/${NGINX_CFG_CM} was created by script (kept in place for stable lab)"
  fi
}
trap _cleanup EXIT

IFS=',' read -r -a arr <<<"${SCENARIOS}"
if [[ "${#arr[@]}" -eq 0 || -z "${arr[0]:-}" ]]; then
  SCENARIOS="$(_matrix_all_scenarios_csv)"
  IFS=',' read -r -a arr <<<"${SCENARIOS}"
fi
for raw in "${arr[@]}"; do
  sc="$(echo "${raw}" | tr -d '[:space:]')"
  if [[ -n "${sc}" ]] && ! _scenario_exists_in_matrix "${sc}"; then
    echo "Unknown scenario in matrix: ${sc}" >&2
    TOTAL_COUNT=$((TOTAL_COUNT + 1))
    FAIL_COUNT=$((FAIL_COUNT + 1))
    _append_report_entry "${sc}" "failed" "0" "" "unknown_scenario_in_matrix"
    continue
  fi
  if [[ -z "${sc}" ]]; then
    continue
  fi
  _dispatch_scenario "${sc}"
done

report_path="$(_emit_report)"
_log "report=${report_path}"
if [[ "${FAIL_COUNT}" -gt 0 ]]; then
  _log "completed with failures: ${FAIL_COUNT}/${TOTAL_COUNT}"
  exit 1
fi
_log "all selected scenarios passed (${TOTAL_COUNT})"
