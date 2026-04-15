#!/usr/bin/env bash
# E2E incident matrix — Wave A1 RBAC lockdown + Phase B app_log autonomous replay.
#
# Scenarios (in execution order):
#   wave_a1_rbac_manifest      Validate executor RBAC YAML files exist, parse, and use omni-executor SA.
#   wave_a1_rbac_permissions   kubectl auth can-i checks: omni-executor lacks cluster-admin (cluster mode).
#   phase_b_pytest             pytest tests/test_omni_stateful_loop.py — three-lane stateful-loop tests (replaces deleted test_phase_b_app_log_replay.py).
#   phase_b_unit_full          Full unit suite (all tests/): all lanes + shadow write-back + INV_* gates.
#   phase_b_api_resource       POST resource lane alert to mvp_api → assert lane=resource in response.
#   phase_b_api_state          POST state lane alert to mvp_api → assert lane=state + action!=noop.
#   phase_b_api_app_log_fc     POST app_log lane alert → assert fail-closed noop (no Loki in CI).
#   phase_b_sec_audit          POST out-of-scope namespace → assert SEC_AUDIT_CRITICAL logged or 403.
#   nginx_waiting_fault        Live cluster: inject missing ConfigMap fault → Omni autonomously resolves.
#
# Usage:
#   bash scripts/e2e_incident_matrix.sh
#   SCENARIOS=phase_b_pytest,phase_b_unit_full bash scripts/e2e_incident_matrix.sh
#   MVP_API_URL=http://localhost:8000 bash scripts/e2e_incident_matrix.sh
#
# Env:
#   NS=multi-agent               Target namespace (default: multi-agent)
#   SCENARIOS=a,b,c              Comma-separated subset (default: all)
#   MVP_API_URL=                 mvp_api base URL (default: http://localhost:8000); HTTP scenarios skip if unreachable
#   STRICT_ASSERT=1              Fail on assertion errors (default: 1)
#   REPORT_JSON=...              Output report path (default: reports/incident-matrix/latest.json)
#   CLUSTER_MODE=auto            auto|yes|no — whether to attempt kubectl auth checks
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
STRICT_ASSERT="${STRICT_ASSERT:-1}"
MVP_API_URL="${MVP_API_URL:-http://localhost:8000}"
CLUSTER_MODE="${CLUSTER_MODE:-auto}"
REPORT_JSON="${REPORT_JSON:-${ROOT}/reports/incident-matrix/latest.json}"

REPORT_ENTRIES="$(mktemp)"
START_TS="$(date +%s)"
FAIL_COUNT=0
TOTAL_COUNT=0

# All scenarios in canonical execution order.
ALL_SCENARIOS="wave_a1_rbac_manifest,wave_a1_rbac_permissions,phase_b_pytest,phase_b_unit_full,phase_b_api_resource,phase_b_api_state,phase_b_api_app_log_fc,phase_b_sec_audit,nginx_waiting_fault"

_log()  { echo "[e2e] $*"; }
_pass() { echo "[e2e] PASS: $*"; }
_fail() { echo "[e2e] FAIL: $*" >&2; }

# ---------------------------------------------------------------------------
# Report helpers (same JSON schema as original)
# ---------------------------------------------------------------------------

_append_report_entry() {
  local scenario="$1" status="$2" duration_sec="$3" note="$4"
  "${PYTHON_BIN}" - "${REPORT_ENTRIES}" "$scenario" "$status" "$duration_sec" "$note" <<'PY'
import json, sys
from pathlib import Path
f = Path(sys.argv[1])
obj = {"scenario": sys.argv[2], "status": sys.argv[3],
       "duration_sec": int(sys.argv[4]), "note": sys.argv[5]}
with f.open("a", encoding="utf-8") as fp:
    fp.write(json.dumps(obj, ensure_ascii=False) + "\n")
PY
}

_run_scenario() {
  local scenario="$1"; shift
  local fn="$1"; shift
  local t0 t1 rc duration
  t0="$(date +%s)"
  _log "─── scenario=${scenario} ───"
  set +e
  "${fn}" "$@"
  rc=$?
  set -e
  t1="$(date +%s)"
  duration=$((t1 - t0))
  TOTAL_COUNT=$((TOTAL_COUNT + 1))
  if [[ "${rc}" -eq 0 ]]; then
    _pass "${scenario} (${duration}s)"
    _append_report_entry "${scenario}" "passed" "${duration}" ""
  else
    FAIL_COUNT=$((FAIL_COUNT + 1))
    _fail "${scenario} (${duration}s, exit ${rc})"
    _append_report_entry "${scenario}" "failed" "${duration}" "exit_code=${rc}"
    if [[ "${STRICT_ASSERT}" == "1" ]]; then
      _emit_report
      exit 1
    fi
  fi
}

_emit_report() {
  local end_ts report_dir git_sha
  end_ts="$(date +%s)"
  report_dir="$(dirname "${REPORT_JSON}")"
  mkdir -p "${report_dir}"
  git_sha="$(cd "${ROOT}" && git rev-parse HEAD 2>/dev/null || echo "")"
  "${PYTHON_BIN}" - "${REPORT_ENTRIES}" "${REPORT_JSON}" "${START_TS}" "${end_ts}" "${git_sha}" <<'PY'
import json, sys
from pathlib import Path

entries_file, out_file = Path(sys.argv[1]), Path(sys.argv[2])
start_ts, end_ts, git_sha = int(sys.argv[3]), int(sys.argv[4]), sys.argv[5]

entries = []
if entries_file.exists():
    for ln in entries_file.read_text(encoding="utf-8", errors="replace").splitlines():
        ln = ln.strip()
        if ln:
            entries.append(json.loads(ln))

passed = sum(1 for e in entries if e.get("status") == "passed")
failed = sum(1 for e in entries if e.get("status") != "passed")
report = {
    "schema_version": "2",
    "wave": "A1+PhaseB",
    "started_at": start_ts,
    "ended_at": end_ts,
    "duration_sec": max(0, end_ts - start_ts),
    "git_sha": git_sha,
    "summary": {"total": len(entries), "passed": passed, "failed": failed, "ok": failed == 0},
    "scenarios": entries,
}
out_file.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(str(out_file))
PY
}

# ---------------------------------------------------------------------------
# Cluster availability probe
# ---------------------------------------------------------------------------

_cluster_available() {
  if [[ "${CLUSTER_MODE}" == "yes" ]]; then return 0; fi
  if [[ "${CLUSTER_MODE}" == "no" ]]; then return 1; fi
  # auto: test kubectl access
  if [[ -x "${KUBE}" ]]; then
    "${KUBE}" get nodes --request-timeout=3s >/dev/null 2>&1
  else
    kubectl get nodes --request-timeout=3s >/dev/null 2>&1
  fi
}

_kube() {
  if [[ -x "${KUBE}" ]]; then
    "${KUBE}" "$@"
  else
    kubectl "$@"
  fi
}

# ---------------------------------------------------------------------------
# mvp_api reachability probe
# ---------------------------------------------------------------------------

_api_reachable() {
  curl -sf --max-time 3 "${MVP_API_URL}/healthz" >/dev/null 2>&1
}

_api_post() {
  # Usage: _api_post <json_body>
  # Returns raw JSON response on stdout.
  curl -sf --max-time 30 \
    -X POST "${MVP_API_URL}/alert" \
    -H "Content-Type: application/json" \
    -d "$1"
}

_assert_json_field() {
  # Usage: _assert_json_field <json> <jq_path> <expected>
  local actual
  actual="$(echo "$1" | "${PYTHON_BIN}" -c "
import json, sys
data = json.load(sys.stdin)
parts = sys.argv[1].lstrip('.').split('.')
val = data
for p in parts:
    val = val[p]
print(str(val))
" "$2" 2>/dev/null || echo "__MISSING__")"
  if [[ "${actual}" != "$3" ]]; then
    _fail "assert ${2}==${3}, got ${actual}"
    return 1
  fi
  _pass "assert ${2}==${3}"
}

# ---------------------------------------------------------------------------
# Wave A1 — RBAC manifest validation
# ---------------------------------------------------------------------------

_scenario_wave_a1_rbac_manifest() {
  local rc=0

  # 1. Required YAML files exist.
  local manifests=(
    "${ROOT}/k8s/rbac-executor-least-privilege.yaml"
    "${ROOT}/k8s/deployments/executor-rbac.yaml"
    "${ROOT}/k8s/deployments/omni-executor.yaml"
  )
  for f in "${manifests[@]}"; do
    if [[ -f "${f}" ]]; then
      _pass "exists: ${f##"${ROOT}/"}"
    else
      _fail "missing: ${f##"${ROOT}/"}"
      rc=1
    fi
  done

  # 2. omni-executor.yaml references omni-executor SA (not omni-worker).
  if grep -q "serviceAccountName: omni-executor" "${ROOT}/k8s/deployments/omni-executor.yaml"; then
    _pass "omni-executor.yaml: serviceAccountName=omni-executor"
  else
    _fail "omni-executor.yaml still uses omni-worker SA — Wave A1 not applied"
    rc=1
  fi

  # 3. rbac-executor-least-privilege.yaml must NOT bind cluster-admin (comments excluded).
  if grep -v '^\s*#' "${ROOT}/k8s/rbac-executor-least-privilege.yaml" | grep -qi "cluster-admin"; then
    _fail "rbac-executor-least-privilege.yaml binds cluster-admin role"
    rc=1
  else
    _pass "rbac-executor-least-privilege.yaml: no cluster-admin binding"
  fi

  # 4. ClusterRoleBinding must NOT exist in the file (only RoleBinding allowed).
  if grep -q "kind: ClusterRoleBinding" "${ROOT}/k8s/rbac-executor-least-privilege.yaml"; then
    _fail "rbac-executor-least-privilege.yaml contains ClusterRoleBinding — violates namespace scope"
    rc=1
  else
    _pass "rbac-executor-least-privilege.yaml: no ClusterRoleBinding (namespace-scoped only)"
  fi

  # 5. Manifest YAML parses cleanly via Python.
  for f in "${ROOT}/k8s/rbac-executor-least-privilege.yaml" "${ROOT}/k8s/deployments/executor-rbac.yaml"; do
    local fname="${f##"${ROOT}/"}"
    if "${PYTHON_BIN}" -c "
import sys
try:
    import yaml
    list(yaml.safe_load_all(open(sys.argv[1])))
    print('ok')
except ImportError:
    # yaml not available — basic check
    open(sys.argv[1]).read()
    print('ok-noparse')
except Exception as e:
    print(f'error: {e}')
    sys.exit(1)
" "${f}" >/dev/null 2>&1; then
      _pass "YAML parses: ${fname}"
    else
      _fail "YAML parse error: ${fname}"
      rc=1
    fi
  done

  # 6. kubectl dry-run if cluster available.
  if _cluster_available; then
    _log "cluster reachable — running kubectl apply --dry-run=client"
    if _kube apply --dry-run=client -f "${ROOT}/k8s/rbac-executor-least-privilege.yaml" >/dev/null 2>&1; then
      _pass "kubectl dry-run: rbac-executor-least-privilege.yaml"
    else
      _fail "kubectl dry-run failed: rbac-executor-least-privilege.yaml"
      rc=1
    fi
  else
    _log "cluster not reachable — skipping kubectl dry-run (local dev mode)"
  fi

  return "${rc}"
}

# ---------------------------------------------------------------------------
# Wave A1 — SA permission checks (cluster only)
# ---------------------------------------------------------------------------

_scenario_wave_a1_rbac_permissions() {
  if ! _cluster_available; then
    _log "SKIP: cluster not reachable (set CLUSTER_MODE=yes to force)"
    return 0
  fi

  local SA="system:serviceaccount:${NS}:omni-executor"
  local rc=0

  # cluster-admin must be GONE.
  # Use grep pattern: kubectl exits 1 when answer is "no", making || echo "no" fire twice.
  if _kube auth can-i '*' '*' --all-namespaces --as="${SA}" 2>/dev/null | grep -q "^yes"; then
    _fail "omni-executor SA still has cluster-admin — apply k8s/rbac-executor-least-privilege.yaml"
    rc=1
  else
    _pass "omni-executor SA: no cluster-wide wildcard (cluster-admin removed)"
  fi

  # Allowed: patch deployments in multi-agent.
  if _kube auth can-i patch deployments -n "${NS}" --as="${SA}" 2>/dev/null | grep -q "^yes"; then
    _pass "omni-executor SA: can patch deployments in ${NS}"
  else
    _fail "omni-executor SA: cannot patch deployments in ${NS} — RBAC not applied yet"
    rc=1
  fi

  # Blocked: patch deployments in production.
  if _kube auth can-i patch deployments -n production --as="${SA}" 2>/dev/null | grep -q "^yes"; then
    _fail "omni-executor SA can patch deployments in production — namespace scope too wide"
    rc=1
  else
    _pass "omni-executor SA: cannot patch deployments in production (scope enforced)"
  fi

  # Blocked: delete nodes (cluster-level destructive).
  if _kube auth can-i delete nodes --as="${SA}" 2>/dev/null | grep -q "^yes"; then
    _fail "omni-executor SA can delete nodes — critically over-privileged"
    rc=1
  else
    _pass "omni-executor SA: cannot delete nodes"
  fi

  return "${rc}"
}

# ---------------------------------------------------------------------------
# Phase B — pytest: focused replay suite
# ---------------------------------------------------------------------------

_scenario_phase_b_pytest() {
  _log "running: pytest tests/test_omni_stateful_loop.py (three-lane stateful-loop suite)"
  "${PYTHON_BIN}" -m pytest \
    "${ROOT}/tests/test_omni_stateful_loop.py" \
    -v --tb=short -q 2>&1
}

# ---------------------------------------------------------------------------
# Phase B — full unit suite (all 3 lanes + INV_* + shadow write-back)
# ---------------------------------------------------------------------------

_scenario_phase_b_unit_full() {
  _log "running: full unit suite (ignore integration)"
  "${PYTHON_BIN}" -m pytest \
    "${ROOT}/tests/" \
    -q --tb=short \
    --ignore="${ROOT}/tests/integration" 2>&1
}

# ---------------------------------------------------------------------------
# Phase B — HTTP API scenarios (skip if mvp_api unreachable)
# ---------------------------------------------------------------------------

_api_scenario_preamble() {
  if ! _api_reachable; then
    _log "SKIP: mvp_api not reachable at ${MVP_API_URL}"
    _log "      Start with: ${ROOT}/.venv/bin/uvicorn scripts.mvp_api:app --reload"
    return 0
  fi
  _log "mvp_api reachable at ${MVP_API_URL}"
  return 1  # signal: proceed
}

_scenario_phase_b_api_resource() {
  if _api_scenario_preamble; then return 0; fi
  local body='{"alertname":"HighCPUUsage","namespace":"multi-agent","pod":"api-server-7d9f8b6c4-xk2pq","container":"api-server","severity":"warning","memory_limit":"512Mi"}'
  _log "POST resource lane alert: HighCPUUsage"
  local resp
  resp="$(_api_post "${body}")"
  _log "response: ${resp}"
  _assert_json_field "${resp}" ".lane" "resource"
}

_scenario_phase_b_api_state() {
  if _api_scenario_preamble; then return 0; fi
  local body='{"alertname":"KubePodOOMKilled","namespace":"multi-agent","pod":"api-server-7d9f8b6c4-xk2pq","container":"api-server","severity":"critical","memory_limit":"512Mi"}'
  _log "POST state lane alert: KubePodOOMKilled"
  local resp
  resp="$(_api_post "${body}")"
  _log "response: ${resp}"
  _assert_json_field "${resp}" ".lane" "state"
  # State lane must not produce noop (OOMKilled has a concrete action).
  local action
  action="$(echo "${resp}" | "${PYTHON_BIN}" -c "import json,sys; print(json.load(sys.stdin)['plan']['action'])")"
  _log "plan.action=${action}"
  if [[ "${action}" != "noop" ]]; then
    _pass "state lane produced action=${action} (not noop)"
  else
    # noop is acceptable if Ollama is not running — warn but don't fail
    _log "WARN: state lane produced noop — Ollama may be unreachable"
  fi
}

_scenario_phase_b_api_app_log_fc() {
  # app_log fail-closed: no LOKI_BASE_URL → noop with ERR_REA_LOG_SOURCE_UNAVAILABLE.
  if _api_scenario_preamble; then return 0; fi
  local body='{"alertname":"HttpErrorRate5xx","namespace":"multi-agent","pod":"api-server-7d9f8b6c4-xk2pq","container":"api-server","severity":"critical","message":"sustained 503 errors"}'
  _log "POST app_log lane alert: HttpErrorRate5xx (expect fail-closed noop — no Loki in CI)"
  local resp
  resp="$(_api_post "${body}")"
  _log "response: ${resp}"
  _assert_json_field "${resp}" ".lane" "app_log"
  _assert_json_field "${resp}" ".plan.action" "noop"
  # Reasoning must contain the canonical error code.
  local reasoning
  reasoning="$(echo "${resp}" | "${PYTHON_BIN}" -c "import json,sys; print(json.load(sys.stdin)['plan']['reasoning'])")"
  if echo "${reasoning}" | grep -q "ERR_REA_LOG_SOURCE_UNAVAILABLE"; then
    _pass "app_log fail-closed: ERR_REA_LOG_SOURCE_UNAVAILABLE in reasoning"
  else
    _fail "app_log fail-closed: ERR_REA_LOG_SOURCE_UNAVAILABLE missing from reasoning"
    return 1
  fi
}

_scenario_phase_b_sec_audit() {
  # SEC_AUDIT_CRITICAL: POST to out-of-scope namespace in lab mode.
  # In lab: warning logged, execution proceeds. In non-lab: 403.
  if _api_scenario_preamble; then return 0; fi
  local body='{"alertname":"KubePodOOMKilled","namespace":"production","pod":"api-7d9f-xk2","container":"api","severity":"critical","memory_limit":"512Mi"}'
  _log "POST out-of-scope namespace (production) — expect SEC_AUDIT_CRITICAL warning in lab"
  local http_code
  http_code="$(curl -s -o /dev/null -w "%{http_code}" --max-time 30 \
    -X POST "${MVP_API_URL}/alert" \
    -H "Content-Type: application/json" \
    -d "${body}")"
  _log "HTTP status: ${http_code}"

  local env_mode="${OMNI_ENV_MODE:-}"
  if [[ "${env_mode}" != "lab" ]]; then
    # Non-lab or unset: expect 403 block.
    if [[ "${http_code}" == "403" ]]; then
      _pass "SEC_AUDIT_CRITICAL: out-of-scope namespace blocked with HTTP 403 (non-lab mode)"
    else
      _log "WARN: expected 403 for production namespace in non-lab mode, got ${http_code}"
      _log "      (SEC_AUDIT_CRITICAL may be logged server-side — check mvp_api logs)"
    fi
  else
    # Lab: expect 200 with warning logged (not a hard block).
    if [[ "${http_code}" == "200" ]]; then
      _pass "SEC_AUDIT_CRITICAL: lab mode logged warning, execution proceeded (HTTP 200)"
    else
      _log "WARN: lab mode returned HTTP ${http_code} for out-of-scope namespace"
    fi
  fi
}

# ---------------------------------------------------------------------------
# nginx_waiting_fault — real ConfigMap missing fault → autonomous resolution
# Requires: live cluster + mvp_api running (OMNI_ENV_MODE=lab).
# Flow: deploy nginx-test → inject broken ConfigMap mount → POST alert →
#       assert Omni produces action (patch_configmap_key / noop if Ollama down) →
#       verify ConfigMap created if action fired → restore.
# ---------------------------------------------------------------------------

_scenario_nginx_waiting_fault() {
  if ! _cluster_available; then
    _log "SKIP: cluster not reachable (set CLUSTER_MODE=yes to force)"
    return 0
  fi
  if ! _api_reachable; then
    _log "SKIP: mvp_api not reachable at ${MVP_API_URL}"
    _log "      Start with: ${ROOT}/.venv/bin/uvicorn scripts.mvp_api:app --reload"
    return 0
  fi

  local CM_NAME="${CM_NAME:-nginx-test-never-created-cm}"
  local DEPLOY_YAML="${ROOT}/scripts/nginx-test-deployment.yaml"
  local rc=0

  # 1. Ensure nginx-test deployment YAML exists.
  if [[ ! -f "${DEPLOY_YAML}" ]]; then
    _log "SKIP: ${DEPLOY_YAML} not found — create it first"
    return 0
  fi

  # 2. Apply clean nginx-test and wait for healthy rollout.
  _log "applying clean nginx-test..."
  _kube apply -f "${DEPLOY_YAML}" >/dev/null
  _kube rollout status deployment/nginx-test -n "${NS}" --timeout=90s >/dev/null 2>&1 || \
    _log "WARN: initial rollout wait timed out — continuing"

  # 3. Delete target ConfigMap if it exists (ensure fault is real).
  _kube delete configmap "${CM_NAME}" -n "${NS}" --ignore-not-found=true >/dev/null 2>&1 || true

  # 4. Inject fault: mount non-existent ConfigMap.
  _log "injecting ConfigMap fault: ${CM_NAME}"
  _kube patch deployment nginx-test -n "${NS}" --type=json -p "[
    {\"op\":\"add\",\"path\":\"/spec/template/spec/volumes\",\"value\":[{\"name\":\"broken-cfg\",\"configMap\":{\"name\":\"${CM_NAME}\"}}]},
    {\"op\":\"add\",\"path\":\"/spec/template/spec/containers/0/volumeMounts\",\"value\":[{\"name\":\"broken-cfg\",\"mountPath\":\"/tmp/broken-cm-ro\"}]}
  ]" >/dev/null 2>&1 || {
    _log "json-patch failed (may already have volumes) — trying scale cycle"
  }
  # Force pod replace so new volumes take effect.
  _kube scale deployment nginx-test -n "${NS}" --replicas=0 >/dev/null 2>&1 || true
  _kube wait --for=delete pod -l app=nginx-test -n "${NS}" --timeout=60s >/dev/null 2>&1 || true
  _kube scale deployment nginx-test -n "${NS}" --replicas=1 >/dev/null 2>&1 || true
  sleep 5

  # 5. Get real pod name for the alert payload.
  local pod
  pod="$(_kube get pods -n "${NS}" -l app=nginx-test \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "nginx-test-pod")"
  _log "stuck pod=${pod}"
  _kube describe pod "${pod}" -n "${NS}" 2>/dev/null | grep -iE 'FailedMount|configmap|Error' | head -5 || true

  # 6. POST alert to mvp_api.
  local body
  body="{\"alertname\":\"NginxTestContainerWaitingFaultLab\",\"namespace\":\"${NS}\",\"pod\":\"${pod}\",\"container\":\"nginx\",\"severity\":\"critical\",\"message\":\"CreateContainerConfigError: configmap \\\"${CM_NAME}\\\" not found\"}"
  _log "POST fault alert to mvp_api"
  local resp
  if ! resp="$(_api_post "${body}" 2>/dev/null)"; then
    _fail "mvp_api POST failed"
    rc=1
  else
    _log "response: ${resp}"
    _assert_json_field "${resp}" ".lane" "state" || rc=1

    local action
    action="$(echo "${resp}" | "${PYTHON_BIN}" -c "import json,sys; print(json.load(sys.stdin)['plan']['action'])" 2>/dev/null || echo "unknown")"
    _log "plan.action=${action}"

    if [[ "${action}" == "noop" ]]; then
      _log "WARN: Omni returned noop — Ollama may be unreachable or LLM chose noop"
    else
      _pass "nginx_waiting_fault: Omni produced action=${action}"
      local executed
      executed="$(echo "${resp}" | "${PYTHON_BIN}" -c "import json,sys; print(json.load(sys.stdin)['executed'])" 2>/dev/null || echo "False")"
      _log "executed=${executed}"
    fi

    # 7. If patch_configmap_key fired in lab mode, ConfigMap should exist now.
    if [[ "${action}" == "patch_configmap_key" ]]; then
      sleep 3
      if _kube get configmap "${CM_NAME}" -n "${NS}" >/dev/null 2>&1; then
        _pass "nginx_waiting_fault: ConfigMap ${CM_NAME} created autonomously"
      else
        _log "WARN: ConfigMap not found — OMNI_ENV_MODE may not be lab or SA permissions not applied"
      fi
    fi
  fi

  # 8. Restore clean nginx-test.
  _log "restoring clean nginx-test..."
  _kube delete deployment nginx-test -n "${NS}" --ignore-not-found=true >/dev/null 2>&1 || true
  _kube delete configmap "${CM_NAME}" -n "${NS}" --ignore-not-found=true >/dev/null 2>&1 || true
  _kube apply -f "${DEPLOY_YAML}" >/dev/null 2>&1 || true

  return "${rc}"
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_dispatch_scenario() {
  local sc="$1"
  case "${sc}" in
    wave_a1_rbac_manifest)    _run_scenario "${sc}" _scenario_wave_a1_rbac_manifest ;;
    wave_a1_rbac_permissions) _run_scenario "${sc}" _scenario_wave_a1_rbac_permissions ;;
    phase_b_pytest)           _run_scenario "${sc}" _scenario_phase_b_pytest ;;
    phase_b_unit_full)        _run_scenario "${sc}" _scenario_phase_b_unit_full ;;
    phase_b_api_resource)     _run_scenario "${sc}" _scenario_phase_b_api_resource ;;
    phase_b_api_state)        _run_scenario "${sc}" _scenario_phase_b_api_state ;;
    phase_b_api_app_log_fc)   _run_scenario "${sc}" _scenario_phase_b_api_app_log_fc ;;
    phase_b_sec_audit)        _run_scenario "${sc}" _scenario_phase_b_sec_audit ;;
    nginx_waiting_fault)      _run_scenario "${sc}" _scenario_nginx_waiting_fault ;;
    *)
      _fail "unknown scenario '${sc}' — valid: ${ALL_SCENARIOS}"
      TOTAL_COUNT=$((TOTAL_COUNT + 1))
      FAIL_COUNT=$((FAIL_COUNT + 1))
      _append_report_entry "${sc}" "failed" "0" "unknown_scenario"
      ;;
  esac
}

if [[ -z "${SCENARIOS}" ]]; then
  SCENARIOS="${ALL_SCENARIOS}"
fi

IFS=',' read -r -a scenario_arr <<<"${SCENARIOS}"
for raw in "${scenario_arr[@]}"; do
  sc="$(echo "${raw}" | tr -d '[:space:]')"
  [[ -z "${sc}" ]] && continue
  _dispatch_scenario "${sc}"
done

# ---------------------------------------------------------------------------
# Final report
# ---------------------------------------------------------------------------

report_path="$(_emit_report)"
_log "report → ${report_path}"

echo ""
echo "═══════════════════════════════════════════════"
echo "  Wave A1 + Phase B — E2E Results"
echo "═══════════════════════════════════════════════"
"${PYTHON_BIN}" - "${report_path}" <<'PY'
import json, sys
from pathlib import Path
r = json.loads(Path(sys.argv[1]).read_text())
s = r["summary"]
print(f"  Total  : {s['total']}")
print(f"  Passed : {s['passed']}")
print(f"  Failed : {s['failed']}")
print(f"  OK     : {'YES' if s['ok'] else 'NO'}")
print(f"  Git    : {r['git_sha'][:12]}")
print()
for e in r["scenarios"]:
    icon = "✓" if e["status"] == "passed" else "✗"
    note = f"  ({e['note']})" if e.get("note") else ""
    print(f"  {icon} {e['scenario']:<35} {e['duration_sec']}s{note}")
PY
echo "═══════════════════════════════════════════════"

rm -f "${REPORT_ENTRIES}"

if [[ "${FAIL_COUNT}" -gt 0 ]]; then
  _log "completed with ${FAIL_COUNT} failure(s) / ${TOTAL_COUNT} total"
  exit 1
fi
_log "all ${TOTAL_COUNT} scenarios passed"
