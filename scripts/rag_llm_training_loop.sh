#!/usr/bin/env bash
# Lặp "full test" (pytest + toàn bộ incident matrix) nhiều lần để tích lũy luồng
# gateway → worker → Ollama (embed/chat) → ghi RAG / experience trong cluster.
#
# Usage:
#   ITERATIONS=8 NS=multi-agent STRICT_ASSERT=0 SLEEP_SEC=5 bash scripts/rag_llm_training_loop.sh
# Env:
#   ITERATIONS=5..10   (default 8)
#   NS=<ns>                        **required** (lab: multi-agent)
#   MATRIX_PATHS       (default: training + prometheus_firing_simulation, xem e2e_incident_matrix.sh)
#   STRICT_ASSERT, SLEEP_SEC — truyền cho e2e
#   SKIP_PYTEST=1      — chỉ chạy e2e matrix
#   SKIP_E2E=1         — chỉ pytest
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [[ -z "${NS:-}" ]]; then
  echo "rag_llm_training_loop.sh: set NS to the Kubernetes namespace (no default)." >&2
  exit 2
fi
ITERATIONS="${ITERATIONS:-8}"
if [[ "${ITERATIONS}" -lt 1 ]] || [[ "${ITERATIONS}" -gt 20 ]]; then
  echo "ITERATIONS must be 1..20 (got ${ITERATIONS})" >&2
  exit 2
fi

PY="${ROOT}/.venv/bin/python"
if [[ ! -x "${PY}" ]]; then
  PY="${PYTHON:-python3}"
fi

LOG_DIR="${LOG_DIR:-${ROOT}/reports/rag-training-loops}"
mkdir -p "${LOG_DIR}"
STAMP="$(date +%Y%m%d_%H%M%S)"
SUMMARY_JSON="${LOG_DIR}/summary_${STAMP}.json"
RUN_LOG="${LOG_DIR}/run_${STAMP}.log"

_log() { echo "[rag-train-loop] $*" | tee -a "${RUN_LOG}"; }

_log "iterations=${ITERATIONS} ROOT=${ROOT}"
_log "STRICT_ASSERT=${STRICT_ASSERT:-} SLEEP_SEC=${SLEEP_SEC:-} NS=${NS}"

results=()
overall_ok=0

for i in $(seq 1 "${ITERATIONS}"); do
  _log "========== iteration ${i}/${ITERATIONS} =========="
  t0=$(date +%s)
  step_ok=1

  if [[ "${SKIP_PYTEST:-0}" != "1" ]]; then
    _log "pytest (exclude integration)..."
    if ! "${PY}" -m pytest "${ROOT}/tests/" -q --ignore="${ROOT}/tests/integration" >>"${RUN_LOG}" 2>&1; then
      _log "FAIL: pytest iteration ${i}"
      step_ok=0
    fi
  fi

  if [[ "${SKIP_E2E:-0}" != "1" ]] && [[ "${step_ok}" -eq 1 ]]; then
    _log "e2e incident matrix (full SCENARIOS list)..."
    export MATRIX_PATHS="${MATRIX_PATHS:-${ROOT}/config/incident_training_matrix.yaml:${ROOT}/config/prometheus_firing_simulation.yaml}"
    export REPORT_JSON="${LOG_DIR}/incident_matrix_iter_${STAMP}_${i}.json"
    if ! (
      cd "${ROOT}"
      STRICT_ASSERT="${STRICT_ASSERT:-0}" SLEEP_SEC="${SLEEP_SEC:-5}" \
        NS="${NS}" \
        bash "${ROOT}/scripts/e2e_incident_matrix.sh" >>"${RUN_LOG}" 2>&1
    ); then
      _log "FAIL: e2e_incident_matrix iteration ${i}"
      step_ok=0
    fi
  fi

  t1=$(date +%s)
  dur=$((t1 - t0))
  if [[ "${step_ok}" -eq 1 ]]; then
    _log "iteration ${i} OK (${dur}s)"
  else
    overall_ok=1
    _log "iteration ${i} FAILED (${dur}s)"
  fi
  results+=("${i}:${step_ok}:${dur}")
done

"${PY}" - "${SUMMARY_JSON}" "${ITERATIONS}" "${STAMP}" "${overall_ok}" "${results[@]}" <<'PY'
import json
import sys

out = sys.argv[1]
iters = int(sys.argv[2])
stamp = sys.argv[3]
ok = int(sys.argv[4]) == 0
rest = sys.argv[5:]
rows = []
for part in rest:
    a, b, c = part.split(":", 2)
    rows.append({"iteration": int(a), "ok": bool(int(b)), "duration_sec": int(c)})

doc = {
    "stamp": stamp,
    "iterations_requested": iters,
    "all_passed": ok,
    "iterations": rows,
}
open(out, "w", encoding="utf-8").write(json.dumps(doc, indent=2) + "\n")
print(out)
PY

_log "summary written: ${SUMMARY_JSON}"
if [[ "${overall_ok}" -ne 0 ]]; then
  exit 1
fi
exit 0
