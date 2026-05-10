#!/usr/bin/env bash
# Chaos / incident matrix lab run aligned with docs/reports/chaos-rag-selflearn-runbook.md
#
# Wraps scripts/e2e_incident_matrix.sh with:
#   - default smoke SCENARIOS (fast gateway_payload) or full matrix
#   - STRICT_ASSERT=0 for lab (override with STRICT_ASSERT=1)
#   - JSON report under reports/chaos-rag-lab/
#   - registry fragment JSONL (scenario_id, trace_id) derived from report
#
# Usage:
#   NS=<ns> bash scripts/chaos_rag_lab_run.sh              # smoke (2 gateway scenarios)
#   NS=<ns> CHAOS_RAG_FULL=1 bash scripts/chaos_rag_lab_run.sh   # all scenarios from MATRIX_PATHS
#   NS=<ns> SCENARIOS=nginx_waiting_fault,redis_probe_fault bash scripts/chaos_rag_lab_run.sh
#
# Env: same as e2e_incident_matrix.sh (NS, MATRIX_PATHS, SLEEP_SEC, STRICT_ASSERT, REPORT_JSON)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [[ -x "${ROOT}/.venv/bin/python" ]]; then
  PY="${ROOT}/.venv/bin/python"
else
  PY="${PY:-python3}"
fi

STRICT_ASSERT="${STRICT_ASSERT:-0}"
SLEEP_SEC="${SLEEP_SEC:-8}"
if [[ -z "${NS:-}" ]]; then
  echo "chaos_rag_lab_run.sh: set NS to the target Kubernetes namespace (no default)." >&2
  exit 2
fi
REPORT_JSON="${REPORT_JSON:-${ROOT}/reports/chaos-rag-lab/latest.json}"
CHAOS_RUN_ID="${CHAOS_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
export NS STRICT_ASSERT SLEEP_SEC REPORT_JSON MATRIX_PATHS

if [[ -z "${SCENARIOS:-}" ]]; then
  if [[ "${CHAOS_RAG_FULL:-0}" == "1" ]]; then
    SCENARIOS=""
  else
    # Smoke: two gateway_payload scenarios (no nginx/redis fault injectors)
    SCENARIOS="cpu_throttling_fake_healthy,memory_leak_slow_poison"
  fi
fi
export SCENARIOS

mkdir -p "$(dirname "${REPORT_JSON}")"
META_JSON="${ROOT}/reports/chaos-rag-lab/run-meta.json"

echo "[chaos-rag-lab] chaos_run_id=${CHAOS_RUN_ID} STRICT_ASSERT=${STRICT_ASSERT} SLEEP_SEC=${SLEEP_SEC} NS=${NS}"
echo "[chaos-rag-lab] SCENARIOS=${SCENARIOS:-<all from matrix>}"
echo "[chaos-rag-lab] REPORT_JSON=${REPORT_JSON}"

"${PY}" - "$META_JSON" "$CHAOS_RUN_ID" "$STRICT_ASSERT" "$SLEEP_SEC" "$NS" "${SCENARIOS:-FULL}" <<'PY'
import json, sys, time
from pathlib import Path
meta = {
    "chaos_run_id": sys.argv[2],
    "strict_assert": sys.argv[3],
    "sleep_sec": sys.argv[4],
    "namespace": sys.argv[5],
    "scenarios": sys.argv[6],
    "started_at_unix": int(time.time()),
}
Path(sys.argv[1]).write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
PY

set +e
bash "${ROOT}/scripts/e2e_incident_matrix.sh"
RC=$?
set -e

REGISTRY_JSONL="${ROOT}/reports/chaos-rag-lab/registry-from-report.jsonl"
"${PY}" - "$REPORT_JSON" "$REGISTRY_JSONL" "$CHAOS_RUN_ID" <<'PY'
import json
import sys
from pathlib import Path

report_path = Path(sys.argv[1])
out_path = Path(sys.argv[2])
run_id = sys.argv[3]

if not report_path.exists():
    out_path.write_text("", encoding="utf-8")
    print("no report yet:", report_path)
    raise SystemExit(0)

data = json.loads(report_path.read_text(encoding="utf-8"))
lines = []
for e in data.get("scenarios") or []:
    sid = e.get("scenario") or ""
    tid = e.get("trace_id") or ""
    row = {
        "chaos_run_id": run_id,
        "scenario_id": sid,
        "trace_id": tid,
        "learning_round": 1,
        "matrix_status": e.get("status"),
        "note": "auto_from_chaos_rag_lab_run",
    }
    lines.append(json.dumps(row, ensure_ascii=False))

out_path.parent.mkdir(parents=True, exist_ok=True)
out_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
print(str(out_path))
PY

echo "[chaos-rag-lab] registry_jsonl=${REGISTRY_JSONL}"
echo "[chaos-rag-lab] report=${REPORT_JSON}"
echo "[chaos-rag-lab] meta=${META_JSON}"

exit "${RC}"
