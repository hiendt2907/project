#!/usr/bin/env bash
# E2E incident matrix (real faults, not only CPU/RAM/DISK).
# Scenarios:
#   - nginx_waiting_fault: induce CreateContainerConfigError on nginx-test, then verify gateway trace path.
#   - redis_probe_fault: reuse redis_exporter_probe_lab.sh (break readiness probe -> verify -> restore).
#   - nginx_cpu_overlap: optional CPU overlap scenario for comparison.
#
# Usage:
#   bash scripts/e2e_incident_matrix.sh
# Env:
#   NS=multi-agent
#   SCENARIOS=nginx_waiting_fault,redis_probe_fault,nginx_cpu_overlap
#   SLEEP_SEC=35
#   STRICT_ASSERT=1
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KUBE="${ROOT}/scripts/with_working_kube.sh"
NS="${NS:-multi-agent}"
SCENARIOS="${SCENARIOS:-nginx_waiting_fault,redis_probe_fault}"
SLEEP_SEC="${SLEEP_SEC:-35}"
STRICT_ASSERT="${STRICT_ASSERT:-1}"
REPORT_JSON="${REPORT_JSON:-${ROOT}/reports/incident-matrix/latest.json}"

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
  python3 - "$REPORT_ENTRIES" "$scenario" "$status" "$duration_sec" "$trace_id" "$note" <<'PY'
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

_extract_trace_id() {
  local log_file="$1"
  python3 - "$log_file" <<'PY'
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
  NS="${NS}" STRICT_ASSERT="${STRICT_ASSERT}" SLEEP_SEC="${SLEEP_SEC}" \
    bash "${ROOT}/scripts/gateway_alert_loki_verify.sh" "${payload}"
  _restore_nginx
}

_run_redis_probe_fault() {
  KUBE_NS="${NS}" STRICT_ASSERT="${STRICT_ASSERT}" SLEEP_SEC="${SLEEP_SEC}" \
    bash "${ROOT}/scripts/redis_exporter_probe_lab.sh"
}

_run_nginx_cpu_overlap() {
  NS="${NS}" STRICT_ASSERT="${STRICT_ASSERT}" SLEEP_SEC=45 STRESS_OVERLAP_ALERT=1 WARMUP_SEC=15 OVERLAP_STRESS_SEC=120 WAIT_PROM_CPU=1 \
    bash "${ROOT}/scripts/nginx_test_cpu_alert_lab.sh"
}

_emit_report() {
  local end_ts report_dir
  end_ts="$(date +%s)"
  report_dir="$(dirname "${REPORT_JSON}")"
  mkdir -p "${report_dir}"
  python3 - "$REPORT_ENTRIES" "$REPORT_JSON" "$START_TS" "$end_ts" <<'PY'
import json
import sys
from pathlib import Path

entries_file = Path(sys.argv[1])
out_file = Path(sys.argv[2])
start_ts = int(sys.argv[3])
end_ts = int(sys.argv[4])

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
    "started_at": start_ts,
    "ended_at": end_ts,
    "duration_sec": max(0, end_ts - start_ts),
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
for raw in "${arr[@]}"; do
  sc="$(echo "${raw}" | tr -d '[:space:]')"
  case "${sc}" in
    nginx_waiting_fault) _run_scenario "${sc}" _run_nginx_waiting_fault ;;
    redis_probe_fault) _run_scenario "${sc}" _run_redis_probe_fault ;;
    nginx_cpu_overlap) _run_scenario "${sc}" _run_nginx_cpu_overlap ;;
    "") ;;
    *)
      echo "Unknown scenario: ${sc}" >&2
      TOTAL_COUNT=$((TOTAL_COUNT + 1))
      FAIL_COUNT=$((FAIL_COUNT + 1))
      _append_report_entry "${sc}" "failed" "0" "" "unknown_scenario"
      ;;
  esac
done

report_path="$(_emit_report)"
_log "report=${report_path}"
if [[ "${FAIL_COUNT}" -gt 0 ]]; then
  _log "completed with failures: ${FAIL_COUNT}/${TOTAL_COUNT}"
  exit 1
fi
_log "all selected scenarios passed (${TOTAL_COUNT})"
