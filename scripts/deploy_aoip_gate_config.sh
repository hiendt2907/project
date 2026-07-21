#!/usr/bin/env bash
# Deploy the canonical AOIP recovery-gate config (config/aoip_agent_gate.env)
# to each VM's /opt/omni-remote-agent/run.env, touching ONLY the managed keys
# (AOIP_GATE_*/AOIP_ALLOWED_SYSTEMD_UNITS) and leaving every other run.env
# key (AOIP_REDIS_URL, AOIP_AGENT_MODE, AOIP_AUDIT_LOG_PATH, ...) untouched.
#
# Why this exists: gate config used to be hand-edited on each VM (sed over
# SSH) with no canonical source in git — capabilities silently drifted
# between cust-app/cust-edge/cust-db and there was no automated way to prove
# the 3 VMs agreed (see task context 2026-07-21). This script makes
# config/aoip_agent_gate.env the single source of truth and gives every run
# a PASS/FAIL verification against it.
#
# Usage:
#   scripts/deploy_aoip_gate_config.sh                    # deploy to $AOIP_GATE_VMS or DEFAULT_VMS
#   scripts/deploy_aoip_gate_config.sh cust-app cust-db    # explicit VM list (CLI wins)
#   AOIP_GATE_VMS="cust-app cust-edge cust-db" scripts/deploy_aoip_gate_config.sh
#   scripts/deploy_aoip_gate_config.sh --canonical /path/to/other.env cust-app
#
# Idempotent: running twice with no canonical change produces identical
# run.env content on the VM and SKIPS the restart on the second run.
set -euo pipefail

# ── VM roster — declared here, NOT hardcoded into the merge/verify logic
# below. Override via CLI args or AOIP_GATE_VMS env var (space-separated).
DEFAULT_VMS=(cust-app cust-edge cust-db)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CANONICAL_FILE="${REPO_ROOT}/config/aoip_agent_gate.env"
REMOTE_INSTALL_DIR="/opt/omni-remote-agent"
REMOTE_RUN_ENV="${REMOTE_INSTALL_DIR}/run.env"
REMOTE_SERVICE="aoip-agent.service"

# Keys this script is allowed to touch. Anything else in run.env is left
# byte-for-byte alone. Regex used with grep -E against the start of a line.
MANAGED_KEY_REGEX='^(AOIP_GATE_[A-Za-z0-9_]+|AOIP_ALLOWED_SYSTEMD_UNITS)='

# ── orb exec wrappers (same convention as scripts/enroll_remote_agent.py) ──
_orb_run() {
  local machine="$1"; shift
  orb run -m "${machine}" -u root "$@"
}

_orb_cat_run_env() {
  local machine="$1"
  _orb_run "${machine}" cat "${REMOTE_RUN_ENV}"
}

_orb_write_run_env() {
  local machine="$1" content="$2"
  orb run -m "${machine}" -u root bash -c \
    "cat > ${REMOTE_RUN_ENV} << 'AOIPGATEEOF'
${content}
AOIPGATEEOF
chmod 600 ${REMOTE_RUN_ENV}"
}

_orb_restart_service() {
  local machine="$1"
  _orb_run "${machine}" systemctl restart "${REMOTE_SERVICE}"
}

_orb_service_active() {
  local machine="$1"
  _orb_run "${machine}" systemctl is-active "${REMOTE_SERVICE}"
}

# ── Pure merge logic — no orb/network here, so this is directly unit
# testable by feeding tmp files (see scripts/deploy_aoip_gate_config.sh
# --self-test, and tests/test_aoip_gate_config.py for the Python-side
# canonical-file checks).
#
# Strategy: strip every line matching MANAGED_KEY_REGEX out of the current
# run.env content (comments/blank lines/other keys pass through UNCHANGED,
# same order), then append the managed KEY=VALUE lines from the canonical
# file. Deterministic canonical order ⇒ re-running against output that
# already has the managed block produces byte-identical content (idempotent).
merge_managed_keys() {
  local canonical_file="$1" current_file="$2"
  # Kept: every current line that is NOT a managed key (comments, blanks,
  # AOIP_REDIS_URL, AOIP_AGENT_MODE, AOIP_AUDIT_LOG_PATH, etc. survive as-is).
  local kept
  kept="$(grep -v -E "${MANAGED_KEY_REGEX}" "${current_file}" || true)"
  # Managed block: only real KEY=VALUE lines from canonical (skip its
  # comments so we don't spam every VM's run.env with our doc comments).
  local managed
  managed="$(grep -E "${MANAGED_KEY_REGEX}" "${canonical_file}" || true)"
  if [[ -n "${kept}" ]]; then
    printf '%s\n' "${kept}"
  fi
  printf '%s\n' "${managed}"
}

# ── Per-VM deploy + verify ───────────────────────────────────────────────
deploy_one_vm() {
  local machine="$1"
  local tmp_current tmp_new tmp_after
  tmp_current="$(mktemp)"; tmp_new="$(mktemp)"; tmp_after="$(mktemp)"
  trap 'rm -f "${tmp_current}" "${tmp_new}" "${tmp_after}"' RETURN

  echo "== ${machine} =="
  if ! _orb_cat_run_env "${machine}" > "${tmp_current}" 2>/dev/null; then
    echo "[${machine}] FAIL — could not read ${REMOTE_RUN_ENV} (VM unreachable or file missing)"
    return 1
  fi

  merge_managed_keys "${CANONICAL_FILE}" "${tmp_current}" > "${tmp_new}"

  if diff -q <(sed -e '$a\' "${tmp_current}") <(sed -e '$a\' "${tmp_new}") > /dev/null 2>&1; then
    echo "[${machine}] run.env already up to date — skip write + restart (idempotent)"
  else
    echo "[${machine}] managed keys differ — writing run.env + restarting ${REMOTE_SERVICE}"
    _orb_write_run_env "${machine}" "$(cat "${tmp_new}")"
    if ! _orb_restart_service "${machine}"; then
      echo "[${machine}] FAIL — restart ${REMOTE_SERVICE} failed"
      return 1
    fi
    sleep 4
    if ! _orb_service_active "${machine}" | grep -q active; then
      echo "[${machine}] FAIL — ${REMOTE_SERVICE} not active after restart"
      return 1
    fi
  fi

  # ── Verify: re-read run.env, compare every managed key against canonical ──
  if ! _orb_cat_run_env "${machine}" > "${tmp_after}" 2>/dev/null; then
    echo "[${machine}] FAIL — could not re-read ${REMOTE_RUN_ENV} for verification"
    return 1
  fi

  local ok=1 key expected actual
  while IFS='=' read -r key expected; do
    [[ -z "${key}" ]] && continue
    actual="$(grep -E "^${key}=" "${tmp_after}" | tail -n1 | cut -d= -f2-)"
    if [[ "${actual}" == "${expected}" ]]; then
      echo "[${machine}]   PASS ${key}=${actual}"
    else
      echo "[${machine}]   FAIL ${key} expected=${expected} actual=${actual:-<missing>}"
      ok=0
    fi
  done < <(grep -E "${MANAGED_KEY_REGEX}" "${CANONICAL_FILE}")

  if [[ "${ok}" -eq 1 ]]; then
    echo "[${machine}] PASS — all managed keys match canonical"
    return 0
  else
    echo "[${machine}] FAIL — managed keys drifted from canonical"
    return 1
  fi
}

# ── Self-test mode: exercises merge_managed_keys() against tmp files only,
# NO orb/network calls. Used to validate parse/idempotency logic locally.
run_self_test() {
  local tmp_dir current new1 new2
  tmp_dir="$(mktemp -d)"
  trap 'rm -rf "${tmp_dir}"' RETURN
  current="${tmp_dir}/run.env.current"
  new1="${tmp_dir}/run.env.new1"
  new2="${tmp_dir}/run.env.new2"

  cat > "${current}" << 'EOF'
AOIP_REDIS_URL=redis://127.0.0.1:6379/0
AOIP_AGENT_MODE=observe_only
AOIP_AUDIT_LOG_PATH=/var/lib/aoip/audit.log
AOIP_GATE_ALLOWED_FAILURE_MODES=process_down
AOIP_GATE_ALLOWED_SUBSTRATES=systemd
AOIP_GATE_SCOPE_PREFIX=svc:
AOIP_GATE_MAX_RISK=0.3
AOIP_GATE_MIN_DIAGNOSIS_CONFIDENCE=0.5
AOIP_GATE_MAX_DIAGNOSIS_AGE_S=300
AOIP_ALLOWED_SYSTEMD_UNITS=payment-api.service
EOF

  merge_managed_keys "${CANONICAL_FILE}" "${current}" > "${new1}"

  local fails=0
  grep -q '^AOIP_REDIS_URL=redis://127.0.0.1:6379/0$' "${new1}" \
    || { echo "SELF-TEST FAIL: AOIP_REDIS_URL not preserved"; fails=1; }
  grep -q '^AOIP_AGENT_MODE=observe_only$' "${new1}" \
    || { echo "SELF-TEST FAIL: AOIP_AGENT_MODE not preserved"; fails=1; }
  grep -q '^AOIP_AUDIT_LOG_PATH=/var/lib/aoip/audit.log$' "${new1}" \
    || { echo "SELF-TEST FAIL: AOIP_AUDIT_LOG_PATH not preserved"; fails=1; }
  grep -q "^AOIP_GATE_ALLOWED_FAILURE_MODES=process_down,failed_state_stale,disk_pressure_journal\$" "${new1}" \
    || { echo "SELF-TEST FAIL: AOIP_GATE_ALLOWED_FAILURE_MODES not updated from canonical"; fails=1; }
  [[ "$(grep -c '^AOIP_GATE_ALLOWED_FAILURE_MODES=' "${new1}")" -eq 1 ]] \
    || { echo "SELF-TEST FAIL: duplicate AOIP_GATE_ALLOWED_FAILURE_MODES lines"; fails=1; }

  # Idempotency: merging again against the already-updated file must be a no-op.
  merge_managed_keys "${CANONICAL_FILE}" "${new1}" > "${new2}"
  if diff -q <(sed -e '$a\' "${new1}") <(sed -e '$a\' "${new2}") > /dev/null 2>&1; then
    echo "SELF-TEST PASS: idempotent on second merge"
  else
    echo "SELF-TEST FAIL: second merge changed content (not idempotent)"
    diff "${new1}" "${new2}" || true
    fails=1
  fi

  if [[ "${fails}" -eq 0 ]]; then
    echo "SELF-TEST: ALL PASS"
    return 0
  else
    echo "SELF-TEST: FAILURES ABOVE"
    return 1
  fi
}

usage() {
  cat << EOF
Usage: $(basename "$0") [--canonical FILE] [--self-test] [vm ...]

  vm ...           VMs to deploy to (default: \$AOIP_GATE_VMS env var, space-
                    separated, else the built-in DEFAULT_VMS list).
  --canonical FILE  Override canonical config path (default: ${CANONICAL_FILE}).
  --self-test       Run local merge/idempotency checks only — NO orb/network
                    calls, does not touch any VM. Safe to run anywhere.
EOF
}

main() {
  local vms=() self_test=0

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --canonical) CANONICAL_FILE="$2"; shift 2 ;;
      --self-test) self_test=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) vms+=("$1"); shift ;;
    esac
  done

  if [[ "${self_test}" -eq 1 ]]; then
    run_self_test
    exit $?
  fi

  if [[ ! -f "${CANONICAL_FILE}" ]]; then
    echo "FATAL: canonical config not found: ${CANONICAL_FILE}" >&2
    exit 2
  fi

  if [[ "${#vms[@]}" -eq 0 ]]; then
    if [[ -n "${AOIP_GATE_VMS:-}" ]]; then
      # shellcheck disable=SC2206
      vms=(${AOIP_GATE_VMS})
    else
      vms=("${DEFAULT_VMS[@]}")
    fi
  fi

  echo "Deploying ${CANONICAL_FILE} managed keys to: ${vms[*]}"
  local overall=0
  for vm in "${vms[@]}"; do
    if ! deploy_one_vm "${vm}"; then
      overall=1
    fi
    echo
  done

  if [[ "${overall}" -eq 0 ]]; then
    echo "RESULT: PASS — all VMs match canonical config"
  else
    echo "RESULT: FAIL — one or more VMs did not verify (see per-VM output above)"
  fi
  exit "${overall}"
}

# Allow sourcing this file (e.g. from a test harness) without running main.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main "$@"
fi
