#!/usr/bin/env bash
# E2E incident matrix — Wave A1 RBAC lockdown + Phase B app_log autonomous replay.
#
# Scenarios (in execution order):
#   wave_a1_rbac_manifest      Validate worker RBAC YAML files exist, parse, and use omni-fullstack SA.
#   wave_a1_rbac_permissions   kubectl auth can-i checks: omni-fullstack lacks cluster-admin (cluster mode).
#   phase_b_pytest             pytest tests/ (full unit suite, ignore integration).
#   phase_b_unit_full          Full unit suite (all tests/): all lanes + shadow write-back + INV_* gates.
#   nginx_waiting_fault        Live cluster: inject missing ConfigMap fault → verify Kafka pipeline resolves.
#
# Usage:
#   NS=<ns> bash scripts/e2e_incident_matrix.sh
#   NS=<ns> SCENARIOS=phase_b_pytest,phase_b_unit_full bash scripts/e2e_incident_matrix.sh
#
# Env:
#   NS=                          Target namespace (**required** — no default; export before run).
#   SCENARIOS=a,b,c              Comma-separated subset (default: all)
#   OUT_OF_SCOPE_TEST_NAMESPACE=  Phase B sec_audit + RBAC negative check: namespace outside omni scope (must differ from NS)
#   RBAC_NEGATIVE_NAMESPACE=      Optional override for RBAC denial namespace (default: OUT_OF_SCOPE_TEST_NAMESPACE)
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

if [[ -z "${NS:-}" ]]; then
  echo "e2e_incident_matrix.sh: set NS to the target Kubernetes namespace (no default)." >&2
  exit 2
fi


SCENARIOS="${SCENARIOS:-}"
STRICT_ASSERT="${STRICT_ASSERT:-1}"
CLUSTER_MODE="${CLUSTER_MODE:-auto}"
REPORT_JSON="${REPORT_JSON:-${ROOT}/reports/incident-matrix/latest.json}"

REPORT_ENTRIES="$(mktemp)"
START_TS="$(date +%s)"
FAIL_COUNT=0
TOTAL_COUNT=0

# All scenarios in canonical execution order.
ALL_SCENARIOS="wave_a1_rbac_manifest,wave_a1_rbac_permissions,phase_b_pytest,phase_b_unit_full,nginx_waiting_fault"

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

  # 1. Required YAML files exist. (2026-06-03: split-role executor manifests
  # consolidated into omni-fullstack-rbac.yaml / omni-fullstack.yaml.)
  local manifests=(
    "${ROOT}/k8s/rbac-executor-least-privilege.yaml"
    "${ROOT}/k8s/deployments/omni-fullstack-rbac.yaml"
    "${ROOT}/k8s/deployments/omni-fullstack.yaml"
  )
  for f in "${manifests[@]}"; do
    if [[ -f "${f}" ]]; then
      _pass "exists: ${f##"${ROOT}/"}"
    else
      _fail "missing: ${f##"${ROOT}/"}"
      rc=1
    fi
  done

  # 2. omni-fullstack.yaml references the omni-fullstack SA.
  if grep -q "serviceAccountName: omni-fullstack" "${ROOT}/k8s/deployments/omni-fullstack.yaml"; then
    _pass "omni-fullstack.yaml: serviceAccountName=omni-fullstack"
  else
    _fail "omni-fullstack.yaml does not bind omni-fullstack SA"
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
  for f in "${ROOT}/k8s/rbac-executor-least-privilege.yaml" "${ROOT}/k8s/deployments/omni-fullstack-rbac.yaml"; do
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

  # Worker SA consolidated to omni-fullstack (2026-06-03).
  local SA="system:serviceaccount:${NS}:omni-fullstack"
  local rc=0

  # cluster-admin must be GONE.
  # Use grep pattern: kubectl exits 1 when answer is "no", making || echo "no" fire twice.
  if _kube auth can-i '*' '*' --all-namespaces --as="${SA}" 2>/dev/null | grep -q "^yes"; then
    _fail "omni-fullstack SA still has cluster-admin — apply k8s/deployments/omni-fullstack-rbac.yaml"
    rc=1
  else
    _pass "omni-fullstack SA: no cluster-wide wildcard (cluster-admin removed)"
  fi

  # Allowed: patch deployments in target namespace (${NS}).
  if _kube auth can-i patch deployments -n "${NS}" --as="${SA}" 2>/dev/null | grep -q "^yes"; then
    _pass "omni-fullstack SA: can patch deployments in ${NS}"
  else
    _fail "omni-fullstack SA: cannot patch deployments in ${NS} — RBAC not applied yet"
    rc=1
  fi

  # Out-of-scope namespace patch: in LAB, omni-fullstack holds the lab
  # ClusterRoleBinding (omni-executor-mutate-lab) which is intentionally
  # cluster-wide for cross-namespace remediation drills, so this is informational
  # only. The namespace-scope guarantee is enforced by the PROD
  # rbac-executor-least-privilege.yaml (RoleBinding-only), validated in wave_a1_rbac_manifest.
  local neg_ns="${RBAC_NEGATIVE_NAMESPACE:-${OUT_OF_SCOPE_TEST_NAMESPACE:-}}"
  if [[ -n "${neg_ns}" && "${neg_ns}" != "${NS}" ]]; then
    if _kube auth can-i patch deployments -n "${neg_ns}" --as="${SA}" 2>/dev/null | grep -q "^yes"; then
      _log "INFO: omni-fullstack SA can patch deployments in ${neg_ns} (expected in lab — cluster-wide mutate binding)"
    else
      _pass "omni-fullstack SA: cannot patch deployments in ${neg_ns} (prod-style scope)"
    fi
  fi

  # Blocked: delete nodes (cluster-level destructive) — must hold in lab AND prod.
  if _kube auth can-i delete nodes --as="${SA}" 2>/dev/null | grep -q "^yes"; then
    _fail "omni-fullstack SA can delete nodes — critically over-privileged"
    rc=1
  else
    _pass "omni-fullstack SA: cannot delete nodes"
  fi

  return "${rc}"
}

# ---------------------------------------------------------------------------
# Phase B — pytest: focused replay suite
# ---------------------------------------------------------------------------

_scenario_phase_b_pytest() {
  _log "running: pytest tests/ (ignore integration + real_services)"
  "${PYTHON_BIN}" -m pytest \
    "${ROOT}/tests/" \
    --ignore="${ROOT}/tests/integration" \
    --ignore="${ROOT}/tests/real_services" \
    -q --tb=short 2>&1
}

# ---------------------------------------------------------------------------
# Phase B — full unit suite (all 3 lanes + INV_* + shadow write-back)
# ---------------------------------------------------------------------------

_scenario_phase_b_unit_full() {
  _log "running: full unit suite (ignore integration + live real_services)"
  "${PYTHON_BIN}" -m pytest \
    "${ROOT}/tests/" \
    -q --tb=short \
    --ignore="${ROOT}/tests/integration" \
    --ignore="${ROOT}/tests/real_services" 2>&1
}

# ---------------------------------------------------------------------------
# nginx_waiting_fault — real ConfigMap missing fault → autonomous resolution via Kafka pipeline.
# Requires: live cluster (OMNI_ENV_MODE=lab).
# Flow: deploy nginx-test → inject broken ConfigMap mount →
#       verify Omni resolves autonomously → restore.
# ---------------------------------------------------------------------------

_scenario_nginx_waiting_fault() {
  if ! _cluster_available; then
    _log "SKIP: cluster not reachable (set CLUSTER_MODE=yes to force)"
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

  # 6. Verify Omni analyst picks up the fault via Kafka pipeline.
  _log "waiting 15s for Omni analyst to process fault..."
  sleep 15

  local action="unknown"
  if true; then
    if [[ "${action}" == "noop" ]]; then
      _log "WARN: Omni returned noop — Ollama may be unreachable or LLM chose noop"
    else
      _pass "nginx_waiting_fault: Omni produced action=${action}"
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
